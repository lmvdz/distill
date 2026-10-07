import asyncio

import pytest

from distiller.db import Queue
from distiller.forms import Outline, Span
from distiller.model import AdaptiveGate, Busy, ModelError, _chunk_error, _classify
from distiller.stages import _remembered


def test_busy_responses_are_told_apart_from_real_errors() -> None:
    assert isinstance(_classify(503, '{"code":"chat_admission_busy"}'), Busy)
    assert isinstance(_classify(504, "local rate-limit execution expiration"), Busy)
    assert isinstance(_classify(429, "slow down"), Busy)
    assert not isinstance(_classify(400, "bad request"), Busy)
    assert isinstance(_classify(400, "bad request"), ModelError)
    assert isinstance(_chunk_error({"message": "[504]: Request exceeded ... rate-limit"}), Busy)
    assert not isinstance(_chunk_error({"message": "upstream exploded"}), Busy)


async def test_gate_halves_on_busy_and_recovers_on_success() -> None:
    gate = AdaptiveGate(4)
    for _ in range(2):
        await gate.acquire()
        await gate.release(busy=True)
    assert gate.limit == 1.0
    for _ in range(20):
        await gate.acquire()
        await gate.release(busy=False)
    assert gate.limit == 4.0
    assert gate.in_flight == 0


async def test_gate_holds_calls_beyond_the_limit() -> None:
    gate = AdaptiveGate(1)
    await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    await gate.release(busy=False)
    await asyncio.wait_for(waiter, 1)
    assert gate.in_flight == 1


async def test_remembered_answers_are_replayed_not_asked_again() -> None:
    q = Queue(":memory:")
    calls = 0

    async def ask() -> Outline:
        nonlocal calls
        calls += 1
        return Outline(techniques=[Span(technique="swirl", start=1, end=5)])

    first = await _remembered(q, "v", "outline:0", Outline, ask)
    again = await _remembered(q, "v", "outline:0", Outline, ask)
    assert calls == 1
    assert again == first
    assert await _remembered(q, "other", "outline:0", Outline, ask) == first
    assert calls == 2


async def test_a_failed_ask_saves_nothing() -> None:
    q = Queue(":memory:")

    async def boom() -> Outline:
        raise Busy("503")

    with pytest.raises(Busy):
        await _remembered(q, "v", "outline:0", Outline, boom)
    assert q.memo("v", "outline:0") is None


def test_renew_extends_only_a_held_lease() -> None:
    q = Queue(":memory:")
    q.enqueue(site="youtube", site_id="a", url="u")
    v = q.lease(60, now=1000)
    assert v is not None
    q.renew(v.id, 10**9)
    assert q.lease(60, now=2000) is None
    q.finish(v.id, "carded")
    q.renew(v.id, 10**9)
    assert q.counts()["carded"] == 1


def test_outline_with_spans_past_the_end_is_rejected() -> None:
    from distiller.stages import outline_problem

    ok = Outline(
        techniques=[
            Span(technique="a", start=0, end=100),
            Span(technique="b", start=100, end=379.5),
        ]
    )
    assert outline_problem(ok, 379) is None
    bad = Outline(
        techniques=[Span(technique="a", start=0, end=100), Span(technique="b", start=557, end=609)]
    )
    assert outline_problem(bad, 379) == "1 of 2 spans end after the clip's end (609 s > 379 s)"


def test_youtube_ready_names_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from distiller import runner

    monkeypatch.setattr(runner.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert len(runner.youtube_ready()) == 2
    monkeypatch.setattr(runner.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/bin/deno")
    assert runner.youtube_ready() == []


async def test_omniroute_down_waits_and_ends_busy_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from distiller import config, model

    m = model.Model(
        config.Settings(**{**config.load().__dict__, "omniroute_url": "http://127.0.0.1:9"})
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(model.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(model, "BUSY_PATIENCE_S", -1)

    async def refuse(body: dict) -> str:
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(m, "_stream", refuse)
    with pytest.raises(model.Busy):
        await m.chat([{"type": "text", "text": "hi"}])
    assert m.gate.in_flight == 0
    await m.close()


def test_sweep_work_removes_only_old_folders(tmp_path) -> None:
    import os
    import time

    from distiller.runner import sweep_work

    old, fresh = tmp_path / "old", tmp_path / "fresh"
    old.mkdir()
    fresh.mkdir()
    (old / "source.mp4").write_bytes(b"x")
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert sweep_work(tmp_path, 3600) == 1
    assert not old.exists() and fresh.exists()
    assert sweep_work(tmp_path / "missing", 3600) == 0


def test_only_a_normally_finished_reply_is_an_answer() -> None:
    from distiller.model import Truncated, _Transient, check_finish

    assert check_finish("stop", '{"a": 1}') == '{"a": 1}'
    assert check_finish(None, "text") == "text"
    with pytest.raises(Truncated):
        check_finish("length", '{"groups": [')
    with pytest.raises(_Transient):
        check_finish("error", "")
    with pytest.raises(_Transient):
        check_finish("stop", "   ")


def test_quota_exhaustion_waits_for_the_reset_omniroute_names() -> None:
    from distiller.model import Busy, _classify, busy_wait

    err = _classify(429, "All antigravity accounts have exhausted their quota", "300")
    assert isinstance(err, Busy) and err.retry_after == 300
    assert 301 <= busy_wait(err, 1) <= 310
    plain = _classify(503, "chat_admission_busy")
    assert isinstance(plain, Busy) and plain.retry_after is None
    assert busy_wait(plain, 1) <= 120 * 1.3
    assert _classify(429, "x", "soon").retry_after is None


def test_publish_creates_the_catalog_folder(tmp_path) -> None:
    from distiller.catalog import publish

    publish(tmp_path / "new" / "catalog", [])
    assert (tmp_path / "new" / "catalog" / "llms.txt").exists()


def test_a_card_verified_without_recipe_can_be_saved_and_replayed() -> None:
    from distiller.forms import Card
    from distiller.stages import Carded

    card = Card(
        technique="t",
        made_with=None,
        idea="i",
        idea_t=0,
        result="r",
        consumes=[],
        produces=["mask"],
        mediums=["shader"],
        app_chore=False,
        textbook_basic=False,
        non_obvious=False,
        has_numbers=False,
        mechanism=[{"text": "m", "t": 1}],
        decisions=[],
        knobs=[],
        recipe=[],
        pitfalls=[],
    )
    saved = Carded(cards=[card]).model_dump()
    assert Carded.model_validate(saved).cards[0].mechanism[0].text == "m"


def test_quota_in_the_stream_and_bot_checks_pause_instead_of_failing() -> None:
    from distiller.media import is_bot_check
    from distiller.model import Busy, _chunk_error

    quota = {"message": "[antigravity/gemini] All antigravity accounts have exhausted their quota"}
    assert isinstance(_chunk_error(quota), Busy)
    assert is_bot_check(Exception("ERROR: [youtube] x: Sign in to confirm you're not a bot"))
    assert not is_bot_check(Exception("HTTP Error 403: Forbidden"))


def test_a_failing_span_is_retried_then_dropped_on_the_last_attempt() -> None:
    from test_forms import card

    from distiller.stages import Carded, settle

    results = [Carded(cards=[card()]), ModelError("invalid Card")]
    with pytest.raises(RuntimeError, match="1 of 2 cards failed"):
        settle(results, last_try=False)
    assert [c.technique for c in settle(results, last_try=True)] == [card().technique]


def test_busy_always_sends_the_video_back() -> None:
    from distiller.stages import settle

    with pytest.raises(Busy):
        settle([Busy("admission")], last_try=True)


def test_span_bounds_pads_and_clamps_to_the_clip() -> None:
    from distiller.forms import Span
    from distiller.stages import span_bounds

    middle = Span(technique="t", start=100, end=140)
    assert span_bounds(middle, pad=8, duration=600) == (92, 148)
    at_start = Span(technique="t", start=2, end=20)
    assert span_bounds(at_start, pad=8, duration=600)[0] == 0
    at_end = Span(technique="t", start=590, end=598)
    assert span_bounds(at_end, pad=8, duration=598) == (582, 598)
    # a span the whole width of a short clip still yields a non-empty window
    whole = Span(technique="t", start=0, end=10)
    lo, hi = span_bounds(whole, pad=8, duration=10)
    assert lo < hi <= 10


async def test_prescreen_gate_skips_without_captions_and_drops_on_low_depth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from distiller import stages
    from distiller.config import load
    from distiller.db import Queue, Video
    from distiller.forms import PreScreen

    class FakeGates:
        async def run(self, site, fn, *args):
            return fn(*args)

    class FakeModel:
        def __init__(self, answer: PreScreen | None) -> None:
            self.answer = answer

        async def fill(self, form, instructions, clip=None, data=None):
            return self.answer

    s = load()
    video = Video(id="youtube:a", url="u", site="youtube", title="t", duration=100.0, attempts=0)
    q = Queue(":memory:")

    # no captions (or not YouTube): the gate never asks the model, and always proceeds
    monkeypatch.setattr(stages, "fetch_prescreen_text", lambda *a: None)
    gate = stages._prescreen_gate(video, s, q, FakeModel(None), FakeGates(), tmp_path)
    assert await gate is None

    # captions present but a clear click-along with no reasoning: dropped before any download
    monkeypatch.setattr(
        stages, "fetch_prescreen_text", lambda *a: {"title": "t", "captions": "click file, save"}
    )
    low = PreScreen(
        on_topic=True,
        explains_why=False,
        has_mechanism_or_math_terms=False,
        mostly_ui_clicks=True,
        is_showcase_or_reel=False,
        depth=1,
        reasons="",
    )
    reason = await stages._prescreen_gate(video, s, q, FakeModel(low), FakeGates(), tmp_path)
    assert reason == "button clicks, no reasoning"


def test_card_body_stores_depth_alongside_the_shifted_timestamps() -> None:
    from test_forms import card

    from distiller.stages import card_body

    body = card_body(card(non_obvious=True, has_numbers=True), offset=50.0)
    assert body["idea_t"] == 51.0
    assert body["depth"] == 5
