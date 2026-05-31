"""Shortlist rules: size, age, code-block bulk, and cross-file duplicate detection.

Given a list of :class:`bootstrap_doctor.parsing.Section` records spanning every
tracked bootstrap file, :func:`shortlist` returns the subset that look like
plausible move-to-card candidates. The LLM judge in ``judge.py`` makes the
final call; these heuristics just narrow the field deterministically.

Triggers ("reasons"):

  * ``"large"``         - section body is over ``cfg.min_section_chars``.
  * ``"long-code-block"`` - section contains at least one fenced code block
    with more than 10 content lines (fence markers themselves excluded).
  * ``"stale"``         - the file's last git commit touching it is older
    than ``cfg.stale_days`` days. Sections in files with no git mtime are
    NOT considered stale (we cannot judge).
  * ``"duplicate"``     - section body is >= 0.80 cosine-ish similarity
    (``difflib.SequenceMatcher.ratio``) with at least one OTHER section,
    measured on normalized text (lowercase + whitespace collapse).

Preamble sections (``heading_level == 0``) are excluded entirely; they
rarely move cleanly to a card.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .parsing import Section, last_touched_git_mtime
from .paths import Config

# Constants (no config knobs in v1) ---------------------------------------

#: A fenced code block must exceed this many content lines (lines BETWEEN
#: the opening and closing ```) to count as "long".
DEFAULT_LONG_CODE_BLOCK_LINES = 10

#: Pairwise similarity threshold for the duplicate heuristic. Tuned by hand
#: against real bootstrap content; below 0.80 we picked up coincidental
#: phrasing matches (two unrelated "## Recent Session" sections sharing
#: stock prose). Above 0.85 we missed paraphrased copies. 0.80 is the
#: balance point. Kept as a module constant rather than a config knob so
#: the heuristic stays predictable across runs.
DEFAULT_DUPLICATE_THRESHOLD = 0.80

#: Sections shorter than this are not compared for duplicates. Short
#: bodies hit accidental high-ratio matches against each other ("see
#: above", "TODO", boilerplate), so we exclude them rather than pollute
#: the shortlist with noise.
DEFAULT_DUPLICATE_MIN_CHARS = 100


# Public dataclass --------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A section the heuristics flagged for the judge to consider."""

    section: Section
    reasons: tuple[str, ...]
    duplicate_of: tuple[Section, ...] = field(default=())


# Individual rules --------------------------------------------------------


def is_large(section: Section, cfg: Config) -> bool:
    """True iff ``section.char_count > cfg.min_section_chars`` (strict)."""
    return section.char_count > cfg.min_section_chars


# Matches the opening of a fence: ``` optionally followed by a language tag.
# We don't capture the language; we only care about open/close transitions.
_FENCE_RE = re.compile(r"^\s*```")


def has_long_code_block(
    section: Section, min_lines: int = DEFAULT_LONG_CODE_BLOCK_LINES
) -> bool:
    """True iff any single fenced code block in ``section.body`` has more
    than ``min_lines`` content lines.

    Content lines = lines strictly between the opening ``` and closing ```.
    Two short blocks do NOT aggregate; one block must clear the bar.
    An unclosed fence (no matching ``` after the open) does not count.
    """
    if not section.body:
        return False
    in_fence = False
    current_block_lines = 0
    for raw in section.body.split("\n"):
        if _FENCE_RE.match(raw):
            if in_fence:
                # Closing fence: check the block we just finished.
                if current_block_lines > min_lines:
                    return True
                in_fence = False
                current_block_lines = 0
            else:
                in_fence = True
                current_block_lines = 0
            continue
        if in_fence:
            current_block_lines += 1
    # If we ended still inside a fence the block was never closed; do not
    # honor it. A malformed unclosed fence is the parser's problem, not
    # ours.
    return False


def is_stale(
    section: Section,
    cfg: Config,
    *,
    last_touched_ts: int | None,
    now_ts: int | None = None,
) -> bool:
    """True iff ``last_touched_ts`` is older than ``cfg.stale_days`` days.

    ``last_touched_ts`` is the UNIX timestamp of the last git commit
    touching ``section.file``; the caller computes it once per file and
    passes it in (see :func:`shortlist`). If it is ``None`` we return
    ``False`` - we cannot judge an untracked or unknown file.

    ``now_ts`` defaults to ``time.time()`` but is injectable for
    deterministic tests. Comparison is strict greater-than: a section
    exactly ``stale_days`` old is not yet stale.
    """
    if last_touched_ts is None:
        return False
    if now_ts is None:
        now_ts = int(time.time())
    age_seconds = now_ts - last_touched_ts
    return age_seconds > cfg.stale_days * 86400


# Duplicate detection -----------------------------------------------------


_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _normalize_body(text: str) -> str:
    """Lowercase, strip outer whitespace, collapse internal whitespace runs.

    Used for the ``SequenceMatcher`` ratio so two bodies that differ only
    in formatting (case, extra blank lines, inconsistent indent) still
    register as duplicates.
    """
    return _WHITESPACE_RUN_RE.sub(" ", text.lower()).strip()


def find_duplicates(
    sections: list[Section],
    *,
    similarity_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    min_chars: int = DEFAULT_DUPLICATE_MIN_CHARS,
) -> dict[Section, list[Section]]:
    """For each section, return the list of OTHER sections with a body
    similarity at or above ``similarity_threshold``.

    The output dict has an entry for every section in the input (empty
    list when no duplicates were found), which lets callers ``dups[s]``
    without a ``KeyError``. Preamble sections and sections under
    ``min_chars`` are excluded from BOTH sides of every comparison
    (they appear in the output with an empty list).
    """
    result: dict[Section, list[Section]] = {s: [] for s in sections}

    # Pre-normalize bodies once and pre-filter eligible sections.
    eligible: list[tuple[Section, str]] = []
    for s in sections:
        if s.heading_level == 0:
            continue
        if len(s.body) < min_chars:
            continue
        eligible.append((s, _normalize_body(s.body)))

    for i, (a, body_a) in enumerate(eligible):
        for b, body_b in eligible[i + 1 :]:
            ratio = SequenceMatcher(None, body_a, body_b).ratio()
            if ratio >= similarity_threshold:
                result[a].append(b)
                result[b].append(a)

    return result


# Shortlist ---------------------------------------------------------------


def shortlist(
    sections: list[Section],
    cfg: Config,
    *,
    now_ts: int | None = None,
) -> list[Candidate]:
    """Run every heuristic over ``sections`` and return triggered candidates.

    Behavior:
      1. Cache ``last_touched_git_mtime`` per unique file (one subprocess
         per file, not per section).
      2. For each non-preamble section, collect reasons in the order
         ``["large", "long-code-block", "stale"]``.
      3. Run :func:`find_duplicates` once over the whole input; any section
         with at least one duplicate gets ``"duplicate"`` appended and the
         duplicate list captured in ``duplicate_of``.
      4. Drop sections with zero reasons and preserve input order in the
         output.
    """
    # Cache mtimes one call per unique file.
    file_mtimes: dict[Path, int | None] = {}
    for s in sections:
        if s.file not in file_mtimes:
            file_mtimes[s.file] = last_touched_git_mtime(s.file)

    # Per-section reasons (excluding "duplicate", which we add below).
    per_section_reasons: dict[Section, list[str]] = {}
    for s in sections:
        if s.heading_level == 0:
            continue
        reasons: list[str] = []
        if is_large(s, cfg):
            reasons.append("large")
        if has_long_code_block(s):
            reasons.append("long-code-block")
        if is_stale(s, cfg, last_touched_ts=file_mtimes[s.file], now_ts=now_ts):
            reasons.append("stale")
        per_section_reasons[s] = reasons

    # Duplicate pass over the whole input (preamble and short sections
    # excluded internally).
    dups = find_duplicates(sections)

    candidates: list[Candidate] = []
    for s in sections:
        if s.heading_level == 0:
            continue
        reasons = list(per_section_reasons.get(s, []))
        dup_list = dups.get(s, [])
        if dup_list:
            reasons.append("duplicate")
        if not reasons:
            continue
        candidates.append(
            Candidate(
                section=s,
                reasons=tuple(reasons),
                duplicate_of=tuple(dup_list),
            )
        )
    return candidates
