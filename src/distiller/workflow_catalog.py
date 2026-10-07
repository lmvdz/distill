"""The workflow catalog: one file per workflow card, an `llms.txt` and an `index.html`.

Kept apart from the technique catalog (`catalog.py`) so the graphics library stays clean. Cards
are not merged across videos: two modders doing the same task give two cards, each credited and
timestamped, since the differences (versions, tools, workarounds) are the point.
"""

import html
import json
from pathlib import Path
from typing import Any

from .catalog import _at, _stamp
from .forms import technique_key

TITLE = "Modding Workflows"
SUMMARY = (
    "How people make custom game content with real tools, distilled from tutorial videos: the "
    "goal, the tools and their versions, the ordered steps, the file formats going in and out, "
    "the game version targeted, the pain points and workarounds, and the time it took. Every "
    "claim was checked against its video and cites a timestamp."
)


def assign_paths(rows: list[dict[str, Any]]) -> None:
    """A stable, unique path per card: workflows/<slug>.md."""
    taken: set[str] = set()
    for row in rows:
        base = technique_key(row["body"]["goal"])[:80] or "workflow"
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        taken.add(slug)
        row["path"] = f"workflows/{slug}.md"


def _tools(body: dict[str, Any]) -> str:
    return (
        ", ".join(
            f"{t['name']} {t['version']}" if t["version"] else t["name"] for t in body["tools"]
        )
        or "none stated"
    )


def _formats(body: dict[str, Any]) -> str:
    return f"{', '.join(body['inputs']) or 'none'} → {', '.join(body['outputs']) or 'none'}"


def _link(row: dict[str, Any], t: float) -> str:
    return f"[{_stamp(t)}]({_at(row['url'], t)})"


def index_line(row: dict[str, Any]) -> str:
    b = row["body"]
    pains = len(b["pain_points"])
    return (
        f"- [{b['goal']}]({row['path']}): {b['result'].rstrip('.')}. "
        f"Versions: {', '.join(b['game_versions'])}. Cores: {', '.join(b['server_cores'])}. "
        f"Tools: {_tools(b)}. Files: {_formats(b)}. {len(b['steps'])} steps, "
        f"{pains} pain point{'' if pains == 1 else 's'}"
        + (f", about: {b['time_spent']['text']}" if b.get("time_spent") else "")
    )


def workflow_md(row: dict[str, Any]) -> str:
    b = row["body"]
    tools = "".join(
        f"- {t['name']}{f' {t["version"]}' if t['version'] else ''}: {t['role']} "
        f"({_link(row, t['t'])})\n"
        for t in b["tools"]
    )
    steps = "".join(
        f"{i}. {c['text']} ({_link(row, c['t'])})\n" for i, c in enumerate(b["steps"], 1)
    )
    pains = "".join(f"- {c['text']} ({_link(row, c['t'])})\n" for c in b["pain_points"])
    spent = b.get("time_spent")
    when = f" (published {row['upload_date']})" if row.get("upload_date") else ""
    return (
        f"---\ngame_versions: {json.dumps(b['game_versions'])}\n"
        f"server_cores: {json.dumps(b['server_cores'])}\n"
        f"inputs: {json.dumps(b['inputs'])}\noutputs: {json.dumps(b['outputs'])}\n---\n\n"
        f"# {b['goal']}\n\n> {b['result']}\n\n"
        f"**Game version:** {', '.join(b['game_versions'])} · "
        f"**Server core:** {', '.join(b['server_cores'])} · **Files:** {_formats(b)}\n\n"
        f"## Tools\n\n{tools or 'None stated.' + chr(10)}\n"
        f"## Steps\n\n{steps}\n"
        + (f"## Pain points and workarounds\n\n{pains}\n" if pains else "")
        + (f"## Time\n\n{spent['text']} ({_link(row, spent['t'])})\n\n" if spent else "")
        + f"## Source\n\n<{row['url']}>{when}, goal stated at {_link(row, b['goal_t'])}\n"
    )


def llms_txt(rows: list[dict[str, Any]]) -> str:
    parts = [f"# {TITLE}\n\n> {SUMMARY}\n"]
    by_version: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_version.setdefault(row["body"]["game_versions"][0], []).append(row)
    for version in sorted(by_version):
        parts.append(f"## Game version {version}\n")
        parts.append("\n".join(index_line(r) for r in by_version[version]) + "\n")
    parts.append(
        "## Optional\n\n- [Everything in one file](llms-full.txt)\n"
        "- [Machine-readable](catalog.json)\n"
    )
    return "\n".join(parts)


def llms_full(rows: list[dict[str, Any]]) -> str:
    body = "\n\n---\n\n".join(workflow_md(r).split("---\n\n", 1)[1] for r in rows)
    return f"# {TITLE}\n\n> {SUMMARY}\n\n{body}"


def gallery_html(rows: list[dict[str, Any]]) -> str:
    cards = []
    for r in rows:
        b = r["body"]
        steps = "".join(f"<li>{html.escape(c['text'])}</li>" for c in b["steps"])
        pains = "".join(f"<li>{html.escape(c['text'])}</li>" for c in b["pain_points"])
        text = " ".join(
            [b["goal"], b["result"], _tools(b), *b["game_versions"], *b["server_cores"]]
        )
        cards.append(
            f'<details data-text="{html.escape(text.lower(), quote=True)}"><summary>'
            f"<b>{html.escape(b['goal'])}</b> <small>{html.escape(', '.join(b['game_versions']))}"
            f" · {html.escape(_tools(b))} · {html.escape(_formats(b))}</small></summary>"
            f"<p>{html.escape(b['result'])}</p><ol>{steps}</ol>"
            + (f"<h4>Pain points</h4><ul>{pains}</ul>" if pains else "")
            + (
                f"<p>Time: {html.escape(b['time_spent']['text'])}</p>"
                if b.get("time_spent")
                else ""
            )
            + f'<p><a href="{html.escape(_at(r["url"], b["goal_t"]), quote=True)}">source video</a>'
            "</p></details>"
        )
    return (
        f"<!doctype html><meta charset=utf-8><title>{html.escape(TITLE)}</title>"
        "<style>body{font:15px system-ui;max-width:60rem;margin:2rem auto;padding:0 1rem}"
        "details{border:1px solid #8884;border-radius:6px;padding:.5rem 1rem;margin:.5rem 0}"
        "summary{cursor:pointer}input{width:100%;padding:.5rem;font-size:1rem}</style>"
        f"<h1>{html.escape(TITLE)}</h1><p>{html.escape(SUMMARY)}</p>"
        '<input id=q placeholder="filter: tool, format, version, goal">'
        f"<div id=list>{''.join(cards)}</div>"
        "<script>q.oninput=()=>{const t=q.value.toLowerCase();"
        "for(const d of list.children)d.hidden=!d.dataset.text.includes(t)}</script>"
    )


def publish(out_dir: Path, rows: list[dict[str, Any]]) -> None:
    """Writes the whole workflow catalog to `out_dir`, replacing the previous card files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    assign_paths(rows)
    folder = out_dir / "workflows"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.md"):
        old.unlink()
    for row in rows:
        (out_dir / row["path"]).write_text(workflow_md(row), encoding="utf-8")
    catalog = [
        {
            "path": r["path"],
            "url": r["url"],
            "title": r.get("title"),
            "channel": r.get("channel"),
            "published": r.get("upload_date"),
            **r["body"],
        }
        for r in rows
    ]
    files = {
        "llms.txt": llms_txt(rows),
        "llms-full.txt": llms_full(rows),
        "index.html": gallery_html(rows),
        "catalog.json": json.dumps({"workflows": catalog}, indent=1, ensure_ascii=False),
    }
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
