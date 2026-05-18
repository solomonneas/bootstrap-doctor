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
