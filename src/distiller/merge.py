"""Cards to entries: group equal techniques across videos and write one entry each; then
resolve observed compositions and studied references onto those entries.

The model proposes which technique names mean the same thing and writes each entry's prose.
Code checks the grouping, counts sources, unions the signals and mediums, and writes every
number and link.
"""

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from .audit import notes
from .db import Queue
from .forms import CLAIM_LISTS, technique_key
from .model import Model, ModelError

log = logging.getLogger("distill")

CLUSTER = """<data> is a numbered list of technique names extracted from different tutorials,
all producing the same kind of signal. Most are distinct. Find only the names that describe the
same technique: the same result by essentially the same method. Different methods for the same
result are distinct.
Answer with the numbers of each set of duplicates, e.g. {"same": [[3, 7], [12, 15, 20]]}. Leave
every distinct name out. If there are no duplicates, answer {"same": []}."""

ENTRY = """Write one library entry from the cards in <data>, which describe the same technique
from different tutorials. Two readers use it: an AI agent composing techniques into a new effect
and rebuilding it in any application, engine or shader; and a person browsing a gallery. The
entry teaches how to think about the technique, never which buttons to press in which program.

- `name`: a clear title-case name.
- `idea`: the core insight in one or two application-independent sentences (what it does and
  the principle that makes it work). This is what an agent reads first.
- `result`: one line on what the viewer sees.
- `use_when`: 2 to 5 short situations it suits, in a game developer's words (e.g. "spawn/despawn
  effect", "toon outline on emissive VFX").
- `markdown`: the body, in exactly this layout (omit a section whose cards have nothing for it):

**How it works**
- <mechanism> [n]

**Decisions**
- <choice and the reason given> [n]

**Knobs**
- <parameter>: <what changing it does> [n]

**Recipe**
```
<maths or pseudo-code, with the creators' values as examples>
```
<one line per step if the code needs it, each cited [n]>

**Pitfalls**
- <pitfall> [n]

Cite each line by the card's `source` number as [n]. Where sources differ, give the variants side
by side, each cited. Use only what the cards say: no values, reasons or steps of your own, no
application names or menus, and no source list (it is appended for you)."""


class EntryText(BaseModel):
    name: str
    idea: str
    result: str
    use_when: list[str] = Field(min_length=1, max_length=6)
    markdown: str


class Same(BaseModel):
    same: list[list[int]] = Field(description="Sets of 2 or more numbers naming one technique.")


def canonical_groups(names: list[str], same: Same) -> dict[str, str]:
    """Maps each name to its group's first name. Numbers out of range or already used are
    ignored; a name in no set stands alone."""
    out: dict[str, str] = {}
    for group in same.same:
        claimed = [names[i] for i in dict.fromkeys(group) if 0 <= i < len(names)]
        claimed = [n for n in claimed if n not in out]
        for member in claimed:
            out[member] = claimed[0]
    for name in names:
        out.setdefault(name, name)
    return out


def cards_hash(cards: list[dict[str, Any]]) -> str:
    """Changes when the cards or the entry prompt change, so either rewrites the entry."""
    payload = json.dumps(sorted(c["id"] for c in cards))
    if any(c["body"].get("citations") for c in cards):
        payload = json.dumps(sorted(cards, key=lambda c: c["id"]), sort_keys=True)
    return hashlib.sha256((payload + ENTRY).encode()).hexdigest()


def _sources(cards: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    urls: list[str] = []
    for c in cards:
        if c["url"] not in urls:
            urls.append(c["url"])
    return urls, {u: i + 1 for i, u in enumerate(urls)}


def _entry_input(cards: list[dict[str, Any]], index: dict[str, int]) -> str:
    return json.dumps([{**c["body"], "source": index[c["url"]]} for c in cards])


def evidence(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The claims whose timestamps are worth linking: how it works and how it is built."""
    return [*body["mechanism"], *body["recipe"]]


def source_list(cards: list[dict[str, Any]], urls: list[str]) -> list[dict[str, Any]]:
    """Each source with the step timestamps that cite it, for links and the footer. A studied
    source has no timestamps to cite — it is credited by project instead, with the evidence
    (from the source vs inferred) and confidence the import carried on the card."""
    out = []
    for n, url in enumerate(urls, 1):
        matches = [c for c in cards if c["url"] == url]
        studied = next((c["body"] for c in matches if c["body"].get("studied")), None)
        if studied:
            out.append(
                {
                    "n": n,
                    "url": url,
                    "times": [],
                    "published": studied.get("research_bundle", {}).get("published"),
                    "studied": True,
                    "project_title": studied.get("project_title"),
                    "credit": studied.get("credit"),
                    "author": studied.get("author"),
                    "author_url": studied.get("author_url"),
                    "evidence": studied.get("evidence"),
                    "confidence": studied.get("confidence"),
                    "evidence_level": studied.get("evidence_level"),
                    "maturity": studied.get("maturity"),
                    "citations": [
                        {"claim": claim["text"], **ref}
                        for c in matches
                        for claim in [
                            *[
                                {"text": c["body"][key], "citations": refs}
                                for key, refs in c["body"].get("citations", {}).items()
                            ],
                            *[
                                item
                                for _, field in CLAIM_LISTS
                                for item in c["body"].get(field, [])
                            ],
                        ]
                        for ref in claim.get("citations", [])
                    ],
                }
            )
            continue
        ts = sorted({int(x["t"]) for c in matches for x in evidence(c["body"])})
        date = next((c.get("upload_date") for c in matches), None)
        out.append({"n": n, "url": url, "times": ts[:8], "published": _iso(date)})
    return out


def _iso(date: str | None) -> str | None:
    return f"{date[:4]}-{date[4:6]}-{date[6:]}" if date and len(date) == 8 else None


def representative(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The media of the card with the most recipe steps, the one most likely to show it all."""
    with_media = [c for c in cards if c["body"].get("media")]
    if not with_media:
        return None
    return max(with_media, key=lambda c: len(c["body"]["recipe"]))["body"]["media"]


def union(cards: list[dict[str, Any]], field: str) -> list[str]:
    """Every value any card gives, in first-seen order."""
    return list(dict.fromkeys(v for c in cards for v in c["body"].get(field) or []))


async def _mapping(q: Queue, model: Model, signal: str, names: list[str]) -> dict[str, str]:
    """Which names are the same technique. Kept until the set of names changes, so a re-merge
    replays the earlier grouping instead of asking again."""
    if len(names) < 2:
        return {n: n for n in names}
    key, digest = f"clusters/{signal}", hashlib.sha256(json.dumps(names).encode()).hexdigest()
    cached = q.merged(key)
    if cached and cached[0] == digest:
        return json.loads(cached[1])
    try:
        numbered = "\n".join(f"{i}. {n}" for i, n in enumerate(names))
        same = await model.fill(Same, CLUSTER, data=numbered)
    except ModelError as err:
        # Ungrouped loses nothing, only leaves duplicates; not saved, so the next build retries.
        log.warning("grouping %s failed, leaving it ungrouped: %s", signal, err)
        return {n: n for n in names}
    mapping = canonical_groups(names, same)
    q.save_merge(key, digest, json.dumps(mapping))
    return mapping


def _is_cached(q: Queue, signal: str, name: str, members: list[dict[str, Any]]) -> bool:
    cached = q.merged(f"{signal}/{name}")
    return bool(cached and cached[0] == cards_hash(members))


async def _entry(
    q: Queue,
    model: Model,
    signal: str,
    name: str,
    members: list[dict[str, Any]],
    audits: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    urls, index = _sources(members)
    key, digest = f"{signal}/{name}", cards_hash(members)
    cached = q.merged(key)
    if cached and cached[0] == digest:
        text = EntryText.model_validate_json(cached[1])
    else:
        try:
            text = await model.fill(EntryText, ENTRY, data=_entry_input(members, index))
        except ModelError as err:
            # Left out of this build only; its cards stay, so the next build tries again.
            log.warning("entry %s failed, left out of this build: %s", key, str(err)[:160])
            return None
        q.save_merge(key, digest, text.model_dump_json())
    return {
        "id": key,
        "signal": signal,
        "key": name,
        **text.model_dump(),
        "consumes": union(members, "consumes"),
        "produces": union(members, "produces"),
        "mediums": union(members, "mediums"),
        "made_with": sorted({c["body"].get("made_with") or "unknown" for c in members}),
        "depth": max((c["body"].get("depth", 0) for c in members), default=0),
        "sources": source_list(members, urls),
        "media": representative(members),
        "card_ids": sorted(c["id"] for c in members),
        "technique_keys": sorted({c["technique"] for c in members}),
        "audit": [
            {**n, "source": index[c["url"]]} for c in members for n in notes(c, audits.get(c["id"]))
        ],
    }


async def merge(q: Queue, model: Model) -> list[dict[str, Any]]:
    """Every catalog entry, best-supported first within each signal it produces."""
    by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in q.cards():
        if "produces" in card["body"]:
            by_signal[card["topic"]].append(card)

    audits = q.audits()
    total = sum(map(len, by_signal.values()))
    log.info("catalog: %d cards across %d signals", total, len(by_signal))
    entries: list[dict[str, Any]] = []
    for signal, cards in sorted(by_signal.items()):
        mapping = await _mapping(q, model, signal, sorted({c["technique"] for c in cards}))
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in cards:
            groups[mapping[c["technique"]]].append(c)
        fresh = sum(1 for n, m in groups.items() if not _is_cached(q, signal, n, m))
        log.info(
            "  produces %s: %d cards -> %d techniques (%d to write, %d unchanged)",
            signal,
            len(cards),
            len(groups),
            fresh,
            len(groups) - fresh,
        )
        made = await asyncio.gather(
            *(_entry(q, model, signal, n, m, audits) for n, m in sorted(groups.items()))
        )
        found = [e for e in made if e is not None]
        found.sort(key=entry_sort_key)
        entries += found
    return entries


def entry_sort_key(e: dict[str, Any]) -> tuple[int, int, str]:
    """Deepest first, then best-supported, within a signal: a shallow app-chore-adjacent entry
    should not outrank a well-evidenced insight just because more videos happened to teach it."""
    return (-e["depth"], -len(e["sources"]), e["name"])


def entry_index(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Card technique key -> the id of the entry it was merged into."""
    return {k: e["id"] for e in entries for k in e["technique_keys"]}


def _observed(body: dict[str, Any], index: dict[str, str]) -> list[dict[str, Any]]:
    links = []
    for link in body["links"]:
        a = index.get(technique_key(link["source"]))
        b = index.get(technique_key(link["target"]))
        if a and b and a != b:
            links.append({**link, "source": a, "target": b})
    return links


def _parts(body: dict[str, Any], index: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {**p, "matches": [index[k] for k in map(technique_key, p["matches"]) if k in index]}
        for p in body["parts"]
    ]


def resolve_compositions(
    rows: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Observed compositions and studied references, with technique names replaced by entry
    ids. A link whose ends did not make it into the catalog is left out."""
    index = entry_index(entries)
    observed, references = [], []
    for row in rows:
        body = row["body"]
        source = {"url": row["url"], "published": _iso(row.get("upload_date"))}
        if row["kind"] == "observed":
            links = _observed(body, index)
            if links:
                observed.append(
                    {"name": body["name"], "result": body["result"], "links": links, **source}
                )
        else:
            parts = _parts(body, index)
            gaps = [p["what"] for p in parts if not p["matches"]]
            references.append({**body, "parts": parts, "gaps": gaps, **source})
    return observed, references
