"""Command-line entrypoint for bootstrap-doctor.

Subcommands:

  * ``status``  - read-only size/limit report (delegates to ``status.run``).
  * ``audit``   - parse + heuristics shortlist + LLM judge, render verdicts.
  * ``trim``    - same flow as audit, then build + (optionally) apply a plan.

Exit codes:

  * ``0`` - success / nothing actionable.
  * ``1`` - soft warning. Any soft-band file in status, any move/unsure
    verdict in audit, or a dry-run trim plan with actions.
  * ``2`` - hard error. Bad config, dirty workspace blocking ``--apply``,
    gateway-side failures during audit, hard-limit violations in status.
  * ``3`` - unexpected exception. Falls through the broad catch at the
    top level. Set ``BOOTSTRAP_DOCTOR_TRACE=1`` to see the traceback.

Every mutating verb is dry-run by default. Only ``trim --apply`` writes
files, and only after a git-clean preflight (unless ``--force``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from bootstrap_doctor import __version__


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    """Common flags every subcommand accepts."""
    p.add_argument("--config", default=None, help="Path to config.toml")
    p.add_argument("--workspace-dir", default=None, help="Workspace root")
    p.add_argument("--cards-dir", default=None, help="Memory cards directory")
    p.add_argument("--gateway-url", default=None, help="OpenClaw gateway URL")
    p.add_argument("--gateway-model", default=None, help="Gateway model id")
    p.add_argument("--soft-limit", type=int, default=None, help="Soft char limit")
    p.add_argument("--hard-limit", type=int, default=None, help="Hard char limit")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with all three subcommands."""
    root = argparse.ArgumentParser(
        prog="bootstrap-doctor",
        description="Audit and trim OpenClaw bootstrap files.",
    )
    root.add_argument(
        "--version",
        action="version",
        version=f"bootstrap-doctor {__version__}",
    )
    sub = root.add_subparsers(dest="verb", required=True)

    p_status = sub.add_parser("status", help="Read-only size + limit report")
    _add_common(p_status)
    p_status.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human table"
    )

    p_audit = sub.add_parser(
        "audit", help="Heuristic shortlist + LLM judge (no mutations)"
    )
    _add_common(p_audit)
    p_audit.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-judge every shortlisted section",
    )
    p_audit.add_argument(
        "--max-input-chars",
        type=int,
        default=200_000,
        help="Per-run judge prompt-char budget (default 200000)",
    )
    p_audit.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable verdict output",
    )

    p_trim = sub.add_parser(
        "trim",
        help="Build and (with --apply) execute a trim plan from move verdicts",
    )
    _add_common(p_trim)
    p_trim.add_argument(
        "--apply",
        action="store_true",
        help="Actually write files; default is dry-run with diff preview",
    )
    p_trim.add_argument(
        "--force",
        action="store_true",
        help="Bypass the dirty-git preflight",
    )
    p_trim.add_argument(
        "--no-cache",
        action="store_true",
        help="Re-judge before trimming",
    )
    p_trim.add_argument(
        "--collision",
        choices=("skip", "overwrite", "rename"),
        default="skip",
        help="Behavior when target card already exists (default: skip)",
    )
    p_trim.add_argument(
        "--json",
        action="store_true",
        help="Emit summary as JSON",
    )

    return root


# ---------------------------------------------------------------------------
# Error printing
# ---------------------------------------------------------------------------


def _print_error(msg: str) -> None:
    """Standard ``bootstrap-doctor: <msg>`` stderr line."""
    print(f"bootstrap-doctor: {msg}", file=sys.stderr)


def _trace_enabled() -> bool:
    return os.environ.get("BOOTSTRAP_DOCTOR_TRACE") == "1"


# ---------------------------------------------------------------------------
# Config resolution wrapper (so verbs can opt into allow_missing_cards)
# ---------------------------------------------------------------------------


def _resolve_cfg(args: argparse.Namespace, *, allow_missing_cards: bool):
    """Run paths.resolve_config with CLI overrides, mapping flags to kwargs.

    Raises the same ConfigError as resolve_config; the caller maps it to
    an exit code.
    """
    from bootstrap_doctor.paths import resolve_config

    return resolve_config(
        config_file=args.config,
        workspace_dir=args.workspace_dir,
        cards_dir=args.cards_dir,
        gateway_url=args.gateway_url,
        gateway_model=args.gateway_model,
        soft_limit=args.soft_limit,
        hard_limit=args.hard_limit,
        allow_missing_cards=allow_missing_cards,
    )


def _handle_config_error(args: argparse.Namespace, err: Exception) -> int:
    """Emit a user-friendly message for a ConfigError.

    Special-cases the "workspace dir does not exist" path with a hint when
    the user did not pass any override (no --workspace-dir, no env var).
    """
    msg = str(err)
    has_env_ws = "BOOTSTRAP_DOCTOR_WORKSPACE_DIR" in os.environ
    if (
        "workspace_dir does not exist" in msg
        and args.workspace_dir is None
        and not has_env_ws
    ):
        _print_error(f"workspace dir not found: {msg.split(':', 1)[-1].strip()}")
        print(
            "hint: pass --workspace-dir or set BOOTSTRAP_DOCTOR_WORKSPACE_DIR",
            file=sys.stderr,
        )
        return 2
    _print_error(msg)
    return 2


# ---------------------------------------------------------------------------
# Verb: status
# ---------------------------------------------------------------------------


def run_status(args: argparse.Namespace) -> int:
    from bootstrap_doctor import status as status_mod
    from bootstrap_doctor.paths import ConfigError

    try:
        cfg = _resolve_cfg(args, allow_missing_cards=True)
    except ConfigError as exc:
        return _handle_config_error(args, exc)
    return status_mod.run(cfg, as_json=args.json)


# ---------------------------------------------------------------------------
# Shared: parse all workspaces -> sections -> shortlist -> judge
# ---------------------------------------------------------------------------


def _collect_sections(cfg) -> list:
    """Walk every (workspace, tracked_file) pair and parse every file.

    Returns one flat list of Section across all workspaces. Missing files
    or unreadable bytes are skipped silently here; the status verb is the
    place to surface that. Audit/trim just operate on what they can parse.
    """
    from bootstrap_doctor.parsing import parse_file
    from bootstrap_doctor.safety import UnsafeTargetError, ensure_within

    sections: list = []
    scopes: list[tuple[str, Path]] = [("workspace", cfg.workspace_dir)]
    for name in cfg.named_workspaces:
        try:
            resolved = ensure_within(cfg.workspace_dir, cfg.workspace_dir / name)
        except UnsafeTargetError:
            continue
        scopes.append((name, resolved))

    for _label, ws_dir in scopes:
        if not ws_dir.is_dir():
            continue
        for name in cfg.tracked_files:
            path = ws_dir / name
            if not path.exists() or not path.is_file():
                continue
            try:
                sections.extend(parse_file(path))
            except (OSError, UnicodeDecodeError):
                continue
    return sections


def _rel_to_workspace(cfg, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(cfg.workspace_dir.resolve()))
    except ValueError:
        return str(path)


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


# ---------------------------------------------------------------------------
# Verb: audit
# ---------------------------------------------------------------------------


def _render_audit_human(
    rows: list[tuple], cfg, stats
) -> str:
    """Version that also has access to the original candidate reasons."""
    lines: list[str] = []
    header = (
        f"{'workspace':<10}  {'file':<14}  {'heading':<28}  "
        f"{'chars':>6}  {'reasons':<22}  {'decision':<8}  topic / reasoning"
    )
    lines.append(header)
    for candidate, v in rows:
        sec = v.section
        ws = _rel_to_workspace(cfg, sec.file.parent)
        if ws in ("", "."):
            ws = "workspace"
        heading = " > ".join(sec.heading_path) if sec.heading_path else "(preamble)"
        reasons = ", ".join(candidate.reasons)
        decision = v.decision
        topic = v.topic if v.topic else _truncate(v.reasoning, 40)
        topic = _truncate(topic, 50)
        lines.append(
            f"{_truncate(ws, 10):<10}  {_truncate(sec.file.name, 14):<14}  "
            f"{_truncate(heading, 28):<28}  {sec.char_count:>6}  "
            f"{_truncate(reasons, 22):<22}  {decision:<8}  {topic}"
        )
    lines.append("")
    lines.append(
        f"stats: gateway_requests={stats.requests_made}  "
        f"cache_hits={stats.cache_hits}  failures={stats.failures}"
    )
    return "\n".join(lines)


def _render_audit_json(rows: list[tuple], cfg) -> str:
    out: list[dict[str, Any]] = []
    for candidate, v in rows:
        sec = v.section
        out.append(
            {
                "file": _rel_to_workspace(cfg, sec.file),
                "heading_path": list(sec.heading_path),
                "char_count": sec.char_count,
                "reasons": list(candidate.reasons),
                "decision": v.decision,
                "topic": v.topic,
                "category": v.category,
                "tags": list(v.tags),
                "hook": v.hook,
                "reasoning": v.reasoning,
                "source": v.source,
            }
        )
    return json.dumps(out, indent=2)


def _run_audit_pipeline(
    args: argparse.Namespace, cfg
) -> tuple[list, list, Any] | int:
    """Shared between audit and trim. Returns (candidates, verdicts, stats),
    or an int exit code if there's nothing to do."""
    from bootstrap_doctor import judge as judge_mod
    from bootstrap_doctor.heuristics import shortlist

    sections = _collect_sections(cfg)
    candidates = shortlist(sections, cfg)
    if not candidates:
        print("no candidates flagged; nothing to audit.")
        return 0

    verdicts, stats = judge_mod.judge_all(
        candidates,
        cfg,
        use_cache=not args.no_cache,
        max_input_chars=args.max_input_chars
        if hasattr(args, "max_input_chars")
        else 200_000,
    )
    return candidates, verdicts, stats


def run_audit(args: argparse.Namespace) -> int:
    from bootstrap_doctor.paths import ConfigError

    try:
        cfg = _resolve_cfg(args, allow_missing_cards=True)
    except ConfigError as exc:
        return _handle_config_error(args, exc)

    result = _run_audit_pipeline(args, cfg)
    if isinstance(result, int):
        return result
    candidates, verdicts, stats = result

    rows = list(zip(candidates, verdicts))
    if args.json:
        print(_render_audit_json(rows, cfg))
    else:
        print(_render_audit_human(rows, cfg, stats))

    if stats.failures > 0:
        return 2
    actionable = any(v.decision in ("move", "unsure") for v in verdicts)
    return 1 if actionable else 0


# ---------------------------------------------------------------------------
# Verb: trim
# ---------------------------------------------------------------------------


def run_trim(args: argparse.Namespace) -> int:
    from bootstrap_doctor import trim as trim_mod
    from bootstrap_doctor.paths import ConfigError
    from bootstrap_doctor.safety import DirtyWorkspaceError, UnsafeTargetError

    try:
        cfg = _resolve_cfg(args, allow_missing_cards=False)
    except ConfigError as exc:
        return _handle_config_error(args, exc)

    # max_input_chars isn't a trim flag; audit pipeline expects it. Provide
    # the default explicitly so _run_audit_pipeline works.
    if not hasattr(args, "max_input_chars"):
        args.max_input_chars = 200_000

    result = _run_audit_pipeline(args, cfg)
    if isinstance(result, int):
        return result
    _candidates, verdicts, _stats = result

    move_verdicts = [v for v in verdicts if v.decision == "move"]
    if not move_verdicts:
        print("no actions to take.")
        return 0

    actions = trim_mod.build_plan(
        move_verdicts, cfg, existing_card_collision=args.collision
    )
    live_actions = [a for a in actions if not a.skipped]
    if not live_actions and all(a.skipped for a in actions):
        # Plan is entirely skip; still print so user can see why.
        print(trim_mod.render_plan(actions, cfg))
        if not actions:
            print("no actions to take.")
            return 0
        return 0

    if not actions:
        print("no actions to take.")
        return 0

    print(trim_mod.render_plan(actions, cfg))

    if args.apply:
        try:
            summary = trim_mod.apply_plan(
                actions, cfg, apply=True, force=args.force
            )
        except DirtyWorkspaceError as exc:
            _print_error(f"dirty workspace: {exc}")
            return 2
        except UnsafeTargetError as exc:
            _print_error(f"unsafe path: {exc}")
            return 2
        print(
            f"applied {summary.actions_applied} actions, "
            f"wrote {len(summary.cards_written)} cards, "
            f"modified {len(summary.files_changed)} bootstrap files, "
            f"skipped {summary.skipped}"
        )
        return 0

    print("DRY RUN: re-run with --apply to persist these changes.")
    return 1


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.verb == "status":
            return run_status(args)
        if args.verb == "audit":
            return run_audit(args)
        if args.verb == "trim":
            return run_trim(args)
        parser.error(f"unknown verb: {args.verb}")
        return 2
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _print_error("interrupted")
        return 130
    except Exception as exc:
        if _trace_enabled():
            traceback.print_exc(file=sys.stderr)
        _print_error(f"unexpected error: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
