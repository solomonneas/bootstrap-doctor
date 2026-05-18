"""Tests for trim.py: build move-plan + apply card writes + breadcrumb in-place."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bootstrap_doctor.judge import Verdict
from bootstrap_doctor.parsing import Section, parse_file
from bootstrap_doctor.paths import Config, resolve_config
from bootstrap_doctor.safety import DirtyWorkspaceError
from bootstrap_doctor import safety as safety_mod
from bootstrap_doctor import trim as trim_mod
from bootstrap_doctor.trim import (
    CardWriteError,
    TrimAction,
    TrimSummary,
    apply_plan,
    build_plan,
    render_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


TODAY = "2026-05-18"


def make_section(
    file: Path,
    heading: str = "Old Setup",
    body: str = "some body content\nmore content",
    level: int = 2,
    heading_path: tuple[str, ...] | None = None,
    start_line: int = 1,
    end_line: int | None = None,
) -> Section:
    if heading_path is None:
        heading_path = (heading,)
    if end_line is None:
        end_line = start_line + (body.count("\n") if body else 0)
    return Section(
        file=file,
        heading_level=level,
        heading_text=heading,
        heading_path=heading_path,
        body=body,
        char_count=len(body),
        line_count=body.count("\n") + 1 if body else 0,
        start_line=start_line,
        end_line=end_line,
    )


def make_verdict(
    section: Section,
    decision: str = "move",
    topic: str = "Old Setup Notes",
    category: str = "session-log",
    tags: tuple[str, ...] = ("setup", "old"),
    hook: str = "Notes about the old setup process.",
    reasoning: str = "historical content",
) -> Verdict:
    return Verdict(
        section=section,
        decision=decision,
        topic=topic,
        category=category,
        tags=tags,
        hook=hook,
        reasoning=reasoning,
        source="gateway",
        body_sha="x" * 64,
    )


@pytest.fixture
def cfg(tmp_path: Path, workspace_dir: Path, cards_dir: Path) -> Config:
    cache_dir = tmp_path / "cache"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'''
workspace_dir = "{workspace_dir}"
cards_dir = "{cards_dir}"
gateway_url = "http://localhost:18789"
gateway_model = "test-model"

[cache]
dir = "{cache_dir}"
'''
    )
    return resolve_config(config_file=str(config_file))


def write_bootstrap(workspace_dir: Path, name: str, body: str) -> Path:
    path = workspace_dir / name
    path.write_text(body)
    return path


def init_clean_git(workspace_dir: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=workspace_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "config", "commit.gpgsign", "false"],
        cwd=workspace_dir,
        check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=workspace_dir, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=workspace_dir,
        check=True,
    )


# ---------------------------------------------------------------------------
# build_plan: single move verdict
# ---------------------------------------------------------------------------


def test_build_plan_skips_non_move_verdicts(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Foo\nbody\n")
    sec = make_section(bs)
    keep = make_verdict(sec, decision="keep", topic="", category="", tags=(), hook="")
    unsure = make_verdict(sec, decision="unsure", topic="", category="", tags=(), hook="")
    plan = build_plan([keep, unsure], cfg, today_iso=TODAY)
    assert plan == []


def test_build_plan_single_move_action_shape(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(
        workspace_dir,
        "AGENTS.md",
        "## Old Setup\nsome body content\nmore content\n",
    )
    sec = make_section(bs, body="some body content\nmore content", start_line=1, end_line=3)
    v = make_verdict(sec)
    plan = build_plan([v], cfg, today_iso=TODAY)
    assert len(plan) == 1
    action = plan[0]
    assert action.verdict is v
    assert action.bootstrap_path == bs
    assert action.original_section is sec
    assert action.skipped is False
    # Card path is inside cards_dir, slugified from topic.
    assert action.card_path.parent == cfg.cards_dir
    assert action.card_path.name == "old-setup-notes.md"
    # Card body has the required frontmatter keys.
    body = action.card_body
    assert body.startswith("---\n")
    assert "topic: Old Setup Notes" in body
    assert "category: session-log" in body
    assert "tags: [setup, old]" in body
    assert f"created: {TODAY}" in body
    assert f"updated: {TODAY}" in body
    assert "source: bootstrap-doctor" in body
    assert "source_file: AGENTS.md" in body
    assert "source_heading: Old Setup" in body
    # Body content is preserved verbatim after the frontmatter.
    assert "some body content\nmore content" in body
    # Breadcrumb is a single line with the correct shape.
    assert action.breadcrumb_line == (
        "- See [Old Setup Notes](memory/cards/old-setup-notes.md)"
        " - Notes about the old setup process."
    )


def test_card_body_has_required_frontmatter_keys(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Old\nbody\n")
    sec = make_section(bs, body="body")
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    body = plan[0].card_body
    # Frontmatter delimited by --- on its own line.
    assert body.startswith("---\n")
    second_delim = body.index("\n---\n", 4)
    fm = body[4:second_delim]
    for key in ("topic:", "category:", "tags:", "created:", "updated:"):
        assert key in fm
    # Trailing newline at end of file (markdown hygiene).
    assert body.endswith("\n")


def test_card_body_omits_empty_category_line(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Old\nbody\n")
    sec = make_section(bs, body="body")
    v = make_verdict(sec, category="")
    plan = build_plan([v], cfg, today_iso=TODAY)
    body = plan[0].card_body
    assert "category:" not in body
    assert "topic:" in body  # other keys still present


def test_card_body_omits_empty_tags_line(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Old\nbody\n")
    sec = make_section(bs, body="body")
    v = make_verdict(sec, tags=())
    plan = build_plan([v], cfg, today_iso=TODAY)
    body = plan[0].card_body
    assert "tags:" not in body


# ---------------------------------------------------------------------------
# build_plan: slug derivation + collisions
# ---------------------------------------------------------------------------


def test_slug_falls_back_to_heading_when_topic_slugs_empty(
    cfg: Config, workspace_dir: Path
) -> None:
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Real Heading\nbody\n")
    sec = make_section(bs, heading="Real Heading", body="body")
    # Topic is all punctuation; slugify returns "" so fallback to heading.
    v = make_verdict(sec, topic="!!!")
    plan = build_plan([v], cfg, today_iso=TODAY)
    assert len(plan) == 1
    action = plan[0]
    assert action.skipped is False
    assert action.card_path.name == "real-heading.md"


def test_slug_skip_when_topic_and_heading_both_empty(
    cfg: Config, workspace_dir: Path
) -> None:
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## !!!\nbody\n")
    sec = make_section(bs, heading="!!!", body="body")
    v = make_verdict(sec, topic="???")
    plan = build_plan([v], cfg, today_iso=TODAY)
    assert len(plan) == 1
    assert plan[0].skipped is True
    assert "could not derive slug" in plan[0].skip_reason


def test_duplicate_slug_in_plan_second_skipped(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(
        workspace_dir, "AGENTS.md", "## Old Setup\nbody1\n## Old Setup\nbody2\n"
    )
    s1 = make_section(bs, heading="Old Setup", body="body1", start_line=1)
    s2 = make_section(bs, heading="Old Setup", body="body2", start_line=3)
    v1 = make_verdict(s1)
    v2 = make_verdict(s2)
    plan = build_plan([v1, v2], cfg, today_iso=TODAY)
    assert len(plan) == 2
    assert plan[0].skipped is False
    assert plan[1].skipped is True
    assert "duplicate slug" in plan[1].skip_reason


def test_existing_card_default_skip(cfg: Config, workspace_dir: Path) -> None:
    # Pre-create the target card on disk.
    (cfg.cards_dir / "old-setup-notes.md").write_text("existing\n")
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Old\nbody\n")
    sec = make_section(bs, body="body")
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    assert plan[0].skipped is True
    assert "already exists" in plan[0].skip_reason


def test_existing_card_overwrite(cfg: Config, workspace_dir: Path) -> None:
    (cfg.cards_dir / "old-setup-notes.md").write_text("existing\n")
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Old\nbody\n")
    sec = make_section(bs, body="body")
    plan = build_plan(
        [make_verdict(sec)], cfg, today_iso=TODAY, existing_card_collision="overwrite"
    )
    assert plan[0].skipped is False
    assert plan[0].card_path.name == "old-setup-notes.md"


def test_existing_card_rename_appends_suffix(cfg: Config, workspace_dir: Path) -> None:
    (cfg.cards_dir / "old-setup-notes.md").write_text("existing\n")
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Old\nbody\n")
    sec = make_section(bs, body="body")
    plan = build_plan(
        [make_verdict(sec)], cfg, today_iso=TODAY, existing_card_collision="rename"
    )
    assert plan[0].skipped is False
    assert plan[0].card_path.name == "old-setup-notes-2.md"


def test_existing_card_rename_skips_after_max_attempts(
    cfg: Config, workspace_dir: Path
) -> None:
    # Saturate 1..10 (the original + 9 -N suffixes through -10).
    (cfg.cards_dir / "old-setup-notes.md").write_text("x\n")
    for i in range(2, 12):
        (cfg.cards_dir / f"old-setup-notes-{i}.md").write_text("x\n")
    bs = write_bootstrap(workspace_dir, "TOOLS.md", "## Old\nbody\n")
    sec = make_section(bs, body="body")
    plan = build_plan(
        [make_verdict(sec)], cfg, today_iso=TODAY, existing_card_collision="rename"
    )
    assert plan[0].skipped is True


# ---------------------------------------------------------------------------
# apply_plan: dry-run
# ---------------------------------------------------------------------------


def test_apply_plan_dry_run_changes_nothing(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(
        workspace_dir, "AGENTS.md", "## Old Setup\nsome body content\n"
    )
    original = bs.read_text()
    sec = make_section(bs, body="some body content")
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    summary = apply_plan(plan, cfg, apply=False)
    assert summary.actions_planned == 1
    assert summary.actions_applied == 0
    assert summary.files_changed == ()
    assert summary.cards_written == ()
    # No card on disk, bootstrap untouched.
    assert not (cfg.cards_dir / "old-setup-notes.md").exists()
    assert bs.read_text() == original


# ---------------------------------------------------------------------------
# apply_plan: real apply path (no git dirty check for in-tmp tests)
# ---------------------------------------------------------------------------


def test_apply_plan_writes_card_and_breadcrumb(
    cfg: Config, workspace_dir: Path
) -> None:
    bs_text = "## Old Setup\nsome body content\nmore content\n"
    bs = write_bootstrap(workspace_dir, "AGENTS.md", bs_text)
    sections = parse_file(bs)
    sec = next(s for s in sections if s.heading_text == "Old Setup")
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    summary = apply_plan(plan, cfg, apply=True, force=True)
    assert summary.actions_planned == 1
    assert summary.actions_applied == 1
    assert summary.skipped == 0
    assert bs in summary.files_changed
    card_path = cfg.cards_dir / "old-setup-notes.md"
    assert card_path in summary.cards_written
    # Card content matches what the plan said it would.
    card_text = card_path.read_text()
    assert "topic: Old Setup Notes" in card_text
    assert "some body content\nmore content" in card_text
    # Bootstrap now has the breadcrumb line and the original heading.
    bs_after = bs.read_text()
    assert "## Old Setup" in bs_after
    assert "- See [Old Setup Notes](memory/cards/old-setup-notes.md)" in bs_after
    # And the original body lines are gone.
    assert "some body content" not in bs_after.split("- See")[0]


def test_apply_preserves_heading_line_verbatim(
    cfg: Config, workspace_dir: Path
) -> None:
    # Heading with trailing closure hashes; parser normalizes the text but
    # the raw line must survive on disk.
    bs_text = "## Old Setup ##\nbody\n"
    bs = write_bootstrap(workspace_dir, "TOOLS.md", bs_text)
    sections = parse_file(bs)
    sec = sections[0]
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    apply_plan(plan, cfg, apply=True, force=True)
    bs_after = bs.read_text()
    assert bs_after.splitlines()[0] == "## Old Setup ##"


def test_apply_creates_card_parent_dirs(tmp_path: Path, workspace_dir: Path) -> None:
    # Use a config that points cards_dir at a NOT-YET-EXISTING dir under workspace.
    # The parent (`memory/`) must exist for `allow_missing_cards` to accept it.
    (workspace_dir / "memory").mkdir(parents=True, exist_ok=True)
    cards_dir = workspace_dir / "memory" / "cards-new"
    cache_dir = tmp_path / "cache"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'''
workspace_dir = "{workspace_dir}"
cards_dir = "{cards_dir}"
gateway_url = "http://localhost:18789"
gateway_model = "test-model"

[cache]
dir = "{cache_dir}"
'''
    )
    cfg2 = resolve_config(config_file=str(config_file), allow_missing_cards=True)
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old\nbody\n")
    sec = make_section(bs, heading="Old", body="body")
    plan = build_plan([make_verdict(sec)], cfg2, today_iso=TODAY)
    apply_plan(plan, cfg2, apply=True, force=True)
    # Parent dirs should have been created by atomic_write_text.
    assert cards_dir.exists()
    assert any(cards_dir.iterdir())


def test_multiple_actions_same_file_processed_in_reverse_line_order(
    cfg: Config, workspace_dir: Path
) -> None:
    text = (
        "## First\n"
        "first body line 1\n"
        "first body line 2\n"
        "\n"
        "## Second\n"
        "second body line 1\n"
        "second body line 2\n"
        "second body line 3\n"
        "\n"
        "## Third\n"
        "third body line 1\n"
    )
    bs = write_bootstrap(workspace_dir, "AGENTS.md", text)
    sections = parse_file(bs)
    secs = {s.heading_text: s for s in sections}
    # Move First and Third; leave Second alone.
    v1 = make_verdict(
        secs["First"],
        topic="First Topic",
        hook="first hook",
        tags=(),
        category="",
    )
    v3 = make_verdict(
        secs["Third"],
        topic="Third Topic",
        hook="third hook",
        tags=(),
        category="",
    )
    plan = build_plan([v1, v3], cfg, today_iso=TODAY)
    summary = apply_plan(plan, cfg, apply=True, force=True)
    assert summary.actions_applied == 2
    bs_after = bs.read_text()
    # Both headings still there.
    assert "## First" in bs_after
    assert "## Second" in bs_after
    assert "## Third" in bs_after
    # Second's body preserved.
    assert "second body line 1" in bs_after
    assert "second body line 2" in bs_after
    assert "second body line 3" in bs_after
    # First and Third bodies replaced by breadcrumbs.
    assert "first body line 1" not in bs_after
    assert "third body line 1" not in bs_after
    assert "- See [First Topic]" in bs_after
    assert "- See [Third Topic]" in bs_after


def test_apply_dirty_git_check_blocks_without_force(
    cfg: Config, workspace_dir: Path
) -> None:
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old\nbody\n")
    init_clean_git(workspace_dir)
    # Make workspace dirty.
    (workspace_dir / "AGENTS.md").write_text("## Old\nbody\nedited\n")
    sec = make_section(bs, heading="Old", body="body")
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    with pytest.raises(DirtyWorkspaceError):
        apply_plan(plan, cfg, apply=True, force=False)


def test_apply_dirty_git_force_bypasses(cfg: Config, workspace_dir: Path) -> None:
    bs_text = "## Old\nbody\n"
    bs = write_bootstrap(workspace_dir, "AGENTS.md", bs_text)
    init_clean_git(workspace_dir)
    (workspace_dir / "AGENTS.md").write_text("## Old\nbody\nedited\n")
    sections = parse_file(bs)
    sec = sections[0]
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    summary = apply_plan(plan, cfg, apply=True, force=True)
    assert summary.actions_applied == 1


# ---------------------------------------------------------------------------
# Idempotency + edge cases
# ---------------------------------------------------------------------------


def test_idempotent_rerun_after_apply_skips_everything(
    cfg: Config, workspace_dir: Path
) -> None:
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old Setup\nbody content\n")
    sections = parse_file(bs)
    sec = sections[0]
    v = make_verdict(sec)
    plan = build_plan([v], cfg, today_iso=TODAY)
    apply_plan(plan, cfg, apply=True, force=True)
    # Re-run with the same verdict. Card exists -> default collision policy skips.
    plan2 = build_plan([v], cfg, today_iso=TODAY)
    assert len(plan2) == 1
    assert plan2[0].skipped is True
    assert "already exists" in plan2[0].skip_reason


def test_source_file_missing_at_apply_time(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old\nbody\n")
    sec = make_section(bs, heading="Old", body="body")
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    bs.unlink()  # remove the file after the plan was built
    summary = apply_plan(plan, cfg, apply=True, force=True)
    assert summary.actions_applied == 0
    assert summary.skipped == 1
    # Card NOT written when source vanished.
    assert not (cfg.cards_dir / "old-setup-notes.md").exists()


def test_section_changed_since_plan_built(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old Setup\nbody content\n")
    sections = parse_file(bs)
    sec = sections[0]
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    # Rewrite the bootstrap so the heading no longer exists at that position.
    bs.write_text("## Totally Different\nnew body\n")
    summary = apply_plan(plan, cfg, apply=True, force=True)
    assert summary.actions_applied == 0
    assert summary.skipped == 1


def test_multiple_verdicts_for_same_section_first_wins(
    cfg: Config, workspace_dir: Path
) -> None:
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old Setup\nbody\n")
    sec = make_section(bs, body="body")
    v1 = make_verdict(sec, topic="First Topic", hook="first hook")
    v2 = make_verdict(sec, topic="Second Topic", hook="second hook")
    plan = build_plan([v1, v2], cfg, today_iso=TODAY)
    assert len(plan) == 2
    assert plan[0].skipped is False
    assert plan[1].skipped is True
    assert (
        "multiple verdicts" in plan[1].skip_reason
        or "duplicate slug" in plan[1].skip_reason
    )


# ---------------------------------------------------------------------------
# render_plan
# ---------------------------------------------------------------------------


def test_render_plan_lists_new_cards_and_diffs(cfg: Config, workspace_dir: Path) -> None:
    bs = write_bootstrap(
        workspace_dir, "AGENTS.md", "## Old Setup\nsome body content\n"
    )
    sections = parse_file(bs)
    sec = sections[0]
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    out = render_plan(plan, cfg)
    # NEW CARD block lists the slug, topic, category.
    assert "NEW CARD" in out
    assert "old-setup-notes.md" in out
    assert "Old Setup Notes" in out
    assert "session-log" in out
    # Diff section shows the breadcrumb addition.
    assert "--- a/AGENTS.md" in out
    assert "+++ b/AGENTS.md" in out
    assert "+- See [Old Setup Notes]" in out
    # Footer counts.
    assert "planned" in out.lower() or "actions" in out.lower()


def test_render_plan_lists_skipped_actions(cfg: Config, workspace_dir: Path) -> None:
    (cfg.cards_dir / "old-setup-notes.md").write_text("existing\n")
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old Setup\nbody\n")
    sec = make_section(bs, body="body")
    plan = build_plan([make_verdict(sec)], cfg, today_iso=TODAY)
    out = render_plan(plan, cfg)
    assert "SKIPPED" in out
    assert "already exists" in out


def test_render_plan_summary_footer_counts(cfg: Config, workspace_dir: Path) -> None:
    bs1 = write_bootstrap(workspace_dir, "AGENTS.md", "## A\nbody a\n")
    bs2 = write_bootstrap(workspace_dir, "TOOLS.md", "## B\nbody b\n")
    s1 = make_section(bs1, heading="A", body="body a")
    s2 = make_section(bs2, heading="B", body="body b")
    v1 = make_verdict(s1, topic="Topic A", hook="hook a")
    v2 = make_verdict(s2, topic="Topic B", hook="hook b")
    plan = build_plan([v1, v2], cfg, today_iso=TODAY)
    out = render_plan(plan, cfg)
    # Footer mentions both cards.
    assert "2" in out  # two cards, two files


# ---------------------------------------------------------------------------
# IMPORTANT 6: git-clean check covers cards_dir repo when it is separate
# ---------------------------------------------------------------------------


def test_apply_blocks_on_dirty_cards_repo_separate_from_workspace(
    tmp_path: Path,
) -> None:
    """When cards_dir lives in its own git repo, a dirty state there
    must block apply just like a dirty workspace repo does."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cards = tmp_path / "cards-repo"
    cards.mkdir()
    cache = tmp_path / "cache"

    (ws / "AGENTS.md").write_text("## Old\nbody\n")
    init_clean_git(ws)
    # Cards lives in its own repo.
    (cards / "README.md").write_text("just a marker\n")
    init_clean_git(cards)
    # Make the cards repo dirty AFTER the initial commit.
    (cards / "README.md").write_text("just a marker\nedited\n")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'''
workspace_dir = "{ws}"
cards_dir = "{cards}"
gateway_url = "http://localhost:18789"
gateway_model = "test-model"

[cache]
dir = "{cache}"
'''
    )
    cfg2 = resolve_config(config_file=str(cfg_path))

    sections = parse_file(ws / "AGENTS.md")
    sec = sections[0]
    plan = build_plan([make_verdict(sec)], cfg2, today_iso=TODAY)
    with pytest.raises(DirtyWorkspaceError) as exc:
        apply_plan(plan, cfg2, apply=True, force=False)
    # Error mentions the cards repo so the operator knows which to clean.
    assert str(cards) in str(exc.value) or "cards" in str(exc.value).lower()


def test_apply_warns_when_cards_dir_not_in_any_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cards-dir outside any git repo: warn, don't abort.

    Untracked-but-non-git cards directories are a valid setup (e.g.,
    centralized cards under a non-repo path).
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    cards = tmp_path / "non-repo-cards"
    cards.mkdir()
    cache = tmp_path / "cache"

    (ws / "AGENTS.md").write_text("## Old\nbody\n")
    init_clean_git(ws)

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'''
workspace_dir = "{ws}"
cards_dir = "{cards}"
gateway_url = "http://localhost:18789"
gateway_model = "test-model"

[cache]
dir = "{cache}"
'''
    )
    cfg2 = resolve_config(config_file=str(cfg_path))

    sections = parse_file(ws / "AGENTS.md")
    sec = sections[0]
    plan = build_plan([make_verdict(sec)], cfg2, today_iso=TODAY)
    # Should NOT raise.
    summary = apply_plan(plan, cfg2, apply=True, force=False)
    assert summary.actions_applied == 1
    err = capsys.readouterr().err
    # A warning about the non-repo cards path should appear on stderr.
    assert "cards" in err.lower()
    assert str(cards) in err or "not a git repo" in err.lower()


# ---------------------------------------------------------------------------
# IMPORTANT 5: defense-in-depth sanitization of topic / hook in trim
# ---------------------------------------------------------------------------


def test_trim_sanitizes_embedded_newlines_in_hook(
    cfg: Config, workspace_dir: Path
) -> None:
    """A directly-constructed Verdict (bypassing judge.py) with newlines
    in the hook must still produce a single-line breadcrumb. Defense
    in depth: even if a future caller forges a Verdict, trim cannot
    emit a multi-line breadcrumb that smuggles new content into the
    bootstrap file."""
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old\nbody\n")
    sec = make_section(bs, heading="Old", body="body")
    v = make_verdict(
        sec,
        topic="Legit Topic",
        hook="line1\n## Injected H2\nmore",
    )
    plan = build_plan([v], cfg, today_iso=TODAY)
    assert len(plan) == 1 and not plan[0].skipped
    breadcrumb = plan[0].breadcrumb_line
    assert "\n" not in breadcrumb
    assert "## Injected" not in breadcrumb


def test_trim_sanitizes_embedded_newlines_in_topic(
    cfg: Config, workspace_dir: Path
) -> None:
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old\nbody\n")
    sec = make_section(bs, heading="Old", body="body")
    v = make_verdict(
        sec,
        topic="real topic\n## Injected",
        hook="hook",
    )
    plan = build_plan([v], cfg, today_iso=TODAY)
    breadcrumb = plan[0].breadcrumb_line
    assert "\n" not in breadcrumb
    assert "## Injected" not in breadcrumb


def test_trim_escapes_yaml_quotes_in_frontmatter(
    cfg: Config, workspace_dir: Path
) -> None:
    """A topic or hook containing a double quote must not break the
    rendered frontmatter when we eventually move to quoted-yaml values."""
    bs = write_bootstrap(workspace_dir, "AGENTS.md", "## Old\nbody\n")
    sec = make_section(bs, heading="Old", body="body")
    v = make_verdict(
        sec,
        topic='topic with "quotes"',
        hook='hook with "quotes"',
    )
    plan = build_plan([v], cfg, today_iso=TODAY)
    body = plan[0].card_body
    # Both the topic and hook should still be parseable - either by
    # escaping or by not introducing structural breakage.
    assert 'topic: ' in body
    # The body must still end with a frontmatter delimiter and content.
    assert body.startswith("---\n")
    assert "\n---\n" in body


# ---------------------------------------------------------------------------
# IMPORTANT 4: heading boundary detection matches parsing.py
# ---------------------------------------------------------------------------


def test_apply_respects_tab_separated_headings_as_boundaries(
    cfg: Config, workspace_dir: Path
) -> None:
    """``##\\tFoo`` parses as an H2; trim must recognize it as a boundary.

    Mixing a space-separated H2 followed by a tab-separated H2 was
    silently flattening the second heading's body into the first
    section's replacement, because the literal ``startswith('## ')``
    check missed the tab-separated heading.
    """
    bs_text = (
        "## First\n"
        "first body line\n"
        "\n"
        "##\tSecond\n"
        "second body line\n"
    )
    bs = write_bootstrap(workspace_dir, "AGENTS.md", bs_text)
    sections = parse_file(bs)
    secs = {s.heading_text: s for s in sections}
    # Parser sees both headings.
    assert set(secs) == {"First", "Second"}
    v = make_verdict(
        secs["First"], topic="First Topic", hook="moved.", tags=(), category=""
    )
    plan = build_plan([v], cfg, today_iso=TODAY)
    apply_plan(plan, cfg, apply=True, force=True)
    bs_after = bs.read_text()
    # Second section's heading + body must survive verbatim.
    assert "##\tSecond" in bs_after
    assert "second body line" in bs_after
    # First section's body was replaced by a breadcrumb.
    assert "first body line" not in bs_after
    assert "- See [First Topic]" in bs_after


# ---------------------------------------------------------------------------
# BLOCKER 1: cards write before bootstrap rewrites
# ---------------------------------------------------------------------------


def test_card_write_failure_does_not_touch_any_bootstrap(
    cfg: Config,
    workspace_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a card write fails, no bootstrap file may be rewritten.

    Set up two actions across one file. Patch atomic_write_text so the
    FIRST card write raises OSError. The bootstrap file must remain
    untouched, and the partial summary must reflect what actually
    completed (zero cards, zero files changed).
    """
    text = (
        "## First\n"
        "first body line\n"
        "\n"
        "## Second\n"
        "second body line\n"
    )
    bs = write_bootstrap(workspace_dir, "AGENTS.md", text)
    original = bs.read_text()
    sections = parse_file(bs)
    secs = {s.heading_text: s for s in sections}
    v1 = make_verdict(
        secs["First"], topic="First Topic", hook="h1", tags=(), category=""
    )
    v2 = make_verdict(
        secs["Second"], topic="Second Topic", hook="h2", tags=(), category=""
    )
    plan = build_plan([v1, v2], cfg, today_iso=TODAY)

    real_write = trim_mod.atomic_write_text

    def fail_on_card(target: Path, content: str) -> None:
        # Card writes go to cfg.cards_dir; bootstrap writes go elsewhere.
        if target.parent == cfg.cards_dir:
            raise OSError("disk full (simulated)")
        real_write(target, content)

    monkeypatch.setattr(trim_mod, "atomic_write_text", fail_on_card)

    with pytest.raises(CardWriteError):
        apply_plan(plan, cfg, apply=True, force=True)

    # Bootstrap is untouched.
    assert bs.read_text() == original
    # No card files on disk.
    assert list(cfg.cards_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# BLOCKER 2: collision policy re-checked at apply_plan time
# ---------------------------------------------------------------------------


def test_collision_skip_rechecked_at_apply_time(
    cfg: Config, workspace_dir: Path
) -> None:
    """Card appears between build_plan and apply_plan -> apply skips it.

    The original collision policy was 'skip', so apply must honor that
    same policy at apply time (not blindly overwrite). Bootstrap stays
    unmodified.
    """
    bs = write_bootstrap(
        workspace_dir, "AGENTS.md", "## Old Setup\nsome body content\n"
    )
    original_bs_text = bs.read_text()
    sections = parse_file(bs)
    sec = sections[0]
    plan = build_plan(
        [make_verdict(sec)], cfg, today_iso=TODAY, existing_card_collision="skip"
    )
    assert len(plan) == 1 and not plan[0].skipped
    # Race: a card with the same slug appears AFTER the plan was built.
    target = cfg.cards_dir / "old-setup-notes.md"
    target.write_text("pre-existing content\n")

    summary = apply_plan(plan, cfg, apply=True, force=True)
    # Skip honored at apply time: no bootstrap rewrite, card preserved.
    assert summary.actions_applied == 0
    assert summary.skipped == 1
    assert bs.read_text() == original_bs_text
    assert target.read_text() == "pre-existing content\n"


def test_collision_overwrite_rechecked_at_apply_time(
    cfg: Config, workspace_dir: Path
) -> None:
    """With overwrite, the action proceeds even if the card appeared later."""
    bs = write_bootstrap(
        workspace_dir, "AGENTS.md", "## Old Setup\nsome body content\n"
    )
    sections = parse_file(bs)
    sec = sections[0]
    plan = build_plan(
        [make_verdict(sec)],
        cfg,
        today_iso=TODAY,
        existing_card_collision="overwrite",
    )
    # Race: card appears after the plan but policy says overwrite.
    target = cfg.cards_dir / "old-setup-notes.md"
    target.write_text("pre-existing content\n")

    summary = apply_plan(plan, cfg, apply=True, force=True)
    assert summary.actions_applied == 1
    # Card was overwritten with the planned body.
    assert "topic: Old Setup Notes" in target.read_text()


def test_collision_rename_rechecked_at_apply_time(
    cfg: Config, workspace_dir: Path
) -> None:
    """With rename, a collision discovered at apply time picks a fresh slug,
    and the breadcrumb in the bootstrap points at the actual file written."""
    bs = write_bootstrap(
        workspace_dir, "AGENTS.md", "## Old Setup\nsome body content\n"
    )
    sections = parse_file(bs)
    sec = sections[0]
    plan = build_plan(
        [make_verdict(sec)],
        cfg,
        today_iso=TODAY,
        existing_card_collision="rename",
    )
    # Plan picked the unsuffixed slug because cards_dir was empty at plan time.
    assert plan[0].card_path.name == "old-setup-notes.md"
    # Race: the unsuffixed card materializes between plan and apply.
    (cfg.cards_dir / "old-setup-notes.md").write_text("pre-existing\n")

    summary = apply_plan(plan, cfg, apply=True, force=True)
    assert summary.actions_applied == 1
    # New card landed at -2.md, original preserved.
    assert (cfg.cards_dir / "old-setup-notes-2.md").exists()
    assert (
        cfg.cards_dir / "old-setup-notes.md"
    ).read_text() == "pre-existing\n"
    # Breadcrumb in the bootstrap points at the renamed slug.
    bs_after = bs.read_text()
    assert "old-setup-notes-2.md" in bs_after
    assert "[Old Setup Notes](memory/cards/old-setup-notes.md)" not in bs_after


def test_card_write_failure_after_partial_success_leaves_completed_cards(
    cfg: Config,
    workspace_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the SECOND card write fails, the first card stays on disk but no
    bootstrap may be modified, and the error must clearly identify the
    partial state.
    """
    text = (
        "## First\n"
        "first body line\n"
        "\n"
        "## Second\n"
        "second body line\n"
    )
    bs = write_bootstrap(workspace_dir, "AGENTS.md", text)
    original = bs.read_text()
    sections = parse_file(bs)
    secs = {s.heading_text: s for s in sections}
    v1 = make_verdict(
        secs["First"], topic="First Topic", hook="h1", tags=(), category=""
    )
    v2 = make_verdict(
        secs["Second"], topic="Second Topic", hook="h2", tags=(), category=""
    )
    plan = build_plan([v1, v2], cfg, today_iso=TODAY)

    real_write = trim_mod.atomic_write_text
    state = {"card_writes": 0}

    def fail_on_second_card(target: Path, content: str) -> None:
        if target.parent == cfg.cards_dir:
            state["card_writes"] += 1
            if state["card_writes"] == 2:
                raise OSError("disk full on second card (simulated)")
        real_write(target, content)

    monkeypatch.setattr(trim_mod, "atomic_write_text", fail_on_second_card)

    with pytest.raises(CardWriteError) as exc_info:
        apply_plan(plan, cfg, apply=True, force=True)

    # The error names which card failed and how many succeeded before.
    err = str(exc_info.value)
    assert "card" in err.lower()
    # Bootstrap is untouched.
    assert bs.read_text() == original
    # One card present, one missing.
    written_cards = sorted(p.name for p in cfg.cards_dir.iterdir())
    assert len(written_cards) == 1
