"""Measure nightly corpus invariants."""

from __future__ import annotations

import json
from pathlib import Path

from .context import MaintenanceRun
from .. import graph, invariants, maintenance


class InvariantFoldsMixin:
    """Provide the LNK-03b and WAT-01 measurement folds."""

    def knowledge_orphan_fold(self, run: MaintenanceRun) -> None:
        """Measure cheap daily knowledge-layer orphan count and sustained growth."""
        try:
            metrics = graph.graph_hygiene_metrics(self.index.conn)
            orphan_count = metrics.get("orphan_count", 0)
            run.results["kl_orphans"] = {"orphan_count": orphan_count}
            previous = (
                run.state.get("daily")
                if isinstance(run.state.get("daily"), dict)
                else {}
            )
            run.state["daily"] = {**previous, "kl_orphans": orphan_count}
            if run.dry_run:
                return
            history = maintenance.read_health_history(Path(self.vault))
            growth = maintenance.kl_orphan_sustained_growth(history, orphan_count)
            if maintenance.should_alert_kl_orphan_sustained_growth(growth):
                iso = run.date.isocalendar()
                self._append_hot_once(
                    f"maintain:kl-orphans-sustained:{iso[0]}-W{iso[1]:02d}",
                    maintenance.render_kl_orphan_sustained_growth_hot_entry(
                        orphan_count, growth, run.date
                    ),
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"kl_orphans daily counter failed: {exc}",
                    "index read",
                    "next maintain run",
                )
            )

    def corpus_invariants_fold(self, run: MaintenanceRun) -> None:
        """Measure WAT-01 metrics, ratchet floors, and emit the BAK-04 link lane."""
        try:
            metrics = invariants.corpus_invariants(self.index.conn, Path(self.vault))
            values = invariants.metric_values(metrics)
            previous = (
                run.state.get(invariants.STATE_KEY)
                if isinstance(run.state.get(invariants.STATE_KEY), dict)
                else {}
            )
            # A golden-set expansion changes unreachable_gold's denominator;
            # the floor re-seeds instead of alerting nightly against a
            # different-sized label set.
            previous_floors = invariants.rebase_unreachable_floor(
                previous.get("floors"), metrics
            )
            # FIX-05: unlinked_sources' raw count is no longer ratcheted — the
            # weekly linking lane works that backlog down, so the count
            # growing is ordinary intake, not a defect. The other seven
            # metrics' floor/regression/decline behaviour is untouched.
            ratchet_values = {k: v for k, v in values.items() if k != "unlinked_sources"}
            regressions = invariants.invariant_regressions(previous_floors, ratchet_values)
            # A defect count improves when the corpus SHRINKS, so a run taken
            # while documents were missing must not set a floor no healthy
            # vault can match (invariant_floors).
            declined = invariants.declined_floors(previous_floors, ratchet_values, metrics)
            floors = invariants.update_floors_guarded(previous_floors, ratchet_values, metrics)

            lane_health_state = self._lane_health_check(run, previous, metrics)
            lane_regression = invariants.lane_health_regression(lane_health_state)
            if lane_regression:
                regressions = [*regressions, lane_regression]

            run.results["corpus_invariants"] = metrics
            run.state[invariants.STATE_KEY] = {
                **previous,
                # Truncated at the PERSISTENCE boundary: `metrics` carries an
                # unbounded population list for the repair branch, and this
                # dict is rewritten into maintain-state.json every hour.
                "metrics": invariants.persistable_metrics(metrics),
                "floors": floors,
                "regressions": regressions,
                "floors_declined": declined,
                invariants.LANE_HEALTH_STATE_SUBKEY: lane_health_state,
            }
            if not run.dry_run and regressions:
                self._append_hot_once(
                    f"maintain:corpus-invariants:{run.date.isoformat()}",
                    invariants.render_invariants_hot_entry(
                        regressions, metrics, run.date
                    ),
                )
            if not run.dry_run:
                run.results["link_lane"] = invariants.write_link_lane(
                    self.index.conn, Path(self.vault)
                )
            run.mark(invariants.STATE_KEY, True)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"corpus-invariants watchdog failed: {exc}",
                    "index read",
                    "next maintain run",
                )
            )
            run.mark("corpus_invariants", False, f"{type(exc).__name__}: {exc}")

    def _lane_health_check(
        self, run: MaintenanceRun, previous: dict, metrics: dict
    ) -> dict:
        """FIX-05: one ``lane_health()`` check, read off the worklist as it
        stood BEFORE this run's own ``write_link_lane`` call below — reading
        it after would always see a file this run just refreshed, and
        ``worklist_fresh`` could never fail short of the whole fold crashing
        (already covered by WAT-01's own dead-man's switch)."""
        lane_path = Path(self.vault) / invariants.LINK_LANE_RELPATH
        try:
            lane_payload = json.loads(lane_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lane_payload = None
        population = int((metrics.get("unlinked_sources") or {}).get("population") or 0)
        previous_lane_health = previous.get(invariants.LANE_HEALTH_STATE_SUBKEY)
        first_seen = (
            previous_lane_health.get("first_seen")
            if isinstance(previous_lane_health, dict) else None
        )
        check = invariants.lane_health(
            lane_payload, population=population, first_seen=first_seen, today=run.date
        )
        return invariants.update_lane_health(previous_lane_health, check, run.date)
