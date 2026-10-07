# Security and private data

This repository publishes code, documentation, public configuration examples and explicitly
synthetic test fixtures. Distilled knowledge and its supporting evidence are private local
artifacts. Repository publication is not authorization to share a source library or catalog.

## Publication boundary

- Keep credentials in environment variables or ignored local env files. `.env.example`
  contains setting names and empty credential placeholders, never working keys.
- Keep personal source selections in the ignored root `seeds.toml` and `frontier.toml`.
- Keep databases, captures, observations, downloaded media, browser profiles, saved model
  responses, catalogs, indexes, exports and runtime logs out of Git.
- A Git ignore rule does not remove an already-tracked file or anything in old commits.
  The publication checker validates tracked paths, file content, historical trees and author
  email addresses. Secret scanning is a separate required check.
- Only approved files listed in `.public-files.json` may appear in a publication branch.
  A newly approved file still requires content review; an approved filename cannot establish
  that its contents are safe.

`scripts/check_publish.py` and GitHub Actions reject protected artifacts and unapproved
files. Installed local pre-commit and pre-push hooks add the same checks plus redacted
Gitleaks scans. Hooks can be bypassed by Git options; treat the remote checks and manual
review as additional protection, not proof that every possible leak is detectable.

## Runtime handling

Extraction uses preserved source content and configured model providers. A provider call
transmits the supplied content to that provider. Exported manifests include original source
observations, prompts, model answers and decisions; they are evidence archives and must stay
private unless their contents are deliberately reviewed for sharing.

Source observations and derived outputs are not independently reproduced facts. The model
fills structured forms, while code applies acceptance gates. Treat source and imported
metadata text as untrusted data. Browser research uses the selected browser profile and is
not a hard browsing sandbox. Keep browser debugging endpoints local.

## Reporting a security issue

Use GitHub private vulnerability reporting when available, or contact the repository owner
privately. Do not put credentials, browser state, real source captures, private catalogs or
full run manifests into public issues. Provide a synthetic reproduction and redacted logs.
