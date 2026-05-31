"""Tests for heuristics.py shortlist rules."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bootstrap_doctor.heuristics import (
    find_duplicates,
    has_long_code_block,
    is_large,
    is_stale,
    shortlist,
)
from bootstrap_doctor.parsing import Section
from bootstrap_doctor.paths import resolve_config

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def make_section(
    file: str | Path = "A.md",
    heading: str = "Test",
    body: str = "x",
    level: int = 2,
    *,
    heading_path: tuple[str, ...] | None = None,
    start_line: int = 1,
    end_line: int | None = None,
) -> Section:
    """Build a Section fixture without going through the parser."""
    if isinstance(file, str):
        file = Path(file)
    if heading_path is None:
        heading_path = (heading,) if level > 0 else ()
    if end_line is None:
        end_line = start_line + (body.count("\n") if body else 0)
    char_count = len(body)
    if body:
        line_count = body.count("\n") + 1
    else:
        line_count = 0
    return Section(
        file=file,
        heading_level=level,
        heading_text=heading if level > 0 else "",
        heading_path=heading_path,
        body=body,
        char_count=char_count,
        line_count=line_count,
        start_line=start_line,
        end_line=end_line,
    )


@pytest.fixture
def cfg(workspace_dir: Path, cards_dir: Path):
    """Default Config with min_section_chars=400, stale_days=60."""
    cfg_file = workspace_dir / "config.toml"
    cfg_file.write_text(
        f'workspace_dir = "{workspace_dir}"\n'
        f'cards_dir = "{cards_dir}"\n'
    )
    return resolve_config(config_file=str(cfg_file))


# ---------------------------------------------------------------------------
# is_large
# ---------------------------------------------------------------------------


def test_is_large_strictly_greater_than_threshold(cfg) -> None:
    s = make_section(body="x" * (cfg.min_section_chars + 1))
    assert is_large(s, cfg) is True


def test_is_large_equal_to_threshold_is_false(cfg) -> None:
    s = make_section(body="x" * cfg.min_section_chars)
    assert is_large(s, cfg) is False


def test_is_large_below_threshold_is_false(cfg) -> None:
    s = make_section(body="x" * (cfg.min_section_chars - 1))
    assert is_large(s, cfg) is False


# ---------------------------------------------------------------------------
# has_long_code_block
# ---------------------------------------------------------------------------


def test_has_long_code_block_short_block_false() -> None:
    body = "intro\n```\n" + "\n".join(f"line{i}" for i in range(5)) + "\n```\n"
    s = make_section(body=body)
    assert has_long_code_block(s) is False


def test_has_long_code_block_long_block_true() -> None:
    body = "intro\n```\n" + "\n".join(f"line{i}" for i in range(15)) + "\n```\n"
    s = make_section(body=body)
    assert has_long_code_block(s) is True


def test_has_long_code_block_two_short_blocks_false() -> None:
    """Total lines across blocks don't aggregate; need ONE block over the threshold."""
    block_a = "```\n" + "\n".join(f"a{i}" for i in range(6)) + "\n```\n"
    block_b = "```\n" + "\n".join(f"b{i}" for i in range(6)) + "\n```\n"
    s = make_section(body=block_a + "\nprose\n" + block_b)
    assert has_long_code_block(s) is False


def test_has_long_code_block_language_tag_still_detected() -> None:
    body = "```python\n" + "\n".join(f"line{i}" for i in range(15)) + "\n```\n"
    s = make_section(body=body)
    assert has_long_code_block(s) is True


def test_has_long_code_block_no_fences_false() -> None:
    s = make_section(body="just prose, no code at all\nsecond line\n")
    assert has_long_code_block(s) is False


def test_has_long_code_block_custom_threshold() -> None:
    body = "```\n" + "\n".join(f"line{i}" for i in range(7)) + "\n```\n"
    s = make_section(body=body)
    assert has_long_code_block(s, min_lines=5) is True
    assert has_long_code_block(s, min_lines=10) is False


def test_has_long_code_block_unclosed_fence_no_match() -> None:
    """A fence that never closes is malformed and should not crash or count."""
    body = "```\n" + "\n".join(f"line{i}" for i in range(15)) + "\n"
    s = make_section(body=body)
    # No closing fence -> no detected code block
    assert has_long_code_block(s) is False


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


def test_is_stale_older_than_threshold_true(cfg) -> None:
    now = 1_700_000_000
    last = now - (61 * 86400)
    s = make_section()
    assert is_stale(s, cfg, last_touched_ts=last, now_ts=now) is True


def test_is_stale_younger_than_threshold_false(cfg) -> None:
    now = 1_700_000_000
    last = now - (59 * 86400)
    s = make_section()
    assert is_stale(s, cfg, last_touched_ts=last, now_ts=now) is False


def test_is_stale_no_mtime_returns_false(cfg) -> None:
    now = 1_700_000_000
    s = make_section()
    assert is_stale(s, cfg, last_touched_ts=None, now_ts=now) is False


def test_is_stale_exactly_at_threshold_false(cfg) -> None:
    """Strict greater-than on age; exactly stale_days old isn't stale yet."""
    now = 1_700_000_000
    last = now - (cfg.stale_days * 86400)
    s = make_section()
    assert is_stale(s, cfg, last_touched_ts=last, now_ts=now) is False


def test_is_stale_uses_time_time_when_now_ts_none(cfg, monkeypatch) -> None:
    """If now_ts is None, fall back to time.time()."""
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    last = int(fixed_now) - (61 * 86400)
    s = make_section()
    assert is_stale(s, cfg, last_touched_ts=last, now_ts=None) is True


# ---------------------------------------------------------------------------
# find_duplicates
# ---------------------------------------------------------------------------


def test_find_duplicates_identical_bodies_across_files() -> None:
    body = "This is a substantial body of text that we expect to find duplicated. " * 3
    a = make_section(file="A.md", body=body)
    b = make_section(file="B.md", body=body)
    dups = find_duplicates([a, b])
    assert b in dups[a]
    assert a in dups[b]


def test_find_duplicates_whitespace_and_case_normalized() -> None:
    body_a = "This Is A Long Body Of Text That Should Match The Other One Exactly. " * 3
    body_b = "  this   is a  long body of  text that should  match the other  one exactly.  " * 3
    a = make_section(file="A.md", body=body_a)
    b = make_section(file="B.md", body=body_b)
    dups = find_duplicates([a, b])
    assert b in dups[a]


def test_find_duplicates_unrelated_bodies_no_match() -> None:
    a = make_section(
        file="A.md",
        body="The quick brown fox jumps over the lazy dog. " * 5,
    )
    b = make_section(
        file="B.md",
        body="Completely unrelated paragraph about distributed system consensus. " * 5,
    )
    dups = find_duplicates([a, b])
    assert dups[a] == []
    assert dups[b] == []


def test_find_duplicates_short_sections_excluded() -> None:
    """Sections under 100 chars are too noisy to compare; skip them."""
    short_body = "tiny"
    a = make_section(file="A.md", body=short_body)
    b = make_section(file="B.md", body=short_body)
    dups = find_duplicates([a, b])
    assert dups[a] == []
    assert dups[b] == []


def test_find_duplicates_preamble_excluded() -> None:
    body = "This is a substantial body of text that we expect to find duplicated. " * 3
    a = make_section(file="A.md", body=body, level=0, heading="", heading_path=())
    b = make_section(file="B.md", body=body, level=2)
    dups = find_duplicates([a, b])
    # Preamble must be excluded as both source and target
    assert dups[a] == []
    assert dups[b] == []


def test_find_duplicates_section_does_not_match_itself() -> None:
    body = "This is a substantial body of text that we expect to find duplicated. " * 3
    a = make_section(file="A.md", body=body)
    dups = find_duplicates([a])
    assert dups[a] == []


# ---------------------------------------------------------------------------
# shortlist
# ---------------------------------------------------------------------------


def test_shortlist_section_with_multiple_reasons(cfg, tmp_path, monkeypatch) -> None:
    """A single section meeting size + code-block + stale criteria gets all three reasons."""
    big_body = (
        "Prose preamble. " * 50
        + "\n```\n"
        + "\n".join(f"line{i}" for i in range(15))
        + "\n```\n"
    )
    file_a = tmp_path / "A.md"
    file_a.write_text("# A\n")
    s = make_section(file=file_a, body=big_body)

    now = 1_700_000_000
    # Force mtime older than stale window
    monkeypatch.setattr(
        "bootstrap_doctor.heuristics.last_touched_git_mtime",
        lambda p: now - (cfg.stale_days + 5) * 86400,
    )

    out = shortlist([s], cfg, now_ts=now)
    assert len(out) == 1
    reasons = out[0].reasons
    assert "large" in reasons
    assert "long-code-block" in reasons
    assert "stale" in reasons


def test_shortlist_excludes_sections_with_no_reasons(cfg, tmp_path, monkeypatch) -> None:
    s = make_section(body="short clean body")
    monkeypatch.setattr(
        "bootstrap_doctor.heuristics.last_touched_git_mtime",
        lambda p: None,
    )
    out = shortlist([s], cfg)
    assert out == []


def test_shortlist_excludes_preamble(cfg, tmp_path, monkeypatch) -> None:
    """Preamble (heading_level=0) is skipped entirely, even if it has triggering content."""
    big_body = "x" * (cfg.min_section_chars + 100)
    s = make_section(body=big_body, level=0, heading="", heading_path=())
    monkeypatch.setattr(
        "bootstrap_doctor.heuristics.last_touched_git_mtime",
        lambda p: None,
    )
    out = shortlist([s], cfg)
    assert out == []


def test_shortlist_detects_duplicates_across_files(cfg, tmp_path, monkeypatch) -> None:
    body = "This is a substantial body of text that we expect to find duplicated. " * 3
    file_a = tmp_path / "A.md"
    file_b = tmp_path / "B.md"
    file_a.write_text("# A\n")
    file_b.write_text("# B\n")
    a = make_section(file=file_a, body=body)
    b = make_section(file=file_b, body=body)

    monkeypatch.setattr(
        "bootstrap_doctor.heuristics.last_touched_git_mtime",
        lambda p: None,
    )

    out = shortlist([a, b], cfg)
    assert len(out) == 2
    by_section = {c.section: c for c in out}
    assert "duplicate" in by_section[a].reasons
    assert "duplicate" in by_section[b].reasons
    assert b in by_section[a].duplicate_of
    assert a in by_section[b].duplicate_of


def test_shortlist_preserves_input_order(cfg, tmp_path, monkeypatch) -> None:
    file_a = tmp_path / "A.md"
    file_a.write_text("# A\n")
    big = "x" * (cfg.min_section_chars + 100)
    s1 = make_section(file=file_a, heading="first", body=big, start_line=1)
    s2 = make_section(file=file_a, heading="second", body=big, start_line=20)
    s3 = make_section(file=file_a, heading="third", body=big, start_line=40)

    monkeypatch.setattr(
        "bootstrap_doctor.heuristics.last_touched_git_mtime",
        lambda p: None,
    )
    out = shortlist([s1, s2, s3], cfg)
    assert [c.section.heading_text for c in out] == ["first", "second", "third"]


def test_shortlist_mtime_cached_per_file(cfg, tmp_path) -> None:
    """shortlist should call last_touched_git_mtime once per unique file."""
    file_a = tmp_path / "A.md"
    file_b = tmp_path / "B.md"
    file_a.write_text("# A\n")
    file_b.write_text("# B\n")
    big = "x" * (cfg.min_section_chars + 100)
    s_a1 = make_section(file=file_a, heading="a1", body=big, start_line=1)
    s_a2 = make_section(file=file_a, heading="a2", body=big, start_line=20)
    s_b = make_section(file=file_b, heading="b", body=big, start_line=1)

    calls: list[Path] = []

    def mock_mtime(p: Path) -> int | None:
        calls.append(p)
        return None

    import bootstrap_doctor.heuristics as h
    original = h.last_touched_git_mtime
    h.last_touched_git_mtime = mock_mtime
    try:
        shortlist([s_a1, s_a2, s_b], cfg)
    finally:
        h.last_touched_git_mtime = original

    # Exactly one call per unique file path
    assert sorted(calls) == sorted([file_a, file_b])


def test_shortlist_injected_now_ts_deterministic(cfg, tmp_path, monkeypatch) -> None:
    """Pass in now_ts to make stale detection deterministic in tests."""
    now = 1_700_000_000
    file_a = tmp_path / "A.md"
    file_a.write_text("# A\n")
    s = make_section(file=file_a, heading="h", body="short")
    monkeypatch.setattr(
        "bootstrap_doctor.heuristics.last_touched_git_mtime",
        lambda p: now - (cfg.stale_days + 5) * 86400,
    )
    out = shortlist([s], cfg, now_ts=now)
    assert len(out) == 1
    assert "stale" in out[0].reasons


def test_shortlist_candidate_has_empty_duplicate_of_when_not_duplicate(
    cfg, tmp_path, monkeypatch
) -> None:
    file_a = tmp_path / "A.md"
    file_a.write_text("# A\n")
    big = "x" * (cfg.min_section_chars + 100)
    s = make_section(file=file_a, body=big)
    monkeypatch.setattr(
        "bootstrap_doctor.heuristics.last_touched_git_mtime",
        lambda p: None,
    )
    out = shortlist([s], cfg)
    assert len(out) == 1
    assert out[0].duplicate_of == ()
    assert "large" in out[0].reasons
    assert "duplicate" not in out[0].reasons
