"""FIX-05 — BAK-04 lane health: `invariant:unlinked_sources` rekeyed off
whether the weekly linking lane is WORKING, not off the raw backlog count.

The backlog grows every time a new `raw/` source lands and shrinks only when
the weekly synthesis session links some of it — so the raw count is mostly
INTAKE noise, not a defect. `invariants.py`'s min-ever ratchet stays wired to
the other seven metrics; this metric alerts only when the lane itself stops
working.

**Every leg is ABSOLUTE — read off the CURRENT worklist only** (WAT-01
doctrine, dual-model hardening 2026-08-20): a week-over-week comparison "dies
at zero" exactly like the ratchet this replaces, so none of the three legs
below compares today's reading to a prior one.

1. ``worklist_fresh``          — the lane's own worklist
   (`invariant_coverage.LINK_LANE_RELPATH`) was produced within
   ``LANE_MAX_AGE_DAYS``.
2. ``consumption_recorded``    — the lane's own stalled-consumer signal
   (`invariant_coverage.lane_consumption`'s docstring): candidates exist and
   NONE left the population today.
3. ``backlog_within_headroom`` — the lane consumed its full weekly budget and
   the backlog is STILL above that budget — a structural overload, not a lazy
   session (a session that did not use its full budget fails leg 2 instead).

A single unhealthy check is not a finding: the escalation counts DISTINCT ISO
WEEKS unhealthy, matching the registry's own accounting
(``remediation.BRANCH_CADENCE_DAYS["link_lane"] == 7``,
``invariant:unlinked_sources``'s ``escalate_after == 2`` — "2 MISSED WEEKS at
the link_lane cadence"). Any healthy check resets the streak immediately.

**"No data yet" is not "the lane is dead."** A vault with zero linkable raw
sources, or one whose lane-health history is younger than
``LANE_MIN_HISTORY_DAYS``, reports ``not_applicable`` and never regresses —
the counter-move the session brief names for a fresh install.
"""
from __future__ import annotations

import datetime
from typing import Any

#: The worklist must have been (re)produced within this many days.
LANE_MAX_AGE_DAYS = 8
#: A vault (or a lane-health check) younger than this has had no chance to
#: build the history the legs above reason over.
LANE_MIN_HISTORY_DAYS = 14
#: Distinct unhealthy ISO weeks before the regression fires — "2 MISSED
#: WEEKS at the link_lane cadence" (remediation.py's own registry note).
LANE_HEALTH_ESCALATE_AFTER = 2

STATE_SUBKEY = "lane_health"

NOT_APPLICABLE, HEALTHY, UNHEALTHY = "not_applicable", "healthy", "unhealthy"


def _iso_week(d: datetime.date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _lane_age_days(generated: Any, today: datetime.date) -> int | None:
    if not generated:
        return None
    try:
        return (today - datetime.date.fromisoformat(str(generated))).days
    except (TypeError, ValueError):
        return None


def lane_health(
    lane: dict[str, Any] | None,
    *,
    population: int,
    first_seen: str | None,
    today: datetime.date,
) -> dict[str, Any]:
    """One ABSOLUTE check of the current worklist. Never raises.

    ``lane`` is the parsed ``LINK_LANE_RELPATH`` JSON (or ``None`` if it has
    never been written). ``population`` is ``unlinked_sources()``'s own
    linkable-population count — zero means there is nothing the lane could
    ever have worked. ``first_seen`` is the date this check first ran on this
    vault (persisted by the caller, ``None`` on the very first call).

    Returns ``{"applicable", "healthy", "legs", "reason"}``.
    """
    if population <= 0:
        return {"applicable": False, "healthy": True, "legs": {},
                "reason": "no linkable raw sources yet"}
    try:
        history_days = (
            0 if not first_seen else
            (today - datetime.date.fromisoformat(str(first_seen))).days)
    except ValueError:
        history_days = 0
    if history_days < LANE_MIN_HISTORY_DAYS:
        return {"applicable": False, "healthy": True, "legs": {},
                "reason": f"only {history_days}d of lane-health history "
                          f"(< {LANE_MIN_HISTORY_DAYS}d)"}

    if not isinstance(lane, dict) or not lane.get("generated"):
        return {"applicable": True, "healthy": False,
                "legs": {"worklist_fresh": False},
                "reason": "no worklist has ever been produced"}

    age = _lane_age_days(lane.get("generated"), today)
    worklist_fresh = age is not None and age <= LANE_MAX_AGE_DAYS
    total_unlinked = int(lane.get("total_unlinked") or 0)
    budget = int(lane.get("budget") or 0)
    consumed_today = int(lane.get("consumed_today") or 0)
    # The stalled-consumer signal `lane_consumption` was built for: candidates
    # exist and nothing left the population today.
    consumption_recorded = total_unlinked == 0 or consumed_today > 0
    # The lane worked its FULL budget and the backlog is still above it — a
    # structural overload, not a session that simply skipped this week (that
    # is `consumption_recorded` failing instead).
    full_budget_used = budget > 0 and consumed_today >= budget
    backlog_within_headroom = not (full_budget_used and total_unlinked > budget)

    legs = {"worklist_fresh": worklist_fresh, "consumption_recorded": consumption_recorded,
            "backlog_within_headroom": backlog_within_headroom, "age_days": age,
            "total_unlinked": total_unlinked, "budget": budget,
            "consumed_today": consumed_today}
    reasons = []
    if not worklist_fresh:
        reasons.append("worklist has never been produced" if age is None
                        else f"worklist {age}d old (> {LANE_MAX_AGE_DAYS}d)")
    if not consumption_recorded:
        reasons.append(f"{total_unlinked} candidate(s) and 0 consumed today")
    if not backlog_within_headroom:
        reasons.append(f"backlog {total_unlinked} > budget {budget} even at full consumption")
    healthy = worklist_fresh and consumption_recorded and backlog_within_headroom
    return {"applicable": True, "healthy": healthy, "legs": legs,
            "reason": "; ".join(reasons)}


def update_lane_health(
    previous: dict[str, Any] | None, check: dict[str, Any], today: datetime.date,
) -> dict[str, Any]:
    """Advance the persisted lane-health row from one ``lane_health()`` check.

    The unhealthy streak counts DISTINCT ISO WEEKS, never raw checks — the
    fold that calls this runs far more often than once a week, and a
    per-invocation counter would reach ``LANE_HEALTH_ESCALATE_AFTER`` in
    hours instead of weeks (the same class of bug `remediation_state.py`
    documents at length for its own counters). A healthy check resets the
    streak on the spot, whatever week it is."""
    previous = previous if isinstance(previous, dict) else {}
    first_seen = previous.get("first_seen") or today.isoformat()
    base = {"first_seen": first_seen, "checked": today.isoformat(), "legs": check["legs"]}
    if not check["applicable"]:
        return {**base, "consecutive_unhealthy": 0, "status": NOT_APPLICABLE}
    if check["healthy"]:
        return {**base, "consecutive_unhealthy": 0, "status": HEALTHY}
    week = _iso_week(today)
    consecutive = int(previous.get("consecutive_unhealthy", 0) or 0)
    if previous.get("last_unhealthy_week") != week:
        consecutive += 1
    return {**base, "consecutive_unhealthy": consecutive, "status": UNHEALTHY,
            "last_unhealthy_week": week, "reason": check["reason"]}


def lane_health_regression(state: dict[str, Any]) -> dict[str, Any] | None:
    """The ``invariant_regressions()``-shaped finding once the lane has been
    unhealthy for ``LANE_HEALTH_ESCALATE_AFTER`` distinct weeks — ``None``
    otherwise. Read back out by the SAME ``invariants.state_regressions``
    path every other corpus-invariant regression rides (GRILL ruling,
    2026-08-20), so s02's routing (convergence semantics, the three liveness
    states) applies unchanged."""
    consecutive = int(state.get("consecutive_unhealthy", 0) or 0)
    if consecutive < LANE_HEALTH_ESCALATE_AFTER:
        return None
    return {
        "metric": "unlinked_sources", "value": consecutive,
        "floor": LANE_HEALTH_ESCALATE_AFTER - 1, "tolerance": 0,
        "summary": (f"BAK-04 linking lane unhealthy for {consecutive} consecutive "
                    f"week(s): {state.get('reason') or 'see lane_health legs'}"),
    }
