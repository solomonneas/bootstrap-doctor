"""Tests for the argparse entrypoint in bootstrap_doctor.cli."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bootstrap_doctor import __version__
from bootstrap_doctor import cli as cli_mod
from bootstrap_doctor.judge import JudgeStats, Verdict
from bootstrap_doctor.paths import Config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    """Initialize a git repo at `repo` and commit any existing content."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=repo, check=True
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init", "--allow-empty"], cwd=repo, check=True
    )


def _write_config(
    tmp_path: Path,
    *,
    workspace: Path,
    cards: Path,
    soft: int = 100,
    hard: int = 200,
    tracked: tuple[str, ...] = ("AGENTS.md",),
) -> Path:
    """Write a config.toml for end-to-end CLI runs."""
    cache = tmp_path / "cache"
    cfg_path = tmp_path / "config.toml"
    tracked_list = ", ".join(f'"{n}"' for n in tracked)
    cfg_path.write_text(
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
soft_limit = {soft}
hard_limit = {hard}
tracked_files = [{tracked_list}]
named_workspaces = []

[cache]
dir = "{cache}"
"""
    )
    return cfg_path


def _mk_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create workspace and cards dirs under tmp_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cards = workspace / "memory" / "cards"
    cards.mkdir(parents=True)
    return workspace, cards


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_returns_argument_parser() -> None:
    import argparse

    p = cli_mod.build_parser()
    assert isinstance(p, argparse.ArgumentParser)


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    # argparse prints version to stdout in Python 3.4+
    out = captured.out + captured.err
    assert __version__ in out
    assert f"bootstrap-doctor {__version__}" in out


def test_no_subcommand_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_mod.main([])
    assert exc.value.code != 0


def test_unknown_subcommand_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["bogus-verb"])
    assert exc.value.code != 0


def test_status_flags_parse() -> None:
    parser = cli_mod.build_parser()
    args = parser.parse_args(["status", "--json"])
    assert args.verb == "status"
    assert args.json is True


def test_audit_flags_parse() -> None:
    parser = cli_mod.build_parser()
    args = parser.parse_args(
        ["audit", "--no-cache", "--max-input-chars", "12345", "--json"]
    )
    assert args.verb == "audit"
    assert args.no_cache is True
    assert args.max_input_chars == 12345
    assert args.json is True


def test_trim_flags_parse() -> None:
    parser = cli_mod.build_parser()
    args = parser.parse_args(
        ["trim", "--apply", "--force", "--no-cache", "--collision", "overwrite"]
    )
    assert args.verb == "trim"
    assert args.apply is True
    assert args.force is True
    assert args.no_cache is True
    assert args.collision == "overwrite"


def test_trim_collision_default_is_skip() -> None:
    parser = cli_mod.build_parser()
    args = parser.parse_args(["trim"])
    assert args.collision == "skip"


def test_trim_collision_rename_parses() -> None:
    parser = cli_mod.build_parser()
    args = parser.parse_args(["trim", "--collision", "rename"])
    assert args.collision == "rename"


def test_trim_collision_invalid_rejected() -> None:
    parser = cli_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["trim", "--collision", "smash"])


def test_common_flags_propagate(tmp_path: Path) -> None:
    parser = cli_mod.build_parser()
    args = parser.parse_args(
        [
            "status",
            "--workspace-dir",
            str(tmp_path),
            "--gateway-url",
            "http://x:1",
            "--gateway-model",
            "m",
            "--soft-limit",
            "1000",
            "--hard-limit",
            "1100",
        ]
    )
    assert args.workspace_dir == str(tmp_path)
    assert args.gateway_url == "http://x:1"
    assert args.gateway_model == "m"
    assert args.soft_limit == 1000
    assert args.hard_limit == 1100


# ---------------------------------------------------------------------------
# Dispatch: each verb routes to the right function
# ---------------------------------------------------------------------------


def test_status_dispatches_to_status_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("x" * 10)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    seen: dict[str, Any] = {}

    def fake_run(cfg: Config, *, as_json: bool = False) -> int:
        seen["cfg"] = cfg
        seen["as_json"] = as_json
        return 0

    monkeypatch.setattr(cli_mod, "_status_run", fake_run, raising=False)
    # status.run is imported lazily, so patch via the module attribute.
    from bootstrap_doctor import status as status_mod

    monkeypatch.setattr(status_mod, "run", fake_run)

    code = cli_mod.main(["status", "--config", str(cfg_path), "--json"])
    assert code == 0
    assert seen["as_json"] is True
    assert isinstance(seen["cfg"], Config)


def test_audit_dispatches_through_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "# Preamble\n\n## Big Section\n\n" + ("a" * 500) + "\n"
    (workspace / "AGENTS.md").write_text(body)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="move",
                    topic="big chunk to move",
                    category="session-log",
                    tags=("stale",),
                    hook="Move this to a card.",
                    reasoning="historical content",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["audit", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    # Move decision => operator should review => exit 1
    assert code == 1
    assert "move" in captured.out.lower() or "Big Section" in captured.out


def test_audit_no_candidates_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    # Empty bootstrap file -> no sections -> no candidates.
    (workspace / "AGENTS.md").write_text("")
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    code = cli_mod.main(["audit", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "no candidates" in captured.out.lower()


def test_audit_all_keep_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Section A\n\n" + ("x" * 500) + "\n"
    (workspace / "AGENTS.md").write_text(body)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="keep",
                    topic="",
                    category="",
                    tags=(),
                    hook="",
                    reasoning="active rule",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["audit", "--config", str(cfg_path)])
    assert code == 0


def test_audit_failures_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Section A\n\n" + ("x" * 500) + "\n"
    (workspace / "AGENTS.md").write_text(body)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="unsure",
                    topic="",
                    category="",
                    tags=(),
                    hook="",
                    reasoning="judge_error: HTTP 500",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(failures=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["audit", "--config", str(cfg_path)])
    assert code == 2


def test_audit_json_output_is_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Section A\n\n" + ("x" * 500) + "\n"
    (workspace / "AGENTS.md").write_text(body)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="move",
                    topic="topic here",
                    category="session-log",
                    tags=("a",),
                    hook="hook here",
                    reasoning="reason",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["audit", "--config", str(cfg_path), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == 1
    entry = data[0]
    assert entry["decision"] == "move"
    assert entry["topic"] == "topic here"


def test_trim_dry_run_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Big Section\n\n" + ("x" * 500) + "\n"
    bootstrap = workspace / "AGENTS.md"
    bootstrap.write_text(body)
    _git_init(workspace)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="move",
                    topic="big section",
                    category="session-log",
                    tags=(),
                    hook="moved.",
                    reasoning="historical",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["trim", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "NEW CARD" in captured.out
    assert "DRY RUN" in captured.out
    # Nothing was actually written.
    assert not any(cards.iterdir())


def test_trim_apply_writes_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Big Section\n\n" + ("x" * 500) + "\n"
    bootstrap = workspace / "AGENTS.md"
    bootstrap.write_text(body)
    _git_init(workspace)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="move",
                    topic="big section",
                    category="session-log",
                    tags=(),
                    hook="moved.",
                    reasoning="historical",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["trim", "--config", str(cfg_path), "--apply"])
    captured = capsys.readouterr()
    assert code == 0
    written = list(cards.iterdir())
    assert len(written) == 1
    # Bootstrap was modified.
    new_body = bootstrap.read_text()
    assert "See [big section]" in new_body
    assert "x" * 500 not in new_body
    assert "applied" in captured.out.lower()


def test_trim_apply_force_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Big Section\n\n" + ("x" * 500) + "\n"
    bootstrap = workspace / "AGENTS.md"
    bootstrap.write_text(body)
    _git_init(workspace)
    # Make it dirty.
    bootstrap.write_text(body + "\n## Dirty Edit\n\nhello\n")
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod
    from bootstrap_doctor import trim as trim_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            if c.section.heading_text == "Big Section":
                verdicts.append(
                    Verdict(
                        section=c.section,
                        decision="move",
                        topic="big section",
                        category="session-log",
                        tags=(),
                        hook="moved.",
                        reasoning="historical",
                        source="gateway",
                        body_sha="x" * 64,
                    )
                )
            else:
                verdicts.append(
                    Verdict(
                        section=c.section,
                        decision="keep",
                        topic="",
                        category="",
                        tags=(),
                        hook="",
                        reasoning="keep",
                        source="gateway",
                        body_sha="y" * 64,
                    )
                )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    seen: dict[str, Any] = {}
    real_apply_plan = trim_mod.apply_plan

    def spy_apply(actions, cfg, *, apply=False, force=False):
        seen["apply"] = apply
        seen["force"] = force
        return real_apply_plan(actions, cfg, apply=apply, force=force)

    monkeypatch.setattr(trim_mod, "apply_plan", spy_apply)

    code = cli_mod.main(
        ["trim", "--config", str(cfg_path), "--apply", "--force"]
    )
    assert seen["apply"] is True
    assert seen["force"] is True
    assert code == 0


def test_trim_apply_dirty_workspace_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Big Section\n\n" + ("x" * 500) + "\n"
    bootstrap = workspace / "AGENTS.md"
    bootstrap.write_text(body)
    _git_init(workspace)
    # Make the workspace dirty AFTER commit.
    bootstrap.write_text(body + "\nedit\n")
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="move",
                    topic="big section",
                    category="session-log",
                    tags=(),
                    hook="moved.",
                    reasoning="historical",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["trim", "--config", str(cfg_path), "--apply"])
    captured = capsys.readouterr()
    assert code == 2
    assert "bootstrap-doctor:" in captured.err


def test_trim_aborts_when_audit_has_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Even with one move verdict, a non-zero failures count blocks apply.

    Gateway failures during the audit mean the verdict set is incomplete,
    so mutating bootstrap files based on it could move the wrong content.
    Exit 2 and do not touch disk.
    """
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Big Section\n\n" + ("x" * 500) + "\n"
    bootstrap = workspace / "AGENTS.md"
    bootstrap.write_text(body)
    _git_init(workspace)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)
    original_bs = bootstrap.read_text()

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = [
            Verdict(
                section=candidates[0].section,
                decision="move",
                topic="big section",
                category="session-log",
                tags=(),
                hook="moved.",
                reasoning="historical",
                source="gateway",
                body_sha="x" * 64,
            )
        ]
        return verdicts, JudgeStats(requests_made=1, failures=2)

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["trim", "--config", str(cfg_path), "--apply"])
    captured = capsys.readouterr()
    assert code == 2
    assert "refusing to trim" in captured.err.lower()
    assert "2" in captured.err  # mentions the failure count
    # No bootstrap mutation.
    assert bootstrap.read_text() == original_bs
    assert not any(cards.iterdir())


def test_trim_aborts_with_failures_even_under_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--force is for dirty-git, not for ignoring judge failures."""
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Big Section\n\n" + ("x" * 500) + "\n"
    bootstrap = workspace / "AGENTS.md"
    bootstrap.write_text(body)
    _git_init(workspace)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = [
            Verdict(
                section=candidates[0].section,
                decision="move",
                topic="big section",
                category="session-log",
                tags=(),
                hook="moved.",
                reasoning="historical",
                source="gateway",
                body_sha="x" * 64,
            )
        ]
        return verdicts, JudgeStats(failures=1)

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(
        ["trim", "--config", str(cfg_path), "--apply", "--force"]
    )
    assert code == 2


def test_trim_card_write_error_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Big Section\n\n" + ("x" * 500) + "\n"
    bootstrap = workspace / "AGENTS.md"
    bootstrap.write_text(body)
    _git_init(workspace)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod
    from bootstrap_doctor import trim as trim_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = [
            Verdict(
                section=candidates[0].section,
                decision="move",
                topic="big section",
                category="session-log",
                tags=(),
                hook="moved.",
                reasoning="historical",
                source="gateway",
                body_sha="x" * 64,
            )
        ]
        return verdicts, JudgeStats(requests_made=1)

    def fail_card_write(*_args: Any, **_kwargs: Any):
        target = cards / "big-section.md"
        raise trim_mod.CardWriteError(
            "card write failed at big-section.md",
            failed_card=target,
            cards_written=(),
        )

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)
    monkeypatch.setattr(trim_mod, "apply_plan", fail_card_write)

    code = cli_mod.main(["trim", "--config", str(cfg_path), "--apply"])
    captured = capsys.readouterr()
    assert code == 2
    assert "card write failed" in captured.err
    assert "unexpected error" not in captured.err


def test_trim_no_actions_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    body = "## Section A\n\n" + ("x" * 500) + "\n"
    (workspace / "AGENTS.md").write_text(body)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts = []
        for c in candidates:
            verdicts.append(
                Verdict(
                    section=c.section,
                    decision="keep",
                    topic="",
                    category="",
                    tags=(),
                    hook="",
                    reasoning="active",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(["trim", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "no actions" in captured.out.lower()


# ---------------------------------------------------------------------------
# End-to-end: status with real file
# ---------------------------------------------------------------------------


def test_status_e2e_soft_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("x" * 150)  # soft (100 < 150 < 200)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    code = cli_mod.main(["status", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "SOFT" in captured.out


def test_status_e2e_all_ok_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("x" * 10)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    code = cli_mod.main(["status", "--config", str(cfg_path)])
    assert code == 0


def test_status_does_not_create_cache_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-only status verb must not create ~/.cache/bootstrap-doctor
    (or any cache dir) as a side effect.

    Lazy creation lives in judge.py and only fires on cache writes.
    """
    workspace, cards = _mk_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("x" * 10)
    # Point HOME at tmp_path so the default cache path is hermetic.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BOOTSTRAP_DOCTOR_CONFIG", raising=False)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    # Point the cache to a not-yet-existing path AND run status (which
    # is read-only). Cache dir must not exist afterwards regardless of
    # whether status returned 0 or 1.
    expected_cache = tmp_path / "would-be-cache"
    cfg_path.write_text(
        cfg_path.read_text().replace(
            f'dir = "{tmp_path / "cache"}"',
            f'dir = "{expected_cache}"',
        )
    )
    assert not expected_cache.exists()
    cli_mod.main(["status", "--config", str(cfg_path)])
    assert not expected_cache.exists()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_workspace_dir_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Workspace missing and no override given -> exit 2 with the hint."""
    # Make sure no env vars or default config interferes.
    monkeypatch.delenv("BOOTSTRAP_DOCTOR_CONFIG", raising=False)
    monkeypatch.delenv("BOOTSTRAP_DOCTOR_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("BOOTSTRAP_DOCTOR_CARDS_DIR", raising=False)
    # Point HOME at tmp_path so the default ~/.openclaw/workspace lookup
    # resolves to a missing path and there is no default config file.
    monkeypatch.setenv("HOME", str(tmp_path))

    # No --workspace-dir override given; relies on the default which points
    # at the (nonexistent) ~/.openclaw/workspace under our fake HOME.
    code = cli_mod.main(["status"])
    captured = capsys.readouterr()
    assert code == 2
    assert "workspace" in captured.err.lower()
    # The hint should mention how to fix it.
    assert (
        "--workspace-dir" in captured.err
        or "BOOTSTRAP_DOCTOR_WORKSPACE_DIR" in captured.err
    )


def test_missing_workspace_dir_with_explicit_flag_no_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the user explicitly passed --workspace-dir, skip the hint."""
    monkeypatch.delenv("BOOTSTRAP_DOCTOR_CONFIG", raising=False)
    monkeypatch.delenv("BOOTSTRAP_DOCTOR_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    nonexistent = tmp_path / "does-not-exist"
    code = cli_mod.main(["status", "--workspace-dir", str(nonexistent)])
    captured = capsys.readouterr()
    assert code == 2
    # No hint because the user already supplied --workspace-dir.
    assert "hint:" not in captured.err


def test_invalid_config_file_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad_cfg = tmp_path / "bad.toml"
    bad_cfg.write_text("this is = not = valid = toml")
    code = cli_mod.main(["status", "--config", str(bad_cfg)])
    captured = capsys.readouterr()
    assert code == 2
    assert "bootstrap-doctor:" in captured.err


def test_unsafe_named_workspace_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A named workspace that resolves outside the workspace base must
    surface as exit 2 with a clear error, not be silently skipped.

    A symlink under workspace_dir pointing OUTSIDE is the canonical
    case (path traversal / misconfiguration). The user should know.
    """
    workspace, cards = _mk_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("## A\nbody\n")
    # Target lives outside the workspace; the symlink lives inside it.
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (workspace / "workspace-evil").symlink_to(outside)

    cache = tmp_path / "cache"
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(
        f'''
workspace_dir = "{workspace}"
cards_dir = "{cards}"
soft_limit = 100
hard_limit = 200
tracked_files = ["AGENTS.md"]
named_workspaces = ["workspace-evil"]

[cache]
dir = "{cache}"
'''
    )

    code = cli_mod.main(["audit", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 2
    err = captured.err.lower()
    assert "unsafe" in err or "outside" in err or "escape" in err
    assert "workspace-evil" in captured.err


def test_unexpected_exception_returns_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("x" * 10)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    from bootstrap_doctor import status as status_mod

    def boom(cfg: Config, *, as_json: bool = False) -> int:
        raise RuntimeError("kaboom from status.run")

    monkeypatch.setattr(status_mod, "run", boom)

    code = cli_mod.main(["status", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 3
    assert "bootstrap-doctor:" in captured.err
    # No traceback by default.
    assert "Traceback" not in captured.err


def test_trace_env_var_shows_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, cards = _mk_workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("x" * 10)
    cfg_path = _write_config(tmp_path, workspace=workspace, cards=cards)

    monkeypatch.setenv("BOOTSTRAP_DOCTOR_TRACE", "1")

    from bootstrap_doctor import status as status_mod

    def boom(cfg: Config, *, as_json: bool = False) -> int:
        raise RuntimeError("traced explosion")

    monkeypatch.setattr(status_mod, "run", boom)

    code = cli_mod.main(["status", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 3
    assert "Traceback" in captured.err or "traced explosion" in captured.err
