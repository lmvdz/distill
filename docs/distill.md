# Using Distill directives

The `distill knowledge` commands implement the general pipeline described in
[`DISTILL_OUTLINE.md`](../DISTILL_OUTLINE.md). They use an additive registry at
`data/distill.sqlite`; existing `queue.sqlite`, graphics catalogs, workflow catalogs,
video verification and autopilot continue to use their existing commands.

## Try the offline proof

```powershell
uv run --no-sync distill knowledge --registry data-distill-demo/distill.sqlite --output data-distill-demo/catalogs demo
```

The demonstration uses a **synthetic source and scripted model responses**. It compiles
two requests, produces a fabrication catalog with two methods and a workflow catalog
with one interpretation, and replays both without a provider. A directive revision
withdraws the fabrication outputs while leaving the workflow publication unchanged.
The report at `data-distill-demo/catalogs/demo-report.json` links to the original
versioned galleries. The fabrication catalog's current gallery reflects the withdrawal.
This proves execution, provenance and recovery mechanics; it does not measure extraction quality.

## Connect a library

A JSON fixture implements the same item contract used by the SQL adapter:

```powershell
uv run --no-sync distill knowledge sync examples/distill/library.json
```

Each item supplies an external ID, a revision, an original HTTP(S) URL, a title,
authors, collections, attributed tags, a source type and an optional evidence capture.
Saved titles, tags and previews cannot substitute for source content. A supplied
capture must identify its origin and modality; PDF captures can supply page spans.
URL-only items are fetched through the existing bounded HTML/plain-text/PDF collector.
PDF acquisition requires the `papers` extra. Supplied captures remain pinned to their
item revision; URL-only items follow shared source observations on refresh.

Library revisions are immutable. Changes to any metadata or supplied capture require
a new upstream revision. A full snapshot withdraws omitted items; an incremental batch
must include explicit removal records. Neither behavior deletes original evidence.

### SQL access

The bookmark application is expected to expose a SQL database. Its engine and schema
are not yet known. `SQLiteAdapter` is the concrete initial implementation; a projection
keeps application tables independent from Distill's contract.

```powershell
uv run --no-sync distill knowledge sync-sql C:/path/bookmarks.sqlite --library bookmarks --query examples/distill/bookmark-projection.sql
```

The query must return an `item_json` column containing one JSON item per external ID.
Adapt the example query to the actual tables. The connection opens in read-only mode
with `query_only` enabled. The adapter never changes bookmarks, tags, captures or schemas.
The revision must change when joined tags, collections or capture data change, too.

Incremental queries additionally return `change_cursor`, ordered in the upstream
cursor's progression, and accept a bound `:cursor` parameter:

```sql
SELECT item_json, changed AS change_cursor
FROM distill_item_changes
WHERE :cursor IS NULL OR changed > CAST(:cursor AS INTEGER)
ORDER BY changed;
```

Pass `--incremental` for this mode. Project the latest state of each external item
once per batch, including deletion tombstones. A snapshot exceeding `--max-items`
(default 10,000) fails without importing a partial snapshot. Narrow the projection
or implement upstream pagination for larger libraries.

`sync` and `sync-sql` register their adapter configuration. Maintenance polls those
connections and invalidates affected directives when imported revisions change.
A different SQL engine needs a driver implementing `LibraryAdapter.changes()` and
`capabilities()` against a read-only connection. No driver or schema is assumed for
the unavailable application.

## Create, inspect and run

Model and registry options go **before** the action name:

```powershell
uv run --no-sync distill knowledge --provider codex --model gpt-6.1-sol create "Catalog affordable robotics fabrication methods from the bookmarks library's robotics collection. Use only saved items. Extract methods, prerequisites, steps, costs, tradeoffs and failure modes." --key robotics --save robotics.directive.json
uv run --no-sync distill knowledge show robotics
uv run --no-sync distill knowledge --provider codex --model gpt-6.1-sol run robotics
uv run --no-sync distill knowledge search robotics "pilot holes"
```

The compiler receives the available library names, collections and tag vocabulary.
Code validates its response and stores the exact compiled structure. Unsupported
operations and unresolved required settings prevent execution and are displayed.
`knowledge schema` emits the contract's JSON Schema.

Edit the saved directive JSON and load it to revise the directive:

```powershell
uv run --no-sync distill knowledge load robotics.directive.json
uv run --no-sync distill knowledge show robotics --version directive-<hash>
```

Each content change produces a new stable version. Older results retain the directive
version that produced them. The key and catalog have separate identities; another
directive cannot take ownership of that catalog. A directive's catalog cannot be
renamed by a revision. Run-time model configuration must match its recorded model.

General, graphics, modding and fabrication presets provide field vocabularies.
Each field contains cited claims, has a maximum length and can be required or optional.
The compiler can define other declarative field sets without generating executable code.
The general pipeline accepts text evidence; direct video delivery and timestamp-based
verification remain in the legacy video pipeline.

## Evidence, selection and relationships

Code applies library, collection, tag and source-type predicates, then saves a separate
model relevance assessment. Uncertain classifications remain visible; by default they
do not enter a catalog. Imported, user and proposed model tags retain their origins and
vocabulary versions. Proposed tags do not alter upstream metadata. Labels are not
calibrated probabilities.

Every surviving claim, including a record title, needs an exact excerpt from a preserved
observation and exactly one qualifying verifier result. Missing, duplicate, unclear,
unsupported or invented evidence cannot pass. Required fields must survive these checks.
Inferences require explicit permission and retain a distinct label.

Code derives source/evidence edges. Permitted model-proposed relationships are checked
within observations and across sources, preserving their evidence and scope. Similarity
alone never accepts a relationship. Graph comparisons are bounded by `max_graph_records`
and the recorded observation-text budget. An empty proposal is not a claim that no
relationship exists.

Source identity conservatively normalizes HTTP(S) URLs while retaining meaningful query,
path, scheme and revision distinctions. Identical source URLs and capture content reuse
stable entities. Concurrent acquisition uses renewable-by-retry source leases; successful
freshness checks are tracked separately from immutable observations. Imported captures
from different revisions remain separate observations.

## Publication and replay

Complete runs publish to `data/catalogs/<catalog>/`. Each immutable run directory contains:

- A searchable `index.html` with source attribution and supporting quotes.
- `catalog.json`, `catalog.md` and `llms.txt` for programs and agents.
- `manifest.json` with directive, compiler trace, observations, imported revisions,
  candidate ordering, schemas, prompts, model settings, responses, decisions and relationships.

The current gallery links to its immutable version. SQLite records the authoritative
catalog head and memberships. A failed job, expired lease, paused directive or change to
the run's directive/inputs prevents publication and preserves the previous complete run.
Files are written atomically; `knowledge export <catalog>` regenerates a completed export.
The gallery provides local text filtering; CLI search is lexical. General catalog records
are not yet integrated into the legacy embedding index.

```powershell
uv run --no-sync distill knowledge replay path/to/manifest.json
uv run --no-sync distill knowledge diff old/manifest.json new/manifest.json
uv run --no-sync distill knowledge export robotics
```

Replay is entirely offline and checks identical semantic output and relationship content.
Stage keys include complete prompts, schemas, model settings, observations and executable
dependencies. Rule source hashes, Python and Pydantic versions must match the recorded
implementation; replay rejects a version mismatch rather than pretending to run old rules.
Operational timestamps and job leases do not enter semantic output or identities.
Reordered candidate inputs do not change semantic results.

Fresh discovery and fresh model calls can vary. Replay reuses their recorded observations
and responses. This does not guarantee fresh provider reproducibility. The legacy video,
paper and prose/grouping caches have not all been migrated to this new manifest contract.

## Connected catalogs and legacy migration

```powershell
uv run --no-sync distill knowledge import-catalog path/to/manifest.json
uv run --no-sync distill knowledge migrate data/queue.sqlite
```

Catalog import replays and validates the supplied manifest before exposing a
`catalog:<name>` library. It imports the **original observations**, keeps upstream
manifests and typed lineage, and rejects circular derivation. A derived catalog cannot
be counted as independent source support. The lineage depth is bounded to ten catalogs.

Migration creates additive source/entity mappings and legacy row aliases. It preserves
original card bodies, citations, timestamps and attribution. It neither rewrites the queue
nor creates fictional observations from legacy interpretations. Fresh video verification
still needs original media; existing downloaded media may already have been deleted.
Legacy catalogs keep their existing publication paths.

## Maintenance and limits

```powershell
uv run --no-sync distill knowledge --provider codex --model gpt-6.1-sol maintain --once
uv run --no-sync distill knowledge --provider codex --model gpt-6.1-sol maintain --daily-cap 200
uv run --no-sync distill knowledge status
uv run --no-sync distill knowledge pause robotics
uv run --no-sync distill knowledge resume robotics
uv run --no-sync distill knowledge retire robotics
uv run --no-sync distill knowledge retry robotics
uv run --no-sync distill knowledge maintain --stop
```

Maintenance polls configured adapters, orders due directives by the least recent start,
refreshes URL-only sources and explicit discovery URLs, and runs one directive at a time.
Leases, responses, attempts and rolling shared/per-directive stage-request budgets persist
across restarts. Exhausted run budgets preserve completed responses for a later retry;
provider quota pauses do not consume the failed-input attempt allowance. Transport-level
retries inside the existing model backend are not separately counted as stage requests.
Token, monetary and provider-wide quotas are not implemented by these stage counters.

Source or tag changes invalidate relevant library scopes. Unchanged interpretations reuse
saved responses; unrelated library directives keep their publication. `withdrawal=exclude`
removes withdrawn items from the current catalog; `retain` explicitly keeps them eligible.
Both preserve evidence and historical publications. Retire stops scheduling and keeps the
last publication. There is no automatic hard deletion of sources or evidence.

Explicit URL discovery is implemented. Free-form web search and general feed compilation
remain unsupported requirements in the first directive contract; the existing graphics
frontier continues through legacy autopilot. Native bookmark UI/writeback and automatic
process startup after machine restart await the actual application's deployment architecture.

On Windows, `scripts/Start-Distill.ps1` starts the maintenance process hidden with logs.
The process must remain alive; the launcher does not install an operating-system service.

See [`DISTILL_IMPLEMENTATION.md`](../DISTILL_IMPLEMENTATION.md) for verified scope and remaining work.
