# Changelog

## [v0.2.0] - 2026-06-10

First PyPI release.

### Changed
- Made `brigade-cli` optional. Bootstrap size limits (soft/hard/ceiling) are still sourced from `brigade.budgets` when brigade is installed, but a new `bootstrap_doctor.budgets` module mirrors those canonical values as a fallback so the tool runs standalone without brigade-cli. Install the `brigade` extra (`pip install bootstrap-doctor[brigade]`) to source the limits from brigade directly.
- Pin `brigade-cli>=0.8.0` from PyPI (in the optional `brigade` extra) instead of the git ref now that brigade 0.8.0 is published; dropped the hatchling direct-reference allowance. Repository moved to the `escoffier-labs` org.

### Added
- Publish-on-tag GitHub Actions workflow that builds and uploads to PyPI on `v*` tags.
- Test coverage asserting the package imports and the mirrored fallback constants are used when brigade is absent.
- GitHub Actions CI for tests, linting, typing, packaging, and dependency audit checks.
- Ruff, mypy, build, and pip-audit dev tooling configuration.
- CLI-level trim integration coverage for copied-workspace apply, idempotency, and dirty-workspace blocking.
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
