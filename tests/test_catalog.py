import json
from pathlib import Path

from distiller.catalog import (
    assign_slugs,
    gallery_html,
    index_line,
    llms_full,
    llms_txt,
    publish,
    studied_credit,
    technique_md,
)
from distiller.merge import entry_sort_key, representative, resolve_compositions, source_list
from distiller.previews import backfill, card_times, media_names, preview_window, speed_for


def entry(name: str = "Fresnel Rim", signal: str = "mask", n: int = 1, depth: int = 3) -> dict:
    return {
        "id": f"{signal}/{name.lower()}",
        "signal": signal,
        "key": name.lower(),
        "name": name,
        "idea": (
            "A Fresnel term peaks at grazing angles, so a hard step on it finds the silhouette."
        ),
        "result": "crisp black silhouette edges.",
        "use_when": ["toon outlines", "flame edges"],
        "consumes": ["coords"],
        "produces": [signal],
        "mediums": ["shader", "2d-frames"],
        "made_with": ["blender"],
        "depth": depth,
        "markdown": "**Recipe**\n1. rim = 1 - dot(n, v) [1]",
        "sources": [
            {"n": i + 1, "url": f"https://www.youtube.com/watch?v=v{i}", "times": [146, 151]}
            for i in range(n)
        ],
        "media": {"clip": "media/y_v0/fresnel.mp4", "thumb": "media/y_v0/fresnel.jpg"},
        "card_ids": [1],
        "technique_keys": [name.lower().replace(" ", "-")],
    }


def test_preview_window_hugs_the_steps_inside_the_video() -> None:
    assert preview_window([10, 20, 30], 100) == (9.0, 34.0)
    assert preview_window([0.5], 100) == (0.0, 4.5)
    assert preview_window([98], 99) == (97.0, 99.0)
    assert preview_window([50, 50], 200) == (49.0, 54.0)


def test_long_windows_become_a_timelapse() -> None:
    assert speed_for(6) == 1.0
    assert speed_for(40) == 5.0


def test_card_times_and_media_names() -> None:
    body = {
        "idea_t": 144.0,
        "recipe": [{"t": 146.0}, {"t": 161.0}],
        "mechanism": [{"t": 150.0}],
        "technique": "Fresnel Rim",
    }
    assert card_times(body) == [144.0, 146.0, 161.0, 150.0]
    assert media_names("youtube:abc", body) == (
        "media/youtube_abc/fresnel-rim.mp4",
        "media/youtube_abc/fresnel-rim.jpg",
    )


def test_slugs_are_unique_per_signal() -> None:
    es = [entry(), entry(), entry(signal="color")]
    assign_slugs(es)
    assert [e["path"] for e in es] == [
        "techniques/mask/fresnel-rim.md",
        "techniques/mask/fresnel-rim-2.md",
        "techniques/color/fresnel-rim.md",
    ]


def test_index_line_gives_idea_signals_mediums_and_use() -> None:
    e = entry()
    assign_slugs([e])
    assert index_line(e) == (
        "- [Fresnel Rim](techniques/mask/fresnel-rim.md): A Fresnel term peaks at grazing "
        "angles, so a hard step on it finds the silhouette. Signals: coords → mask. "
        "In: shader, 2d-frames. Use when: toon outlines; flame edges. Depth: 3/5. "
        "Sources: 1 (unconfirmed)"
    )
    two = entry(n=2)
    assign_slugs([two])
    assert index_line(two).endswith("Sources: 2")


def test_entries_sort_deepest_first_within_a_signal() -> None:
    shallow, deep, tie_a, tie_b = (
        entry("Shallow", depth=1),
        entry("Deep", depth=4),
        entry("Zed", depth=2, n=1),
        entry("Alpha", depth=2, n=1),
    )
    ordered = sorted([shallow, deep, tie_a, tie_b], key=entry_sort_key)
    assert [e["name"] for e in ordered] == ["Deep", "Alpha", "Zed", "Shallow"]


def observed(a: dict, b: dict) -> list[dict]:
    link = {
        "source": a["id"],
        "target": b["id"],
        "signal": "mask",
        "role": "the ramp input",
        "why": "w",
        "t": 12.0,
    }
    return [
        {
            "name": "Rim glow",
            "result": "glowing edges",
            "links": [link],
            "url": "u",
            "published": None,
        }
    ]


def test_llms_txt_groups_by_signal_and_lists_compositions() -> None:
    rim, glow = entry(), entry("Emission Ramp", signal="color")
    glow["consumes"] = ["mask"]
    es = [rim, glow]
    assign_slugs(es)
    ref = {
        "name": "Fire pillar",
        "result": "a pillar of fire",
        "made_with": "unreal",
        "url": "r",
        "parts": [
            {"what": "core", "signal": "mask", "how": "h", "matches": [rim["id"]], "search": "s"}
        ],
        "gaps": ["rings"],
        "composition": "c",
    }
    text = llms_txt(es, observed(rim, glow), [ref])
    assert text.startswith("# Visual Techniques\n\n> ")
    assert "## How to compose" in text
    assert text.index("## Produces `mask`") < text.index("## Produces `color`")
    assert "Fresnel Rim --mask--> Emission Ramp (the ramp input)" in text
    assert "Buildable from: Fresnel Rim. Not yet in the catalog: rings." in text


def test_technique_file_links_media_timestamps_and_neighbours() -> None:
    rim, glow = entry(), entry("Emission Ramp", signal="color")
    assign_slugs([rim, glow])
    by_id = {e["id"]: e for e in (rim, glow)}
    md = technique_md(rim, by_id, observed(rim, glow))
    assert "![Fresnel Rim](../../media/y_v0/fresnel.jpg)" in md
    assert "[2:26](https://www.youtube.com/watch?v=v0&t=146s)" in md
    assert "## Sources (1, unconfirmed)" in md
    assert "- feeds [Emission Ramp](../../techniques/color/emission-ramp.md) via `mask`" in md


def test_publish_writes_both_front_doors_and_the_graph(tmp_path: Path) -> None:
    rim, glow = entry(), entry("Emission Ramp", signal="color", n=2)
    publish(tmp_path, [rim, glow], observed(rim, glow), [])
    for name in (
        "llms.txt",
        "llms-full.txt",
        "signals.md",
        "catalog.json",
        "index.html",
        "SKILL.md",
    ):
        assert (tmp_path / name).exists(), name
    assert (tmp_path / "techniques/color/emission-ramp.md").exists()
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "__DATA__" not in page and "Emission Ramp" in page
    data = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert {e["name"] for e in data["techniques"]} == {"Fresnel Rim", "Emission Ramp"}
    assert "card_ids" not in data["techniques"][0]
    assert data["graph"] == [
        {
            "source": rim["id"],
            "target": glow["id"],
            "signal": "mask",
            "role": "the ramp input",
            "composition": "Rim glow",
            "url": "u",
        }
    ]
    signals = (tmp_path / "signals.md").read_text(encoding="utf-8")
    assert "## `mask`\n\nMade by: [Fresnel Rim]" in signals


def test_representative_media_is_the_fullest_card() -> None:
    cards = [
        {"body": {"recipe": [1], "media": {"clip": "a"}}},
        {"body": {"recipe": [1, 2, 3], "media": {"clip": "b"}}},
        {"body": {"recipe": [1, 2, 3, 4]}},
    ]
    assert representative(cards) == {"clip": "b"}
    assert representative([{"body": {"recipe": []}}]) is None


def test_source_list_numbers_urls_with_their_step_times() -> None:
    cards = [
        {
            "url": "u1",
            "upload_date": "20260227",
            "body": {"mechanism": [{"t": 9}], "recipe": [{"t": 5.2}]},
        },
        {"url": "u2", "body": {"mechanism": [], "recipe": [{"t": 70}]}},
    ]
    assert source_list(cards, ["u1", "u2"]) == [
        {"n": 1, "url": "u1", "times": [5, 9], "published": "2026-02-27"},
        {"n": 2, "url": "u2", "times": [70], "published": None},
    ]


def test_resolve_compositions_maps_names_to_entries_and_drops_unknown_ends() -> None:
    rim, glow = entry(), entry("Emission Ramp", signal="color")
    rows = [
        {
            "kind": "observed",
            "url": "u",
            "upload_date": "20260101",
            "body": {
                "name": "Rim glow",
                "result": "r",
                "links": [
                    {
                        "source": "Fresnel Rim",
                        "target": "Emission Ramp",
                        "signal": "mask",
                        "role": "x",
                        "why": "w",
                        "t": 1,
                    },
                    {
                        "source": "Fresnel Rim",
                        "target": "Gone",
                        "signal": "mask",
                        "role": "x",
                        "why": "w",
                        "t": 1,
                    },
                ],
            },
        },
        {
            "kind": "inferred",
            "url": "r",
            "upload_date": None,
            "body": {
                "name": "Pillar",
                "result": "r",
                "composition": "c",
                "made_with": None,
                "parts": [
                    {
                        "what": "core",
                        "signal": "mask",
                        "how": "h",
                        "matches": ["fresnel-rim", "gone"],
                        "search": "s",
                    },
                    {"what": "rings", "signal": "shape", "how": "h", "matches": [], "search": "s"},
                ],
            },
        },
    ]
    obs, refs = resolve_compositions(rows, [rim, glow])
    assert [(x["source"], x["target"]) for x in obs[0]["links"]] == [(rim["id"], glow["id"])]
    assert obs[0]["published"] == "2026-01-01"
    assert refs[0]["parts"][0]["matches"] == [rim["id"]]
    assert refs[0]["gaps"] == ["rings"]


def test_publish_removes_folders_left_by_an_older_layout(tmp_path: Path) -> None:
    stale = tmp_path / "techniques" / "blender" / "shading"
    stale.mkdir(parents=True)
    (stale / "old.md").write_text("x", encoding="utf-8")
    publish(tmp_path, [entry()], [], [])
    assert not (tmp_path / "techniques" / "blender").exists()
    assert (tmp_path / "techniques" / "mask").is_dir()


SYNTHETIC_CREDIT = "Original work by Synthetic Author — fabricated for automated tests."


def studied_card(url: str = "https://example.org/synthetic-author/fixture") -> dict:
    return {
        "url": url,
        "upload_date": None,
        "body": {
            "mechanism": [],
            "recipe": [],
            "studied": True,
            "project": "fixture",
            "project_title": "Synthetic Mask Example",
            "credit": SYNTHETIC_CREDIT,
            "author": "Synthetic Author",
            "author_url": "https://example.org/synthetic-author/profile",
            "evidence": "source",
            "confidence": "high",
        },
    }


def study_style() -> dict:
    return {
        "source_id": "synthetic-author",
        "author": "Synthetic Author",
        "author_url": "https://example.org/synthetic-author/profile",
        "home": "https://example.org/synthetic-author",
        "credit": SYNTHETIC_CREDIT,
        "markdown": "# Synthetic style\n\nThis fabricated style fixture uses two flat tones.",
        "screenshots": ["media/web_synthetic-author_fixture/fixture-1.png"],
    }


def test_source_list_marks_a_studied_source_with_no_timestamps() -> None:
    [src] = source_list([studied_card()], ["https://example.org/synthetic-author/fixture"])
    assert src["studied"] is True
    assert src["times"] == [] and src["published"] is None
    assert src["project_title"] == "Synthetic Mask Example"
    assert src["evidence"] == "source" and src["confidence"] == "high"
    assert src["credit"] == SYNTHETIC_CREDIT


def test_index_line_and_technique_file_carry_studied_attribution() -> None:
    e = entry()
    e["sources"] = source_list([studied_card()], ["https://example.org/synthetic-author/fixture"])
    assign_slugs([e])
    assert studied_credit(e) == SYNTHETIC_CREDIT
    assert index_line(e).endswith(SYNTHETIC_CREDIT)
    md = technique_md(e, {e["id"]: e}, [])
    assert f"> Includes a studied source. {SYNTHETIC_CREDIT}" in md
    assert (
        "studied: Synthetic Mask Example (<https://example.org/synthetic-author/fixture>) — "
        f"{SYNTHETIC_CREDIT} "
        "[from source, high confidence]" in md
    )


def test_video_only_entries_carry_no_studied_attribution() -> None:
    e = entry()
    assign_slugs([e])
    assert studied_credit(e) is None
    assert index_line(e).endswith("Sources: 1 (unconfirmed)")  # no trailing credit sentence
    assert "Includes a studied source" not in technique_md(e, {e["id"]: e}, [])


def test_llms_txt_lists_styles_with_author_and_link() -> None:
    text = llms_txt([], [], [], [study_style()])
    assert "## Styles" in text
    assert "[Synthetic Author](https://example.org/synthetic-author/profile)" in text
    assert "styles/synthetic-author.md" in text


def test_llms_full_includes_style_guides_verbatim() -> None:
    text = llms_full([], [], [study_style()])
    assert "# Styles" in text
    assert "This fabricated style fixture uses two flat tones." in text


def test_gallery_html_and_publish_include_a_styles_tab(tmp_path: Path) -> None:
    styles = [study_style()]
    page = gallery_html([], [], [], styles)
    assert '"styles"' in page and "Synthetic style" in page

    publish(tmp_path, [], [], [], styles)
    assert (tmp_path / "styles" / "synthetic-author.md").read_text(encoding="utf-8") == styles[0][
        "markdown"
    ]
    data = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert data["styles"][0]["id"] == "synthetic-author"
    assert "<h1>Synthetic style</h1>" in data["styles"][0]["html"]


def test_publish_removes_styles_no_longer_imported(tmp_path: Path) -> None:
    publish(tmp_path, [], [], [], [study_style()])
    assert (tmp_path / "styles" / "synthetic-author.md").exists()
    publish(tmp_path, [], [], [], [])
    assert not (tmp_path / "styles" / "synthetic-author.md").exists()


def test_backfill_never_downloads_a_studied_web_row() -> None:
    class FakeQueue:
        def cards(self) -> list[dict]:
            return [{"id": 1, "video_id": "web:synthetic-author/fixture", "url": "x", "body": {}}]

    made = backfill(FakeQueue(), Path("unused"), Path("unused"), "firefox")
    assert made == 0
