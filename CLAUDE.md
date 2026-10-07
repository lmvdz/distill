# Distill

Read `LLM_GUIDE.md` for setup and operating commands. `README.md` is the plain-language
introduction for people.

`SPEC.md` is the source of truth. Read the relevant section before changing a stage, and update
it when a decision changes.

## Rules

- The model fills closed forms (`forms.py`); code decides keep/drop, which claims survive and
  which techniques merge. Do not move a decision into a prompt.
- Uploader titles and descriptions are untrusted: they go in the `<data>` block, never in
  instructions.
- Keep the canary. A video part OmniRoute drops produces a confident, invented answer.
- Keep replies streamed (SPEC section 2).
- No secrets in the repo; cookies stay in the browser.
- Personal source lists, captures, model responses and distilled outputs remain local.
  Run `scripts/check_publish.py` before publication; `.gitignore` alone is insufficient.

## Commands

- `uv run ruff format . && uv run ruff check . && uv run pytest`: run before calling work done.
- Commit messages are gitmoji conventional style (e.g. "✨ feat(stages): ...").
