"""Tests for the section parser.

Coverage: H2/H3 splitting, preamble handling, code-fence-aware splits,
heading text normalization, CRLF normalization, edge cases, and
``last_touched_git_mtime`` against a temp git repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bootstrap_doctor.parsing import (
    Section,
    last_touched_git_mtime,
    parse_file,
    parse_text,
)


# ----- parse_text basics --------------------------------------------------


def test_parse_text_empty_returns_empty_list(tmp_path: Path) -> None:
    assert parse_text("", tmp_path / "x.md") == []


def test_parse_text_only_whitespace_returns_empty_list(tmp_path: Path) -> None:
    # Pure whitespace strips to nothing; no preamble emitted.
    assert parse_text("\n\n   \n", tmp_path / "x.md") == []


def test_preamble_only_emits_single_section(tmp_path: Path) -> None:
    text = "intro line one\nintro line two\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    s = sections[0]
    assert s.heading_level == 0
    assert s.heading_text == ""
    assert s.heading_path == ()
    assert s.body == "intro line one\nintro line two"
    assert s.char_count == len(s.body)
    assert s.line_count == 2
    assert s.start_line == 1
    assert s.end_line == 2


def test_file_starts_with_h2_emits_no_preamble(tmp_path: Path) -> None:
    text = "## Title\nbody line\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    s = sections[0]
    assert s.heading_level == 2
    assert s.heading_text == "Title"
    assert s.heading_path == ("Title",)
    assert s.body == "body line"


def test_blank_lines_before_first_h2_are_not_a_preamble(tmp_path: Path) -> None:
    # Leading whitespace alone shouldn't manufacture a preamble section.
    text = "\n\n## Title\nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    assert sections[0].heading_text == "Title"
    assert sections[0].heading_level == 2


def test_two_h2_sections_split_correctly(tmp_path: Path) -> None:
    text = (
        "## Alpha\n"
        "alpha body\n"
        "\n"
        "## Beta\n"
        "beta body\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    assert [s.heading_text for s in sections] == ["Alpha", "Beta"]
    assert sections[0].body == "alpha body"
    assert sections[1].body == "beta body"
    assert sections[0].heading_path == ("Alpha",)
    assert sections[1].heading_path == ("Beta",)


def test_h3_nests_under_most_recent_h2(tmp_path: Path) -> None:
    text = (
        "## Alpha\n"
        "alpha intro\n"
        "### Alpha-1\n"
        "first sub\n"
        "### Alpha-2\n"
        "second sub\n"
        "## Beta\n"
        "beta body\n"
        "### Beta-1\n"
        "beta sub\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    levels = [(s.heading_level, s.heading_path) for s in sections]
    assert levels == [
        (2, ("Alpha",)),
        (3, ("Alpha", "Alpha-1")),
        (3, ("Alpha", "Alpha-2")),
        (2, ("Beta",)),
        (3, ("Beta", "Beta-1")),
    ]
    # H2 body stops at first H3.
    assert sections[0].body == "alpha intro"
    assert sections[1].body == "first sub"
    assert sections[2].body == "second sub"
    assert sections[3].body == "beta body"
    assert sections[4].body == "beta sub"


def test_h3_without_preceding_h2_has_single_element_heading_path(tmp_path: Path) -> None:
    # Defensive: if a file starts with an H3 (unusual), we still place it.
    text = "### Solo\nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    s = sections[0]
    assert s.heading_level == 3
    assert s.heading_text == "Solo"
    assert s.heading_path == ("Solo",)
    assert s.body == "body"


def test_preamble_then_h2(tmp_path: Path) -> None:
    text = "preface\n\n## Title\nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 2
    assert sections[0].heading_level == 0
    assert sections[0].body == "preface"
    assert sections[1].heading_text == "Title"
    assert sections[1].body == "body"


# ----- heading text normalization -----------------------------------------


def test_heading_text_strips_trailing_whitespace(tmp_path: Path) -> None:
    text = "## Title   \nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert sections[0].heading_text == "Title"


def test_heading_text_strips_trailing_hash_closure(tmp_path: Path) -> None:
    text = "## Title ##\nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert sections[0].heading_text == "Title"


def test_heading_text_keeps_internal_punctuation(tmp_path: Path) -> None:
    text = "## Tools (local)\nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert sections[0].heading_text == "Tools (local)"


def test_h4_does_not_split(tmp_path: Path) -> None:
    text = (
        "## Outer\n"
        "intro\n"
        "#### Inner h4\n"
        "deeper stuff\n"
        "##### h5 too\n"
        "more\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    body = sections[0].body
    assert "#### Inner h4" in body
    assert "deeper stuff" in body
    assert "##### h5 too" in body


def test_atx_only_setext_underline_ignored(tmp_path: Path) -> None:
    # Setext-style headings should NOT split; they appear verbatim in body.
    text = (
        "## Real Heading\n"
        "intro\n"
        "Not A Heading\n"
        "=============\n"
        "still in body\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    assert "Not A Heading" in sections[0].body
    assert "=============" in sections[0].body


# ----- code fence handling ------------------------------------------------


def test_fenced_heading_inside_code_block_does_not_split(tmp_path: Path) -> None:
    text = (
        "## Real\n"
        "intro\n"
        "```\n"
        "## fake heading inside fence\n"
        "### fake h3 inside fence\n"
        "```\n"
        "after fence\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    body = sections[0].body
    assert "## fake heading inside fence" in body
    assert "### fake h3 inside fence" in body
    assert "after fence" in body


def test_multiple_code_fences_split_correctly(tmp_path: Path) -> None:
    text = (
        "## A\n"
        "```\n"
        "## not split 1\n"
        "```\n"
        "between fences\n"
        "```\n"
        "## not split 2\n"
        "```\n"
        "## B\n"
        "b body\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    assert [s.heading_text for s in sections] == ["A", "B"]
    assert "## not split 1" in sections[0].body
    assert "between fences" in sections[0].body
    assert "## not split 2" in sections[0].body
    assert sections[1].body == "b body"


# ----- body trimming, char/line counts ------------------------------------


def test_body_strips_leading_and_trailing_blank_lines(tmp_path: Path) -> None:
    text = (
        "## Title\n"
        "\n"
        "\n"
        "content line\n"
        "\n"
        "\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    assert sections[0].body == "content line"
    assert sections[0].char_count == len("content line")
    assert sections[0].line_count == 1


def test_empty_body_section_has_zero_counts(tmp_path: Path) -> None:
    text = "## Title\n## Next\nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert sections[0].heading_text == "Title"
    assert sections[0].body == ""
    assert sections[0].char_count == 0
    assert sections[0].line_count == 0
    # end_line == start_line for empty body
    assert sections[0].end_line == sections[0].start_line


def test_char_count_matches_body_length(tmp_path: Path) -> None:
    text = "## Title\nline 1\nline 2\nline 3\n"
    sections = parse_text(text, tmp_path / "x.md")
    s = sections[0]
    assert s.body == "line 1\nline 2\nline 3"
    assert s.char_count == len(s.body)
    assert s.line_count == 3


def test_line_count_counts_newlines_plus_one(tmp_path: Path) -> None:
    text = "## Title\nsolo\n"
    sections = parse_text(text, tmp_path / "x.md")
    s = sections[0]
    assert s.body == "solo"
    assert s.line_count == 1


# ----- line numbers -------------------------------------------------------


def test_start_line_points_to_heading(tmp_path: Path) -> None:
    text = (
        "preface\n"
        "more preface\n"
        "## Real\n"
        "body\n"
        "## Next\n"
        "next body\n"
    )
    sections = parse_text(text, tmp_path / "x.md")
    assert sections[0].heading_level == 0
    assert sections[0].start_line == 1
    assert sections[1].heading_text == "Real"
    assert sections[1].start_line == 3
    assert sections[2].heading_text == "Next"
    assert sections[2].start_line == 5


def test_end_line_points_to_last_body_line(tmp_path: Path) -> None:
    text = (
        "## Real\n"   # line 1
        "body 1\n"     # line 2
        "body 2\n"     # line 3
        "## Next\n"    # line 4
        "next body\n"  # line 5
    )
    sections = parse_text(text, tmp_path / "x.md")
    real, nxt = sections
    assert real.start_line == 1
    assert real.end_line == 3
    assert nxt.start_line == 4
    assert nxt.end_line == 5


# ----- newline edge cases -------------------------------------------------


def test_file_ending_without_trailing_newline(tmp_path: Path) -> None:
    text = "## Title\nbody"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    assert sections[0].body == "body"
    assert sections[0].heading_text == "Title"


def test_crlf_line_endings_are_normalized(tmp_path: Path) -> None:
    text = "## Title\r\nbody line one\r\nbody line two\r\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    s = sections[0]
    # CRLF normalized to LF before measuring char_count.
    assert "\r" not in s.body
    assert s.body == "body line one\nbody line two"
    assert s.char_count == len(s.body)


def test_mixed_crlf_and_lf(tmp_path: Path) -> None:
    text = "## A\r\nalpha\n## B\r\nbeta\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert [s.heading_text for s in sections] == ["A", "B"]
    assert sections[0].body == "alpha"
    assert sections[1].body == "beta"


# ----- file association ---------------------------------------------------


def test_sections_carry_file_path(tmp_path: Path) -> None:
    p = tmp_path / "fixture.md"
    text = "## Alpha\nbody\n"
    sections = parse_text(text, p)
    assert sections[0].file == p


def test_section_is_frozen_dataclass(tmp_path: Path) -> None:
    sections = parse_text("## A\nbody\n", tmp_path / "x.md")
    with pytest.raises(Exception):
        sections[0].heading_text = "Mutated"  # type: ignore[misc]


# ----- parse_file roundtrip ------------------------------------------------


def test_parse_file_reads_disk(tmp_path: Path) -> None:
    p = tmp_path / "AGENTS.md"
    p.write_text("## H\nbody\n")
    sections = parse_file(p)
    assert len(sections) == 1
    assert sections[0].file == p
    assert sections[0].heading_text == "H"


def test_parse_file_handles_crlf_on_disk(tmp_path: Path) -> None:
    p = tmp_path / "crlf.md"
    p.write_bytes(b"## H\r\nbody\r\n")
    sections = parse_file(p)
    assert sections[0].body == "body"


# ----- four-pound prefix doesn't trick H2/H3 regex -----------------------


def test_four_pound_heading_not_split_as_h3(tmp_path: Path) -> None:
    # An H4 line starts with "#### " and must not be misread as an H3.
    text = "## Outer\nintro\n#### Sub\nsubbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    assert len(sections) == 1
    assert "#### Sub" in sections[0].body


def test_heading_requires_space_after_hashes(tmp_path: Path) -> None:
    # "##NoSpace" is not a heading.
    text = "##NoSpace\nbody\n"
    sections = parse_text(text, tmp_path / "x.md")
    # Falls through to preamble.
    assert len(sections) == 1
    assert sections[0].heading_level == 0
    assert "##NoSpace" in sections[0].body


# ----- last_touched_git_mtime ---------------------------------------------


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _git_init(repo: Path) -> None:
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    _run(["git", "config", "commit.gpgsign", "false"], repo)


def test_last_touched_git_mtime_returns_int_for_tracked_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    f = repo / "AGENTS.md"
    f.write_text("hello\n")
    _run(["git", "add", "AGENTS.md"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    ts = last_touched_git_mtime(f)
    assert isinstance(ts, int)
    assert ts > 0


def test_last_touched_git_mtime_returns_none_for_untracked_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    # Initial commit so .git/HEAD is real.
    seed = repo / "seed.md"
    seed.write_text("seed\n")
    _run(["git", "add", "seed.md"], repo)
    _run(["git", "commit", "-qm", "seed"], repo)
    # Untracked file in the same repo.
    untracked = repo / "UNTRACKED.md"
    untracked.write_text("nope\n")
    assert last_touched_git_mtime(untracked) is None


def test_last_touched_git_mtime_returns_none_outside_git_repo(tmp_path: Path) -> None:
    # tmp_path itself isn't a git repo (and pytest's tmp_path root isn't either).
    loose = tmp_path / "loose.md"
    loose.write_text("nope\n")
    assert last_touched_git_mtime(loose) is None


def test_last_touched_git_mtime_returns_none_for_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"
    assert last_touched_git_mtime(missing) is None


def test_last_touched_git_mtime_walks_up_to_find_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sub = repo / "deep" / "nested"
    sub.mkdir(parents=True)
    _git_init(repo)
    f = sub / "leaf.md"
    f.write_text("leaf\n")
    _run(["git", "add", "deep/nested/leaf.md"], repo)
    _run(["git", "commit", "-qm", "deep"], repo)
    ts = last_touched_git_mtime(f)
    assert isinstance(ts, int)
    assert ts > 0


# ----- integration-ish: realistic bootstrap shape -------------------------


def test_realistic_bootstrap_shape(tmp_path: Path) -> None:
    text = (
        "# AGENTS.md\n"
        "\n"
        "Preface text describing this file.\n"
        "\n"
        "## Quick Start\n"
        "\n"
        "Run the gateway:\n"
        "\n"
        "```bash\n"
        "## not a heading\n"
        "systemctl --user start openclaw-gateway\n"
        "```\n"
        "\n"
        "## Tools (local)\n"
        "\n"
        "### Code Search\n"
        "\n"
        "Port 5204.\n"
        "\n"
        "### Prompt Library\n"
        "\n"
        "Port 5202.\n"
        "\n"
        "## Gotchas\n"
        "\n"
        "Watch out.\n"
    )
    sections = parse_text(text, tmp_path / "AGENTS.md")
    headings = [(s.heading_level, s.heading_path) for s in sections]
    assert headings == [
        (0, ()),
        (2, ("Quick Start",)),
        (2, ("Tools (local)",)),
        (3, ("Tools (local)", "Code Search")),
        (3, ("Tools (local)", "Prompt Library")),
        (2, ("Gotchas",)),
    ]
    # Quick Start body keeps the entire code fence including the fake heading.
    qs = sections[1]
    assert "## not a heading" in qs.body
    assert "systemctl --user start openclaw-gateway" in qs.body
    # Tools (local) parent body is empty (only H3 children).
    tools = sections[2]
    assert tools.body == ""
    # H3 bodies are intact.
    cs = sections[3]
    assert cs.body == "Port 5204."
    pl = sections[4]
    assert pl.body == "Port 5202."
    # Final H2 body.
    assert sections[5].body == "Watch out."
