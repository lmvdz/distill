"""Additive legacy mapping: preserve cards and timestamps without inventing captures."""

import json
import sqlite3
from pathlib import Path

from .directives import canonical, identity, source_url
from .distill_store import Registry


def migrate(store: Registry, path: Path) -> dict:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    counts, skipped = {}, []
    try:
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        with store.db:
            for table, kind in (("cards", "technique"), ("workflow_cards", "workflow")):
                counts[kind] = 0
                if table not in tables:
                    continue
                for row in connection.execute(
                    f"SELECT c.*,v.url FROM {table} c"
                    " JOIN videos v ON c.video_id=v.id ORDER BY c.id"
                ):
                    try:
                        url = source_url(row["url"])
                    except ValueError:
                        skipped.append(
                            {"table": table, "row": row["id"], "reason": "non-HTTP source"}
                        )
                        continue
                    original = {**dict(row), "body": json.loads(row["body"])}
                    source = identity("source", url)
                    ident = identity("legacy", [kind, url, original["body"]])
                    body = {
                        "original": original,
                        "source": source,
                        "kind": kind,
                        "evidence": "legacy-interpretation; no fresh verification performed",
                        "retention": "saved responses and cited locators; "
                        "original media may be absent",
                    }
                    store.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (source, url))
                    cur = store.db.execute(
                        "INSERT OR IGNORE INTO legacy_entities VALUES (?,?,?,?)",
                        (ident, source, kind, canonical(body)),
                    )
                    counts[kind] += cur.rowcount
                    store.db.execute(
                        "INSERT OR REPLACE INTO legacy_aliases VALUES (?,?,?,?,?)",
                        (str(path.resolve()), kind, row["id"], ident, canonical(original)),
                    )
    finally:
        connection.close()
    return {
        "new_legacy_entities": counts,
        "skipped": skipped,
        "policy": "additive mapping; existing queue, catalogs, media and evidence unchanged",
    }
