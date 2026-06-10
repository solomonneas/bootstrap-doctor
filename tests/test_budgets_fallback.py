"""Tests for the brigade-optional budgets fallback.

bootstrap-doctor sources its bootstrap size thresholds from ``brigade.budgets``
when brigade-cli is installed, but it must also run standalone. These tests
verify that the package imports cleanly and falls back to the mirrored
constants when ``brigade`` is not importable, and that the fallback values
match brigade's canonical numbers.

The "brigade absent" case runs in a subprocess so that hiding ``brigade`` and
reloading the budgets module cannot pollute import state for the rest of the
suite (reloading swaps class identities like ``ConfigError`` out from under
other tests).
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import bootstrap_doctor.budgets as budgets_mod

# The canonical values brigade.budgets currently exposes. The fallback must
# mirror these exactly so behavior is identical whether or not brigade is
# installed.
CANONICAL_SOFT_LIMIT = 10_000
CANONICAL_HARD_LIMIT = 11_500
CANONICAL_HARD_LIMIT_CEILING = 12_000


def test_fallback_values_match_canonical() -> None:
    """The exported constants equal brigade's canonical numbers regardless of source."""
    assert budgets_mod.DEFAULT_SOFT_LIMIT == CANONICAL_SOFT_LIMIT
    assert budgets_mod.DEFAULT_HARD_LIMIT == CANONICAL_HARD_LIMIT
    assert budgets_mod.HARD_LIMIT_CEILING == CANONICAL_HARD_LIMIT_CEILING


def test_imports_and_uses_fallback_when_brigade_absent() -> None:
    """With brigade hidden, the package imports and uses the mirrored fallback.

    Runs in a fresh subprocess whose import system raises ``ImportError`` for
    any ``brigade`` import. Asserts that ``bootstrap_doctor.budgets`` takes the
    fallback branch (``BRIGADE_AVAILABLE is False``), the constants match
    brigade's canonical numbers, and ``bootstrap_doctor.paths`` (which
    re-exports them) still imports standalone.
    """
    script = textwrap.dedent(
        f"""
        import builtins
        import sys

        _real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "brigade" or name.startswith("brigade."):
                raise ImportError("brigade hidden for test: " + name)
            return _real_import(name, *args, **kwargs)

        builtins.__import__ = _blocked_import
        # Defensively poison the module cache too.
        sys.modules["brigade"] = None  # type: ignore[assignment]

        import bootstrap_doctor.budgets as b

        assert b.BRIGADE_AVAILABLE is False, "expected fallback branch"
        assert b.DEFAULT_SOFT_LIMIT == {CANONICAL_SOFT_LIMIT}
        assert b.DEFAULT_HARD_LIMIT == {CANONICAL_HARD_LIMIT}
        assert b.HARD_LIMIT_CEILING == {CANONICAL_HARD_LIMIT_CEILING}

        import bootstrap_doctor.paths as p

        assert p.DEFAULT_SOFT_LIMIT == {CANONICAL_SOFT_LIMIT}
        assert p.DEFAULT_HARD_LIMIT == {CANONICAL_HARD_LIMIT}
        assert p.HARD_LIMIT_CEILING == {CANONICAL_HARD_LIMIT_CEILING}

        print("OK")
        """
    )
    # Hand the subprocess the same import path this test run uses, so it can
    # find bootstrap_doctor whether the package is pip-installed or only on
    # sys.path (e.g. the pytest ``pythonpath = ["src"]`` setting).
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout
