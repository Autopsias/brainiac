"""Finalize one maintenance run into durable operational reporting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .context import MaintenanceRun
from .. import exceptions_page, maintenance, remediation_questions, remediation_routing


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
        """Fire best-effort alarms from the post-fold outcome snapshot.

        REG-03: every finding is ROUTED first. What a live, converging branch
        owns is suppressed to a ``hot.md`` line, owner-class findings become
        one inbox question each, log-class findings never banner — and only
        what is left reaches the marker/notification path unchanged. The
        router's state is read HOST-PRIVATE; ``run.state`` (the mount-visible
        maintain-state) is passed on only for the ES-01 branch-escalation
        findings it has always produced, never as suppression evidence."""
        try:
            router = remediation_routing.router_for(
                self, run.date,
                targets=remediation_routing.target_signatures(run.results),
                # EXC-01: what each owner-class finding is ABOUT. `subjects`
                # keys a singleton question on the population the detector
                # found (and names it in the question); `pairs` is what the
                # cross-tier batch stages one proposal from. Both come from
                # THIS run's `corpus_invariants` metrics — a run without them
                # converts nothing and the findings banner, unchanged.
                subjects=remediation_questions.subjects_from_results(run.results),
                pairs=remediation_questions.pairs_from_results(run.results))
        except Exception:  # noqa: BLE001 — no router means everything banners
            router = None
        try:
            candidates = maintenance.pending_notifications(
                Path(self.vault),
                run.outcomes(),
                trend_findings,
                run.date,
                maintain_state=run.state,
                router=router,
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
            # EXC-02: the exceptions page reads the owner queue + the
            # findings feed `health_history_fold` (above) just settled, so
            # it regenerates AFTER both, same best-effort posture as the
            # health report right above it — a rendering bug here must
            # never fail the maintain run.
            try:
                run.results["exceptions_page"] = exceptions_page.generate(
                    self, today=run.date)
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
                # FIX-04/registry: `update:applied` is LOG, not a banner —
                # "good news is not an alert". A dated hot.md line is the
                # whole record; `brain alerts`' `update_alerts()` no longer
                # surfaces this status at all (the dead banner it used to
                # repeat for up to 7 days is deleted, not just quieted here).
                self._append_hot_once(
                    f"maintain:auto-update:{run.date.isoformat()}",
                    f"## {run.date.isoformat()} — auto-update\n"
                    f"- **Applied:** engine auto-updated to "
                    f"{result.get('latest')}\n",
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
            elif result.get("auto_update") == "escalated":
                # FIX-04: bounded retries exhausted (`brain_update.
                # retry_decision`) — no more automatic attempts for this
                # version. `brain alerts`' `update_alerts()` already renders
                # this from the host-global state; the blocked entry is this
                # RUN's own record of it.
                run.blocked.append(
                    maintenance.blocked_item(
                        f"auto-update to {result.get('latest')}: "
                        f"{result.get('reason')}",
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
