"""Tests for safety primitives: atomic writes, path guard, slugify, git-clean."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bootstrap_doctor.safety import (
    DirtyWorkspaceError,
    UnsafeTargetError,
    assert_git_clean,
    assert_git_clean_or_force,
    atomic_write_text,
    ensure_within,
    resolve_card_target,
    slugify,
)

# --- atomic_write_text -------------------------------------------------------


def test_atomic_write_text_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello world\n")
    assert target.read_text() == "hello world\n"


def test_atomic_write_text_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "out.txt"
    atomic_write_text(target, "data")
    assert target.read_text() == "data"
    assert target.parent.is_dir()


def test_atomic_write_text_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old contents")
    atomic_write_text(target, "new contents")
    assert target.read_text() == "new contents"


def test_atomic_write_text_preserves_existing_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old contents")
    os.chmod(target, 0o640)

    atomic_write_text(target, "new contents")

    assert target.read_text() == "new contents"
    assert target.stat().st_mode & 0o777 == 0o640


def test_atomic_write_text_cleans_up_tempfile_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"

    real_replace = os.replace

    def boom(*_a, **_kw):
        raise RuntimeError("simulated failure")

    with patch("bootstrap_doctor.safety.os.replace", side_effect=boom):
        with pytest.raises(RuntimeError, match="simulated failure"):
            atomic_write_text(target, "payload")

    # No leftover .tmp tempfiles in the target dir.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    # Sanity: real os.replace is still callable (we did not stub the module-level binding).
    assert real_replace is os.replace


def test_atomic_write_text_no_partial_file_on_crash(tmp_path: Path) -> None:
    """A pre-existing target file must remain intact if the new write blows up."""
    target = tmp_path / "out.txt"
    target.write_text("original")

    with patch("bootstrap_doctor.safety.os.replace", side_effect=OSError("fs full")):
        with pytest.raises(OSError):
            atomic_write_text(target, "new")

    assert target.read_text() == "original"


# --- ensure_within ----------------------------------------------------------


def test_ensure_within_allows_path_inside_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    candidate = base / "child.md"
    result = ensure_within(base, candidate)
    assert result == candidate.resolve()


def test_ensure_within_allows_nonexistent_path_inside_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    candidate = base / "not_yet" / "deeper.md"
    # Should not raise: structurally inside base even though missing.
    result = ensure_within(base, candidate)
    assert str(result).startswith(str(base.resolve()))


def test_ensure_within_rejects_parent_traversal(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    candidate = base / ".." / "outside" / "evil.md"
    with pytest.raises(UnsafeTargetError):
        ensure_within(base, candidate)


def test_ensure_within_rejects_absolute_path_outside_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(UnsafeTargetError):
        ensure_within(base, Path("/etc/passwd"))


# --- resolve_card_target ----------------------------------------------------


def test_resolve_card_target_simple_slug(cards_dir: Path) -> None:
    result = resolve_card_target(cards_dir, "my-card")
    assert result == (cards_dir / "my-card.md").resolve()


def test_resolve_card_target_already_md_no_double_extension(cards_dir: Path) -> None:
    result = resolve_card_target(cards_dir, "my-card.md")
    assert result == (cards_dir / "my-card.md").resolve()
    assert not str(result).endswith(".md.md")


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "\t"],
)
def test_resolve_card_target_rejects_empty(cards_dir: Path, bad: str) -> None:
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(cards_dir, bad)


@pytest.mark.parametrize(
    "bad",
    ["../foo", "foo/bar", "foo\\bar", "/etc/passwd"],
)
def test_resolve_card_target_rejects_separators_and_traversal(
    cards_dir: Path, bad: str
) -> None:
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(cards_dir, bad)


@pytest.mark.parametrize(
    "bad",
    [".", "..", ".hidden", ".secret.md"],
)
def test_resolve_card_target_rejects_dotfiles(cards_dir: Path, bad: str) -> None:
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(cards_dir, bad)


@pytest.mark.parametrize(
    "bad",
    ["with\nnewline", "with\x00null"],
)
def test_resolve_card_target_rejects_control_chars(cards_dir: Path, bad: str) -> None:
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(cards_dir, bad)


def test_resolve_card_target_rejects_whitespace_padded(cards_dir: Path) -> None:
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(cards_dir, "  card  ")


# --- slugify ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Recent Session Summary", "recent-session-summary"),
        ("Tools & Utilities", "tools-utilities"),
        ("  Edge Cases (v2)  ", "edge-cases-v2"),
        ("###", ""),
        ("already-slug", "already-slug"),
        ("UPPER CASE", "upper-case"),
        ("multi   space", "multi-space"),
        ("dashes---collapse", "dashes-collapse"),
        ("---leading-and-trailing---", "leading-and-trailing"),
        ("with.dots.and_underscores", "with-dots-and-underscores"),
        ("Spec / Design / V1", "spec-design-v1"),
    ],
)
def test_slugify_examples(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_slugify_truncates_long_input() -> None:
    raw = "a" * 200
    out = slugify(raw)
    assert len(out) <= 80
    assert out == "a" * 80


def test_slugify_prefers_dash_boundary_when_truncating() -> None:
    # Construct a string where a dash falls within the last 10 chars of the 80-char window.
    # Word: 70 'a's, then '-', then 30 'b's.
    raw = ("a" * 70) + "-" + ("b" * 30)
    out = slugify(raw)
    assert len(out) <= 80
    # Should cut at the dash boundary at position 70 rather than mid-'b' run at 80.
    assert out == "a" * 70


def test_slugify_no_dash_boundary_does_hard_cut() -> None:
    # No dash anywhere near the boundary -> hard cut at 80.
    raw = "a" * 90
    out = slugify(raw)
    assert out == "a" * 80


def test_slugify_strips_trailing_dash_after_truncation() -> None:
    # Truncation lands right on a dash; trailing dash should be stripped.
    raw = ("a" * 79) + "-" + ("b" * 5)
    out = slugify(raw)
    assert not out.endswith("-")


# --- assert_git_clean -------------------------------------------------------


def _init_repo(path: Path) -> None:
    """Initialize a fresh git repo at `path` with one committed file."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )


def test_assert_git_clean_passes_on_clean_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # Does not raise.
    assert_git_clean(tmp_path)


def test_assert_git_clean_fails_on_modified_tracked_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("modified\n")
    with pytest.raises(DirtyWorkspaceError):
        assert_git_clean(tmp_path)


def test_assert_git_clean_fails_on_untracked_by_default(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "newfile.txt").write_text("new\n")
    with pytest.raises(DirtyWorkspaceError):
        assert_git_clean(tmp_path)


def test_assert_git_clean_allows_untracked_when_flag_set(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "newfile.txt").write_text("new\n")
    assert_git_clean(tmp_path, allow_untracked=True)


def test_assert_git_clean_walks_up_to_find_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert_git_clean(nested)


def test_assert_git_clean_rejects_non_git_dir(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(DirtyWorkspaceError, match="not a git repo"):
        assert_git_clean(not_a_repo)


def test_assert_git_clean_fails_on_staged_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("staged change\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    with pytest.raises(DirtyWorkspaceError):
        assert_git_clean(tmp_path)


def test_assert_git_clean_raises_on_timeout(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    def fake_run(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    with patch("bootstrap_doctor.safety.subprocess.run", side_effect=fake_run):
        with pytest.raises(DirtyWorkspaceError):
            assert_git_clean(tmp_path)


def test_assert_git_clean_raises_on_subprocess_error(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    def fake_run(*_a, **_kw):
        raise OSError("no git binary")

    with patch("bootstrap_doctor.safety.subprocess.run", side_effect=fake_run):
        with pytest.raises(DirtyWorkspaceError):
            assert_git_clean(tmp_path)


# --- assert_git_clean_or_force ---------------------------------------------


def test_assert_git_clean_or_force_passes_when_force(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("dirty\n")
    # Would raise without force, but force=True bypasses entirely.
    assert_git_clean_or_force(tmp_path, force=True)


def test_assert_git_clean_or_force_enforces_when_not_force(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("dirty\n")
    with pytest.raises(DirtyWorkspaceError):
        assert_git_clean_or_force(tmp_path, force=False)


def test_assert_git_clean_or_force_clean_repo_no_force(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert_git_clean_or_force(tmp_path, force=False)
