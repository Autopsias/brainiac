"""Schedule graphify from drift or monthly floors."""

from __future__ import annotations

import json
import os
from typing import Any

from .context import MaintenanceRun
from .. import config, maintenance


class GraphFoldsMixin:
    """Provide graphify scheduling folds."""

    def graphify_drift_fold(self, run: MaintenanceRun) -> bool:
        """Measure FRESH-01 corpus drift without building the graph."""
        triggered = False
        try:
            try:
                old_manifest = json.loads(
                    config.graph_manifest_path(self.vault).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                old_manifest = None
            ratio = maintenance.graphify_drift(old_manifest, self.index.conn)
            marker = run.state.get("_graphify_drift")
            marker = marker if isinstance(marker, dict) else None
            triggered = bool(
                old_manifest
            ) and maintenance.should_trigger_drift_graphify(ratio, marker, run.date)
            run.results["graphify_drift"] = {
                "ratio": round(ratio, 4),
                "triggered": triggered,
                "has_baseline": bool(old_manifest),
            }
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"graphify drift check failed: {exc}",
                    "index read",
                    "next maintain run",
                )
            )
        return triggered

    def graphify_fold(self, run: MaintenanceRun, drift_triggered: bool) -> None:
        """Run the unified monthly-floor or FRESH-01 drift graph build."""
        monthly_due = "graphify" in run.branches
        if not (monthly_due or drift_triggered):
            return
        marker = run.state.get("_graphify_drift")
        marker = marker if isinstance(marker, dict) else None
        cooldown_days = int(
            os.environ.get(
                maintenance.GRAPHIFY_COOLDOWN_DAYS_ENV,
                maintenance.DEFAULT_GRAPHIFY_COOLDOWN_DAYS,
            )
        )
        if not maintenance.graphify_drift_marker_due(marker, run.date, cooldown_days):
            run.results["graphify"] = {
                "ritual": "graphify",
                "invoked": False,
                "published": False,
                "status": "cooldown_deferred",
                "note": "a recent failed attempt is backing off; the build "
                "retries once the (exponential) cooldown elapses",
            }
            return
        reason = "drift" if drift_triggered else "monthly-floor"
        try:
            result = self._run_bounded_graphify(
                force=False,
                dry_run=run.dry_run,
                today=run.date,
                state=run.state,
                reason=reason,
                builder=run.graphify_runner,
            )
            run.results["graphify"] = result
            self._surface_graphify_candidates(run, result)
            self._record_graphify_outcome(run, result, reason, monthly_due)
        except Exception as exc:  # noqa: BLE001
            run.blocked.append(
                maintenance.blocked_item(
                    "graphify branch raised (maintain-side handling)",
                    f"{type(exc).__name__}: {exc}",
                    "re-run maintain after the underlying error is fixed",
                )
            )

    def _surface_graphify_candidates(
        self, run: MaintenanceRun, result: dict[str, Any]
    ) -> None:
        """Queue published graph candidates without changing build success."""
        if run.dry_run or not result.get("published") or not result.get("candidates"):
            return
        try:
            self._append_hot_once(
                f"maintain:graphify:{run.date.isoformat()}",
                maintenance.render_graphify_hot_entry(result["candidates"], run.date),
            )
        except Exception as exc:  # noqa: BLE001
            run.action_required.append(
                maintenance.action_required_item(
                    "graphify hot-queue entry could not be written",
                    f"{type(exc).__name__}: {exc}",
                    "check .brain/memory/hot.md writability; the graph itself "
                    "published fine",
                    "graphify hot-queue",
                )
            )

    def _record_graphify_outcome(
        self,
        run: MaintenanceRun,
        result: dict[str, Any],
        reason: str,
        monthly_due: bool,
    ) -> None:
        """Translate one bounded build result into outcomes and branch state."""
        build = result.get("build") or {}
        published = bool(result.get("published"))
        skipped = bool(result.get("skipped"))
        preview = bool(result.get("dry_run"))
        duration = build.get("duration_seconds")
        duration_suffix = f" ({duration}s)" if duration is not None else ""
        if preview and build.get("action_required"):
            duration_text = (
                f"{duration}s" if duration is not None else "an unknown duration"
            )
            run.action_required.append(
                maintenance.action_required_item(
                    f"graphify dry-run build took {duration_text} "
                    f"(> {build.get('action_required_seconds')}s soft budget)",
                    "the PREVIEW build exceeded the soft wall-clock budget — the "
                    "real scheduled build likely will too",
                    "investigate corpus scale / vector backend before the next "
                    "scheduled build",
                    "graphify build",
                )
            )
        elif not preview and not published and not skipped:
            self._record_graphify_failure(run, result, reason, duration_suffix)
        elif not preview and published and build.get("action_required"):
            self._record_slow_graphify(run, build, reason, duration)
        if preview:
            return
        if published or skipped:
            run.mark("graphify", True)
        elif monthly_due:
            run.mark("graphify", False, result.get("status", "build_failed"))

    def _record_graphify_failure(
        self,
        run: MaintenanceRun,
        result: dict[str, Any],
        reason: str,
        duration_suffix: str,
    ) -> None:
        """Record a failed bounded graph build in both alarm channels."""
        summary = (
            f"graphify build ({reason}, status={result.get('status', 'unknown')}) "
            f"failed to complete{duration_suffix}"
        )
        run.blocked.append(
            maintenance.blocked_item(
                summary,
                "the in-process graph build raised, returned a bad result, or "
                "failed to build/validate (build_failed/invalid_artifact)",
                "capped exponential backoff will retry automatically once the "
                "cooldown elapses",
            )
        )
        run.action_required.append(
            maintenance.action_required_item(
                summary,
                "the in-process graph build raised, returned a bad result, or "
                "failed to build/validate",
                "inspect the result's error/status detail and "
                ".brain/graph/BUILD_FAILED.json; capped exponential backoff will "
                "retry automatically once the cooldown elapses",
                "graphify build",
            )
        )

    def _record_slow_graphify(
        self,
        run: MaintenanceRun,
        build: dict[str, Any],
        reason: str,
        duration: object,
    ) -> None:
        """Record a successful graph build that exceeded its soft budget."""
        duration_text = (
            f"{duration}s" if duration is not None else "an unknown duration"
        )
        run.action_required.append(
            maintenance.action_required_item(
                f"graphify build ({reason}) published but took {duration_text} "
                f"(> {build.get('action_required_seconds')}s soft budget)",
                "the graph build exceeded its soft wall-clock budget but completed "
                "and published successfully",
                "investigate corpus scale / vector backend before the next scheduled "
                "build — no retry is needed, this build already succeeded",
                "graphify build",
            )
        )
