"""Size and limit reporting for tracked bootstrap files (read-only).

The `status` verb walks every (workspace, tracked_file) pair, measures size
in chars + lines, computes the distance to the configured soft/hard limits,
and renders the result as either a human table or stable JSON. No LLM calls,
no mutations, stdlib-only.

Severity precedence (highest wins): missing > unreadable > hard > soft > ok.
Exit-code policy: 0 if every file is `ok`, 1 if any are `soft`, 2 if any are
`hard` / `missing` / `unreadable`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from bootstrap_doctor.paths import Config
from bootstrap_doctor.safety import UnsafeTargetError, ensure_within


PRIMARY_LABEL = "workspace"

# Severity strings -------------------------------------------------------

SEV_OK = "ok"
SEV_SOFT = "soft"
SEV_HARD = "hard"
SEV_MISSING = "missing"
SEV_UNREADABLE = "unreadable"

# Flag glyphs shown in the rendered table (kept short to align with numeric
# columns). The full severity word still lives in FileStatus.severity for
# machine readers.
_FLAGS = {
    SEV_OK: "ok",
    SEV_SOFT: "SOFT",
    SEV_HARD: "HARD",
    SEV_MISSING: "MISSING",
    SEV_UNREADABLE: "UNREAD",
}


@dataclass(frozen=True)
class FileStatus:
    path: Path
    workspace_label: str
    name: str
    exists: bool
    bytes: int
    chars: int
    lines: int
    soft_remaining: int
    hard_remaining: int
    severity: str


# Internals --------------------------------------------------------------


def _count_lines(text: str) -> int:
    """Line count: number of '\\n' + 1 if the last line lacks a trailing '\\n'.

    An empty string counts as 0 lines.
    """
    if not text:
        return 0
    n = text.count("\n")
    if not text.endswith("\n"):
        n += 1
    return n


def _classify(chars: int, cfg: Config) -> str:
    if chars >= cfg.hard_limit:
        return SEV_HARD
    if chars >= cfg.soft_limit:
        return SEV_SOFT
    return SEV_OK


def _measure_file(path: Path, label: str, name: str, cfg: Config) -> FileStatus:
    """Build a FileStatus for a single (workspace, tracked_file) pair."""
    if not path.exists():
        return FileStatus(
            path=path,
            workspace_label=label,
            name=name,
            exists=False,
            bytes=0,
            chars=0,
            lines=0,
            soft_remaining=cfg.soft_limit,
            hard_remaining=cfg.hard_limit,
            severity=SEV_MISSING,
        )
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return FileStatus(
            path=path,
            workspace_label=label,
            name=name,
            exists=True,
            bytes=size_bytes,
            chars=0,
            lines=0,
            soft_remaining=cfg.soft_limit,
            hard_remaining=cfg.hard_limit,
            severity=SEV_UNREADABLE,
        )
    chars = len(text)
    lines = _count_lines(text)
    return FileStatus(
        path=path,
        workspace_label=label,
        name=name,
        exists=True,
        bytes=size_bytes,
        chars=chars,
        lines=lines,
        soft_remaining=cfg.soft_limit - chars,
        hard_remaining=cfg.hard_limit - chars,
        severity=_classify(chars, cfg),
    )


def _workspace_scopes(cfg: Config) -> list[tuple[str, Path]]:
    """Return (label, dir) for the primary workspace + each named workspace.

    Validates each named workspace via safety.ensure_within so a misconfigured
    entry (absolute path, '../', symlink escape) raises rather than silently
    pointing the scan outside the workspace tree.
    """
    scopes: list[tuple[str, Path]] = [(PRIMARY_LABEL, cfg.workspace_dir)]
    for name in cfg.named_workspaces:
        candidate = cfg.workspace_dir / name
        # ensure_within() resolves both sides; if the named workspace doesn't
        # exist yet (its own valid case below), resolve() still works on a
        # non-existent path under a real base. Escapes raise UnsafeTargetError.
        # Note: we deliberately let UnsafeTargetError propagate - that's a
        # config error, not a runtime condition we mask.
        try:
            resolved = ensure_within(cfg.workspace_dir, candidate)
        except UnsafeTargetError:
            raise
        scopes.append((name, resolved))
    return scopes


# Public API -------------------------------------------------------------


def collect(cfg: Config) -> list[FileStatus]:
    """Walk every (workspace, tracked_file) pair and return one FileStatus each.

    Order: primary workspace first, then each named workspace in declared
    order. Within a workspace, the order follows ``cfg.tracked_files``.

    If a named workspace directory is missing, every tracked file in that
    workspace surfaces as severity='missing'. Rendering layer reports the
    missing directory as a separate warning.
    """
    rows: list[FileStatus] = []
    for label, ws_dir in _workspace_scopes(cfg):
        for name in cfg.tracked_files:
            file_path = ws_dir / name
            rows.append(_measure_file(file_path, label, name, cfg))
    return rows


# Rendering helpers ------------------------------------------------------


def _missing_named_workspace_dirs(cfg: Config) -> list[tuple[str, Path]]:
    """Return (label, dir) for each named workspace whose dir doesn't exist."""
    out: list[tuple[str, Path]] = []
    for name in cfg.named_workspaces:
        candidate = cfg.workspace_dir / name
        try:
            resolved = ensure_within(cfg.workspace_dir, candidate)
        except UnsafeTargetError:
            # Shouldn't happen here - collect() would have raised first - but
            # be defensive when rendering.
            continue
        if not resolved.is_dir():
            out.append((name, resolved))
    return out


def _fmt_delta(n: int) -> str:
    """Signed integer with explicit '+' for non-negative."""
    return f"{n:+d}"


def render_text(rows: list[FileStatus], cfg: Config) -> str:
    """Human-readable table, grouped by workspace_label."""
    ceiling = 12000  # matches paths.HARD_LIMIT_CEILING
    lines: list[str] = [
        f"bootstrap-doctor status  "
        f"(soft={cfg.soft_limit}, hard={cfg.hard_limit}, ceiling={ceiling})"
    ]

    # Warn up front about any named workspace directories that are missing on
    # disk, so the operator notices before they scan the per-file rows.
    missing_dirs = _missing_named_workspace_dirs(cfg)
    for name, path in missing_dirs:
        lines.append(f"warning: named workspace {name!r} does not exist: {path}")

    # Group rows by workspace_label, preserving first-seen order.
    by_label: dict[str, list[FileStatus]] = {}
    label_order: list[str] = []
    label_dirs: dict[str, Path] = {}
    for r in rows:
        if r.workspace_label not in by_label:
            by_label[r.workspace_label] = []
            label_order.append(r.workspace_label)
            # All rows in a label share the same parent dir.
            label_dirs[r.workspace_label] = r.path.parent
        by_label[r.workspace_label].append(r)

    # Column widths.
    name_w = max(
        [len("file")] + [len(r.name) for r in rows] if rows else [len("file")]
    )
    chars_w = max(
        [len("chars")]
        + [len(str(r.chars)) for r in rows]
        if rows else [len("chars")]
    )
    lines_w = max(
        [len("lines")]
        + [len(str(r.lines)) for r in rows]
        if rows else [len("lines")]
    )
    soft_w = max(
        [len("soft")]
        + [len(_fmt_delta(r.soft_remaining)) for r in rows]
        if rows else [len("soft")]
    )
    hard_w = max(
        [len("hard")]
        + [len(_fmt_delta(r.hard_remaining)) for r in rows]
        if rows else [len("hard")]
    )

    header_row = (
        f"  {'file':<{name_w}}  "
        f"{'chars':>{chars_w}}  "
        f"{'lines':>{lines_w}}  "
        f"{'soft':>{soft_w}}  "
        f"{'hard':>{hard_w}}  "
        f"sev"
    )

    for label in label_order:
        lines.append("")
        ws_dir = label_dirs[label]
        lines.append(f"{label}  {ws_dir}")
        lines.append(header_row)
        for r in by_label[label]:
            flag = _FLAGS.get(r.severity, r.severity)
            chars_cell = "-" if r.severity == SEV_MISSING else str(r.chars)
            lines_cell = "-" if r.severity == SEV_MISSING else str(r.lines)
            if r.severity in (SEV_MISSING, SEV_UNREADABLE):
                soft_cell = "-"
                hard_cell = "-"
            else:
                soft_cell = _fmt_delta(r.soft_remaining)
                hard_cell = _fmt_delta(r.hard_remaining)
            lines.append(
                f"  {r.name:<{name_w}}  "
                f"{chars_cell:>{chars_w}}  "
                f"{lines_cell:>{lines_w}}  "
                f"{soft_cell:>{soft_w}}  "
                f"{hard_cell:>{hard_w}}  "
                f"{flag}"
            )

    # Footer / summary.
    total = len(rows)
    hard_count = sum(1 for r in rows if r.severity == SEV_HARD)
    soft_count = sum(1 for r in rows if r.severity == SEV_SOFT)
    missing_count = sum(1 for r in rows if r.severity == SEV_MISSING)
    unread_count = sum(1 for r in rows if r.severity == SEV_UNREADABLE)
    lines.append("")
    summary = (
        f"summary: {total} files, "
        f"{hard_count} over hard, "
        f"{soft_count} over soft, "
        f"{missing_count} missing"
    )
    if unread_count:
        summary += f", {unread_count} unreadable"
    lines.append(summary)
    return "\n".join(lines)


def render_json(rows: list[FileStatus], cfg: Config) -> str:
    """Stable JSON: {'soft_limit', 'hard_limit', 'rows'}."""
    out_rows: list[dict] = []
    for r in rows:
        d = asdict(r)
        d["path"] = str(r.path)
        out_rows.append(d)
    payload = {
        "soft_limit": cfg.soft_limit,
        "hard_limit": cfg.hard_limit,
        "rows": out_rows,
    }
    return json.dumps(payload, indent=2)


def _exit_code(rows: list[FileStatus]) -> int:
    """0 if all ok, 1 if any soft, 2 if any hard/missing/unreadable."""
    code = 0
    for r in rows:
        if r.severity in (SEV_HARD, SEV_MISSING, SEV_UNREADABLE):
            return 2
        if r.severity == SEV_SOFT:
            code = 1
    return code


def run(cfg: Config, *, as_json: bool = False) -> int:
    """Entrypoint called by cli.py. Prints rendered output, returns exit code."""
    rows = collect(cfg)
    print(render_json(rows, cfg) if as_json else render_text(rows, cfg))
    return _exit_code(rows)
