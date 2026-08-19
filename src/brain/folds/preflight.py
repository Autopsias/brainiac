"""Validate supersession recovery before maintenance branches run."""

from __future__ import annotations

from .context import MaintenanceRun
from .. import maintenance


class PreflightFoldsMixin:
    """Provide the ENF-01 maintenance preflight fold."""

    def journal_preflight_fold(self, run: MaintenanceRun) -> None:
        """Recover a pending supersede journal exactly once before any branch."""
        from ..core import SupersedeJournalUnreadable

        try:
            result = self.recover_pending_supersede(dry_run=run.dry_run)
            if not result:
                return
            run.results["supersede_journal"] = result
            if result.get("restored"):
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "supersede-journal",
                        result.get("journal", ""),
                        f"rolled back an interrupted {result.get('op')} "
                        f"({', '.join(result['restored'])} side(s) restored)",
                    )
                )
            elif result.get("pending"):
                run.action_required.append(
                    maintenance.action_required_item(
                        "a supersede crash journal is pending",
                        "--dry-run never writes, so it was reported not recovered",
                        "re-run `brain maintain` without --dry-run",
                        result.get("journal", ""),
                    )
                )
        except SupersedeJournalUnreadable as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"supersede crash journal unreadable: {exc}",
                    "the two notes it names + the audit log",
                    "a human repairing the pair and deleting the journal",
                )
            )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"supersede journal preflight failed: {exc}",
                    "index/write path",
                    "next maintain run",
                )
            )
