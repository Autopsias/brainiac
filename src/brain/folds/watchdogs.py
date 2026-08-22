"""Surface maintenance findings to the owner queue."""

from __future__ import annotations

from pathlib import Path

from typing import Any

from .context import MaintenanceRun
from .. import maintenance, maintenance_watchdog
from .. import remediation_state as rs


def _synthesis_retry_branch_cost(
    prev_marked_for: Any, row: dict[str, Any], entry: dict[str, Any] | None,
) -> tuple[float, int]:
    """SPD-01 — the cost attributable to THIS branch's own re-fire.

    Zero (a MEASURED zero, design-freeze (d) rule 2) unless a retry was
    previously marked AND the observation just called
    (``maintenance_watchdog.synthesis_retry_observation``, which mutates
    ``row`` in place) recorded a DIFFERENT ``retry_marked_for`` than before —
    that transition is the one run where a genuinely newer synthesis attempt
    landed since the retry was marked, i.e. the re-fire this branch exists to
    observe actually happened. Every other run, including the one that only
    marks intent, spent nothing of its own."""
    new_marked_for = row.get("retry_marked_for")
    if prev_marked_for is None or new_marked_for == prev_marked_for:
        return 0.0, 0
    cost = entry.get("est_cost_usd") if isinstance(entry, dict) else None
    tokens = entry.get("tokens") if isinstance(entry, dict) else None
    cost_usd = float(cost) if isinstance(cost, (int, float)) and cost > 0 else 0.0
    tokens_int = int(tokens) if isinstance(tokens, (int, float)) and tokens > 0 else 0
    return cost_usd, tokens_int


def _merge_remediation_branch_report(
    run: MaintenanceRun, branch: str, mode: str, cost_usd: float, tokens: int,
) -> None:
    """Fold ``synthesis_retry``'s own outcome into ``run.results["remediation"]``
    — the SAME per-run structure ``remediation_folds.BranchOutcome.report()``
    populates for the sign_repair/reguard/extract_retry branches, so
    ``maintenance_folds_2._remediation_fields`` sums cost across every branch
    without a special case for this one. ``remediation_fold`` runs earlier in
    ``daily_fold`` and always leaves a dict here (even when disabled or
    writer-busy), so this only ever ADDS a key, never replaces the dict."""
    rem = run.results.setdefault(
        "remediation", {"target_signatures": {}, "branches": {}})
    branches = rem.setdefault("branches", {})
    branches[branch] = {
        "branch": branch, "mode": mode, "healed": 0, "remaining": 0,
        "targets": 0, "skipped": 0, "cost_usd": cost_usd, "tokens": tokens,
    }


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
        """FIX-04: retry-by-scheduling. A failing/stale synthesis marks a
        retry-intent and stays quiet the FIRST time it is seen (maintain
        never re-fires model-backed work itself — the existing synthesis
        launchd lane does); only a SECOND same-condition sighting after the
        retry window escalates to the banner this fold has always produced."""
        try:
            finding = maintenance.synthesis_heartbeat_finding(
                Path(self.vault), run.date
            )
            vault = Path(self.vault)
            state = rs.read_state(vault)
            branches = dict(state.get("branches") or {})
            row = dict(branches.get("synthesis_retry") or {})
            if finding is None:
                if row.pop("retry_marked_for", None) is not None:
                    row.pop("retry_marked_on", None)
                    row["last_cost_usd"] = 0.0
                    branches["synthesis_retry"] = row
                    rs.write_state(vault, {"branches": branches})
                _merge_remediation_branch_report(
                    run, "synthesis_retry", "healthy", 0.0, 0)
                return
            _path, entry = maintenance_watchdog._load_synthesis_entry(vault)
            prev_marked_for = row.get("retry_marked_for")
            decision = maintenance_watchdog.synthesis_retry_observation(
                entry, run.date, row)
            # SPD-01: this branch's OWN re-fire only, never the whole night's
            # synthesis spend (already `synthesis_cost_usd` on the health
            # record — double-counting that figure under a second key is
            # exactly the failure mode the plan calls out). Nonzero only on
            # the run where a genuinely NEWER attempt has landed since a
            # retry was marked: every other run — including the one that
            # merely marks intent — reports a MEASURED zero.
            cost_usd, tokens = _synthesis_retry_branch_cost(
                prev_marked_for, row, entry)
            row["last_cost_usd"] = cost_usd
            branches["synthesis_retry"] = row
            rs.write_state(vault, {"branches": branches})
            _merge_remediation_branch_report(
                run, "synthesis_retry", decision, cost_usd, tokens)
            if decision == "retry-marked":
                self._append_hot_once(
                    f"synthesis-watchdog-retry:{run.date.isoformat()}",
                    f"## {run.date.isoformat()} — synthesis watchdog (retry marked)\n"
                    f"- **Finding:** {finding['finding']}\n"
                    "- **Next:** at most one re-fire before this escalates.\n",
                )
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
