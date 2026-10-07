"""Offline proof: one synthetic saved source, two requests, two catalogs, replay and revision."""

import json
from pathlib import Path

from .directives import Directive, canonical, preset
from .distill_store import Registry
from .engine import Assessment, Extraction, Relationships, Verification, execute, replay
from .library import Capture, ChangeBatch, LibraryItem
from .publication import diff, export

TEXT = """A small robotics bracket can be made by cutting sheet plywood with a hand saw.
Drill pilot holes before fastening the bracket with screws to avoid splitting the plywood.
Prerequisites are a hand saw, a drill, clamps, eye protection and a sheet of plywood.
The demonstrated material cost is 8 dollars for a single bracket, excluding tool purchases.
Plywood is affordable and easy to cut, but absorbs moisture and has direction-dependent strength.
Clamp the sheet before cutting; an unsupported sheet can move and cause an inaccurate cut.
An alternative is printing the same bracket with a desktop filament printer.
Printing needs a printer and a bracket model; it permits complex shapes but takes longer.
The printed bracket material cost is 3 dollars, excluding the printer and electricity.
Inspect printed layer adhesion because a bracket can separate under load along layer boundaries.
The plywood bracket and the printed bracket are alternative ways to mount the same small robot.
"""


class FixtureModel:
    """Deliberately scripted responses prove mechanics, not extraction or model quality."""

    name = "fixture-v1"

    def __init__(self):
        self.calls = 0

    async def fill(self, form, prompt, data=None):
        self.calls += 1
        payload = json.loads(data)
        if form is Directive:
            return fixture_directive(payload)
        if form is Assessment:
            return Assessment(
                verdict="include", reason="fixture: relevant robotics fabrication source"
            )
        if form is Relationships:
            return Relationships()
        observed_id = payload.get("observation_id")
        if form is Verification:
            return Verification(
                checks=[
                    {
                        "claim": key,
                        "verdict": "supported",
                        "reason": "fixture: the original text states this claim",
                    }
                    for key in payload["claims"]
                ]
            )
        if form is not Extraction:
            raise ValueError(f"unsupported fixture form: {form}")
        directive = Directive.model_validate(payload["directive"])

        def claim(text):
            return {"text": text, "citations": [{"observation": observed_id, "quote": text}]}

        fields = {
            "idea": TEXT.splitlines()[0],
            "method": TEXT.splitlines()[0],
            "goal": TEXT.splitlines()[0],
            "mechanism": TEXT.splitlines()[1],
            "prerequisites": TEXT.splitlines()[2],
            "tools": TEXT.splitlines()[2],
            "steps": TEXT.splitlines()[1],
            "recipe": TEXT.splitlines()[1],
            "costs": TEXT.splitlines()[3],
            "tradeoffs": TEXT.splitlines()[4],
            "failure_modes": TEXT.splitlines()[5],
        }
        records = [
            {
                "local_id": "plywood",
                "title": claim(TEXT.splitlines()[0]),
                "fields": {
                    f.name: [claim(fields[f.name])] if f.name in fields else []
                    for f in directive.fields
                },
            }
        ]
        if directive.domain == "fabrication":
            printed = {
                "method": TEXT.splitlines()[6],
                "prerequisites": TEXT.splitlines()[7],
                "steps": TEXT.splitlines()[7],
                "costs": TEXT.splitlines()[8],
                "tradeoffs": TEXT.splitlines()[7],
                "failure_modes": TEXT.splitlines()[9],
            }
            records.append(
                {
                    "local_id": "printed",
                    "title": claim(TEXT.splitlines()[6]),
                    "fields": {
                        f.name: [claim(printed[f.name])] if f.name in printed else []
                        for f in directive.fields
                    },
                }
            )
        relationships = []
        if len(records) > 1 and "alternative_to" in directive.relationships.types:
            relationships.append(
                {
                    "source": "plywood",
                    "target": "printed",
                    "type": "alternative_to",
                    "claim": claim(TEXT.splitlines()[10]),
                }
            )
        return Extraction(records=records, relationships=relationships)


def fixture_directive(payload: dict) -> Directive:
    kind = "fabrication" if "fabrication" in payload["request"].lower() else "general"
    result = preset(payload["key"], payload["request"], kind, payload["model"])
    result.catalog = payload["catalog"]
    if payload["key"].startswith("demo-"):
        result.selection.libraries = ["demo-library"]
        if kind == "fabrication":
            result.relationships.types = ["alternative_to"]
    return result


async def demonstration(store: Registry, root: Path) -> dict:
    from .directives import compile_request

    model = FixtureModel()
    item = LibraryItem(
        external_id="robot-bracket",
        revision="1",
        url="https://example.org/robot-bracket",
        title="Synthetic robotics bracket guide",
        authors=["Distill fixture"],
        collections=["robotics"],
        tags=[{"value": "affordable", "origin": "user"}],
        capture=Capture(text=TEXT, origin="synthetic fixture; not a real-world source"),
    )
    store.import_batch(ChangeBatch(library="demo-library", full_snapshot=True, items=[item]))
    first = await compile_request(
        model,
        "Catalog affordable robotics fabrication methods.",
        "demo-fabrication",
        model.name,
        record=store.record_compilation,
    )
    first.selection.libraries = ["demo-library"]
    first.relationships.types = ["alternative_to"]
    second = await compile_request(
        model,
        "Catalog operational steps and failure modes for robotics.",
        "demo-workflows",
        model.name,
        record=store.record_compilation,
    )
    second.selection.libraries = ["demo-library"]
    store.save_directive(first)
    store.save_directive(second)
    manifests = [await execute(store, key, model) for key in (first.key, second.key)]
    galleries = []
    for manifest in manifests:
        path = export(manifest, root)
        galleries.append(str(path.parent / manifest["id"] / "index.html"))
    for manifest in manifests:
        await replay(manifest)
    first.selection.exclude_tags = ["affordable"]
    store.save_directive(first)
    revised = await execute(store, first.key, model)
    await replay(revised)
    export(revised, root)
    report = {
        "synthetic": True,
        "sources": store.report()["counts"]["sources"],
        "observations": store.report()["counts"]["observations"],
        "catalogs": [m["semantic"]["catalog"] for m in manifests],
        "records": [len(m["semantic"]["records"]) for m in manifests],
        "replay": "identical semantic outputs for both catalogs and revision",
        "unrelated_catalog_unchanged": store.head(second.catalog)["id"] == manifests[1]["id"],
        "revision_diff": diff(manifests[0], revised),
        "galleries": galleries,
        "model": "scripted fixture; quality not evaluated",
    }
    (root / "demo-report.json").write_text(canonical(report), encoding="utf-8")
    return report
