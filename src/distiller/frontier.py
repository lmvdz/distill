"""Public scholarly feeds and stable research bundle identities. No model decisions here."""

import hashlib
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

ARXIV = re.compile(r"/(?:abs|html|pdf)/(\d{4}\.\d{4,5})(v\d+)?(?:\.pdf)?/?$")
TOPICS = re.compile(
    r"render|shad|texture|mesh|topolog|geometry|geometrical|animation|motion|skeleton|rigging|"
    r"simulation|deform|relight|material|gaussian|3d|reconstruction|radiance|ray trac|"
    r"surface|styliz|composit|procedural|splat|nurbs|vector graphic|character|contact",
    re.I,
)


def public_url(url: str) -> str:
    p = urlparse(url)
    host = p.hostname or ""
    if (
        p.scheme not in {"http", "https"}
        or not host
        or p.username
        or p.password
        or host in {"localhost", "localhost.localdomain"}
        or "." not in host
        or host.replace(".", "").isdigit()
        or ":" in host
        or host.endswith((".local", ".internal"))
        or p.port not in {None, 80, 443}
    ):
        raise ValueError("research source must be a public HTTP(S) URL without credentials")
    return urlunparse(("https", p.netloc.lower(), p.path or "/", "", p.query, ""))


def arxiv_id(url: str) -> tuple[str, str] | None:
    p = urlparse(url)
    match = ARXIV.fullmatch(p.path)
    if p.hostname in {"arxiv.org", "export.arxiv.org", "www.arxiv.org"} and match:
        return match[1], match[2] or ""
    return None


def canonical_url(url: str) -> str:
    url = public_url(url)
    ident = arxiv_id(url)
    if ident:
        return f"https://arxiv.org/abs/{ident[0]}"
    p = urlparse(url)
    if p.hostname == "github.com":
        parts = p.path.strip("/").split("/")
        if len(parts) >= 2:
            return f"https://github.com/{parts[0].lower()}/{parts[1].removesuffix('.git').lower()}"
    return url.rstrip("/")


def bundle(title: str, sources: list[dict], **metadata: Any) -> dict[str, Any]:
    sources = [{**s, "url": public_url(s["url"])} for s in sources]
    if not sources:
        raise ValueError("a research bundle needs a source")
    paper = next((s for s in sources if arxiv_id(s["url"])), None)
    identity = canonical_url((paper or sources[0])["url"])
    return {
        "id": "research-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
        "identity": identity,
        "title": title,
        "sources": sources,
        "priority": 10 if metadata.get("user_selected") else 1,
        **metadata,
    }


def load_plan(path: Path) -> dict:
    plan = tomllib.loads(path.read_text(encoding="utf-8"))
    for feed in plan.get("feed", []):
        if feed["kind"] not in {"arxiv", "eccv", "page"}:
            raise ValueError(f"unsupported frontier feed: {feed['kind']}")
        if feed["kind"] != "arxiv":
            public_url(feed["url"])
    return plan


def configured_bundles(plan: dict) -> list[dict]:
    return [
        bundle(
            b["title"],
            [{"url": u, "role": "primary"} for u in b["urls"]],
            user_selected=True,
            authors=b.get("authors", ""),
            maturity=b.get("maturity", "unknown"),
        )
        for b in plan.get("bundle", [])
    ]


def page_bundles(pages: list[dict], feed: dict) -> list[dict]:
    """A public course/program can expose several independent slide PDFs."""
    resources = list(
        dict.fromkeys(
            link["url"]
            for page in pages
            for link in page.get("links", [])
            if urlparse(link["url"]).path.lower().endswith(".pdf")
        )
    )
    out = [bundle(feed.get("title", feed["url"]), [{"url": feed["url"], "role": "primary"}])]
    for url in resources[: int(feed.get("limit", 12))]:
        name = urlparse(url).path.rsplit("/", 1)[-1].removesuffix(".pdf").replace("%20", " ")
        out.append(
            bundle(
                feed.get("title", "Course material") + ": " + name,
                [{"url": url, "role": "author_slides"}],
                discovered_from=feed["url"],
                maturity="public course material",
            )
        )
    return out


def feed_url(feed: dict) -> str:
    if feed["kind"] != "arxiv":
        return feed["url"]
    order = feed.get("sort", "submittedDate")
    if order not in {"submittedDate", "lastUpdatedDate"}:
        raise ValueError("arXiv feed sort must be submittedDate or lastUpdatedDate")
    return "https://export.arxiv.org/api/query?" + urlencode(
        {
            "search_query": feed["query"],
            "sortBy": order,
            "sortOrder": "descending",
            "max_results": int(feed.get("limit", 80)),
        }
    )


def arxiv_bundles(text: str, feed: dict) -> list[dict]:
    root = ET.fromstring(text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise ValueError("arXiv returned something other than an Atom feed")
    out = []
    for e in root.findall("a:entry", ns):
        url = e.findtext("a:id", "", ns)
        if not arxiv_id(url):
            raise ValueError("arXiv error or malformed entry")
        title = " ".join(e.findtext("a:title", "", ns).split())
        abstract = e.findtext("a:summary", "", ns)
        if not TOPICS.search(title + " " + abstract):
            continue
        out.append(
            bundle(
                title,
                [{"url": url, "role": "paper"}],
                abstract=abstract,
                authors=", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)),
                published=e.findtext("a:published", "", ns),
                version=e.findtext("a:updated", "", ns),
                maturity="preprint; acceptance unverified",
                discovered_from=feed_url(feed),
            )
        )
        published = e.findtext("a:updated", "", ns) or e.findtext("a:published", "", ns)
        if published:
            date = datetime.fromisoformat(published.replace("Z", "+00:00")).replace(tzinfo=UTC)
            if (datetime.now(UTC) - date).days <= 14:
                out[-1]["priority"] = 3
    return out


def eccv_bundles(text: str, feed: dict) -> list[dict]:
    data = json.loads(text)
    rows = data.get("results")
    if not isinstance(rows, list) or not rows:
        raise ValueError("ECCV catalog missing presentation records")
    grouped: dict[int, dict] = {}
    for row in rows:
        key = abs(int(row["sourceid"]))
        group = grouped.setdefault(key, {"row": row, "urls": [], "keywords": [], "decisions": []})
        group["keywords"].extend(row.get("keywords") or [])
        group["decisions"].append(row.get("decision") or row.get("eventtype", ""))
        for field in ("virtualsite_url", "paper_pdf_url", "url"):
            url = row.get(field)
            if url and urlparse(url).scheme in {"http", "https"}:
                group["urls"].append({"url": url, "role": field})
    out = []
    for key, g in grouped.items():
        row = g["row"]
        title = row["name"]
        if not TOPICS.search(title + " " + " ".join(g["keywords"])) or not g["urls"]:
            continue
        sources = list({s["url"]: s for s in g["urls"]}.values())
        # A project arXiv link gives a cross-feed identity; otherwise the stable venue record.
        sources.sort(key=lambda s: (not bool(arxiv_id(s["url"])), s["role"] != "virtualsite_url"))
        item = bundle(
            title,
            sources,
            authors=", ".join(a.get("fullname", "") for a in row.get("authors", [])),
            maturity=f"ECCV {feed.get('year', '')} accepted; " + ", ".join(g["decisions"]),
            venue_id=f"eccv:{feed.get('year', '')}:{key}",
            discovered_from=feed["url"],
        )
        item["priority"] = 3 if any("oral" in d.lower() for d in g["decisions"]) else 2
        out.append(item)
    return out
