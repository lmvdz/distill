import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from distiller.demo import TEXT, FixtureModel, demonstration
from distiller.directives import (
    Directive,
    canonical,
    compile_request,
    identity,
    preset,
    source_url,
)
from distiller.distill_store import Registry
from distiller.engine import (
    Assessment,
    Extraction,
    Relationships,
    Verification,
    execute,
    replay,
    seal,
)
from distiller.library import ChangeBatch, FixtureAdapter, LibraryItem, SQLiteAdapter
from distiller.lineage import import_catalog
from distiller.maintenance import acquire, capture_pages, cycle, maintain, shared_capture
from distiller.migration import migrate
from distiller.model import Busy
from distiller.publication import diff, export, gallery, search


@pytest.fixture
def registry(tmp_path):
    store = Registry(tmp_path / "distill.sqlite")
    yield store
    store.close()


def saved_item(**changes):
    body = {
        "external_id": "bracket",
        "revision": "1",
        "url": "https://example.org/bracket",
        "title": "Bracket fabrication",
        "authors": ["Example author"],
        "collections": ["robotics"],
        "tags": [{"value": "affordable", "origin": "user"}],
        "capture": {"text": TEXT, "origin": "synthetic fixture"},
    }
    return LibraryItem.model_validate({**body, **changes})


def prepare(store, key="fabrication", items=None):
    directive = preset(key, "Catalog affordable fabrication methods", "fabrication")
    directive.selection.libraries = ["bookmarks"]
    directive.relationships.types = ["alternative_to"]
    store.save_directive(directive)
    store.import_batch(
        ChangeBatch(
            library="bookmarks",
            full_snapshot=True,
            items=items if items is not None else [saved_item()],
        )
    )
    return directive


class BrokenModel(FixtureModel):
    def __init__(self, stage, mutate):
        super().__init__()
        self.stage, self.mutate = stage, mutate

    async def fill(self, form, prompt, data=None):
        result = await super().fill(form, prompt, data)
        if form is self.stage:
            result = self.mutate(result)
        return result


async def test_demo_proves_shared_source_replay_and_scoped_revision(registry, tmp_path):
    report = await demonstration(registry, tmp_path / "catalogs")
    assert report["sources"] == report["observations"] == 1
    assert report["records"] == [2, 1]
    assert report["unrelated_catalog_unchanged"]
    assert len(report["revision_diff"]["withdrawn"]) == 2
    assert all(Path(path).is_file() for path in report["galleries"])


async def test_directive_compilation_and_revision_round_trip(registry):
    directive = await compile_request(
        FixtureModel(), "Catalog fabrication costs", "robots", "fixture-v1"
    )
    version = registry.save_directive(directive)
    assert registry.directive("robots").model_dump() == directive.model_dump()
    directive.purpose += " including industrial methods"
    new = registry.save_directive(directive)
    assert new != version
    assert registry.directive("robots", version).purpose != directive.purpose


def test_invalid_and_unsupported_directive_contract():
    directive = preset("valid", "Inspect methods")
    body = directive.model_dump()
    body["key"] = "../../escape"
    with pytest.raises(ValidationError):
        Directive.model_validate(body)
    body = directive.model_dump()
    body["fields"].append(body["fields"][0])
    with pytest.raises(ValidationError, match="unique"):
        Directive.model_validate(body)
    body = directive.model_dump()
    body["exec"] = "run arbitrary code"
    with pytest.raises(ValidationError):
        Directive.model_validate(body)


@pytest.mark.parametrize(
    "url", ["file:///secret", "https://u:p@example.org/a", "javascript:alert(1)"]
)
def test_source_identity_rejects_non_http_and_credentials(url):
    with pytest.raises(ValueError):
        source_url(url)


def test_source_identity_keeps_distinct_resources():
    assert source_url("https://EXAMPLE.org:443/a?rev=2#part") == "https://example.org/a?rev=2"
    assert identity("source", source_url("https://github.com/a/b/file-one")) != identity(
        "source", source_url("https://github.com/a/b/file-two")
    )


def test_dedup_immutable_revisions_and_atomic_import(registry):
    prepare(registry)
    registry.import_batch(ChangeBatch(library="second-library", items=[saved_item()]))
    assert registry.report()["counts"]["sources"] == 1
    assert registry.report()["counts"]["observations"] == 1
    with pytest.raises(ValueError, match="revision reused"):
        registry.import_batch(
            ChangeBatch(
                library="bookmarks",
                items=[
                    saved_item(external_id="new", url="https://example.org/new"),
                    saved_item(title="Changed without revision increment"),
                ],
            )
        )
    assert registry.report()["counts"]["sources"] == 1
    assert registry.report()["counts"]["item_revisions"] == 2


async def test_overlapping_catalogs_share_evidence_and_keep_memberships(registry):
    first = prepare(registry)
    second = preset("operations", "Catalog operational methods", "general")
    second.selection.libraries = ["bookmarks"]
    registry.save_directive(second)
    a = await execute(registry, first.key, FixtureModel())
    b = await execute(registry, second.key, FixtureModel())
    source_a = a["semantic"]["records"][0]["source"]
    source_b = b["semantic"]["records"][0]["source"]
    assert source_a == source_b
    members = registry.db.execute(
        "SELECT catalog FROM memberships WHERE entity=?", (source_a,)
    ).fetchall()
    assert {r[0] for r in members} == {first.catalog, second.catalog}
    assert registry.report()["counts"]["observations"] == 1
    assert a["semantic"]["records"][0]["fields"] != b["semantic"]["records"][0]["fields"]


async def test_offline_replay_uses_only_saved_inputs_and_responses(registry):
    prepare(registry)
    model = FixtureModel()
    manifest = await execute(registry, "fabrication", model)
    count = model.calls
    registry.close()
    # A completely independent registry is unnecessary: replay has no store/provider argument.
    semantic = await replay(manifest)
    assert semantic == manifest["semantic"] and model.calls == count
    registry.db = sqlite3.connect(":memory:")  # fixture teardown remains valid


async def test_replay_detects_tampering_missing_responses_and_semantic_changes(registry):
    prepare(registry)
    manifest = await execute(registry, "fabrication", FixtureModel())
    bad = copy.deepcopy(manifest)
    bad["semantic"]["records"] = []
    with pytest.raises(ValueError, match="integrity"):
        await replay(bad)
    bad = seal(manifest["inputs"], manifest["calls"][:-1], manifest["semantic"])
    with pytest.raises(ValueError, match="missing saved response"):
        await replay(bad)
    bad = seal(manifest["inputs"], manifest["calls"], {**manifest["semantic"], "records": []})
    with pytest.raises(ValueError, match="semantic output"):
        await replay(bad)


async def test_candidate_order_and_cache_are_deterministic(registry):
    prepare(registry, items=[saved_item(), saved_item(external_id="duplicate")])
    model = FixtureModel()
    first = await execute(registry, "fabrication", model)
    assert model.calls == 4  # two selections; one shared extraction and verification
    second = await execute(registry, "fabrication", model)
    assert model.calls == 4 and first == second
    inputs = copy.deepcopy(first["inputs"])
    inputs["candidates"].reverse()
    reordered = seal(inputs, first["calls"], first["semantic"])
    assert await replay(reordered) == first["semantic"]


async def test_changed_metadata_reuses_interpretation_and_new_observation_invalidates(registry):
    prepare(registry)
    model = FixtureModel()
    first = await execute(registry, "fabrication", model)
    registry.import_batch(
        ChangeBatch(library="bookmarks", items=[saved_item(revision="2", title="Edited title")])
    )
    second = await execute(registry, "fabrication", model)
    assert model.calls == 4  # only selection changed
    assert first["semantic"]["records"] == second["semantic"]["records"]
    registry.import_batch(
        ChangeBatch(
            library="bookmarks",
            items=[
                saved_item(
                    revision="3",
                    capture={"text": TEXT + "New source revision.", "origin": "synthetic fixture"},
                )
            ],
        )
    )
    third = await execute(registry, "fabrication", model)
    assert model.calls == 7
    assert third["semantic"]["records"] != second["semantic"]["records"]


def corrupt_quote(extraction):
    for record in extraction.records:
        record.title.citations[0].quote = "This quotation was never present in the observed source."
    return extraction


async def test_positive_model_verdict_cannot_accept_invented_excerpts(registry):
    prepare(registry)
    manifest = await execute(registry, "fabrication", BrokenModel(Extraction, corrupt_quote))
    assert not manifest["semantic"]["records"]
    assert not manifest["semantic"]["relationships"]
    assert any(
        not c["accepted"] for d in manifest["semantic"]["decisions"] for c in d.get("claims", [])
    )


def duplicate_checks(verification):
    verification.checks.extend(copy.deepcopy(verification.checks))
    return verification


async def test_duplicate_or_absent_verifier_checks_fail_closed(registry):
    prepare(registry)
    manifest = await execute(registry, "fabrication", BrokenModel(Verification, duplicate_checks))
    assert not manifest["semantic"]["records"]


async def test_inferred_claims_cannot_pass_source_assertion_gate(registry):
    prepare(registry)

    def inference(result):
        for record in result.records:
            record.title.kind = "model_inferred"
        return result

    manifest = await execute(registry, "fabrication", BrokenModel(Extraction, inference))
    assert not manifest["semantic"]["records"]


async def test_unknown_fields_are_pipeline_failure_and_preserve_publication(registry):
    directive = prepare(registry)
    original = await execute(registry, directive.key, FixtureModel())
    directive.purpose += " more detail"
    registry.save_directive(directive)

    def unknown(result):
        result.records[0].fields["invented_field"] = []
        return result

    with pytest.raises(ValueError, match="unknown directive field"):
        await execute(registry, directive.key, BrokenModel(Extraction, unknown))
    assert registry.head(directive.catalog) == original
    assert registry.report()["attempts"][0]["status"] == "failed"


async def test_interrupted_execution_resumes_without_duplicate_results(registry):
    prepare(registry)

    class Interrupted(FixtureModel):
        async def fill(self, form, prompt, data=None):
            if form is Verification:
                raise RuntimeError("simulated interruption")
            return await super().fill(form, prompt, data)

    with pytest.raises(RuntimeError):
        await execute(registry, "fabrication", Interrupted())
    assert registry.head("fabrication") is None
    model = FixtureModel()
    manifest = await execute(registry, "fabrication", model)
    assert model.calls == 1  # earlier selection and extraction were persisted
    assert len(manifest["semantic"]["records"]) == 2
    assert registry.report()["counts"]["runs"] == 1


async def test_per_run_budget_and_attempt_cap_persist_across_restart(registry, tmp_path):
    directive = prepare(registry)
    directive.limits.max_model_calls = 1
    directive.limits.max_attempts = 1
    registry.save_directive(directive)
    with pytest.raises(ValueError, match="run model-call budget"):
        await execute(registry, directive.key, FixtureModel())
    reopened = Registry(tmp_path / "distill.sqlite")
    try:
        with pytest.raises(ValueError, match="attempt limit"):
            await execute(reopened, directive.key, FixtureModel())
        reopened.retry(directive.key)
        with pytest.raises(ValueError, match="run model-call budget"):
            await execute(reopened, directive.key, FixtureModel())
    finally:
        reopened.close()


async def test_rolling_shared_budget_is_not_reset_by_new_directive(registry):
    directive = prepare(registry)
    await execute(registry, directive.key, FixtureModel(), shared_cap=3)
    second = preset("other", "Catalog robotics", "general")
    second.selection.libraries = ["bookmarks"]
    registry.save_directive(second)
    with pytest.raises(ValueError, match="rolling daily"):
        await execute(registry, second.key, FixtureModel(), shared_cap=3)
    assert registry.head(directive.catalog)
    assert not registry.head(second.catalog)


async def test_withdrawal_removes_membership_without_deleting_evidence(registry):
    directive = prepare(registry)
    before = await execute(registry, directive.key, FixtureModel())
    registry.import_batch(ChangeBatch(library="bookmarks", full_snapshot=True, items=[]))
    after = await execute(registry, directive.key, FixtureModel())
    assert not after["semantic"]["records"]
    assert registry.report()["counts"]["observations"] == 1
    assert len(diff(before, after)["withdrawn"]) == 2
    assert await replay(before) == before["semantic"]


async def test_retain_withdrawal_policy_is_explicit(registry):
    directive = prepare(registry)
    directive.withdrawal = "retain"
    registry.save_directive(directive)
    registry.import_batch(ChangeBatch(library="bookmarks", full_snapshot=True, items=[]))
    manifest = await execute(registry, directive.key, FixtureModel())
    assert len(manifest["semantic"]["records"]) == 2


async def test_uncertain_selections_remain_inspectable(registry):
    prepare(registry)

    def uncertain(result):
        result.verdict = "uncertain"
        result.reason = "source scope cannot be determined"
        return result

    manifest = await execute(registry, "fabrication", BrokenModel(Assessment, uncertain))
    assert not manifest["semantic"]["records"]
    assert manifest["semantic"]["decisions"][0]["verdict"] == "uncertain"


async def test_candidate_budget_counts_only_eligible_collection(registry):
    directive = prepare(
        registry, items=[saved_item(), saved_item(external_id="other", collections=["other"])]
    )
    directive.selection.collections = ["robotics"]
    directive.limits.max_candidates = 1
    registry.save_directive(directive)
    manifest = await execute(registry, directive.key, FixtureModel())
    assert len(manifest["semantic"]["records"]) == 2


async def test_unresolved_contract_and_missing_observation_cannot_publish(registry):
    directive = prepare(registry, items=[saved_item(capture=None)])
    with pytest.raises(ValueError, match="no preserved"):
        await execute(registry, directive.key, FixtureModel())
    directive.unresolved = ["bookmark source schema mapping required"]
    registry.save_directive(directive)
    with pytest.raises(ValueError, match="unresolved/unsupported"):
        await execute(registry, directive.key, FixtureModel())
    assert not registry.head(directive.catalog)


async def test_pause_retire_and_lease_prevent_execution(registry):
    directive = prepare(registry)
    registry.set_state(directive.key, "paused")
    with pytest.raises(ValueError, match="paused"):
        await execute(registry, directive.key, FixtureModel())
    registry.set_state(directive.key, "active")
    token = registry.claim(directive.key)
    with pytest.raises(ValueError, match="busy"):
        await execute(registry, directive.key, FixtureModel())
    registry.finish(directive, token, None)
    registry.set_state(directive.key, "retired")
    assert directive.key not in registry.due()


async def test_directive_revision_during_run_preserves_prior_publication(registry):
    directive = prepare(registry)
    original = await execute(registry, directive.key, FixtureModel())
    directive.purpose += " updated"
    registry.save_directive(directive)

    class Revising(FixtureModel):
        async def fill(self, form, prompt, data=None):
            result = await super().fill(form, prompt, data)
            if form is Verification:
                changed = directive.model_copy(deep=True)
                changed.purpose += " another revision"
                registry.save_directive(changed)
            return result

    with pytest.raises(ValueError, match="directive changed"):
        await execute(registry, directive.key, Revising())
    assert registry.head(directive.catalog) == original


async def test_connected_catalog_keeps_original_evidence_and_rejects_cycles(registry):
    directive = prepare(registry)
    original = await execute(registry, directive.key, FixtureModel())
    await import_catalog(registry, original)
    derived = preset("derived", "Catalog workflows from fabrication", "general")
    derived.selection.libraries = [f"catalog:{directive.catalog}"]
    registry.save_directive(derived)
    manifest = await execute(registry, derived.key, FixtureModel())
    assert manifest["inputs"]["upstream_runs"][original["id"]] == original
    assert any(edge["type"] == "derived_from" for edge in manifest["semantic"]["relationships"])
    observed = manifest["inputs"]["candidates"][0]["observation"]
    assert observed["text"] == TEXT
    assert await replay(manifest) == manifest["semantic"]
    await import_catalog(registry, manifest)
    directive.selection.libraries = [f"catalog:{derived.catalog}"]
    registry.save_directive(directive)
    with pytest.raises(ValueError, match="circular"):
        await execute(registry, directive.key, FixtureModel())
    assert registry.head(directive.catalog) == original


def create_sql_library(path, items):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE saved (external_id TEXT, item_json TEXT, changed INTEGER)")
        connection.executemany(
            "INSERT INTO saved VALUES (?,?,?)",
            [
                (item.external_id, canonical(item.model_dump()), n)
                for n, item in enumerate(items, 1)
            ],
        )
    connection.close()


def test_sql_projection_read_only_bounded_and_cursor(registry, tmp_path):
    database = tmp_path / "bookmarks.sqlite"
    create_sql_library(database, [saved_item()])
    adapter = SQLiteAdapter(
        database, "sql-bookmarks", "SELECT item_json FROM saved ORDER BY external_id"
    )
    batch = adapter.changes()
    registry.import_batch(batch)
    assert not adapter.changes(batch.cursor).items
    before = database.read_bytes()
    bad = SQLiteAdapter(database, "sql-bookmarks", "DELETE FROM saved RETURNING item_json")
    with pytest.raises(ValueError, match="read-only"):
        bad.changes()
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO saved SELECT 'extra',item_json,2 FROM saved")
    with pytest.raises(ValueError, match="exceeds import bound"):
        SQLiteAdapter(
            database, "sql-bookmarks", "SELECT item_json FROM saved", max_items=1
        ).changes()


def test_sql_incremental_cursor_and_missing_aliases(tmp_path):
    database = tmp_path / "bookmarks.sqlite"
    create_sql_library(
        database, [saved_item(), saved_item(external_id="b", revision="2", removed=True)]
    )
    adapter = SQLiteAdapter(
        database,
        "sql",
        "SELECT item_json,changed AS change_cursor FROM saved"
        " WHERE :cursor IS NULL OR changed>CAST(:cursor AS INTEGER) ORDER BY changed",
        full_snapshot=False,
    )
    batch = adapter.changes("1")
    assert batch.cursor == "2" and len(batch.items) == 1 and batch.items[0].removed
    assert not adapter.changes(batch.cursor).items
    with pytest.raises(ValueError, match="item_json"):
        SQLiteAdapter(database, "sql", "SELECT external_id FROM saved").changes()


def test_fixture_adapter_reimport_does_not_withdraw_unchanged_items(tmp_path):
    path = tmp_path / "library.json"
    path.write_text(
        canonical(
            ChangeBatch(library="bookmarks", full_snapshot=True, items=[saved_item()]).model_dump()
        ),
        encoding="utf-8",
    )
    adapter = FixtureAdapter(path)
    first = adapter.changes()
    second = adapter.changes(first.cursor)
    assert not second.items and not second.full_snapshot


async def test_url_only_acquisition_is_shared_and_keeps_metadata(registry, monkeypatch):
    first = prepare(registry, items=[saved_item(capture=None)])
    second = preset("other", "Catalog workflow methods")
    second.selection.libraries = ["bookmarks"]
    registry.save_directive(second)
    reads = []

    async def fetch(client, url):
        reads.append(url)
        return [{"url": url, "text": TEXT, "locator": "PDF page 2"}]

    monkeypatch.setattr("distiller.maintenance.fetch_page", fetch)
    await acquire(registry, first.key)
    await acquire(registry, second.key)
    assert reads == ["https://example.org/bracket"]
    assert registry.report()["counts"]["item_revisions"] == 1
    assert registry.candidates(first)[0]["observation"]["spans"][0]["locator"] == "PDF page 2"
    assert registry.candidates(first)[0]["metadata"]["title"] == "Bracket fabrication"


async def test_maintenance_fairness_failure_isolation_and_model_setting(registry, tmp_path):
    first = prepare(registry)
    bad = preset("bad", "Catalog methods")
    bad.unresolved = ["needs missing source library"]
    registry.save_directive(bad)
    result = await cycle(registry, FixtureModel(), tmp_path / "catalogs", limit=1)
    assert result[0]["directive"] == "bad" and result[0]["status"] == "failed"
    result = await cycle(registry, FixtureModel(), tmp_path / "catalogs", limit=1)
    assert result[0]["directive"] == first.key and result[0]["status"] == "complete"
    registry.retry(first.key)
    result = await cycle(
        registry, FixtureModel(), tmp_path / "catalogs", model_name="another-model"
    )
    assert result[0]["status"] == "failed" and "requires model" in result[0]["error"]


async def test_stop_file_prevents_maintenance_work(registry, tmp_path):
    prepare(registry)
    stop = tmp_path / "stop"
    stop.touch()
    assert await maintain(registry, FixtureModel(), tmp_path, stop, once=True) == []
    assert registry.head("fabrication") is None


def test_page_spans_bound_text_and_do_not_create_fake_timestamps():
    capture = capture_pages(
        [
            {"url": "https://example.org/a", "text": "a" * 119990, "locator": "PDF page 1"},
            {"url": "https://example.org/a", "text": "b" * 100, "locator": "PDF page 2"},
        ],
        "https://example.org/a",
    )
    assert len(capture.text) == 120000
    assert capture.spans[-1].end == 120000
    assert capture.modality == "pdf"


async def test_exports_escape_source_text_and_preserve_evidence(registry, tmp_path):
    prepare(registry, items=[saved_item(title="<script>alert('x')</script>")])
    manifest = await execute(registry, "fabrication", FixtureModel())
    path = export(manifest, tmp_path / "catalogs")
    assert "&lt;script&gt;" in path.read_text(encoding="utf-8")
    assert "<script>alert('x')</script>" not in gallery(manifest)
    version = path.parent / manifest["id"]
    assert json.loads((version / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert "synthetic fixture" in (version / "manifest.json").read_text(encoding="utf-8")
    assert "wood" in search(manifest, "plywood")[0]["title"]["text"]


def test_additive_migration_preserves_legacy_bodies_and_media_locators(registry, tmp_path):
    path = tmp_path / "queue.sqlite"
    body = {
        "idea": "Preserved legacy claim",
        "idea_t": 12.5,
        "citations": {"idea": [{"quote": "original"}]},
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE videos(id TEXT,url TEXT);"
            "CREATE TABLE cards(id INTEGER,video_id TEXT,body TEXT);"
        )
        connection.execute("INSERT INTO videos VALUES ('one','https://example.org/legacy')")
        connection.execute("INSERT INTO cards VALUES (1,'one',?)", (canonical(body),))
    before = path.read_bytes()
    result = migrate(registry, path)
    assert result["new_legacy_entities"]["technique"] == 1
    stored = json.loads(registry.db.execute("SELECT body FROM legacy_entities").fetchone()[0])
    assert stored["original"]["body"] == body
    assert path.read_bytes() == before
    assert migrate(registry, path)["new_legacy_entities"]["technique"] == 0
    assert registry.report()["counts"]["observations"] == 0


async def test_concurrent_directives_acquire_shared_source_once(registry, monkeypatch):
    reads = []

    async def fetch(client, url):
        reads.append(url)
        await asyncio.sleep(0.02)
        return [{"url": url, "text": TEXT}]

    monkeypatch.setattr("distiller.maintenance.fetch_page", fetch)
    url = "https://example.org/concurrent"
    first, second = await asyncio.gather(
        shared_capture(registry, None, url, 86400), shared_capture(registry, None, url, 86400)
    )
    assert reads == [url]
    assert first == second
    assert registry.db.execute("SELECT COUNT(*) FROM source_leases").fetchone()[0] == 0


async def test_polling_sql_tag_edit_invalidates_only_affected_directive(registry, tmp_path):
    directive = prepare(registry)
    original = await execute(registry, directive.key, FixtureModel())
    unaffected = preset("unrelated", "Catalog different library")
    unaffected.selection.libraries = ["other-library"]
    registry.save_directive(unaffected)
    registry.import_batch(ChangeBatch(library="other-library", items=[]))
    untouched = await execute(registry, unaffected.key, FixtureModel())
    path = tmp_path / "bookmarks.sqlite"
    create_sql_library(path, [saved_item()])
    registry.configure_adapter(
        "bookmarks",
        {
            "kind": "sqlite",
            "path": str(path),
            "query": "SELECT item_json FROM saved ORDER BY external_id",
            "full_snapshot": True,
            "max_items": 10000,
        },
    )
    changed = saved_item(revision="2", tags=[{"value": "excluded", "origin": "user"}])
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE saved SET item_json=?", (canonical(changed.model_dump()),))
    result = await cycle(registry, FixtureModel(), tmp_path / "catalogs")
    assert [r["directive"] for r in result] == [directive.key]
    assert registry.head(directive.catalog)["id"] != original["id"]
    assert registry.head(unaffected.catalog) == untouched


async def test_poll_failure_preserves_complete_publication(registry, tmp_path):
    directive = prepare(registry)
    original = await execute(registry, directive.key, FixtureModel())
    registry.configure_adapter(
        "bookmarks", {"kind": "fixture", "path": str(tmp_path / "missing.json")}
    )
    registry.retry(directive.key)
    result = await cycle(registry, FixtureModel(), tmp_path / "catalogs")
    assert result[0]["status"] == "failed" and "polling failed" in result[0]["error"]
    assert registry.head(directive.catalog) == original


async def test_metadata_change_during_run_cannot_publish_stale_selection(registry):
    directive = prepare(registry)
    original = await execute(registry, directive.key, FixtureModel())
    directive.purpose += " changed purpose to require a fresh model response"
    registry.save_directive(directive)

    class Editing(FixtureModel):
        async def fill(self, form, prompt, data=None):
            result = await super().fill(form, prompt, data)
            if form is Verification:
                registry.import_batch(
                    ChangeBatch(
                        library="bookmarks",
                        items=[saved_item(revision="2", title="Changed while extraction ran")],
                    )
                )
            return result

    with pytest.raises(ValueError, match="metadata changed during run"):
        await execute(registry, directive.key, Editing())
    assert registry.head(directive.catalog) == original


async def test_cross_source_relationships_are_verified_and_replayable(registry):
    directive = prepare(
        registry,
        items=[saved_item(), saved_item(external_id="second", url="https://example.org/second")],
    )

    class Connecting(FixtureModel):
        async def fill(self, form, prompt, data=None):
            if form is Relationships:
                payload = json.loads(data)
                records = payload["records"]
                source = records[0]
                target = next(r for r in records if r["source"] != source["source"])
                quote = TEXT.splitlines()[10]
                return Relationships(
                    relationships=[
                        {
                            "source": source["id"],
                            "target": target["id"],
                            "type": "alternative_to",
                            "claim": {
                                "text": quote,
                                "citations": [
                                    {"observation": source["observation"], "quote": quote}
                                ],
                            },
                        }
                    ]
                )
            return await super().fill(form, prompt, data)

    manifest = await execute(registry, directive.key, Connecting())
    outputs = {r["id"]: r for r in manifest["semantic"]["records"]}
    cross_source = [
        e
        for e in manifest["semantic"]["relationships"]
        if e["type"] == "alternative_to"
        and outputs[e["source"]]["source"] != outputs[e["target"]]["source"]
    ]
    assert len(cross_source) == 1
    assert cross_source[0]["evidence"][0]["citations"]
    assert await replay(manifest) == manifest["semantic"]


async def test_compiler_trace_is_preserved_and_replayed(registry):
    directive = await compile_request(
        FixtureModel(),
        "Catalog fabrication",
        "compiled",
        "fixture-v1",
        record=registry.record_compilation,
    )
    registry.save_directive(directive)
    registry.import_batch(ChangeBatch(library="bookmarks", items=[saved_item()]))
    manifest = await execute(registry, directive.key, FixtureModel())
    trace = manifest["inputs"]["compilation"]
    assert trace["response"] == directive.model_dump()
    assert trace["request"]["data"]["request"] == directive.request
    assert "Compile" in trace["request"]["prompt"]
    assert await replay(manifest) == manifest["semantic"]


def test_sql_read_releases_windows_file_handle(tmp_path):
    database = tmp_path / "bookmarks.sqlite"
    create_sql_library(database, [saved_item()])
    adapter = SQLiteAdapter(database, "bookmarks", "SELECT item_json FROM saved")
    assert len(adapter.changes().items) == 1
    renamed = tmp_path / "released.sqlite"
    database.rename(renamed)
    assert renamed.is_file()


async def test_provider_pause_does_not_exhaust_input_attempt_limit(registry):
    directive = prepare(registry)
    directive.limits.max_attempts = 1
    registry.save_directive(directive)

    class Paused(FixtureModel):
        async def fill(self, form, prompt, data=None):
            raise Busy("provider quota pause", retry_after=60)

    with pytest.raises(Busy):
        await execute(registry, directive.key, Paused())
    assert registry.report()["attempts"][0]["status"] == "waiting"
    manifest = await execute(registry, directive.key, FixtureModel())
    assert len(manifest["semantic"]["records"]) == 2


async def test_required_unknown_library_is_not_a_silent_empty_catalog(registry):
    directive = prepare(registry)
    directive.selection.libraries = ["missing-library"]
    registry.save_directive(directive)
    with pytest.raises(ValueError, match="libraries are not connected"):
        await execute(registry, directive.key, FixtureModel())
    assert not registry.head(directive.catalog)
