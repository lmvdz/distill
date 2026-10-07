import asyncio
import json
from pathlib import Path

import pytest

from distiller import runner, stages
from distiller.catalog import _stamp
from distiller.config import load
from distiller.db import Queue, Video
from distiller.forms import (
    Check,
    Claim,
    Verification,
    WorkflowCard,
    WorkflowCarded,
    WorkflowOutline,
    WorkflowScreen,
    WorkflowSpan,
    WorkflowTool,
    apply_workflow_verification,
    route_workflow,
    shift_workflow_card,
    workflow_claim_ids,
)
from distiller.media import Clip
from distiller.runner import Scope, load_seeds
from distiller.workflow_catalog import llms_txt, publish, workflow_md

WORKFLOW_SEED = "https://example.org/example-workflows"


def queue_with_two_seeds() -> Queue:
    q = Queue(":memory:")
    q.enqueue(
        site="youtube", site_id="wow1", url="https://y/wow1", seed=WORKFLOW_SEED, priority=1.0
    )
    q.enqueue(
        site="youtube", site_id="wow2", url="https://y/wow2", seed=WORKFLOW_SEED, priority=1.0
    )
    q.enqueue(site="youtube", site_id="gfx", url="https://y/gfx", seed="https://y/@GraphicsExample")
    q.enqueue(site="youtube", site_id="byc", url="https://y/byc", channel="Example-Workflows")
    return q


# --- --only ---------------------------------------------------------------------------------


def test_lease_only_takes_videos_whose_seed_channel_or_url_match() -> None:
    q = queue_with_two_seeds()
    got = {q.lease(60, only="example-workflows").id for _ in range(3)}  # seed x2, channel x1
    assert got == {"youtube:wow1", "youtube:wow2", "youtube:byc"}
    assert q.lease(60, only="example-workflows") is None  # the graphics video is never taken
    assert q.lease(60, only="youtube.com/watch|y/gfx").id == "youtube:gfx"


def test_lease_only_matches_the_url_too_and_ignores_case() -> None:
    q = queue_with_two_seeds()
    assert q.lease(60, only="WOW2$").id == "youtube:wow2"
    assert q.lease(60, only="nothing-matches") is None


def test_without_only_lease_is_unchanged() -> None:
    q = queue_with_two_seeds()
    assert sum(q.lease(60) is not None for _ in range(5)) == 4


def test_pending_requeue_and_recard_respect_only() -> None:
    q = queue_with_two_seeds()
    assert q.pending("example-workflows") == 3 and q.pending() == 4
    a, b = q.lease(60, only="wow1"), q.lease(60, only="gfx")
    q.finish(a.id, "failed", error="x")
    q.finish(b.id, "failed", error="x")
    assert q.requeue_failed("example-workflows") == 1
    assert q.counts()["failed"] == 1  # the graphics video stays failed
    q.finish("youtube:wow2", "carded")
    q.finish("youtube:gfx", "carded")
    assert q.requeue_for_new_forms("example-workflows") == ["youtube:wow2"]
    assert q.counts()["carded"] == 1


async def test_a_limit_stops_the_worker_after_that_many_videos(monkeypatch) -> None:
    q = queue_with_two_seeds()
    taken: list[Video] = []

    async def fake_work(n, video, *rest) -> None:
        taken.append(video)
        q.finish(video.id, "dropped")

    monkeypatch.setattr(runner, "_work", fake_work)
    scope = Scope(only="example-workflows", limit=2)
    await asyncio.wait_for(
        runner.worker(0, load(), q, None, None, asyncio.Event(), scope), timeout=5
    )
    assert len(taken) == 2 and scope.full()
    assert all("gfx" not in v.id for v in taken)
    assert q.pending("example-workflows") == 1


# --- mode -----------------------------------------------------------------------------------


def test_seed_mode_defaults_to_technique_and_rejects_unknown(tmp_path: Path) -> None:
    f = tmp_path / "seeds.toml"
    f.write_text(
        f'[[seed]]\nurl = "{WORKFLOW_SEED}"\nmode = "workflow"\n\n[[seed]]\nurl = "https://y/other"\n',
        encoding="utf-8",
    )
    seeds, _ = load_seeds(f)
    assert [s.mode for s in seeds] == ["workflow", "technique"]
    f.write_text(f'[[seed]]\nurl = "{WORKFLOW_SEED}"\nmode = "bogus"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="mode must be one of"):
        load_seeds(f)


def test_public_seed_example_is_a_technique_seed() -> None:
    seeds, _ = load_seeds(Path(__file__).parent.parent / "examples" / "seeds.example.toml")
    assert len(seeds) == 1
    assert seeds[0].mode == "technique"


def test_scope_tags_a_video_with_its_seeds_mode() -> None:
    scope = Scope(modes={WORKFLOW_SEED: "workflow"})
    video = Video(id="y:a", url="u", site="youtube", title=None, duration=1.0, attempts=1)
    assert scope.tag(video).mode == "technique"
    assert scope.tag(Video(**{**video.__dict__, "seed": WORKFLOW_SEED})).mode == "workflow"
    assert scope.tag(Video(**{**video.__dict__, "seed": "elsewhere"})).mode == "technique"


# --- routing a video through process() --------------------------------------------------------


def workflow_screen(**kw) -> WorkflowScreen:
    base = dict(
        on_topic=True,
        shows_workflow=True,
        made_with=["Noggit"],
        game_versions=["3.3.5a"],
        quality=4,
        reasons="",
    )
    return WorkflowScreen(**{**base, **kw})


def wf_card(**kw) -> WorkflowCard:
    base = dict(
        goal="Add a custom item with SQL",
        goal_t=1,
        result="the item drops in game",
        game_versions=["3.3.5a"],
        server_cores=["azerothcore"],
        tools=[
            WorkflowTool(name="Keira3", version="1.2", role="edit item_template", t=2),
            WorkflowTool(name="HeidiSQL", version=None, role="run the query", t=3),
        ],
        inputs=["SQL"],
        outputs=["SQL", "DBC"],
        steps=[Claim(text="open item_template", t=2), Claim(text="set entry to 90000", t=4)],
        pain_points=[Claim(text="server must restart", t=6)],
        time_spent=Claim(text="takes about five minutes", t=8),
    )
    return WorkflowCard(**{**base, **kw})


class Recorder:
    """A model that answers by form and records which instructions it was given."""

    def __init__(self, screen: WorkflowScreen) -> None:
        self.screen = screen
        self.calls: list[str] = []
        self.instructions: list[str] = []

    async def fill(self, form, instructions, clip=None, data=None):
        self.calls.append(form.__name__)
        self.instructions.append(instructions)
        if form is WorkflowScreen:
            return self.screen
        if form is WorkflowOutline:
            return WorkflowOutline(
                workflows=[WorkflowSpan(goal="Add a custom item", start=0, end=50)]
            )
        if form is WorkflowCard:
            return wf_card()
        if form is Verification:
            ids = list(json.loads(data))
            return Verification(checks=[Check(claim=i, verdict="supported", t=5) for i in ids])
        raise AssertionError(f"unexpected form {form.__name__}")


class FakeGates:
    async def run(self, site, fn, *args):
        return fn(*args)


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def no_date_gate(*a):
        return None

    async def forbidden_prescreen(*a):
        raise AssertionError("the caption pre-screen must not run for a workflow video")

    clip_file = tmp_path / "clip.mp4"
    clip_file.write_bytes(b"x")
    monkeypatch.setattr(stages, "_date_gate", no_date_gate)
    monkeypatch.setattr(stages, "_prescreen_gate", forbidden_prescreen)
    monkeypatch.setattr(stages, "download", lambda *a: clip_file)
    monkeypatch.setattr(stages, "cut", lambda *a: [Clip(clip_file, 100.0, 60.0)])
    monkeypatch.setattr(stages, "cut_span", lambda src, dst, lo, hi: dst.write_bytes(b"x"))
    s = load()
    s = type(s)(**{**s.__dict__, "data": tmp_path})
    q = Queue(":memory:")
    q.enqueue(site="youtube", site_id="a", url="https://y/a", seed=WORKFLOW_SEED)
    video = q.lease(60)
    return s, q, Video(**{**video.__dict__, "mode": "workflow"})


async def test_a_workflow_video_is_screened_carded_and_stored_apart(pipeline) -> None:
    s, q, video = pipeline
    model = Recorder(workflow_screen())
    out = await stages.process(video, s, q, model, FakeGates())
    assert (out.status, out.cards) == ("carded", 1)
    assert stages.WORKFLOW_SCREEN in model.instructions
    assert stages.SCREEN not in model.instructions
    assert q.cards() == []  # the technique library is untouched
    [row] = q.workflow_cards()
    assert row["video_id"] == video.id and row["body"]["goal"] == "Add a custom item with SQL"
    # clip start 100 s: every timestamp is stored in video time
    assert row["body"]["goal_t"] == 101 and row["body"]["steps"][1]["t"] == 104
    assert [t["name"] for t in row["body"]["tools"]] == ["Keira3", "HeidiSQL"]
    assert q.counts()["carded"] == 1


async def test_a_click_along_is_kept_in_workflow_mode_but_a_showcase_is_dropped(pipeline) -> None:
    s, q, video = pipeline
    model = Recorder(workflow_screen(shows_workflow=False))
    out = await stages.process(video, s, q, model, FakeGates())
    assert out.status == "dropped" and "no workflow shown" in out.note
    assert model.calls == ["WorkflowScreen"]
    assert q.workflow_cards() == []


async def test_a_technique_video_still_takes_the_technique_screen(
    pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, q, video = pipeline
    video = Video(**{**video.__dict__, "mode": "technique"})
    asked: list[str] = []

    async def prescreen_ok(*a):
        asked.append("prescreen")

    class TechniqueModel:
        async def fill(self, form, instructions, clip=None, data=None):
            asked.append(form.__name__)
            return stages.Screen(
                teaches=False,
                teaches_reasoning=False,
                ui_walkthrough_only=True,
                is_reference=False,
                made_with=None,
                signals=[],
                quality=1,
                reasons="",
            )

    monkeypatch.setattr(stages, "_prescreen_gate", prescreen_ok)
    out = await stages.process(video, s, q, TechniqueModel(), FakeGates())
    assert out.status == "dropped"
    assert asked == ["prescreen", "Screen"]


# --- forms and verification ---------------------------------------------------------------------


def test_route_workflow_keeps_walkthroughs_and_drops_the_rest() -> None:
    assert route_workflow(workflow_screen(), 2) == ("card", "shows a workflow")
    assert route_workflow(workflow_screen(on_topic=False), 2)[0] == "drop"
    assert route_workflow(workflow_screen(shows_workflow=False), 2)[0] == "drop"
    assert route_workflow(workflow_screen(quality=1), 2) == ("drop", "quality 1 < 2")


def test_only_supported_workflow_claims_survive() -> None:
    card = wf_card()
    ids = workflow_claim_ids([card])
    assert set(ids) == {
        "w0.goal", "w0.t0", "w0.t1", "w0.s0", "w0.s1", "w0.p0", "w0.time",
    }  # fmt: skip
    assert ids["w0.t0"] == "uses Keira3 1.2 to edit item_template"
    checks = Verification(
        checks=[
            Check(claim="w0.goal", verdict="supported", t=1),
            Check(claim="w0.t0", verdict="supported", t=2),
            Check(claim="w0.s0", verdict="supported", t=2),
            Check(claim="w0.s1", verdict="unsupported", t=4),
            Check(claim="w0.p0", verdict="supported", t=900),  # outside the 60 s clip
            Check(claim="w0.time", verdict="unclear", t=None),
        ]
    )
    [kept] = apply_workflow_verification([card], checks, duration=60)
    assert [c.text for c in kept.steps] == ["open item_template"]
    assert [t.name for t in kept.tools] == ["Keira3"]
    assert kept.pain_points == [] and kept.time_spent is None


def test_a_workflow_card_needs_its_goal_and_a_step_to_survive() -> None:
    card = wf_card()
    no_goal = Verification(checks=[Check(claim="w0.s0", verdict="supported", t=1)])
    assert apply_workflow_verification([card], no_goal, 60) == []
    no_steps = Verification(checks=[Check(claim="w0.goal", verdict="supported", t=1)])
    assert apply_workflow_verification([card], no_steps, 60) == []


def test_shifting_a_workflow_card_moves_every_timestamp() -> None:
    moved = shift_workflow_card(wf_card(), 10)
    assert moved.goal_t == 11 and moved.tools[0].t == 12
    assert [c.t for c in moved.steps] == [12, 14]
    assert moved.pain_points[0].t == 16 and moved.time_spent.t == 18
    assert shift_workflow_card(wf_card(time_spent=None), 10).time_spent is None


def test_workflow_carded_round_trips_through_the_memo() -> None:
    saved = WorkflowCarded(cards=[wf_card()]).model_dump()
    assert WorkflowCarded.model_validate(saved).cards[0].tools[0].version == "1.2"


# --- the separate catalog --------------------------------------------------------------------


def rows() -> list[dict]:
    body = wf_card().model_dump()
    return [
        {
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "t",
            "channel": "c",
            "upload_date": "20240101",
            "body": body,
        },
        {"url": "https://www.youtube.com/watch?v=def", "body": body},
    ]


def test_workflow_catalog_has_its_own_llms_txt_index_and_unique_files(tmp_path: Path) -> None:
    out = tmp_path / "catalog-workflows"
    publish(out, rows())
    for name in ("llms.txt", "llms-full.txt", "index.html", "catalog.json"):
        assert (out / name).exists()
    files = sorted(p.name for p in (out / "workflows").glob("*.md"))
    assert files == ["add-a-custom-item-with-sql-2.md", "add-a-custom-item-with-sql.md"]
    txt = (out / "llms.txt").read_text(encoding="utf-8")
    assert "## Game version 3.3.5a" in txt and "Keira3 1.2" in txt and "SQL → SQL, DBC" in txt
    assert "Visual Techniques" not in txt


def test_a_workflow_card_file_lists_ordered_steps_with_timestamped_links() -> None:
    [row, _] = rows()
    publish_rows = [row]
    from distiller.workflow_catalog import assign_paths

    assign_paths(publish_rows)
    md = workflow_md(row)
    assert "1. open item_template ([0:02](https://www.youtube.com/watch?v=abc&t=2s))" in md
    assert "2. set entry to 90000" in md
    assert "## Pain points and workarounds" in md and "## Time" in md
    assert _stamp(65) == "1:05"
    assert "about: takes about five minutes" in llms_txt(publish_rows)
