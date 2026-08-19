"""Orchestrate every fold inside the single sanctioned maintenance lock."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .context import MaintenanceRun
from .. import config, maintenance


class MaintenanceOrchestratorMixin:
    """Provide the short host-broker maintenance entry point."""

    def maintain(
        self,
        *,
        dry_run: bool = False,
        today: Any = None,
        min_score: float = 0.95,
        near_dup_k: int = 5,
        graphify_runner: Any = None,
        golden_runner: Any = None,
    ) -> dict[str, Any]:
        """Run the persistence-budget.md umbrella under THE LOCK.

        The inherited fold map stays daily; Mon->health, Tue->integrity,
        Wed->graph hygiene, Sun->digest/golden, 1st-of-month->graphify. ADR-0003
        Ruling 6 keeps graphify's monthly floor; FRESH-01 may trigger it early.
        Supersession-journal recovery runs exactly once before every branch.
        """
        self._require_host("run the maintain umbrella")
        date = today or dt.date.today()
        lock_path = config.maintain_lock_path(self.vault)
        lock_info = self._acquire_maintain_lock(lock_path)
        if lock_info is None:
            held = self._read_maintain_lock(lock_path)
            return {
                "ritual": "maintain",
                "dry_run": dry_run,
                "date": date.isoformat(),
                "skipped": "locked",
                "note": "another maintain run holds the lock "
                f"(pid={held.get('pid')}, started={held.get('started')}) — "
                "skipping this run",
                "outcomes": maintenance.build_outcomes(),
            }
        try:
            state = self._load_maintain_state()
            last_runs = {
                key: (value.get("last_run") if isinstance(value, dict) else value)
                for key, value in state.items()
                if not str(key).startswith("_")
            }
            run = MaintenanceRun(
                core=self,
                dry_run=dry_run,
                date=date,
                state=state,
                branches=maintenance.maintain_branches(date, last_runs=last_runs),
                min_score=min_score,
                near_dup_k=near_dup_k,
                graphify_runner=graphify_runner,
                golden_runner=golden_runner,
            )
            self.journal_preflight_fold(run)
            writer_busy = self.daily_fold(run)
            if writer_busy is not None:
                return writer_busy
            drift_triggered = self.graphify_drift_fold(run)
            self.health_fold(run)
            self.integrity_fold(run)
            self.graph_hygiene_fold(run)
            self.digest_fold(run)
            self.golden_fold(run)
            self.graphify_fold(run, drift_triggered)
            self.health_history_fold(run)
            return self.finalize_maintenance_fold(run)
        finally:
            self._release_maintain_lock(lock_path)
