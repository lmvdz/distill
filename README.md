# Distill

Distill turns sources into evidence-backed, versioned knowledge catalogs. It supports a
general directive-driven pipeline alongside its graphics tutorial and research workflows.
Your saved libraries, distilled catalogs, evidence, credentials and runtime state stay local.

For a fresh checkout, create local configuration from the public examples:

```powershell
uv sync
Copy-Item .env.example .env
Copy-Item examples/seeds.example.toml seeds.toml
Copy-Item examples/frontier.example.toml frontier.toml
```

Personal discovery configuration is ignored by Git. See [publishing safeguards](PUBLISHING.md)
and [security and data handling](SECURITY.md) before preparing a GitHub push.

The general **Distill** pipeline is now available through `distill knowledge`: versioned
natural-language directives, shared source observations, read-only bookmark SQL import,
source-checked structured fields, connected catalogs, offline replay and scheduled maintenance.
See [the usage guide](docs/distill.md) and [implementation status](DISTILL_IMPLEMENTATION.md).

```powershell
uv run --no-sync distill knowledge --registry data-distill-demo/distill.sqlite --output data-distill-demo/catalogs demo
```

This bounded demo uses synthetic evidence and scripted responses to prove one shared source,
two catalogs, identical offline replay and an isolated directive revision. Live model quality
and the actual bookmark application's SQL schema/UI remain to be evaluated.

Distills graphics tutorials from YouTube, Instagram and TikTok, and primary research papers,
into source-checked technique cards, merged into a catalog with two front doors: `data/catalog/llms.txt`
for agents and `data/catalog/index.html`, a hover-to-play gallery, for people. See `SPEC.md`.

Needs `uv`, `ffmpeg` on `PATH`, and OmniRoute running locally.

```sh
uv sync
uv run distill canary                 # proves the model receives video
uv run distill add <url> [<url>...]   # a video, channel, playlist, profile or ytsearchN:query
uv run distill import <dir>           # import a studied source (cards.json, style.md, shots/)
uv run distill run --workers 4        # runs until stopped; refills from seeds.toml
uv run distill run --only channel-pattern --limit 5   # only matching videos (seed/channel/url regex), at most 5
uv run distill workflows              # build data/catalog-workflows/ from workflow-mode cards
uv run distill status
uv run distill retry                  # re-queue failed videos
uv run distill catalog                # build data/catalog/ from all cards (safe mid-run)
uv run distill previews               # cut missing thumbnails/clips for already-carded videos
uv run distill sources                # watched/carded/cards/yield per seed and channel
uv run distill audit --provider claude --model opus   # judge every claim; --report lists flags
```

A seed with `mode = "workflow"` in seeds.toml is watched as modding workflows (tools used, ordered
steps, file formats, game version, pain points) and carded into `data/catalog-workflows/`, apart
from the technique catalog. See `SPEC.md` section 11.

Instagram uses Firefox's cookies (`DISTILL_COOKIES_BROWSER`). Settings are in `.env.example`.

Steps that don't watch video (prescreen, technique grouping, entry text) can run on Claude or
Codex for stronger reasoning: `DISTILL_TEXT_MODEL=claude:opus` or `codex:gpt-6.1-sol` uses that
CLI's own login. Video steps stay on OmniRoute. Any command takes the same as flags:
`uv run distill run --provider claude --model opus` (or `--provider codex --model gpt-6.1-sol`),
and `--video-model` for the video model.

Checks: `uv run ruff check . && uv run pytest`.

`autopilot` connects discovery, research, ingestion, distillation, cataloging and semantic
indexing into a continuous local service. `frontier.toml` covers recent arXiv graphics/vision
papers and revisions, the ECCV 2026 public program, real-time rendering course material, and
curated projects spanning animation, contact simulation, geometry, neural rendering and 3D
generation. It uses public HTTP/HTML, plain text and PDF reads; repositories and weights are never
installed or executed. Video links enter the existing worker and retain its video canary.

```sh
uv sync --extra papers --extra search
uv run --extra papers --extra search distill autopilot --provider codex --model gpt-6.1-sol
# Use this when `distill run` is already running:
uv run --extra papers --extra search distill autopilot --external-video-worker --provider codex --model gpt-6.1-sol
uv run --extra papers --extra search distill autopilot --once --limit 2 --external-video-worker --provider codex --model gpt-6.1-sol
uv run --no-sync distill autopilot --report
uv run --no-sync distill autopilot --stop
uv run --no-sync distill autopilot --retry  # failed/watch research jobs; does not reset video jobs
```

Defaults: two research bundles per cycle, a 30-minute interval, and **50 research attempts per
rolling 24 hours**, persisted across restarts (`--daily-cap`, `--limit`, `--interval` override
them). The existing video limits are separate. Feed refreshes are independent; failed feeds
retry after an hour. Job leases, exponential retry delays and cached model answers survive a
crash. Only one autopilot can own a data directory. An insufficient-method bundle is watched
again after a week; permanently failed jobs remain visible until `--retry`. New arXiv versions
reopen existing jobs. Exact title/resource aliases deduplicate conference, arXiv and user bundles.

Research goes through a closed checklist: topical method evidence, depth at least 3,
non-obvious reasoning, and comparison to retrieved existing methods. Similarity retrieves
comparisons; it never decides novelty. Code drops promotion, introductory content and duplicates
without new tradeoffs/evidence. Extraction and a separate verification call produce paper
cards with **exact observed excerpts**, URLs, page/text locators and snapshot hashes, never
invented timestamps. Quotes must match the saved source, and the verifier must support the
whole claim. These are author claims supported by sources, **not independent reproductions**.
Claims, citations, maturity and attribution travel into the gallery, Markdown catalog and search.

Durable state lives in `data/automation.sqlite`; source observations in `data/research/` remain
unverified; `data/autopilot-report.json` records each cycle.
`--report` labels a search failure `recovered` when a later validated index rebuild succeeds,
with separate failure/recovery times; the saved historical cycle record remains unchanged.
Complete catalog builds publish automatically; a failed entry prevents publication and leaves
the prior catalog intact. Search
updates separately and preserves its existing dimensions/image setting. `--no-search` runs
without the search extra, using lexical method comparisons and disabling semantic refresh.
Source collection reads at most six URLs per bundle, 100 MB per response, 150 PDF pages and
120,000 characters of model evidence. Exact excerpts refer to the observed portion of a source.
Stopping finishes the current research job; Ctrl+C cancels immediately with resumable answers.
The service runs while its process is alive; run it again after a machine restart.

On Windows, `./scripts/Start-Autopilot.ps1` starts a hidden process with timestamped logs in
`data/`, using the authenticated Codex text backend. It detects an existing video worker and
uses it; otherwise autopilot owns one. Re-running the script recognizes an active autopilot.
Pass `-DailyCap`, `-Provider` and `-Model` to override the defaults. It prefers `.venv-search`
when available (the local GPU environment), otherwise `.venv`. Model-provider quotas remain
provider pauses; the service never automatically switches providers or buys additional usage.

Local semantic search uses EmbeddingGemma 2 over stored technique and workflow cards. The first
index downloads the pinned model weights (about 1.5 GB); subsequent searches run locally.
The encoder caps inputs at 8,192 tokens, uses bfloat16 on supported GPUs and float32 elsewhere,
and reduces batches automatically under memory pressure. In automatic device mode it falls
back to CPU if loading or even a single-input GPU batch cannot fit. Set
`DISTILL_SEARCH_DEVICE=cpu` to force CPU, or `cuda` to require GPU. These precision choices follow the
[model card](https://huggingface.co/google/embeddinggemma-2#4-numerical-precision).
Install the optional dependencies, then index and search:

```sh
uv sync --extra search
uv run --extra search distill index
uv run --extra search distill search "make stylized fire with animated noise" --limit 5
uv run --extra search distill search "add custom sound to an MPQ" --kind workflow --json
uv run --extra search distill index --images
uv run --extra search distill search --image reference.png
```

`--images` also embeds existing thumbnails; videos remain deleted after carding. Search returns
stored source timestamps and evidence labels. Similarity ranks results and does not verify a
claim. The disposable `data/search.sqlite` index caches unchanged inputs, removes deleted cards,
and rebuilds when the model, dimensions or image setting changes. Re-run `distill index` after
carding, importing, editing cards or collecting browser sources. Use the same `--images` and
`--dimensions` options on each rebuild to preserve your chosen index configuration.

Jev can collect observed source text into the same search corpus. Set `TYPESAFE_API_KEY` for
direct access, or `EXPERIENTIAL_API_KEY` for Experiential's native `jev-latest:free` route.
Set `DISTILL_RESEARCH_KEYS_FILE` to an external env file, or pass `--keys-file`, to read only
research credentials without copying their values. Configure a text helper in `.env`, or use
`--provider claude --model opus` to generate field text through the existing text backend.
Connect Chrome using `uv run --extra research browser-harness --doctor`. The normal Browser
Harness path uses the existing Chrome profile. Give Jev a public reading/navigation task:

```sh
uv run --extra research distill research https://docs.blender.org/manual/en/latest/ \
  --goal "Find the documentation explaining noise texture inputs" --max-steps 20 \
  --provider claude --model opus
uv run --extra search distill index
uv run --extra search distill search "noise texture inputs" --kind web
```

Captures in `data/research/` contain observed viewport text, URLs and visible links, not a full-page
crawl. A prompt asks the agent to stick to public reading; this is not a hard browser sandbox.
Captures record `done`, `blocked` or `step-limit`; all remain **unverified web observations**,
including when Jev says `done`. Captures alone never become technique cards. Autopilot applies
separate method, extraction and source-verification gates; video ingestion retains video verification.
The Experiential adapter uses the native decision endpoint, preserves the explicit `:free`
model spelling, limits target heads to 64 observed choices, and resolves a lone compatible
target in code because the gateway requires at least two choice options. It sends one request
without retries or a paid-model fallback. Direct TypeSafe and separately configured text
helpers follow their providers' pricing. Attached Chrome targets are resumed before renderer
commands. For an explicitly supplied local Chrome debugging endpoint, use
`--cdp-url http://127.0.0.1:9222`; the direct connection retains Jev's DOM snapshots, freshness
guards and executor. This works with isolated headless Chrome too; the command does not launch
or change a browser profile itself. For XML/XHTML references, `--http` may extract links more
reliably than Jev's HTML control reader.

To discover and ingest tutorials, add `--enqueue` to a research task, or queue links from
existing captures. The active runner takes them through the normal screen, card and video
verification stages. Only observed single-video links on YouTube, Instagram, Vimeo and TikTok
are accepted; code canonicalizes and deduplicates them, with a cap on new queue rows.

```sh
uv run --extra research distill research https://realtimevfx.com/ \
  --goal "Find tutorials explaining procedural fire shaders" --enqueue --limit 10
uv run distill discover-web --limit 50
```

To check tutorial claims against documentation, first capture the relevant official pages
with `distill research`, then re-index. Pick a card id from search results and supply the exact
documentation host. This uses the configured text model, not EmbeddingGemma, to fill a checklist:

```sh
uv run distill research https://registry.khronos.org/OpenGL-Refpages/gl4/html/step.xhtml --http
uv run --extra search distill index
uv run --extra search distill check-docs --card technique:123 \
  --docs-host registry.khronos.org --provider claude --model opus
uv run distill check-docs --report
```

Only HTTPS sources on the supplied hosts, captured within `--max-age-days` (default 7), are
eligible. Code records support or contradiction only with an exact quote from an observed
source and a model assessment that it applies to the tutorial's version; missing evidence,
invalid citations or unknown version applicability stay unclear. Workflow steps and their
game/tool versions are included. Assessments are saved separately in `data/doc-checks/`, cached
until claims, sources or model change, and reported as stale when cards change or captures age
out. A recent capture is not proof that the documentation itself describes the latest version.
These are review notes; neither the original card nor video verification is changed.

`research --http` captures static HTML/XHTML without browser or model credentials. It excludes
script, style and head content and records the final URL after redirects. For pages that block
HTTP requests or load their content with JavaScript, use Jev's normal `--goal` mode.

On Windows, an active `distill.exe` worker can prevent `uv sync` from replacing its executable.
For routine commands while workers run, use `uv run --no-sync distill ...`. Plain `uv run`
automatically syncs the environment and can try to rebuild/reinstall the package, which fails
with Windows error 32 while its launcher is open. `--no-sync` runs the existing installation;
it also preserves the GPU environment's manually selected PyTorch wheels.
Use an isolated environment while it runs:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD/.venv-search"
uv sync --extra search --extra research
uv run --no-sync distill index
uv run --no-sync distill search "stylized animated fire" --limit 5
```

For NVIDIA acceleration, install CUDA-enabled PyTorch wheels suited to your driver into that
environment. For example, an environment using CUDA 13.0 wheels can be configured with:

```powershell
uv pip install --python .venv-search/Scripts/python.exe --no-deps `
  --index-url https://download.pytorch.org/whl/cu130 `
  "torch==2.14.1+cu130" "torchvision==0.29.1+cu130"
```

Use `uv run --no-sync` after this manual wheel replacement; a normal sync restores the lock's
portable PyTorch build. Search chooses CUDA automatically when PyTorch exposes an available GPU.
