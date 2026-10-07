import json
import time
from dataclasses import replace

import httpx
import pytest

from distiller import autopilot, papers
from distiller.automation_store import Store
from distiller.config import load
from distiller.db import Queue
from distiller.frontier import (
    arxiv_bundles,
    bundle,
    canonical_url,
    eccv_bundles,
    page_bundles,
    public_url,
)
from distiller.merge import EntryText, Same, cards_hash, source_list
from distiller.model import Busy, ModelError, cli_reply
from distiller.retrieval import documents

TEXT = "The anisotropic kernel follows the surface metric instead of using a planar UV atlas."


def candidate():
    return bundle(
        "Intrinsic kernel texture",
        [{"url": "https://example.org/method", "role": "primary"}],
        user_selected=True,
        authors="Example authors",
    )


def extraction():
    claim = {"text": TEXT, "citations": [{"source": "s0.0", "quote": TEXT}]}
    return papers.Extraction.model_validate(
        {
            "cards": [
                {
                    "technique": "Intrinsic Kernel Texture",
                    "made_with": None,
                    "idea": claim,
                    "result": claim,
                    "consumes": ["shape"],
                    "produces": ["color"],
                    "mediums": ["shader"],
                    "app_chore": False,
                    "textbook_basic": False,
                    "non_obvious": True,
                    "has_numbers": False,
                    "mechanism": [claim],
                    "decisions": [],
                    "knobs": [],
                    "recipe": [],
                    "pitfalls": [],
                }
            ]
        }
    )


def checks():
    return papers.SourceVerification(
        checks=[
            papers.SourceCheck(claim=k, verdict="supported", reason="source")
            for k in papers.claims(extraction())
        ]
    )


def curation(**overrides):
    return papers.Curation.model_validate(
        {
            "on_topic": True,
            "teaches_mechanism": True,
            "mostly_promotion": False,
            "textbook_basic": False,
            "non_obvious": True,
            "depth": 4,
            "has_full_method": True,
            "has_implementation": False,
            "maturity": "preprint",
            "duplicate_of": None,
            "adds_tradeoff_or_evidence": False,
            "reasons": "A specific transferable method.",
            **overrides,
        }
    )


def research():
    page = {"url": "https://example.org/method", "text": TEXT, "links": []}
    return {
        "chunks": papers.chunks(page, 0),
        "pages": [page],
        "errors": [],
        "capture": "capture.json",
    }


class FakeModel:
    def __init__(self):
        self.calls = []

    async def fill(self, form, prompt, data):
        self.calls.append(form)
        if form == papers.Curation:
            return curation()
        if form == papers.Extraction:
            return extraction()
        if form == papers.SourceVerification:
            return checks()
        if form == Same:
            return Same(same=[])
        if form == EntryText:
            return EntryText(
                name="Intrinsic Kernel Texture",
                idea=TEXT,
                result=TEXT,
                use_when=["mesh texture"],
                markdown="**How it works**\n- " + TEXT + " [1]",
            )
        raise AssertionError(form)


def test_canonical_identity_and_public_source_validation():
    assert (
        canonical_url("http://arxiv.org/pdf/2401.12345v2.pdf") == "https://arxiv.org/abs/2401.12345"
    )
    assert (
        canonical_url("https://github.com/User/Repo/blob/main/README.md")
        == "https://github.com/user/repo"
    )
    for url in (
        "http://localhost/x",
        "http://127.0.0.1/x",
        "file:///C:/keys.env",
        "https://user:pass@example.org/x",
        "http://[::1]/",
        "https://example.local/",
    ):
        with pytest.raises(ValueError):
            public_url(url)


def test_arxiv_atom_versions_and_topic_gate():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/2401.12345v2</id><title>Intrinsic Mesh Textures</title>
    <summary>Surface texture method</summary><author><name>A Author</name></author>
    <published>2026-09-01</published><updated>2026-09-03</updated></entry></feed>"""
    item = arxiv_bundles(xml, {"kind": "arxiv", "query": "cat:cs.GR"})[0]
    assert item["authors"] == "A Author"
    assert item["identity"].endswith("2401.12345")
    assert item["version"] == "2026-09-03"
    with pytest.raises(ValueError):
        arxiv_bundles(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>error</id></entry></feed>', {}
        )


def test_eccv_merges_duplicate_presentations_and_keeps_poster_keywords():
    rows = [
        {
            "sourceid": 17,
            "name": "Kernel Method",
            "keywords": ["texture"],
            "authors": [{"fullname": "A"}],
            "virtualsite_url": "https://eccv.ecva.net/poster/17",
        },
        {
            "sourceid": -17,
            "name": "Kernel Method",
            "eventtype": "oral",
            "keywords": [],
            "url": "https://example.org/project",
            "paper_pdf_url": "https://arxiv.org/pdf/2401.12345",
        },
    ]
    items = eccv_bundles(
        json.dumps({"results": rows}), {"url": "https://eccv.ecva.net/feed", "year": 2026}
    )
    assert len(items) == 1 and len(items[0]["sources"]) == 3
    assert items[0]["identity"] == "https://arxiv.org/abs/2401.12345"
    assert items[0]["priority"] == 3


def test_store_restart_lease_ownership_and_global_daily_budget(tmp_path):
    path = tmp_path / "jobs.sqlite"
    a, b = Store(path), Store(path)
    item = candidate()
    assert a.put(item) and not b.put(item)
    first = a.claim(2, lease_s=10, now=time.time())
    assert b.claim(2) is None
    second = b.claim(2, now=time.time() + 11)
    assert second["attempts"] == 2
    assert not a.finish(first, "carded", {})
    assert b.finish(second, "carded", {})
    a.put(bundle("Other geometry", [{"url": "https://example.org/other"}]))
    assert a.claim(2) is None
    a.close()
    restarted = Store(path)
    assert restarted.claim(2) is None
    assert restarted.report()["counts"]["carded"] == 1
    restarted.close()
    b.close()


def test_store_deduplicates_venue_and_arxiv_and_revisits_new_versions(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    venue = bundle("Kernel Textures", [{"url": "https://eccv.ecva.net/paper/17"}])
    paper = bundle("Kernel Textures", [{"url": "https://arxiv.org/abs/2401.12345v1"}], version="v1")
    assert store.put(venue) and not store.put(paper)
    job = store.claim(50)
    assert len(job["body"]["sources"]) == 2
    store.finish(job, "carded", {})
    assert not store.put({**paper, "version": "v2"})
    assert store.claim(50)["body"]["version"] == "v2"
    store.close()


def test_distinct_papers_sharing_a_title_or_toolkit_are_not_conflated(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    one = bundle(
        "Mesh Method",
        [
            {"url": "https://arxiv.org/abs/2609.00001"},
            {"url": "https://github.com/example/toolkit"},
        ],
    )
    two = bundle(
        "Mesh Method",
        [
            {"url": "https://arxiv.org/abs/2609.00002"},
            {"url": "https://github.com/example/toolkit"},
        ],
    )
    assert store.put(one) and store.put(two)
    assert store.report()["counts"]["new"] == 2
    store.close()


def test_watch_waits_a_week_and_failure_backoff_is_not_success(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    store.put(candidate())
    job = store.claim(50)
    store.finish(job, "watch", {"reason": "abstract only"})
    assert store.claim(50) is None
    later = store.claim(50, now=time.time() + 8 * 86400)
    assert later is not None
    store.fail(later, "timeout", max_attempts=1)
    assert store.report()["counts"]["failed"] == 1
    assert store.retry() == 1
    assert store.claim(50) is not None
    store.close()


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"on_topic": False}, "dropped"),
        ({"mostly_promotion": True}, "dropped"),
        ({"has_full_method": False}, "watch"),
        ({"depth": 2}, "dropped"),
        ({"duplicate_of": "technique:7"}, "dropped"),
        ({"duplicate_of": "invented:1"}, "extract"),
        ({"duplicate_of": "technique:7", "adds_tradeoff_or_evidence": True}, "extract"),
    ],
)
def test_code_decides_curation(updates, expected):
    assert papers.curate_route(curation(**updates), [{"id": "technique:7"}])[0] == expected


def test_source_verification_requires_exact_unique_support_and_no_fake_times():
    ext = extraction()
    cards = papers.verified_cards(ext, checks(), research()["chunks"])
    assert len(cards) == 1 and cards[0]["depth"] >= 3
    assert "idea_t" not in cards[0] and "t" not in cards[0]["mechanism"][0]
    assert cards[0]["mechanism"][0]["citations"][0]["sha256"]
    ext.cards[0].mechanism[0].citations[
        0
    ].quote = "A made-up quote that never appeared in the source."
    assert papers.verified_cards(ext, checks(), research()["chunks"]) == []
    duplicate = checks()
    duplicate.checks.append(duplicate.checks[0])
    assert papers.verified_cards(extraction(), duplicate, research()["chunks"]) == []


def test_atomic_card_replacement_rolls_back_on_failure():
    q = Queue(":memory:")
    original = [("research", "color", "kernel", {"idea": "original"})]
    q.replace_studied_cards("web:test/method", "https://example.org/method", "Kernel", original)
    with pytest.raises(TypeError):
        q.replace_studied_cards(
            "web:test/method",
            "https://example.org/method",
            "Kernel",
            [original[0], ("research", "color", "bad", {"broken": object()})],
        )
    assert len(q.cards()) == 1 and q.cards()[0]["body"]["idea"] == "original"
    assert q.lease(3600) is None
    assert q.finished_since(0) == 0  # research never consumes the video's budget


async def test_model_answers_resume_without_repeating_successful_calls():
    model, q, item = FakeModel(), Queue(":memory:"), candidate()
    first = await papers.distill(item, research(), [], q, model, "fake")
    second = await papers.distill(item, research(), [], q, model, "fake")
    assert first == second and len(model.calls) == 3
    papers.store_cards(q, item, first)
    rows = q.cards()
    sources = source_list(rows, [item["identity"]])
    assert sources[0]["times"] == [] and sources[0]["citations"]
    changed = [{**rows[0], "body": {**rows[0]["body"], "idea": "updated"}}]
    assert cards_hash(rows) != cards_hash(changed)


def test_research_retry_does_not_compare_against_its_own_committed_cards(tmp_path):
    q, item = Queue(":memory:"), candidate()
    result = {
        "cards": papers.verified_cards(extraction(), checks(), research()["chunks"]),
        "capture": "capture.json",
        "curation": curation().model_dump(),
    }
    papers.store_cards(q, item, result)
    assert autopilot.comparisons(q, item, tmp_path, semantic=False) == []


async def test_complete_cycle_is_restartable_and_catalog_contains_citations(tmp_path, monkeypatch):
    s = replace(load(tmp_path), data=tmp_path / "data")
    store, q, model = Store(s.data / "automation.sqlite"), Queue(s.db_path), FakeModel()
    item = candidate()
    plan = {"bundle": [{"title": item["title"], "urls": [item["identity"]]}]}

    async def collect(*args):
        return research()

    monkeypatch.setattr(papers, "collect", collect)
    first = await autopilot.cycle(s, plan, store, q, model, 2, 50, False)
    assert first["research"][0]["status"] == "carded"
    assert first["catalog"]["status"] == "published"
    catalog = json.loads((s.catalog_dir / "catalog.json").read_text(encoding="utf-8"))
    entry = catalog["techniques"][0]
    assert entry["sources"][0]["citations"][0]["quote"] == TEXT
    text = (s.catalog_dir / entry["path"]).read_text(encoding="utf-8")
    assert "characters" in text and "not independently reproduced" in text
    second = await autopilot.cycle(s, plan, store, q, model, 2, 50, False)
    assert second["research"] == [] and second["catalog"]["status"] == "current"
    assert len(q.cards()) == 1
    store.close()


async def test_incomplete_catalog_preserves_published_files(tmp_path):
    s = replace(load(tmp_path), data=tmp_path / "data")
    store, q = Store(s.data / "automation.sqlite"), Queue(s.db_path)
    q.replace_studied_cards(
        "web:test/method",
        "https://example.org/method",
        "Kernel",
        [("research", "color", "kernel", {"produces": ["color"]})],
    )
    s.catalog_dir.mkdir()
    path = s.catalog_dir / "catalog.json"
    path.write_text("existing catalog", encoding="utf-8")

    class Broken:
        async def fill(self, *args, **kwargs):
            raise ModelError("rate limited")

    with pytest.raises(RuntimeError, match="incomplete"):
        await autopilot.refresh_catalog(s, store, q, Broken())
    assert path.read_text() == "existing catalog"
    assert store.get("catalog:fingerprint") is None
    store.close()


async def test_html_fetch_extracts_public_text_and_links():
    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<script>bad()</script><h1>Mesh method</h1>"
            '<a href="https://arxiv.org/abs/2401.12345">Paper</a>',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        pages = await papers.fetch_page(client, "https://example.org/method")
    assert "bad()" not in pages[0]["text"]
    assert papers.linked_sources(pages) == ["https://arxiv.org/abs/2401.12345"]


def test_author_video_embeds_enter_normal_verified_video_queue():
    from distiller.research import PageText

    parser = PageText("https://example.org/project")
    parser.feed(
        '<iframe src="https://www.youtube.com/embed/abcDEF12345"></iframe>'
        '<iframe src="https://player.vimeo.com/video/1234567"></iframe>'
    )
    q = Queue(":memory:")
    assert autopilot.enqueue_videos(q, [link["url"] for link in parser.links]) == 2
    assert q.pending() == 2
    assert q.counts().get("cards", 0) == 0  # embedded presence never verifies a technique


def test_pdf_pages_sharing_a_url_remain_distinct_search_documents(tmp_path):
    capture = {
        "captured_at": "2026-10-06T12:00:00+00:00",
        "status": "http-fetched",
        "pages": [
            {"url": "https://example.org/paper.pdf", "locator": "PDF page 1", "text": "method"},
            {"url": "https://example.org/paper.pdf", "locator": "PDF page 2", "text": "limits"},
        ],
    }
    (tmp_path / "capture.json").write_text(json.dumps(capture), encoding="utf-8")
    docs = documents(Queue(":memory:"), tmp_path)
    assert len({d["id"] for d in docs}) == 2
    assert {d["locator"] for d in docs} == {"PDF page 1", "PDF page 2"}


def test_singleton_lock_released_after_error(tmp_path):
    path = tmp_path / "autopilot.lock"
    with (
        autopilot.exclusive(path),
        pytest.raises(ValueError, match="already owns"),
        autopilot.exclusive(path),
    ):
        pass
    with autopilot.exclusive(path):
        pass


def test_course_feed_discovers_separate_author_slides():
    page = {
        "links": [
            {"url": "https://example.org/one.pdf"},
            {"url": "https://example.org/two.pdf"},
            {"url": "https://example.org/two.pdf"},
        ]
    }
    items = page_bundles([page], {"url": "https://example.org/course", "title": "Graphics Course"})
    assert len(items) == 3
    assert len({b["id"] for b in items}) == 3


def test_monthly_provider_limit_is_busy_not_bad_research():
    with pytest.raises(Busy):
        cli_reply("claude", 1, "", "You've hit your monthly spend limit", "")


def test_report_distinguishes_later_recovery_from_historical_search_failure(tmp_path):
    store = Store(tmp_path / "automation.sqlite")
    failure = {"finished_at": 10, "search": {"status": "error", "error": "CUDA out of memory"}}
    store.set("last_cycle", failure)
    store.set("index:recovery", {"status": "successful", "completed_at": 20, "documents": 42})
    displayed = autopilot.cycle_report(store)
    assert displayed["search"]["status"] == "recovered"
    assert displayed["search"]["failed_at"] == 10
    assert displayed["search"]["recovered_at"] == 20
    assert (
        "error" not in displayed["search"] and "previous_error_summary" not in displayed["search"]
    )
    assert store.get("last_cycle") == failure  # history is preserved, not overwritten
    store.set("last_cycle", {**failure, "finished_at": 30})
    assert autopilot.cycle_report(store)["search"]["status"] == "error"
    store.close()
