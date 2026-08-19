"""Health-metrics history: append, rotate, sparse metrics, and reads (OBS-01)."""
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
import time
import uuid


# ---------------------------------------------------------------------------
# OBS-01 — health-metrics history (health-history.jsonl). Every ``maintain``
# run appends ONE record (schema below) so trend questions ("worse than last
# week?") have something to answer from instead of amnesia. HARDENED
# corrections applied throughout (see s02 context bundle):
#   1. time-based (7-CALENDAR-DAY) baseline for high-frequency metrics, never
#      "last 7 records" (that's ~7 hours on an hourly cadence); sparse weekly
#      metrics (golden_score, synthesis_cost_usd) compare against the
#      trailing non-null observation regardless of window.
#   2. ONE final immutable append per run, built from a single run-context
#      object (``results``) that a later branch (s07's golden-eval fold) can
#      still fold into via ``results["golden"] = {...}`` before this append —
#      never a JSONL line rewrite.
#   3. concurrency-safe append+rotation under a DEDICATED short-lived lock
#      (the coarse 2h maintain-lock can legitimately let two runs overlap);
#      monotonically-named archive segments; every record carries a unique
#      ``run_id`` the reader dedups on, tolerating one trailing partial line.
#   4. every new maintain-state marker this session touches is ``_``-prefixed
#      (core.py:1973-1977 treats a bare key as a due-branch name); cost is
#      metered from the structured usage stream only.
#   5. a PER-METRIC daily-bucket reducer (never one generic "representative")
#      so a single-hour blocked/latency spike survives bucketing.
# ---------------------------------------------------------------------------
HEALTH_HISTORY_MAX_BYTES_ENV = "BRAIN_HEALTH_HISTORY_MAX_BYTES"
DEFAULT_HEALTH_HISTORY_MAX_BYTES = 1_000_000
HEALTH_HISTORY_LOCK_STALE_SECONDS = 30.0
# Fix [6]: bound the archive re-read + add retention pruning. 14 days
# comfortably covers health_trend's 7-day trailing baseline plus a weekly
# sparse-metric (golden_score/synthesis_cost_usd) lookback; retention is a
# much longer, separate knob (mirrors scripts/brain-synthesis.sh's
# `find -mtime +N -delete` posture for its own out-json captures).
HEALTH_HISTORY_READ_WINDOW_DAYS_ENV = "BRAIN_HEALTH_HISTORY_READ_WINDOW_DAYS"
DEFAULT_HEALTH_HISTORY_READ_WINDOW_DAYS = 14
HEALTH_ARCHIVE_RETENTION_DAYS_ENV = "BRAIN_HEALTH_ARCHIVE_RETENTION_DAYS"
DEFAULT_HEALTH_ARCHIVE_RETENTION_DAYS = 90


def new_health_run_id() -> str:
    """A short, unique-enough id stamped on every health-history record so a
    reader merging the live file + rotated archives can dedup instead of
    double-counting a record two racing writers might otherwise both see."""
    import time
    import uuid

    return f"{int(time.time() * 1000):x}-{uuid.uuid4().hex[:8]}"


def _count_files(dir_path: Path) -> int:
    """Recursive PENDING file count under ``dir_path`` — 0 if it does not
    exist yet (a fresh vault has no ``_quarantine``/``_duplicate`` dir at
    all).

    Anything under a ``_resolved/`` directory is triaged, not pending, and
    is excluded — the 2026-08-15 retirement session parked 212 files under
    ``_quarantine/_resolved/`` and the quarantine trend alert then fired
    nightly for a growth that was 99% already-dispositioned content (835 of
    841 files). Hidden files (``.DS_Store``) are excluded for the same
    reason: they are not content awaiting an owner."""
    if not dir_path.is_dir():
        return 0
    return sum(
        1 for p in dir_path.rglob("*")
        if p.is_file() and not p.name.startswith(".")
        and "_resolved" not in p.relative_to(dir_path).parts)


def _acquire_health_history_lock(
    lock_path: Path, *, stale_after: float = HEALTH_HISTORY_LOCK_STALE_SECONDS,
) -> None:
    """Best-effort exclusive lock scoped ONLY to the tiny append+rotate
    critical section (correction 3) — deliberately separate from
    ``BrainCore._acquire_maintain_lock``: that lock's 2h auto-break lets two
    ``maintain`` runs overlap by design, so append/rotation needs its own
    much-shorter-lived lock or two overlapping runs could both decide to
    rotate onto the same archive name. Blocks briefly (busy-wait), self-heals
    a lock older than ``stale_after`` (a crash mid-critical-section), and
    never blocks indefinitely."""
    import os
    import time

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode("ascii"))
            os.close(fd)
            return
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = stale_after + 1
            if age > stale_after:
                lock_path.unlink(missing_ok=True)
                continue
            time.sleep(0.05)


def _release_health_history_lock(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


def _rotate_health_history(path: Path, archive_dir: Path) -> str:
    """Move the current file to a MONOTONICALLY-NAMED, create-exclusive
    archive segment — never overwrites an existing segment even if two
    rotations somehow land in the same millisecond."""
    import os
    import time

    archive_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1000):
        stamp = f"{int(time.time() * 1000):x}-{attempt:03d}"
        dest = archive_dir / f"health-history-{stamp}.jsonl"
        try:
            fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            continue
        os.replace(path, dest)
        return str(dest)
    raise RuntimeError("could not allocate a unique health-history archive segment")


def _prune_old_files(dir_path: Path, pattern: str, retention_days: int) -> None:
    """Delete files under ``dir_path`` matching ``pattern`` whose mtime is
    older than ``retention_days``. Best-effort: a file that vanishes mid-scan
    or resists deletion is skipped, never raised. Mirrors
    ``scripts/brain-synthesis.sh``'s ``find -mtime +N -delete`` posture. One
    shared implementation for the two near-identical mtime pruners this
    session added — the health-archive and the notify-marker cleanups (review
    finding [8])."""
    import time

    if not dir_path.is_dir():
        return
    cutoff = time.time() - retention_days * 86400.0
    for p in dir_path.glob(pattern):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue


def _prune_health_archive(archive_dir: Path, retention_days: int) -> None:
    """Delete rotated ``health-history-*.jsonl`` segments older than
    ``retention_days`` (fix [6] retention companion)."""
    _prune_old_files(archive_dir, "health-history-*.jsonl", retention_days)


def append_health_record(
    vault: Path, record: dict[str, Any], *, max_bytes: int | None = None,
    archive_retention_days: int | None = None,
) -> dict[str, Any]:
    """Append ONE JSONL record under the dedicated health-history lock,
    rotating to an archive segment first if the live file would cross
    ``max_bytes`` (~1MB default, env-overridable). Never raises past a
    caller — a health-history write failure is reported by the caller as a
    ``blocked`` item, never allowed to fail the whole maintain run.

    Also prunes archive segments past ``archive_retention_days`` (default
    90, env-overridable — fix [6]) every call: cheap (a small glob under the
    lock already held) and keeps the archive dir from growing forever."""
    import os

    from . import config as _config

    limit = max_bytes if max_bytes is not None else int(
        os.environ.get(HEALTH_HISTORY_MAX_BYTES_ENV, DEFAULT_HEALTH_HISTORY_MAX_BYTES))
    retention = archive_retention_days if archive_retention_days is not None else int(
        os.environ.get(HEALTH_ARCHIVE_RETENTION_DAYS_ENV, DEFAULT_HEALTH_ARCHIVE_RETENTION_DAYS))
    path = _config.health_history_path(vault)
    archive_dir = _config.health_archive_dir(vault)
    lock_path = _config.health_history_lock_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)

    _acquire_health_history_lock(lock_path)
    try:
        line = json.dumps(record, sort_keys=True)
        rotated = None
        if path.is_file() and path.stat().st_size + len(line) + 1 > limit:
            rotated = _rotate_health_history(path, archive_dir)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _append_sparse_metrics(_config.health_sparse_path(vault), record)
        _prune_health_archive(archive_dir, retention)
        return {"appended": True, "rotated": rotated}
    finally:
        _release_health_history_lock(lock_path)


# The sidecar mirrors ONLY the GENUINELY sparse metric — ``golden_score``,
# which is null on every record until the (quarterly-cadence) golden-eval
# branch produces one. ``synthesis_cost_usd`` is deliberately NOT here: it is
# the PERSISTED last metered cost (``latest_synthesis_cost``), non-null on
# every hourly record once synthesis has run once — mirroring it would grow
# the never-rotated sidecar unbounded (review finding [0]), and nothing
# trend-compares it anyway (only ``golden_score`` has a sparse check).
# WAT-01 adds `invariant_unreachable_gold` for exactly the same reason: it is
# read from a reachability artifact produced on a monthly-ish cadence, so it
# is null on ~every hourly record and would fall out of the 14-day read
# window between measurements.
SPARSE_METRICS = ("golden_score", "invariant_unreachable_gold")


def _append_sparse_metrics(sparse_path: Path, record: dict[str, Any]) -> None:
    """Mirror a record's non-null sparse metrics into the never-rotated
    sidecar (review finding [7]). No-op when the record carries none — so the
    sidecar only ever gains a line on the golden-eval (quarterly) cadence.
    Called under the same append lock as the main history write; BEST-EFFORT
    (review finding [2]): a sidecar write failure must NOT propagate and fail
    the main history append that already succeeded — the golden point also
    lives in the main record, and ``health_trend`` unions the two sources."""
    sparse = {k: record.get(k) for k in SPARSE_METRICS if record.get(k) is not None}
    if not sparse:
        return
    sparse["ts"] = record.get("ts")
    sparse["run_id"] = record.get("run_id")
    try:
        sparse_path.parent.mkdir(parents=True, exist_ok=True)
        with sparse_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(sparse, sort_keys=True) + "\n")
    except OSError:
        _log.warning("[health] sparse sidecar append failed (main record kept): %s",
                     sparse_path)


def read_sparse_history(vault: Path) -> list[dict[str, Any]]:
    """Full (never-windowed) sparse-metric history from the sidecar — tiny by
    construction (review finding [7]). De-duplicated by ``run_id`` and sorted
    by ``ts``; tolerant of a trailing partial line. Empty list when the
    sidecar does not exist yet."""
    from . import config as _config

    path = _config.health_sparse_path(vault)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: dict[str, dict[str, Any]] = {}
    for rec in parse_recommendation_lines(text):
        key = rec.get("run_id") or f"__no_run_id__{rec.get('ts')}"
        records[key] = rec
    return sorted(records.values(), key=lambda r: str(r.get("ts") or ""))


def read_health_history(
    vault: Path, *, window_days: int | None = None,
) -> list[dict[str, Any]]:
    """Merge the live ``health-history.jsonl`` with RECENT rotated archive
    segments — bounded to the last ``window_days`` by file mtime (default
    14, env-overridable via ``$BRAIN_HEALTH_HISTORY_READ_WINDOW_DAYS``; fix
    [6] — re-reading and re-parsing EVERY archive segment on every hourly
    run does not scale as segments accumulate). De-duplicated by ``run_id``
    and sorted by ``ts``. Read-only — safe to call from ``health_trend`` on
    every run without touching state. The live file is always included
    regardless of age (it is small until it next rotates)."""
    import os
    import time

    from . import config as _config

    win = window_days if window_days is not None else int(
        os.environ.get(HEALTH_HISTORY_READ_WINDOW_DAYS_ENV, DEFAULT_HEALTH_HISTORY_READ_WINDOW_DAYS))
    cutoff = time.time() - win * 86400.0

    records: dict[str, dict[str, Any]] = {}
    paths: list[Path] = []
    archive_dir = _config.health_archive_dir(vault)
    if archive_dir.is_dir():
        for p in sorted(archive_dir.glob("health-history-*.jsonl")):
            try:
                if p.stat().st_mtime >= cutoff:
                    paths.append(p)
            except OSError:
                continue
    live = _config.health_history_path(vault)
    if live.is_file():
        paths.append(live)
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for rec in parse_recommendation_lines(text):
            key = rec.get("run_id") or f"__no_run_id__{p}__{rec.get('ts')}"
            records[key] = rec
    return sorted(records.values(), key=lambda r: str(r.get("ts") or ""))


DEFAULT_LATENCY_REGRESSION_PCT = 0.50
DEFAULT_QUARANTINE_REGRESSION_PCT = 0.25
DEFAULT_GOLDEN_REGRESSION_PCT = 0.05
HEALTH_TREND_MIN_DAYS = 7
HEALTH_TREND_MIN_BASELINE_DAYS = 2
# A median over a PARTIAL day is not a median. The hourly self-test is ONE
# timing sample on a shared laptop (52ms..28s observed on the reference vault),
# so the first one or two runs of a day routinely read as a huge regression
# against a trailing median of whole days. Measured false positive, 2026-08-04:
# at 10:15 the day held 2 samples and fired "+184.3%"; by 16:02 the same day
# held 8 samples and the finding was gone. Median-reduced metrics wait for
# enough of the day to have happened.
HEALTH_TREND_MIN_CURRENT_SAMPLES = 4

# Correction 5 — per-metric daily-bucket reducer. A single generic
# "representative" (e.g. always "last") would average/suppress a real
# single-hour spike; each metric family gets the reducer that keeps that
# spike visible after bucketing hourly records into one-per-day.
_DAILY_REDUCERS: dict[str, str] = {
    "notes": "last", "chunks": "last",
    "snapshot_gen": "last", "snapshot_age_s": "last",
    "quarantine": "last", "duplicate": "last", "decision_candidates": "last",
    "selftest_ms": "median",
    "blocked": "max", "action_required": "max",
    "golden_score": "last_non_null", "synthesis_cost_usd": "last_non_null",
}


def _record_date(rec: dict[str, Any]) -> datetime.date | None:
    ts = str(rec.get("ts") or "")
    try:
        return datetime.date.fromisoformat(ts[:10])
    except ValueError:
        return None


def _bucket_daily(history: list[dict[str, Any]], metric: str) -> dict[datetime.date, Any]:
    """One representative value per calendar day for ``metric``, per its
    schema reducer (see ``_DAILY_REDUCERS``)."""
    reducer = _DAILY_REDUCERS.get(metric, "last")
    per_day: dict[datetime.date, list[Any]] = {}
    for rec in history:
        d = _record_date(rec)
        if d is None:
            continue
        per_day.setdefault(d, []).append(rec.get(metric))

    out: dict[datetime.date, Any] = {}
    for d, values in per_day.items():
        if reducer == "median":
            nums = sorted(x for x in values if isinstance(x, (int, float)))
            if not nums:
                v = None
            else:
                mid = len(nums) // 2
                v = nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2
        elif reducer == "max":
            nums = [x for x in values if isinstance(x, (int, float))]
            v = max(nums) if nums else None
        elif reducer == "last_non_null":
            v = next((x for x in reversed(values) if x is not None), None)
        else:  # "last" — gauge counts (end-of-day snapshot)
            v = values[-1]
        out[d] = v
    return out

# Cross-section binds, deferred past this module's own defs.
from .maintenance_recommendations import parse_recommendation_lines as parse_recommendation_lines  # noqa: E402
from .maintenance import _log as _log  # noqa: E402
