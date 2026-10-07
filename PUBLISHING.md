# Publishing Distill without private data

The public payload contains code, documentation, public example configuration and synthetic
fixtures. Personal discovery settings and all currently distilled data stay local.

## Prepared publication branch

The first publication is prepared as a clean root commit on `publish/distill`. It does not
inherit the local development history or its personal author email and source-selection files.
The original `main` history remains local. The optional `.publish/distill` worktree contains
only the public files for review and testing; it does not contain the private data directory.

Review the prepared payload before creating the GitHub repository:

```powershell
git show --stat publish/distill
uv run --no-project --python 3.13 python scripts/check_publish.py --ref publish/distill --require-gitleaks
```

Create a GitHub repository named `distill` with the visibility you intend. Push only the
prepared branch to its `main` branch:

```powershell
git push github publish/distill:main
```

The `github` remote is a destination only; preparing it does not create or publish a repository.
Do not push `--all`, mirror this repository or upload a ZIP of the workspace. Those operations
can include development history or private files that are outside the reviewed payload.

## Ongoing checks

The publication policy is an explicit file list in `.public-files.json`. Updating the list
is a review decision; do not approve captures or derived outputs just to make a check pass.

```powershell
uv run --no-project --python 3.13 python scripts/check_publish.py --staged --require-gitleaks
uv run --no-sync ruff check .
uv run --no-sync pytest
```

For complete-history checks, use `--ref <branch-or-commit>`. Pre-push checks validate every
local commit being proposed to the remote. A source checkout's old development branch may
fail the publication policy even when its current files are clean; use the prepared branch.

The checker finds Gitleaks on `PATH`, through `GITLEAKS_BIN`, or in Git's local tools
directory. Install the official Gitleaks release and verify its published checksum when
setting up another machine. Reports remain local and are redacted. GitHub Actions repeats
the history/payload checks and runs Gitleaks with pinned, checksum-verified releases.

Private data stays in its existing local locations. Making a clean publication branch does
not delete, move, anonymize or otherwise change that data.
