# Repository Guidance

## Definition of Done
Before reporting ANY change complete, run and pass ALL of these, re-run after your final edit:
- `python3 -m pytest` (full suite, 309 tests, under a second; no install needed, pyproject sets `pythonpath = ["src"]`)
Report the actual command output. If anything fails, report the failure verbatim and do not claim success.
Ruff and mypy are configured in pyproject but not installed in the system Python; run them only after installing the `dev` extra in a venv. Never claim a lint or type check passed without having run it. CI runs pytest, ruff, mypy, build, wheel smoke test, and pip-audit on 3.11 and 3.12; do not break configs those steps read.

## Project Shape
- Python 3.11+ CLI that audits OpenClaw bootstrap markdown files brushing the ~12k char truncation ceiling, then relocates oversized sections to `memory/cards/` with one-line breadcrumbs left in the original.
- Hatchling src layout. Package is `src/bootstrap_doctor/`, console script `bootstrap-doctor` maps to `bootstrap_doctor.cli:main`.
- Three subcommands: `status` (read-only report), `audit` (heuristics + LLM verdicts, read-only), `trim` (dry-run by default, writes only with `--apply`).
- Pipeline modules: `paths.py` (config layering), `parsing.py` (H2/H3 section splitter), `heuristics.py` (shortlist), `judge.py` (gateway client + verdict cache), `trim.py`, `status.py`, `safety.py` (atomic writes, git-clean gate, slug traversal guards).
- Size limits are imported from `brigade.budgets` (the `brigade-cli>=0.8.0` dependency), not defined locally. `hard_limit` must stay strictly below `HARD_LIMIT_CEILING` (12000).
- Config layering order: built-in defaults, then `~/.config/bootstrap-doctor/config.toml`, then `BOOTSTRAP_DOCTOR_*` env vars, then CLI flags.

## Prohibitions
- Failing test? Never weaken its assertions, skip it, xfail it, or delete it to get green. Fix the code, or report the failure and stop.
- Unsure what a command, flag, or function does? Never guess or invent it. Read `cli.py`, the module source, or `docs/bootstrap-doctor-design.md` first, then cite what you found.
- Blocked by sandboxing, auth, or a missing tool? Report the exact blocker (command + error) and stop. Do not silently work around it or substitute a fake result.
- Pushing? The repo sets `core.hooksPath = hooks`, so `git push` runs `hooks/pre-push`, which scans the tree with content-guard against `~/repos/content-guard/policies/public-repo.json` and blocks on violations. Never push with `--no-verify`. On a block: fix the leak, or add an inline `<!-- content-guard: allow <rule-id> -->` tag on the offending line.
- Tempted to commit local artifacts? `implementation-notes.md`, `memory/`, and `.brigade/` are gitignored. Never commit them, and never delete `memory/cards/` contents.

## Verification
- `python3 -m pytest` runs everything. Works without an editable install.
- Targeted change? Run `python3 -m pytest tests/test_<module>.py` while iterating (test files mirror source modules one to one), then the full suite before reporting done.
- PEP 668 blocks `pip install -e .` into the system Python on this machine. `brigade` resolves from the local clone at `~/repos/brigade/src`; pytest needs no install step.

## Safety Rules
- Adding behavior that writes files? Only `trim --apply` may mutate. Keep every other code path read-only and put new mutating behavior behind the same dry-run default.
- Running `trim` yourself? Dry-run first, always. Never run `trim --apply` against real workspace bootstrap files unless the user explicitly asks for it in this session; it rewrites their files in place. Prefer fixture copies in a temp dir.
- Touching `trim --apply` code? Preserve its preconditions: clean git workspace (`safety.assert_git_clean`), atomic same-directory writes, card targets validated by `resolve_card_target`/`ensure_within`. Cards are written before originals are rewritten. Keep that order.
- `unsure` verdicts are never auto-applied. Do not change that.
- Writing tests that touch `judge.py`? It calls a live LLM gateway (`{gateway_url}/v1/chat/completions`, default `http://localhost:11434`). Inject the `http_post` stub; never let a test hit a real gateway.

## Gotchas
- Changing CLI exits? Exit codes are contract: 0 ok, 1 soft-limit findings, 2 hard error (missing/unreadable file, over hard limit, bad config, dirty workspace on `--apply`).
- Touching `parsing.py`? The section parser is code-fence aware and treats H4+ headings as body text, not section splits. Char counts are measured on LF-normalized text. Read the module docstrings and `docs/bootstrap-doctor-design.md` first.
- Touching the verdict cache? Verdicts are cached by SHA256 of section body at `{cache_dir}/verdicts.json` (schema version 1). Failures and budget-exceeded results are deliberately not cached so retries re-ask the gateway. Keep both properties.

## Memory Handoff
At the end of any substantial task, write a handoff note to `.claude/memory-handoffs/` using that directory's `TEMPLATE.md`.
Record durable discoveries, gotchas, root causes, and decisions. Do not wait to be reminded.
