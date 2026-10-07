"""Studied sources: knowledge distilled by hand or by another agent from a creator's own
projects, rather than watched from video. Code never asks a model here — the same rule as the
video path applies: cards arrive already in the closed `forms.Card` shape, and code alone
validates, drops what does not fit, and computes `depth`.

Input contract, produced elsewhere (see SPEC.md section 9):

    <dir>/cards.json = {
      "source": {"id", "author", "author_url", "home", "credit"},
      "projects": [{"slug", "title", "url", "screenshots": [...]}],
      "cards": [{...every forms.Card field..., "project", "evidence", "confidence"}],
    }
    <dir>/style.md   -- a style guide, first line is the credit
    <dir>/shots/*.png
"""

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .db import Queue
from .forms import Card, card_depth, technique_key

log = logging.getLogger("distill")

CARD_FIELDS = set(Card.model_fields)
EVIDENCE_VALUES = frozenset({"source", "inferred"})
CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})


def _dir_name(video_id: str) -> str:
    """A flat, filesystem-safe name for a video id, matching how previews name their folder
    (`:` replaced) but also flattening the `/` a studied video id carries (`web:<src>/<slug>`)."""
    return video_id.replace(":", "_").replace("/", "_")


def video_id(source_id: str, slug: str) -> str:
    return f"web:{source_id}/{slug}"


@dataclass
class ImportResult:
    source_id: str
    projects: int
    kept: int
    dropped: int
    reasons: list[str]


def _copy_screenshots(study_dir: Path, catalog_dir: Path, vid: str, shots: list[str]) -> list[str]:
    """Copies a project's screenshots into the catalog media folder, the same layout previews
    use, and returns their catalog-relative paths, first one first."""
    out_dir = catalog_dir / "media" / _dir_name(vid)
    out_dir.mkdir(parents=True, exist_ok=True)
    rels = []
    for shot in shots:
        src = study_dir / shot
        if not src.is_file():
            log.warning("import: screenshot %s missing, skipped", src)
            continue
        dest = out_dir / Path(shot).name
        shutil.copyfile(src, dest)
        rels.append(str(Path("media") / _dir_name(vid) / dest.name).replace("\\", "/"))
    return rels


def _validated_card(raw: dict[str, Any]) -> Card | None:
    """The card fields alone, validated against the closed form. None (with a logged reason)
    for anything that does not fit, same as a dropped video card."""
    try:
        return Card.model_validate({k: v for k, v in raw.items() if k in CARD_FIELDS})
    except ValidationError as err:
        log.warning("import: invalid card %r dropped: %s", raw.get("technique"), str(err)[:200])
        return None


def import_study(study_dir: Path, q: Queue, catalog_dir: Path) -> ImportResult:
    """Validates and stores one studied source: a `carded` row per project (no lease, no
    download, ever) and its cards, attributed and thumbnailed from the project's screenshots.
    Idempotent: clears each project's cards and replaces the style guide, so a re-import updates
    in place rather than duplicating."""
    data = json.loads((study_dir / "cards.json").read_text(encoding="utf-8"))
    source = data["source"]
    projects = {p["slug"]: p for p in data["projects"]}
    thumbs: dict[str, list[str]] = {}
    for slug, p in projects.items():
        vid = video_id(source["id"], slug)
        q.upsert_web_video(id=vid, url=p["url"], title=p["title"], seed=f"studied:{source['id']}")
        q.clear_cards(vid)
        thumbs[slug] = _copy_screenshots(study_dir, catalog_dir, vid, p.get("screenshots") or [])

    kept, dropped, reasons = 0, 0, []
    for raw in data["cards"]:
        slug = raw.get("project")
        if slug not in projects:
            dropped += 1
            reasons.append(f"unknown project {slug!r}")
            log.warning("import %s: card for unknown project %r dropped", source["id"], slug)
            continue
        card = _validated_card(raw)
        if card is None:
            dropped += 1
            reasons.append(f"invalid card in {slug}")
            continue
        if card.app_chore:  # dropped outright, exactly like a video's app-chore cards
            dropped += 1
            continue
        evidence = raw.get("evidence") if raw.get("evidence") in EVIDENCE_VALUES else "inferred"
        confidence = (
            raw.get("confidence") if raw.get("confidence") in CONFIDENCE_VALUES else "medium"
        )
        body = card.model_dump()
        body["depth"] = card_depth(card)
        body.update(
            project=slug,
            project_title=projects[slug]["title"],
            evidence=evidence,
            confidence=confidence,
            credit=source["credit"],
            author=source["author"],
            author_url=source.get("author_url"),
            studied=True,
        )
        if thumbs.get(slug):
            body["media"] = {"thumb": thumbs[slug][0]}
        q.add_card(
            video_id(source["id"], slug),
            body.get("made_with") or "unknown",
            body["produces"][0],
            technique_key(body["technique"]),
            body,
        )
        kept += 1

    style_path = study_dir / "style.md"
    if style_path.exists():
        markdown = style_path.read_text(encoding="utf-8")
        credit = markdown.splitlines()[0].strip() if markdown.strip() else source["credit"]
        # The style guide is illustrated with every project's screenshots, already copied above.
        shots = list(dict.fromkeys(rel for rels in thumbs.values() for rel in rels))
        q.save_style(
            source["id"],
            author=source.get("author"),
            author_url=source.get("author_url"),
            home=source.get("home"),
            credit=credit,
            markdown=markdown,
            screenshots=shots,
        )

    log.info(
        "import %s: %d projects, %d cards kept, %d dropped",
        source["id"],
        len(projects),
        kept,
        dropped,
    )
    return ImportResult(source["id"], len(projects), kept, dropped, reasons)
