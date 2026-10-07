# Distill implementation status

Reviewed 2026-10-07. The runnable directive-driven core is implemented alongside the
existing graphics and workflow pipelines. This report measures it against the broader
product direction in `DISTILL_OUTLINE.md`; it does not claim every proposed integration
or future operation is finished.

## What this is

Distill turns saved material into useful catalogs for different purposes. Each result
keeps the source passages and decisions that produced it, so it can be checked and rebuilt.

## Overall

Completion and confidence percentages below are **engineering estimates**, not measured
quality scores. Passing-test counts and the demonstration results are measured.

```text
Built     [###############-----]  75% of the proposed product
Trusted   [###########---------]  55% for the proposed end-to-end product
```

The local core works in the bounded demonstration and automated tests. The friend's
application has not been inspected, and live model extraction quality has not been
measured. Those missing links limit confidence in the complete product even though
the mechanics have stronger evidence.

## The parts

```text
Part                       Built (estimate)          Trusted (estimate)
Requests and rules         [##################--] 90%    80%
Shared sources/evidence    [##################--] 90%    85%
Knowledge extraction      [################----] 80%    55%
Catalogs and connections   [##################--] 90%    80%
Offline rebuilding        [################----] 80%    85%
Ongoing maintenance        [################----] 80%    80%
Bookmark app connection    [######--------------] 30%    25%
Existing system migration [##############------] 70%    80%
```

- **Requests and rules:** strict versioned contracts, explicit defaults, unresolved settings,
  compiler traces and grounded library inventories. Compiler behavior is tested with fixtures;
  live request interpretation remains unmeasured.
- **Shared sources/evidence:** immutable imported revisions and captures, exact excerpts,
  PDF page spans, shared acquisition leases and distinct interpretation identities. SQL
  projection, revision conflicts, withdrawals and concurrent acquisition have passing tests.
- **Knowledge extraction:** generic cited-field extraction, independent source checks,
  required-field gates, explicit inference and graphics/modding/fabrication presets. These
  checks establish source support, not independent truth or calibrated model quality.
- **Catalogs and connections:** arbitrary catalog identities and memberships, separately
  checked within-source/cross-source relationships, preserved upstream catalogs and cycle
  rejection. JSON, Markdown, agent indexes and HTML galleries preserve attribution.
- **Offline rebuilding:** self-contained manifests, full stage cache dependencies, executable
  hashes, saved compiler/stage answers and semantic equality checks. This covers the new
  pipeline; legacy video/paper/grouping/prose stages are not all migrated to the contract.
- **Ongoing maintenance:** registered SQL/fixture polling, scoped invalidation, fair sequential
  scheduling, rolling stage-request budgets, restart recovery and pause/resume/retire/retry.
  A Windows hidden-process launcher exists; automatic machine-start service management does not.
- **Bookmark app connection:** a read-only SQL projection contract and tested SQLite adapter
  exist. The real database engine, schema, distribution model and native UI remain unknown.
- **Existing system migration:** additive stable source/entity mappings and original row
  aliases preserve legacy bodies, citations, timestamps and attribution. Legacy catalogs
  continue operating; legacy interpretations are not fabricated into new observations.

## Evidence and trend

```text
Passing tests in this session (vertical: count; horizontal: checkpoint)
267 |                                  * final regression
264 |                        * automation/connections
258 |              * first general pipeline
221 | * baseline
    +------------------------------------------------------
      baseline     core      automation       final
```

`uv run --no-sync pytest -q`: **267 passed**, including 46 new general-pipeline tests.
`uv run --no-sync ruff check .`: passed. The existing 221 tests continue to pass.
The example SQL projection was exercised against a real temporary SQLite database;
its read-only behavior and Windows file-handle release were verified. The Windows
launcher passed PowerShell syntax parsing; it was not started against a live provider.

The scripted proof demonstrates:

1. One saved source and one preserved observation shared by two directives.
2. Two source-checked fabrication records and one different workflow interpretation.
3. Identical semantic outputs and relationships on offline replay.
4. An isolated directive revision with visible withdrawals and an unchanged other catalog.

The suite also exercises interrupted-stage recovery, invented citations, duplicate checks,
run/daily/attempt limits, provider quota pauses, metadata changes during a run, library
polling failures, inferred-claim gates, connected-catalog lineage and SQL mutation rejection.

Live providers, the real bookmark database, current public feeds and native app UI were
not exercised. Browser surfaces were unavailable for visual inspection; exports were
checked structurally and for evidence preservation and escaped source text.

## Acceptance gates from the outline

| Phase | Implemented and verified | Remaining before the full phase is complete |
|---|---|---|
| 1: Contract and integration discovery | Validated directives, immutable versions, inspectable unsupported settings, adapter contract | Inspect the app/database and settle deployment |
| 2: Sources and lineage | Shared sources, immutable observations, independent interpretations, additive legacy mappings | Full legacy interpretation/catalog migration and media retention decisions |
| 3: Adapter and general extraction | Fixture/SQL projection adapters, presets, non-graphics fabrication proof | Real app projection and representative live extraction evaluation |
| 4: Connected graphs | Arbitrary catalogs, overlapping evidence membership, typed supported edges and upstream lineage | Real app graph presentation and evaluated relationship quality |
| 5: Publication and replay | Fixture request-to-publication proof, exact offline replay, saved-stage restart recovery | Native app UI and unification of legacy stage/prose replay |
| 6: Maintenance | Polling, source refresh, scoped invalidation, fair scheduling, budgets, diffs and failure preservation | Broader discovery providers, quality evaluation and final process deployment |

## Task list

- [x] Build and verify the directive/source/catalog/replay core.
- [x] Add a read-only SQL projection adapter and persistent polling configuration.
- [x] Preserve existing graphics/workflow behavior and original evidence during additive migration.
- [x] Verify shared acquisition, restart recovery, withdrawals and failure isolation.
- [x] Provide a runnable offline proof, usage guide and Windows launcher.
- [ ] Inspect the bookmark application's SQL engine/schema and implement its actual projection/driver.
- [ ] Connect derived catalogs/status/evidence to that application's UI.
- [ ] Evaluate real selection, extraction and relationship quality on representative sources.
- [ ] Add general feed/free-form search discovery beyond explicit URLs and legacy graphics frontier.
- [ ] Extend the new replay contract to the remaining legacy video/paper/grouping/prose stages.
- [ ] Integrate general records with the existing semantic embedding index.
- [ ] Choose evidence deletion/media retention and automatic startup policies for the final deployment.

## What needs attention

- Scripted responses prove behavior under supplied answers; they cannot establish live model accuracy.
- The real SQL schema may need different revision/cursor handling or a non-SQLite driver.
- Original video media may be absent; saved timestamps are insufficient for fresh video verification.
- Replay requires the recorded executable dependencies, and some legacy processing remains outside it.

## What happens next

Inspect the bookmark database's engine and schema. That will turn the tested SQL boundary
into a concrete application connection and identify the native UI/deployment work.

Run commands and integration contracts: [`docs/distill.md`](docs/distill.md).
