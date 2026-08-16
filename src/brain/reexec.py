"""Re-exec `brain update` when it upgrades the engine underneath itself.

Every step after the venv refresh — dist rebuild, workspace re-stage, the
doctor verify — runs from modules already imported into the RUNNING process.
pip replaces files on disk; it cannot replace modules in memory. So the run
that installs new staging logic executes the OLD staging logic, and the new
behaviour only appears on the NEXT invocation.

Measured on the 0.20.12 -> 0.20.13 cut (2026-08-16): that release shipped the
missing ELF-staging leg, and the very `brain update` that installed it did not
stage the ELFs. A second run did. Nothing reported a problem either time — the
first run said `ok: true` while the workspace kept binaries a release behind.

Split into its own module (rather than living in update.py) for the same
reason as doctor_vendor.py and vmstaging.py: update.py is at its size ratchet
and may not grow.
"""
from __future__ import annotations

import os
import re
import sys

#: Set in the child so it can never re-exec again.
REEXEC_ENV = "BRAIN_UPDATE_REEXECED"


def engine_version_moved(engine_result: dict) -> bool:
    """True when the venv refresh actually CHANGED the installed version.

    Parsed out of the reported strings rather than compared raw: they carry a
    ``brain `` prefix, and a dry-run reports ``[dry-run] not executed`` — which
    must never count as a move.
    """
    old = re.search(r"\d+\.\d+\.\d+", engine_result.get("old_version") or "")
    new = re.search(r"\d+\.\d+\.\d+", engine_result.get("new_version") or "")
    return bool(old and new and old.group(0) != new.group(0))


def reexec_after_engine_move(engine_result: dict, *, dry_run: bool = False) -> None:
    """Replace this process so the rest of the chain IS the version installed.

    Returns normally when no re-exec is warranted; otherwise never returns.
    """
    if dry_run or not engine_version_moved(engine_result):
        return
    if os.environ.get(REEXEC_ENV) == "1":
        return
    # Semgrep flags argv/environ reaching exec as "user controlled content".
    # Justified, not bypassed: the interpreter is `sys.executable` (fixed, not
    # from input), the module is a literal, there is NO shell, and the only
    # pass-through is THIS process's own argv and environ — the command the
    # operator already ran, plus one guard flag. Re-running it grants no
    # capability the caller did not already exercise, and no privilege boundary
    # is crossed: `brain update` is host-broker only and refuses `--role vm`.
    # Dropping the pass-through instead would silently discard the operator's
    # flags (--engine-src, --json), which is a correctness bug.
    # The annotation must sit on the line directly above the call — placed
    # further up it is silently ignored, which is how this first shipped.
    # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
    os.execve(sys.executable, [sys.executable, "-m", "brain.cli", *sys.argv[1:]],
              {**os.environ, REEXEC_ENV: "1"})
