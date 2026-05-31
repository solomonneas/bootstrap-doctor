"""CLI-level trim integration coverage with a copied workspace."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bootstrap_doctor import cli as cli_mod
from bootstrap_doctor.judge import JudgeStats, Verdict
from bootstrap_doctor.paths import Config


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init", "--allow-empty"],
        cwd=repo,
        check=True,
    )


def _write_config(tmp_path: Path, workspace: Path, cards: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""
workspace_dir = "{workspace}"
cards_dir = "{cards}"
soft_limit = 100
hard_limit = 1000
tracked_files = ["AGENTS.md"]
named_workspaces = []

[cache]
dir = "{tmp_path / "cache"}"
"""
    )
    return cfg_path


def test_trim_apply_copied_workspace_idempotency_and_dirty_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace-copy"
    cards = workspace / "memory" / "cards"
    cards.mkdir(parents=True)
    bootstrap = workspace / "AGENTS.md"
    original_body = (
        "## Move Me\n\n"
        + ("historical setup detail that should live in a card\n" * 20)
        + "\n## Keep Me\n\nsmall active rule\n"
    )
    bootstrap.write_text(original_body)
    cfg_path = _write_config(tmp_path, workspace, cards)
    _git_init(workspace)

    from bootstrap_doctor import judge as judge_mod

    def fake_judge_all(
        candidates: list, cfg: Config, **kwargs: Any
    ) -> tuple[list[Verdict], JudgeStats]:
        verdicts: list[Verdict] = []
        for candidate in candidates:
            verdicts.append(
                Verdict(
                    section=candidate.section,
                    decision="move",
                    topic="historical setup detail",
                    category="session-log",
                    tags=("setup",),
                    hook="Moved historical setup detail.",
                    reasoning="historical reference detail",
                    source="gateway",
                    body_sha="x" * 64,
                )
            )
        return verdicts, JudgeStats(requests_made=len(verdicts))

    monkeypatch.setattr(judge_mod, "judge_all", fake_judge_all)

    code = cli_mod.main(
        ["trim", "--config", str(cfg_path), "--apply", "--collision", "overwrite"]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err

    card = cards / "historical-setup-detail.md"
    assert card.exists()
    assert "historical setup detail that should live in a card" in card.read_text()
    trimmed = bootstrap.read_text()
    assert "- See [historical setup detail]" in trimmed
    assert "memory/cards/historical-setup-detail.md" in trimmed
    assert "## Keep Me\n\nsmall active rule" in trimmed
    assert len(trimmed) < len(original_body)

    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "trim"], cwd=workspace, check=True)

    code = cli_mod.main(["trim", "--config", str(cfg_path)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "no candidates" in captured.out.lower()

    bootstrap.write_text(original_body)
    code = cli_mod.main(
        ["trim", "--config", str(cfg_path), "--apply", "--collision", "overwrite"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "dirty workspace" in captured.err.lower()
