"""Quality for discovery: code decides what a candidate is worth before any model call.

`Queue.lease` used to take the oldest `new` row, so the first seeds' beginner search results
blocked everything behind them for days. This module scores a candidate on arrival (source
track record, duration band, title terms), tracks which sources actually pay off in cards, and
grows new sources on its own: channels that keep carding, links they mention, forum threads,
and searches aimed at whichever signal or medium the catalog is thin on. No model involvement
anywhere here — it is all free at discover time.
"""

import re
from dataclasses import dataclass

# --- duration band -----------------------------------------------------------

# Most technique tutorials worth carding run 6 to 90 minutes. Under a minute is almost always a
# button click or a "here's the result" recap — except on sites built around short video.
DURATION_SWEET_MIN_S = 360.0
DURATION_SWEET_MAX_S = 5400.0
DURATION_SWEET_BONUS = 15.0
DURATION_VERY_SHORT_S = 60.0
DURATION_VERY_SHORT_PENALTY = -20.0
SHORT_TOLERANT_SITES = frozenset({"instagram", "tiktok"})


def _duration_score(duration: float | None, site: str) -> float:
    if duration is None:
        return 0.0
    if DURATION_SWEET_MIN_S <= duration <= DURATION_SWEET_MAX_S:
        return DURATION_SWEET_BONUS
    if duration < DURATION_VERY_SHORT_S and site not in SHORT_TOLERANT_SITES:
        return DURATION_VERY_SHORT_PENALTY
    return 0.0


# --- title terms ---------------------------------------------------------------

TITLE_BONUS_WEIGHT = 6.0
TITLE_PENALTY_WEIGHT = -15.0

TITLE_BONUS_TERMS: tuple[str, ...] = (
    "breakdown",
    "explained",
    "gdc",
    "siggraph",
    "talk",
    "math",
    "maths",
    "shader",
    "deep dive",
    "theory",
    "technique",
)
# "how ... works", e.g. "how subsurface scattering works", not just "how to" (a how-to is a
# button-click walkthrough far more often than a mechanism).
TITLE_BONUS_PATTERNS: tuple[re.Pattern[str], ...] = (re.compile(r"\bhow\b.*\bworks?\b"),)

TITLE_PENALTY_TERMS: tuple[str, ...] = (
    "beginner",
    "in 60 seconds",
    "#shorts",
    "speedrun",
    "speed run",
    "speed build",
    "timelapse",
    "time-lapse",
    "free download",
    "asset pack",
    "giveaway",
    "reaction",
)


def _title_score(title: str) -> float:
    t = title.lower()
    bonus = sum(1 for term in TITLE_BONUS_TERMS if term in t)
    bonus += sum(1 for pat in TITLE_BONUS_PATTERNS if pat.search(t))
    penalty = sum(1 for term in TITLE_PENALTY_TERMS if term in t)
    return TITLE_BONUS_WEIGHT * bonus + TITLE_PENALTY_WEIGHT * penalty


# --- source track record --------------------------------------------------------

# A source with no history yet scores as if its cards-per-watched rate were this, so it gets a
# fair trial instead of being judged on zero evidence.
YIELD_BASELINE = 1.0
SOURCE_YIELD_WEIGHT = 40.0


# A video counts as "watched" once it is off the queue with an outcome, whether or not it
# carded: that is what a source spent its trial on.
FINISHED_STATUSES = frozenset({"carded", "dropped", "referenced"})


@dataclass(frozen=True)
class SourceStats:
    """Per source (a seed, or a channel), from the videos and cards tables."""

    watched: int = 0
    carded: int = 0
    value: float = 0.0  # sum of `video_value` over its videos


# A card made before depth existed is taken as a textbook basic, not as deep.
LEGACY_CARD_DEPTH = 2
# A video is worth its best few cards: a beginner tutorial that yields twenty shallow cards must
# not outrank a talk that yields three deep ones, so card count alone buys nothing.
VALUE_TOP_CARDS = 3
# What one solid video is worth (three depth-3 cards), so a yield of 1.0 means "a solid video
# per video watched" and seed tiers (1 = 2.0, 2 = 1.0, 3 = 0.5) sit on the same scale.
SOLID_VIDEO = 9.0


def card_depth(body: dict) -> int:
    """A card's depth (0-5), or LEGACY_CARD_DEPTH for cards made before that field existed."""
    depth = body.get("depth")
    if isinstance(depth, int) and not isinstance(depth, bool):
        return depth
    return LEGACY_CARD_DEPTH


def video_value(depths: list[int]) -> float:
    """What one video yielded, in solid videos: the sum of its deepest few cards' depths."""
    return sum(sorted(depths, reverse=True)[:VALUE_TOP_CARDS]) / SOLID_VIDEO


YIELD_PRIOR_WATCHED = 4.0


def smoothed_yield(stats: SourceStats, prior: float = YIELD_BASELINE) -> float:
    """Value per watched video (1.0 = one solid video each), smoothed toward `prior` so a
    source is not judged on one lucky or unlucky video. `prior` is a seed's declared tier when
    it has one — a curated tier-1 channel starts with the benefit of the doubt a random keyword
    search does not."""
    return (stats.value + YIELD_PRIOR_WATCHED * prior) / (stats.watched + YIELD_PRIOR_WATCHED)


# A keyword search no longer in seeds.toml (or never tiered) is judged like tier 3.
UNTIERED_SEARCH_PRIOR = 0.5


def seed_prior(seed: str | None, priors: dict[str, float]) -> float:
    """The benefit of the doubt a video's seed gets before its record speaks."""
    if seed in priors:
        return priors[seed]
    return UNTIERED_SEARCH_PRIOR if (seed or "").startswith("ytsearch") else YIELD_BASELINE


def score(*, title: str | None, duration: float | None, site: str, source_yield: float) -> float:
    """A candidate's queue priority: what its source has earned, plus free length and title
    signals. Higher leases first."""
    return (
        SOURCE_YIELD_WEIGHT * source_yield
        + _duration_score(duration, site)
        + _title_score(title or "")
    )


# --- refill budget ---------------------------------------------------------------

REFILL_MULTIPLIER_MIN = 0.25
REFILL_MULTIPLIER_MAX = 2.5


def refill_multiplier(source_yield: float) -> float:
    """Scales a seed's listing limit by how well it has paid off, clamped so a strong source
    cannot eat a whole refill and a weak one is never starved to nothing."""
    return min(REFILL_MULTIPLIER_MAX, max(REFILL_MULTIPLIER_MIN, source_yield / YIELD_BASELINE))


PAUSE_MIN_WATCHED = 8


def is_paused(stats: SourceStats) -> bool:
    """A seed given a fair trial (enough watched videos) that has produced zero cards is
    paused, not deleted: it stops eating refill budget but stays in seeds.toml to revisit."""
    return stats.watched >= PAUSE_MIN_WATCHED and stats.value == 0


# --- snowball: channels and links they mention ------------------------------------

SNOWBALL_MIN_CARDED = 2


def is_snowball_channel(carded: int, source_yield: float) -> bool:
    """A channel worth following as its own source: it has carded more than once and its yield
    is at or above what a fresh source is assumed to have."""
    return carded >= SNOWBALL_MIN_CARDED and source_yield >= YIELD_BASELINE


def channel_uploads_url(channel: str) -> str:
    """The listing yt-dlp treats as that channel's uploads."""
    channel = channel.rstrip("/")
    return channel if channel.endswith(("/videos", "/reels")) else f"{channel}/videos"


_YOUTUBE_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"youtube\.com/watch\?v=[\w-]{6,}[^\s)\"'<]*|youtu\.be/[\w-]{6,}[^\s)\"'<]*"
    r")",
    re.IGNORECASE,
)


def extract_youtube_links(text: str) -> list[str]:
    """YouTube links mentioned in a video's description. Descriptions are untrusted text — the
    code only ever pulls out URLs matching this pattern, never anything read as an instruction."""
    seen: list[str] = []
    for m in _YOUTUBE_LINK_RE.finditer(text or ""):
        url = m.group(0)
        if url not in seen:
            seen.append(url)
    return seen


_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/)([\w-]{6,})")


def youtube_video_id(url: str) -> str | None:
    m = _YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


# --- realtimevfx.com (Discourse) feed ------------------------------------------------


@dataclass(frozen=True)
class DiscourseTopic:
    id: int
    slug: str
    url: str


def discourse_listing_urls(base: str, tags: tuple[str, ...]) -> list[str]:
    base = base.rstrip("/")
    return [f"{base}/latest.json", *(f"{base}/tag/{tag}.json" for tag in tags)]


def discourse_topics(listing: dict, base: str) -> list[DiscourseTopic]:
    base = base.rstrip("/")
    out = []
    seen: set[int] = set()
    for t in listing.get("topic_list", {}).get("topics", []):
        tid, slug = t.get("id"), t.get("slug")
        if tid is None or not slug or tid in seen:
            continue
        seen.add(tid)
        out.append(DiscourseTopic(tid, slug, f"{base}/t/{slug}/{tid}"))
    return out


_VIDEO_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"youtube\.com/watch\?v=[\w-]{6,}[^\s)\"'<]*"
    r"|youtu\.be/[\w-]{6,}[^\s)\"'<]*"
    r"|vimeo\.com/\d+[^\s)\"'<]*"
    r"|instagram\.com/(?:p|reel)/[\w-]+/?[^\s)\"'<]*"
    r")",
    re.IGNORECASE,
)


def discourse_topic_links(topic_json: dict) -> list[str]:
    """Video links posted in a topic's posts. Post bodies are untrusted text; only URLs matching
    this pattern are ever pulled out."""
    seen: list[str] = []
    for post in topic_json.get("post_stream", {}).get("posts", []):
        text = post.get("cooked") or ""
        for m in _VIDEO_LINK_RE.finditer(text):
            url = m.group(0)
            if url not in seen:
                seen.append(url)
    return seen


# --- coverage-driven search -----------------------------------------------------

# A fixed, code-side template per thin signal or medium. The model never picks a query; this map
# does, so a gap in the catalog turns into a search deterministically.
COVERAGE_QUERIES: dict[str, str] = {
    "compute": "webgpu compute shader explained",
    "buffer": "ping pong render target simulation shader",
    "modeling": "hard surface topology explained",
    "normal": "normal map baking explained",
    "light": "stylized lighting model shader explained",
    "distance": "signed distance field raymarching explained",
    "particles": "gpu particle simulation compute shader explained",
    "deform": "procedural deformation shader explained",
    "motion": "animation timing and spacing explained",
    "screen": "post processing screen shader explained",
    "geometry": "geometry nodes procedural technique explained",
    "lighting": "real-time lighting technique explained",
    "2d-frames": "2d frame by frame animation technique explained",
    "post": "post processing pipeline technique explained",
    "animation": "animation principles explained for games",
}

COVERAGE_THIN_BELOW = 3


def thin_topics(counts: dict[str, int], universe: tuple[str, ...]) -> list[str]:
    """Signals or mediums with few or no cards yet, in the order they're declared."""
    return [t for t in universe if counts.get(t, 0) < COVERAGE_THIN_BELOW]


def coverage_queries(counts: dict[str, int], universe: tuple[str, ...], cap: int) -> list[str]:
    queries = [COVERAGE_QUERIES[t] for t in thin_topics(counts, universe) if t in COVERAGE_QUERIES]
    return queries[:cap]
