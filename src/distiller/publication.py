"""Immutable portable catalog versions, escaped HTML search and inspectable diffs."""

import html
import os
import tempfile
from pathlib import Path

from .directives import Directive, canonical


def atomic_file(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".distill-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def source_map(manifest: dict) -> dict:
    return {c["source"]: c for c in manifest["inputs"]["candidates"]}


def markdown(manifest: dict) -> str:
    semantic = manifest["semantic"]
    sources = source_map(manifest)
    lines = [
        f"# {semantic['catalog']}",
        "",
        manifest["inputs"]["directive"]["purpose"],
        "",
        "Source-checked claims; not independently reproduced.",
        "",
        f"Directive: `{semantic['directive']}`",
        f"Publication: `{manifest['id']}`",
        "",
    ]
    for record in semantic["records"]:
        source = sources[record["source"]]
        lines.extend(
            [
                f"## {record['title']['text']}",
                "",
                f"Source: [{source['metadata']['title']}]({source['url']})",
                "",
                "Authors: " + (", ".join(source["metadata"]["authors"]) or "unspecified"),
                "",
            ]
        )
        for field, claims in record["fields"].items():
            if not claims:
                continue
            lines.extend([f"### {field.replace('_', ' ')}", ""])
            for claim in claims:
                lines.extend([f"- {claim['text']} ({claim['kind']})"])
                for ref in claim["citations"]:
                    lines.append(
                        f"  - Evidence: {ref['quote']!r}; {ref['locator']}, "
                        f"characters {ref['start']}..{ref['end']}; `{ref['observation']}`"
                    )
            lines.append("")
    lines.extend(["## Relationships", ""])
    for edge in semantic["relationships"]:
        lines.append(
            f"- `{edge['source']}` **{edge['type']}** `{edge['target']}` "
            f"({edge['provenance']}): {edge['reason']}"
        )
    lines.extend(["", "Full decisions and original bounded observations: `manifest.json`.", ""])
    return "\n".join(lines)


def gallery(manifest: dict, *, versioned: bool = False) -> str:
    escape = html.escape
    semantic, sources = manifest["semantic"], source_map(manifest)
    prefix = "" if versioned else manifest["id"] + "/"
    articles = []
    for record in semantic["records"]:
        source = sources[record["source"]]
        fields = []
        for name, claims in record["fields"].items():
            if not claims:
                continue
            content = "".join(
                f"<li>{escape(claim['text'])} <small>{escape(claim['kind'])}</small>"
                + "".join(
                    f"<blockquote>{escape(ref['quote'])}<footer>"
                    f"{escape(ref['locator'])}, chars {ref['start']}..{ref['end']}"
                    "</footer></blockquote>"
                    for ref in claim["citations"]
                )
                + "</li>"
                for claim in claims
            )
            fields.append(f"<h3>{escape(name.replace('_', ' '))}</h3><ul>{content}</ul>")
        articles.append(
            f"<article id='{record['id']}'><h2>{escape(record['title']['text'])}</h2>"
            f"<a href='{escape(source['url'], quote=True)}'>"
            f"{escape(source['metadata']['title'])}</a>"
            f"<p>{escape(', '.join(source['metadata']['authors']) or 'Author unspecified')}</p>"
            + "".join(fields)
            + "</article>"
        )
    edges = "".join(
        f"<tr><td>{escape(e['type'])}</td><td>{escape(e['provenance'])}</td>"
        f"<td>{escape(e['reason'])}</td></tr>"
        for e in semantic["relationships"]
    )
    unresolved = [d for d in semantic["decisions"] if d.get("verdict") == "uncertain"]
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(semantic["catalog"])} · Distill</title>
<style>body{{font:16px/1.6 system-ui;margin:0;background:#f4f3ee;color:#24312b}}
main{{max-width:1000px;margin:3rem auto;padding:0 1.5rem}}h1{{font-size:3rem}}
input{{width:100%;box-sizing:border-box;padding:1rem;border:1px solid #aab9ad;border-radius:8px}}
article{{background:white;border:1px solid #d8ded6;padding:2rem;margin:1.5rem 0;border-radius:12px}}
blockquote{{border-left:3px solid #6f9077;padding-left:1rem;color:#526154}}
small,footer{{font-size:.8rem;color:#65746b}}a{{color:#2f6546}}table{{width:100%}}
td{{padding:.5rem;border-bottom:1px solid #d8ded6}}[hidden]{{display:none}}</style>
<main><p>DISTILL / LIVING KNOWLEDGE</p><h1>{escape(semantic["catalog"])}</h1>
<p>{escape(manifest["inputs"]["directive"]["purpose"])}</p>
<p>Source-checked claims; not independently reproduced. {len(unresolved)} uncertain selections.</p>
<p><a href="{prefix}catalog.json">JSON</a> ·
<a href="{prefix}catalog.md">Markdown</a> ·
<a href="{prefix}manifest.json">Evidence and decisions</a></p>
<label for="search">Search this catalog</label>
<input id="search" type="search" placeholder="Methods, steps, costs…">
<p id="count">{len(articles)} records</p><section>{"".join(articles)}</section>
<h2>Relationships</h2><table><thead><tr><th>Type</th><th>Origin</th><th>Reason</th></tr></thead>
<tbody>{edges}</tbody></table><footer>Publication {manifest["id"]}</footer></main>
<script>const items=[...document.querySelectorAll('article')];
document.querySelector('#search').addEventListener('input',e=>{{
const q=e.target.value.toLocaleLowerCase().trim().split(/\\s+/).filter(Boolean);
items.forEach(a=>a.hidden=!q.every(t=>a.textContent.toLocaleLowerCase().includes(t)));
document.querySelector('#count').textContent=items.filter(a=>!a.hidden).length+' records';
}});</script></html>"""


def export(manifest: dict, root: Path) -> Path:
    directive = Directive.model_validate(manifest["inputs"]["directive"])
    catalog = root / directive.catalog
    version = catalog / manifest["id"]
    version.mkdir(parents=True, exist_ok=True)
    files = {
        "catalog.json": canonical(manifest["semantic"]),
        "manifest.json": canonical(manifest),
        "catalog.md": markdown(manifest),
        "llms.txt": markdown(manifest),
        "index.html": gallery(manifest, versioned=True),
    }
    for name, body in files.items():
        atomic_file(version / name, body)
    # The root gallery is self-contained, and its links point to the immutable version.
    atomic_file(catalog / "index.html", gallery(manifest))
    atomic_file(
        catalog / "current.json",
        canonical({"version": manifest["id"], "semantic_hash": manifest["semantic_hash"]}),
    )
    return catalog / "index.html"


def diff(before: dict, after: dict) -> dict:
    old = {r["id"]: r for r in before["semantic"]["records"]}
    new = {r["id"]: r for r in after["semantic"]["records"]}
    old_edges = {r["id"] for r in before["semantic"]["relationships"]}
    new_edges = {r["id"] for r in after["semantic"]["relationships"]}
    return {
        "from": before["id"],
        "to": after["id"],
        "added": sorted(new.keys() - old.keys()),
        "withdrawn": sorted(old.keys() - new.keys()),
        "relationships_added": sorted(new_edges - old_edges),
        "relationships_removed": sorted(old_edges - new_edges),
    }


def search(manifest: dict, query: str, limit: int = 10) -> list[dict]:
    terms = set(query.casefold().split())
    if not terms or limit < 1:
        raise ValueError("search requires a query and a positive limit")
    ranked = []
    for record in manifest["semantic"]["records"]:
        text = canonical(record).casefold()
        score = sum(term in text for term in terms) / len(terms)
        if score:
            ranked.append({"score": score, **record})
    return sorted(ranked, key=lambda r: (-r["score"], r["id"]))[:limit]
