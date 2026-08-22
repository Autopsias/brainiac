"""Synthesis heartbeat and the off-host daily watchdog (WATCHDOG-01)."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
import os


# ---------------------------------------------------------------------------
# WATCHDOG-01 (2026-07-11): the two sanctioned scheduled tasks watch EACH
# OTHER, so a dead task is caught by the live one instead of by a human
# reading logs. Direction 1 (here): the hourly maintain umbrella checks the
# synthesis heartbeat (written by scripts/brain-synthesis.sh after every
# vault pass). Direction 2: the weekly synthesis session's prompt starts
# with `brain status`/`brain doctor`, which surface the maintain heartbeat.
# ---------------------------------------------------------------------------
SYNTHESIS_STATE_ENV = "BRAIN_SYNTHESIS_STATE"
SYNTHESIS_STALE_DAYS = 8  # weekly task + one day of grace


def _load_synthesis_entry(
    vault: Path, state_path: Path | None = None,
) -> tuple[Path, dict[str, Any] | None]:
    """Shared ``synthesis-state.json`` resolution + read + per-vault lookup
    (fix for review finding [8] — this state-file/read/lookup sequence was
    duplicated between ``synthesis_heartbeat_finding`` and
    ``latest_synthesis_cost``). Returns ``(resolved_path, entry)`` — entry is
    ``None`` on any absence/parse failure/missing-vault-entry (never
    raises); the path is still returned so a caller (the watchdog finding)
    can report where it looked."""

    path = state_path or Path(
        os.environ.get(SYNTHESIS_STATE_ENV, "")
        or Path.home() / ".brain" / "synthesis-state.json")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, None
    entry = state.get(str(vault))
    return path, (entry if isinstance(entry, dict) else None)


def synthesis_heartbeat_finding(
    vault: Path, today: datetime.date,
    state_path: Path | None = None,
    stale_days: int = SYNTHESIS_STALE_DAYS,
) -> dict[str, Any] | None:
    """An ``action_required`` finding when this vault's last SUCCESSFUL
    synthesis run is older than ``stale_days`` (or attempts keep failing) —
    ``None`` when healthy, unknown, or synthesis simply isn't set up here.

    Fail-quiet on absence by design: no state file means the synthesis task
    has never run on this host (not installed, or first week) — flagging
    that would nag every non-synthesis install forever. Once a heartbeat
    EXISTS for this vault, silence longer than the cadence is a real
    failure signal."""
    path, entry = _load_synthesis_entry(vault, state_path)
    if entry is None:
        return None
    last_ok = entry.get("last_success")
    last_try = entry.get("last_attempt")
    ref = last_ok or last_try
    if not ref:
        return None
    try:
        age = (today - datetime.date.fromisoformat(str(ref)[:10])).days
    except ValueError:
        return None
    failing = entry.get("rc", 0) != 0 and last_ok != last_try
    if age <= stale_days and not failing:
        return None
    what = (f"last SUCCESSFUL synthesis for this vault was {ref} "
            f"({age}d ago; cadence is weekly)" if not failing else
            f"synthesis attempts are FAILING (last attempt {last_try}, "
            f"rc={entry.get('rc')}; last success {last_ok or 'never'})")
    return action_required_item(
        f"brain-synthesis watchdog: {what}",
        "the weekly synthesis session keeps the state/MOC layer current; "
        "a silent death re-opens the decision-staleness gap the 2026-07 "
        "benchmark exposed",
        "check ~/.brain/logs/synthesis-*.log and `launchctl list "
        "com.brainiac.synthesis`; re-run scripts/brain-synthesis.sh manually "
        "to confirm the fix",
        str(path))


def latest_synthesis_cost(vault: Path, state_path: Path | None = None) -> float | None:
    """OBS-04 lift: the most recently METERED ``est_cost_usd`` for this vault
    from ``synthesis-state.json`` (written by ``scripts/brain-synthesis.sh``
    from the ``claude -p --output-format json`` structured usage stream —
    NEVER scraped human-readable text, per HARDENED correction 4: a scraped
    format drifts silently to a wrong zero). Absent, unparseable, or a bare
    ``0`` all record as ``None`` — a real cost of exactly zero is
    indistinguishable from "never measured" and the caller must not read a
    zero as a healthy trend point."""
    _, entry = _load_synthesis_entry(vault, state_path)
    if entry is None:
        return None
    cost = entry.get("est_cost_usd")
    return float(cost) if isinstance(cost, (int, float)) and cost > 0 else None


#: FIX-04 — synthesis retries by SCHEDULING, never by maintain re-firing the
#: model itself (HARDENED:adv-2026-08-20: model-backed work must never run
#: under the maintain writer lock). At most one re-fire per night: the FIRST
#: sighting of a failure marks a retry-intent and stays quiet (LOG); the
#: existing synthesis launchd lane is what actually re-fires; the NEXT time
#: maintain reads the state, either the newest attempt has advanced (the
#: retry happened) or it has not. A newer attempt that ALSO failed escalates;
#: the SAME failure sitting unattempted past one day escalates too, rather
#: than waiting forever for a re-fire that may never come.
def synthesis_retry_observation(
    entry: dict[str, Any] | None, today: datetime.date, row: dict[str, Any],
    stale_days: int = SYNTHESIS_STALE_DAYS,
) -> str:
    """``"healthy" | "retry-marked" | "escalate"`` for this run — mutates
    ``row`` (the ``synthesis_retry`` branch's own host-private state row) in
    place with the bookkeeping the next run reads.

    ``row`` deliberately lives in ``remediation_state``'s HOST-PRIVATE store
    (grill ruling 2026-08-20), never on the VM-visible mount: a forged
    ``retry_marked_for`` there could make a genuinely-still-failing night
    read as merely "retry marked" forever."""
    if entry is None:
        row.pop("retry_marked_for", None)
        row.pop("retry_marked_on", None)
        return "healthy"
    last_ok = entry.get("last_success")
    last_attempt = entry.get("last_attempt")
    ref = last_attempt or last_ok
    if not ref:
        return "healthy"
    failing = entry.get("rc", 0) != 0 and last_ok != last_attempt
    try:
        age = (today - datetime.date.fromisoformat(str(ref)[:10])).days
    except ValueError:
        return "healthy"
    if not failing and age <= stale_days:
        row.pop("retry_marked_for", None)
        row.pop("retry_marked_on", None)
        return "healthy"
    marked_for = row.get("retry_marked_for")
    if marked_for is None:
        row["retry_marked_for"] = str(ref)
        row["retry_marked_on"] = today.isoformat()
        return "retry-marked"
    if marked_for != str(ref):
        # A NEWER attempt has landed since the retry was marked, and it is
        # STILL the failure/staleness condition — the re-fire happened and
        # failed again (or never happened and time simply passed, which the
        # day-bound below also catches).
        row["retry_marked_for"] = str(ref)
        row["retry_marked_on"] = today.isoformat()
        return "escalate"
    try:
        marked_on = datetime.date.fromisoformat(
            str(row.get("retry_marked_on") or ref)[:10])
    except ValueError:
        marked_on = today
    if (today - marked_on).days >= 1:
        # No newer attempt showed up by the next calendar day — whatever "the
        # existing launchd lane re-fires it" assumes, it did not happen in
        # time, and silence must not be able to mean "still just waiting"
        # forever.
        return "escalate"
    return "retry-marked"


# ---------------------------------------------------------------------------
# WD-01 (2026-07-12) — off-host watchdog of last resort. If launchd itself
# dies (or its plists get wiped), the brain-nightly umbrella — and the
# synthesis-heartbeat check running INSIDE it (WATCHDOG-01, commit d28c0ce)
# — die with it. This EXTENDS that shipped pattern rather than rebuilding
# it: `offhost_watchdog_findings` reuses `synthesis_heartbeat_finding` and
# `health_trend` UNCHANGED, adding only the one check neither of those
# covers — "is `maintain` itself still firing at all".
#
# Freshness is keyed on the LATEST health-history record's `ts` (a precise
# ISO datetime, written every maintain run), never on `maintain-state.json`'s
# `last_run` (an ISO DATE only — it advances in whole-day steps, so it
# cannot express a >26h threshold: the first value it could ever cross is
# 48h). This is the correction that makes an hourly-cadence watchdog
# meaningful at sub-day granularity.
#
# LOCAL-first (owner decision 2026-07-12): the off-host CLOUD leg (a Claude
# `/schedule` routine reading this remotely, weekly) is DEFERRED. Verified
# (not assumed) via the `schedule` skill's own documentation: a `/schedule`
# cloud routine "cannot access local files, local services, or local
# environment variables" — there is no remote-export transport yet to get
# this vault's local state to it. What ships now is the LOCAL check +
# macOS-push failsafe (the SAME `fire_notification` osascript channel
# OBS-02 already uses — deliberately not a new outbound/remote channel).
# See docs/operations/wd01-offhost-watchdog-spec.md.
# ---------------------------------------------------------------------------
OFFHOST_DAILY_STALE_HOURS_ENV = "BRAIN_OFFHOST_DAILY_STALE_HOURS"
DEFAULT_OFFHOST_DAILY_STALE_HOURS = 26


def _union_by_run_id(*record_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union health records across the main history + sparse sidecar, deduped
    by ``run_id`` (falling back to ``ts`` then object identity). Later lists
    win on a key collision — callers pass ``(history, sparse)`` so a sidecar
    record supersedes the same-id main-history one. Fix [8]: the ONE
    union-dedup both the off-host watchdog's freshness check and
    ``health_trend``'s golden lookback share, instead of two copies."""
    merged: dict[str, dict[str, Any]] = {}
    for r in itertools.chain.from_iterable(record_lists):
        rid = str(r.get("run_id") or r.get("ts") or id(r))
        merged[rid] = r
    return list(merged.values())


def offhost_watchdog_findings(
    vault: Path, now: datetime.datetime | None = None,
    *, daily_stale_hours: float | None = None,
) -> list[str]:
    """Human-readable breach strings for THIS vault — an empty list means
    healthy (silent); any entry means fire the failsafe notification.

    The failsafe's PURPOSE is "maintain/health is DEAD" — so it is scoped to
    STALENESS ONLY (fix [4]); the transient blocked/regression fold was
    DROPPED because that is OBS-02's on-host job and folding it here fired an
    un-deduped, hourly-re-firing notification that duplicated the on-host
    alarm. Two independent staleness checks (one absent signal never hides
    another):

    1. **Heartbeat freshness** — the latest health-history record (main +
       sparse union) is older than ``daily_stale_hours`` (default 26h — an
       hourly cadence + grace). NO history at all (fresh install, or
       genuinely never run) is SILENT, not a breach — mirrors
       `synthesis_heartbeat_finding`'s fail-quiet-on-absence: nothing to
       watch yet is not "went quiet". If NO record has a parseable ``ts`` at
       all, freshness cannot be determined — that IS a breach ("cannot
       determine health freshness"; fix [6]), never silent-healthy. When some
       records parse, the newest VALID record drives freshness: a lone
       corrupt/lexically-large ts cannot be mistaken for "latest" and mask a
       fresh valid one (re-review [825]), and a PERSISTENT corruption ages the
       youngest valid record past the window so it still breaches on staleness.
    2. **Synthesis heartbeat** — delegates to `synthesis_heartbeat_finding`
       (WATCHDOG-01) UNCHANGED — itself a staleness (last-success-too-old)
       check.
    """
    import os as _os

    now = now or datetime.datetime.now(datetime.timezone.utc)
    stale_hours = daily_stale_hours if daily_stale_hours is not None else float(
        _os.environ.get(OFFHOST_DAILY_STALE_HOURS_ENV, DEFAULT_OFFHOST_DAILY_STALE_HOURS))
    findings: list[str] = []

    merged = _union_by_run_id(read_health_history(vault), read_sparse_history(vault))
    if merged:
        # Pick the newest record by PARSED ts, not a lexical max over the raw
        # strings (re-review): a corrupt but lexically-large ts on a NON-newest
        # record ("2026-07-12T25:00:00Z") would otherwise be selected as
        # "latest" and force a false freshness breach while a genuinely fresh
        # valid record exists. Parse every ts; the newest VALID record drives
        # freshness. Only if NO record has a parseable ts is it a breach.
        parsed = []
        for r in merged:
            try:
                parsed.append((datetime.datetime.strptime(
                    str(r.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=datetime.timezone.utc), r))
            except ValueError:
                continue
        if not parsed:
            findings.append(
                f"{vault}: no health-history record has a parseable ts — cannot "
                f"determine health freshness (a breach, not silent-healthy)")
        else:
            latest_dt, latest = max(parsed, key=lambda t: t[0])
            age_hours = (now - latest_dt).total_seconds() / 3600.0
            if age_hours > stale_hours:
                findings.append(
                    f"{vault}: no health-history record in {age_hours:.1f}h "
                    f"(> {stale_hours:.0f}h) — the hourly maintain umbrella "
                    f"(and whatever schedules it) may have died")

    synth = synthesis_heartbeat_finding(vault, now.date())
    if synth is not None:
        findings.append(f"{vault}: {synth['finding']}")

    return findings

# Cross-section binds, deferred past this module's own defs.
from .maintenance_healthlog import read_health_history as read_health_history  # noqa: E402
from .maintenance_healthlog import read_sparse_history as read_sparse_history  # noqa: E402
from .maintenance_outcomes import action_required_item as action_required_item  # noqa: E402
