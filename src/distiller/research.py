"""Bounded Jev browsing. Save observed text separately from verified cards."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, parse_qs, urljoin, urlparse

import httpx

from .browser_transport import local_cdp
from .config import Settings
from .db import Queue
from .discovery import score
from .jev_gateway import transport

VISIBLE_LINKS = """(() => ({url: location.href, links: [...document.querySelectorAll('a[href]')]
    .filter(a => {const r=a.getBoundingClientRect(); return r.width && r.height &&
        r.bottom>0 && r.top<innerHeight && a.checkVisibility({checkVisibilityCSS:true});})
    .slice(0,500).map(a => ({url:a.href, title:a.innerText.trim().slice(0,300)}))}))()"""


class PageText(HTMLParser):
    """Static HTML only. Script/style/head content is never treated as documentation."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"head", "script", "style", "noscript"}:
            self.hidden += 1
        if not self.hidden and tag in {"a", "iframe"}:
            href = dict(attrs).get("href" if tag == "a" else "src")
            if href and len(self.links) < 500:
                url = urljoin(self.url, href)
                if urlparse(url).scheme in {"http", "https"}:
                    self.links.append({"url": url})

    def handle_endtag(self, tag: str) -> None:
        if tag in {"head", "script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def capture_http(url: str, directory: Path) -> Path:
    if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
        raise ValueError("research URL must be an HTTP(S) page")
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as err:
        raise ValueError(
            f"HTTP capture failed: {err}. Use Jev for pages requiring a browser."
        ) from err
    if "html" not in response.headers.get("content-type", ""):
        raise ValueError("HTTP research supports HTML pages; use Jev for rendered pages")
    parser = PageText(str(response.url))
    parser.feed(response.text)
    text = "\n".join(parser.parts)[:48000]
    if not text:
        raise ValueError("No static page text; use Jev for rendered pages")
    return _save_capture(
        url,
        "Read static HTML",
        "http-fetched",
        [{"url": str(response.url), "text": text, "links": parser.links, "capture_method": "http"}],
        directory,
    )


def _observe(pages: dict[str, dict[str, Any]], state: dict[str, Any]) -> None:
    page = state["page"]
    page_url, text = page["url"], page.get("text", "").strip()
    if urlparse(page_url).scheme not in {"http", "https"} or not text:
        return
    saved = pages.setdefault(
        page_url,
        {"url": page_url, "title": page.get("title"), "text": "", "observations": [], "links": []},
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest not in saved["observations"]:
        saved["text"] = (saved["text"] + "\n\n" + text).strip()[:24000]
        saved["observations"].append(digest)
    known = {link["url"] for link in saved["links"]}
    saved["links"].extend(link for link in page.get("links", []) if link["url"] not in known)
    saved["links"] = saved["links"][:500]


def _snapshot_links(agent: Any, state: dict[str, Any]) -> None:
    observed = agent.browser.evaluate(VISIBLE_LINKS)
    if observed and observed["url"] == state["page"]["url"]:
        state["page"]["links"] = observed["links"]


def capture(
    url: str,
    goal: str,
    directory: Path,
    max_steps: int = 20,
    settings: Settings | None = None,
) -> Path:
    if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
        raise ValueError("research URL must be an HTTP(S) page")
    if not goal.strip() or not 1 <= max_steps <= 80:
        raise ValueError("research needs a goal and a step limit between 1 and 80")
    if not (os.environ.get("TYPESAFE_API_KEY") or os.environ.get("EXPERIENTIAL_API_KEY")):
        raise ValueError(
            "Jev research needs TYPESAFE_API_KEY or EXPERIENTIAL_API_KEY (see .env.example)."
        )
    try:
        from jev_ultrafast import Agent
    except ImportError as err:
        raise ValueError("Browser research needs `uv sync --extra research`.") from err
    pages: dict[str, dict[str, Any]] = {}

    task = (
        "Research by reading and navigating public pages. Do not log in, post, send messages, "
        "upload, purchase, or change account settings. Stop when relevant source text is visible. "
        + goal.strip()
    )
    with transport(settings), local_cdp(), Agent(url, task, screenshots=False) as agent:
        state = agent.snapshot()
        _snapshot_links(agent, state)
        _observe(pages, state)
        for _ in range(max_steps):
            state = agent.command("tick")
            _snapshot_links(agent, state)
            _observe(pages, state)
            if state["status"] in {"done", "blocked"}:
                break
        status = state["status"] if state["status"] in {"done", "blocked"} else "step-limit"
    if not pages:
        raise ValueError("Jev observed no source text; no research capture saved.")
    return _save_capture(url, goal, status, list(pages.values()), directory)


def _save_capture(url: str, goal: str, status: str, pages: list[dict], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    ident = hashlib.sha256(f"{url}:{goal}:{now.isoformat()}".encode()).hexdigest()[:16]
    path = directory / f"{ident}.json"
    path.write_text(
        json.dumps(
            {
                "captured_at": now.isoformat(),
                "start_url": url,
                "goal": goal,
                "status": status,
                "evidence": "unverified-web-observation",
                "pages": pages,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _youtube_candidate(
    parsed: ParseResult, host: str, parts: list[str]
) -> tuple[str, str, str] | None:
    if host == "youtu.be":
        ident = parts[0]
    elif parts[0] in {"shorts", "embed"} and len(parts) == 2:
        ident = parts[1]
    elif parsed.path == "/watch":
        ident = parse_qs(parsed.query).get("v", [""])[0]
    else:
        return None
    if re.fullmatch(r"[\w-]{6,}", ident):
        return "youtube", ident, f"https://www.youtube.com/watch?v={ident}"
    return None


def video_candidate(url: str) -> tuple[str, str, str] | None:
    """Canonical single-video URLs only, parsed by host and path, never model suggestions."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    parts = parsed.path.strip("/").split("/")
    if host in {"youtube.com", "youtu.be"}:
        return _youtube_candidate(parsed, host, parts)
    elif host == "instagram.com" and len(parts) == 2 and parts[0] in {"p", "reel"}:
        if re.fullmatch(r"[\w-]+", parts[1]):
            return "instagram", parts[1], f"https://www.instagram.com/{parts[0]}/{parts[1]}/"
    elif host == "vimeo.com" and len(parts) == 1 and parts[0].isdigit():
        return "other", parts[0], f"https://vimeo.com/{parts[0]}"
    elif (
        host == "player.vimeo.com"
        and len(parts) == 2
        and parts[0] == "video"
        and parts[1].isdigit()
    ):
        return "other", parts[1], f"https://vimeo.com/{parts[1]}"
    elif host == "tiktok.com" and len(parts) == 3 and parts[1] == "video" and parts[2].isdigit():
        return "tiktok", parts[2], f"https://www.tiktok.com/{parts[0]}/video/{parts[2]}"
    return None


def enqueue_captures(q: Queue, directory: Path, limit: int = 50) -> tuple[int, int]:
    if limit < 1:
        raise ValueError("discovery limit must be positive")
    candidates = {}
    for path in sorted(directory.glob("*.json")):
        capture = json.loads(path.read_text(encoding="utf-8"))
        for page in capture["pages"]:
            for link in [{"url": page["url"], "title": page.get("title")}, *page.get("links", [])]:
                found = video_candidate(link["url"])
                if found:
                    site, ident, url = found
                    candidates.setdefault(
                        (site, ident), {"url": url, "title": link.get("title"), "seed": page["url"]}
                    )
    added = 0
    for (site, ident), candidate in candidates.items():
        priority = score(title=candidate["title"], duration=None, site=site, source_yield=0.5)
        added += q.enqueue(site=site, site_id=ident, priority=priority, **candidate)
        if added >= limit:
            break
    return len(candidates), added
