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
        # WAT-01 invariant fields are kept in one extracted mapping.
        **invariant_health_history_fields(inv_values, inv_metrics, inv_age),
    }


def health_trend(
    history: list[dict[str, Any]], today: datetime.date, *,
    sparse_history: list[dict[str, Any]] | None = None,
    latency_regression_pct: float | None = None,
    quarantine_regression_pct: float | None = None,
    golden_regression_pct: float | None = None,
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
    """
    return _health_trend_impl(
        history, today, sparse_history=sparse_history,
        latency_regression_pct=latency_regression_pct,
        quarantine_regression_pct=quarantine_regression_pct,
        golden_regression_pct=golden_regression_pct,
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


def _health_trend_impl(
    history: list[dict[str, Any]], today: datetime.date, *,
    sparse_history: list[dict[str, Any]] | None = None,
    latency_regression_pct: float | None = None,
    quarantine_regression_pct: float | None = None,
    golden_regression_pct: float | None = None,
) -> list[dict[str, Any]]:
    import os

    lat_pct = latency_regression_pct if latency_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_LATENCY_REGRESSION_PCT", DEFAULT_LATENCY_REGRESSION_PCT))
    quar_pct = quarantine_regression_pct if quarantine_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_QUARANTINE_REGRESSION_PCT", DEFAULT_QUARANTINE_REGRESSION_PCT))
    gold_pct = golden_regression_pct if golden_regression_pct is not None else float(
        os.environ.get("BRAIN_HEALTH_GOLDEN_REGRESSION_PCT", DEFAULT_GOLDEN_REGRESSION_PCT))

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
HEALTH_TREND_MIN_BASELINE_DAYS = _maintenance.HEALTH_TREND_MIN_BASELINE_DAYS
HEALTH_TREND_MIN_CURRENT_SAMPLES = _maintenance.HEALTH_TREND_MIN_CURRENT_SAMPLES
HEALTH_TREND_MIN_DAYS = _maintenance.HEALTH_TREND_MIN_DAYS
invariant_health_history_fields = _maintenance.invariant_health_history_fields
latest_synthesis_cost = _maintenance.latest_synthesis_cost
