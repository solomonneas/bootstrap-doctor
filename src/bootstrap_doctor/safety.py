"""Safety primitives every mutating verb must route through.

Covers four concerns:

1. Atomic writes via same-directory tempfile + os.replace, so a crash mid-write
   leaves either the old file or the new one, never a torn mix.
2. Path-traversal guard for card targets, defending the cards directory against
   tricks like '../', absolute paths, or null bytes.
3. Slugification of arbitrary heading text into safe card filenames.
4. Git-clean preflight so any mutation is revertable; bypassable with --force.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


class UnsafeTargetError(Exception):
    """Raised when a candidate path or slug would escape its allowed root."""


class DirtyWorkspaceError(Exception):
    """Raised when the workspace git tree is not clean (or not a repo)."""


# --- atomic_write_text ------------------------------------------------------


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via tempfile + os.replace.

    Same-dir tempfile guarantees the rename is atomic on POSIX filesystems,
    so a crash mid-write leaves either the old file or the new file, never a
    truncated mix. Parent dirs are created if missing. On any exception during
    the write we best-effort unlink the tempfile so we do not leak debris.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- ensure_within ----------------------------------------------------------


def ensure_within(base: Path, candidate: Path) -> Path:
    """Resolve `candidate` and verify it lives inside `base.resolve()`.

    Returns the resolved candidate on success. Raises UnsafeTargetError if the
    candidate resolves outside base. Does not require candidate to exist.
    """
    resolved_base = base.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_base):
        raise UnsafeTargetError(
            f"path {resolved_candidate} escapes base {resolved_base}"
        )
    return resolved_candidate


# --- resolve_card_target ----------------------------------------------------


def resolve_card_target(cards_dir: Path, slug: str) -> Path:
    """Validate `slug` and return the resolved path inside `cards_dir`.

    Rejects empty/whitespace-padded slugs, separators, dotfiles, control chars,
    and anything that resolves outside cards_dir. Appends '.md' if missing.
    """
    if slug is None or slug == "" or slug.strip() == "":
        raise UnsafeTargetError(f"empty or whitespace-only slug: {slug!r}")
    if slug.strip() != slug:
        raise UnsafeTargetError(f"whitespace-padded slug: {slug!r}")
    if "/" in slug or "\\" in slug:
        raise UnsafeTargetError(f"slug must not contain path separators: {slug!r}")
    if "\n" in slug or "\0" in slug or "\r" in slug:
        raise UnsafeTargetError(f"slug must not contain control chars: {slug!r}")
    if slug in {".", ".."}:
        raise UnsafeTargetError(f"slug must not be '.' or '..': {slug!r}")
    if slug.startswith("."):
        raise UnsafeTargetError(f"slug must not start with '.': {slug!r}")

    name = slug if slug.endswith(".md") else f"{slug}.md"
    candidate = cards_dir / name
    return ensure_within(cards_dir, candidate)


# --- slugify ----------------------------------------------------------------


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SLUG_DASH_RUN_RE = re.compile(r"-+")
_MAX_SLUG_LEN = 80
_BOUNDARY_WINDOW = 10


def slugify(text: str) -> str:
    """Turn arbitrary heading text into a card-safe slug.

    Rules: lowercase, strip, replace non-[a-z0-9] runs with single '-', collapse
    repeated dashes, strip leading/trailing dashes, truncate to 80 chars with
    preference for the last dash boundary in the final 10 chars.

    Returns "" if input slugs to nothing; callers must handle that case.
    """
    if not text:
        return ""
    lowered = text.strip().lower()
    if not lowered:
        return ""
    # Replace any run of non-alphanumeric characters with a single dash.
    dashed = _SLUG_NON_ALNUM_RE.sub("-", lowered)
    # Collapse repeated dashes (belt-and-suspenders; regex above already does it).
    collapsed = _SLUG_DASH_RUN_RE.sub("-", dashed)
    stripped = collapsed.strip("-")
    if not stripped:
        return ""

    if len(stripped) <= _MAX_SLUG_LEN:
        return stripped

    # Truncate. Prefer cutting at the last dash within the final BOUNDARY_WINDOW
    # characters of the 80-char window so we do not chop a word ugly-mid.
    window = stripped[:_MAX_SLUG_LEN]
    boundary_start = _MAX_SLUG_LEN - _BOUNDARY_WINDOW
    last_dash = window.rfind("-", boundary_start)
    if last_dash != -1:
        truncated = window[:last_dash]
    else:
        truncated = window
    return truncated.strip("-")


# --- assert_git_clean -------------------------------------------------------


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a `.git` entry (file or dir). Returns
    the directory containing it, or None if no repo is found."""
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def assert_git_clean(repo_dir: Path, *, allow_untracked: bool = False) -> None:
    """Verify `repo_dir` has a clean git working tree.

    Walks up from repo_dir to find .git. Runs `git status --porcelain` from the
    repo root and inspects its output. Raises DirtyWorkspaceError on any of:
      - not inside a git repo
      - any modified/staged tracked file
      - untracked files when allow_untracked is False
      - subprocess timeout/error (refuse to assume clean on a failed probe)
    """
    root = _find_repo_root(repo_dir)
    if root is None:
        raise DirtyWorkspaceError(f"not a git repo: {repo_dir}")

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise DirtyWorkspaceError(f"git status timed out in {root}") from exc
    except OSError as exc:
        raise DirtyWorkspaceError(f"git status failed in {root}: {exc}") from exc

    if result.returncode != 0:
        raise DirtyWorkspaceError(
            f"git status exited {result.returncode} in {root}: {result.stderr.strip()}"
        )

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if allow_untracked:
        lines = [ln for ln in lines if not ln.startswith("??")]
    if lines:
        preview = "\n".join(lines[:5])
        raise DirtyWorkspaceError(
            f"workspace not clean in {root}:\n{preview}"
        )


def assert_git_clean_or_force(repo_dir: Path, force: bool) -> None:
    """Bypass-aware wrapper: skip the clean check if `force` is True."""
    if force:
        return
    assert_git_clean(repo_dir)
