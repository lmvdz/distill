import json
import logging
from pathlib import Path

import pytest

from distiller.db import Queue
from distiller.studies import import_study


def good_card(
    technique: str = "Synthetic Alternating Mask", project: str = "fixture", **overrides
) -> dict:
    card = {
        "technique": technique,
        "made_with": "synthetic fixture",
        "idea": "This fabricated fixture assigns alternating sample indices to a binary mask.",
        "idea_t": 0.0,
        "result": "The synthetic fixture contains alternating zero and one values.",
        "consumes": ["coords"],
        "produces": ["mask"],
        "mediums": ["shader"],
        "app_chore": False,
        "textbook_basic": False,
        "non_obvious": True,
        "has_numbers": True,
        "mechanism": [
            {
                "text": "The fabricated fixture uses sample_index modulo two to alternate a bit.",
                "t": 0,
            }
        ],
        "decisions": [{"text": "Synthetic inputs use consecutive integer sample indices.", "t": 0}],
        "knobs": [{"text": "Synthetic initial parity changes the first mask bit.", "t": 0}],
        "recipe": [{"text": "fixture_mask = sample_index % 2", "t": 0}],
        "pitfalls": [],
        "project": project,
        "evidence": "source",
        "confidence": "high",
    }
    card.update(overrides)
    return card


def make_study(tmp_path: Path, cards: list[dict] | None = None) -> Path:
    study_dir = tmp_path / "synthetic-author"
    (study_dir / "shots").mkdir(parents=True)
    (study_dir / "shots" / "fixture-1.png").write_bytes(b"fake-png")
    (study_dir / "shots" / "fixture-2.png").write_bytes(b"fake-png-2")
    data = {
        "source": {
            "id": "synthetic-author",
            "author": "Synthetic Author",
            "author_url": "https://example.org/synthetic-author/profile",
            "home": "https://example.org/synthetic-author",
            "credit": "Original work by Synthetic Author — fabricated for automated tests.",
        },
        "projects": [
            {
                "slug": "fixture",
                "title": "Synthetic Mask Example",
                "url": "https://example.org/synthetic-author/fixture",
                "screenshots": ["shots/fixture-1.png", "shots/fixture-2.png"],
            }
        ],
        "cards": cards if cards is not None else [good_card()],
    }
    (study_dir / "cards.json").write_text(json.dumps(data), encoding="utf-8")
    (study_dir / "style.md").write_text(
        "Original work by Synthetic Author — fabricated for automated tests."
        "\n\n# Synthetic style\n\nThis fabricated style fixture uses two flat tones.",
        encoding="utf-8",
    )
    return study_dir


def test_import_creates_a_carded_web_row_never_a_leasable_one(tmp_path: Path) -> None:
    q = Queue(":memory:")
    study_dir = make_study(tmp_path)
    result = import_study(study_dir, q, tmp_path / "catalog")
    assert result.projects == 1 and result.kept == 1 and result.dropped == 0
    assert q.counts()["carded"] == 1
    assert q.lease(3600) is None  # never leasable, straight to carded


def test_card_depth_is_computed_and_thumbnail_is_the_first_screenshot(tmp_path: Path) -> None:
    q = Queue(":memory:")
    study_dir = make_study(tmp_path)
    catalog_dir = tmp_path / "catalog"
    import_study(study_dir, q, catalog_dir)
    [card] = q.cards()
    assert card["body"]["depth"] > 0
    assert card["body"]["media"] == {"thumb": "media/web_synthetic-author_fixture/fixture-1.png"}
    assert (catalog_dir / "media/web_synthetic-author_fixture/fixture-1.png").exists()
    assert (catalog_dir / "media/web_synthetic-author_fixture/fixture-2.png").exists()


def test_card_carries_attribution_and_evidence(tmp_path: Path) -> None:
    q = Queue(":memory:")
    study_dir = make_study(tmp_path)
    import_study(study_dir, q, tmp_path / "catalog")
    [card] = q.cards()
    body = card["body"]
    assert body["studied"] is True
    assert body["project"] == "fixture"
    assert body["project_title"] == "Synthetic Mask Example"
    assert body["author"] == "Synthetic Author"
    assert body["evidence"] == "source"
    assert body["confidence"] == "high"
    assert body["credit"].startswith("Original work by Synthetic Author")
    assert (
        card["url"] == "https://example.org/synthetic-author/fixture"
    )  # joined from the video row


def test_invalid_and_app_chore_cards_are_dropped_with_a_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    q = Queue(":memory:")
    broken = good_card(technique="Missing Fields")
    del broken["idea"]  # required by forms.Card
    chore = good_card(technique="Set a File Path", app_chore=True)
    study_dir = make_study(tmp_path, cards=[good_card(), broken, chore])
    with caplog.at_level(logging.WARNING):
        result = import_study(study_dir, q, tmp_path / "catalog")
    assert result.kept == 1 and result.dropped == 2
    assert [c["technique"] for c in q.cards()] == ["synthetic-alternating-mask"]


def test_card_for_unknown_project_is_dropped(tmp_path: Path) -> None:
    q = Queue(":memory:")
    study_dir = make_study(tmp_path, cards=[good_card(project="no-such-project")])
    result = import_study(study_dir, q, tmp_path / "catalog")
    assert result.kept == 0 and result.dropped == 1
    assert q.cards() == []


def test_reimport_replaces_cards_instead_of_duplicating(tmp_path: Path) -> None:
    q = Queue(":memory:")
    study_dir = make_study(tmp_path)
    import_study(study_dir, q, tmp_path / "catalog")
    import_study(study_dir, q, tmp_path / "catalog")
    assert len(q.cards()) == 1


def test_evidence_and_confidence_fall_back_when_missing_or_invalid(tmp_path: Path) -> None:
    q = Queue(":memory:")
    card = good_card()
    del card["evidence"]
    card["confidence"] = "extremely high"
    study_dir = make_study(tmp_path, cards=[card])
    import_study(study_dir, q, tmp_path / "catalog")
    [c] = q.cards()
    assert c["body"]["evidence"] == "inferred"
    assert c["body"]["confidence"] == "medium"


def test_style_guide_is_stored_with_credit_and_screenshots(tmp_path: Path) -> None:
    q = Queue(":memory:")
    study_dir = make_study(tmp_path)
    import_study(study_dir, q, tmp_path / "catalog")
    [style] = q.styles()
    assert style["source_id"] == "synthetic-author"
    assert style["author"] == "Synthetic Author"
    assert style["credit"].startswith("Original work by Synthetic Author")
    assert "# Synthetic style" in style["markdown"]
    assert style["screenshots"] == [
        "media/web_synthetic-author_fixture/fixture-1.png",
        "media/web_synthetic-author_fixture/fixture-2.png",
    ]


def test_reimport_replaces_the_style_guide(tmp_path: Path) -> None:
    q = Queue(":memory:")
    study_dir = make_study(tmp_path)
    import_study(study_dir, q, tmp_path / "catalog")
    (study_dir / "style.md").write_text("New credit line.\n\nUpdated guide.", encoding="utf-8")
    import_study(study_dir, q, tmp_path / "catalog")
    styles = q.styles()
    assert len(styles) == 1
    assert styles[0]["credit"] == "New credit line."
