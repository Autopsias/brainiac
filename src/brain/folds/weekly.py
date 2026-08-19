"""Run the calendar-gated weekly maintenance branches."""

from __future__ import annotations

from .context import MaintenanceRun
from .. import graph as graph_mod
from .. import maintenance


class WeeklyFoldsMixin:
    """Provide health, integrity, hygiene, and digest folds."""

    def health_fold(self, run: MaintenanceRun) -> None:
        """Run the Monday health branch when due."""
        if "health" not in run.branches:
            return
        try:
            result = self.health()
            framework_finding = self._framework_sync_finding()
            if framework_finding is not None:
                result["outcomes"]["action_required"].append(framework_finding)
            run.results["health"] = result
            run.action_required += result["outcomes"]["action_required"]
            run.blocked += result["outcomes"]["blocked"]
            run.mark("health", True)
        except Exception as exc:
            self._record_weekly_failure(run, "health", exc)

    def integrity_fold(self, run: MaintenanceRun) -> None:
        """Run the Tuesday integrity branch when due."""
        if "integrity" not in run.branches:
            return
        try:
            result = self.integrity(min_score=run.min_score, k=run.near_dup_k)
            run.results["integrity"] = result
            run.blocked += result.get("blocked", [])
            if result.get("audit_issue"):
                run.action_required.append(result["audit_issue"])
            pairs = result.get("near_dup_pairs")
            if pairs:
                run.action_required.append(
                    maintenance.action_required_item(
                        f"{len(pairs)} near-duplicate pair(s) found >= {run.min_score}",
                        "de-dup is a human merge/keep judgment, never auto-merged",
                        "run `brain integrity --json` for the gated pair list and review",
                        "near-dup scan",
                    )
                )
            run.mark("integrity", True)
        except Exception as exc:
            self._record_weekly_failure(run, "integrity", exc)

    def graph_hygiene_fold(self, run: MaintenanceRun) -> None:
        """Run the Wednesday graph-hygiene branch when due."""
        if "graph_hygiene" not in run.branches:
            return
        try:
            metrics = graph_mod.graph_hygiene_metrics(self.index.conn)
            previous = run.state.get("graph_hygiene")
            previous = previous if isinstance(previous, dict) else {}
            previous_metrics = previous.get("metrics")
            previous_metrics = (
                previous_metrics if isinstance(previous_metrics, dict) else None
            )
            growth = maintenance.graph_hygiene_orphan_growth(previous_metrics, metrics)
            run.results["graph_hygiene"] = metrics
            run.state["graph_hygiene"] = {**previous, "metrics": metrics}
            self._surface_graph_hygiene_growth(run, metrics, growth)
            if not run.dry_run:
                try:
                    self.graph_report(today=run.date)
                except Exception:  # noqa: BLE001
                    pass
            run.mark("graph_hygiene", True)
        except Exception as exc:
            self._record_weekly_failure(run, "graph_hygiene", exc)

    def _surface_graph_hygiene_growth(
        self,
        run: MaintenanceRun,
        metrics: dict[str, object],
        growth: int | None,
    ) -> None:
        """Queue an owner finding when graph orphan growth breaches its budget."""
        if run.dry_run or not maintenance.should_alert_graph_hygiene_growth(growth):
            return
        try:
            self._append_hot_once(
                f"maintain:graph_hygiene:{run.date.isoformat()}:"
                f"{metrics.get('orphan_count')}",
                maintenance.render_graph_hygiene_hot_entry(metrics, growth, run.date),
            )
        except Exception as exc:  # noqa: BLE001
            run.action_required.append(
                maintenance.action_required_item(
                    "graph-hygiene hot-queue entry could not be written",
                    f"{type(exc).__name__}: {exc}",
                    "check .brain/memory/hot.md writability; the metrics "
                    "themselves were computed fine",
                    "graph hygiene",
                )
            )

    def digest_fold(self, run: MaintenanceRun) -> None:
        """Run the Sunday digest, curation, promotion, and retro branch."""
        if "digest" not in run.branches:
            return
        try:
            run.results["digest"] = self.digest(days=7)
            curate_result = self.curate(dry_run=run.dry_run, today=run.date)
            promote_result = self.promote_scan()
            run.results["curate"] = curate_result
            run.results["promote_scan"] = promote_result
            if not run.dry_run:
                self._publish_digest_artifacts(run, curate_result, promote_result)
            run.mark("digest", True)
        except Exception as exc:
            self._record_weekly_failure(
                run, "digest", exc, "digest branch (digest/curate/promote-scan) raised"
            )

    def _publish_digest_artifacts(
        self,
        run: MaintenanceRun,
        curate_result: dict[str, object],
        promote_result: dict[str, object],
    ) -> None:
        """Publish due Sunday artifacts through idempotent owner-queue keys."""
        stale_links = curate_result.get("stale_links")
        revisit_sample = curate_result.get("revisit_sample")
        if stale_links or revisit_sample:
            self._append_hot_once(
                "maintain:curate:" + maintenance.curation_finding_key(stale_links),
                maintenance.render_curation_hot_entry(
                    stale_links, revisit_sample, run.date
                ),
            )
        candidates = promote_result.get("candidates")
        if candidates:
            self._append_hot_once(
                "maintain:promote-scan:"
                + maintenance.promote_scan_finding_key(candidates),
                maintenance.render_promote_scan_hot_entry(candidates, run.date),
            )
        run.results["digest_html"] = self.digest_html(days=7, today=run.date)
        retro_result = self.retro(today=run.date)
        run.results["retro"] = retro_result
        written = retro_result["feedback_written"]
        if written:
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "retro",
                    "engine-feedback/",
                    f"filed {len(written)} engine bug prompt(s): {', '.join(written)}",
                )
            )

    def _record_weekly_failure(
        self,
        run: MaintenanceRun,
        branch: str,
        exc: Exception,
        description: str | None = None,
    ) -> None:
        """Record one isolated weekly branch failure without aborting siblings."""
        detail = f"{type(exc).__name__}: {exc}"
        run.blocked.append(
            maintenance.blocked_item(
                description or f"{branch} branch raised",
                detail,
                "re-run maintain after the underlying error is fixed",
            )
        )
        run.mark(branch, False, detail)
