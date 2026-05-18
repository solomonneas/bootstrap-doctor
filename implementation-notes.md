# bootstrap-doctor - Implementation Notes

Running log of design decisions, deviations from the spec, and tradeoffs discovered during build.

## 2026-05-18 - Scaffold
- Initial scaffold created, mirrors memory-doctor's hatchling/src-layout/pytest pattern.
- Added `requests>=2.31` as the only runtime dep (for the gateway client in `judge.py`).
- (subsequent entries appended as work proceeds)

## 2026-05-18 - paths.py (config layering)
- Bumped `requires-python` from `>=3.10` to `>=3.11` so we can hard-require stdlib `tomllib` and skip the `tomli` backport. Dropped the 3.10 classifier in pyproject.toml. Rationale: pure-stdlib TOML parsing is simpler than an optional dep and the user's environments are all 3.11+.
- Added `[tool.pytest.ini_options]` with `pythonpath = ["src"]` so `python3 -m pytest` works without an editable install (PEP 668 blocks `pip install -e .` on this Ubuntu system Python).
- Enforced the 12k char ceiling explicitly: `hard_limit` must be strictly less than `HARD_LIMIT_CEILING = 12000`. The constant lives at the top of `paths.py` so other modules can import it if they need the same bound. Reasoning: hard_limit is supposed to leave headroom before content truncation kicks in around 12k, so allowing `hard_limit = 12000` defeats the safety margin.
- `cache_dir` is auto-created at config-resolution time (it's a cache, that's fine). `workspace_dir` and `cards_dir` are never auto-created; `allow_missing_cards=True` only relaxes `cards_dir` (and only if the parent dir exists, so we can create it later in `trim`).
- Default config-file lookup at `~/.config/bootstrap-doctor/config.toml` is best-effort (missing is fine). An explicit `--config` flag or `BOOTSTRAP_DOCTOR_CONFIG` env var that points at a missing file raises `ConfigError`, because that's almost always a typo.
- Env-var coverage for v1: only the six knobs in the spec (`WORKSPACE_DIR`, `CARDS_DIR`, `GATEWAY_URL`, `GATEWAY_MODEL`, `SOFT_LIMIT`, `HARD_LIMIT`). `tracked_files`, `named_workspaces`, heuristics, and cache dir stay config-file-only; they're list-shaped or namespaced and don't map cleanly to env strings.

## 2026-05-18 - parsing.py
- CRLF normalization happens once at the top of `parse_text` via a literal `\r\n` -> `\n` replace (plus lone `\r` -> `\n` as a defensive second pass). All downstream measurements (`char_count`, `line_count`, `start_line`, `end_line`) run on the LF-normalized form, so two files that differ only in line-ending convention produce identical Section output. `parse_file` reads bytes (not text) so Python's `open()` newline rewriting doesn't pre-empt this and silently change what we measure. Char counts are therefore the LF-normalized count, which is what the heuristics threshold (400 chars) and the 10k/11.5k limits should compare against in a platform-stable way.
- H4 and deeper headings (`#### `, `##### `, ...) intentionally do NOT split sections; they stay in the parent section's body verbatim. Implementation gotcha: the H3 regex `^###\s+` also matches `#### Foo` (since `### ` is a prefix of `#### `), so we explicitly guard with a separate `H4_PLUS_RE` check before treating a line as an H3. Without that guard, an H4 would mis-split as an H3 named `# Foo`. Captured in the `test_four_pound_heading_not_split_as_h3` test.
- Code-fence-aware splitting: maintain a single `in_fence` bool that flips whenever we see a line whose stripped form starts with ``` (three backticks). The fence flag is consulted before every heading check, so a `## fake heading` inside a fenced block is appended to the parent section's body and never starts a new section. Fences in the preamble are tracked too, so a preamble that opens a fence containing `## Real` doesn't get cut short mid-fence.
- Preamble emission rule: a preamble Section is only emitted if there's at least one non-blank line before the first H2/H3. Leading blank lines alone do not manufacture an empty preamble. This keeps `parse_text("\n\n## Title\nbody\n")` from emitting a noisy zero-body preamble that would clutter every heuristics run.
- Heading text normalization strips outer whitespace and a trailing run of `#` (with optional inner whitespace), per the ATX closure idiom (`## Title ##` -> `Title`). Inline markdown is preserved as-is, so `Tools (local)` and `**Bold**` survive verbatim. This matches the spec's "do NOT process inline markdown" requirement.
- `last_touched_git_mtime` walks parent dirs looking for a `.git` entry (file or dir, to cope with worktrees and submodules), then runs `git log -1 --format=%ct -- <relative_path>` from the repo root. Empty stdout (untracked file), non-zero exit, subprocess failure, and a missing input file all yield `None` without raising. Subprocess has a 10s timeout to guarantee the function returns even if git hangs.
