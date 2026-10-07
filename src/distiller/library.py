"""Read-only source library adapter contract and a revisioned JSON fixture adapter."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .directives import Contract, Tag, identity, source_url


class TextSpan(Contract):
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    locator: str
    url: str


class Capture(Contract):
    text: str = Field(min_length=1, max_length=120000)
    locator: str = "bounded text"
    origin: str = Field(min_length=1)
    modality: Literal["text", "html", "pdf", "video", "catalog"] = "text"
    upstream: list[str] = Field(default_factory=list)
    # Video captures contain observed transcript text, never proof of video delivery.
    retention: Literal["bounded-text", "external-media"] = "bounded-text"
    spans: list[TextSpan] = Field(default_factory=list, max_length=150)
    lineage: list[dict] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_spans(self):
        for span in self.spans:
            if not 0 <= span.start < span.end <= len(self.text):
                raise ValueError("capture spans must point inside preserved text")
        return self


class LibraryItem(Contract):
    external_id: str = Field(min_length=1, max_length=500)
    revision: str = Field(min_length=1, max_length=500)
    url: str
    title: str = Field(min_length=1, max_length=2000)
    authors: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    source_type: Literal["text", "html", "pdf", "video", "catalog"] = "text"
    capture: Capture | None = None
    removed: bool = False

    @field_validator("url")
    @classmethod
    def url_valid(cls, value: str) -> str:
        return source_url(value)

    @model_validator(mode="after")
    def capture_type(self):
        if self.capture and self.capture.modality != self.source_type:
            raise ValueError("capture modality must match source type")
        return self


class ChangeBatch(Contract):
    library: str = Field(min_length=1, max_length=200)
    adapter_version: str = "fixture-v1"
    cursor: str | None = None
    full_snapshot: bool = False
    items: list[LibraryItem] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique_items(self):
        if len({i.external_id for i in self.items}) != len(self.items):
            raise ValueError("a change batch must contain unique external item IDs")
        return self


class LibraryAdapter(Protocol):
    def capabilities(self) -> dict: ...
    def changes(self, cursor: str | None) -> ChangeBatch: ...


class FixtureAdapter:
    def __init__(self, path: Path):
        self.path = path

    def capabilities(self) -> dict:
        return {
            "version": "fixture-v1",
            "read_only": True,
            "captures": True,
            "polling": True,
            "full_snapshot": True,
            "writeback": False,
        }

    def changes(self, cursor: str | None = None) -> ChangeBatch:
        body = json.loads(self.path.read_text(encoding="utf-8"))
        batch = ChangeBatch.model_validate(body)
        batch.cursor = batch.cursor or identity("cursor", body)
        if cursor == batch.cursor:
            batch.items = []
            batch.full_snapshot = False
        return batch


class SQLiteAdapter:
    """Read-only SQL projection. Each row exposes an item_json matching LibraryItem.

    An application can provide a view or a SELECT with json_object to adapt its schema.
    Snapshot queries must return the entire bounded projection, including withdrawn rows
    if desired. Incremental queries use :cursor and return ordered change_cursor values.
    No capture is inferred from a bookmark preview or title.
    """

    def __init__(
        self,
        path: Path,
        library: str,
        query: str,
        *,
        full_snapshot: bool = True,
        max_items: int = 10000,
    ):
        if not 1 <= max_items <= 10000:
            raise ValueError("SQL import bound must be 1..10000 items")
        self.path, self.library, self.query = path.resolve(), library, query
        self.full_snapshot, self.max_items = full_snapshot, max_items

    def capabilities(self) -> dict:
        return {
            "version": "sqlite-v1",
            "read_only": True,
            "captures": True,
            "polling": True,
            "full_snapshot": self.full_snapshot,
            "writeback": False,
            "incremental_cursor": not self.full_snapshot,
        }

    def changes(self, cursor: str | None = None) -> ChangeBatch:
        # Both URI mode and query_only protect upstream writes, including query mistakes.
        uri = self.path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            try:
                rows = connection.execute(self.query, {"cursor": cursor}).fetchmany(
                    self.max_items + 1
                )
            except sqlite3.Error as err:
                raise ValueError(f"read-only library query failed: {err}") from err
        if len(rows) > self.max_items:
            raise ValueError(
                "SQL library exceeds import bound; narrow the query (no partial snapshot)"
            )
        columns = set(rows[0].keys()) if rows else set()
        if rows and "item_json" not in columns:
            raise ValueError("SQL query must project an item_json column")
        items = [LibraryItem.model_validate_json(row["item_json"]) for row in rows]
        if self.full_snapshot:
            next_cursor = identity("cursor", [i.model_dump() for i in items])
        else:
            if rows and "change_cursor" not in columns:
                raise ValueError("incremental query must project ordered change_cursor values")
            next_cursor = str(rows[-1]["change_cursor"]) if rows else cursor
        unchanged = self.full_snapshot and cursor == next_cursor
        return ChangeBatch(
            library=self.library,
            adapter_version="sqlite-v1",
            cursor=next_cursor,
            full_snapshot=self.full_snapshot and not unchanged,
            items=[] if unchanged else items,
        )
