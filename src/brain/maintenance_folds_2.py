"""Nightly health-history fold."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

def collect_health_metrics(
    core: Any, *, outcomes: dict[str, Any], results: dict[str, Any],
    run_id: str, ts: str | None = None,
) -> dict[str, Any]:
    """Build ONE health-history record (schema in the s02 context bundle) from
    already-computed run state. ``results`` is the SAME run-context dict
    ``BrainCore.maintain`` accumulates branch outputs into — this is the
    "structured partial-result hook" a later golden-eval branch (s07) folds
    into: it need only set ``results["golden"] = {"score": ...}`` before this
    is called, no JSONL rewrite required. Never raises on a missing piece —
    every field degrades to ``None`` rather than aborting the run's own
    health-history append."""
    return _collect_health_metrics_impl(
        core, outcomes=outcomes, results=results, run_id=run_id, ts=ts,
    )


def _len_or_none(d: dict[str, Any], key: str) -> int | None:
    """``len(d[key])`` when the fold ran this run, else None — the record
    omits (None) a metric from a run where the producing fold errored out."""
    return len(d[key]) if key in d else None


def _remediation_fields(results: dict[str, Any]) -> dict[str, Any]:
    """REG-04: what the repair branches did this run.

    WAT-01 binds a fix to its metric, and these branches SHRINK two invariants
    that already trend here — so without a number of their own, "the repairs
    are working" and "the fold was deleted" produce the identical history.
    ``healed``/``remaining`` are summed across branches; ``shadow`` counts the
    branches still in their report-only window, so a lane stuck in shadow is
    visible rather than reading as a lane with nothing to do."""
    rem = results.get("remediation")
    branches = rem.get("branches") if isinstance(rem, dict) else None
    if not isinstance(branches, dict) or not branches:
        return {"remediation_healed": None, "remediation_remaining": None,
                "remediation_shadow": None, "remediation_cost_usd": None}
    rows = [r for r in branches.values() if isinstance(r, dict)]
    # SPD-01: total spend across every branch this run. A mechanical branch
    # never sets `cost_usd` (default 0.0), so a run with no model-backed
    # spend sums to exactly 0 — and per design-freeze (d) rule 1, a bare 0 is
    # recorded as None (indistinguishable from "never measured"), never a
    # healthy trend point of zero.
    cost_total = sum(float(r.get("cost_usd", 0) or 0) for r in rows)
    return {
        "remediation_healed": sum(int(r.get("healed", 0) or 0) for r in rows),
        "remediation_remaining": sum(int(r.get("remaining", 0) or 0) for r in rows),
        "remediation_shadow": sum(1 for r in rows if r.get("mode") == "shadow"),
        "remediation_cost_usd": cost_total if cost_total > 0 else None,
    }


def _link_lane_consumed(results: dict[str, Any]) -> int | None:
    """BAK-04 (F3, 2026-08-18): lane candidates consumed today, cumulative
    within the day (see ``invariants.lane_consumption``). A week of zeros
    while candidates exist is the stalled-consumer signal the 503-source
    backlog grew behind."""
    lane = results.get("link_lane")
    return lane.get("consumed_today") if isinstance(lane, dict) else None


def _collect_health_metrics_impl(
    core: Any, *, outcomes: dict[str, Any], results: dict[str, Any],
    run_id: str, ts: str | None = None,
) -> dict[str, Any]:
    import datetime as _dt
    import time as _time

    status: dict[str, Any] = {}
    try:
        status = core.status()
    except Exception:  # noqa: BLE001 — a broken status() must not break history
        status = {}
    idx = status.get("index") if isinstance(status.get("index"), dict) else {}
    snap = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}

    selftest_ms: float | None = None
    try:
        t0 = _time.perf_counter()
        core.hybrid_search("brain", k=1)
        selftest_ms = round((_time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001 — probe failure is just a null latency point
        selftest_ms = None

    vault = Path(core.vault)
    counts = outcomes.get("counts", {}) if isinstance(outcomes.get("counts"), dict) else {}
    decision_candidates = None
    dc = results.get("decision_capture")
    if isinstance(dc, dict):
        decision_candidates = dc.get("candidates")
    golden = results.get("golden") if isinstance(results.get("golden"), dict) else {}
    graph_hygiene = results.get("graph_hygiene") if isinstance(results.get("graph_hygiene"), dict) else {}
    autodedup = results.get("autodedup") if isinstance(results.get("autodedup"), dict) else {}
    kl_orphans = results.get("kl_orphans") if isinstance(results.get("kl_orphans"), dict) else {}
    # WAT-01: the four corpus invariants + the fold's OWN liveness. The
    # history record is a fixed dict literal, so a metric not named here
    # never persists — every new invariant gets a key in this block.
    from . import invariants as _inv

    inv_metrics = results.get("corpus_invariants") if isinstance(
        results.get("corpus_invariants"), dict) else {}
    inv_values = _inv.metric_values(inv_metrics)
    if inv_metrics:
        inv_age: int | None = 0  # the fold ran THIS run
    else:
        try:
            inv_age = _inv.invariants_age_days(core._load_maintain_state())
        except Exception:  # noqa: BLE001 — an unreadable state file is a null point
            inv_age = None

    return {
        "ts": ts or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "notes": idx.get("notes") if isinstance(idx, dict) else None,
        "chunks": idx.get("chunks") if isinstance(idx, dict) else None,
        "snapshot_gen": snap.get("generation") if isinstance(snap, dict) else None,
        "snapshot_age_s": snap.get("age_seconds") if isinstance(snap, dict) else None,
        "quarantine": _count_files(vault / "inbox" / "_quarantine"),
        "duplicate": _count_files(vault / "inbox" / "_duplicate"),
        "link_lane_consumed": _link_lane_consumed(results),
        "selftest_ms": selftest_ms,
        "action_required": counts.get("action_required", 0),
        "blocked": counts.get("blocked", 0),
        "decision_candidates": decision_candidates,
        "golden_score": golden.get("score"),
        "synthesis_cost_usd": latest_synthesis_cost(vault),
        # GRH-01: only present when the graph_hygiene branch ran this run —
        # a record from a run where it was not due simply omits these (None).
        "graph_orphans": graph_hygiene.get("orphan_count"),
        "graph_islands": graph_hygiene.get("island_count"),
        "graph_dangling": graph_hygiene.get("dangling_target_count"),
        # LNK-03b: unlike graph_orphans (Wednesday-only), this is present on
        # EVERY run once this fold ships — the sustained-growth baseline
        # reads 7 of these, not 7 of the sparse weekly graph_orphans values.
        "kl_orphans": kl_orphans.get("orphan_count"),
        # DDP-01: only present when the autodedup fold ran this run — a
        # record from a run where it errored out entirely simply omits these
        # (None), same posture as the graph_hygiene fields above.
        "autodedup_retired": len(autodedup["retired"]) if "retired" in autodedup else None,
        # ENF-01: the body-floor refusals. Present from the same run as the
        # other autodedup keys so a floor that suddenly stops firing (or
        # starts) is visible in the trend, not only in one run's report.
        "autodedup_skipped_short_body": _len_or_none(autodedup, "skipped_short_body"),
        "autodedup_skipped_classification": _len_or_none(autodedup, "skipped_classification"),
        "autodedup_skipped_recurring": _len_or_none(autodedup, "skipped_recurring"),
        # REG-04: the repair branches' own numbers, so a working lane is
        # distinguishable from a deleted one in the trend.
        **_remediation_fields(results),
        # WAT-01 invariant fields are kept in one extracted mapping.
        **invariant_health_history_fields(inv_values, inv_metrics, inv_age),
    }


def health_trend(
    history: list[dict[str, Any]], today: datetime.date, *,
    sparse_history: list[dict[str, Any]] | None = None,
    latency_regression_pct: float | None = None,
    quarantine_regression_pct: float | None = None,
    golden_regression_pct: float | None = None,
    remediation_cost_regression_multiple: float | None = None,
    remediation_cost_alert_usd: float | None = None,
) -> list[dict[str, Any]]:
    """Week-over-week regression findings. Each finding is
    ``{metric, severity, current, baseline, delta_pct, summary}``.

    - ``blocked`` fires immediately from the LATEST record alone (no
      baseline needed — any blocked>0 is already actionable).
    - ``selftest_ms``/``quarantine`` (high-frequency, appended every hourly
      run) are daily-bucketed and compared against a trailing-median
      baseline, but ONLY once >= ``HEALTH_TREND_MIN_DAYS`` calendar days of
      history exist AND the baseline has >= ``HEALTH_TREND_MIN_BASELINE_DAYS``
      non-null days — otherwise these two checks silently skip (never a
      false regression from a too-thin history; correction 1).
    - ``golden_score`` (sparse — null on nearly every hourly record) compares
      the latest non-null value against the PREVIOUS non-null value
      regardless of window/day-count (correction 1). A null on either side
      skips the check — a null is "absent", never a "-100%" drop.
    - ``remediation_cost`` (sparse, SPD-01) fires on a >= 5x week-over-week
      jump, on the FIRST night any model-backed branch ever records nonzero
      spend, or on any night crossing the absolute
      ``BRAIN_REMEDIATION_COST_ALERT_USD`` floor — never on a cap, because
      there is none (owner ruling: unlimited spend, trend-alerted).
    """
    return _health_trend_impl(
        history, today, sparse_history=sparse_history,
        latency_regression_pct=latency_regression_pct,
        quarantine_regression_pct=quarantine_regression_pct,
        golden_regression_pct=golden_regression_pct,
        remediation_cost_regression_multiple=remediation_cost_regression_multiple,
        remediation_cost_alert_usd=remediation_cost_alert_usd,
    )


def _append_health_high_freq_finding(
    findings: list[dict[str, Any]], ordered: list[dict[str, Any]],
    *, span_ok: bool, metric: str, pct: float, label: str,
) -> None:
    buckets = _bucket_daily(ordered, metric)
    if not buckets:
        return
    days_sorted = sorted(buckets)
    current_day = days_sorted[-1]
    current = buckets[current_day]
    if _DAILY_REDUCERS.get(metric) == "median":
        samples = sum(1 for record in ordered
                      if _record_date(record) == current_day
                      and isinstance(record.get(metric), (int, float)))
        if samples < HEALTH_TREND_MIN_CURRENT_SAMPLES:
            return
    baseline_vals = [buckets[day] for day in days_sorted[:-1] if buckets[day] is not None]
    if current is None or not span_ok or len(baseline_vals) < HEALTH_TREND_MIN_BASELINE_DAYS:
        return
    base_sorted = sorted(baseline_vals)
    mid = len(base_sorted) // 2
    baseline = (base_sorted[mid] if len(base_sorted) % 2
                else (base_sorted[mid - 1] + base_sorted[mid]) / 2)
    if not baseline:
        return
    delta = (current - baseline) / baseline
    if delta > pct:
        findings.append({
            "metric": metric, "severity": "regression",
            "current": current, "baseline": baseline,
            "delta_pct": round(delta * 100, 1),
            "summary": f"{label}: {current} vs trailing baseline {baseline} "
                       f"(+{round(delta * 100, 1)}%, threshold +{round(pct * 100)}%)",
        })


def _append_health_golden_finding(
    findings: list[dict[str, Any]], ordered: list[dict[str, Any]],
    sparse_history: list[dict[str, Any]] | None, pct: float,
) -> None:
    golden_records = [record for record in _union_by_run_id(ordered, sparse_history or [])
                      if record.get("golden_score") is not None]
    golden_points = [record["golden_score"]
                     for record in sorted(golden_records, key=lambda r: str(r.get("ts") or ""))]
    if len(golden_points) < 2:
        return
    previous, current = golden_points[-2], golden_points[-1]
    if not (isinstance(previous, (int, float)) and isinstance(current, (int, float))
            and previous):
        return
    delta = (current - previous) / previous
    if delta < -pct:
        findings.append({
            "metric": "golden_score", "severity": "regression",
            "current": current, "baseline": previous,
            "delta_pct": round(delta * 100, 1),
            "summary": f"golden retrieval score regressed: {current} vs "
                       f"previous {previous} ({round(delta * 100, 1)}%, "
                       f"threshold -{round(pct * 100)}%)",
        })


def _append_health_remediation_cost_finding(
    findings: list[dict[str, Any]], ordered: list[dict[str, Any]],
    sparse_history: list[dict[str, Any]] | None, *,
    regression_multiple: float, alert_floor: float,
) -> None:
    """SPD-01 — no cap on remediation spend, but a jump is an exception.

    Sparse, same shape as ``golden_score``: compares the latest non-null
    ``remediation_cost_usd`` against the previous non-null point. TWO grill
    rulings fire OUTSIDE the plain week-over-week comparison, because a trend
    check has nothing to compare against on the very first data point:

    1. **First occurrence** — the first night ANY model-backed branch ever
       records nonzero spend, with no previous point to compare against.
    2. **Absolute floor** — ``current >= alert_floor``
       (``$BRAIN_REMEDIATION_COST_ALERT_USD``, default 5), regardless of the
       trend, so a single expensive night alerts even without a prior data
       point to jump from.

    Either reason alone is enough for the ONE finding this appends; both can
    be true at once and still produce a single finding, never two."""
    records = [record for record in _union_by_run_id(ordered, sparse_history or [])
               if record.get("remediation_cost_usd") is not None]
    points = [record["remediation_cost_usd"]
              for record in sorted(records, key=lambda r: str(r.get("ts") or ""))]
    if not points or not isinstance(points[-1], (int, float)) or points[-1] <= 0:
        return
    current = points[-1]
    previous = points[-2] if len(points) >= 2 and isinstance(points[-2], (int, float)) else None
    reasons = []
    if previous is None:
        reasons.append("first recorded remediation spend")
    elif previous > 0 and current / previous >= regression_multiple:
        reasons.append(
            f"{round(current / previous, 1)}x jump over the previous "
            f"${previous}")
    if current >= alert_floor:
        reasons.append(f">= the ${alert_floor} alert floor")
    if not reasons:
        return
    findings.append({
        "metric": "remediation_cost", "severity": "regression",
        "current": current, "baseline": previous, "delta_pct": (
            None if not previous else round((current - previous) / previous * 100, 1)),
        "summary": f"remediation branch spend ${current} this run "
                   f"({'; '.join(reasons)}) — no cap is enforced, this is a "
                   "trend alert only",
    })


def _health_trend_impl(
    history: list[dict[str, Any]], today: datetime.date, *,
    sparse_history: list[dict[str, Any]] | None = None,
    latency_regression_pct: float | None = None,
    quarantine_regression_pct: float | None = None,
    golden_regression_pct: float | None = None,
    remediation_cost_regression_multiple: float | None = None,
    remediation_cost_alert_usd: float | None = None,
) -> list[dict[str, Any]]:
    import os

    lat_pct = latency_regression_pct if latency_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_LATENCY_REGRESSION_PCT", DEFAULT_LATENCY_REGRESSION_PCT))
    quar_pct = quarantine_regression_pct if quarantine_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_QUARANTINE_REGRESSION_PCT", DEFAULT_QUARANTINE_REGRESSION_PCT))
    gold_pct = golden_regression_pct if golden_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_GOLDEN_REGRESSION_PCT", DEFAULT_GOLDEN_REGRESSION_PCT))
    cost_multiple = (
        remediation_cost_regression_multiple
        if remediation_cost_regression_multiple is not None else float(
            os.environ.get("BRAIN_HEALTH_REMEDIATION_COST_REGRESSION_MULTIPLE",
                           DEFAULT_REMEDIATION_COST_REGRESSION_MULTIPLE)))
    cost_floor = remediation_cost_alert_usd if remediation_cost_alert_usd is not None else float(
        os.environ.get(BRAIN_REMEDIATION_COST_ALERT_USD_ENV,
                       DEFAULT_REMEDIATION_COST_ALERT_USD))

    findings: list[dict[str, Any]] = []
    if not history:
        return findings
    ordered = sorted(history, key=lambda record: str(record.get("ts") or ""))
    latest = ordered[-1]
    blocked_now = latest.get("blocked")
    if isinstance(blocked_now, (int, float)) and blocked_now > 0:
        findings.append({
            "metric": "blocked", "severity": "regression",
            "current": blocked_now, "baseline": 0, "delta_pct": None,
            "summary": f"{int(blocked_now)} blocked finding(s) in the latest maintain run",
        })
    dates_present = sorted({
        record_date for record in ordered
        if (record_date := _record_date(record)) is not None
    })
    span_ok = bool(dates_present) and (today - dates_present[0]).days >= HEALTH_TREND_MIN_DAYS
    _append_health_high_freq_finding(
        findings, ordered, span_ok=span_ok,
        metric="selftest_ms", pct=lat_pct,
        label="search self-test latency regressed",
    )
    _append_health_high_freq_finding(
        findings, ordered, span_ok=span_ok,
        metric="quarantine", pct=quar_pct, label="quarantine growth",
    )
    _append_health_golden_finding(findings, ordered, sparse_history, gold_pct)
    _append_health_remediation_cost_finding(
        findings, ordered, sparse_history,
        regression_multiple=cost_multiple, alert_floor=cost_floor)
    return findings


from . import maintenance as _maintenance  # noqa: E402

_DAILY_REDUCERS = _maintenance._DAILY_REDUCERS
_bucket_daily = _maintenance._bucket_daily
_count_files = _maintenance._count_files
_record_date = _maintenance._record_date
_union_by_run_id = _maintenance._union_by_run_id
DEFAULT_GOLDEN_REGRESSION_PCT = _maintenance.DEFAULT_GOLDEN_REGRESSION_PCT
DEFAULT_LATENCY_REGRESSION_PCT = _maintenance.DEFAULT_LATENCY_REGRESSION_PCT
DEFAULT_QUARANTINE_REGRESSION_PCT = _maintenance.DEFAULT_QUARANTINE_REGRESSION_PCT
DEFAULT_REMEDIATION_COST_REGRESSION_MULTIPLE = (
    _maintenance.DEFAULT_REMEDIATION_COST_REGRESSION_MULTIPLE)
DEFAULT_REMEDIATION_COST_ALERT_USD = _maintenance.DEFAULT_REMEDIATION_COST_ALERT_USD
BRAIN_REMEDIATION_COST_ALERT_USD_ENV = _maintenance.BRAIN_REMEDIATION_COST_ALERT_USD_ENV
HEALTH_TREND_MIN_BASELINE_DAYS = _maintenance.HEALTH_TREND_MIN_BASELINE_DAYS
HEALTH_TREND_MIN_CURRENT_SAMPLES = _maintenance.HEALTH_TREND_MIN_CURRENT_SAMPLES
HEALTH_TREND_MIN_DAYS = _maintenance.HEALTH_TREND_MIN_DAYS
invariant_health_history_fields = _maintenance.invariant_health_history_fields
latest_synthesis_cost = _maintenance.latest_synthesis_cost
