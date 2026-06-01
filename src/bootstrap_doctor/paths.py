"""Config resolution and default path layering.

Layering order (lowest precedence first):

  1. Built-in defaults (constants at the top of this module)
  2. Config file at ``~/.config/bootstrap-doctor/config.toml`` (overridable
     via ``BOOTSTRAP_DOCTOR_CONFIG`` env var or the ``--config`` CLI flag)
  3. Environment variables (``BOOTSTRAP_DOCTOR_*``)
  4. CLI flags passed to :func:`resolve_config` (highest precedence)

TOML schema:

    workspace_dir = "~/.openclaw/workspace"
    cards_dir = "~/.openclaw/workspace/memory/cards"
    gateway_url = "http://localhost:11434"
    gateway_model = "deepseek-v4-pro:cloud"
    soft_limit = 10000
    hard_limit = 11500
    tracked_files = ["AGENTS.md", "TOOLS.md", "SOUL.md"]
    named_workspaces = ["workspace-claude", "workspace-main"]

    [heuristics]
    min_section_chars = 400
    stale_days = 60

    [cache]
    dir = "~/.cache/bootstrap-doctor"

Recognized environment variables:

  - ``BOOTSTRAP_DOCTOR_CONFIG`` (config file path)
  - ``BOOTSTRAP_DOCTOR_WORKSPACE_DIR``
  - ``BOOTSTRAP_DOCTOR_CARDS_DIR``
  - ``BOOTSTRAP_DOCTOR_GATEWAY_URL``
  - ``BOOTSTRAP_DOCTOR_GATEWAY_MODEL``
  - ``BOOTSTRAP_DOCTOR_SOFT_LIMIT``
  - ``BOOTSTRAP_DOCTOR_HARD_LIMIT``

The remaining knobs (tracked_files, named_workspaces, heuristics, cache dir)
are config-file-only in v1.

Validation rules (all raise :class:`ConfigError`):

  - ``workspace_dir`` must exist and be a directory.
  - ``cards_dir`` must exist and be a directory, unless
    ``allow_missing_cards=True``.
  - ``0 < soft_limit < hard_limit < 12000`` (12k is the empirical ceiling;
    ``hard_limit`` must leave headroom).
  - ``min_section_chars > 0`` and ``stale_days > 0``.
  - ``gateway_url`` must start with ``http://`` or ``https://``.
  - ``tracked_files`` must be non-empty; each entry ends in ``.md`` and
    contains no path separators.
  - Each ``named_workspaces`` entry must be a non-empty string without path
    separators.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Bootstrap size thresholds are owned by brigade.budgets (the canonical source
# of truth shared across the escoffier-labs tooling). Imported under the local
# names this module already exposes so downstream references stay unchanged.
from brigade.budgets import (
    DEFAULT_BOOTSTRAP_SOFT_LIMIT as DEFAULT_SOFT_LIMIT,
    DEFAULT_BOOTSTRAP_HARD_LIMIT as DEFAULT_HARD_LIMIT,
    BOOTSTRAP_HARD_LIMIT_CEILING as HARD_LIMIT_CEILING,
)

# Defaults -----------------------------------------------------------------

DEFAULT_WORKSPACE_DIR = "~/.openclaw/workspace"
DEFAULT_CARDS_DIR = "~/.openclaw/workspace/memory/cards"
DEFAULT_GATEWAY_URL = "http://localhost:11434"
DEFAULT_GATEWAY_MODEL = "deepseek-v4-pro:cloud"
DEFAULT_TRACKED_FILES: list[str] = [
    "AGENTS.md",
    "TOOLS.md",
    "SOUL.md",
    "USER.md",
    "SAFETY_RULES.md",
    "IDENTITY.md",
    "HEARTBEAT.md",
    "MEMORY.md",
]
DEFAULT_NAMED_WORKSPACES: list[str] = []
DEFAULT_MIN_SECTION_CHARS = 400
DEFAULT_STALE_DAYS = 60
DEFAULT_CACHE_DIR = "~/.cache/bootstrap-doctor"


class ConfigError(Exception):
    """Raised when configuration is invalid or required paths are missing."""


@dataclass(frozen=True)
class Config:
    workspace_dir: Path
    cards_dir: Path
    gateway_url: str
    gateway_model: str
    soft_limit: int
    hard_limit: int
    tracked_files: tuple[str, ...]
    named_workspaces: tuple[str, ...]
    min_section_chars: int
    stale_days: int
    cache_dir: Path


# Helpers ------------------------------------------------------------------


def _expand(path_str: str) -> Path:
    """Expand ``~`` and resolve to an absolute Path."""
    return Path(os.path.expanduser(path_str)).resolve()


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _validate_string_value(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{label} must be a non-empty string, got {raw!r}")
    if raw.strip() != raw:
        raise ConfigError(f"{label} must not have leading or trailing whitespace")
    if _has_control_chars(raw):
        raise ConfigError(f"{label} must not contain control characters")
    return raw


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file is not valid TOML: {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file: {path}: {exc}") from exc


def _coerce_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        # bools are ints in Python, but a bool here is almost certainly a typo.
        raise ConfigError(f"{label} must be an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{label} must be an integer, got {value!r}") from exc
    raise ConfigError(f"{label} must be an integer, got {type(value).__name__}")


def _resolve_config_file_path(
    *, cli_config_file: str | None
) -> Path | None:
    """Resolve which config file to load, if any.

    CLI flag wins over env var. If neither is set, fall back to the default
    location at ``~/.config/bootstrap-doctor/config.toml``; that one is
    treated as optional (missing is fine).
    """
    if cli_config_file is not None:
        path = _expand(cli_config_file)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return path
    env_path = os.environ.get("BOOTSTRAP_DOCTOR_CONFIG")
    if env_path:
        path = _expand(env_path)
        if not path.exists():
            raise ConfigError(f"config file not found (from env): {path}")
        return path
    default = _expand("~/.config/bootstrap-doctor/config.toml")
    if default.exists():
        return default
    return None


def _layer_values(file_data: dict[str, Any]) -> dict[str, Any]:
    """Merge defaults, then config file, then env vars into a flat dict.

    CLI flags are NOT applied here; they get layered on top by the caller
    inside :func:`resolve_config` so that ``None`` flags fall through
    cleanly.
    """
    # Start with defaults.
    out: dict[str, Any] = {
        "workspace_dir": DEFAULT_WORKSPACE_DIR,
        "cards_dir": DEFAULT_CARDS_DIR,
        "gateway_url": DEFAULT_GATEWAY_URL,
        "gateway_model": DEFAULT_GATEWAY_MODEL,
        "soft_limit": DEFAULT_SOFT_LIMIT,
        "hard_limit": DEFAULT_HARD_LIMIT,
        "tracked_files": list(DEFAULT_TRACKED_FILES),
        "named_workspaces": list(DEFAULT_NAMED_WORKSPACES),
        "min_section_chars": DEFAULT_MIN_SECTION_CHARS,
        "stale_days": DEFAULT_STALE_DAYS,
        "cache_dir": DEFAULT_CACHE_DIR,
    }

    # Apply config file. Reject unknown top-level keys would be too strict
    # for v1; just pick the keys we care about.
    for key in (
        "workspace_dir",
        "cards_dir",
        "gateway_url",
        "gateway_model",
        "soft_limit",
        "hard_limit",
        "tracked_files",
        "named_workspaces",
    ):
        if key in file_data:
            out[key] = file_data[key]

    heuristics = file_data.get("heuristics")
    if isinstance(heuristics, dict):
        if "min_section_chars" in heuristics:
            out["min_section_chars"] = heuristics["min_section_chars"]
        if "stale_days" in heuristics:
            out["stale_days"] = heuristics["stale_days"]

    cache = file_data.get("cache")
    if isinstance(cache, dict) and "dir" in cache:
        out["cache_dir"] = cache["dir"]

    # Environment overrides.
    env_map = {
        "BOOTSTRAP_DOCTOR_WORKSPACE_DIR": "workspace_dir",
        "BOOTSTRAP_DOCTOR_CARDS_DIR": "cards_dir",
        "BOOTSTRAP_DOCTOR_GATEWAY_URL": "gateway_url",
        "BOOTSTRAP_DOCTOR_GATEWAY_MODEL": "gateway_model",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            out[cfg_key] = val

    soft_env = os.environ.get("BOOTSTRAP_DOCTOR_SOFT_LIMIT")
    if soft_env is not None:
        out["soft_limit"] = _coerce_int(soft_env, "BOOTSTRAP_DOCTOR_SOFT_LIMIT")
    hard_env = os.environ.get("BOOTSTRAP_DOCTOR_HARD_LIMIT")
    if hard_env is not None:
        out["hard_limit"] = _coerce_int(hard_env, "BOOTSTRAP_DOCTOR_HARD_LIMIT")

    return out


# Validation ---------------------------------------------------------------


def _validate_workspace_dir(raw: Any) -> Path:
    raw = _validate_string_value(raw, "workspace_dir")
    path = _expand(raw)
    if not path.exists():
        raise ConfigError(f"workspace_dir does not exist: {path}")
    if not path.is_dir():
        raise ConfigError(f"workspace_dir is not a directory: {path}")
    return path


def _validate_cards_dir(raw: Any, *, allow_missing: bool) -> Path:
    raw = _validate_string_value(raw, "cards_dir")
    path = _expand(raw)
    if path.exists():
        if not path.is_dir():
            raise ConfigError(f"cards_dir is not a directory: {path}")
        return path
    if allow_missing:
        return path
    raise ConfigError(f"cards_dir does not exist: {path}")


def _validate_limits(soft: Any, hard: Any) -> tuple[int, int]:
    soft_i = _coerce_int(soft, "soft_limit")
    hard_i = _coerce_int(hard, "hard_limit")
    if soft_i <= 0:
        raise ConfigError(f"soft_limit must be positive, got {soft_i}")
    if hard_i <= 0:
        raise ConfigError(f"hard_limit must be positive, got {hard_i}")
    if soft_i >= hard_i:
        raise ConfigError(
            f"soft_limit ({soft_i}) must be strictly less than hard_limit ({hard_i})"
        )
    if hard_i >= HARD_LIMIT_CEILING:
        raise ConfigError(
            f"hard_limit ({hard_i}) must be below the {HARD_LIMIT_CEILING} "
            f"char ceiling to leave headroom"
        )
    return soft_i, hard_i


def _validate_gateway_url(raw: Any) -> str:
    raw = _validate_string_value(raw, "gateway_url")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError(
            f"gateway_url must start with http:// or https://, got {raw!r}"
        )
    try:
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"gateway_url is not valid: {raw!r}") from exc
    if not parsed.netloc or not hostname:
        raise ConfigError(f"gateway_url must include a host, got {raw!r}")
    return raw


def _validate_gateway_model(raw: Any) -> str:
    return _validate_string_value(raw, "gateway_model")


def _validate_tracked_files(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(
            f"tracked_files must be a list, got {type(raw).__name__}"
        )
    if len(raw) == 0:
        raise ConfigError("tracked_files must not be empty")
    out: list[str] = []
    for entry in raw:
        entry = _validate_string_value(entry, "tracked_files entries")
        if "/" in entry or "\\" in entry:
            raise ConfigError(
                f"tracked_files entries must not contain path separators, got {entry!r}"
            )
        if not entry.endswith(".md"):
            raise ConfigError(
                f"tracked_files entries must end in '.md', got {entry!r}"
            )
        out.append(entry)
    return tuple(out)


def _validate_named_workspaces(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(
            f"named_workspaces must be a list, got {type(raw).__name__}"
        )
    out: list[str] = []
    for entry in raw:
        entry = _validate_string_value(entry, "named_workspaces entries")
        if "/" in entry or "\\" in entry:
            raise ConfigError(
                f"named_workspaces entries must not contain path separators, got {entry!r}"
            )
        out.append(entry)
    return tuple(out)


def _validate_positive(value: Any, label: str) -> int:
    n = _coerce_int(value, label)
    if n <= 0:
        raise ConfigError(f"{label} must be positive, got {n}")
    return n


def _validate_cache_dir(raw: Any) -> Path:
    """Resolve ``cache_dir`` without creating it.

    Does NOT create the directory: read-only verbs like ``status``
    must not have filesystem side effects. Lazy creation lives in
    :mod:`bootstrap_doctor.judge` at cache-write time (the underlying
    ``atomic_write_text`` does ``mkdir(parents=True, exist_ok=True)``
    on the parent right before each write).

    The only structural check here is: if the path already exists,
    it must be a directory.
    """
    raw = _validate_string_value(raw, "cache_dir")
    path = _expand(raw)
    if path.exists() and not path.is_dir():
        raise ConfigError(f"cache_dir is not a directory: {path}")
    return path


# Public API ---------------------------------------------------------------


def resolve_config(
    *,
    config_file: str | None = None,
    workspace_dir: str | None = None,
    cards_dir: str | None = None,
    gateway_url: str | None = None,
    gateway_model: str | None = None,
    soft_limit: int | None = None,
    hard_limit: int | None = None,
    allow_missing_cards: bool = False,
) -> Config:
    """Resolve config from defaults, file, env, and CLI flags.

    See module docstring for the layering rules and validation behavior.
    """
    cfg_path = _resolve_config_file_path(cli_config_file=config_file)
    file_data: dict[str, Any] = _load_toml(cfg_path) if cfg_path else {}

    merged = _layer_values(file_data)

    # Apply CLI flags on top.
    if workspace_dir is not None:
        merged["workspace_dir"] = workspace_dir
    if cards_dir is not None:
        merged["cards_dir"] = cards_dir
    if gateway_url is not None:
        merged["gateway_url"] = gateway_url
    if gateway_model is not None:
        merged["gateway_model"] = gateway_model
    if soft_limit is not None:
        merged["soft_limit"] = soft_limit
    if hard_limit is not None:
        merged["hard_limit"] = hard_limit

    # Validate and coerce.
    ws = _validate_workspace_dir(merged["workspace_dir"])
    cd = _validate_cards_dir(merged["cards_dir"], allow_missing=allow_missing_cards)
    gw_url = _validate_gateway_url(merged["gateway_url"])
    gw_model = _validate_gateway_model(merged["gateway_model"])
    soft_i, hard_i = _validate_limits(merged["soft_limit"], merged["hard_limit"])
    tracked = _validate_tracked_files(merged["tracked_files"])
    named = _validate_named_workspaces(merged["named_workspaces"])
    min_chars = _validate_positive(merged["min_section_chars"], "min_section_chars")
    stale = _validate_positive(merged["stale_days"], "stale_days")
    cache = _validate_cache_dir(merged["cache_dir"])

    return Config(
        workspace_dir=ws,
        cards_dir=cd,
        gateway_url=gw_url,
        gateway_model=gw_model,
        soft_limit=soft_i,
        hard_limit=hard_i,
        tracked_files=tracked,
        named_workspaces=named,
        min_section_chars=min_chars,
        stale_days=stale,
        cache_dir=cache,
    )
