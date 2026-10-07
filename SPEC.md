# Distill

The general directive-driven Distill pipeline is implemented alongside this graphics/video
system. Its contract, evidence gates, SQL projection boundary, replay semantics and operational
limits are documented in `docs/distill.md`; `DISTILL_IMPLEMENTATION.md` distinguishes verified
implementation from the broader proposed scope in `DISTILL_OUTLINE.md`.

Watches tutorials and showcases of visual effects, real-time graphics (shaders, WebGL2, WebGPU,
GPU compute), 3D modeling, animation and stylized looks, and distills
the techniques in them into knowledge that can be rebuilt anywhere: any application, engine,
shader or hand-drawn frames. The point is composition: combining techniques into new effects.
So each technique is described by its idea and by the signals it consumes and produces, and the
catalog records how creators wired techniques together. Where a technique was seen (Blender,
Houdini, a pixel-art editor) is provenance, never a category or a filter. The first consumer is
a WebGL2 renderer, but nothing in the catalog is specific to it.

## Optional retrieval and browser research (2026-10-06)

`distill index` builds a disposable `data/search.sqlite` over raw technique cards, workflow
cards and separately stored browser research. `distill search` ranks these with local
EmbeddingGemma 2 vectors (pinned model revision, 256 dimensions by default, normalized after
truncation). Text uses the model's distinct document and search-query prefixes. `--images`
indexes existing thumbnail images in the shared embedding space and enables image queries.
The index caches by document and image content, drops missing documents and rebuilds on encoder
configuration changes. Failed updates leave the last complete index usable. It is refreshed
explicitly, not automatically by workers. Search results retain source timestamps, evidence
labels and modality; ranking never changes verification, depth, grouping or discovery priority.

`distill research <url> --goal <task> --max-steps N` uses the pinned Jev agent to navigate public
pages and saves observed viewport text and URLs under `data/research/`. It requires Jev's
TypeSafe service and a separate text helper. A reading-only prompt is advisory, not a hard
action/domain sandbox; the user explicitly supplies each browser task. The adapter bounds ticks,
captures initial and subsequent observations, and records done/blocked/step-limit. No screenshots,
typed values or full action traces are persisted. Jev DONE does not prove a research outcome.
Every web capture is labelled unverified and remains separate from verified technique cards;
it can be searched as `--kind web` after re-indexing. Existing extraction, verification,
yt-dlp discovery and the video canary continue as specified below.

Research also captures visible hyperlink destinations with a code-owned DOM reader. Optional
`--enqueue` (or `distill discover-web`) accepts observed single-video links on YouTube,
Instagram, Vimeo and TikTok, validates host/path/id, canonicalizes, deduplicates, assigns the
existing code-side discovery score with a low source prior, and queues at most `--limit` new
rows. No model invents URLs or bypasses the existing screening and video verification stages.

`distill check-docs --card <technique:id|workflow:id> --docs-host <exact host>` retrieves fresh
observations only from the explicitly selected HTTPS documentation hosts (7 days by default).
The text model fills a closed per-claim checklist of agreement, disagreement, version
applicability, source id and exact quote. Code requires an existing source and verbatim quote,
unambiguous agreement/disagreement and applicable version before recording support or
contradiction; absent/duplicate claim replies, fabricated citations and unknown applicability
become unclear. Workflow inputs include tool/game versions and ordered steps. Observations are
incomplete excerpts; a recent capture does not prove latest-version documentation. Results in
`data/doc-checks/` are advisory, separately cached by claims, sources, prompt, model and source
policy, and marked stale when cards change or sources age out. Failed checks retain the prior
report. They never mutate raw cards, verification results, discovery priority or merged entries.

`distill research <url> --http` is a direct HTTP path for static HTML/XHTML documentation,
requiring no browser model credentials. It excludes script/style/head content, observes the
final URL after redirects, and shares the same unverified capture/index/queue/documentation
check flow. It does not execute JavaScript; HTTP-blocked or dynamic pages require Jev.

With `EXPERIENTIAL_API_KEY`, the Jev adapter sends native decisions to
`https://api.experientiallabs.ai/v1/systemone` using the explicit `jev-latest:free` spelling;
that key is never forwarded to TypeSafe. Target heads contain at most 64 observed options.
A lone compatible target is resolved in code (the gateway requires at least two choice
options), after the model selects the operation. No automatic retries or paid-model fallback
are added. `--keys-file` or `DISTILL_RESEARCH_KEYS_FILE` reads only the relevant keys from an
external env file without copying it. Without TEXT_MODEL_API_KEY, field text is filled through
the existing text backend as a closed text form, including the Claude/Codex CLI path.

Attached targets receive Runtime.runIfWaitingForDebugger before renderer commands, preventing
the observed background-tab startup stall. An explicit local `--cdp-url` uses a sequential
WebSocket connection; other connections retain Browser Harness. Only transport is adapted:
Jev's observed element identities and action freshness guards remain. Its HTML control reader
may not offer links in XML/XHTML; the static HTTP path handles those references.

## 1. Rules

- A model fills closed forms; code decides. The keep/drop threshold, which claims survive and
  which cards merge are code, never a model's free choice.
- Every claim on a card cites a timestamp, and a second pass checks it against the video.
  Unsupported claims are dropped, not softened.
- Raw cards are kept forever. Merging can be redone from them without re-watching.
- Videos are deleted once carded. Only metadata and cards are stored.
- No secrets in the repo. OmniRoute runs locally without a key; cookies are read from Firefox
  by yt-dlp at download time and never copied.

## 2. Model access

OmniRoute at `http://127.0.0.1:20128/v1` (OpenAI-style), model `agy/gemini-3.8-flash-high`.

Video goes in as a base64 mp4 in a `file` content part (`image_url` with a video data URI also
works). Measured on 2026-09-23: `video_url` and `input_video` parts are dropped without an
error and the model invents an answer. So every run starts with a canary: a generated clip of
three random solid colours the model must name in order. No canary pass, no work.

Replies are streamed. OmniRoute expires a request that has sent nothing for 15 s
(`resilienceSettings.requestQueue.maxWaitMs`), and card extraction thinks for about a minute
before its first token; unstreamed, it fails with a 504 every time.

Steps that send no clip (prescreen, technique grouping, entry text) go to `DISTILL_TEXT_MODEL`,
for more knowledge and reasoning than the video model has. It defaults to `DISTILL_MODEL`; an
OmniRoute model name uses OmniRoute, and `claude:<model>` or `codex:<model>` (model optional)
runs one headless CLI call per step on that subscription. Neither CLI watches video, so
`DISTILL_MODEL` may not be one: the run refuses to start. A CLI call has no tools, hooks, MCP
servers or user config, and runs in an empty directory, because its prompt holds uploader text.
Its reply is not streamed (no OmniRoute timeout applies); a usage-limit failure counts as busy.
Every command takes `--provider {omniroute,claude,codex}` and `--model` for the text model
(either alone keeps the other part) and `--video-model`, overriding the environment.

## 3. Pipeline

```
seeds ─► discover ─► queue ─► fetch ─► screen ─┬─► outline ─► card ─► verify ─► compose ─► verify ─┬─► previews ─► merge ─► catalog
                                                  └─► study (reference) ─► gaps ─► auto seeds ──────────┘
```

| Stage | Who | Output |
|---|---|---|
| discover | yt-dlp flat listing of channels, playlists, searches, profiles, forum threads | queued URLs, deduped by site id, each scored (section 8) |
| fetch | yt-dlp (+ Firefox cookies for Instagram), ffmpeg to 360p/CRF 32 | segments of at most 10 min |
| screen | a text-only pre-screen (`prescreen_route`) drops an off-topic or shallow YouTube video from its captions alone, before any download; the rest are fetched, then the model fills a checklist and code routes it (`route`) | learn / study / drop, with reasons |
| outline | model, per segment: every technique with its start and end | spans; code keeps those inside the clip and not yet carded |
| card | model, one call per span-sized clip (the span padded ±8 s and cut out with ffmpeg), in parallel | one technique card, with the checklist (`app_chore`, `textbook_basic`, `non_obvious`, `has_numbers`) code turns into `depth` |
| verify | model re-watches that same span clip with the card's claim ids | per-claim supported / unsupported / unclear |
| compose | model, per clip with two or more cards: which technique's output drives which input | effects of links, each verified like a claim; both ends must be cards from this video |
| study | model, for a showcase that does not teach: parts of the look, their signals, likely builds, matching catalog techniques, a search query per part | an inferred reference; code keeps only real matches, and parts with none become auto seeds |
| previews | ffmpeg, from the full-quality source before it is deleted | per card: a thumbnail and a muted clip of its steps plus the result, time-lapsed to ~8 s |
| merge | model proposes which technique names are the same, code checks and records it; model writes one entry per group (`idea`, `result`, `use_when`, the body); code unions signals and mediums and adds sources, links and media; compositions and references are resolved onto entries | entries, compositions, references |
| catalog | code | `data/catalog/` (section 5) |

Queue statuses: `new → leased → dropped | carded | referenced`, or `failed` after three attempts
(`distill retry` re-queues). A worker leases a row; an expired lease returns it to the pool.

One pass asking for every card at once returned one card from a video teaching five techniques.
Outlining first and carding each span separately is what gives recall.

Groupings and entries are cached by the hash of their inputs, so a re-merge replays earlier
decisions and only asks again for groups whose cards changed.

## 4. Technique card

Knowledge, not UI (decided 2026-09-24): menu paths and clicks age with every release and do not
transfer; the idea, the mechanism and the creator's decisions do. And techniques, not
applications (same day): the catalog exists to compose techniques and rebuild them anywhere.

- `idea`: the core insight, application-independent. `result`: what the viewer sees.
- `consumes` / `produces`: signals from a closed vocabulary. `time` (a 0..1 progress or clock),
  `mask` (a 0..1 field selecting where), `coords` (positions or UVs), `shape`, `deform`, `color`,
  `particles`, `screen` (post), `motion` (timing and spacing), `normal` (surface orientation),
  `light` (light arriving at a surface), `distance` (a signed distance field) and `buffer` (GPU
  data one pass writes and another reads). Two techniques compose when one's output type is
  another's input type.
- `mediums`: where it can be realized: shader, geometry (procedural), modeling (shaping a mesh by
  hand: topology, bevels, retopology, UVs), compute, lighting, particles, 2d-frames, post,
  animation. The last four signals and the modeling, compute and lighting mediums were added on
  2026-09-27 to take in graphics programming and modeling (`FORMS_VERSION` v4).
- `app_chore`, `textbook_basic`, `non_obvious`, `has_numbers`: a closed checklist (added
  2026-09-27, `FORMS_VERSION` v5) the model fills so code, not the model, can tell a deep insight
  from a textbook basic or a pure application chore ("offset coordinates to animate flow", "expose
  geometry parameters to interface"). `card_depth` turns the checklist plus the claims that
  survived verification into a 0-5 `depth`, stored on the card body and used to order the
  catalog. `apply_verification` drops an `app_chore` card outright, whatever depth it would score.
- `mechanism`, `decisions`, `knobs`, `pitfalls`, each claim with a timestamp.
- `recipe`: maths or pseudo-code with the creator's values as examples, never node names.
- `made_with`: provenance only.

Measured 2026-09-27: every card and verify call sent the whole clip (up to 10 min) for a
technique spanning 30-120 s, and the screen downloaded videos it went on to drop. Now a YouTube
video with English auto-captions is pre-screened from text alone before any download
(`PreScreen`, filled with `Model.fill`'s text-only path, no clip): `prescreen_route` drops it only
on a clear signal (off-topic, a pure click-along, or depth below `Settings.prescreen_min_depth`);
anything uncertain, or without captions, or not YouTube, proceeds to the real, video-based screen.
And a card's own call and its verify call now watch only that technique's span, padded (`SPAN_PAD_S`)
and clamped to the clip (`span_bounds`), cut out with ffmpeg (`cut_span`) with the same encoding as
`cut`. The model's timestamps come back span-relative; `shift_card` and `shift_verification` move
them to clip time before `apply_verification` runs, so stored cards and memoed answers stay
clip-relative exactly as before the span existed.

The screen routes by code: a video that teaches with reasons (at the quality threshold) is
learned from; a showcase that does not teach but shows a look worth replicating is studied as a
reference; anything else is dropped. No application is filtered out. The outline lists ideas and
folds application chores into the idea they serve. The verifier accepts a reason only if the
creator gives it or plainly shows it, never because it is true in general. A card survives if it
is not an application chore, and its idea and at least one mechanism or recipe claim do. A
composition link survives if it is verified and both its ends are cards from the same video.

A reference is a hypothesis about how a look was made and is always labelled inferred. Its
parts are matched to catalog techniques (code keeps only names that exist); a part with no match
is a gap, and its search query becomes an auto seed, so the looks you collect steer what the
pipeline learns next.

Saved model answers are keyed with `FORMS_VERSION`, so a form change never replays an answer in
the old shape. `distill recard` re-queues videos finished under an older version.

## 5. Catalog

One set of entries, two front doors, all in `data/catalog/`:

- **For agents:** `llms.txt` starts with how to compose, then one line per technique grouped by
  the signal it produces (idea, consumes → produces, mediums, use when, source count), then the
  observed compositions (verified wiring) and the references (inferred breakdowns, what they are
  buildable from, what is missing). `signals.md` lists what makes and what uses each signal.
  Each technique has a file (`techniques/<signal>/<slug>.md`) with its recipe, "seen combined
  with" links and timestamped sources. `llms-full.txt` is everything in one file; `catalog.json`
  holds techniques, compositions, references and the link graph. `SKILL.md` points Claude at
  `llms.txt`.
- **For people:** `index.html`: techniques, compositions and references as galleries whose
  thumbnails play on hover, signal and medium filters, search, and detail views that link
  techniques to each other and to the source video at the right second.

Media stays local: it is cut from other creators' videos, for reference, with links back.

## 6. Failure handling

Measured on 2026-09-23, with 4 workers and a fixed cap of 3 calls, 45 minutes produced no new
videos. Four videos cycled through retries: OmniRoute turned calls away, one rejected call threw
away a video's finished cards, and YouTube refused a download. What replaced that:

- **Busy is not failure.** OmniRoute's 429, 503 `chat_admission_busy` and 504 local-limiter
  answers are `Busy`. The call waits (10 s doubling to 120 s, up to 30 min) and the in-flight
  limit halves, then creeps back up on success (`AdaptiveGate`). If a video still ends busy, it
  goes back in the queue with its attempt refunded.
- **Answers are saved as they arrive** (`memo` table: screen, outline per clip, card per span).
  A retry replays them and asks only for what is missing. Cards reach the catalog only when a
  whole video is done, so a video is never half in it.
- **Leases are renewed** every quarter lease while a video is worked, so a long wait never hands
  the same video to a second worker.
- **Downloads retry** three times with backoff; YouTube's 403s under load are temporary. YouTube
  is fetched anonymously: with Firefox cookies it answers "The page needs to be reloaded".
- **Instagram profiles and hashtags** are listed with gallery-dl; yt-dlp marks its Instagram
  profile extractor as not working (2026.08.19) but still downloads single posts.
- Clips are 2 fps: Gemini samples about 1 fps, and it nearly halves what OmniRoute must admit.

OmniRoute's admission control limits heavy (video) requests, not request count: 10 small calls
at once all pass, 10 calls carrying a 92 s clip are all turned away while another run is active.
Its five Antigravity accounts only help once that admission limit is raised.

## 7. Scale

Throughput is bounded by site download limits and OmniRoute's rate, not CPU. Both are settings:
per-site concurrency and minimum spacing, model concurrency. The runner loops until stopped or
until a daily video cap, and refills the queue from seeds when it runs low.

## 8. Discovery

`Queue.lease` used to take the oldest `new` row. The first seeds' beginner search results (about
40 videos) blocked everything behind them: in three days only 3 of roughly 25 seeds ever ran.
Discovery now scores every candidate on arrival, in code, before any model call, and grows its
own sources instead of exhausting a fixed list (`discovery.py`).

- **Priority.** `discover` carries channel (uploader URL, when the flat listing gives one),
  `view_count` and `upload_date` (when present) from the listing into the queue alongside title
  and duration. `discovery.score` combines: the source's smoothed yield (below) times
  `SOURCE_YIELD_WEIGHT`; a duration band bonus for roughly 6–90 minutes and a penalty under a
  minute (waived for Instagram and TikTok, built around short video); and title terms — a bonus
  for *breakdown, explained, how ... works, GDC, SIGGRAPH, talk, math/maths, shader, deep dive,
  theory, technique*, a penalty for *beginner, in 60 seconds, #shorts, speedrun, timelapse, free
  download, asset pack, giveaway, reaction*. `Queue.lease` orders by this priority, then by age.
  Every refill rescores all waiting videos (`rescore_waiting`) from their source's current
  record, so the queue follows the evidence as it arrives.
- **Source track record.** `SourceStats` (watched, carded, value) is computed from `videos`
  grouped by `seed` and by `channel`, joined with `cards`. A video is worth the depth of its
  three deepest cards, divided by 9 (three depth-3 cards = one solid video); a card made before
  depth existed counts as depth 2. Card count alone buys nothing: measured on 2026-09-28, the old
  beginner searches led a cards-per-video ranking (55 shallow cards from 20 videos) and would have
  outranked every curated channel. `smoothed_yield` blends the observed value-per-watched rate
  with a prior (`YIELD_PRIOR_WATCHED` = 4 watched-video's worth), so a brand-new source is
  judged fairly instead of on zero evidence, and a seed's declared tier sets that prior: tier 1
  (`prior = 2.0`) starts with more benefit of the doubt than a low-effort keyword search (tier 3,
  `prior = 0.5`; a search no longer in `seeds.toml` is judged as tier 3 too). This yield feeds both `score` and the refill: each seed's `limit` is scaled by
  `refill_multiplier` (clamped 0.25×–2.5×), and a seed with at least 8 watched videos and zero
  cards is paused — logged, not deleted, since a fresh seed with a real track record can still
  earn its way back through the multiplier if watched again by hand (`distill add`).
- **Snowball.** A channel with two or more carded videos and yield at or above the baseline
  becomes its own auto seed (its uploads listing), stored in `auto_seeds` with `kind='channel'`.
  Separately, a carded video from a good-yield source has its description fetched once
  (`media.video_metadata`, no download — reusing the call `upload_date` already made) and scanned
  for YouTube links (`extract_youtube_links`); each linked video is enqueued directly with
  `seed = "link:<video id>"`. A link names one video, so it is not kept as a seed that every
  refill would list again. Channel auto seeds are listed like any other seed on the next refill. Titles and descriptions stay untrusted data: code only ever pulls out
  URLs matching a fixed pattern, never anything read as an instruction.
- **Curated seeds.** `seeds.toml` seeds are channels, YouTube searches, or a `discourse` forum
  base with `tags`. `tier` (or an explicit `prior`) sets the source's starting yield; `match` is a
  title regex, for a channel or tag broad enough to carry off-topic uploads (a conference channel,
  a big tag).
- **realtimevfx.com.** A `discourse` seed lists topics under `/latest.json` and each `tag.json`,
  fetches each topic's `/t/<slug>/<id>.json`, and scans its posts' `cooked` HTML for YouTube,
  Vimeo and Instagram links (`discourse_topic_links`), queued with `seed` set to the topic's URL.
  Requests are spaced (`DISCOURSE_PAUSE_S`) and capped per feed (`limit`).
- **Coverage-driven search.** Each refill counts cards per signal (`produces`) and per medium
  (`mediums`); a signal or medium under `COVERAGE_THIN_BELOW` cards gets a query from a fixed,
  code-side template (`COVERAGE_QUERIES`, e.g. `buffer` → "ping pong render target simulation
  shader"), added as a low-prior `kind='search'` auto seed, capped per refill
  (`COVERAGE_SEED_CAP`). No model chooses the query; the map does.
- `distill sources` lists watched, carded, value, yield, refill multiplier and paused state per
  seed and per channel, sorted by yield, for a look at what the queue is actually learning from.

## 9. Studied sources and styles

Not every source of technique is a video someone watched. A studied source is knowledge already
distilled, by hand or by another agent, from a creator's own projects. Preserve the creator's
attribution and respect their permissions and rights. A studied source brings its own cards,
already in the exact shape `forms.Card` fills, plus a style guide and screenshots. `distill
import <dir>` reads the fixed contract in that directory:

```
<dir>/cards.json = {
  "source": {"id", "author", "author_url", "home", "credit"},
  "projects": [{"slug", "title", "url", "screenshots": [...]}],
  "cards": [{...every forms.Card field..., "project", "evidence": "source"|"inferred",
             "confidence": "high"|"medium"|"low"}],
}
<dir>/style.md   -- a style guide; its first line is the credit
<dir>/shots/*.png
```

The model never decides anything at import: code validates each card against `forms.Card`,
drops what does not fit or names an unknown project (logged, not silently lost), drops
`app_chore` outright exactly as the video path does, and computes `depth` with the existing
`forms.card_depth`. Each project becomes one `videos` row — id `web:<source id>/<slug>`, site
`web`, status `carded` straight away, seed `studied:<source id>` — with no lease and no
download, ever. Its cards are replaced whole on every import (clear, then add), so re-running
`distill import` updates a source in place instead of duplicating it. Every card carries
`project`, `evidence`, `confidence` and the source's `credit`, `author`, `author_url`, and
`studied: true`, so attribution travels with the card wherever it merges to. Its screenshots are
copied into the catalog media folder the way previews are, and the first one becomes the card's
thumbnail (`media`: `{"thumb": ...}`, no `clip` — there is no clip to loop, so the gallery just
shows the still).

**Safety.** A `web` row is not a video and must never be treated like one: `Queue.lease`,
`requeue_for_new_forms` (`recard`), `requeue_failed` (`retry`), `carded_videos` (the snowball
link scan) and the seed/channel stats discovery scores from all filter `site != 'web'` (or,
for `lease`/`waiting_videos`, never see a `web` row in the first place, since it is inserted
straight into `carded`). `previews.backfill` skips any card whose video id starts with `web:`
before it ever looks at whether the card has media, since a studied card's media comes from
`distill import`, not a re-download.

**Merging and the catalog.** Studied cards merge with video cards under the ordinary
`merge.merge` grouping — same technique name, same signal, one entry — so a technique can carry
both a video source and a studied one. `merge.source_list` gives a studied source no
timestamps (there is no clip to cite) and instead carries its project title, credit, evidence
and confidence; `catalog.index_line` and `catalog.technique_md` add the credit line whenever any
of an entry's sources is studied, so the attribution shows in the one-line `llms.txt` entry and
in the technique file, never only in a footnote. `data/catalog/index.html`'s gallery treats a
studied source exactly like a video one, except hover-play does nothing when there is no clip.

**Style guides.** A source's `style.md` is stored verbatim (`styles` table, keyed by source id)
and published to `data/catalog/styles/<source id>.md`. `llms.txt` lists every style under a
`## Styles` heading, one line each — author, credit, a link to its file — and `llms-full.txt`
appends every guide's body. `index.html` gets a fourth tab, Styles, rendering the guide as
Markdown (the project already depends on `markdown`) next to the author's credit and the
project's screenshots.

### 9.1 Automated primary research

`distill autopilot` adds timestamp-free `papers.ResearchCard` forms. It fetches primary research
HTML, plain-text documentation and PDFs (optional `papers` dependency), records public
observations, and divides observed text into numbered chunks with URL, PDF page or character
range, and SHA-256 hash. Each idea, result, mechanism, decision, knob, recipe and pitfall carries
one or more exact excerpts. It neither executes repositories nor downloads model checkpoints.

The text model fills `Curation`; code requires on-topic mechanism teaching, a full method,
depth >= 3, non-obvious substance and no promotion or introductory basic. Existing technique
cards are retrieved for method comparison (semantic when enabled, lexical otherwise). Code
rejects a duplicate only when the exact comparison id is known and the checklist reports no
new tradeoff/evidence. A lacking full method becomes `watch`, not an invented technique.

`Extraction` is followed by `SourceVerification`. Code accepts a claim only with exactly one
supported check, known chunk ids and every cited quote matching observed text after whitespace
normalization. A card requires a supported idea, result, and mechanism/recipe, and passes the
same application-chore/basic/non-obvious gates. Closed legacy `Card` shape is used transiently
to calculate depth; research storage has no `t` or `idea_t` values. A fully verified source's
cards are replaced in one queue transaction (`web:<bundle>/method`), so a crash cannot expose
partial replacements. Its source evidence is labelled `primary-source-supported`, confidence
medium, with the explicit level `authors_claim; source-checked, not independently reproduced`.
This verifies attribution/support, not scientific truth or a successful implementation.

Source excerpts, locators, hashes, maturity and author credit survive merging, catalog.json,
Markdown, HTML and semantic search. Research merge hashes include card contents, so an updated
paper invalidates entry prose even if SQLite reuses card ids. Video-only merge cache behavior
remains compatible with existing immutable cards.

### 9.2 Orchestration and recovery

`frontier.toml` declares arXiv Atom queries, ECCV JSON catalogs, primary pages and user bundles.
ECCV oral/spotlight/poster duplicates union their resources and keywords by absolute source id.
Bundle aliases use exact normalized titles and canonical resource URLs (arXiv versions share
one identity; GitHub file links share a repository identity). No fuzzy title or embedding match
merges a work. Different arXiv identities remain separate even with a shared title or toolkit.
Configured bundles have highest scheduling priority, then recent submissions/revisions and
conference orals, other conference work and general feeds. Topic matching is a cheap discovery filter; model checklist
and code gates operate after source collection. Author video links enter the regular queue.

`automation.sqlite` stores jobs, resource aliases, feed refresh times, lease tokens, persisted
rolling-day attempt counts, retry delays, observed-source caches and reports. Defaults: 2 bundles
per cycle, 1800 seconds between cycles, **50 attempted bundles per rolling 24 hours**. The cap
counts failed attempts too; Busy refunds the job failure attempt but consumes the daily budget.
Failures back off exponentially and become visible `failed` jobs after three failure attempts.
`watch` jobs refresh source evidence after seven days; changed arXiv versions reopen jobs.
Model replies are memoized by forms/prompts, text model, bundle, observed chunks and comparisons.
Leases renew during long model calls; token checks reject stale owners. An OS lock permits only
one orchestrator per data directory, and releases after a crash. `--retry` reopens failed/watch
jobs. `--report` performs no model/network work; `--stop` requests completion then shutdown.

The ordinary video runner runs concurrently, with its canary, site gates, renewable leases and
daily cap; `--external-video-worker` uses a separately running worker. Cancellation drains and
cancels owned video tasks before closing their model client. A one-cycle run bounds its owned
video starts to two and refreshes the catalog again after they finish.

Each cycle builds the technique/workflow catalogs and refreshes the local index independently.
Catalog fingerprints suppress unchanged builds. Before publishing, code requires every input
technique card to be represented; an entry-generation failure cannot overwrite the prior
catalog with a partial result. Search preserves prior dimensions/image settings and commits
atomically. Failed stages remain in `autopilot-report.json` and retry next cycle. Optional
`--no-search` disables embeddings and uses lexical comparisons. The service is local and
process-based; this does not install a machine-startup task.

The service publishes its PID, model, limits and a minute heartbeat for readable operational
status. `scripts/Start-Autopilot.ps1` launches it hidden with separate timestamped output/error
logs, detects a currently active orchestrator and uses an existing video worker when present.
Operational `uv run` report/stop/retry commands use `--no-sync`: Windows locks an active
`distill.exe`, and implicit environment synchronization can otherwise attempt to reinstall
that executable. The running installation remains available without synchronization.
Reports distinguish a later successful index rebuild from an earlier failed search stage:
they display `recovered` and both times without changing the saved historical cycle. A newer
failure takes precedence over an older recovery.
PDF page locators distinguish separate search documents sharing one URL; ordinary HTML
observation identities remain compatible with earlier documentation assessments.

## 10. Audit

Verification (section 3) asks whether the creator said a claim; a creator can be wrong and it
still passes. `distill audit` asks whether each claim is true: the text model (section 2, best
with `--provider claude` or `codex`) gets one card's claims as text, no video, and fills
`Audit`, one verdict per claim id: `correct`, `wrong`, `overstated` or `unsure`, with a reason
and, when flagged, how the claim should read.

It only flags. `wrong` and `overstated` claims appear in the catalog under "Audit notes" on
their entry, naming the source and the model; nothing is dropped or rewritten, because the
auditor never saw the clip and can be wrong too. An audit is kept per card with the hash of the
card's claims and the audit prompt and the model's name: a rerun skips cards already audited by
that model, a changed card is audited again, and an audit of older claims is ignored.
`--limit N` audits a sample, `--force` redoes all, `--report` lists flagged claims.

## 11. Workflow mode

The screen (section 3) keeps technique and drops click-alongs, which is right for a technique
library and wrong for learning how modders work, where the tool usage is the content. A seed with
`mode = "workflow"` (seeds.toml; default `technique`) has its videos watched this way instead:

| Stage | Differs from the technique pipeline |
|---|---|
| prescreen | skipped: it asks about graphics technique and would drop a modding video |
| screen | `WorkflowScreen`; `route_workflow` keeps a video that is on topic, shows a task carried out, and has `quality` at `keep_threshold`. A walkthrough is never dropped for being one; a showcase, gameplay or talk is |
| outline | `WorkflowOutline`: the distinct tasks, each a goal a modder would search for, with start and end |
| card | `WorkflowCard`, one call per span: goal; tools with versions only if stated; ordered steps; input and output file formats (closed list: M2, WMO, ADT, BLP, DBC, MPQ, SQL...); game version (1.12, 2.4.3, 3.3.5a, 4.3.4, other, unstated); server core; pain points with workarounds; time spent if a timer, a cut or the creator gives it |
| verify | the same `VERIFY` call over the span clip. Goal, each tool, each step, each pain point and the time are claims; `apply_workflow_verification` keeps the supported ones with an in-clip timestamp. A card survives if its goal and one step do |
| compose, previews, merge | none. Cards are not merged across videos: two modders doing one task give two cards, because the differences are the point |

A video's mode comes from its seed's URL (`Video.seed`, set at `add` time or by discovery), so
videos queued before the option existed pick it up from seeds.toml. Cards are stored in
`workflow_cards`, never in `cards`, so the technique catalog, merge, audit and coverage seeds
never see them. `distill workflows` (and `distill catalog`) write `data/catalog-workflows/`:
`llms.txt` (one line per card, by game version), `llms-full.txt`, `catalog.json`, `index.html`
(filterable) and `workflows/<goal>.md`, each step linked to the video at its second.

Scoping a run: `distill run --only REGEX [--limit N]` leases only videos whose seed, channel or
url matches (case-insensitive), starts at most N, never refills, and ends when none is left.
`discover`, `retry` and `recard` take `--only` too. The run logs its time and model requests.
