"""Entries to a catalog folder with two front doors over the same content.

For agents: `llms.txt` (one line per technique: its idea, the signals it consumes and produces,
where it can be realized, when to use it), one Markdown file per technique, `signals.md` (what
makes and what uses each signal, for composing), `llms-full.txt` and `catalog.json` (the same as
a graph). For people: `index.html`, a gallery whose thumbnails play on hover. `SKILL.md` points
Claude at `llms.txt`.
"""

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import markdown

from .forms import SIGNALS, technique_key

TITLE = "Visual Techniques"
SUMMARY = (
    "Techniques for visual effects, real-time graphics, 3D modeling, animation and stylized "
    "looks, distilled from video tutorials and primary research "
    "into knowledge that works in any application, engine or shader: the idea behind each, how "
    "it works, the decisions and knobs that matter, and the signals it consumes and produces. "
    "Video claims cite checked timestamps; research claims cite source excerpts. "
    "Source support does not establish independent reproduction. One source means unconfirmed."
)
COMPOSE_HOWTO = (
    "Techniques pass signals: `time` (a 0..1 progress or clock), `mask` (a 0..1 field that "
    "selects where), `coords` (positions or UVs), `shape`, `deform`, `color`, `particles`, "
    "`screen` (post), `motion` (timing and spacing), `normal` (surface orientation), `light` "
    "(light arriving at a surface), `distance` (a signed distance field) and `buffer` (GPU data "
    "one pass writes and another reads). Compose by feeding one technique's "
    "output into another's input of the same type: a `time` driver into a `mask`'s threshold, "
    "a `coords` distortion into a noise, a `mask` into a `color` ramp. See `signals.md` for "
    "what makes and uses each signal, and Compositions below for combinations creators used."
)
TEMPLATE = Path(__file__).with_name("catalog.html")


def assign_slugs(entries: list[dict[str, Any]]) -> None:
    """A stable, unique path per entry: techniques/<signal>/<slug>.md."""
    taken: set[str] = set()
    for e in entries:
        base = technique_key(e["name"]) or technique_key(e["key"]) or "technique"
        slug, n = base, 2
        while f"{e['signal']}/{slug}" in taken:
            slug, n = f"{base}-{n}", n + 1
        taken.add(f"{e['signal']}/{slug}")
        e["path"] = f"techniques/{e['signal']}/{slug}.md"


def io_line(e: dict[str, Any]) -> str:
    takes = ", ".join(e["consumes"]) or "nothing"
    return f"{takes} → {', '.join(e['produces'])}"


def studied_credit(e: dict[str, Any]) -> str | None:
    """The credit line of a studied source among this entry's sources, or None if it has none.
    Used to mark attribution wherever the entry is shown, without disturbing entries that have
    none (every existing test and every video-only entry)."""
    return next((s["credit"] for s in e["sources"] if s.get("studied") and s.get("credit")), None)


def index_line(e: dict[str, Any]) -> str:
    n = len(e["sources"])
    base = (
        f"- [{e['name']}]({e['path']}): {e['idea'].rstrip('.')}. "
        f"Signals: {io_line(e)}. In: {', '.join(e['mediums'])}. "
        f"Use when: {'; '.join(e['use_when'])}. Depth: {e.get('depth', 0)}/5. "
        f"Sources: {n}{'' if n > 1 else ' (unconfirmed)'}"
    )
    credit = studied_credit(e)
    return f"{base}. {credit}" if credit else base


def _stamp(t: float) -> str:
    t = int(t)
    return f"{t // 60}:{t % 60:02d}"


def _at(url: str, t: float) -> str:
    return f"{url}&t={int(t)}s" if "youtube.com/watch" in url else url


def _studied_source_line(src: dict[str, Any]) -> str:
    label = (
        "from source"
        if src.get("evidence") in {"source", "primary-source-supported"}
        else "inferred"
    )
    confidence = src.get("confidence") or "unknown"
    title = src.get("project_title") or src["url"]
    line = (
        f"{src['n']}. studied: {title} (<{src['url']}>) — {src.get('credit') or ''} "
        f"[{label}, {confidence} confidence]"
    )
    if src.get("evidence_level"):
        line += f" — {src['evidence_level']}; {src.get('maturity') or 'maturity unknown'}"
    for ref in src.get("citations", []):
        line += f"\n   - {ref['claim']} — <{ref['url']}> ({ref['locator']}): “{ref['quote']}”"
    return line


def _source_links(e: dict[str, Any]) -> str:
    lines = []
    for src in e["sources"]:
        if src.get("studied"):
            lines.append(_studied_source_line(src))
            continue
        marks = ", ".join(f"[{_stamp(t)}]({_at(src['url'], t)})" for t in src["times"])
        when = f" (published {src['published']})" if src.get("published") else ""
        lines.append(f"{src['n']}. <{src['url']}>{when}{' at ' + marks if marks else ''}")
    return "\n".join(lines)


def neighbours(e: dict[str, Any], observed: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """(direction, other entry id, signal) for every observed link touching this entry."""
    out = []
    for comp in observed:
        for link in comp["links"]:
            if link["source"] == e["id"]:
                out.append(("feeds", link["target"], link["signal"]))
            elif link["target"] == e["id"]:
                out.append(("fed by", link["source"], link["signal"]))
    return list(dict.fromkeys(out))


def audit_md(e: dict[str, Any]) -> str:
    """Claims a second model judged wrong or overstated, without seeing the video. A note,
    not a removal: the entry above may still say it, and the auditor can be wrong too."""
    flagged = e.get("audit") or []
    if not flagged:
        return ""
    models = ", ".join(sorted({n["model"] for n in flagged}))
    lines = "".join(
        f"- **{n['verdict']}** (source {n['source']}): {n['text']}  \n  {n['reason']}"
        + (f" Should read: {n['correction']}" if n.get("correction") else "")
        + "\n"
        for n in flagged
    )
    return (
        f"## Audit notes\n\n> Checked by {models} from the claims alone, without the video.\n\n"
        f"{lines}\n"
    )


def technique_md(
    e: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    observed: list[dict[str, Any]],
    depth: int = 2,
) -> str:
    """One technique as a standalone file. `depth` is how far below the catalog root it sits."""
    up = "../" * depth
    thumb = f"\n![{e['name']}]({up}{e['media']['thumb']})\n" if e.get("media") else ""
    n = len(e["sources"])
    seen = neighbours(e, observed)
    combos = "".join(
        f"- {direction} [{by_id[other]['name']}]({up}{by_id[other]['path']}) via `{signal}`\n"
        for direction, other, signal in seen
        if other in by_id
    )
    credit = studied_credit(e)
    attribution = f"> Includes a studied source. {credit}\n\n" if credit else ""
    return (
        f"---\nconsumes: {json.dumps(e['consumes'])}\nproduces: {json.dumps(e['produces'])}\n"
        f"mediums: {json.dumps(e['mediums'])}\n"
        f"use_when: {json.dumps(e['use_when'], ensure_ascii=False)}\n"
        f"seen_in: {json.dumps(e['made_with'], ensure_ascii=False)}\nsources: {n}\n---\n\n"
        f"# {e['name']}\n\n> {e['idea']}\n\n**Result:** {e['result']}\n\n"
        f"**Signals:** {io_line(e)} · **In:** {', '.join(e['mediums'])}\n\n"
        f"**Use when:** {'; '.join(e['use_when'])}\n{thumb}\n"
        f"{attribution}"
        f"{e['markdown'].strip()}\n\n"
        + (f"## Seen combined with\n\n{combos}\n" if combos else "")
        + audit_md(e)
        + f"## Sources ({n}{'' if n > 1 else ', unconfirmed'})\n\n{_source_links(e)}\n"
    )


def _link_text(link: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    a, b = by_id[link["source"]]["name"], by_id[link["target"]]["name"]
    return f"{a} --{link['signal']}--> {b} ({link['role']})"


def compositions_lines(observed: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> str:
    lines = []
    for c in observed:
        wiring = "; ".join(_link_text(link, by_id) for link in c["links"])
        lines.append(f"- **{c['name']}**: {c['result'].rstrip('.')}. {wiring}. <{c['url']}>")
    return "\n".join(lines)


def references_lines(references: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> str:
    lines = []
    for r in references:
        built = sorted({by_id[m]["name"] for p in r["parts"] for m in p["matches"] if m in by_id})
        gaps = "; ".join(r["gaps"]) or "none"
        lines.append(
            f"- **{r['name']}** (inferred, seen in {r.get('made_with') or 'unknown'}): "
            f"{r['result'].rstrip('.')}. Buildable from: {', '.join(built) or 'nothing yet'}. "
            f"Not yet in the catalog: {gaps}. <{r['url']}>"
        )
    return "\n".join(lines)


def styles_lines(styles: list[dict[str, Any]]) -> str:
    lines = []
    for s in styles:
        author = (
            f"[{s['author']}]({s['author_url']})"
            if s.get("author_url")
            else (s.get("author") or "unknown")
        )
        home = f" · <{s['home']}>" if s.get("home") else ""
        lines.append(f"- {author}: {s['credit']}{home} → [style guide](styles/{s['source_id']}.md)")
    return "\n".join(lines)


def llms_txt(
    entries: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    references: list[dict[str, Any]],
    styles: list[dict[str, Any]] = (),  # type: ignore[assignment]
) -> str:
    by_id = {e["id"]: e for e in entries}
    by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_signal[e["signal"]].append(e)
    parts = [f"# {TITLE}\n\n> {SUMMARY}\n\n## How to compose\n\n{COMPOSE_HOWTO}\n"]
    for signal in SIGNALS:
        if by_signal.get(signal):
            parts.append(f"## Produces `{signal}`\n")
            parts.append("\n".join(index_line(e) for e in by_signal[signal]) + "\n")
    if observed:
        parts.append(
            f"## Compositions (observed, verified)\n\n{compositions_lines(observed, by_id)}\n"
        )
    if references:
        lines = references_lines(references, by_id)
        parts.append(f"## References (inferred breakdowns of looks)\n\n{lines}\n")
    if styles:
        parts.append(f"## Styles\n\n{styles_lines(styles)}\n")
    parts.append(
        "## Optional\n\n- [Signal map](signals.md): what makes and what uses each signal\n"
        "- [Everything in one file](llms-full.txt)\n"
        "- [Machine-readable](catalog.json): techniques, compositions, references and the graph\n"
    )
    return "\n".join(parts)


def signals_md(entries: list[dict[str, Any]]) -> str:
    out = [f"# Signal map\n\n{COMPOSE_HOWTO}\n"]
    for signal in SIGNALS:
        makes = [e for e in entries if signal in e["produces"]]
        uses = [e for e in entries if signal in e["consumes"]]
        if not makes and not uses:
            continue
        out.append(f"## `{signal}`\n")
        out.append("Made by: " + (", ".join(f"[{e['name']}]({e['path']})" for e in makes) or "-"))
        out.append(
            "\nUsed by: " + (", ".join(f"[{e['name']}]({e['path']})" for e in uses) or "-") + "\n"
        )
    return "\n".join(out)


def llms_full(
    entries: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    styles: list[dict[str, Any]] = (),  # type: ignore[assignment]
) -> str:
    by_id = {e["id"]: e for e in entries}
    body = "\n\n---\n\n".join(
        technique_md(e, by_id, observed, depth=0).split("---\n\n", 1)[1] for e in entries
    )
    out = f"# {TITLE}\n\n> {SUMMARY}\n\n## How to compose\n\n{COMPOSE_HOWTO}\n\n{body}"
    if styles:
        guides = "\n\n---\n\n".join(
            f"## Style: {s.get('author') or s['source_id']}\n\n{s['markdown'].strip()}\n"
            for s in styles
        )
        out += f"\n\n---\n\n# Styles\n\n{guides}"
    return out


SKILL = """---
name: visual-techniques
description: >-
  Techniques for visual effects, animation and stylized looks, distilled from video tutorials
  into application-independent knowledge: the idea behind each, how it works, the knobs that
  matter, and the signals it consumes and produces so techniques can be composed into new
  effects. Use when designing or building an effect, animation, shader or look in any engine
  or tool, or when combining techniques into something new.
---

# Visual techniques

Start with `llms.txt`: one line per technique with its idea, its signals (consumes → produces),
where it can be realized and when to use it, then observed compositions and inferred reference
breakdowns. To compose, match one technique's output signal to another's input (`signals.md`
lists what makes and uses each). Open only the technique files you need. One source means
unconfirmed. Some entries include a studied source: knowledge distilled from a creator's own
project rather than a watched video, always credited and marked from-source or inferred. A
`## Styles` section, when present, lists style guides (`styles/<id>.md`) worth matching when a
look, not just a technique, is asked for. `catalog.json` has the same data as a graph.
"""


def styles_data(styles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Styles as the gallery renders them: markdown turned to HTML, same as a technique body."""
    return [
        {
            "id": s["source_id"],
            "author": s.get("author"),
            "author_url": s.get("author_url"),
            "home": s.get("home"),
            "credit": s["credit"],
            "screenshots": s["screenshots"],
            "html": markdown.markdown(s["markdown"], extensions=["sane_lists", "fenced_code"]),
        }
        for s in styles
    ]


def gallery_html(
    entries: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    references: list[dict[str, Any]],
    styles: list[dict[str, Any]] = (),  # type: ignore[assignment]
) -> str:
    by_id = {e["id"]: e for e in entries}
    techniques = [
        {
            **{
                k: e[k]
                for k in (
                    "id",
                    "name",
                    "signal",
                    "idea",
                    "result",
                    "use_when",
                    "consumes",
                    "produces",
                    "mediums",
                    "made_with",
                    "sources",
                    "path",
                )
            },
            "media": e.get("media"),
            "html": markdown.markdown(
                f"{e['markdown']}\n\n{audit_md(e)}", extensions=["sane_lists", "fenced_code"]
            ),
            "combos": [
                {"dir": d, "id": other, "name": by_id[other]["name"], "signal": sig}
                for d, other, sig in neighbours(e, observed)
                if other in by_id
            ],
        }
        for e in entries
    ]
    data = {
        "techniques": techniques,
        "compositions": observed,
        "references": references,
        "styles": styles_data(styles),
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8")
    return page.replace("__TITLE__", html.escape(TITLE)).replace("__DATA__", blob)


def graph(observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source": link["source"],
            "target": link["target"],
            "signal": link["signal"],
            "role": link["role"],
            "composition": c["name"],
            "url": c["url"],
        }
        for c in observed
        for link in c["links"]
    ]


def publish(
    catalog_dir: Path,
    entries: list[dict[str, Any]],
    observed: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
    styles: list[dict[str, Any]] | None = None,
) -> None:
    observed, references, styles = observed or [], references or [], styles or []
    catalog_dir.mkdir(parents=True, exist_ok=True)
    assign_slugs(entries)
    by_id = {e["id"]: e for e in entries}
    techniques = catalog_dir / "techniques"
    if techniques.exists():
        for old in techniques.rglob("*.md"):
            old.unlink()
        # Folders from an older layout (e.g. per application) would linger as empty shells.
        for folder in sorted(techniques.rglob("*"), key=lambda d: len(d.parts), reverse=True):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    for e in entries:
        path = catalog_dir / e["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(technique_md(e, by_id, observed), encoding="utf-8")
    styles_dir = catalog_dir / "styles"
    styles_dir.mkdir(parents=True, exist_ok=True)
    keep = {f"{s['source_id']}.md" for s in styles}
    for old in styles_dir.glob("*.md"):
        if old.name not in keep:
            old.unlink()
    for s in styles:
        (styles_dir / f"{s['source_id']}.md").write_text(s["markdown"], encoding="utf-8")
    catalog = {
        "techniques": [
            {k: v for k, v in e.items() if k not in ("card_ids", "technique_keys")} for e in entries
        ],
        "compositions": observed,
        "references": references,
        "styles": styles_data(styles),
        "graph": graph(observed),
    }
    files = {
        "llms.txt": llms_txt(entries, observed, references, styles),
        "signals.md": signals_md(entries),
        "llms-full.txt": llms_full(entries, observed, styles),
        "SKILL.md": SKILL,
        "index.html": gallery_html(entries, observed, references, styles),
        "catalog.json": json.dumps(catalog, indent=1, ensure_ascii=False),
    }
    for name, text in files.items():
        (catalog_dir / name).write_text(text, encoding="utf-8")
