"""Import catalogs through original observations, never as independent support."""

from .directives import canonical, identity
from .distill_store import Registry
from .engine import replay
from .library import Capture, ChangeBatch, LibraryItem


async def import_catalog(store: Registry, manifest: dict) -> dict:
    await replay(manifest)
    catalog = manifest["semantic"]["catalog"]
    selected = {r["observation"] for r in manifest["semantic"]["records"]}
    items = []
    for observed_id in sorted(selected):
        candidate = next(
            c for c in manifest["inputs"]["candidates"] if c["observation_id"] == observed_id
        )
        observation = candidate["observation"]
        prior_lineage = observation.get("lineage", [])
        if len(prior_lineage) >= 10:
            raise ValueError("catalog lineage exceeds the bounded ten-catalog depth")
        output_ids = [
            r["id"] for r in manifest["semantic"]["records"] if r["observation"] == observed_id
        ]
        entry = {
            "run": manifest["id"],
            "catalog": catalog,
            "directive_key": manifest["inputs"]["directive"]["key"],
            "observation": observed_id,
            "outputs": output_ids,
        }
        capture_body = {k: v for k, v in observation.items() if k != "source"}
        capture_body.update(
            modality="catalog",
            upstream=sorted(
                set([*observation.get("upstream", []), observed_id, manifest["id"], *output_ids])
            ),
            lineage=[*prior_lineage, entry],
        )
        capture = Capture.model_validate(capture_body)
        items.append(
            LibraryItem(
                external_id=observed_id,
                revision=manifest["id"],
                url=candidate["url"],
                title=candidate["metadata"]["title"],
                authors=candidate["metadata"]["authors"],
                collections=[catalog],
                source_type="catalog",
                capture=capture,
            )
        )
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO upstream_runs VALUES (?,?)",
            (manifest["id"], canonical(manifest)),
        )
        for key, upstream in manifest["inputs"].get("upstream_runs", {}).items():
            store.db.execute(
                "INSERT OR IGNORE INTO upstream_runs VALUES (?,?)", (key, canonical(upstream))
            )
    return store.import_batch(
        ChangeBatch(
            library=f"catalog:{catalog}",
            adapter_version="catalog-v1",
            cursor=identity("cursor", manifest["id"]),
            full_snapshot=True,
            items=items,
        )
    )
