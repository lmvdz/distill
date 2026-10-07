from typing import Any

from distiller.audit import audit, card_claims, notes, report
from distiller.catalog import audit_md
from distiller.db import Queue
from distiller.forms import Audit, AuditCheck


def body(**over: Any) -> dict[str, Any]:
    return {
        "technique": "fresnel rim",
        "idea": "Brighten edges by the angle between view and normal.",
        "result": "a glowing rim",
        "mechanism": [{"text": "rim = pow(1 - dot(N, V), 5)", "t": 3.0}],
        "decisions": [],
        "knobs": [{"text": "a higher power widens the rim", "t": 9.0}],
        "recipe": [],
        "pitfalls": [],
        **over,
    }


def queue(*bodies: dict[str, Any]) -> Queue:
    q = Queue(":memory:")
    q.enqueue(site="youtube", site_id="a", url="https://y/a")
    for b in bodies:
        q.add_card("youtube:a", "blender", "rim", "fresnel-rim", b)
    return q


class FakeModel:
    """Judges the knob wrong (a higher power narrows the rim) and the rest correct."""

    def __init__(self) -> None:
        self.calls = 0

    async def fill(self, form: type, instructions: str, clip: Any = None, data: Any = None) -> Any:
        self.calls += 1
        assert form is Audit and clip is None and "higher power" in data
        return Audit(
            checks=[
                AuditCheck(claim="idea", verdict="correct", reason="ok", correction=None),
                AuditCheck(claim="m0", verdict="correct", reason="Schlick", correction=None),
                AuditCheck(
                    claim="k0",
                    verdict="wrong",
                    reason="A higher exponent narrows the rim.",
                    correction="a higher power narrows the rim",
                ),
                AuditCheck(claim="x9", verdict="wrong", reason="invented id", correction=None),
            ]
        )


def test_claims_are_numbered_per_list() -> None:
    assert card_claims(body()) == {
        "idea": "Brighten edges by the angle between view and normal.",
        "m0": "rim = pow(1 - dot(N, V), 5)",
        "k0": "a higher power widens the rim",
    }


async def test_audit_flags_wrong_claims_and_skips_unchanged_cards() -> None:
    q, model = queue(body()), FakeModel()
    assert await audit(q, model, "claude:opus") == (1, 0, 0, 0)  # type: ignore[arg-type]
    flagged = report(q)
    assert [(n["claim"], n["verdict"]) for n in flagged] == [("k0", "wrong")]
    assert flagged[0]["text"] == "a higher power widens the rim"
    assert flagged[0]["model"] == "claude:opus"

    assert await audit(q, model, "claude:opus") == (0, 0, 1, 0)  # type: ignore[arg-type]
    assert await audit(q, model, "codex") == (1, 0, 0, 0)  # type: ignore[arg-type]
    assert await audit(q, model, "codex", force=True) == (1, 0, 0, 0)  # type: ignore[arg-type]
    assert model.calls == 3


async def test_an_audit_of_older_claims_is_ignored() -> None:
    q = queue(body())
    await audit(q, FakeModel(), "claude:opus")  # type: ignore[arg-type]
    card = q.cards()[0]
    changed = {**card, "body": body(knobs=[{"text": "a higher power narrows it", "t": 9.0}])}
    assert notes(changed, q.audits()[card["id"]]) == []


async def test_limit_audits_only_that_many() -> None:
    q = queue(body(), body(technique="other"))
    assert await audit(q, FakeModel(), "claude:opus", limit=1) == (1, 0, 0, 1)  # type: ignore[arg-type]


def test_audit_notes_render_as_a_section_and_are_absent_otherwise() -> None:
    entry = {
        "audit": [
            {
                "verdict": "wrong",
                "source": 2,
                "text": "a higher power widens the rim",
                "reason": "A higher exponent narrows the rim.",
                "correction": "a higher power narrows the rim",
                "model": "claude:opus",
            }
        ]
    }
    md = audit_md(entry)
    assert md.startswith("## Audit notes")
    assert "claude:opus" in md and "without the video" in md
    assert "**wrong** (source 2)" in md and "Should read: a higher power narrows the rim" in md
    assert audit_md({}) == "" and audit_md({"audit": []}) == ""


async def test_flagged_claims_reach_the_catalog_entry_with_their_source() -> None:
    from distiller.merge import EntryText, merge

    class Writer(FakeModel):
        async def fill(self, form: type, instructions: str, clip: Any = None, data: Any = None):
            if form is Audit:
                return await super().fill(form, instructions, clip, data)
            return EntryText(name="Fresnel rim", idea="i", result="r", use_when=["u"], markdown="m")

    card = body(produces=["rim"], consumes=[], mediums=["shader"], made_with="blender")
    q, model = queue(card), Writer()
    await audit(q, model, "claude:opus")  # type: ignore[arg-type]
    [entry] = await merge(q, model)  # type: ignore[arg-type]
    assert [(n["claim"], n["source"], n["model"]) for n in entry["audit"]] == [
        ("k0", 1, "claude:opus")
    ]
