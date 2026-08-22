"""Date-gated maintain branches and the escalation ladder."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any




# Date-gated branch names, in cadence order. "daily" is always due; the rest
# fire on the weekday/day-of-month the persistence budget assigns them so the
# WHOLE roster rides the one sanctioned OS task (`brain-nightly`) with ZERO
# extra scheduler entries (persistence-budget.md THE LOCK).
_MONDAY, _TUESDAY, _WEDNESDAY, _SUNDAY = 0, 1, 2, 6
_WEEKLY_TRIGGER_WEEKDAY = {
    "health": _MONDAY, "integrity": _TUESDAY, "graph_hygiene": _WEDNESDAY,
    "digest": _SUNDAY, "golden": _SUNDAY,
}
_ALL_BRANCHES = ("daily", "health", "integrity", "graph_hygiene", "digest", "graphify", "golden")

# ---------------------------------------------------------------------------
# ES-01 — loud escalation for a maintain branch that stops making progress.
#
# Single source of truth shared by doctor.py (exit code), brief.py (banner)
# and BrainCore.maintain (notification): every consumer calls
# ``branch_escalation``/``maintain_escalation`` instead of re-deriving these
# thresholds, so the three surfaces can never drift apart.
#
# Two INDEPENDENT deliberately-different gates already exist upstream and are
# NOT touched here: `brain doctor`'s own consecutive_failures>=2 STALE check
# (doctor.py) and `status()`'s >=2 repeated-failures flag (core.py) — both are
# PULL surfaces (cheap to be noisy). This module governs only the PUSH
# surfaces (banner + notification), at the louder >=3 threshold, PLUS a
# liveness check neither of those upstream gates can see: consecutive_failures
# is incremented only inside a branch handler's try — a process that never
# runs at all (launchd never fired, a crashed import, or a leaked writer lock
# via the `skipped-writer-busy` contract in `_mark_writer_busy`) leaves the
# counter frozen, forever "healthy". Liveness is keyed on `last_run` (set only
# on genuine success) and the writer-busy skip contract's own
# `consecutive_skips`/`writer_busy_since` — never on `last_attempt`, which the
# writer-busy skip refreshes every time regardless of whether any work
# happened.
# ---------------------------------------------------------------------------
FAILURE_ESCALATE_THRESHOLD = 3   # consecutive_failures >= this -> loud (banner+doctor+notify)
SKIP_ESCALATE_THRESHOLD = 6      # consecutive_skips >= this -> loud (leaked-lock regression guard)
WRITER_BUSY_GRACE_HOURS = 12     # writer_busy_since older than this -> loud even if skip count is low
_STALE_CADENCE_MULTIPLIER = 2    # last_run older than this many cadences -> liveness failure
_CADENCE_DAYS = {
    "daily": 1, "health": 7, "integrity": 7, "graph_hygiene": 7, "digest": 7,
    "golden": 7, "graphify": 30,
    # WAT-01: not in `_ALL_BRANCHES` — it runs inside the daily block rather
    # than as its own date-gated branch — but it DOES write a state row, so
    # naming its cadence here gives it ES-01 liveness escalation for free
    # (last_run older than 2 cadences -> loud). That is the SECOND liveness
    # gate; `invariants.liveness_finding` is the explicit, env-tunable one.
    "corpus_invariants": 1,
}


def _cadence_days(branch: str) -> int:
    """How often ``branch`` is expected to run, in days.

    A branch this module's own map does not name falls back to the remediation
    registry, where a REMEDIATION branch declares its cadence
    (``remediation.BRANCH_CADENCE_DAYS``). The two maps are read as ONE, so a
    weekly lane like ``link_lane`` is not judged against the daily default and
    reported dead after two days. Unknown to both -> daily, as before.

    An UNLOADABLE registry (its import runs ``validate()``, so a broken table
    raises) falls back to daily EXPLICITLY. Letting that propagate would be
    caught by the caller's ``except ValueError`` — ``RegistryError`` is one —
    and a table bug would silently switch liveness escalation OFF instead of
    degrading it to the behaviour that shipped before this fallback existed."""
    if branch in _CADENCE_DAYS:
        return _CADENCE_DAYS[branch]
    try:
        from . import remediation
    except Exception:  # noqa: BLE001 — a broken table must not silence liveness
        return 1

    return remediation.BRANCH_CADENCE_DAYS.get(branch, 1)


def _validate_cadence_maps() -> None:
    """Refuse, at import, two cadence maps that DISAGREE about one branch.

    ``_cadence_days`` gives this module's own map silent precedence over the
    remediation registry's, while both this module's docstring and
    ``remediation``'s claim the two are read as ONE and that the registry owns
    a remediation branch's cadence. Nothing checked it. A later remediation
    branch reusing an existing branch NAME (``health``, say) and declaring
    cadence 1 in the registry would keep being judged at 7 days — a dead
    branch staying quiet for 14 days instead of 2, with no test anywhere
    noticing (s01 review finding, 2026-08-21).

    Disjoint is fine and agreeing is fine; only a disagreement raises, and it
    raises HERE rather than at the unlucky lookup. A registry that cannot be
    imported at all is NOT this function's problem — ``_cadence_days`` already
    degrades that to the daily default explicitly.

    Blast radius, considered and checked (adversarial review round 2): raising
    at import fails every surface that imports this module, ``brain doctor``
    included. That is accepted because the trigger is a source-level mistake
    the suite catches before it ships, and because the ONE surface that must
    never go dark does not reach here — ``brain alerts`` imports
    ``remediation`` for the disposition table and never this module, so a
    session-start digest still runs on a vault whose maps disagree."""
    try:
        from . import remediation
    except Exception:  # noqa: BLE001 — see _cadence_days: a broken table is its own bug
        return
    conflicts = {
        branch: (_CADENCE_DAYS[branch], days)
        for branch, days in remediation.BRANCH_CADENCE_DAYS.items()
        if branch in _CADENCE_DAYS and _CADENCE_DAYS[branch] != days
    }
    if conflicts:
        raise ValueError(
            "cadence maps disagree — maintenance_escalation._CADENCE_DAYS "
            "silently wins over remediation.BRANCH_CADENCE_DAYS, so a branch "
            "named in both is judged against a cadence its registry entry does "
            f"not declare: {conflicts}. Rename the branch, or make the two "
            "agree.")


def branch_escalation(
    branch: str, entry: dict[str, Any], today: datetime.date,
) -> dict[str, Any]:
    """Whether ``branch`` (one ``maintain-state.json`` entry) should escalate
    LOUDLY today, and why. Pure, never raises on malformed data (an
    unparseable date degrades to "can't tell", not a crash).

    Returns ``{"escalate": bool, "reasons": [str, ...]}``.
    """
    reasons: list[str] = []

    consecutive_failures = int(entry.get("consecutive_failures", 0) or 0)
    if consecutive_failures >= FAILURE_ESCALATE_THRESHOLD:
        reasons.append(f"{consecutive_failures} consecutive failures")

    consecutive_skips = int(entry.get("consecutive_skips", 0) or 0)
    writer_busy_since = entry.get("writer_busy_since")
    skip_escalate = consecutive_skips >= SKIP_ESCALATE_THRESHOLD
    if not skip_escalate and writer_busy_since:
        try:
            since = datetime.date.fromisoformat(str(writer_busy_since))
            if (today - since).days * 24 >= WRITER_BUSY_GRACE_HOURS:
                skip_escalate = True
        except ValueError:
            pass
    if skip_escalate:
        reasons.append(f"writer-lock held/skipped {consecutive_skips} consecutive run(s)")

    last_run = entry.get("last_run")
    if last_run:
        try:
            age_days = (today - datetime.date.fromisoformat(str(last_run))).days
            cadence = _cadence_days(branch)
            if age_days > cadence * _STALE_CADENCE_MULTIPLIER:
                reasons.append(f"last_run {age_days}d ago (expected every ~{cadence}d)")
        except ValueError:
            pass

    return {"escalate": bool(reasons), "reasons": reasons}


def maintain_escalation(
    state: dict[str, Any], today: datetime.date | None = None,
) -> dict[str, Any]:
    """Escalation status across every branch in ``maintain-state.json``.
    Pure — the caller loads/passes ``state``, this never touches disk.

    Returns ``{"escalate": bool, "branches": [{"branch", "reasons", ...}]}``.
    """
    d = today or datetime.date.today()
    branches: list[dict[str, Any]] = []
    for branch, entry in state.items():
        if str(branch).startswith("_") or not isinstance(entry, dict):
            continue  # marker, not a branch (mirrors maintain()'s own filter)
        result = branch_escalation(branch, entry, d)
        if result["escalate"]:
            branches.append({
                "branch": branch, "reasons": result["reasons"],
                "consecutive_failures": int(entry.get("consecutive_failures", 0) or 0),
            })
    return {"escalate": bool(branches), "branches": branches}


def _next_trigger(branch: str, last_run: datetime.date) -> datetime.date:
    """First calendar date STRICTLY AFTER ``last_run`` on which ``branch`` is
    next due (ADR-0003 Ruling d)."""
    if branch == "daily":
        return last_run + datetime.timedelta(days=1)
    if branch == "graphify":
        y, m = last_run.year, last_run.month + 1
        if m > 12:
            y, m = y + 1, 1
        return datetime.date(y, m, 1)
    weekday = _WEEKLY_TRIGGER_WEEKDAY[branch]
    days_ahead = (weekday - last_run.weekday()) % 7
    return last_run + datetime.timedelta(days=days_ahead or 7)


def maintain_branches(
    today: datetime.date | None = None,
    last_runs: dict[str, str | None] | None = None,
) -> list[str]:
    """Which date-gated branches are due for ``today`` (default: real today).

    ADR-0003 Ruling 5/(d) — DUE-SINCE-LAST-RUN, not calendar-day-only: a
    branch is due when ``today >= next_trigger(last_run)``. ``last_runs`` maps
    branch -> its last SUCCESSFUL run date (ISO string) as persisted in
    ``.brain/maintain-state.json``; a branch absent from the mapping (or
    ``last_runs=None``, e.g. no state file yet — a brand-new install) has
    never run and is due immediately, regardless of weekday. This is safe
    because every branch is idempotent (a re-run finds nothing new to do).

    A missed weekly/monthly branch (the laptop was off on its trigger day)
    fires once on the next run that reaches or passes its next trigger date —
    not once per day it stayed missed.

    Mon -> health, Tue -> integrity, Sun -> digest, 1st-of-month -> graphify
    (ADR-0003 Ruling 6/(a): `maintain` invokes a REAL, bounded graph build on
    this date-gate — drift-gated, embedding-reuse, wall-clock-budgeted; see
    `BrainCore.graphify`). "daily" (index sync + drain + recommendations-aging
    scan) is due at most once per day.
    """
    d = today or datetime.date.today()
    last_runs = last_runs or {}
    due: list[str] = []
    for branch in _ALL_BRANCHES:
        raw = last_runs.get(branch)
        if not raw:
            due.append(branch)  # never run -> due now
            continue
        last = raw if isinstance(raw, datetime.date) else datetime.date.fromisoformat(raw)
        if last > d:
            # A marker AFTER today is corrupt state (clock skew, or a
            # `--today` date-gate test run against a live vault). Left alone
            # it silently disables the branch until real time catches up, so
            # treat it like never-run: due now, and the success rewrite of
            # last_run to today's date self-heals the state file.
            due.append(branch)
            continue
        if d >= _next_trigger(branch, last):
            due.append(branch)
    return due


# ---------------------------------------------------------------------------
# Recommendations lifecycle (MEM-03, ADR-0003 Ruling 5 "daily" unconditional
# fold). Ported pattern: the reference vault's `_recommendations_open.jsonl` /
# `_recommendations_log.md` (schema/pattern only, per Appendix B — never

# Cross-section binds, deferred past this module's own defs.


_validate_cadence_maps()
