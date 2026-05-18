"""Tests for config resolution and layering (defaults, config file, env, CLI flags)."""
from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap_doctor.paths import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CARDS_DIR,
    DEFAULT_GATEWAY_MODEL,
    DEFAULT_GATEWAY_URL,
    DEFAULT_HARD_LIMIT,
    DEFAULT_MIN_SECTION_CHARS,
    DEFAULT_NAMED_WORKSPACES,
    DEFAULT_SOFT_LIMIT,
    DEFAULT_STALE_DAYS,
    DEFAULT_TRACKED_FILES,
    DEFAULT_WORKSPACE_DIR,
    Config,
    ConfigError,
    resolve_config,
)


# Helpers -----------------------------------------------------------------


def _write_toml(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BOOTSTRAP_DOCTOR_CONFIG",
        "BOOTSTRAP_DOCTOR_WORKSPACE_DIR",
        "BOOTSTRAP_DOCTOR_CARDS_DIR",
        "BOOTSTRAP_DOCTOR_GATEWAY_URL",
        "BOOTSTRAP_DOCTOR_GATEWAY_MODEL",
        "BOOTSTRAP_DOCTOR_SOFT_LIMIT",
        "BOOTSTRAP_DOCTOR_HARD_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    d = tmp_path / "workspace"
    d.mkdir()
    return d


@pytest.fixture
def cards(workspace: Path) -> Path:
    d = workspace / "memory" / "cards"
    d.mkdir(parents=True)
    return d


# Defaults ----------------------------------------------------------------


def test_default_constants_match_spec():
    assert DEFAULT_WORKSPACE_DIR == "~/.openclaw/workspace"
    assert DEFAULT_CARDS_DIR == "~/.openclaw/workspace/memory/cards"
    assert DEFAULT_GATEWAY_URL == "http://localhost:18789"
    assert DEFAULT_GATEWAY_MODEL == "openai-codex/gpt-5.5"
    assert DEFAULT_SOFT_LIMIT == 10000
    assert DEFAULT_HARD_LIMIT == 11500
    assert DEFAULT_MIN_SECTION_CHARS == 400
    assert DEFAULT_STALE_DAYS == 60
    assert DEFAULT_CACHE_DIR == "~/.cache/bootstrap-doctor"
    assert "AGENTS.md" in DEFAULT_TRACKED_FILES
    assert "MEMORY.md" in DEFAULT_TRACKED_FILES
    assert DEFAULT_NAMED_WORKSPACES == []


def test_defaults_applied_when_no_config_no_env_no_flags(
    workspace: Path, cards: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch)
    cache_dir = tmp_path / "cache"
    # Point flags at our tmp workspace + cards so we don't depend on the real
    # filesystem. Everything else should come from defaults.
    cfg = resolve_config(
        workspace_dir=str(workspace),
        cards_dir=str(cards),
    )
    assert isinstance(cfg, Config)
    assert cfg.gateway_url == DEFAULT_GATEWAY_URL
    assert cfg.gateway_model == DEFAULT_GATEWAY_MODEL
    assert cfg.soft_limit == DEFAULT_SOFT_LIMIT
    assert cfg.hard_limit == DEFAULT_HARD_LIMIT
    assert cfg.tracked_files == tuple(DEFAULT_TRACKED_FILES)
    assert cfg.named_workspaces == tuple(DEFAULT_NAMED_WORKSPACES)
    assert cfg.min_section_chars == DEFAULT_MIN_SECTION_CHARS
    assert cfg.stale_days == DEFAULT_STALE_DAYS
    # cache_dir resolves to a Path with default location
    assert isinstance(cfg.cache_dir, Path)


# Config file layering ----------------------------------------------------


def test_config_file_overrides_defaults(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "config.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
gateway_url = "http://example.test:9000"
gateway_model = "openai-codex/gpt-9"
soft_limit = 5000
hard_limit = 8000
tracked_files = ["A.md", "B.md"]
named_workspaces = ["workspace-claude"]

[heuristics]
min_section_chars = 200
stale_days = 30

[cache]
dir = "{tmp_path / "custom-cache"}"
""",
    )
    cfg = resolve_config(config_file=str(cfg_path))
    assert cfg.workspace_dir == workspace.resolve()
    assert cfg.cards_dir == cards.resolve()
    assert cfg.gateway_url == "http://example.test:9000"
    assert cfg.gateway_model == "openai-codex/gpt-9"
    assert cfg.soft_limit == 5000
    assert cfg.hard_limit == 8000
    assert cfg.tracked_files == ("A.md", "B.md")
    assert cfg.named_workspaces == ("workspace-claude",)
    assert cfg.min_section_chars == 200
    assert cfg.stale_days == 30
    assert cfg.cache_dir == (tmp_path / "custom-cache").resolve()
    assert cfg.cache_dir.exists()  # auto-created


def test_config_file_via_env_var(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "via-env.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
soft_limit = 1234
hard_limit = 5678
""",
    )
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_CONFIG", str(cfg_path))
    cfg = resolve_config()
    assert cfg.soft_limit == 1234
    assert cfg.hard_limit == 5678


def test_env_vars_override_config_file(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    other_workspace = tmp_path / "other-ws"
    other_workspace.mkdir()
    other_cards = other_workspace / "cards"
    other_cards.mkdir()

    cfg_path = tmp_path / "config.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
gateway_url = "http://from-file:1111"
gateway_model = "from-file/model"
soft_limit = 5000
hard_limit = 8000
""",
    )

    monkeypatch.setenv("BOOTSTRAP_DOCTOR_CONFIG", str(cfg_path))
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_WORKSPACE_DIR", str(other_workspace))
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_CARDS_DIR", str(other_cards))
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_GATEWAY_URL", "http://from-env:2222")
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_GATEWAY_MODEL", "from-env/model")
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_SOFT_LIMIT", "6000")
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_HARD_LIMIT", "9000")

    cfg = resolve_config()
    assert cfg.workspace_dir == other_workspace.resolve()
    assert cfg.cards_dir == other_cards.resolve()
    assert cfg.gateway_url == "http://from-env:2222"
    assert cfg.gateway_model == "from-env/model"
    assert cfg.soft_limit == 6000
    assert cfg.hard_limit == 9000


def test_cli_flags_override_env_vars(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cli_workspace = tmp_path / "cli-ws"
    cli_workspace.mkdir()
    cli_cards = cli_workspace / "cards"
    cli_cards.mkdir()

    monkeypatch.setenv("BOOTSTRAP_DOCTOR_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_CARDS_DIR", str(cards))
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_GATEWAY_URL", "http://from-env:2222")
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_GATEWAY_MODEL", "from-env/model")
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_SOFT_LIMIT", "6000")
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_HARD_LIMIT", "9000")

    cfg = resolve_config(
        workspace_dir=str(cli_workspace),
        cards_dir=str(cli_cards),
        gateway_url="http://from-cli:3333",
        gateway_model="from-cli/model",
        soft_limit=7000,
        hard_limit=10000,
    )
    assert cfg.workspace_dir == cli_workspace.resolve()
    assert cfg.cards_dir == cli_cards.resolve()
    assert cfg.gateway_url == "http://from-cli:3333"
    assert cfg.gateway_model == "from-cli/model"
    assert cfg.soft_limit == 7000
    assert cfg.hard_limit == 10000


def test_cli_flag_config_file_beats_env_config_file(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    env_cfg = tmp_path / "env.toml"
    _write_toml(
        env_cfg,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
soft_limit = 100
hard_limit = 200
""",
    )
    cli_cfg = tmp_path / "cli.toml"
    _write_toml(
        cli_cfg,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
soft_limit = 300
hard_limit = 400
""",
    )
    monkeypatch.setenv("BOOTSTRAP_DOCTOR_CONFIG", str(env_cfg))
    cfg = resolve_config(config_file=str(cli_cfg))
    assert cfg.soft_limit == 300
    assert cfg.hard_limit == 400


# Tilde expansion ---------------------------------------------------------


def test_tilde_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    cd = tmp_path / "cards"
    cd.mkdir()
    cfg = resolve_config(workspace_dir="~/ws", cards_dir="~/cards")
    assert cfg.workspace_dir == ws.resolve()
    assert cfg.cards_dir == cd.resolve()


def test_tilde_expansion_in_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    cd = tmp_path / "cards"
    cd.mkdir()
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        """
workspace_dir = "~/ws"
cards_dir = "~/cards"
""",
    )
    cfg = resolve_config(config_file=str(cfg_path))
    assert cfg.workspace_dir == ws.resolve()
    assert cfg.cards_dir == cd.resolve()


# allow_missing_cards -----------------------------------------------------


def test_allow_missing_cards_passes_if_parent_exists(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    # workspace/memory exists (parent), but workspace/memory/cards does not.
    (workspace / "memory").mkdir()
    missing_cards = workspace / "memory" / "cards"
    cfg = resolve_config(
        workspace_dir=str(workspace),
        cards_dir=str(missing_cards),
        allow_missing_cards=True,
    )
    assert cfg.cards_dir == missing_cards.resolve()


def test_allow_missing_cards_fails_if_parent_missing(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    nowhere = workspace / "deep" / "nope" / "cards"
    with pytest.raises(ConfigError) as exc:
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(nowhere),
            allow_missing_cards=True,
        )
    assert "cards" in str(exc.value).lower()


def test_missing_cards_without_flag_raises(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    (workspace / "memory").mkdir()
    missing_cards = workspace / "memory" / "cards"
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(missing_cards),
        )


# Validation: workspace ---------------------------------------------------


def test_missing_workspace_dir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    nope = tmp_path / "nowhere"
    with pytest.raises(ConfigError) as exc:
        resolve_config(workspace_dir=str(nope))
    assert "workspace" in str(exc.value).lower()


def test_workspace_dir_is_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    f = tmp_path / "afile"
    f.write_text("hi")
    with pytest.raises(ConfigError):
        resolve_config(workspace_dir=str(f))


def test_allow_missing_cards_does_not_relax_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    nope = tmp_path / "nowhere"
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(nope),
            cards_dir=str(tmp_path / "cards"),
            allow_missing_cards=True,
        )


# Validation: limits ------------------------------------------------------


def test_soft_ge_hard_raises(
    workspace: Path, cards: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            soft_limit=8000,
            hard_limit=8000,
        )
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            soft_limit=9000,
            hard_limit=8000,
        )


def test_hard_ge_12000_raises(
    workspace: Path, cards: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            soft_limit=10000,
            hard_limit=12000,
        )
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            soft_limit=10000,
            hard_limit=15000,
        )


def test_negative_or_zero_limits_raise(
    workspace: Path, cards: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            soft_limit=-1,
            hard_limit=8000,
        )
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            soft_limit=0,
            hard_limit=8000,
        )
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            soft_limit=100,
            hard_limit=0,
        )


def test_min_section_chars_must_be_positive(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"

[heuristics]
min_section_chars = 0
""",
    )
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


def test_stale_days_must_be_positive(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"

[heuristics]
stale_days = -1
""",
    )
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


# Validation: gateway URL -------------------------------------------------


def test_invalid_gateway_url_scheme_raises(
    workspace: Path, cards: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            gateway_url="localhost:18789",
        )
    with pytest.raises(ConfigError):
        resolve_config(
            workspace_dir=str(workspace),
            cards_dir=str(cards),
            gateway_url="ftp://example.test",
        )


def test_https_gateway_url_ok(
    workspace: Path, cards: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg = resolve_config(
        workspace_dir=str(workspace),
        cards_dir=str(cards),
        gateway_url="https://gateway.example.test/",
    )
    assert cfg.gateway_url == "https://gateway.example.test/"


# Validation: tracked_files -----------------------------------------------


def test_empty_tracked_files_raises(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
tracked_files = []
""",
    )
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


def test_tracked_file_without_md_raises(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
tracked_files = ["AGENTS.txt"]
""",
    )
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


def test_tracked_file_with_slash_raises(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
tracked_files = ["sub/AGENTS.md"]
""",
    )
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


# Validation: named_workspaces -------------------------------------------


def test_named_workspaces_with_slash_raises(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
named_workspaces = ["workspace-claude", "bad/name"]
""",
    )
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


def test_named_workspaces_empty_string_raises(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
named_workspaces = [""]
""",
    )
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


# cache_dir auto-create ---------------------------------------------------


def test_cache_dir_auto_created(
    workspace: Path,
    cards: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    cache = tmp_path / "fresh-cache"
    assert not cache.exists()
    cfg_path = tmp_path / "cfg.toml"
    _write_toml(
        cfg_path,
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"

[cache]
dir = "{cache}"
""",
    )
    cfg = resolve_config(config_file=str(cfg_path))
    assert cfg.cache_dir.exists()
    assert cfg.cache_dir.is_dir()
    assert isinstance(cfg.cache_dir, Path)


def test_cache_dir_default_when_not_set(
    workspace: Path,
    cards: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    # Point HOME at tmp_path so the default ~/.cache/bootstrap-doctor is hermetic.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = resolve_config(
        workspace_dir=str(workspace),
        cards_dir=str(cards),
    )
    assert cfg.cache_dir == (tmp_path / ".cache" / "bootstrap-doctor").resolve()
    assert cfg.cache_dir.exists()


# Missing config file -----------------------------------------------------


def test_missing_explicit_config_file_raises(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    bogus = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as exc:
        resolve_config(
            config_file=str(bogus),
            workspace_dir=str(workspace),
            cards_dir=str(cards),
        )
    assert "config" in str(exc.value).lower()


def test_malformed_toml_raises(
    workspace: Path, cards: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "broken.toml"
    cfg_path.write_text("this is = not valid = toml")
    with pytest.raises(ConfigError):
        resolve_config(config_file=str(cfg_path))


# Config dataclass is frozen ---------------------------------------------


def test_config_is_frozen(
    workspace: Path, cards: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    cfg = resolve_config(
        workspace_dir=str(workspace),
        cards_dir=str(cards),
    )
    with pytest.raises(Exception):
        cfg.soft_limit = 1  # type: ignore[misc]
