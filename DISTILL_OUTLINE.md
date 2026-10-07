# Distill: full product and implementation outline

Status: proposed direction, grounded in the current working tree reviewed on 2026-10-07.

Implementation update (2026-10-07): the bounded directive/source/catalog/replay core and
SQL projection adapter are built. See `DISTILL_IMPLEMENTATION.md` and `docs/distill.md` for
verified behavior and remaining product scope. The bookmark app is not open source yet;
the integration boundary will read its accessible SQL database.

This document records the intended generalization of the project. `SPEC.md` remains the
description of the existing system. Proposed capabilities below are not claims that they
have already been implemented. The bookmark application has not yet been inspected.

## 1. Purpose

Distill turns a natural-language request into a living knowledge catalog. It discovers
relevant material from the web, bookmark libraries, and existing catalogs; extracts
structured knowledge; checks claims against source evidence; and connects results across
catalogs.

A shared source library serves multiple directives. Versioned rules, preserved evidence,
and saved model responses make processing traceable and replayable.

Graphics techniques are the first application. Other directives could extract fabrication
methods, software architecture decisions, experimental procedures, historical arguments,
or operational workflows. Each directive defines what knowledge matters and what evidence
is required.

The product should be as easy to start as prompting an LLM. The resulting execution should
be as deterministic as practical.

## 2. Intended user experience

Example request:

> Build a catalog of small-scale robotics fabrication methods from my saved items and
> relevant primary sources. Prioritize affordable tools. Extract prerequisites, steps,
> costs, tradeoffs, and failure modes. Keep it current.

Distill should automatically:

1. Translate the request into a validated, persistent directive.
2. Select candidates from connected libraries and existing catalogs.
3. Discover additional sources according to that directive's rules and limits.
4. Collect and preserve the observations needed for extraction and verification.
5. Extract structured knowledge and check source support.
6. Assign catalog membership and create justified relationships.
7. Publish a searchable catalog for people and agents.
8. Maintain it when sources, metadata, or the directive change.

Follow-up instructions such as "include industrial methods" or "restrict this to my
woodworking collection" create a new directive version. Existing results retain the
version that produced them.

Routine execution proceeds automatically within the directive's defined scope and budget.
Ambiguity is represented explicitly. If a required setting cannot be inferred or supplied
by a declared default, Distill asks a focused question and continues independent work.

## 3. Relationship with the bookmark application

The bookmark application is expected to collect saved material from several services into
one interface. Its actual APIs, storage model, tagging behavior, extension mechanisms, and
license remain unknown until its repository is available.

The intended division of responsibility is:

| Responsibility | Intended owner |
|---|---|
| Collect bookmarks and expose saved items | Connected application |
| Organize user collections and user-authored tags | Connected application |
| Display original items and ordinary library browsing | Connected application |
| Interpret requests into versioned directives | Distill |
| Acquire and preserve evidence used for distillation | Distill, reusing suitable supplied captures |
| Extract, check, and connect derived knowledge | Distill |
| Record runs, decisions, dependencies, and publication history | Distill |
| Display derived catalogs and processing status | Connected application through integration |
| Export catalogs and provide command-line operation | Distill |

Use an adapter boundary so Distill can support this application, other source libraries,
and its existing standalone CLI. The deployment shape—an embedded package, local service,
or extension—should follow the application's actual architecture.

Bookmarking supplies discovery candidates and metadata. A saved title, preview, tag, or
existing catalog entry does not by itself establish support for a factual claim. Derived
knowledge should retain its original evidence chain.

## 4. What exists today

### 4.1 Discovery and ingestion

- Video URLs, channels, playlists, profiles, searches, and forum-discovered links.
- Source prioritization informed by observed yield and code-owned coverage queries.
- Primary research bundles and configured arXiv, conference, and page feeds.
- Public HTML, plain-text, PDF, and browser-observed text collection.
- Import of previously studied sources, screenshots, and style guides.

Relevant files: `discovery.py`, `frontier.py`, `research.py`, `papers.py`, `studies.py`,
`media.py`, and `seeds.toml` / `frontier.toml`.

### 4.2 Structured extraction and source checks

- Defined model forms for screening, extraction, verification, and composition.
- Code-controlled routing, acceptance thresholds, and claim survival.
- Video claims with source timestamps and a canary checking video delivery to the model.
- Research claims with observed excerpts, source locators, and hashes.
- Separate modding workflow extraction, including steps, tools, formats, and versions.
- Audit notes and documentation assessments stored separately from original claims.

Source checking establishes that observed material supports a claim. It does not establish
independent reproduction or scientific truth. Model assessments remain fallible.

Relevant files: `forms.py`, `stages.py`, `papers.py`, `audit.py`, `documentation.py`.

### 4.3 Storage, automation, and recovery

- SQLite queues, stored cards, compositions, styles, and cached answers.
- Job leases, retry delays, persisted attempt budgets, and restart recovery.
- Autopilot connecting discovery, research, extraction, cataloging, and indexing.
- Research revision handling, reports, heartbeat, and stop/retry controls.
- An autopilot catalog completeness gate that preserves the prior publication when
  extraction-to-entry coverage is incomplete.
- A Windows launcher for a hidden process; the service still depends on a running process.

Relevant files: `db.py`, `automation_store.py`, `runner.py`, `autopilot.py`, and
`scripts/Start-Autopilot.ps1`.

### 4.4 Catalogs, relationships, and retrieval

- Fixed technique and workflow catalogs.
- HTML galleries, Markdown, JSON, and agent-facing indexes.
- Technique composition relationships and reference hypotheses.
- Attribution, source links, timestamps, and research citations in published outputs.
- Local semantic retrieval over technique cards, workflow cards, and web observations.

Relevant files: `merge.py`, `catalog.py`, `workflow_catalog.py`, `retrieval.py`.

### 4.5 Verification baseline

The existing suite passed in the outline review: **221 tests passed**. This is evidence for
the behavior covered by those tests, including simulated provider interactions. Live model
providers, current external feeds, and the bookmark application were not exercised in that
review. The working tree includes uncommitted implementation, so this inventory describes
the working tree rather than only the latest commit.

## 5. What is missing

### 5.1 A general directive model

Create a validated, versioned directive containing:

- Purpose, domain scope, inclusion rules, and exclusions.
- Eligible libraries, collections, source types, and source discovery rules.
- Tag vocabulary, selection predicates, relevance assessments, and uncertainty handling.
- Extraction schema and required knowledge fields.
- Evidence requirements, source-check rules, and acceptance thresholds.
- Relationship types and the requirements for asserting them.
- Output catalog identity and publication policy.
- Scheduling, refresh intervals, attempt limits, and resource budgets.
- Model configuration and versions of prompts, schemas, and executable rules.
- Explicit defaults and unresolved required settings.

The LLM compiles a request into this declarative structure. Code validates it and executes
supported operations. A directive should not require model-generated executable code.

### 5.2 A unified source and observation model

Separate four concepts:

1. **Library item:** an external saved record with its own collections and metadata.
2. **Source:** the original work or resource with a stable identity.
3. **Observation:** a preserved revision or bounded capture of source content.
4. **Interpretation:** knowledge derived from observations under a directive version.

One source can be referenced by many library items and interpreted by many directives.
One research bundle can include several sources. Existing catalog outputs can become
inputs while retaining upstream lineage.

Replace the assumption that every source is represented through a video record. Introduce
stable identities for sources, observations, claims, outputs, and relationships that do
not depend solely on replaceable SQLite row numbers.

Define an evidence retention policy for each modality. Current video processing deletes
downloaded media, so fresh video verification cannot be reproduced solely from saved card
timestamps. Offline replay of saved responses and a fresh verification run have different
input requirements.

### 5.3 Attributed tags and explicit membership

Support imported tags, user tags, and model-proposed classifications as distinct records.
Record their origin, relevant evidence, vocabulary version, and uncertainty or confidence
label. Confidence labels need evaluation before being treated as calibrated probabilities.

Vocabularies may be shared or directive-specific. Tags retrieve candidates; validated
selection rules and the directive's recorded assessments determine catalog membership.
An uncertain classification should be able to remain unresolved.

### 5.4 Configurable extraction and verification

Generalize graphics and modding-specific criteria into presets. Supply validated output
schemas, reusable extraction stages, and verification policies appropriate to each
knowledge type.

Preserve the existing code-owned decisions and modality-specific evidence handling.
Generalization must support different outputs without erasing useful distinctions between
video timestamps, exact source excerpts, inferred interpretations, and observed workflows.

### 5.5 A catalog registry and multiple connected graphs

Introduce arbitrary catalog identities, directive associations, versions, and memberships.
A source or output may participate in multiple catalogs, with the role and reason recorded.

Represent typed relationships such as:

- Extracted from / supported by.
- Requires / produces / composes with.
- Alternative to / contradicts.
- Revision of / derived from.

Different catalogs may expose different relationship sets over shared entities. The same
pair of entities may have several relationships with different evidence and scopes.
Distinguish observed, source-asserted, model-inferred, and code-derived relationships.
Similarity can propose a relationship for evaluation; acceptance follows explicit rules.

Existing composition graphs are a starting point. SQLite tables can initially represent
typed nodes, edges, membership, and evidence; a separate graph database is not a prerequisite.

### 5.6 Library integration

Define an adapter contract for:

- Stable external item IDs and original source URLs.
- Item revisions, titles, author information, content, tags, and collections.
- Available evidence captures and their provenance.
- Incremental change cursors, events, or polling, according to upstream capabilities.
- Item additions, edits, removals, and collection membership changes.
- Capability discovery and adapter versioning.
- Derived catalog, status, evidence, and relationship access for the application UI.

Deduplicate before acquiring content again. Do not mutate library metadata as a side effect
of extraction. Publishing derived data back into the application is an explicit integration
operation. Persisted metadata and content used in a run must remain identifiable afterward.

### 5.7 Directive-aware automation

Extend the current service to handle multiple directives with:

- Fair scheduling and shared resource limits.
- Shared source acquisition with directive-specific interpretation jobs.
- Incremental invalidation when observations, tags, rules, or model configurations change.
- Start, pause, resume, retire, and revise operations per directive.
- Per-directive progress and explanations of unresolved or failed work.
- Publication and search updates isolated by catalog version.

Define removal behavior separately for withdrawing an item from a collection, retracting a
catalog output, removing a source, and deleting retained evidence. An upstream bookmark
deletion must have an explicit policy rather than an accidental cascading effect.

### 5.8 Complete deterministic replay

Create a run manifest identifying:

- Directive version and executable rule versions.
- Exact source observations and imported metadata revisions.
- Candidate ordering, selected comparison inputs, and classification results.
- Prompts, schemas, model configuration, and saved model responses.
- Dependency versions and retrieval/index configuration where they affect decisions.
- Decision records, output identities, and the published catalog version.

Use consistent cache keys across stages. Paper extraction already includes detailed prompt,
schema, model, and input dependencies; video-stage memoization relies largely on a forms
version and stage key. Both should participate in the same dependency contract.

For fixed directives, observations, metadata, executable versions, and saved model responses,
offline replay should produce identical semantic catalog content and relationships. Define
operational timestamps and other incidental fields separately from semantic output.

Fresh model calls and live discovery can vary. Their results should be bounded, recorded,
and reusable. Concurrent job completion order must not silently change membership, grouping,
identities, or graph edges. Catalog prose generation and grouping also belong in the replay
contract.

### 5.9 Inspectable publication

Provide versioned publication, catalog diffs, and explanations of inclusion, exclusion,
claim survival, and relationship acceptance. Preserve the evidence chain when consuming
another catalog and prevent circular derivation from being counted as independent support.

Expose these through the application's actual extension mechanisms, while retaining
portable Markdown/JSON, agent indexes, and command-line operation.

## 6. Target flow

```text
Connected libraries / existing catalogs / discovered sources
                           |
                    Source adapters
                           |
              Source registry + observations
                           |
               Attributed tags + metadata
                           |
User prompt -> Versioned directive -> Explicit selection rules
                           |
                 Structured extraction
                           |
               Evidence checks + decisions
                           |
             Catalog membership + typed graphs
                           |
                Versioned publication
                           |
        Application interface / search / portable exports

Changes feed dependency tracking and scheduled updates.
Run manifests preserve the inputs and decisions for replay.
```

## 7. Implementation sequence and acceptance gates

### Phase 1: Integration discovery and directive contract

- Inspect the bookmark application's repository when available.
- Establish the adapter boundary and deployment shape.
- Define the first directive schema and supported declarative operations.
- Specify identities, evidence labels, and replay semantics before moving data.

Acceptance: a request becomes a valid, inspectable directive; unsupported or unresolved
requirements are represented explicitly; the directive can be stored and loaded unchanged.

### Phase 2: Sources, observations, and lineage

- Introduce the general source model and external ID mappings.
- Add immutable observation revisions and directive-linked interpretations.
- Define migration and retention behavior for current records.
- Preserve existing catalogs and attribution during migration.

Acceptance: one source is reused across two directives without duplicate acquisition or
overwriting either interpretation; existing source links and evidence remain traceable.

### Phase 3: First application adapter and general extraction

- Import a bounded collection from the connected application.
- Select candidates using attributed tags and directive rules.
- Convert current graphics and modding behavior into presets.
- Add a genuinely different domain to test whether the abstraction holds.

Acceptance: a prompt selects saved items and produces a source-checked catalog in the new
domain; existing domain regression checks still pass.

### Phase 4: Catalog registry and connected graphs

- Add arbitrary catalog memberships and typed relationship records.
- Preserve directive scope, evidence, and uncertainty on relationships.
- Support overlapping catalogs and input catalogs with upstream lineage.

Acceptance: shared entities appear in two catalogs with independently justified membership;
relationship types and provenance survive publication and export.

### Phase 5: Prompt-to-publication vertical slice and replay

- Expose directive creation and progress through the integration.
- Connect collection, extraction, checks, graph updates, and publication.
- Complete manifests, decision logs, cache dependencies, and offline replay.

Acceptance: one shared source, two directives, two catalogs, and identical semantic outputs
on offline replay; interrupted execution resumes without duplicating completed results.

### Phase 6: Continuous maintenance and operational polish

- Handle upstream changes and source revisions incrementally.
- Schedule multiple directives with shared budgets and fair resource allocation.
- Publish diffs and apply explicit withdrawal/deletion policies.
- Evaluate extraction and selection quality on representative examples.
- Choose persistent process management appropriate to the deployment.

Acceptance: source or tag edits update the affected outputs; unrelated outputs remain
current; failures preserve the last complete publication and expose an actionable status.

## 8. Decisions to resolve with implementation evidence

- What integration and extension mechanisms does the bookmark app provide?
- Does it preserve content or only URLs and previews? Can it expose usable capture revisions?
- Which deployment shape fits its runtime and distribution model?
- What is the minimum general directive vocabulary that supports real non-graphics cases?
- Which tags and relationship types should be shared, and which belong to a directive?
- How much video and other evidence should be retained for fresh verification?
- Which outputs require direct evidence, and which explicitly permit inference?
- What are the intended policies for upstream deletion, retraction, and source expiry?

These are design questions for subsequent work. They do not prevent recording the outline
or defining the first contracts using explicit provisional assumptions.

## 9. First proof of the direction

Build a bounded demonstration using the connected application's real interface when
available, or a fixture implementing the same adapter contract during development:

1. Start with one saved source and its original evidence.
2. Create two different directives from natural-language requests.
3. Produce two catalogs with different structured interpretations.
4. Link both outputs to the same source observation.
5. Display the catalogs and their evidence through the integration.
6. Replay with saved inputs and responses and compare semantic outputs.
7. Revise a directive or source and show the resulting scoped changes.

This proves the central product claim: a shared library can support multiple purposes,
with automated processing, connected outputs, and reproducible decisions.
