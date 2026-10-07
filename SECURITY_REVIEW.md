# Distill publication review

Reviewed 2026-10-07. Scope: the proposed GitHub payload, retained local Git history,
package archives, and dependency advisory checks. This is a publication/privacy sweep,
not a penetration test or a guarantee against every possible vulnerability.

## Findings and changes

- No secret findings in the existing development history using Gitleaks 8.30.1.
- No secret findings in the proposed code-only payload. Available local credential values
  were also compared in memory with candidate files and historical blobs; no matches found.
- Candidate files had no exact normalized matches to long saved claim strings examined
  from local distilled records. This check used 80–5,000-character strings and complements
  the artifact/file allowlist; it does not establish that all shorter text is non-sensitive.
- Development history contains personal commit metadata and personal source configuration.
  The prepared publication branch uses a fresh root commit and GitHub no-reply identity.
  The original history remains local and is not an ancestor of the publication branch.
- Personal `seeds.toml` and `frontier.toml` stay on disk but are excluded from publication.
  Public starter examples replace them in the reviewed payload.
- Databases, derived catalogs, evidence captures, model responses, search indexes, media,
  browser profiles, runtime logs, local env files and build outputs are excluded.
- An exact approved-file list, local Git hooks and GitHub Actions enforce the boundary.
  Checks include historic trees, so deleting a leaked file from the newest commit is insufficient.

## Verification

- Ruff code checks and formatting checks passed.
- 288 tests passed, including 21 publication-boundary checks.
- The base runtime dependency advisory audit reported no known vulnerabilities.
- The all-extras audit checked 66 registry dependencies with no known vulnerabilities.
  The pinned Git-sourced `jev-ultrafast` dependency has no coverage in this advisory tool
  and remains unaudited by it rather than being treated as cleared.
- The renamed wheel and source distribution were built and inspected. Their contents
  contain only approved source/documentation files and standard package metadata.
- Secret-scan reports and privacy-sweep details remain in local temporary storage, with
  secret findings redacted. No credentials or private catalog excerpts are included here.

GitHub repository creation, push, remote CI execution and branch protection configuration
have not been performed by this preparation. All published checks must pass again on the
actual pushed commit.
