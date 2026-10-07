"""`distill` command line."""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from . import autopilot, config, documentation, knowledge_cli
from .audit import audit, report
from .automation_store import Store
from .catalog import publish
from .db import Queue
from .jev_gateway import load_keys
from .media import SiteGates, discover
from .merge import merge, resolve_compositions
from .model import Model
from .previews import backfill
from .research import capture, capture_http, enqueue_captures
from .retrieval import DIMENSIONS, Encoder, Index, documents
from .runner import canary, load_seeds, refill, run
from .studies import import_study
from .workflow_catalog import publish as publish_workflows


def _add(s: config.Settings, urls: list[str]) -> None:
    q = Queue(s.db_path)
    for url in urls:
        found = discover(url, 500, s.cookies_browser)
        added = sum(
            q.enqueue(
                site=f.site,
                site_id=f.site_id,
                url=f.url,
                title=f.title,
                duration=f.duration,
                seed=url,
            )
            for f in found
        )
        print(f"{url}: {len(found)} found, {added} new")


async def _canary(s: config.Settings) -> None:
    model = Model(s)
    try:
        await canary(s, model)
        print("canary passed: the model receives video")
    finally:
        await model.close()


async def _catalog(s: config.Settings) -> None:
    model = Model(s)
    try:
        q = Queue(s.db_path)
        entries = await merge(q, model)
        observed, references = resolve_compositions(q.compositions(), entries)
        styles = q.styles()
        logging.getLogger("distill").info("catalog: writing files")
        publish(s.catalog_dir, entries, observed, references, styles)
        workflows = _workflows(s, q)
        with_media = sum(1 for e in entries if e.get("media"))
        print(
            f"{len(entries)} techniques ({with_media} with previews), {len(observed)} "
            f"compositions, {len(references)} references, {len(styles)} styles -> {s.catalog_dir}"
        )
        print(f"  for you:  {s.catalog_dir / 'index.html'}")
        print(f"  for LLMs: {s.catalog_dir / 'llms.txt'}")
        if workflows:
            print(f"{workflows} workflows -> {s.workflow_catalog_dir}")
    finally:
        await model.close()


def _workflows(s: config.Settings, q: Queue) -> int:
    """Builds the workflow catalog (its own folder, no model needed); returns its card count."""
    rows = q.workflow_cards()
    if rows:
        publish_workflows(s.workflow_catalog_dir, rows)
    return len(rows)


async def _audit(s: config.Settings, args: argparse.Namespace) -> None:
    q = Queue(s.db_path)
    if not args.report:
        if s.text_model == s.model:
            logging.getLogger("distill").warning(
                "auditing with the video model %s; --provider claude or codex gives a second "
                "opinion from a stronger one",
                s.model,
            )
        model = Model(s)
        try:
            done, failed, current, left = await audit(
                q, model, s.text_model, force=args.force, limit=args.limit
            )
        finally:
            await model.close()
        print(
            f"{done} cards audited, {failed} failed (retried next run), {current} already "
            f"audited, {left} left for a later run"
        )
    flagged = report(q)
    for n in flagged if args.report else []:
        print(f"[{n['verdict']}] {n['technique']} (card {n['card']}, {n['claim']}) {n['url']}")
        print(f"  claim:  {n['text']}")
        print(f"  why:    {n['reason']}")
        if n.get("correction"):
            print(f"  should: {n['correction']}")
    print(f"{len(flagged)} claims flagged; `distill catalog` shows them as audit notes")


def _import(s: config.Settings, study_dir: Path) -> None:
    q = Queue(s.db_path)
    result = import_study(study_dir, q, s.catalog_dir)
    print(
        f"{result.source_id}: {result.projects} projects, {result.kept} cards kept, "
        f"{result.dropped} dropped"
    )
    for reason in result.reasons:
        print(f"  dropped: {reason}")


def _recard(s: config.Settings, only: str | None) -> None:
    ids = Queue(s.db_path).requeue_for_new_forms(only)
    for video_id in ids:
        shutil.rmtree(s.catalog_dir / "media" / video_id.replace(":", "_"), ignore_errors=True)
    print(f"{len(ids)} videos queued to be watched again with the current forms")


def _previews(s: config.Settings) -> None:
    made = backfill(Queue(s.db_path), s.work_dir, s.catalog_dir, s.cookies_browser)
    print(f"{made} previews cut; run `distill catalog` to show them")


async def _discover(s: config.Settings, seeds: Path, only: str | None) -> None:
    urls, discourse = load_seeds(seeds)
    if only:
        urls = [u for u in urls if re.search(only, u.url, re.IGNORECASE)]
        discourse = [d for d in discourse if re.search(only, d.base, re.IGNORECASE)]
    await refill(s, Queue(s.db_path), SiteGates(), urls, discourse)


def _sources(s: config.Settings) -> None:
    rows = Queue(s.db_path).sources()
    if not rows:
        print("no source history yet")
        return
    header = f"{'kind':8} {'watched':>7} {'carded':>6} {'value':>6} {'yield':>6} {'x':>5}  source"
    print(header)
    for r in rows:
        flag = " PAUSED" if r["paused"] else ""
        print(
            f"{r['kind']:8} {r['watched']:>7} {r['carded']:>6} {r['value']:>6.2f}"
            f" {r['yield']:>6.2f} {r['multiplier']:>5.2f}  {r['source']}{flag}"
        )


def _index(s: config.Settings, args: argparse.Namespace) -> None:
    index = Index(s.data / "search.sqlite")
    try:
        docs = documents(Queue(s.db_path), s.data / "research")
        changed = index.build(docs, s.catalog_dir, Encoder(args.dimensions, args.images))
        print(
            f"{len(docs)} searchable documents, {changed} re-embedded -> {s.data / 'search.sqlite'}"
        )
    finally:
        index.close()


def _search(s: config.Settings, args: argparse.Namespace) -> None:
    index = Index(s.data / "search.sqlite")
    try:
        settings = index.settings()
        if args.image:
            if not settings["images"]:
                raise ValueError("Image search needs an index built with `distill index --images`.")
            if not args.image.is_file():
                raise ValueError(f"Image not found: {args.image}")
            query = {"image": str(args.image.resolve())}
        elif args.query and args.query.strip():
            query = "task: search result | query: " + args.query.strip()
        else:
            raise ValueError("Supply a text query or --image <path>.")
        encoder = Encoder(settings["dimensions"], settings["images"])
        results = index.search(query, encoder, args.limit, args.kind)
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            for r in results:
                print(f"{r['score']:.3f} [{r['id']}; {r['evidence']}] {r['title']}")
                print(f"  {r['url']}")
                if r["times"]:
                    print("  source seconds: " + ", ".join(str(t) for t in r["times"]))
                print(f"  {r['text'][:240]}")
            print(f"{len(results)} matches; similarity is a ranking score, not confidence")
    finally:
        index.close()


def _research(s: config.Settings, args: argparse.Namespace) -> None:
    if args.enqueue and args.limit < 1:
        raise ValueError("discovery limit must be positive")
    if args.http:
        path = capture_http(args.url, s.data / "research")
    elif args.goal:
        load_keys(args.keys_file)
        if args.cdp_url:
            os.environ["BU_CDP_URL"] = args.cdp_url
        path = capture(args.url, args.goal, s.data / "research", args.max_steps, settings=s)
    else:
        raise ValueError("research needs --goal for Jev, or --http for a static HTML page")
    result = json.loads(path.read_text(encoding="utf-8"))
    print(f"{len(result['pages'])} observed pages ({result['status']}) -> {path}")
    print("Unverified source text; run `distill index` to include it in search.")
    if args.enqueue:
        _discover_web(s, args.limit)


def _discover_web(s: config.Settings, limit: int) -> None:
    found, added = enqueue_captures(Queue(s.db_path), s.data / "research", limit)
    print(f"{found} observed video links, {added} queued; `distill run` screens and verifies them")


async def _check_docs(s: config.Settings, args: argparse.Namespace) -> None:
    q = Queue(s.db_path)
    if args.report:
        results = documentation.report(q, s.data / "doc-checks")
    else:
        if not args.card or not args.docs_host:
            raise ValueError("check-docs requires --card and --docs-host (or --report)")
        index = Index(s.data / "search.sqlite")
        model = Model(s)
        try:
            settings = index.settings()
            result = await documentation.check(
                q,
                index,
                Encoder(settings["dimensions"], settings["images"]),
                model,
                s.text_model,
                args.card,
                args.docs_host,
                s.data / "doc-checks",
                source_limit=args.sources,
                max_age_days=args.max_age_days,
                force=args.force,
            )
            results = [result]
        finally:
            index.close()
            await model.close()
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    for result in results:
        stale = " (card changed; re-check needed)" if result.get("stale_card") else ""
        if result.get("stale_sources"):
            stale += " (documentation captures expired; collect fresh sources)"
        print(
            f"{result['card']} against observed documentation, "
            f"checked {result['checked_at']}{stale}"
        )
        for check in result["checks"]:
            print(f"  [{check['verdict']}] {check['claim']}: {check['reason']}")
            if check["source_url"]:
                print(f"    {check['source_url']}")
    print(f"Model assessments saved in {s.data / 'doc-checks'}; original cards are preserved.")


def _model_flags() -> argparse.ArgumentParser:
    """Flags every command takes; they override DISTILL_TEXT_MODEL and DISTILL_MODEL."""
    m = argparse.ArgumentParser(add_help=False)
    g = m.add_argument_group("models")
    g.add_argument(
        "--provider",
        choices=config.PROVIDERS,
        help="where steps without video run (prescreen, grouping, entry text)",
    )
    g.add_argument("--model", help="model for those steps, e.g. opus or gpt-6.1-sol")
    g.add_argument("--video-model", help="OmniRoute model for the steps that watch video")
    return m


def _only_flag(p: argparse.ArgumentParser, what: str) -> None:
    p.add_argument(
        "--only", metavar="REGEX", help=f"{what} only those whose seed, channel or url matches"
    )


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="distill", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    flags = _model_flags()

    def command(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[flags])

    a = command("add", "queue videos, channels, playlists or profiles")
    a.add_argument("urls", nargs="+")
    i = command("import", "import a studied source (cards.json, style.md, shots/)")
    i.add_argument("dir", type=Path)
    d = command("discover", "queue new videos from every seed")
    d.add_argument("--seeds", type=Path, default=Path("seeds.toml"))
    d.add_argument("--only", metavar="REGEX", help="list only seeds whose url matches")
    r = command("run", "watch videos until stopped")
    r.add_argument("--seeds", type=Path, default=Path("seeds.toml"))
    r.add_argument("--workers", type=int)
    r.add_argument("--once", action="store_true", help="stop when the queue is empty")
    _only_flag(r, "lease")
    r.add_argument("--limit", type=int, help="stop after starting this many videos")
    command("catalog", "build the catalog (llms.txt, index.html) from all cards")
    command("merge", "same as catalog")
    command("workflows", "build only the workflow catalog (data/catalog-workflows)")
    command("previews", "cut missing thumbnails and clips for carded videos")
    command("status", "queue and card counts")
    command("canary", "check the model receives video")
    _only_flag(command("retry", "put failed and stuck leased videos back in the queue"), "re-queue")
    _only_flag(command("recard", "re-watch finished videos with the current forms"), "re-queue")
    au = command("audit", "judge every card's claims with the text model, without the video")
    au.add_argument("--report", action="store_true", help="list flagged claims, audit nothing")
    au.add_argument("--force", action="store_true", help="re-audit cards already audited")
    au.add_argument("--limit", type=int, help="audit at most this many cards")
    command("sources", "watched/carded/value/yield per seed and channel")
    knowledge_cli.add_arguments(command("knowledge", "versioned general directives and catalogs"))
    ix = command("index", "index cards and unverified browser research for local search")
    ix.add_argument("--dimensions", type=int, choices=DIMENSIONS, default=256)
    ix.add_argument("--images", action="store_true", help="also embed existing thumbnails")
    se = command("search", "semantic search with source timestamps (needs the search extra)")
    query = se.add_mutually_exclusive_group()
    query.add_argument("query", nargs="?")
    query.add_argument("--image", type=Path, help="find techniques by a reference image")
    se.add_argument("--limit", type=int, default=10)
    se.add_argument("--kind", choices=("technique", "workflow", "web"))
    se.add_argument("--json", action="store_true")
    re_search = command("research", "browse with Jev and save observed text (needs research extra)")
    re_search.add_argument("url")
    re_search.add_argument("--goal")
    re_search.add_argument(
        "--cdp-url", help="connect to an explicitly supplied local Chrome CDP endpoint"
    )
    re_search.add_argument(
        "--keys-file", type=Path, help="read research credentials from an external env file"
    )
    re_search.add_argument(
        "--http", action="store_true", help="fetch static HTML without Jev/API keys"
    )
    re_search.add_argument("--max-steps", type=int, default=20)
    re_search.add_argument(
        "--enqueue", action="store_true", help="queue observed single-video links"
    )
    re_search.add_argument("--limit", type=int, default=50, help="maximum new videos to queue")
    dw = command("discover-web", "queue single-video links from saved browser observations")
    dw.add_argument("--limit", type=int, default=50)
    dc = command("check-docs", "assess a card against fresh observed documentation; never edit it")
    dc.add_argument("--card", help="technique:<id> or workflow:<id>, from search results")
    dc.add_argument(
        "--docs-host", action="append", help="exact official documentation host; repeatable"
    )
    dc.add_argument("--sources", type=int, default=3)
    dc.add_argument(
        "--max-age-days", type=int, default=7, help="maximum age of documentation captures"
    )
    dc.add_argument("--force", action="store_true", help="ignore cached documentation assessments")
    dc.add_argument("--report", action="store_true", help="read saved assessments; no model calls")
    dc.add_argument("--json", action="store_true")
    ap = command(
        "autopilot", "automate frontier discovery, research, distillation, catalog and search"
    )
    ap.add_argument("--plan", type=Path, default=Path("frontier.toml"))
    ap.add_argument("--seeds", type=Path, default=Path("seeds.toml"))
    ap.add_argument("--once", action="store_true", help="run one bounded cycle")
    ap.add_argument("--limit", type=int, default=2, help="research bundles per cycle")
    ap.add_argument(
        "--daily-cap", type=int, default=50, help="research attempts per rolling 24 hours"
    )
    ap.add_argument("--interval", type=int, default=1800, help="seconds between cycles")
    ap.add_argument(
        "--no-search", action="store_true", help="disable semantic comparisons and indexing"
    )
    ap.add_argument(
        "--external-video-worker", action="store_true", help="use an already-running video worker"
    )
    ap.add_argument("--report", action="store_true", help="show durable status; perform no work")
    ap.add_argument("--retry", action="store_true", help="retry failed/watch research jobs")
    ap.add_argument("--stop", action="store_true", help="request stop after current work")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    only = getattr(args, "only", None)
    if only:
        try:
            re.compile(only)
        except re.error as err:
            p.error(f"--only: not a regex: {err}")
    try:
        s = config.with_models(config.load(), args.provider, args.model, args.video_model)
    except ValueError as err:
        p.error(str(err))
    if getattr(args, "workers", None):
        s = config.Settings(**{**s.__dict__, "workers": args.workers})
    logging.getLogger("distill").debug("video model %s, text model %s", s.model, s.text_model)

    try:
        COMMANDS[args.cmd](s, args)
    except ValueError as err:
        p.error(str(err))


def _run(s: config.Settings, args: argparse.Namespace) -> None:
    try:
        asyncio.run(run(s, args.seeds, once=args.once, only=args.only, limit=args.limit))
    except KeyboardInterrupt:
        print("stopped; leased videos return to the queue when their lease expires")


def _autopilot(s: config.Settings, args: argparse.Namespace) -> None:
    if sum((args.report, args.retry, args.stop)) > 1:
        raise ValueError("choose only one of --report, --retry and --stop")
    if args.stop:
        s.data.mkdir(parents=True, exist_ok=True)
        (s.data / "autopilot.stop").touch()
        print("stop requested; the current research work will finish")
    elif args.report or args.retry:
        store = Store(s.data / "automation.sqlite")
        try:
            if args.retry:
                print(f"{store.retry()} research jobs queued again")
            else:
                service = store.get("service")
                if service and service.get("status") == "running":
                    import time

                    service["heartbeat_stale"] = time.time() - service.get("heartbeat_at", 0) > 300
                print(
                    json.dumps(
                        {
                            **store.report(),
                            "service": service,
                            "active_job": store.get("active_job"),
                            "last_cycle": autopilot.cycle_report(store),
                            "index_recovery": store.get("index:recovery"),
                        },
                        indent=2,
                    )
                )
        finally:
            store.close()
    else:
        try:
            asyncio.run(
                autopilot.run(
                    s,
                    args.plan,
                    args.seeds,
                    once=args.once,
                    limit=args.limit,
                    daily_cap=args.daily_cap,
                    interval=args.interval,
                    semantic=not args.no_search,
                    external_video_worker=args.external_video_worker,
                )
            )
        except KeyboardInterrupt:
            print("autopilot stopped; completed evidence and model answers are preserved")


COMMANDS: dict[str, Callable[[config.Settings, argparse.Namespace], None]] = {
    "add": lambda s, a: _add(s, a.urls),
    "import": lambda s, a: _import(s, a.dir),
    "discover": lambda s, a: asyncio.run(_discover(s, a.seeds, a.only)),
    "run": _run,
    "catalog": lambda s, a: asyncio.run(_catalog(s)),
    "merge": lambda s, a: asyncio.run(_catalog(s)),
    "workflows": lambda s, a: print(
        f"{_workflows(s, Queue(s.db_path))} workflows -> {s.workflow_catalog_dir}"
    ),
    "previews": lambda s, a: _previews(s),
    "status": lambda s, a: print(json.dumps(Queue(s.db_path).counts(), indent=2)),
    "retry": lambda s, a: print(f"{Queue(s.db_path).requeue_failed(a.only)} videos re-queued"),
    "canary": lambda s, a: asyncio.run(_canary(s)),
    "recard": lambda s, a: _recard(s, a.only),
    "audit": lambda s, a: asyncio.run(_audit(s, a)),
    "sources": lambda s, a: _sources(s),
    "index": _index,
    "search": _search,
    "research": _research,
    "discover-web": lambda s, a: _discover_web(s, a.limit),
    "check-docs": lambda s, a: asyncio.run(_check_docs(s, a)),
    "autopilot": _autopilot,
    "knowledge": knowledge_cli.command,
}
