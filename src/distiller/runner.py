"""The endless loop: a canary first, then N workers draining and refilling the queue."""

import asyncio
import contextlib
import importlib.util
import logging
import random
import re
import shutil
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
from pydantic import BaseModel

from .config import Settings
from .db import Queue, Video
from .discovery import (
    YIELD_BASELINE,
    SourceStats,
    channel_uploads_url,
    coverage_queries,
    discourse_listing_urls,
    discourse_topic_links,
    discourse_topics,
    extract_youtube_links,
    is_paused,
    is_snowball_channel,
    refill_multiplier,
    score,
    seed_prior,
    smoothed_yield,
    youtube_video_id,
)
from .forms import MEDIUMS, SIGNALS
from .media import (
    CANARY_COLORS,
    SiteGates,
    canary_clip,
    discover,
    site_of,
    video_metadata,
    youtube_search_url,
)
from .model import Busy, Model
from .stages import process

log = logging.getLogger("distill")

REFILL_BELOW = 20
REFILL_EVERY_S = 1800
IDLE_S = 60


class CanaryReply(BaseModel):
    colors: list[str]


async def canary(s: Settings, model: Model) -> None:
    """Proves the model receives video. A dropped video gets a confident, made-up answer."""
    s.work_dir.mkdir(parents=True, exist_ok=True)
    path = s.work_dir / "canary.mp4"
    expected = await asyncio.to_thread(canary_clip, path, random.Random())
    reply = await model.fill(
        CanaryReply,
        "The clip shows solid colours one after another. List them in order, lowercase, "
        f"each one of: {', '.join(CANARY_COLORS)}.",
        path,
    )
    path.unlink(missing_ok=True)
    if [c.strip().lower() for c in reply.colors] != expected:
        raise RuntimeError(
            f"canary failed: expected {expected}, model said {reply.colors}. "
            "The model is not receiving video."
        )
    log.info("canary passed: %s", expected)


def youtube_ready() -> list[str]:
    """What YouTube downloads are missing. Without the challenge solver, stream URLs stay
    scrambled and YouTube answers some downloads with 403."""
    missing = []
    if importlib.util.find_spec("yt_dlp_ejs") is None:
        missing.append("yt-dlp-ejs (the challenge solver; install yt-dlp[default])")
    if not any(shutil.which(rt) for rt in ("deno", "node", "bun")):
        missing.append("a JavaScript runtime for the solver (deno, node or bun)")
    return missing


def sweep_work(work_dir: Path, older_than_s: float) -> int:
    """Removes download folders left by a crashed or killed run. A folder a live worker is using
    is younger than a lease, since the worker recreates it when it starts a video."""
    if not work_dir.exists():
        return 0
    cutoff = time.time() - older_than_s
    stale = [d for d in work_dir.iterdir() if d.is_dir() and d.stat().st_mtime < cutoff]
    for d in stale:
        shutil.rmtree(d, ignore_errors=True)
    return len(stale)


MODES = ("technique", "workflow")


@dataclass(frozen=True)
class Seed:
    url: str
    limit: int
    prior: float = 1.0  # a seed's declared tier: how much benefit of the doubt it starts with
    match: re.Pattern[str] | None = None  # only titles matching this are queued, e.g. a big channel
    mode: str = "technique"  # "workflow": modding tutorials, carded as workflows (SPEC section 11)


@dataclass(frozen=True)
class DiscourseSeed:
    base: str
    tags: tuple[str, ...]
    limit: int = 20


def load_seeds(path: Path) -> tuple[list[Seed], list[DiscourseSeed]]:
    """Each seed is a `url`, a YouTube `query` with an optional `recent` period, or a
    `discourse` forum base with `tags` to list. `prior` (or `tier`: 1, 2, 3 map to 2.0, 1.0,
    0.5) is the source's starting benefit of the doubt; `match` restricts a broad listing (a
    whole channel, a whole tag) to titles on-topic for us. `mode` is "technique" (the default)
    or "workflow", which watches the seed's videos as modding workflows."""
    if not path.exists():
        return [], []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    seeds, discourse = [], []
    tier_prior = {1: 2.0, 2: 1.0, 3: 0.5}
    for seed in data.get("seed", []):
        if "discourse" in seed:
            discourse.append(
                DiscourseSeed(
                    seed["discourse"], tuple(seed.get("tags", [])), int(seed.get("limit", 20))
                )
            )
            continue
        url = seed.get("url") or youtube_search_url(seed["query"], seed.get("recent"))
        prior = seed.get("prior", tier_prior.get(seed.get("tier"), 1.0))
        match = re.compile(seed["match"], re.IGNORECASE) if seed.get("match") else None
        mode = seed.get("mode", "technique")
        if mode not in MODES:
            raise ValueError(f"seed {url}: mode must be one of {', '.join(MODES)}, not {mode!r}")
        seeds.append(Seed(url, int(seed.get("limit", 50)), float(prior), match, mode))
    return seeds, discourse


AUTO_SEED_LIMIT = 15
COVERAGE_SEED_CAP = 6
COVERAGE_SEED_LIMIT = 20
LINK_SCAN_KEY = "discovery:links_scanned"
DISCOURSE_PAUSE_S = 1.0


def rescore_waiting(
    q: Queue,
    seed_stats: dict[str, SourceStats],
    channel_stats: dict[str, SourceStats],
    priors: dict[str, float],
) -> int:
    """Scores every waiting video again from its source's current record, so the queue follows
    the evidence: a source that turns out shallow sinks, one that turns out deep rises."""
    rows = q.waiting_videos()
    scored = []
    for row in rows:
        prior = seed_prior(row["seed"], priors)
        stats = channel_stats.get(row["channel"]) if row["channel"] else None
        if stats is None:
            stats = seed_stats.get(row["seed"] or "", SourceStats())
        source_yield = smoothed_yield(stats, prior)
        priority = score(
            title=row["title"],
            duration=row["duration"],
            site=row["site"],
            source_yield=source_yield,
        )
        scored.append((row["id"], priority))
    # One transaction: a migrated queue can hold thousands of rows.
    q.set_priorities(scored)
    return len(rows)


def _auto_listings(auto: list[dict[str, str]]) -> list[Seed]:
    """Search and snowballed channel auto seeds, as listings to discover."""
    out = []
    for a in auto:
        if a["kind"] == "search":
            out.append(Seed(youtube_search_url(a["query"], None), AUTO_SEED_LIMIT))
        elif a["kind"] == "channel":
            out.append(Seed(a["query"], AUTO_SEED_LIMIT))
        # "link" rows from before links were enqueued directly: nothing left to list.
    return out


async def refill(
    s: Settings, q: Queue, gates: SiteGates, seeds: list[Seed], discourse: list[DiscourseSeed] = ()
) -> int:
    """Lists every seed, scaled and ordered by what it has earned, plus snowballed channels and
    links, a forum feed, and a search for whatever the catalog is thinnest on."""
    seed_stats = q.seed_stats()
    channel_stats = q.channel_stats()
    priors = {seed.url: seed.prior for seed in seeds}
    rescored = await asyncio.to_thread(rescore_waiting, q, seed_stats, channel_stats, priors)
    if rescored:
        log.info("refill: rescored %d waiting videos", rescored)

    added = 0
    for seed in [*seeds, *_auto_listings(q.auto_seeds())]:
        stats = seed_stats.get(seed.url, SourceStats())
        if is_paused(stats):
            log.info("refill: %s paused (%d watched, 0 cards)", seed.url, stats.watched)
            continue
        limit = max(1, round(seed.limit * refill_multiplier(smoothed_yield(stats, seed.prior))))
        try:
            found = await gates.run(site_of(seed.url), discover, seed.url, limit, s.cookies_browser)
        except Exception as err:  # one bad seed must not stop the rest
            log.warning("discover %s failed: %s", seed.url, err)
            continue
        for f in found:
            if seed.match and not seed.match.search(f.title or ""):
                continue
            src_stats = channel_stats.get(f.channel) if f.channel else None
            source_yield = (
                smoothed_yield(src_stats, seed.prior)
                if src_stats
                else smoothed_yield(stats, seed.prior)
            )
            added += q.enqueue(
                site=f.site,
                site_id=f.site_id,
                url=f.url,
                title=f.title,
                duration=f.duration,
                seed=seed.url,
                channel=f.channel,
                view_count=f.view_count,
                priority=score(
                    title=f.title, duration=f.duration, site=f.site, source_yield=source_yield
                ),
            )
    added += await snowball(s, q, gates, channel_stats, seed_stats)
    added += add_coverage_seeds(q)
    for d in discourse:
        try:
            added += await discourse_refill(d, q)
        except Exception as err:
            log.warning("discourse %s failed: %s", d.base, err)
    log.info("refill added %d videos", added)
    return added


async def snowball(
    s: Settings,
    q: Queue,
    gates: SiteGates,
    channel_stats: dict[str, SourceStats],
    seed_stats: dict[str, SourceStats],
) -> int:
    """Two ways good sources beget more sources: a channel that keeps carding becomes its own
    seed, and a carded video's description is scanned once for the links it mentions."""
    added = 0
    known = {a["query"] for a in q.auto_seeds()}
    for channel, stats in channel_stats.items():
        y = smoothed_yield(stats)
        listing = channel_uploads_url(channel)
        eligible = is_snowball_channel(stats.carded, y) and listing not in known
        if eligible and q.add_auto_seed(listing, video_id=f"channel:{channel}", kind="channel"):
            added += 1
            log.info("snowball: new source %s (carded=%d, yield=%.2f)", channel, stats.carded, y)
    for video in q.carded_videos():
        stats = (
            channel_stats.get(video["channel"])
            if video["channel"]
            else seed_stats.get(video["seed"])
        )
        if stats is not None and smoothed_yield(stats) < YIELD_BASELINE:
            continue
        if q.memo(video["id"], LINK_SCAN_KEY) is not None:
            continue
        try:
            meta = await gates.run(
                video["site"], video_metadata, video["url"], video["site"], s.cookies_browser
            )
        except Exception as err:
            log.warning("snowball: metadata for %s failed: %s", video["id"], err)
            continue
        q.remember(video["id"], LINK_SCAN_KEY, True)
        # A link names one video, so it is queued directly rather than kept as a seed that
        # every later refill would list again.
        source_yield = smoothed_yield(stats) if stats is not None else YIELD_BASELINE
        for url in extract_youtube_links(meta.get("description") or ""):
            vid = youtube_video_id(url)
            if vid is None:
                continue
            added += q.enqueue(
                site="youtube",
                site_id=vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                seed=f"link:{video['id']}",
                priority=score(
                    title=None, duration=None, site="youtube", source_yield=source_yield
                ),
            )
    return added


async def discourse_refill(seed: DiscourseSeed, q: Queue) -> int:
    """realtimevfx.com and forums like it: list topics under each tag, then scan each topic's
    posts for video links. Untrusted post text; only URLs matching a fixed pattern are pulled."""
    added = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        topics = []
        for list_url in discourse_listing_urls(seed.base, seed.tags):
            try:
                r = await client.get(list_url)
                r.raise_for_status()
            except httpx.HTTPError as err:
                log.warning("discourse listing %s failed: %s", list_url, err)
                continue
            topics += discourse_topics(r.json(), seed.base)
            await asyncio.sleep(DISCOURSE_PAUSE_S)
        for topic in topics[: seed.limit]:
            try:
                r = await client.get(f"{topic.url}.json")
                r.raise_for_status()
            except httpx.HTTPError as err:
                log.warning("discourse topic %s failed: %s", topic.url, err)
                continue
            await asyncio.sleep(DISCOURSE_PAUSE_S)
            for url in discourse_topic_links(r.json()):
                site = site_of(url)
                site_id = youtube_video_id(url) if site == "youtube" else url
                added += q.enqueue(
                    site=site,
                    site_id=site_id or url,
                    url=url,
                    seed=topic.url,
                    priority=score(title=None, duration=None, site=site, source_yield=1.0),
                )
    return added


def add_coverage_seeds(q: Queue) -> int:
    """A low-prior search for whatever signal or medium the catalog is thinnest on, so what
    gets watched steers toward the gaps rather than repeating what is already well covered."""
    signal_counts: dict[str, int] = {}
    medium_counts: dict[str, int] = {}
    for card in q.cards():
        body = card["body"]
        for signal in body.get("produces") or []:
            signal_counts[signal] = signal_counts.get(signal, 0) + 1
        for medium in body.get("mediums") or []:
            medium_counts[medium] = medium_counts.get(medium, 0) + 1
    queries = [
        *coverage_queries(signal_counts, SIGNALS, COVERAGE_SEED_CAP),
        *coverage_queries(medium_counts, MEDIUMS, COVERAGE_SEED_CAP),
    ]
    added = 0
    for query in queries[:COVERAGE_SEED_CAP]:
        if q.add_auto_seed(query, video_id="coverage", kind="search"):
            added += 1
    return added


@dataclass
class Scope:
    """What this run may take: `only` is a regex on seed, channel and url; `limit` caps the
    videos started. A scoped run never refills, and ends when nothing selected is left."""

    only: str | None = None
    limit: int | None = None
    started: int = 0
    modes: dict[str, str] = field(default_factory=dict)  # seed url -> mode

    @property
    def scoped(self) -> bool:
        return self.only is not None or self.limit is not None

    def full(self) -> bool:
        return self.limit is not None and self.started >= self.limit

    def tag(self, video: Video) -> Video:
        """The video with its seed's mode; a video from no known seed is a technique video."""
        return replace(video, mode=self.modes.get(video.seed or "", "technique"))


async def worker(
    n: int,
    s: Settings,
    q: Queue,
    model: Model,
    gates: SiteGates,
    stop: asyncio.Event,
    scope: Scope | None = None,
) -> None:
    scope = scope or Scope()
    day_start = time.time()
    while not stop.is_set() and not scope.full():
        if time.time() - day_start > 86400:
            day_start = time.time()
        if q.finished_since(day_start) >= s.daily_cap:
            log.info("worker %d: daily cap reached", n)
            await _sleep(stop, IDLE_S * 10)
            continue
        video = q.lease(s.lease_s, only=scope.only)
        if video is None:
            await _sleep(stop, IDLE_S)
            continue
        scope.started += 1
        await _work(n, scope.tag(video), s, q, model, gates, stop)


async def _work(
    n: int, video: Video, s: Settings, q: Queue, model: Model, gates: SiteGates, stop: asyncio.Event
) -> None:
    log.info("worker %d: start %s  %s", n, video.id, (video.title or "")[:70])
    heartbeat = asyncio.create_task(_renew(q, video.id, s.lease_s))
    try:
        out = await process(video, s, q, model, gates)
    except asyncio.CancelledError:
        # Stopped mid-video (Ctrl+C): hand it straight back instead of waiting out the lease.
        q.release(video.id)
        raise
    except Busy as err:
        # OmniRoute or the site is overloaded, not the video broken: back in the queue with its
        # attempt refunded, and this worker waits as long as the cause asks.
        q.release(video.id)
        wait = max(IDLE_S * 2, err.retry_after or 0)
        log.warning("worker %d: %s paused %.0f s: %s", n, video.id, wait, str(err)[:100])
        await _sleep(stop, wait)
    except Exception as err:
        status = q.retry_or_fail(video, f"{type(err).__name__}: {err}", s.max_attempts)
        log.warning("worker %d: %s -> %s: %s", n, video.id, status, str(err)[:200])
    else:
        log.info(
            "worker %d: done %s: %s, %d cards (%s)", n, video.id, out.status, out.cards, out.note
        )
    finally:
        heartbeat.cancel()


async def _renew(q: Queue, video_id: str, lease_s: float) -> None:
    while True:
        await asyncio.sleep(lease_s / 4)
        q.renew(video_id, lease_s)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def run(
    s: Settings,
    seeds_path: Path,
    once: bool = False,
    only: str | None = None,
    limit: int | None = None,
) -> None:
    q = Queue(s.db_path)
    model = Model(s)
    gates = SiteGates()
    stop = asyncio.Event()
    workers = []
    began = time.time()
    try:
        await canary(s, model)
        swept = sweep_work(s.work_dir, s.lease_s)
        if swept:
            log.info("removed %d stale download folders", swept)
        for gap in youtube_ready():
            log.warning("YouTube downloads will fail with 403: missing %s", gap)
        seeds, discourse = load_seeds(seeds_path)
        scope = Scope(only, limit, modes={seed.url: seed.mode for seed in seeds})
        workers = [
            asyncio.create_task(worker(i, s, q, model, gates, stop, scope))
            for i in range(s.workers)
        ]
        last_refill = 0.0
        while not stop.is_set():
            if (
                not scope.scoped
                and q.pending() < REFILL_BELOW
                and time.time() - last_refill > REFILL_EVERY_S
            ):
                last_refill = time.time()
                await refill(s, q, gates, seeds, discourse)
            log.info("queue: %s", q.counts())
            if (once or scope.scoped) and q.pending(only) == 0:
                stop.set()
            if scope.full():  # every worker is finishing its last video, then returns
                await asyncio.wait(workers)
                stop.set()
            elif scope.scoped:  # wakes as soon as the workers are done
                await asyncio.wait(workers, timeout=IDLE_S)
            else:
                await _sleep(stop, IDLE_S)
        await asyncio.gather(*workers)
    finally:
        stop.set()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        log.info(
            "run took %.0f s; model requests: %d video, %d text",
            time.time() - began,
            model.calls["video"],
            model.calls["text"],
        )
        await model.close()
