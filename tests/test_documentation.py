import json
from datetime import UTC, datetime, timedelta

import pytest
from test_retrieval import FakeEncoder

from distiller.db import Queue
from distiller.documentation import (
    DocCheck,
    DocChecks,
    check,
    report,
    resolve_checks,
    source_is_eligible,
)
from distiller.retrieval import Index


def source(**over):
    return {
        "id": "web:1",
        "kind": "web",
        "title": "Noise manual",
        "text": "A higher power narrows the rim.",
        "url": "https://docs.example.com/manual",
        "times": [],
        "captured_at": datetime.now(UTC).isoformat(),
        **over,
    }


def assessment(**over):
    return DocCheck(
        **{
            "claim": "k0",
            "source": "web:1",
            "quote": "A higher power narrows the rim.",
            "agrees": False,
            "disagrees": True,
            "applies_to_tutorial_version": True,
            "reason": "The documented exponent effect is the reverse.",
            **over,
        }
    )


def test_only_direct_anchored_version_applicable_unique_checks_can_flag():
    known = {"k0": "higher power widens the rim", "m0": "rim equation"}
    reply = DocChecks(checks=[assessment(), assessment(claim="invented")])
    resolved = resolve_checks(known, [source()], reply)
    assert [c["verdict"] for c in resolved] == ["contradicted", "unclear"]
    assert resolved[0]["source_url"] == "https://docs.example.com/manual"
    for invalid in (
        assessment(source="made-up"),
        assessment(quote="invented quote"),
        assessment(quote=""),
        assessment(applies_to_tutorial_version=False),
        assessment(agrees=True),
    ):
        assert (
            resolve_checks(known, [source()], DocChecks(checks=[invalid]))[0]["verdict"]
            == "unclear"
        )
    duplicates = DocChecks(checks=[assessment(), assessment()])
    assert resolve_checks(known, [source()], duplicates)[0]["verdict"] == "unclear"


def test_source_host_and_freshness_are_checked_before_ranking():
    hosts = ["docs.example.com"]
    assert source_is_eligible(source(), hosts, 7)
    assert not source_is_eligible(source(url="https://docs.example.com.evil/manual"), hosts, 7)
    assert not source_is_eligible(source(url="http://docs.example.com/manual"), hosts, 7)
    for age in (timedelta(days=8), timedelta(days=-1)):
        captured = (datetime.now(UTC) - age).isoformat()
        assert not source_is_eligible(source(captured_at=captured), hosts, 7)
    assert not source_is_eligible(source(captured_at="2026-10-06"), hosts, 7)


class Model:
    calls = 0
    fail = False

    async def fill(self, form, instructions, data):
        self.calls += 1
        assert form is DocChecks
        payload = json.loads(data)
        assert "claims" in payload["tutorial"] and payload["sources"][0]["id"] == "web:1"
        if self.fail:
            raise RuntimeError("model failed")
        return DocChecks(checks=[assessment()])


def queue():
    q = Queue(":memory:")
    q.enqueue(site="youtube", site_id="example1234", url="https://youtu.be/example1234")
    q.add_card(
        "youtube:example1234",
        "blender",
        "mask",
        "rim",
        {"idea": "rim", "knobs": [{"text": "higher power widens the rim", "t": 42}]},
    )
    return q


async def test_document_check_caches_context_and_flags_stale_cards_without_edits(tmp_path):
    q, index, encoder, model = queue(), Index(tmp_path / "search.sqlite"), FakeEncoder(), Model()
    src = source()
    index.build([src], tmp_path, encoder)
    before = q.cards()[0]["body"]
    args = (
        q,
        index,
        encoder,
        model,
        "fake",
        "technique:1",
        ["docs.example.com"],
        tmp_path / "checks",
    )
    result = await check(*args)
    assert result["checks"][1]["verdict"] == "contradicted"
    assert q.cards()[0]["body"] == before
    assert await check(*args) == result and model.calls == 1
    assert not report(q, tmp_path / "checks")[0]["stale_card"]
    assert not report(q, tmp_path / "checks")[0]["stale_sources"]
    q.update_card(1, {**before, "idea": "new idea"})
    assert report(q, tmp_path / "checks")[0]["stale_card"]
    model.fail = True
    with pytest.raises(RuntimeError, match="model failed"):
        await check(*args)
    assert report(q, tmp_path / "checks")[0]["input_hash"] == result["input_hash"]
    saved_path = tmp_path / "checks" / "technique-1.json"
    old_result = json.loads(saved_path.read_text())
    old_result["sources"][0]["captured_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    saved_path.write_text(json.dumps(old_result))
    assert report(q, tmp_path / "checks")[0]["stale_sources"]
    index.close()


async def test_missing_fresh_sources_does_not_call_reasoning_model(tmp_path):
    q, index, encoder, model = queue(), Index(tmp_path / "search.sqlite"), FakeEncoder(), Model()
    index.build([source()], tmp_path, encoder)
    with pytest.raises(ValueError, match="No fresh documentation"):
        await check(q, index, encoder, model, "fake", "technique:1", ["other.example"], tmp_path)
    assert model.calls == 0
    index.close()


async def test_workflow_steps_use_version_context_and_same_evidence_guards(tmp_path):
    q, index, encoder = queue(), Index(tmp_path / "search.sqlite"), FakeEncoder()
    q.add_workflow_card(
        "youtube:example1234",
        "edit archive",
        {
            "goal": "edit archive",
            "game_versions": ["3.3.5a"],
            "steps": [{"text": "pack patch", "t": 7}],
        },
    )
    index.build([source()], tmp_path, encoder)

    class WorkflowModel:
        async def fill(self, form, instructions, data):
            assert json.loads(data)["tutorial"]["game_versions"] == ["3.3.5a"]
            return DocChecks(checks=[assessment(claim="s0", applies_to_tutorial_version=False)])

    result = await check(
        q,
        index,
        encoder,
        WorkflowModel(),
        "fake",
        "workflow:1",
        ["docs.example.com"],
        tmp_path / "checks",
    )
    assert result["checks"][1]["verdict"] == "unclear"
    assert q.workflow_cards()[0]["body"]["steps"] == [{"text": "pack patch", "t": 7}]
    index.close()
