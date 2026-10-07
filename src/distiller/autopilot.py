"""Discovery -> evidence -> curation -> distillation -> catalog and search, until stopped."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

from . import papers
from .automation_store import Store
from .catalog import publish
from .config import Settings
from .db import Queue
from .frontier import (
    arxiv_bundles,
    configured_bundles,
    eccv_bundles,
    feed_url,
    load_plan,
    page_bundles,
)
from .merge import merge, resolve_compositions
from .model import Busy, Model
from .research import video_candidate
from .retrieval import Encoder, Index, documents
from .runner import run as run_videos
from .workflow_catalog import publish as publish_workflows

log = logging.getLogger("distill")


def cycle_report(store: Store) -> dict | None:
    """Show a later index recovery without rewriting the historical cycle record."""
    report = store.get("last_cycle")
    recovery = store.get("index:recovery", {})
    if (
        report
        and report.get("search", {}).get("status") == "error"
        and recovery.get("status") == "successful"
        and recovery.get("completed_at", 0) > report.get("finished_at", 0)
    ):
        report["search"] = {
            "status": "recovered",
            "documents": recovery["documents"],
            "failed_at": report["finished_at"],
            "recovered_at": recovery["completed_at"],
            "reason": recovery.get("reason"),
            "historical_details": "autopilot-report.json",
        }
    return report


@contextlib.contextmanager
def exclusive(path: Path):
    """One orchestrator per data directory; OS releases this lock even after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            raise ValueError("an autopilot already owns this data directory") from err
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def enqueue_videos(q: Queue, sources: list[str], title: str | None = None) -> int:
    added = 0
    for url in sources:
        found = video_candidate(url)
        if found:
            site, ident, canonical = found
            added += q.enqueue(
                site=site,
                site_id=ident,
                url=canonical,
                title=title,
                seed="frontier:primary-source",
                priority=15,
            )
    return added


async def discover(plan: dict, store: Store, q: Queue, directory: Path) -> dict:
    added, videos, errors = 0, 0, []
    for item in configured_bundles(plan):
        videos += enqueue_videos(q, [s["url"] for s in item["sources"]], item["title"])
        if any(not video_candidate(s["url"]) for s in item["sources"]):
            added += store.put(item)
    async with httpx.AsyncClient(follow_redirects=True, timeout=45) as client:
        for feed in plan.get("feed", []):
            url = feed_url(feed)
            key = "feed:" + url
            state = store.get(key, {})
            if state.get("next_at", 0) > time.time():
                continue
            try:
                if feed["kind"] == "page":
                    pages = await papers.fetch_page(client, url)
                    videos += enqueue_videos(
                        q, [link["url"] for p in pages for link in p.get("links", [])]
                    )
                    items = page_bundles(pages, feed)
                else:
                    response = await client.get(url)
                    response.raise_for_status()
                    text = response.text
                    if len(text) > 20_000_000:
                        raise ValueError("feed exceeds 20 MB")
                    items = (arxiv_bundles if feed["kind"] == "arxiv" else eccv_bundles)(text, feed)
                    directory.mkdir(parents=True, exist_ok=True)
                    filename = hashlib.sha256(url.encode()).hexdigest()[:16] + ".txt"
                    (directory / filename).write_text(text, encoding="utf-8")
                for item in items:
                    added += store.put(item)
                store.set(
                    key,
                    {
                        "next_at": time.time() + int(feed.get("every_hours", 24)) * 3600,
                        "found": len(items),
                        "error": None,
                        "checked_at": time.time(),
                    },
                )
            except Exception as err:
                message = f"{type(err).__name__}: {err}"
                log.warning("frontier feed %s failed: %s", url, message[:200])
                store.set(key, {"next_at": time.time() + 3600, "error": message[:1000]})
                errors.append({"url": url, "error": message[:1000]})
            await asyncio.sleep(3)
    return {"bundles_added": added, "videos_added": videos, "errors": errors}


def comparisons(q: Queue, item: dict, data: Path, semantic: bool = True) -> list[dict]:
    """Retrieve method comparisons. Similarity never decides novelty."""
    selected = []
    if semantic and (data / "search.sqlite").exists():
        index = Index(data / "search.sqlite")
        try:
            settings = index.settings()
            encoder = Encoder(settings["dimensions"], settings["images"])
            if settings["signature"] == encoder.signature:
                selected = index.search(
                    "task: search result | query: " + item["title"],
                    encoder,
                    6,
                    "technique",
                    accept=lambda doc: doc.get("research_id") != item["id"],
                )
            else:
                log.info(
                    "search index needs migration; using lexical method comparisons this cycle"
                )
        finally:
            index.close()
    if not selected:
        tokens = set(re.findall(r"[a-z]{3,}", item["title"].lower()))
        ranked = sorted(
            [r for r in q.cards() if r["body"].get("research_bundle", {}).get("id") != item["id"]],
            key=lambda r: len(
                tokens
                & set(
                    re.findall(
                        r"[a-z]{3,}", (r["body"]["technique"] + " " + r["body"]["idea"]).lower()
                    )
                )
            ),
            reverse=True,
        )
        selected = [
            {
                "id": f"technique:{r['id']}",
                "title": r["body"]["technique"],
                "text": json.dumps(r["body"]),
            }
            for r in ranked[:6]
        ]
    return [{"id": r["id"], "title": r["title"], "text": r["text"][:8000]} for r in selected]


async def heartbeat(store: Store, job: dict) -> None:
    while True:
        await asyncio.sleep(120)
        if not store.renew(job):
            raise RuntimeError("research lease lost")


async def service_heartbeat(store: Store) -> None:
    while True:
        await asyncio.sleep(60)
        service = store.get("service")
        store.set("service", {**service, "heartbeat_at": time.time()})


async def work(
    s: Settings, store: Store, q: Queue, model: Model, job: dict, semantic: bool
) -> dict:
    item = job["body"]
    beats = asyncio.create_task(heartbeat(store, job))
    try:
        research_key = "research:" + item["id"]
        research = store.get(research_key)
        signature = hashlib.sha256(
            json.dumps([item["sources"], item.get("version")]).encode()
        ).hexdigest()
        if (
            not research
            or research.get("source_signature") != signature
            or research.get("collected_at", 0) < time.time() - 7 * 86400
        ):
            research = await papers.collect(item, s.data / "research")
            research.update(source_signature=signature, collected_at=time.time())
            store.set(research_key, research)
        related = await asyncio.to_thread(comparisons, q, item, s.data, semantic)
        result = await papers.distill(item, research, related, q, model, s.text_model)
        if not store.renew(job):
            raise RuntimeError("research lease lost before committing cards")
        if result["status"] == "carded":
            papers.store_cards(q, item, result)
        enqueue_videos(q, [link["url"] for p in research["pages"] for link in p.get("links", [])])
        store.finish(job, result["status"], result)
        log.info(
            "frontier: %s -> %s (%d cards): %s",
            item["title"],
            result["status"],
            len(result["cards"]),
            result["reason"][:150],
        )
        return {
            "id": item["id"],
            "title": item["title"],
            "status": result["status"],
            "cards": len(result["cards"]),
            "reason": result["reason"],
        }
    except asyncio.CancelledError:
        store.fail(job, "interrupted; resumable model answers preserved", busy=True)
        raise
    except Exception as err:
        capacity = isinstance(err, Busy) or "out of memory" in str(err).lower()
        store.fail(job, f"{type(err).__name__}: {err}", busy=capacity)
        log.warning("frontier job %s failed: %s", item["title"], str(err)[:200])
        return {"id": item["id"], "status": "error", "error": str(err)[:1000]}
    finally:
        beats.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beats


def catalog_fingerprint(q: Queue) -> str:
    payload = [q.cards(), q.workflow_cards(), q.compositions(), q.styles(), q.audits()]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def refresh_catalog(s: Settings, store: Store, q: Queue, model: Model) -> dict:
    digest = catalog_fingerprint(q)
    if store.get("catalog:fingerprint") == digest and (s.catalog_dir / "catalog.json").exists():
        return {"status": "current"}
    expected = {r["id"] for r in q.cards() if "produces" in r["body"]}
    entries = await merge(q, model)
    covered = {i for e in entries for i in e["card_ids"]}
    if not expected <= covered:
        raise RuntimeError(
            f"catalog incomplete: {len(expected - covered)} cards omitted; retrying later"
        )
    observed, references = resolve_compositions(q.compositions(), entries)
    # Publish only a complete build. Model failures cannot replace a catalog with a partial one.
    publish(s.catalog_dir, entries, observed, references, q.styles())
    rows = q.workflow_cards()
    if rows:
        publish_workflows(s.workflow_catalog_dir, rows)
    store.set("catalog:fingerprint", digest)
    return {"status": "published", "techniques": len(entries), "workflows": len(rows)}


def refresh_index(s: Settings) -> dict:
    index = Index(s.data / "search.sqlite")
    try:
        try:
            settings = index.settings()
        except ValueError:
            settings = {"dimensions": 256, "images": False}
        docs = documents(Queue(s.db_path), s.data / "research")
        changed = index.build(
            docs, s.catalog_dir, Encoder(settings["dimensions"], settings["images"])
        )
        return {"documents": len(docs), "reembedded": changed}
    finally:
        index.close()


async def cycle(
    s: Settings,
    plan: dict,
    store: Store,
    q: Queue,
    model: Model,
    limit: int,
    daily_cap: int,
    semantic: bool,
) -> dict:
    report = {
        "started_at": time.time(),
        "discovery": await discover(plan, store, q, s.data / "feeds"),
        "research": [],
    }
    for _ in range(limit):
        if (s.data / "autopilot.stop").exists():
            break
        job = store.claim(daily_cap)
        if not job:
            break
        store.set(
            "active_job", {"id": job["id"], "title": job["body"]["title"], "started": time.time()}
        )
        report["research"].append(await work(s, store, q, model, job, semantic))
        store.set("active_job", None)
    for stage, fn in (
        ("catalog", lambda: refresh_catalog(s, store, q, model)),
        ("search", lambda: asyncio.to_thread(refresh_index, s)),
    ):
        if stage == "search" and not semantic:
            report[stage] = {"status": "disabled"}
            continue
        try:
            report[stage] = await fn()
        except Exception as err:
            log.warning("autopilot %s failed: %s", stage, str(err)[:200])
            report[stage] = {"status": "error", "error": str(err)[:1000]}
    report["finished_at"] = time.time()
    store.set("last_cycle", report)
    (s.data / "autopilot-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


async def run(
    s: Settings,
    plan_path: Path,
    seeds: Path,
    *,
    once: bool = False,
    limit: int = 2,
    daily_cap: int = 50,
    interval: int = 1800,
    semantic: bool = True,
    external_video_worker: bool = False,
) -> None:
    if min(limit, daily_cap, interval) < 1:
        raise ValueError("limits, daily cap and interval must be positive")
    plan = load_plan(plan_path)
    with exclusive(s.data / "autopilot.lock"):
        store, q, model = Store(s.data / "automation.sqlite"), Queue(s.db_path), Model(s)
        video_task = None
        stop_file = s.data / "autopilot.stop"
        stop_file.unlink(missing_ok=True)
        store.set(
            "service",
            {
                "pid": os.getpid(),
                "started_at": time.time(),
                "heartbeat_at": time.time(),
                "status": "running",
                "daily_cap": daily_cap,
                "interval": interval,
                "external_video_worker": external_video_worker,
                "once": once,
                "text_model": s.text_model,
                "search": semantic,
            },
        )
        pulse = asyncio.create_task(service_heartbeat(store))
        try:
            while not stop_file.exists():
                if not external_video_worker and video_task is None:
                    video_task = asyncio.create_task(
                        run_videos(s, seeds, once=once, limit=2 if once else None)
                    )
                report = await cycle(s, plan, store, q, model, limit, daily_cap, semantic)
                print(json.dumps(report, indent=2), flush=True)
                if once:
                    await finish_videos(video_task, s, store, q, model, semantic)
                    break
                # Poll stop requests while waiting; a finished/crashed video runner is observable.
                until = time.time() + interval
                while time.time() < until and not stop_file.exists():
                    if video_task and video_task.done():
                        error = video_task.exception()
                        store.set(
                            "video_worker",
                            {
                                "status": "error" if error else "stopped",
                                "error": str(error)[:1000] if error else None,
                            },
                        )
                        if error:
                            log.warning("video worker stopped: %s", str(error)[:200])
                        video_task = None
                    await asyncio.sleep(min(10, max(0, until - time.time())))
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse
            if video_task:
                video_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await video_task
            service = store.get("service")
            store.set("service", {**service, "status": "stopped", "stopped_at": time.time()})
            await model.close()
            store.close()


async def finish_videos(video_task, s, store, q, model, semantic) -> None:
    if video_task:
        await video_task
        await refresh_catalog(s, store, q, model)
        if semantic:
            await asyncio.to_thread(refresh_index, s)
