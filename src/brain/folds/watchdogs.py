"""Surface maintenance findings to the owner queue."""

from __future__ import annotations

from pathlib import Path

from .context import MaintenanceRun
from .. import maintenance


class WatchdogFoldsMixin:
    """Provide owner-facing maintenance watchdog folds."""

    def quarantine_summary_fold(self, run: MaintenanceRun) -> None:
        """Queue one non-destructive quarantine summary per month."""
        try:
            marker = run.state.get("_quarantine_summary")
            marker = marker if isinstance(marker, dict) else None
            if not maintenance.quarantine_summary_due(marker, run.date):
                return
            summary = maintenance.quarantine_triage_summary(Path(self.vault), run.date)
            run.results["quarantine_summary"] = summary
            if summary["total"]:
                self._append_hot_once(
                    f"quarantine-summary:{run.date.strftime('%Y-%m')}",
                    maintenance.render_quarantine_summary_hot_entry(summary, run.date),
                )
                # Burn the month's only slot only when the
                # summary REPORTED something — an empty 00:07
                # run used to blind the rest of the month.
                run.state["_quarantine_summary"] = {
                    "last_month": run.date.strftime("%Y-%m")
                }
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"quarantine triage summary failed: {exc}",
                    "filesystem read",
                    "next maintain run",
                )
            )

    def decision_capture_fold(self, run: MaintenanceRun) -> None:
        """Nudge possible uncaptured decisions without creating notes."""
        try:
            candidates = maintenance.decision_capture_scan(self.index.conn, run.date)
            run.results["decision_capture"] = {"candidates": len(candidates)}
            for candidate in candidates:
                if self._append_hot_once(
                    f"decision-capture:{candidate['id']}",
                    maintenance.render_decision_capture_hot_entry(candidate, run.date),
                ):
                    run.action_required.append(
                        maintenance.action_required_item(
                            f"possible uncaptured decision in `{candidate['id']}` "
                            f"(“{candidate['phrase']}”)",
                            "recording a decision note is a human gate — the fold "
                            "only nudges",
                            "review the hot.md entry; if real, capture a type: decision "
                            "note (+ supersede what it reverses)",
                            candidate["id"],
                        )
                    )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"decision-capture scan failed: {exc}",
                    "index read",
                    "next maintain run",
                )
            )

    def synthesis_watchdog_fold(self, run: MaintenanceRun) -> None:
        """Report a stale weekly synthesis heartbeat at most once per ISO week."""
        try:
            finding = maintenance.synthesis_heartbeat_finding(
                Path(self.vault), run.date
            )
            if finding is None:
                return
            run.action_required.append(finding)
            week = run.date.isocalendar()
            self._append_hot_once(
                f"synthesis-watchdog:{week[0]}-W{week[1]}",
                f"## {run.date.isoformat()} — synthesis watchdog\n"
                f"- **Finding:** {finding['finding']}\n"
                f"- **Owner input needed:** {finding['proposed_action']}\n",
            )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"synthesis watchdog failed: {exc}",
                    "state file read",
                    "next maintain run",
                )
            )
