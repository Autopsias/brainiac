"""Pre-scoring gates of the run validator's entry point (s18 extraction).

One function per early-return stage of ``verify_run``: the dual-location run
record gate, the manifest gate and the ops-dir gate. ``verify_run`` itself
stays in :mod:`brain.cos_runverify` with an unchanged signature — including
its check assembly, which deliberately calls every check through the parent
module's own namespace so a monkeypatched check stays honoured.
"""
from __future__ import annotations

from typing import Any

from . import cos
from .cos_runverify_checks import INCONCLUSIVE, _row


def intruder_row(vault, run_id: str) -> dict[str, Any] | None:
    """Refuse a run whose records exist in two places (gap-05).

    A RUN RECORD IN TWO PLACES IS NOT A RUN THIS CAN SCORE. The manifest, the
    verdict and the plan binding moved off the VirtioFS mount on 2026-08-16
    and were carried forward once; anything left behind is either a refused
    conflict (the two copies disagree) or a file written into a VM-writable
    directory the validator used to trust. Preferring either copy is the
    silent choice this relocation exists to prevent, so the run is
    INCONCLUSIVE and says which files did it. Scoped to the RUN: one planted
    file must not stop every other night being verified.
    """
    intruders = cos.run_record_intruders(vault, run_id)
    if not intruders:
        return None
    why = (f"run records for this run also exist in the legacy on-mount "
           f"directory {cos.legacy_runs_dir(vault)} "
           f"({', '.join(sorted(intruders))}), disagreeing with the "
           "host-private copy or written after the carry-forward. That "
           "directory is inside the Cowork workspace, which is why the "
           "store was moved out of it — and choosing between two copies of "
           "a run's own manifest, verdict or plan binding is not this "
           "validator's call")
    return {"state": "scored", "verdict": cos.RUN_INCONCLUSIVE, "reason": why,
            "checks": [_row("completion", INCONCLUSIVE,
                            "run records present in BOTH the host-private "
                            "store and the legacy on-mount directory",
                            reexecuted=True)]}


def manifest_missing_row(vault, run_id: str) -> dict[str, Any] | None:
    """The manifest gate: a run with no readable manifest cannot be scored."""
    if cos.run_manifest(vault, run_id) is not None:
        return None
    try:
        cos.checked_run_id(run_id)
        why = ("no host run manifest for this run — the host never "
               "recorded what was supposed to run, so it cannot "
               "check whether the run did it")
        short = "no host run manifest"
    except ValueError as exc:
        why = (f"{exc}. That is a MALFORMED run id, NOT a missing "
               "manifest: pass the full host-assigned id, which the host "
               "publishes at .brain/cos/shared/current-run.json")
        short = "malformed run id (not a missing manifest)"
    return {"state": "scored", "verdict": cos.RUN_INCONCLUSIVE, "reason": why,
            "checks": [_row("completion", INCONCLUSIVE, short,
                            reexecuted=True)]}


def ops_dir_row(ops) -> dict[str, Any] | None:
    """The ops-dir gate: no ops dir means the validator could not run."""
    if ops.is_dir():
        return None
    return {"state": "scored", "verdict": cos.RUN_INCONCLUSIVE,
            "reason": (f"the run ops dir {ops} does not exist — the "
                       "validator could not run, which is not the same as "
                       "the run passing"),
            "checks": [_row("completion", INCONCLUSIVE,
                            f"no run ops dir at {ops}", reexecuted=True)]}
