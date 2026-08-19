"""Finalize one maintenance run into durable operational reporting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .context import MaintenanceRun
from .. import maintenance


class ReportingFoldsMixin:
    """Provide health-history, update, and state-finalization folds."""

    def health_history_fold(self, run: MaintenanceRun) -> None:
        """Append exactly one OBS health record before evaluating notifications."""
        health_record: dict[str, Any] | None = None
        trend_findings: list[dict[str, Any]] = []
        notifications: list[str] = []
        try:
            health_record = maintenance.collect_health_metrics(
                self,
                outcomes=run.outcomes(),
                results=run.results,
                run_id=maintenance.new_health_run_id(),
            )
            if not run.dry_run:
                maintenance.append_health_record(Path(self.vault), health_record)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"health-history/trend/notify fold failed: {exc}",
                    "metrics collection or file I/O",
                    "next maintain run",
                )
            )
        if not run.dry_run:
            trend_findings = self._read_health_trend(run)
            notifications = self._fire_health_notifications(run, trend_findings)
        run.results["health_history"] = health_record
        run.results["health_trend"] = trend_findings
        run.results["notifications"] = notifications

    def _read_health_trend(self, run: MaintenanceRun) -> list[dict[str, Any]]:
        """Read best-effort health trends from dense and sparse histories."""
        try:
            history = maintenance.read_health_history(Path(self.vault))
            sparse_history = maintenance.read_sparse_history(Path(self.vault))
            return maintenance.health_trend(
                history, run.date, sparse_history=sparse_history
            )
        except Exception:  # noqa: BLE001
            return []

    def _fire_health_notifications(
        self, run: MaintenanceRun, trend_findings: list[dict[str, Any]]
    ) -> list[str]:
        """Fire best-effort alarms from the post-fold outcome snapshot."""
        try:
            candidates = maintenance.pending_notifications(
                Path(self.vault),
                run.outcomes(),
                trend_findings,
                run.date,
                maintain_state=run.state,
            )
            return maintenance.fire_and_mark_notifications(
                Path(self.vault), candidates, run.date
            )
        except Exception:  # noqa: BLE001
            return []

    def finalize_maintenance_fold(self, run: MaintenanceRun) -> dict[str, Any]:
        """Persist the authoritative state then render the public run result."""
        if not run.dry_run:
            self.auto_update_fold(run)
            try:
                self._rotate_hot_md(run.date)
            except Exception:  # noqa: BLE001
                pass
            self._save_maintain_state(run.state)
            try:
                self.health_report(today=run.date)
            except Exception:  # noqa: BLE001
                pass
        return {
            "ritual": "maintain",
            "dry_run": run.dry_run,
            "date": run.date.isoformat(),
            "weekday": run.date.strftime("%A"),
            "branches_due": run.branches,
            "results": run.results,
            "outcomes": run.outcomes(),
        }

    def auto_update_fold(self, run: MaintenanceRun) -> None:
        """Apply the scheduled engine update without failing maintenance."""
        try:
            result = self._maybe_auto_update(run.date)
            run.results["auto_update"] = result
            if result.get("auto_update") == "applied":
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "auto-update",
                        "brain update",
                        f"auto-updated engine to {result.get('latest')}",
                    )
                )
            elif result.get("auto_update") == "failed":
                run.blocked.append(
                    maintenance.blocked_item(
                        f"auto-update to {result.get('latest')} FAILED: "
                        f"{result.get('notes')}",
                        "update pipeline",
                        "run `brain update` manually",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            run.blocked.append(
                maintenance.blocked_item(
                    f"auto-update check/apply raised: {type(exc).__name__}: {exc}",
                    "update machinery",
                    "next maintain run",
                )
            )
