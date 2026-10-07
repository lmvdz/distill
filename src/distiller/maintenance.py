"""Sequential fair scheduling with shared acquisition and persisted resource budgets."""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from .directives import identity, source_url
from .distill_store import Registry
from .engine import execute, metadata_gate
from .library import Capture, ChangeBatch, FixtureAdapter, LibraryItem, SQLiteAdapter, TextSpan
from .papers import fetch_page
from .publication import export

log = logging.getLogger("distill")


def capture_pages(pages: list[dict], origin: str) -> Capture:
    text, spans = "", []
    for page in pages:
        remaining = 120000 - len(text)
        if remaining <= 2:
            break
        if text:
            text += "\n\n"
        start = len(text)
        text += page["text"][: 120000 - start]
        if len(text) > start:
            spans.append(
                TextSpan(
                    start=start,
                    end=len(text),
                    locator=page.get("locator", "bounded HTTP text"),
                    url=page["url"],
                )
            )
    modality = "pdf" if any("PDF page" in s.locator for s in spans) else "html"
    return Capture(
        text=text,
        locator="bounded HTTP text; see page spans",
        origin=origin,
        modality=modality,
        spans=spans,
    )


async def shared_capture(store: Registry, client, url: str, refresh_seconds: int) -> Capture:
    source = identity("source", source_url(url))
    token = None
    for _ in range(600):
        token = store.acquire_source(source)
        if token:
            break
        await asyncio.sleep(0.1)
    if not token:
        raise ValueError("shared source acquisition still in progress; retry later")
    try:
        return await read_or_fetch(store, client, source, url, refresh_seconds, token)
    finally:
        store.release_source(source, token)


async def read_or_fetch(store, client, source: str, url: str, refresh_seconds: int, token: str):
    row = store.db.execute(
        "SELECT body,created FROM observations WHERE source=?"
        " AND json_extract(body,'$.modality')!='catalog'"
        " ORDER BY created DESC,id LIMIT 1",
        (source,),
    ).fetchone()
    checked = store.db.execute(
        "SELECT checked FROM source_checks WHERE source=?", (source,)
    ).fetchone()
    last_check = checked[0] if checked else row["created"] if row else 0
    if row and time.time() - last_check < refresh_seconds:
        body = json.loads(row["body"])
        return Capture.model_validate({k: v for k, v in body.items() if k != "source"})
    pages = await asyncio.wait_for(fetch_page(client, url), timeout=120)
    capture = capture_pages(pages, url)
    with store.db:
        lease = store.db.execute(
            "SELECT token FROM source_leases WHERE source=?", (source,)
        ).fetchone()
        if not lease or lease[0] != token:
            raise ValueError("shared acquisition lease lost")
        store.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (source, source_url(url)))
        store.observe(source, capture)
        store.db.execute("INSERT OR REPLACE INTO source_checks VALUES (?,?)", (source, time.time()))
    return capture


def poll_libraries(store: Registry) -> dict[str, str]:
    failures = {}
    for row in store.db.execute(
        "SELECT library,body FROM adapter_configs ORDER BY library"
    ).fetchall():
        library, config = row["library"], json.loads(row["body"])
        try:
            if config["kind"] == "fixture":
                adapter = FixtureAdapter(Path(config["path"]))
                batch = adapter.changes(store.cursor(library))
                if batch.library != library:
                    raise ValueError("configured adapter library identity changed")
            else:
                adapter = SQLiteAdapter(
                    Path(config["path"]),
                    library,
                    config["query"],
                    full_snapshot=config["full_snapshot"],
                    max_items=config["max_items"],
                )
                batch = adapter.changes(store.cursor(library))
            store.import_batch(batch)
        except Exception as err:
            failures[library] = f"library polling failed: {type(err).__name__}: {err}"
    return failures


async def acquire(store: Registry, key: str):
    """Collect URL-only library items and explicit URLs, without changing library metadata."""
    directive = store.directive(key)
    if directive.unresolved or directive.unsupported:
        raise ValueError(
            "resolve the directive's unsupported/unresolved settings before acquisition"
        )
    candidates = [c for c in store.candidates(directive) if metadata_gate(c, directive)[0]]
    explicit_new = set(directive.discovery.urls) - {c["url"] for c in candidates}
    if len(candidates) + len(explicit_new) > directive.limits.max_candidates:
        raise ValueError("candidate budget exceeded before source acquisition")
    needed = {
        c["url"]
        for c in candidates
        if c["metadata"]["source_type"] not in {"video", "catalog"}
        and store.db.execute(
            "SELECT observation FROM item_revisions WHERE id=?", (c["item_revision"],)
        ).fetchone()[0]
        is None
    }
    items = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=40) as client:
        for url in sorted(needed | set(directive.discovery.urls)):
            capture = await shared_capture(store, client, url, directive.discovery.refresh_seconds)
            if url in directive.discovery.urls:
                source = identity("source", source_url(url))
                items.append(
                    LibraryItem(
                        external_id=source,
                        revision=identity("capture", capture.model_dump()),
                        url=url,
                        title=url,
                        source_type=capture.modality,
                        capture=capture,
                    )
                )
    # Empty full snapshots withdraw URLs removed from a revised discovery directive.
    store.import_batch(
        ChangeBatch(
            library=f"discovery:{key}", adapter_version="http-v1", full_snapshot=True, items=items
        )
    )


async def cycle(
    store: Registry,
    model,
    root: Path,
    *,
    limit: int = 10,
    shared_cap: int = 500,
    model_name: str | None = None,
) -> list[dict]:
    if limit < 1 or shared_cap < 1:
        raise ValueError("maintenance limits must be positive")
    results = []
    failures = poll_libraries(store)
    for key in store.due()[:limit]:
        directive = store.directive(key)
        try:
            for library, message in failures.items():
                if store.uses_library(directive, library):
                    raise ValueError(message)
            if model_name is not None and directive.model != model_name:
                raise ValueError(
                    f"directive requires model {directive.model}; configured {model_name}"
                )
            await acquire(store, key)
            manifest = await execute(store, key, model, shared_cap=shared_cap, force=False)
            path = export(manifest, root)
            results.append(
                {
                    "directive": key,
                    "status": "complete",
                    "run": manifest["id"],
                    "gallery": str(path),
                }
            )
        except Exception as err:
            message = f"{type(err).__name__}: {err}"
            with store.db:
                store.db.execute(
                    "UPDATE directives SET error=?,next_run=?,last_started=? WHERE key=?",
                    (message, time.time() + directive.limits.retry_seconds, time.time(), key),
                )
            log.warning("directive %s: %s", key, message)
            results.append({"directive": key, "status": "failed", "error": message})
    return results


async def maintain(
    store: Registry,
    model,
    root: Path,
    stop: Path,
    *,
    once: bool = False,
    interval: int = 30,
    limit: int = 10,
    shared_cap: int = 500,
    model_name: str | None = None,
):
    if interval < 1:
        raise ValueError("maintenance interval must be positive")
    while not stop.exists():
        results = await cycle(
            store, model, root, limit=limit, shared_cap=shared_cap, model_name=model_name
        )
        if once:
            return results
        for _ in range(interval):
            if stop.exists():
                break
            await asyncio.sleep(1)
    return []
