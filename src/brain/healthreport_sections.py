"""Health-report collection sections."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from . import brief as brief_mod
from . import doctor as brain_doctor
from . import invariants as inv
from . import maintenance as maint
from .healthreport_unsigned import _unsigned_notes_context


def collect_health_report_data(core: Any, *, today: datetime.date | None = None) -> dict[str, Any]:
    """Gather every render input from data the engine already collects
    elsewhere. Read-only, best-effort per section — one section failing
    (e.g. doctor raising on an odd install) degrades that section, never
    aborts the whole report."""
    return _collect_health_report_data_impl(core, today=today)


def _health_report_doctor() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        doctor_report = brain_doctor.run_doctor()
    except Exception as exc:  # noqa: BLE001 — doctor failing must not sink the report
        doctor_report = {"ok": False, "rows": [], "stale_count": None,
                         "error": f"{type(exc).__name__}: {exc}"}
    doctor_rows = doctor_report.get("rows") or []
    doctor_gating = [row for row in doctor_rows if row.get("status") in ("stale", "unknown")]
    return doctor_report, doctor_gating


def _health_report_branch_actions(escalation: dict[str, Any]) -> list[str]:
    return [
        f"maintain branch '{branch['branch']}': {'; '.join(branch['reasons'])} — "
        f"run `brain doctor` / check ~/.brain/logs/"
        for branch in escalation.get("branches", [])
    ]


def _health_report_doctor_actions(doctor_gating: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    seen_rows: set[tuple[Any, ...]] = set()
    for row in doctor_gating:
        key = (row.get("surface"), row.get("status"), row.get("detail"))
        if key in seen_rows:
            continue
        seen_rows.add(key)
        actions.append(
            f"doctor: {row.get('surface')} is {row.get('status')} — "
            f"{row.get('detail')} — run `brain doctor`"
        )
    return actions


def _health_report_invariant_actions(
    state: dict[str, Any], today: datetime.date,
) -> list[str]:
    actions: list[str] = []
    inv_live = inv.liveness_finding(state, today)
    if inv_live:
        actions.append(f"{inv_live[1]} — run `brain maintain`")
    actions.extend(
        f"{regression.get('summary')} — see the Corpus invariants section"
        for regression in inv.state_regressions(state)
    )
    return actions


def _health_report_trend_actions(trend_rows: list[dict[str, Any]]) -> list[str]:
    if len(trend_rows) < 2:
        return []
    current, previous = trend_rows[0].get("quarantine"), trend_rows[1].get("quarantine")
    if (isinstance(current, (int, float)) and isinstance(previous, (int, float))
            and current > previous):
        return [
            f"quarantine growing ({previous} -> {current}) — "
            f"inspect `vault/inbox/_quarantine/`"
        ]
    return []


def _health_report_snapshot_actions(status: dict[str, Any]) -> list[str]:
    snapshot = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}
    age_seconds = snapshot.get("age_seconds") if isinstance(snapshot, dict) else None
    stale_hours = maint.DEFAULT_OFFHOST_DAILY_STALE_HOURS
    if isinstance(age_seconds, (int, float)) and (age_seconds / 3600) > stale_hours:
        return [
            f"snapshot age {age_seconds / 3600:.1f}h (> {stale_hours}h) — "
            f"run `brain sync --publish`"
        ]
    return []


def _health_report_skip_actions(
    state: dict[str, Any], escalation: dict[str, Any],
) -> list[str]:
    escalated_branches = {branch["branch"] for branch in escalation.get("branches", [])}
    actions: list[str] = []
    for branch, entry in state.items():
        if (str(branch).startswith("_") or not isinstance(entry, dict)
                or branch in escalated_branches):
            continue
        skips = int(entry.get("consecutive_skips", 0) or 0)
        if skips >= maint.SKIP_ESCALATE_THRESHOLD:
            actions.append(
                f"branch '{branch}': {skips} consecutive writer-busy skip(s) — "
                f"run `brain doctor`"
            )
    return actions


def _health_report_actions(
    state: dict[str, Any], escalation: dict[str, Any],
    doctor_gating: list[dict[str, Any]], status: dict[str, Any],
    trend_rows: list[dict[str, Any]], today: datetime.date,
) -> list[str]:
    actions = _health_report_branch_actions(escalation)
    actions.extend(_health_report_doctor_actions(doctor_gating))
    index = status.get("index") if isinstance(status.get("index"), dict) else {}
    if isinstance(index, dict) and index.get("error"):
        actions.append(f"index unreadable: {index['error']} — run `brain status`")
    actions.extend(_health_report_invariant_actions(state, today))
    actions.extend(_health_report_trend_actions(trend_rows))
    actions.extend(_health_report_snapshot_actions(status))
    actions.extend(_health_report_skip_actions(state, escalation))
    return actions


def _collect_health_report_data_impl(
    core: Any, *, today: datetime.date | None = None,
) -> dict[str, Any]:
    from . import __version__ as engine_version

    report_date = today or datetime.date.today()
    vault = Path(core.vault)
    try:
        state = core._load_maintain_state()
    except Exception:
        state = {}
    escalation = maint.maintain_escalation(state, report_date)
    doctor_report, doctor_gating = _health_report_doctor()
    try:
        status = core.status()
    except Exception as exc:  # noqa: BLE001
        status = {"error": f"{type(exc).__name__}: {exc}"}
    trend_rows = _read_trend_rows(vault)
    act_now = _health_report_actions(
        state, escalation, doctor_gating, status, trend_rows, report_date,
    )
    if escalation.get("escalate"):
        verdict = VERDICT_BROKEN
    elif act_now:
        verdict = VERDICT_DEGRADED
    else:
        verdict = VERDICT_HEALTHY
    return {
        "date": report_date.isoformat(),
        "verdict": verdict,
        "act_now": act_now,
        "escalation": escalation,
        "doctor": {"ok": doctor_report.get("ok"), "stale_count": doctor_report.get("stale_count"),
                    "error": doctor_report.get("error")},
        "state": state,
        "status": status,
        "trend": trend_rows,
        "vault": str(vault),
        "engine_version": engine_version,
    }


def _unreachable_gold_extra(m: dict[str, Any]) -> str:
    """Context cell for the unreachable_gold row — measurement date plus the
    F1 buckets (absent_from_index / requires_multi_hop / variant_recoverable
    / ranking_gap), so the headline never reads as one silent aggregate."""
    if not m.get("available"):
        return "no reachability artifact yet"
    b = m.get("buckets") if isinstance(m.get("buckets"), dict) else {}
    bucket_txt = ("; " + ", ".join(
        f"{brief_mod._esc(k)} {brief_mod._esc(v)}" for k, v in b.items()) if b else "")
    return (f"of {brief_mod._esc(m.get('labels', '?'))} gold label(s), "
            f"measured {brief_mod._esc(m.get('generated') or 'never')}{bucket_txt}")


def _corpus_invariants_html(
    state: dict[str, Any], trend: list[dict[str, Any]],
    today: datetime.date | None = None,
) -> str | None:
    """WAT-01: corpus invariants against their RATCHETED floors, plus the
    fold's liveness and a trend table. ``None`` when it has never run."""
    return _corpus_invariants_html_impl(state, trend, today=today)


def _invariant_extra(name: str, metric: dict[str, Any], inv_module: Any) -> str:
    extra = _unsigned_notes_context(metric) if name == "unsigned_notes" else ""
    if name == "unlinked_sources":
        extra = (f"{brief_mod._esc(metric.get('population', '?'))} in population, "
                 f"{brief_mod._esc(metric.get('excluded', 0))} excluded by design")
    elif name == "cross_tier_twins":
        extra = f"of {brief_mod._esc(metric.get('pairs', '?'))} name-twin pair(s)"
    elif name in ("cross_tier_duplicates", "cross_tier_candidates"):
        extra = (f"coverage {brief_mod._esc(metric.get('coverage', '?'))} "
                 f"({brief_mod._esc(metric.get('comparable', '?'))}/"
                 f"{brief_mod._esc(metric.get('population', '?'))} documents "
                 "comparable)")
        if name == "cross_tier_duplicates":
            by_reason = metric.get("excluded_by_reason") or {}
            skipped = sum(
                value for key, value in by_reason.items()
                if key in inv_module.CROSS_TIER_SKIP_REASONS
            )
            extra += (f", {brief_mod._esc(metric.get('candidates', '?'))} undecided, "
                      f"{brief_mod._esc(metric.get('subfloor', '?'))} sub-floor, "
                      f"{brief_mod._esc(metric.get('retained_superseded', 0))} superseded "
                      f"retained, {brief_mod._esc(skipped)} excluded by design")
    elif name == "unguarded_ingests":
        legs = metric.get("raised_by_leg") or {}
        legs_txt = (", ".join(f"{brief_mod._esc(key)} {brief_mod._esc(value)}"
                              for key, value in sorted(legs.items()))
                    or "none yet")
        extra = (f"{brief_mod._esc(metric.get('raised', '?'))} raised to a twin's "
                 f"tier (by leg: {legs_txt}), "
                 f"{brief_mod._esc(metric.get('clear', '?'))} clear, "
                 f"{brief_mod._esc(metric.get('subfloor', '?'))} too short to judge, "
                 f"{brief_mod._esc(metric.get('unstamped', '?'))} predate the guard, "
                 f"of {brief_mod._esc(metric.get('sources', '?'))} raw source(s)")
    elif name == "subfloor_families":
        extra = (f"of {brief_mod._esc(metric.get('families', '?'))} supersession "
                 f"family/families, floor {brief_mod._esc(metric.get('floor', '?'))}B")
    elif name == "unreachable_gold":
        extra = _unreachable_gold_extra(metric)
    if metric.get("error"):
        extra = f"ERROR: {brief_mod._esc(metric['error'])}"
    return extra


def _render_invariant_rows(
    metrics: dict[str, Any], floors: dict[str, Any], regressed: set[Any],
) -> str:
    rows: list[str] = []
    for name in inv.INVARIANT_METRICS:
        metric = metrics.get(name) if isinstance(metrics.get(name), dict) else {}
        value = metric.get("value")
        floor = floors.get(name)
        tolerance = inv.metric_tolerance(name)
        threshold = (f"&le; {brief_mod._esc(floor)}"
                     if isinstance(floor, int) else "(baselining)")
        if isinstance(floor, int) and tolerance:
            threshold += f" (+{tolerance})"
        extra = _invariant_extra(name, metric, inv)
        css_class = ' class="warn"' if name in regressed else ""
        rows.append(
            f"<tr{css_class}><td>{brief_mod._esc(_INVARIANT_LABELS[name])}</td>"
            f"<td>{brief_mod._esc('—' if value is None else value)}</td>"
            f"<td>{threshold}</td><td>{extra}</td></tr>"
        )
    head = ("<tr><th>invariant</th><th>now</th><th>threshold (best recorded)</th>"
            "<th>context</th></tr>")
    return f'<table class="tbl">{head}{"".join(rows)}</table>'


def _render_invariant_liveness(
    state: dict[str, Any], entry: dict[str, Any], regressed: set[Any],
    today: datetime.date | None,
) -> list[str]:
    lines: list[str] = []
    age = inv.invariants_age_days(state, today)
    limit = inv.max_age_days()
    if age is None or age > limit:
        lines.append(
            f'<p class="warn">&#9888; watchdog liveness: last successful run '
            f'{brief_mod._esc("never" if age is None else str(age) + "d ago")} '
            f'(max {limit}d) — the counts above are UNWATCHED</p>')
    else:
        lines.append(f'<p class="meta">watchdog liveness: last successful run '
                     f'{age}d ago (max {limit}d) &middot; '
                     f'{brief_mod._esc(entry.get("last_run", "?"))}</p>')
    if regressed:
        lines.append('<p class="warn">&#9888; regression(s) this run: '
                     + brief_mod._esc(", ".join(sorted(str(item) for item in regressed)))
                     + '</p>')
    return lines


def _render_invariant_trend(trend: list[dict[str, Any]]) -> str | None:
    columns = (("ts", "ts"), ("invariant_unlinked_sources", "unlinked"),
               ("invariant_cross_tier_twins", "twins"),
               ("invariant_cross_tier_duplicates", "x-tier dup"),
               ("invariant_cross_tier_candidates", "x-tier undecided"),
               ("invariant_unguarded_ingests", "unguarded"),
               ("invariant_ingest_guard_raises", "guard raises"),
               ("invariant_subfloor_families", "sub-floor"),
               ("invariant_unreachable_gold", "unreachable gold"),
               ("invariant_age_days", "age (d)"))
    trend_rows = [
        row for row in trend
        if any(row.get(column) is not None for column, _ in columns if column != "ts")
    ]
    if not trend_rows:
        return None
    head = "<tr>" + "".join(f"<th>{label}</th>" for _, label in columns) + "</tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{brief_mod._esc(row.get(column, ''))}</td>"
                          for column, _ in columns)
        + "</tr>"
        for row in trend_rows
    )
    return f'<table class="tbl">{head}{body}</table>'


def _corpus_invariants_html_impl(
    state: dict[str, Any], trend: list[dict[str, Any]],
    today: datetime.date | None = None,
) -> str | None:
    entry = state.get(inv.STATE_KEY)
    if not isinstance(entry, dict):
        return None
    metrics = entry.get("metrics")
    if not isinstance(metrics, dict):
        return None
    floors = entry.get("floors") if isinstance(entry.get("floors"), dict) else {}
    regressed = {regression.get("metric") for regression in inv.state_regressions(state)}
    lines = [_render_invariant_rows(metrics, floors, regressed)]
    lines.extend(_render_invariant_liveness(state, entry, regressed, today))
    trend_html = _render_invariant_trend(trend)
    if trend_html is not None:
        lines.append(trend_html)
    return "".join(lines)


from . import healthreport as _healthreport  # noqa: E402

VERDICT_BROKEN = _healthreport.VERDICT_BROKEN
VERDICT_DEGRADED = _healthreport.VERDICT_DEGRADED
VERDICT_HEALTHY = _healthreport.VERDICT_HEALTHY
_INVARIANT_LABELS = _healthreport._INVARIANT_LABELS
_read_trend_rows = _healthreport._read_trend_rows
