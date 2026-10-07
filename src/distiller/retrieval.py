"""Local retrieval over stored cards and observed (unverified) browser sources.

Embeddings rank evidence; they never verify, merge, or change a card.
The index is disposable and separate from the work queue.
"""

import hashlib
import json
import logging
import math
import os
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .db import Queue

MODEL_ID = "google/embeddinggemma-2"
MODEL_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
DIMENSIONS = (128, 256, 512, 768)
INDEX_VERSION = 2
MAX_TOKENS = 8192
log = logging.getLogger("distill")


class Encoder:
    """Load only the modalities needed. No model/API credentials are sent to a service."""

    def __init__(
        self, dimensions: int = 256, images: bool = False, device: str | None = None
    ) -> None:
        if dimensions not in DIMENSIONS:
            raise ValueError(f"dimensions must be one of {DIMENSIONS}")
        self.dimensions = dimensions
        self.images = images
        self.signature = f"{INDEX_VERSION}:{MODEL_ID}:{MODEL_REVISION}:{dimensions}:{images}"
        self._model: Any = None
        self._batch_size = 8
        self.device = device or os.environ.get("DISTILL_SEARCH_DEVICE", "auto")
        if self.device not in {"auto", "cpu", "cuda"}:
            raise ValueError("DISTILL_SEARCH_DEVICE must be auto, cpu or cuda")
        self._active_device: str | None = None

    def _load_model(self, device: str) -> None:
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as err:
            raise ValueError("Search needs `uv sync --extra search`.") from err
        config = {"audio_config": None}
        if not self.images:
            config["vision_config"] = None
        dtype = (
            torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
        )
        self._model = SentenceTransformer(
            MODEL_ID,
            revision=MODEL_REVISION,
            config_kwargs=config,
            device=device,
            model_kwargs={"torch_dtype": dtype},
        )
        self._model.max_seq_length = MAX_TOKENS
        self._active_device = device

    def _load_available_model(self) -> None:
        try:
            import torch
        except ImportError as err:
            raise ValueError("Search needs `uv sync --extra search`.") from err

        preferred = self.device
        if preferred == "auto":
            preferred = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self._load_model(preferred)
            return
        except RuntimeError as err:
            if not is_oom(err) or preferred != "cuda" or self.device != "auto":
                raise
        # Leave the exception scope first: its traceback can retain GPU tensors.
        self._switch_to_cpu()

    def _switch_to_cpu(self) -> None:
        log.warning("GPU memory exhausted; continuing embedding on CPU")
        self._model = None
        release_cuda_cache()
        self._load_model("cpu")
        self._batch_size = 1

    def encode(self, inputs: Sequence[Any]) -> list[list[float]]:
        if self._model is None:
            self._load_available_model()
        # Inputs already carry the task prefix. Suppress the model's default prompt.
        while True:
            try:
                return self._model.encode(
                    list(inputs),
                    prompt="",
                    truncate_dim=self.dimensions,
                    normalize_embeddings=True,
                    batch_size=self._batch_size,
                    show_progress_bar=len(inputs) > 32,
                ).tolist()
            except RuntimeError as err:
                if not is_oom(err):
                    raise
                retry_cpu = (
                    self._batch_size == 1
                    and self._active_device == "cuda"
                    and self.device == "auto"
                )
                if self._batch_size == 1 and not retry_cpu:
                    raise
            if retry_cpu:
                self._switch_to_cpu()
            else:
                self._batch_size //= 2
                release_cuda_cache()
                log.warning(
                    "embedding memory pressure; retrying with batch size %d", self._batch_size
                )


def is_oom(error: RuntimeError) -> bool:
    return "out of memory" in str(error).lower()


def release_cuda_cache() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _card_document(row: dict[str, Any], kind: str) -> dict[str, Any]:
    body = row["body"]
    workflow = kind == "workflow"
    title = body["goal" if workflow else "technique"]
    fields = (
        ("steps", "pain_points")
        if workflow
        else ("mechanism", "decisions", "knobs", "recipe", "pitfalls")
    )
    claims = [{"field": f, **c} for f in fields for c in body.get(f, [])]
    text = "\n".join(
        [body.get("idea", ""), body.get("result", "")]
        + [f"{c['field']}: {c['text']}" for c in claims]
    )
    if workflow:
        text += "\nTools: " + ", ".join(t["name"] for t in body.get("tools", []))
    else:
        text += "\nSignals: " + ", ".join(body.get("consumes", []) + body.get("produces", []))
    return {
        "id": f"{kind}:{row['id']}",
        "kind": kind,
        "title": title,
        "text": text.strip(),
        "url": row["url"],
        "times": [] if body.get("studied") else sorted({c["t"] for c in claims if "t" in c}),
        "evidence": body.get("evidence", "video-checked"),
        "media": body.get("media"),
        "produces": body.get("produces", []),
        "mediums": body.get("mediums", []),
        "citations": body.get("citations", {}),
        "claim_citations": [
            {"claim": c["text"], **ref} for c in claims for ref in c.get("citations", [])
        ],
        "evidence_level": body.get("evidence_level"),
        "maturity": body.get("maturity"),
        "research_id": body.get("research_bundle", {}).get("id"),
    }


def documents(q: Queue, research_dir: Path) -> list[dict[str, Any]]:
    docs = [_card_document(r, "technique") for r in q.cards()]
    docs.extend(_card_document(r, "workflow") for r in q.workflow_cards())
    for path in sorted(research_dir.glob("*.json")):
        capture = json.loads(path.read_text(encoding="utf-8"))
        for page in capture["pages"]:
            identity = [str(path.name), page["url"]]
            if page.get("locator"):
                identity.append(page["locator"])
            docs.append(
                {
                    "id": "web:" + _hash(identity),
                    "kind": "web",
                    "title": page.get("title") or page["url"],
                    "text": page["text"],
                    "url": page["url"],
                    "times": [],
                    "evidence": "unverified-web-observation",
                    "captured_at": capture["captured_at"],
                    "agent_status": capture["status"],
                    "locator": page.get("locator"),
                }
            )
    return docs


def _inputs(doc: dict[str, Any], catalog: Path, images: bool) -> list[tuple[str, Any]]:
    out = [("text", f"title: {doc['title']} | text: {doc['text']}")]
    thumb = (doc.get("media") or {}).get("thumb")
    if images and thumb:
        path = (catalog / thumb).resolve()
        if not path.is_relative_to(catalog.resolve()):
            raise ValueError(f"thumbnail outside catalog: {thumb}")
        if path.is_file():
            out.append(("image", {"image": str(path)}))
    return out


def _unit(vector: Sequence[float], dimensions: int) -> list[float]:
    values = [float(v) for v in vector]
    if len(values) != dimensions or not all(math.isfinite(v) for v in values):
        raise ValueError("embedding has invalid dimensions or non-finite values")
    length = math.sqrt(sum(v * v for v in values))
    if not length:
        raise ValueError("embedding has zero length")
    return [v / length for v in values]


class Index:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, hash TEXT, body TEXT);"
            "CREATE TABLE IF NOT EXISTS vectors (id TEXT, modality TEXT, vector TEXT,"
            " PRIMARY KEY(id, modality));"
        )

    def close(self) -> None:
        self.db.close()

    def settings(self) -> dict[str, Any]:
        row = self.db.execute("SELECT value FROM metadata WHERE key='encoder'").fetchone()
        if row is None:
            raise ValueError("No search index yet; run `distill index` first.")
        return json.loads(row[0])

    def build(self, docs: list[dict[str, Any]], catalog: Path, encoder: Encoder) -> int:
        settings = {
            "signature": encoder.signature,
            "dimensions": encoder.dimensions,
            "images": encoder.images,
        }
        row = self.db.execute("SELECT value FROM metadata WHERE key='encoder'").fetchone()
        compatible = row is not None and json.loads(row[0]) == settings
        old = dict(self.db.execute("SELECT id, hash FROM documents")) if compatible else {}
        pending = []
        for doc in docs:
            inputs = _inputs(doc, catalog, encoder.images)
            thumb = inputs[-1][1].get("image") if inputs[-1][0] == "image" else None
            image_hash = hashlib.sha256(Path(thumb).read_bytes()).hexdigest() if thumb else None
            digest = _hash([doc, image_hash])
            if old.get(doc["id"]) != digest:
                pending.append((doc, digest, inputs))
        values = [value for _, _, inputs in pending for _, value in inputs]
        vectors = encoder.encode(values) if values else []
        if len(vectors) != len(values):
            raise ValueError("encoder returned the wrong number of vectors")
        vectors = [_unit(v, encoder.dimensions) for v in vectors]
        cursor = 0
        # A failed model call rolls back, preserving the previous searchable index.
        with self.db:
            if not compatible:
                self.db.execute("DELETE FROM documents")
                self.db.execute("DELETE FROM vectors")
            for doc, digest, inputs in pending:
                self.db.execute("DELETE FROM vectors WHERE id=?", (doc["id"],))
                for modality, _ in inputs:
                    self.db.execute(
                        "INSERT INTO vectors VALUES (?, ?, ?)",
                        (doc["id"], modality, json.dumps(vectors[cursor])),
                    )
                    cursor += 1
                self.db.execute(
                    "INSERT OR REPLACE INTO documents VALUES (?, ?, ?)",
                    (doc["id"], digest, json.dumps(doc)),
                )
            active = {d["id"] for d in docs}
            for (ident,) in self.db.execute("SELECT id FROM documents").fetchall():
                if ident not in active:
                    self.db.execute("DELETE FROM documents WHERE id=?", (ident,))
                    self.db.execute("DELETE FROM vectors WHERE id=?", (ident,))
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('encoder', ?)", (json.dumps(settings),)
            )
        return len(pending)

    def search(
        self,
        query: Any,
        encoder: Encoder,
        limit: int = 10,
        kind: str | None = None,
        accept: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if self.settings()["signature"] != encoder.signature:
            raise ValueError("Index uses a different encoder; rebuild with `distill index`.")
        vector = _unit(encoder.encode([query])[0], encoder.dimensions)
        results: dict[str, dict[str, Any]] = {}
        for ident, body, modality, stored in self.db.execute(
            "SELECT d.id, d.body, v.modality, v.vector FROM documents d JOIN vectors v ON d.id=v.id"
        ):
            doc = json.loads(body)
            if (kind and doc["kind"] != kind) or (accept and not accept(doc)):
                continue
            score = sum(a * b for a, b in zip(vector, json.loads(stored), strict=True))
            if ident not in results or score > results[ident]["score"]:
                results[ident] = {**doc, "score": score, "matched_modality": modality}
        return sorted(results.values(), key=lambda r: (-r["score"], r["id"]))[:limit]
