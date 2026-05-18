"""Apply the audit plan: write cards and replace sections with one-line breadcrumbs.

For each ``move`` :class:`bootstrap_doctor.judge.Verdict`, the trim verb:

  1. Synthesizes a new card under ``cfg.cards_dir`` with the standard
     frontmatter convention (``topic`` / ``category`` / ``tags`` /
     ``created`` / ``updated`` plus ``source*`` provenance lines).
  2. Replaces the section body in the original bootstrap file with a
     single breadcrumb line of the form
     ``- See [<topic>](memory/cards/<slug>.md) - <hook>``.
  3. Preserves the heading line verbatim - downstream parsing must still
     pick the section up at the same H2/H3 location, just with less
     content under it.

Mutations are atomic (``safety.atomic_write_text``) and guarded by a
git-clean preflight (overridable with ``force=True``). Dry-run by
default: ``apply_plan(..., apply=False)`` returns a TrimSummary with
``actions_applied=0`` and never touches disk.

Idempotency: at apply time we always re-parse the bootstrap file fresh
and locate the target section by ``heading_path + start_line``. If the
section has shifted or vanished since the plan was built, the action is
skipped, not retried blindly. Re-running the same trim plan after a
successful apply produces a plan where every action skips with
``card already exists`` (since the bootstrap section is now just a
breadcrumb, future heuristic passes won't even reach the judge).
"""
from __future__ import annotations

import datetime as dt
import difflib
from dataclasses import dataclass, field
from pathlib import Path

from .judge import Verdict
from .parsing import Section, parse_file
from .paths import Config
from .safety import (
    UnsafeTargetError,
    assert_git_clean_or_force,
    atomic_write_text,
    resolve_card_target,
    slugify,
)


# How many ``-N`` suffixes we'll try before giving up on a renamed card.
_MAX_RENAME_ATTEMPTS = 10


# --- Public dataclasses -----------------------------------------------------


@dataclass(frozen=True)
class TrimAction:
    """One planned move: write this card, replace that section body."""

    verdict: Verdict
    card_path: Path
    card_body: str
    bootstrap_path: Path
    original_section: Section
    breadcrumb_line: str
    skipped: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class TrimSummary:
    """Outcome of :func:`apply_plan`."""

    actions_planned: int
    actions_applied: int
    skipped: int
    files_changed: tuple[Path, ...]
    cards_written: tuple[Path, ...]


# --- Helpers ----------------------------------------------------------------


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _render_card_body(verdict: Verdict, today_iso: str) -> str:
    """Compose the full card file content (frontmatter + body).

    Optional lines: ``category`` is omitted when empty; ``tags`` is
    omitted when the tuple is empty. Provenance lines (``source*``) are
    always present so future "where did this come from" queries are
    cheap. The body is appended verbatim after a blank line so a
    downstream markdown renderer doesn't merge it into the closing
    frontmatter delimiter.
    """
    section = verdict.section
    heading_path = " > ".join(section.heading_path) if section.heading_path else ""

    lines: list[str] = ["---"]
    lines.append(f"topic: {verdict.topic}")
    if verdict.category:
        lines.append(f"category: {verdict.category}")
    if verdict.tags:
        lines.append(f"tags: [{', '.join(verdict.tags)}]")
    lines.append(f"created: {today_iso}")
    lines.append(f"updated: {today_iso}")
    lines.append("source: bootstrap-doctor")
    lines.append(f"source_file: {section.file.name}")
    lines.append(f"source_heading: {heading_path}")
    lines.append("---")
    lines.append("")
    lines.append(section.body)
    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


def _render_breadcrumb(verdict: Verdict, slug: str) -> str:
    """One-line breadcrumb that replaces the section body.

    Format: ``- See [<topic>](memory/cards/<slug>.md) - <hook>``. Note the
    regular-hyphen separator. No em dashes anywhere in user-facing output
    (see global writing conventions).
    """
    return f"- See [{verdict.topic}](memory/cards/{slug}.md) - {verdict.hook}"


def _derive_slug(verdict: Verdict) -> str | None:
    """Slug from topic, fall back to heading_text, give up otherwise."""
    slug = slugify(verdict.topic)
    if slug:
        return slug
    slug = slugify(verdict.section.heading_text)
    if slug:
        return slug
    return None


def _resolve_or_rename_card_path(
    cfg: Config, slug: str, *, policy: str
) -> tuple[Path, bool, str]:
    """Return ``(path, skipped, reason)`` for a given slug under collision policy.

    ``policy``:
      * ``skip`` (default) - if the original card already exists, mark
        the action as skipped.
      * ``overwrite`` - keep the action; apply will overwrite the card.
      * ``rename`` - try ``slug-2.md``, ``slug-3.md``, ... up to 10
        attempts before skipping.
    """
    try:
        base_path = resolve_card_target(cfg.cards_dir, slug)
    except UnsafeTargetError as exc:
        return cfg.cards_dir / f"{slug}.md", True, f"unsafe card path: {exc}"

    if not base_path.exists():
        return base_path, False, ""

    if policy == "overwrite":
        return base_path, False, ""

    if policy == "rename":
        for i in range(2, _MAX_RENAME_ATTEMPTS + 2):
            candidate_slug = f"{slug}-{i}"
            try:
                candidate = resolve_card_target(cfg.cards_dir, candidate_slug)
            except UnsafeTargetError:
                continue
            if not candidate.exists():
                return candidate, False, ""
        return base_path, True, (
            f"could not find free card slot after {_MAX_RENAME_ATTEMPTS} attempts"
        )

    # Default: skip.
    return base_path, True, f"card already exists: {base_path}"


# --- build_plan -------------------------------------------------------------


def build_plan(
    verdicts: list[Verdict],
    cfg: Config,
    *,
    today_iso: str | None = None,
    existing_card_collision: str = "skip",
) -> list[TrimAction]:
    """Turn a list of judge verdicts into a list of ready-to-apply TrimActions.

    Non-``move`` verdicts are dropped silently (not included in the
    output). Slug collisions within the same plan keep the first
    occurrence and skip later ones with ``duplicate slug in plan``.
    Cross-section collisions on the same heading+file keep the first
    verdict and skip later ones with ``multiple verdicts for same section``.

    The plan is fully synthesized but no disk mutation happens here;
    callers must invoke :func:`apply_plan` with ``apply=True`` to
    persist.
    """
    if today_iso is None:
        today_iso = _today_iso()

    actions: list[TrimAction] = []
    used_slugs: set[str] = set()
    used_sections: set[tuple[Path, tuple[str, ...], int]] = set()

    for verdict in verdicts:
        if verdict.decision != "move":
            continue
        section = verdict.section
        bootstrap_path = section.file

        # Per-section dedupe (same heading_path + start_line in same file).
        section_key = (bootstrap_path, section.heading_path, section.start_line)
        if section_key in used_sections:
            placeholder = TrimAction(
                verdict=verdict,
                card_path=cfg.cards_dir / "_skipped.md",
                card_body="",
                bootstrap_path=bootstrap_path,
                original_section=section,
                breadcrumb_line="",
                skipped=True,
                skip_reason="multiple verdicts for same section",
            )
            actions.append(placeholder)
            continue
        used_sections.add(section_key)

        slug = _derive_slug(verdict)
        if slug is None:
            actions.append(
                TrimAction(
                    verdict=verdict,
                    card_path=cfg.cards_dir / "_skipped.md",
                    card_body="",
                    bootstrap_path=bootstrap_path,
                    original_section=section,
                    breadcrumb_line="",
                    skipped=True,
                    skip_reason="could not derive slug from topic or heading",
                )
            )
            continue

        if slug in used_slugs:
            actions.append(
                TrimAction(
                    verdict=verdict,
                    card_path=cfg.cards_dir / f"{slug}.md",
                    card_body="",
                    bootstrap_path=bootstrap_path,
                    original_section=section,
                    breadcrumb_line="",
                    skipped=True,
                    skip_reason=f"duplicate slug in plan: {slug}",
                )
            )
            continue
        used_slugs.add(slug)

        card_path, skipped, reason = _resolve_or_rename_card_path(
            cfg, slug, policy=existing_card_collision
        )
        if skipped:
            actions.append(
                TrimAction(
                    verdict=verdict,
                    card_path=card_path,
                    card_body="",
                    bootstrap_path=bootstrap_path,
                    original_section=section,
                    breadcrumb_line="",
                    skipped=True,
                    skip_reason=reason,
                )
            )
            continue

        # When renaming bumped the slug we want the breadcrumb to point
        # at the actual file we'll write.
        final_slug = card_path.stem
        card_body = _render_card_body(verdict, today_iso)
        breadcrumb = _render_breadcrumb(verdict, final_slug)

        actions.append(
            TrimAction(
                verdict=verdict,
                card_path=card_path,
                card_body=card_body,
                bootstrap_path=bootstrap_path,
                original_section=section,
                breadcrumb_line=breadcrumb,
            )
        )

    return actions


# --- apply_plan -------------------------------------------------------------


def _find_section_in_fresh_parse(
    fresh_sections: list[Section], target: Section
) -> Section | None:
    """Locate the target section after re-parsing the bootstrap file.

    Match on ``heading_path + start_line``. If line numbers shifted but
    the heading_path is unique, accept that. If nothing matches return
    None and the caller skips the action.
    """
    # Exact heading_path + start_line match.
    for s in fresh_sections:
        if s.heading_path == target.heading_path and s.start_line == target.start_line:
            return s
    # Fallback: unique heading_path match.
    matches = [s for s in fresh_sections if s.heading_path == target.heading_path]
    if len(matches) == 1:
        return matches[0]
    return None


def _replace_section_body(
    text: str, section: Section, breadcrumb: str
) -> str | None:
    """Return ``text`` with ``section``'s body replaced by ``breadcrumb``.

    The heading line itself is preserved verbatim; everything between
    the heading line and the next H2/H3 (or EOF) is replaced with the
    breadcrumb followed by a trailing blank line.

    Returns None if the section's heading line is no longer where the
    Section claimed it was - the file was edited out from under us and
    we'd rather skip than corrupt.
    """
    # Normalize newlines the same way the parser does, so line numbers
    # line up with what we observed at parse time.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    # Strip the empty final element from a trailing-newline split, so
    # indexing matches parse_text's 1-indexed positions.
    had_trailing_newline = normalized.endswith("\n")
    if had_trailing_newline and lines and lines[-1] == "":
        lines.pop()

    heading_idx = section.start_line - 1  # 0-indexed
    if heading_idx < 0 or heading_idx >= len(lines):
        return None

    heading_line = lines[heading_idx]
    # Sanity: the heading line should still start with "## " or "### ".
    if not (heading_line.startswith("## ") or heading_line.startswith("### ")):
        return None

    # Find the next H2/H3 boundary, code-fence-aware.
    in_fence = False
    end_idx = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        ln = lines[j]
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ln.startswith("## ") or ln.startswith("### "):
            end_idx = j
            break

    # Build the replacement: heading + blank + breadcrumb + blank,
    # then resume with whatever followed the section.
    new_lines = lines[: heading_idx + 1] + ["", breadcrumb, ""] + lines[end_idx:]
    rebuilt = "\n".join(new_lines)
    if had_trailing_newline and not rebuilt.endswith("\n"):
        rebuilt += "\n"
    return rebuilt


def apply_plan(
    actions: list[TrimAction],
    cfg: Config,
    *,
    apply: bool = False,
    force: bool = False,
) -> TrimSummary:
    """Persist ``actions`` to disk (when ``apply=True``).

    Dry-run mode (default) returns a summary with ``actions_applied=0``
    and never touches disk. Apply mode runs a git-clean preflight
    (skippable via ``force=True``), then for each non-skipped action
    writes the card and rewrites the source bootstrap file.

    Bootstrap rewrites are grouped per file and processed in REVERSE
    ``start_line`` order so that earlier line offsets don't shift while
    we're still working on later sections in the same file.
    """
    planned = len(actions)
    pre_skipped = sum(1 for a in actions if a.skipped)

    if not apply:
        return TrimSummary(
            actions_planned=planned,
            actions_applied=0,
            skipped=pre_skipped,
            files_changed=(),
            cards_written=(),
        )

    assert_git_clean_or_force(cfg.workspace_dir, force)

    # Group live (non-skipped) actions by bootstrap file.
    live = [a for a in actions if not a.skipped]
    by_file: dict[Path, list[TrimAction]] = {}
    for a in live:
        by_file.setdefault(a.bootstrap_path, []).append(a)

    files_changed: list[Path] = []
    cards_written: list[Path] = []
    applied = 0
    runtime_skipped = 0

    # Track which actions survived the freshness check, by id() of the
    # original TrimAction object; we use this to decide whether to write
    # the card or not after the bootstrap rewrite.
    survived: dict[int, Path] = {}

    for bootstrap_path, file_actions in by_file.items():
        if not bootstrap_path.exists():
            # Whole file vanished; skip every action targeting it.
            runtime_skipped += len(file_actions)
            continue

        try:
            current_text = bootstrap_path.read_text(encoding="utf-8")
        except OSError:
            runtime_skipped += len(file_actions)
            continue

        fresh_sections = parse_file(bootstrap_path)

        # Process in REVERSE start_line order so earlier edits don't
        # shift the line offsets of later edits in the same file.
        ordered = sorted(
            file_actions, key=lambda a: a.original_section.start_line, reverse=True
        )

        new_text = current_text
        any_change = False
        for action in ordered:
            fresh = _find_section_in_fresh_parse(
                fresh_sections, action.original_section
            )
            if fresh is None:
                runtime_skipped += 1
                continue
            rebuilt = _replace_section_body(
                new_text, fresh, action.breadcrumb_line
            )
            if rebuilt is None:
                runtime_skipped += 1
                continue
            new_text = rebuilt
            survived[id(action)] = action.card_path
            any_change = True
            # We process in REVERSE start_line order, so editing this
            # section does not shift the line offsets of any earlier
            # section. `fresh_sections` stays valid for the next loop
            # iteration; no re-parse needed.

        if any_change:
            atomic_write_text(bootstrap_path, new_text)
            files_changed.append(bootstrap_path)

    # Now write the cards for actions that survived the freshness check.
    for action in live:
        target = survived.get(id(action))
        if target is None:
            continue
        atomic_write_text(target, action.card_body)
        cards_written.append(target)
        applied += 1

    return TrimSummary(
        actions_planned=planned,
        actions_applied=applied,
        skipped=pre_skipped + runtime_skipped,
        files_changed=tuple(files_changed),
        cards_written=tuple(cards_written),
    )


# --- render_plan ------------------------------------------------------------


def _rel_to_workspace(cfg: Config, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(cfg.workspace_dir.resolve()))
    except ValueError:
        return str(path)


def _projected_file_text(
    actions: list[TrimAction], bootstrap_path: Path
) -> tuple[str, str]:
    """Return ``(current_text, projected_text)`` for diff purposes.

    Skipped actions don't contribute. Order of application matches
    :func:`apply_plan` (reverse start_line).
    """
    try:
        current = bootstrap_path.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    live = [
        a
        for a in actions
        if not a.skipped and a.bootstrap_path == bootstrap_path
    ]
    if not live:
        return current, current
    text = current
    ordered = sorted(
        live, key=lambda a: a.original_section.start_line, reverse=True
    )
    for action in ordered:
        rebuilt = _replace_section_body(
            text, action.original_section, action.breadcrumb_line
        )
        if rebuilt is None:
            continue
        text = rebuilt
    return current, text


def render_plan(actions: list[TrimAction], cfg: Config) -> str:
    """Human-readable preview: NEW CARD blocks + unified diffs + footer.

    Output sections:
      1. One ``NEW CARD`` block per non-skipped action.
      2. One unified-diff block per affected bootstrap file.
      3. One ``SKIPPED`` block listing every skipped action and its reason.
      4. A summary footer: actions planned, would write N cards, would
         modify N bootstrap files, M skipped.
    """
    out: list[str] = []
    live = [a for a in actions if not a.skipped]
    skipped = [a for a in actions if a.skipped]

    # NEW CARD blocks (one per live action).
    for action in live:
        verdict = action.verdict
        body_preview = verdict.section.body.replace("\n", " ").strip()
        if len(body_preview) > 80:
            body_preview = body_preview[:80] + "..."
        out.append(
            f"NEW CARD: memory/cards/{action.card_path.name}\n"
            f"  topic: {verdict.topic}\n"
            f"  category: {verdict.category or '(none)'}\n"
            f"  body: {body_preview}"
        )

    if live:
        out.append("")

    # Unified diff per affected bootstrap file.
    affected: list[Path] = []
    for action in live:
        if action.bootstrap_path not in affected:
            affected.append(action.bootstrap_path)
    for bootstrap_path in affected:
        current, projected = _projected_file_text(actions, bootstrap_path)
        if current == projected:
            continue
        rel = _rel_to_workspace(cfg, bootstrap_path)
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            projected.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
        diff_text = "".join(diff)
        if diff_text:
            out.append(diff_text.rstrip("\n"))
            out.append("")

    # Skipped block.
    if skipped:
        out.append("SKIPPED:")
        for action in skipped:
            topic = action.verdict.topic or action.original_section.heading_text
            out.append(f"  - {topic}: {action.skip_reason}")
        out.append("")

    # Footer.
    n_planned = len(actions)
    n_cards = len(live)
    n_files = len(affected)
    n_skipped = len(skipped)
    out.append(
        f"Summary: {n_planned} actions planned, would write {n_cards} card(s), "
        f"would modify {n_files} bootstrap file(s), {n_skipped} skipped."
    )

    return "\n".join(out)
