from distiller.forms import (
    Card,
    Check,
    Claim,
    Composition,
    Effect,
    Link,
    PreScreen,
    Reference,
    RefPart,
    Screen,
    Verification,
    apply_link_verification,
    apply_verification,
    card_depth,
    claim_ids,
    clean_reference,
    gaps,
    link_ids,
    prescreen_route,
    route,
    shift_card,
    shift_verification,
    technique_key,
)
from distiller.merge import Same, canonical_groups
from distiller.model import extract_json


def screen(**kw) -> Screen:
    base = dict(
        teaches=True,
        teaches_reasoning=True,
        ui_walkthrough_only=False,
        is_reference=False,
        made_with="blender",
        signals=["mask"],
        quality=4,
        reasons="",
    )
    return Screen(**{**base, **kw})


def card(recipe: int = 2, **kw) -> Card:
    base = dict(
        technique="Noise Dissolve",
        made_with="blender",
        idea="compare noise to a moving threshold",
        idea_t=1,
        result="the shape is eaten away",
        consumes=["time", "coords"],
        produces=["mask"],
        mediums=["shader"],
        app_chore=False,
        textbook_basic=False,
        non_obvious=False,
        has_numbers=False,
        mechanism=[Claim(text="m", t=2)],
        decisions=[Claim(text="d", t=3)],
        knobs=[Claim(text="k", t=3)],
        recipe=[Claim(text=f"r{i}", t=i) for i in range(recipe)],
        pitfalls=[Claim(text="p", t=4)],
    )
    return Card(**{**base, **kw})


def ok(claim: str, t: float | None = 1.0) -> Check:
    return Check(claim=claim, verdict="supported", t=t)


def test_route_learns_studies_or_drops_whatever_the_application() -> None:
    assert route(screen(), 3) == ("card", "teaches")
    assert route(screen(made_with="houdini"), 3) == ("card", "teaches")
    assert route(screen(made_with=None), 3) == ("card", "teaches")
    assert route(screen(teaches=False, is_reference=True), 3) == (
        "reference",
        "a look to replicate",
    )
    assert route(screen(teaches_reasoning=False, is_reference=True), 3)[0] == "reference"
    assert route(screen(teaches=False), 3)[0] == "drop"
    assert route(screen(teaches_reasoning=False), 3) == ("drop", "no reasoning, only UI steps")
    assert route(screen(quality=2), 3) == ("drop", "quality 2 < 3")


def test_claim_ids_cover_every_claim() -> None:
    assert set(claim_ids([card()])) == {
        "c0.idea",
        "c0.m0",
        "c0.d0",
        "c0.k0",
        "c0.r0",
        "c0.r1",
        "c0.p0",
    }


def test_verification_drops_unsupported_and_unmentioned_claims() -> None:
    checks = Verification(
        checks=[
            ok("c0.idea"),
            ok("c0.r1"),
            Check(claim="c0.r0", verdict="unclear", t=1),
            ok("c0.k0"),
            Check(claim="c0.d0", verdict="unsupported", t=3),
        ]
    )
    [kept] = apply_verification([card()], checks, duration=60)
    assert [c.text for c in kept.recipe] == ["r1"]
    assert [c.text for c in kept.knobs] == ["k"]
    assert kept.mechanism == [] and kept.decisions == [] and kept.pitfalls == []
    assert kept.produces == ["mask"] and kept.consumes == ["time", "coords"]


def test_card_needs_its_idea_and_a_mechanism_or_recipe() -> None:
    assert apply_verification([card()], Verification(checks=[ok("c0.r0")]), 60) == []
    only_knob = Verification(checks=[ok("c0.idea"), ok("c0.k0")])
    assert apply_verification([card()], only_knob, 60) == []
    [kept] = apply_verification([card()], Verification(checks=[ok("c0.idea"), ok("c0.m0")]), 60)
    assert kept.recipe == [] and [c.text for c in kept.mechanism] == ["m"]


def test_timestamp_outside_clip_is_not_evidence() -> None:
    checks = Verification(checks=[ok("c0.idea"), ok("c0.r0", t=500), ok("c0.r1", t=None)])
    assert apply_verification([card()], checks, duration=60) == []


def composition() -> Composition:
    def link(source: str, target: str) -> Link:
        return Link(source=source, signal="time", target=target, role="threshold", why="w", t=5)

    return Composition(
        effects=[
            Effect(
                name="Shockwave",
                result="r",
                links=[
                    link("Timing Curve", "Noise Dissolve"),
                    link("Timing Curve", "Invented Thing"),
                    link("Noise Dissolve", "Timing Curve"),
                ],
            ),
            Effect(name="Other", result="r", links=[link("Timing Curve", "Noise Dissolve")]),
        ]
    )


def test_links_survive_only_if_verified_and_both_ends_were_carded() -> None:
    comp = composition()
    assert set(link_ids(comp)) == {"e0.l0", "e0.l1", "e0.l2", "e1.l0"}
    checks = Verification(checks=[ok("e0.l0"), ok("e0.l1"), ok("e0.l2", t=None)])
    known = {"timing-curve", "noise-dissolve"}
    kept = apply_link_verification(comp, checks, duration=60, known=known)
    assert [e.name for e in kept.effects] == ["Shockwave"]
    assert [(link.source, link.target) for link in kept.effects[0].links] == [
        ("Timing Curve", "Noise Dissolve")
    ]


def test_reference_keeps_real_matches_and_turns_the_rest_into_searches() -> None:
    ref = Reference(
        name="Fire pillar",
        result="r",
        composition="c",
        parts=[
            RefPart(
                what="core",
                signal="mask",
                how="h",
                matches=["Noise Dissolve", "Made Up"],
                search="fire core shader",
            ),
            RefPart(
                what="rings", signal="shape", how="h", matches=["Nope"], search="orbit rings vfx"
            ),
            RefPart(what="glow", signal="screen", how="h", matches=[], search="  "),
        ],
    )
    cleaned = clean_reference(ref, {"noise-dissolve"})
    assert [p.matches for p in cleaned.parts] == [["Noise Dissolve"], [], []]
    assert gaps(cleaned) == ["orbit rings vfx"]


def test_technique_key() -> None:
    assert technique_key("  Noise Threshold -> Dissolve! ") == "noise-threshold-dissolve"


def test_canonical_groups_from_numbered_duplicates() -> None:
    names = ["a", "b", "c", "d", "e"]
    same = Same(same=[[0, 1, 99], [1, 2], [4, 4]])
    assert canonical_groups(names, same) == {"a": "a", "b": "a", "c": "c", "d": "d", "e": "e"}
    assert canonical_groups(names, Same(same=[])) == {n: n for n in names}


def test_extract_json_from_fenced_or_chatty_replies() -> None:
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('here: {"a": [1, 2]} done') == {"a": [1, 2]}


def test_spans_keep_in_clip_unique_unseen_techniques() -> None:
    from distiller.forms import Outline, Span
    from distiller.stages import spans

    outline = Outline(
        techniques=[
            Span(technique="Noise Mask", start=5, end=20),
            Span(technique="noise mask", start=30, end=40),
            Span(technique="Swirl", start=50, end=40),
            Span(technique="Twist", start=80, end=200),
            Span(technique="Timing Curve", start=0, end=4),
        ]
    )
    assert [s.technique for s in spans(outline, 92, ["timing curve"])] == ["Noise Mask"]


def test_extract_json_forgives_trailing_commas() -> None:
    assert extract_json('{"groups": [{"a": 1,}, ],}') == {"groups": [{"a": 1}]}


def test_extract_json_keeps_code_blocks_inside_strings() -> None:
    reply = '{"name": "x", "markdown": "**Recipe**\n```\nm = step(t, n)\n```\n1. step [1]"}'
    assert extract_json(reply)["markdown"].startswith("**Recipe**")
    assert extract_json("```json\n" + reply + "\n```")["name"] == "x"


def test_verification_drops_an_app_chore_even_when_verified() -> None:
    checks = Verification(checks=[ok("c0.idea"), ok("c0.m0")])
    assert apply_verification([card(app_chore=True)], checks, duration=60) == []


def test_shift_card_moves_idea_and_every_claim() -> None:
    shifted = shift_card(card(), offset=100.0)
    assert shifted.idea_t == 101.0
    assert [c.t for c in shifted.mechanism] == [102.0]
    assert [c.t for c in shifted.decisions] == [103.0]
    assert [c.t for c in shifted.recipe] == [100.0, 101.0]
    assert shifted.technique == card().technique  # only timestamps move


def test_shift_verification_moves_checks_but_leaves_null_alone() -> None:
    unclear = Check(claim="c0.m0", verdict="unclear", t=None)
    shifted = shift_verification(Verification(checks=[ok("c0.idea", t=5.0), unclear]), offset=10.0)
    assert [c.t for c in shifted.checks] == [15.0, None]


def prescreen(**kw) -> PreScreen:
    base = dict(
        on_topic=True,
        explains_why=True,
        has_mechanism_or_math_terms=True,
        mostly_ui_clicks=False,
        is_showcase_or_reel=False,
        depth=4,
        reasons="",
    )
    return PreScreen(**{**base, **kw})


def test_prescreen_route_drops_off_topic_clicks_and_shallow() -> None:
    assert prescreen_route(prescreen(), 2) == (True, "proceed")
    assert prescreen_route(prescreen(on_topic=False), 2)[0] is False
    assert prescreen_route(prescreen(mostly_ui_clicks=True, explains_why=False), 2) == (
        False,
        "button clicks, no reasoning",
    )
    # ui clicks alone, still explaining why, is not a clear enough signal to drop
    assert prescreen_route(prescreen(mostly_ui_clicks=True), 2)[0] is True
    assert prescreen_route(prescreen(depth=1), 2) == (False, "depth 1 < 2")
    assert prescreen_route(prescreen(depth=2), 2)[0] is True


def test_card_depth_zero_for_an_app_chore() -> None:
    assert card_depth(card(app_chore=True)) == 0


def test_card_depth_rewards_non_obvious_and_numbers_over_textbook_basics() -> None:
    basic = card_depth(card(textbook_basic=True, recipe=0))
    plain = card_depth(card(textbook_basic=False, non_obvious=False, recipe=0))
    deep = card_depth(card(non_obvious=True, has_numbers=True))
    assert basic < plain < deep
    assert 0 <= basic <= 5 and 0 <= deep <= 5


def test_card_depth_caps_at_five() -> None:
    assert card_depth(card(non_obvious=True, has_numbers=True, recipe=5)) == 5
