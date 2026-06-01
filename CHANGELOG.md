# Changelog

## Unreleased

### Changed
- Source bootstrap size limits (soft/hard/ceiling) from `brigade.budgets` (added `brigade-cli` dependency) instead of redeclaring them locally, ending drift across the escoffier-labs tooling. Repository moved to the `escoffier-labs` org.

- Added GitHub Actions CI for tests, linting, typing, packaging, and dependency audit checks.
- Added Ruff, mypy, build, and pip-audit dev tooling configuration.
- Added CLI-level trim integration coverage for copied-workspace apply, idempotency, and dirty-workspace blocking.
- Tightened config validation for malformed gateway URLs, path separators, control characters, and whitespace-padded string values.
- Read-only verbs can resolve config when `cards_dir` does not exist yet.

### Fixed

- Atomic text writes now preserve existing file permissions on overwrite.
- `trim --apply` now reports card-write failures as expected hard failures instead of unexpected exceptions.

## [v0.1.0] - 2026-05-18

First release.

### Added

- `bootstrap-doctor status` reports per-file char count, line count, and distance from soft/hard limits across the primary workspace and any configured named workspaces.
- `bootstrap-doctor audit` shortlists oversize sections via heuristic rules (size, code-block length, git staleness, cross-file duplication) and asks an OpenAI-compatible LLM endpoint to classify each as `keep`, `move`, or `unsure`. Verdicts are cached on disk by SHA256 of section body.
- `bootstrap-doctor trim` builds a plan from `move` verdicts and (with `--apply`) writes each section out as a memory card, replacing the original with a one-line breadcrumb in the same heading location. Dry-run by default.
- Config layering: defaults, then `~/.config/bootstrap-doctor/config.toml`, then `BOOTSTRAP_DOCTOR_*` env vars, then CLI flags.
- Atomic writes (tempfile plus rename) for every persisted file.
- Git-clean assertion runs before any `--apply` writes against both the workspace repo and (if separate) the cards-dir repo. `--force` bypasses.
- Path-traversal guard on card slugs.
- Card collision policy (`--collision skip|overwrite|rename`) re-checked at apply time, with `O_CREAT|O_EXCL` claim for skip/rename so a race cannot overwrite.
- Cards written before bootstrap rewrites, so a card-write failure cannot leave an orphaned breadcrumb.
- Trim aborts if the audit run had gateway failures, to avoid mutating files from an incomplete audit.
- 294 tests covering parsing, heuristics, judge, trim, safety, status, paths, and the CLI.

### Notes

- Pre-1.0 software. Working and used by the author, no backwards-compat promise.
- Default LLM endpoint is Ollama at `localhost:11434` with `deepseek-v4-pro:cloud`. Any OpenAI-compatible chat-completions endpoint works.
- Requires Python 3.11+ for stdlib `tomllib`.
