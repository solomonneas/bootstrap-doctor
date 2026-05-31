"""Section splitter: parse tracked markdown into H2/H3 sections with metadata.

Each tracked bootstrap file (AGENTS.md, TOOLS.md, ...) is broken into
addressable :class:`Section` records keyed by H2/H3 heading. The output is
consumed by ``heuristics.py``, ``judge.py``, and ``trim.py`` to decide which
sections to offload into ``memory/cards/``.

Parsing rules (full spec in ``docs/bootstrap-doctor-design.md``):

  * Preamble (anything before the first ATX heading) becomes a synthetic
    Section with ``heading_level=0`` and an empty ``heading_path``. Blank
    lines before the first heading do NOT count as a preamble.
  * Splits fire only on ``^## `` and ``^### `` (ATX, two or three hashes
    followed by a space). H4+ stays inside the parent section's body.
  * Fenced code blocks (``` ``` ```) are heading-immune; a ``## fake``
    line inside a fence does not trigger a split.
  * Heading text strips leading/trailing whitespace and any trailing
    closure ``#`` characters (``## Title ##`` -> ``Title``). Inline
    markdown is NOT processed.
  * CRLF line endings are normalized to LF before character counts and
    body content are computed, so output is platform-stable.
  * Setext-style headings (``===``/``---`` underlines) are intentionally
    ignored; bootstrap files use ATX only.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# A line counts as an H2 heading iff it matches `^## ` (exactly two hashes,
# then a space). H3 is `^### `. Anything deeper (`^#### `, `^##### `, etc.)
# is NOT a section break: matched separately below to keep H3 from grabbing
# H4 lines, since `### ` is a prefix of `#### `.
H2_RE = re.compile(r"^##\s+(.*)$")
H3_RE = re.compile(r"^###\s+(.*)$")
H4_PLUS_RE = re.compile(r"^####+\s")  # H4 or deeper: leave in body


@dataclass(frozen=True)
class Section:
    """One addressable chunk of a bootstrap file.

    See module docstring for field semantics.
    """

    file: Path
    heading_level: int            # 0 (preamble), 2, or 3
    heading_text: str             # without leading `#`s; empty for preamble
    heading_path: tuple[str, ...]  # () for preamble, (H2,) or (H2, H3)
    body: str                      # post-heading content, blank-stripped
    char_count: int                # len(body)
    line_count: int                # body.count("\n") + 1, or 0 if body == ""
    start_line: int                # 1-indexed; line of heading (1 for preamble)
    end_line: int                  # 1-indexed; last body line, or start_line if empty


# ---------------------------------------------------------------------------
# Heading normalization
# ---------------------------------------------------------------------------


def _normalize_heading_text(raw: str) -> str:
    """Strip whitespace and trailing closure hashes from heading text.

    ``## Title ##`` -> ``Title``. ``## Tools (local)`` -> ``Tools (local)``.
    Inline markdown is preserved verbatim; only outer whitespace and
    trailing ``#`` characters (with optional whitespace) are removed.
    """
    s = raw.strip()
    # Drop a trailing run of `#` (with optional preceding spaces). The pattern
    # `##` is the ATX-closure idiom: `## Title ##` or `## Title###`.
    s = re.sub(r"\s*#+\s*$", "", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Parsing core
# ---------------------------------------------------------------------------


def _normalize_newlines(text: str) -> str:
    """Normalize CRLF and lone CR to LF.

    All downstream measurements (``char_count``, ``line_count``,
    ``start_line``, ``end_line``) operate on the normalized form, so two
    files that differ only in line-ending convention produce identical
    Section output.
    """
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _trim_blank_edges(body_lines: list[str]) -> tuple[list[str], int]:
    """Drop leading and trailing all-whitespace lines.

    Returns the trimmed list and the count of leading lines removed (so the
    caller can adjust line numbers).
    """
    start = 0
    end = len(body_lines)
    while start < end and not body_lines[start].strip():
        start += 1
    while end > start and not body_lines[end - 1].strip():
        end -= 1
    return body_lines[start:end], start


def _finalize_section(
    *,
    file: Path,
    heading_level: int,
    heading_text: str,
    heading_path: tuple[str, ...],
    body_lines: list[str],
    heading_line_num: int,
) -> Section:
    """Build a Section from accumulated body lines.

    ``heading_line_num`` is the 1-indexed line number of the heading itself
    (or 1 for the preamble; the preamble synthesizes ``start_line=1`` even
    when there are leading blank lines).
    """
    trimmed, leading_blank = _trim_blank_edges(body_lines)
    body = "\n".join(trimmed)
    if body:
        line_count = body.count("\n") + 1
        # The first non-blank body line lives at heading_line + 1 + leading_blank,
        # except for the preamble which has no heading line above it.
        if heading_level == 0:
            first_body_line = 1 + leading_blank
        else:
            first_body_line = heading_line_num + 1 + leading_blank
        start_line = heading_line_num if heading_level > 0 else 1
        end_line = first_body_line + line_count - 1
    else:
        line_count = 0
        start_line = heading_line_num if heading_level > 0 else 1
        end_line = start_line
    return Section(
        file=file,
        heading_level=heading_level,
        heading_text=heading_text,
        heading_path=heading_path,
        body=body,
        char_count=len(body),
        line_count=line_count,
        start_line=start_line,
        end_line=end_line,
    )


def parse_text(text: str, file: Path) -> list[Section]:
    """Parse markdown ``text`` into a list of :class:`Section` records.

    ``file`` is stored on each Section for downstream reference. It is NOT
    read from disk here; pair this with :func:`parse_file` when you need
    on-disk content.
    """
    text = _normalize_newlines(text)
    if not text.strip():
        return []

    lines = text.split("\n")
    # A trailing "\n" in the source produces an empty final element after
    # split; drop it so line counts line up with 1-indexed file positions.
    if lines and lines[-1] == "":
        lines.pop()

    sections: list[Section] = []

    # Preamble state. We only emit a preamble Section if at least one
    # non-blank line appears before the first H2/H3.
    in_fence = False
    preamble_lines: list[str] = []
    preamble_has_content = False
    first_heading_line: int | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Track code fences in the preamble too: a fence opened in the
        # preamble must close before we honor any heading.
        if stripped.startswith("```"):
            in_fence = not in_fence
            preamble_lines.append(line)
            if stripped:
                preamble_has_content = True
            i += 1
            continue
        if not in_fence and (H2_RE.match(line) or H3_RE.match(line)):
            first_heading_line = i + 1  # 1-indexed
            break
        preamble_lines.append(line)
        if stripped:
            preamble_has_content = True
        i += 1

    if preamble_has_content:
        sections.append(
            _finalize_section(
                file=file,
                heading_level=0,
                heading_text="",
                heading_path=(),
                body_lines=preamble_lines,
                heading_line_num=1,
            )
        )

    if first_heading_line is None:
        return sections

    # Heading walk. Maintain the current H2 so any H3 can attach to it.
    current_h2: str | None = None
    in_fence = False
    j = first_heading_line - 1  # 0-indexed cursor into `lines`

    # State for the "open" section we're collecting body for.
    cur_level: int | None = None
    cur_text: str | None = None
    cur_path: tuple[str, ...] | None = None
    cur_heading_line: int = 0
    cur_body: list[str] = []

    def flush() -> None:
        if cur_level is None:
            return
        assert cur_text is not None and cur_path is not None
        sections.append(
            _finalize_section(
                file=file,
                heading_level=cur_level,
                heading_text=cur_text,
                heading_path=cur_path,
                body_lines=cur_body,
                heading_line_num=cur_heading_line,
            )
        )

    while j < len(lines):
        line = lines[j]
        stripped = line.strip()

        if stripped.startswith("```"):
            in_fence = not in_fence
            cur_body.append(line)
            j += 1
            continue

        if not in_fence:
            m2 = H2_RE.match(line)
            m3 = H3_RE.match(line) if not m2 else None
            # H4+ must not match the H3 regex; H3_RE requires exactly `### `
            # which DOES match `#### Foo` as `# Foo`. Guard explicitly.
            if m3 and H4_PLUS_RE.match(line):
                m3 = None

            if m2:
                flush()
                heading_text = _normalize_heading_text(m2.group(1))
                current_h2 = heading_text
                cur_level = 2
                cur_text = heading_text
                cur_path = (heading_text,)
                cur_heading_line = j + 1
                cur_body = []
                j += 1
                continue
            if m3:
                flush()
                heading_text = _normalize_heading_text(m3.group(1))
                cur_level = 3
                cur_text = heading_text
                if current_h2 is not None:
                    cur_path = (current_h2, heading_text)
                else:
                    cur_path = (heading_text,)
                cur_heading_line = j + 1
                cur_body = []
                j += 1
                continue

        cur_body.append(line)
        j += 1

    flush()
    return sections


def parse_file(path: Path) -> list[Section]:
    """Read ``path`` from disk and parse it.

    Reads bytes (not text) so CRLF normalization happens in one place
    inside :func:`parse_text` rather than being silently rewritten by the
    Python ``open()`` newline handling.
    """
    raw = path.read_bytes().decode("utf-8")
    return parse_text(raw, path)


# ---------------------------------------------------------------------------
# Git mtime helper
# ---------------------------------------------------------------------------


def _find_git_dir(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``.git`` directory or file.

    Returns the repository root (the dir containing ``.git``), or None if
    no git repo is found before hitting the filesystem root.
    """
    cur = start if start.is_dir() else start.parent
    try:
        cur = cur.resolve()
    except (OSError, RuntimeError):
        return None
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def last_touched_git_mtime(path: Path) -> int | None:
    """Return UNIX timestamp of the last commit touching ``path``.

    Returns ``None`` if the file is not in a git repo, is untracked, or if
    any subprocess error occurs. Never raises.
    """
    if not path.exists():
        return None
    repo = _find_git_dir(path)
    if repo is None:
        return None
    try:
        rel = path.resolve().relative_to(repo)
    except ValueError:
        return None
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(rel)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None
