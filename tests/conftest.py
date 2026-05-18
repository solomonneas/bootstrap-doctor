"""Test fixtures: hermetic tmp_path-based workspace + cards dirs."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Empty workspace dir at tmp_path/workspace."""
    d = tmp_path / "workspace"
    d.mkdir()
    return d


@pytest.fixture
def cards_dir(workspace_dir: Path) -> Path:
    """Empty cards dir at workspace_dir/memory/cards."""
    d = workspace_dir / "memory" / "cards"
    d.mkdir(parents=True)
    return d


def write_bootstrap_file(workspace_dir: Path, name: str, body: str) -> Path:
    """Write a bootstrap file at workspace_dir/<name>."""
    path = workspace_dir / name
    path.write_text(body)
    return path
