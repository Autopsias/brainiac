"""Orchestrate the unconditional nightly fold."""

from __future__ import annotations

from typing import Any

from .context import MaintenanceRun
from .. import maintenance
from ..lock import WriterLockBusy


class DailyFoldsMixin:
    """Provide the daily maintenance branch."""

    def daily_fold(self, run: MaintenanceRun) -> dict[str, Any] | None:
        """Run intake, reconciliation, organization, retention, and publication."""
        if run.dry_run:
            run.results["status"] = self.status()
            return None
        self.future_artifact_fold(run)
        self.workspace_sweep_fold(run)
        self.provision_drain_fold(run)
        self.cos_ingest_sweep_fold(run)
        self.cos_broker_summary_fold(run)
        try:
            self.sync_reconcile_fold(run)
            # AFTER the sync and BEFORE the publish, and both halves matter.
            # After: `reguard` asks the ENF-04 guard about a corpus that
            # INCLUDES this run's admissions — judged against the pre-sync
            # index, a source whose higher-tier twin arrived this hour is
            # stamped `clear` and leaves the unguarded population for good.
            # Before: `publish_daily_fold` reconciles fold mutations and
            # republishes the snapshot at the end of this block, so a raised
            # tier still reaches the index and the VM's snapshot in the SAME
            # run instead of leaking through the low copy for another hour.
            self.remediation_fold(run)
            self.version_chain_fold(run)
            self.auto_dedup_fold(run)
            self.auto_para_fold(run)
            self.navigation_fold(run)
            self.knowledge_orphan_fold(run)
            self.corpus_invariants_fold(run)
            self.retention_schedule_fold(run)
            self.quarantine_summary_fold(run)
            self.decision_capture_fold(run)
            self.synthesis_watchdog_fold(run)
            self.publish_daily_fold(run)
        except WriterLockBusy as exc:
            self._mark_writer_busy(run.state, "daily", run.date, exc)
            self._save_maintain_state(run.state)
            return {
                "ritual": "maintain",
                "dry_run": run.dry_run,
                "date": run.date.isoformat(),
                "status": "skipped-writer-busy",
                "note": f"writer busy (held by pid={exc.holder.get('pid')}, "
                f"verb={exc.holder.get('verb')}) — skipping this run",
                "held_by": exc.holder,
                "consecutive_skips": run.state.get("daily", {}).get(
                    "consecutive_skips"
                ),
                "outcomes": run.outcomes(),
            }
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    "daily branch (sync/brief/recommendations-aging) raised",
                    f"{type(exc).__name__}: {exc}",
                    "re-run maintain after the underlying error is fixed",
                )
            )
            run.mark("daily", False, f"{type(exc).__name__}: {exc}")
        return None
