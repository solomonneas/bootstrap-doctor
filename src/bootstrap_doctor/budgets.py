"""Bootstrap size thresholds, decoupled from a hard brigade dependency.

brigade.budgets is the canonical source of truth for these numbers across the
escoffier-labs tooling (its own ``doctor``, ``ingest``, ``handoff``, and
``repos`` stations import from there). bootstrap-doctor prefers those canonical
values when brigade is installed, but it must also run standalone, so this
module imports them with a graceful fallback.

The fallback constants below mirror brigade.budgets' current values. brigade
owns the canonical definitions; if it drifts, the installed brigade wins and
this fallback only applies when brigade is absent.
"""
from __future__ import annotations

try:
    from brigade.budgets import (
        BOOTSTRAP_HARD_LIMIT_CEILING as HARD_LIMIT_CEILING,
    )
    from brigade.budgets import (
        DEFAULT_BOOTSTRAP_HARD_LIMIT as DEFAULT_HARD_LIMIT,
    )
    from brigade.budgets import (
        DEFAULT_BOOTSTRAP_SOFT_LIMIT as DEFAULT_SOFT_LIMIT,
    )

    BRIGADE_AVAILABLE = True
except ImportError:
    # Fallback values mirroring brigade.budgets (which owns the canonical
    # numbers). Keep these in sync if brigade's defaults change.
    DEFAULT_SOFT_LIMIT = 10_000
    DEFAULT_HARD_LIMIT = 11_500
    HARD_LIMIT_CEILING = 12_000

    BRIGADE_AVAILABLE = False


__all__ = [
    "BRIGADE_AVAILABLE",
    "DEFAULT_HARD_LIMIT",
    "DEFAULT_SOFT_LIMIT",
    "HARD_LIMIT_CEILING",
]
