"""A thumbnail and a short looping clip per card, cut from the full-quality video.

The window comes from the card's own verified timestamps, so it shows the build and a
few seconds of the result after them. Long windows are sped up into a time-lapse.
"""

import logging
import shutil
from pathlib import Path
from typing import Any

from .forms import technique_key
from .media import _run, download, duration_of

log = logging.getLogger("distill")

LEAD_S = 1.0
TAIL_S = 4.0
MIN_WINDOW_S = 3.0
TARGET_S = 8.0
SCALE = "scale='if(gt(iw,ih),-2,360)':'if(gt(iw,ih),360,-2)'"


def card_times(body: dict[str, Any]) -> list[float]:
    times = [body["idea_t"], *(c["t"] for f in ("recipe", "mechanism") for c in body[f])]
    return [float(t) for t in times if t is not None]


def preview_window(times: list[float], duration: float) -> tuple[float, float]:
    """From just before the first step to a few seconds after the last, inside the video."""
    start = max(0.0, min(times) - LEAD_S)
    end = min(duration, max(times) + TAIL_S)
    if end - start < MIN_WINDOW_S:
        end = min(duration, start + MIN_WINDOW_S)
    return start, end


def speed_for(window_s: float) -> float:
    return max(1.0, window_s / TARGET_S)


def media_names(video_id: str, body: dict[str, Any]) -> tuple[str, str]:
    """Paths relative to the catalog root."""
    stem = f"media/{video_id.replace(':', '_')}/{technique_key(body['technique'])}"
    return f"{stem}.mp4", f"{stem}.jpg"


def cut_preview(source: Path, start: float, end: float, clip: Path, thumb: Path) -> None:
    clip.parent.mkdir(parents=True, exist_ok=True)
    speed = speed_for(end - start)
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start:.2f}",
            "-t",
            f"{end - start:.2f}",
            "-i",
            str(source),
            "-vf",
            f"setpts=PTS/{speed:.3f},fps=15,{SCALE}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(clip),
        ]
    )
    # The result is on screen just after the last step.
    at = max(start, end - TAIL_S + 1)
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{at:.2f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            SCALE,
            "-q:v",
            "4",
            str(thumb),
        ]
    )


def add_previews(
    source: Path, catalog_dir: Path, video_id: str, bodies: list[dict[str, Any]]
) -> int:
    """Cuts media for each card body and records it on the body. A card whose cut fails keeps
    no media; the card itself is never lost over a preview."""
    duration = duration_of(source)
    made = 0
    for body in bodies:
        times = card_times(body)
        if not times:
            continue
        clip_rel, thumb_rel = media_names(video_id, body)
        start, end = preview_window(times, duration)
        try:
            cut_preview(source, start, end, catalog_dir / clip_rel, catalog_dir / thumb_rel)
        except RuntimeError as err:
            log.warning("preview for %s / %s failed: %s", video_id, body["technique"], err)
            continue
        body["media"] = {"clip": clip_rel, "thumb": thumb_rel, "window": [start, end]}
        made += 1
    return made


REFERENCE_S = 12.0


def add_reference_preview(source: Path, catalog_dir: Path, video_id: str) -> dict[str, Any] | None:
    """A reference is the look itself, so its preview is its opening seconds."""
    stem = f"media/{video_id.replace(':', '_')}/reference"
    end = min(duration_of(source), REFERENCE_S)
    try:
        cut_preview(source, 0.0, end, catalog_dir / f"{stem}.mp4", catalog_dir / f"{stem}.jpg")
    except RuntimeError as err:
        log.warning("reference preview for %s failed: %s", video_id, err)
        return None
    return {"clip": f"{stem}.mp4", "thumb": f"{stem}.jpg", "window": [0.0, end]}


def backfill(q: Any, work_dir: Path, catalog_dir: Path, cookies_browser: str) -> int:
    """Re-downloads carded videos whose cards have no media yet, once each, and cuts them.

    A studied source's `web` rows are never downloadable: `distill import` gives every card its
    thumbnail from the source's own screenshots, so one missing here is left alone rather than
    handed to yt-dlp.
    """
    by_video: dict[str, list[dict[str, Any]]] = {}
    for card in q.cards():
        if card["video_id"].startswith("web:"):
            continue
        if not card["body"].get("media"):
            by_video.setdefault(card["video_id"], []).append(card)
    made = 0
    for video_id, cards in by_video.items():
        site, url = cards[0]["video_id"].split(":", 1)[0], cards[0]["url"]
        work = work_dir / f"preview_{video_id.replace(':', '_')}"
        try:
            source = download(url, site, work, cookies_browser)
            bodies = [c["body"] for c in cards]
            made += add_previews(source, catalog_dir, video_id, bodies)
            for card in cards:
                q.update_card(card["id"], card["body"])
            log.info("previews for %s: %d cards", video_id, len(cards))
        except Exception as err:  # one video's download must not stop the rest
            log.warning("previews for %s failed: %s", video_id, err)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return made
