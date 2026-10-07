"""Finding, downloading and cutting videos. yt-dlp for sites, ffmpeg for clips."""

import asyncio
import json
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import yt_dlp

from .config import SITE_LIMITS
from .model import Busy

SITE_HOSTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
}


def site_of(url: str) -> str:
    if url.startswith("ytsearch"):
        return "youtube"
    host = (urlparse(url).hostname or "").removeprefix("www.").removeprefix("m.")
    return next((site for h, site in SITE_HOSTS.items() if host.endswith(h)), "other")


class SiteGates:
    """Per-site concurrency and minimum spacing between request starts."""

    def __init__(self) -> None:
        self._sem = {s: asyncio.Semaphore(lim.concurrency) for s, lim in SITE_LIMITS.items()}
        self._next = dict.fromkeys(SITE_LIMITS, 0.0)
        self._lock = asyncio.Lock()

    async def run[T](self, site: str, fn: Any, *args: Any) -> T:
        site = site if site in SITE_LIMITS else "other"
        async with self._sem[site]:
            async with self._lock:
                wait = self._next[site] - time.monotonic()
                self._next[site] = max(time.monotonic(), self._next[site]) + (
                    SITE_LIMITS[site].min_spacing_s * random.uniform(1.0, 1.5)
                )
            if wait > 0:
                await asyncio.sleep(wait)
            return await asyncio.to_thread(fn, *args)


def _ydl_opts(site: str, cookies_browser: str, **extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True, **extra}
    # Instagram only. Logged-in YouTube answers "The page needs to be reloaded"; anonymous works.
    if site == "instagram" and cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


@dataclass(frozen=True)
class Found:
    site: str
    site_id: str
    url: str
    title: str | None
    duration: float | None
    channel: str | None = None  # the uploader's canonical URL, when the flat listing gives one
    view_count: int | None = None


def is_instagram_listing(url: str) -> bool:
    """A profile, reels tab or hashtag page, as opposed to a single post."""
    path = urlparse(url).path
    return site_of(url) == "instagram" and not path.startswith(("/p/", "/reel/", "/tv/"))


def instagram_seed(url: str) -> str:
    """A bare profile lists its reels tab, so photo posts are never fetched."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) == 1:
        return f"https://www.instagram.com/{parts[0]}/reels/"
    return url


def instagram_found(items: list[Any], limit: int) -> list[Found]:
    """Video posts from gallery-dl's `-j` output, once per post."""
    out: list[Found] = []
    seen: set[str] = set()
    for item in items:
        meta = next((x for x in reversed(item) if isinstance(x, dict)), None)
        if not meta:
            continue
        code = meta.get("post_shortcode") or meta.get("shortcode")
        duration = meta.get("video_duration") or meta.get("audio_duration")
        if not code or code in seen or not (duration or meta.get("extension") == "mp4"):
            continue
        seen.add(code)
        caption = (meta.get("description") or "").strip()
        out.append(
            Found(
                "instagram",
                code,
                f"https://www.instagram.com/p/{code}/",
                caption[:300] or None,
                duration,
            )
        )
        if len(out) >= limit:
            break
    return out


def discover_instagram(seed: str, limit: int, cookies_browser: str) -> list[Found]:
    # yt-dlp's Instagram profile lister is marked broken (2026.08); gallery-dl's works.
    args = [
        sys.executable,
        "-m",
        "gallery_dl",
        "-q",
        "--simulate",
        "-j",
        "--range",
        f"1-{limit * 2}",
        instagram_seed(seed),
    ]
    if cookies_browser:
        args[3:3] = ["--cookies-from-browser", cookies_browser]
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=False)
    if done.returncode != 0 or not done.stdout.strip():
        raise RuntimeError(f"gallery-dl failed: {done.stderr[-500:]}")
    return instagram_found(json.loads(done.stdout), limit)


def _channel_of(e: dict[str, Any]) -> str | None:
    """The uploader's own listing, so cards from it can be tracked as one source and, if it
    keeps paying off, snowballed into a seed of its own."""
    url = e.get("channel_url") or e.get("uploader_url")
    if url:
        return str(url)
    channel_id = e.get("channel_id")
    return f"https://www.youtube.com/channel/{channel_id}" if channel_id else None


def discover(seed: str, limit: int, cookies_browser: str) -> list[Found]:
    """Lists videos under a channel, playlist, profile or `ytsearchN:` query without downloading."""
    if is_instagram_listing(seed):
        return discover_instagram(seed, limit, cookies_browser)
    site = site_of(seed)
    opts = _ydl_opts(site, cookies_browser, extract_flat="in_playlist", playlistend=limit)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(seed, download=False) or {}
    entries = info.get("entries") or [info]
    out = []
    for e in entries[:limit]:
        if not e or not e.get("id") or not is_video_entry(e):
            continue
        url = e.get("url") or e.get("webpage_url") or ""
        if site == "youtube" and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        out.append(
            Found(
                site_of(url) if url else site,
                str(e["id"]),
                url,
                e.get("title"),
                e.get("duration"),
                _channel_of(e) if site == "youtube" else None,
                e.get("view_count"),
            )
        )
    return out


def is_video_entry(e: dict[str, Any]) -> bool:
    """Search results mix in playlists and channels; only single videos are queued."""
    url = e.get("url") or ""
    if (e.get("ie_key") or "").startswith("YoutubeTab"):
        return False
    return "list=" not in url and "/channel/" not in url


# YouTube's own search filters: upload date within the period, newest relevant first.
RECENT_FILTERS = {
    "hour": "EgIIAQ%3D%3D",
    "day": "EgIIAg%3D%3D",
    "week": "EgIIAw%3D%3D",
    "month": "EgIIBA%3D%3D",
    "year": "EgIIBQ%3D%3D",
}


def youtube_search_url(query: str, recent: str | None) -> str:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    if recent:
        url += f"&sp={RECENT_FILTERS[recent]}"
    return url


def video_metadata(url: str, site: str, cookies_browser: str) -> dict[str, Any]:
    """The page's own metadata, without downloading: upload date, description and links a
    creator mentions, all things worth knowing before spending a fetch and a model call."""
    opts = _ydl_opts(site, cookies_browser, skip_download=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False, process=False) or {}


def upload_date(url: str, site: str, cookies_browser: str) -> str | None:
    """YYYYMMDD from the video's page, without downloading it. None if the site does not say."""
    info = video_metadata(url, site, cookies_browser)
    date = info.get("upload_date") or info.get("release_date")
    if not date and info.get("timestamp"):
        date = datetime.fromtimestamp(info["timestamp"], UTC).strftime("%Y%m%d")
    return date


def age_days(date: str, today: datetime) -> int:
    return (today.date() - datetime.strptime(date, "%Y%m%d").replace(tzinfo=UTC).date()).days


PRESCREEN_TEXT_LIMIT = 8000


def vtt_to_text(vtt: str, limit: int = PRESCREEN_TEXT_LIMIT) -> str:
    """Plain, de-duplicated text from a WebVTT auto-caption file.

    YouTube's auto-captions are a rolling window: the same line reappears, one word further
    along, across several cues. Keeping each line only once, in first-seen order, turns that
    into ordinary text instead of a wall of repeats.
    """
    skip = ("WEBVTT", "Kind:", "Language:")
    seen: set[str] = set()
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = re.sub(r"<[^>]+>", "", raw).strip()  # inline <00:00:01.000><c> timing/style tags
        if not line or "-->" in line or line.isdigit() or line.startswith(skip):
            continue
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines)[:limit]


def take_captions(out_dir: Path) -> str | None:
    """The first English caption track yt-dlp wrote, as text, removing the files. None when it
    wrote none: many videos have no English auto-captions, and that is no error."""
    vtt_files = sorted(out_dir.glob("prescreen.en*.vtt"))
    try:
        if not vtt_files:
            return None
        return vtt_to_text(vtt_files[0].read_text(encoding="utf-8", errors="replace")) or None
    finally:
        for f in vtt_files:
            f.unlink(missing_ok=True)


def fetch_prescreen_text(
    url: str, site: str, out_dir: Path, cookies_browser: str
) -> dict[str, Any] | None:
    """Title, description, chapters and English auto-captions, without downloading the video.

    None for anything but YouTube, or when yt-dlp raises: the pre-screen only ever runs where
    this can answer confidently, and its caller treats None as "skip the pre-screen".
    """
    if site != "youtube":
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = _ydl_opts(
        site,
        cookies_browser,
        skip_download=True,
        writeautomaticsub=True,
        subtitleslangs=["en.*"],
        subtitlesformat="vtt",
        outtmpl=str(out_dir / "prescreen.%(ext)s"),
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # download=True, with skip_download in opts: yt-dlp only skips the video itself, but
            # still runs the step that writes subtitles. download=False would skip both.
            info = ydl.extract_info(url, download=True) or {}
    except yt_dlp.utils.DownloadError:
        return None
    captions = take_captions(out_dir)
    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "chapters": [c["title"] for c in (info.get("chapters") or []) if c.get("title")],
        "captions": captions,
    }


DOWNLOAD_TRIES = 3
# YouTube's "confirm you're not a bot" wall lifts after a pause (measured 2026-09-25: the same
# videos downloaded fine hours later, with or without cookies).
BOT_CHECK_PAUSE_S = 600.0


def is_bot_check(err: Exception) -> bool:
    text = str(err)
    return "not a bot" in text or "Sign in to confirm" in text


def download(url: str, site: str, out_dir: Path, cookies_browser: str) -> Path:
    """Downloads with retries: YouTube's 403s under load are temporary."""
    for attempt in range(1, DOWNLOAD_TRIES + 1):
        try:
            return _download_once(url, site, out_dir, cookies_browser)
        except yt_dlp.utils.DownloadError as err:
            if is_bot_check(err):
                raise Busy(f"{site} bot check: {str(err)[:120]}", BOT_CHECK_PAUSE_S) from err
            if attempt == DOWNLOAD_TRIES:
                raise
            shutil.rmtree(out_dir, ignore_errors=True)
            time.sleep(30 * attempt)
    raise RuntimeError("unreachable")


def _download_once(url: str, site: str, out_dir: Path, cookies_browser: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = _ydl_opts(
        site,
        cookies_browser,
        format="bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        merge_output_format="mp4",
        outtmpl=str(out_dir / "source.%(ext)s"),
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    found = sorted(out_dir.glob("source.*"))
    if not found:
        raise RuntimeError("download produced no file")
    return found[0]


def _run(args: list[str]) -> str:
    done = subprocess.run(args, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {done.stderr[-500:]}")
    return done.stdout


def duration_of(path: Path) -> float:
    out = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    )
    return float(json.loads(out)["format"]["duration"])


@dataclass(frozen=True)
class Clip:
    path: Path
    start: float
    duration: float


def cut(source: Path, out_dir: Path, segment_s: int) -> list[Clip]:
    """Shrinks to 360p on the short side and splits into segments the model accepts inline."""
    # Gemini samples video at about 1 frame per second, so 2 fps loses nothing it would see and
    # nearly halves the upload OmniRoute has to admit.
    scale = "fps=2,scale='if(gt(iw,ih),-2,360)':'if(gt(iw,ih),360,-2)'"
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            scale,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "32",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-ac",
            "1",
            "-f",
            "segment",
            "-segment_time",
            str(segment_s),
            "-reset_timestamps",
            "1",
            str(out_dir / "clip_%03d.mp4"),
        ]
    )
    clips, start = [], 0.0
    for path in sorted(out_dir.glob("clip_*.mp4")):
        d = duration_of(path)
        clips.append(Clip(path, start, d))
        start += d
    return clips


def cut_span(clip_path: Path, out_path: Path, start: float, end: float) -> None:
    """Cuts one technique's span out of an already-encoded clip, for card and verify calls.

    The clip is already 2 fps and scaled by `cut`, so this only trims; the encode settings match
    it so a re-watch of the span looks like the same video, just shorter.
    """
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            str(start),
            "-to",
            str(end),
            "-i",
            str(clip_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "32",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-ac",
            "1",
            str(out_path),
        ]
    )


CANARY_COLORS = ("red", "green", "blue", "yellow", "magenta", "cyan", "white")


def canary_clip(out: Path, rng: random.Random) -> list[str]:
    """A clip of three random solid colours, 2 s each. Only a model that saw it can name them."""
    colors = rng.sample(CANARY_COLORS, 3)
    inputs: list[str] = []
    for c in colors:
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=320x240:d=2:r=10"]
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )
    return colors
