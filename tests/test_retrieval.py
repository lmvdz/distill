import json

import pytest

from distiller.db import Queue
from distiller.retrieval import Encoder, Index, documents


class FakeEncoder(Encoder):
    def __init__(self, dimensions=128, images=False):
        super().__init__(dimensions, images)
        self.calls = []
        self.fail = False

    def encode(self, inputs):
        self.calls.extend(inputs)
        if self.fail:
            raise RuntimeError("model unavailable")
        return [
            [2.0, 0.0] + [0.0] * (self.dimensions - 2)
            if isinstance(item, dict) or "noise" in item
            else [0.0, 3.0] + [0.0] * (self.dimensions - 2)
            for item in inputs
        ]


def doc(ident="1", text="noise", kind="technique"):
    return {
        "id": ident,
        "kind": kind,
        "title": "Example",
        "text": text,
        "url": "https://example.com/source",
        "times": [42.0],
        "evidence": "video-checked" if kind == "technique" else "unverified-web-observation",
    }


def test_cache_changes_removals_and_reconfiguration(tmp_path):
    index, encoder = Index(tmp_path / "search.sqlite"), FakeEncoder()
    first, second = doc(), doc("2", "rim")
    assert index.build([first, second], tmp_path, encoder) == 2
    encoder.calls.clear()
    assert index.build([first, second], tmp_path, encoder) == 0
    assert encoder.calls == []
    assert index.build([{**first, "text": "changed"}], tmp_path, encoder) == 1
    assert index.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 1
    assert index.build([first], tmp_path, FakeEncoder(256)) == 1
    with pytest.raises(ValueError, match="different encoder"):
        index.search("noise", encoder)
    index.close()


def test_failed_rebuild_preserves_prior_index_and_configuration(tmp_path):
    index, encoder = Index(tmp_path / "search.sqlite"), FakeEncoder()
    index.build([doc()], tmp_path, encoder)
    broken = FakeEncoder(256)
    broken.fail = True
    with pytest.raises(RuntimeError, match="unavailable"):
        index.build([doc("replacement")], tmp_path, broken)
    assert index.settings()["dimensions"] == 128
    assert index.search("noise", encoder)[0]["id"] == "1"
    index.close()


def test_rank_normalized_vectors_filter_kinds_and_keep_evidence(tmp_path):
    index, encoder = Index(tmp_path / "search.sqlite"), FakeEncoder()
    index.build([doc("rim", "rim"), doc("noise"), doc("web", kind="web")], tmp_path, encoder)
    matches = index.search("noise", encoder, limit=2)
    assert matches[0]["id"] == "noise"
    assert matches[0]["score"] == pytest.approx(1.0)
    assert matches[0]["times"] == [42.0]
    web = index.search("noise", encoder, kind="web")
    assert len(web) == 1 and web[0]["evidence"] == "unverified-web-observation"
    with pytest.raises(ValueError, match="positive"):
        index.search("noise", encoder, limit=0)
    index.close()


def test_thumbnails_invalidate_by_content_and_results_are_deduplicated(tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"first image")
    card = {**doc(text="rim"), "media": {"thumb": "thumb.jpg"}}
    index, encoder = Index(tmp_path / "search.sqlite"), FakeEncoder(images=True)
    assert index.build([card], tmp_path, encoder) == 1
    matches = index.search({"image": "reference.jpg"}, encoder)
    assert len(matches) == 1 and matches[0]["matched_modality"] == "image"
    assert index.build([card], tmp_path, encoder) == 0
    thumb.write_bytes(b"second image")
    assert index.build([card], tmp_path, encoder) == 1
    thumb.unlink()
    assert index.build([card], tmp_path, encoder) == 1
    assert index.db.execute("SELECT modality FROM vectors").fetchall() == [("text",)]
    with pytest.raises(ValueError, match="outside catalog"):
        index.build([{**card, "media": {"thumb": "../outside.jpg"}}], tmp_path, encoder)
    index.close()


def test_invalid_embeddings_roll_back(tmp_path):
    index, encoder = Index(tmp_path / "search.sqlite"), FakeEncoder()
    index.build([doc()], tmp_path, encoder)
    encoder.encode = lambda inputs: [[float("nan")] * 128 for _ in inputs]
    with pytest.raises(ValueError, match="non-finite"):
        index.build([doc("new")], tmp_path, encoder)
    assert index.db.execute("SELECT id FROM documents").fetchall() == [("1",)]
    index.close()


def test_corpus_preserves_source_timestamps_and_studied_evidence(tmp_path):
    q = Queue(":memory:")
    q.enqueue(site="youtube", site_id="v", url="https://youtube.com/watch?v=v")
    body = {
        "technique": "Noise",
        "idea": "A noise field animates a mask",
        "result": "flame",
        "mechanism": [{"text": "sample noise", "t": 42}],
        "recipe": [{"text": "threshold it", "t": 43}],
    }
    q.add_card("youtube:v", "", "", "noise", body)
    q.add_card("youtube:v", "", "", "inferred", {**body, "studied": True, "evidence": "inferred"})
    q.add_workflow_card(
        "youtube:v", "Sound", {"goal": "Sound", "steps": [{"text": "pack it", "t": 70}]}
    )
    (tmp_path / "capture.json").write_text(
        json.dumps(
            {
                "captured_at": "now",
                "status": "done",
                "pages": [{"url": "https://docs.example", "text": "noise inputs"}],
            }
        )
    )
    corpus = documents(q, tmp_path)
    assert corpus[0]["times"] == [42, 43]
    assert corpus[1]["times"] == [] and corpus[1]["evidence"] == "inferred"
    assert corpus[2]["times"] == [70] and corpus[2]["kind"] == "workflow"
    assert corpus[3]["evidence"] == "unverified-web-observation"
    assert corpus[3]["agent_status"] == "done"
    index, encoder = Index(tmp_path / "search.sqlite"), FakeEncoder()
    index.build(corpus, tmp_path, encoder)
    assert encoder.calls[0].startswith("title: Noise | text: ")
    assert q.counts()["cards"] == 2
    index.close()


def test_encoder_suppresses_default_prompt_and_normalizes_truncation():
    class Model:
        def encode(self, inputs, **options):
            assert inputs == ["task: search result | query: noise"]
            assert options["prompt"] == ""
            assert options["normalize_embeddings"] and options["truncate_dim"] == 256
            return self

        def tolist(self):
            return [[1.0] + [0.0] * 255]

    encoder = Encoder()
    encoder._model = Model()
    assert len(encoder.encode(["task: search result | query: noise"])[0]) == 256


def test_embedding_memory_pressure_reduces_batch_without_losing_input_order(monkeypatch):
    tried = []

    class Model:
        def encode(self, inputs, **options):
            tried.append(options["batch_size"])
            assert inputs == ["first", "second"]
            if options["batch_size"] > 2:
                raise RuntimeError("CUDA out of memory")
            return self

        def tolist(self):
            return [[1.0], [2.0]]

    monkeypatch.setattr("distiller.retrieval.release_cuda_cache", lambda: None)
    encoder = Encoder()
    encoder._model = Model()
    assert encoder.encode(["first", "second"]) == [[1.0], [2.0]]
    assert tried == [8, 4, 2]
    tried.clear()
    encoder.encode(["first", "second"])
    assert tried == [2]  # retain the working size on later calls


def test_auto_encoder_recovers_on_cpu_when_single_gpu_input_cannot_fit(monkeypatch):
    tried = []

    class GpuModel:
        def encode(self, inputs, **options):
            tried.append(options["batch_size"])
            raise RuntimeError("CUDA out of memory")

    class CpuModel:
        def encode(self, inputs, **options):
            assert inputs == ["first", "second"]
            assert options["batch_size"] == 1
            return self

        def tolist(self):
            return [[1.0], [2.0]]

    encoder = Encoder(device="auto")
    encoder._model = GpuModel()
    encoder._active_device = "cuda"

    def load(device):
        assert device == "cpu"
        encoder._model = CpuModel()
        encoder._active_device = device

    monkeypatch.setattr(encoder, "_load_model", load)
    monkeypatch.setattr("distiller.retrieval.release_cuda_cache", lambda: None)
    assert encoder.encode(["first", "second"]) == [[1.0], [2.0]]
    assert tried == [8, 4, 2, 1]
    assert encoder._active_device == "cpu"
