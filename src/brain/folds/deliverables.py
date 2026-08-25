"""Materialize the deliverables shelf on every nightly firing (DLV-04).

Thin by design: the census, the copies and the host-private ledger live in
``brain.deliverables_sync``, which needs no ``BrainCore`` and is therefore
testable on its own. This file is only the wiring — a fold rather than a new
scheduled task, so it rides the umbrella's run-lock, state file, escalation and
health-history plumbing for free (AGENTS.md §6: recurring vault-metadata work
joins ``brain-nightly`` rather than growing a second plist).

It goes in ``folds/`` and nowhere else: ``maintenance_folds*.py`` are s15-era
re-export shims (``maintenance.py:248-252``), and a fold written there is never
composed onto ``BrainCore`` and never fires.
"""

from __future__ import annotations

from typing import Any

from .context import MaintenanceRun
from .. import deliverables_shelf, deliverables_sync, maintenance

BRANCH = "deliverables_shelf"


class DeliverablesFoldsMixin:
    """Provide the nightly shelf-materialization fold."""

    def deliverables_shelf_fold(self, run: MaintenanceRun) -> None:
        """Keep the resolved shelf equal to the census, or say why it is not.

        HOST-ONLY, asserted here as well as at the umbrella's entry point: the
        shelf holds real payload bytes at their true tier, so the VM leg — which
        never signs and never commits — must never materialize one either.
        """
        self._require_host("materialize the deliverables shelf")
        if run.dry_run:
            return
        try:
            result = deliverables_sync.sync(self.vault)
        except Exception as exc:
            run.blocked.append(maintenance.blocked_item(
                f"deliverables-shelf fold failed: {type(exc).__name__}: {exc}",
                "shelf filesystem / host-private ledger",
                "next maintain run",
            ))
            run.mark(BRANCH, False, f"{type(exc).__name__}: {exc}")
            return
        run.results["deliverables_shelf"] = result
        if result.get("refused"):
            self._record_shelf_refusal(run, result)
            return
        self._record_shelf_work(run, result)
        run.mark(BRANCH, True)

    def _record_shelf_refusal(self, run: MaintenanceRun, result: dict[str, Any]) -> None:
        """The resolver refused. NOTHING was written, and there is deliberately
        no fallback inside ``vault/`` — a shelf that cannot be placed safely is
        not placed. The refusal already carries its own ``notify_key``, which is
        what gets it out of the maintain result and onto ``brain alerts``."""
        run.action_required.append({
            **maintenance.action_required_item(
                result["finding"],
                "the shelf target could not be resolved safely, so no "
                "deliverable was copied out of the vault this run",
                "resolve the refusal named in the finding, then re-run "
                "`brain maintain`",
                "brain status --json (deliverables block)",
            ),
            "notify_key": result["notify_key"],
        })
        run.mark(BRANCH, False, result["notify_key"])

    def _record_shelf_work(self, run: MaintenanceRun, result: dict[str, Any]) -> None:
        """Report what the run actually moved. A run that copies nothing on an
        unchanged vault is the NORMAL case and stays silent — but it still
        audited the target, so anything it found there is reported."""
        moved = len(result["copied"]) + len(result["displaced"])
        if moved:
            run.auto_fixed.append(maintenance.auto_fixed_item(
                "deliverables-shelf",
                result["shelf"],
                f"{len(result['copied'])} copied, {len(result['displaced'])} "
                f"retired to _previous/ ({result['census']} deliverable(s) in "
                f"the census)",
            ))
        self._record_shelf_guards(run, result)
        if result["permission_failures"]:
            run.action_required.append(maintenance.action_required_item(
                f"{len(result['permission_failures'])} shelf path(s) are still "
                "group- or world-readable after the owner-only chmod",
                "the shelf carries payload bytes at their true tier, so another "
                "local account being able to read them is a confidentiality "
                "problem, not a cosmetic one",
                "check the filesystem's permission support and the umask of the "
                "account running the nightly",
                result["permission_failures"][0],
            ))

    def _record_shelf_guards(self, run: MaintenanceRun,
                             result: dict[str, Any]) -> None:
        """Surface the two guards that REFUSED to act this run.

        Both carry a stable ``notify_key``: without one an action-required item
        stays in the maintain result and never reaches ``brain alerts``
        (``maintenance_notify.py:71-73``), and ``hot.md`` is by AGENTS.md §9 "a
        LOG, not a must-read queue — the owner never has to open it". A guard
        nobody can find out about is a guard that failed silently.
        """
        if result["moves_held"] == deliverables_sync.HELD_BY_CAP:
            run.action_required.append({
                **maintenance.action_required_item(
                    f"deliverables shelf refused to move "
                    f"{result['moves_planned']} file(s) in one run — the cap "
                    f"is {result['move_cap']}",
                    "every move is recoverable (copies go to _previous/, "
                    "never to /dev/null), but a run this large usually means "
                    "the vault was read wrong rather than that it changed",
                    "check the census (`brain shelf census --json`) against "
                    "the shelf, then set $BRAIN_SHELF_MAX_MOVES for one run "
                    "if the count is genuine",
                    result["shelf"],
                ),
                "notify_key": deliverables_shelf.NOTIFY_MOVE_CAP,
            })
        if result["moves_held"] == deliverables_sync.HELD_BY_EMPTY_CENSUS:
            run.action_required.append({
                **maintenance.action_required_item(
                    f"deliverables shelf moved nothing: the census read zero "
                    f"deliverables while the ledger still claims "
                    f"{result['ledger_entries']} file(s)",
                    "a vault that could not be READ looks exactly like a "
                    "vault that emptied, and acting on it would retire every "
                    "live copy on the shelf in one run",
                    "confirm with `brain shelf census --json` — if the vault "
                    "really has no deliverables left, set "
                    "$BRAIN_SHELF_MAX_MOVES for one run to let the shelf "
                    "catch up",
                    result["shelf"],
                ),
                "notify_key": deliverables_shelf.NOTIFY_REFUSED,
            })
        if result["diverged"]:
            paths = ", ".join(d["path"] for d in result["diverged"][:3])
            run.action_required.append({
                **maintenance.action_required_item(
                    f"{len(result['diverged'])} shelf file(s) hold bytes the "
                    f"fold did not write and were left untouched: {paths}",
                    "an edited or hand-replaced shelf copy is never "
                    "overwritten or moved aside — but until it is resolved "
                    "that deliverable stops being updated",
                    "keep the edit by saving it somewhere else and deleting "
                    "the shelf copy, or discard it and let the next run "
                    "restore the vault's version",
                    result["shelf"],
                ),
                "notify_key": deliverables_shelf.NOTIFY_DIVERGED,
            })
