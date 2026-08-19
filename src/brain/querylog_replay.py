"""Private-replay evaluation over captured query-log months (host-only)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from . import classification as cls
from . import config, egress
from . import querylog_status as _querylog_status
from .querylog_digest import (
    _normalise_fingerprint,
    empty_digest,
    live_index_fingerprint,
    projection_from_gated,
)

def _read_records(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            # A replay may be pointed directly at the current private month.
            # Cooperate with the capture writer so a valid in-flight append
            # cannot be misclassified as malformed JSONL.
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            except (ImportError, OSError):
                # Non-POSIX capture is disabled, so no compatible appender can
                # be active there. Keep exported-log replay usable if a host
                # copied the file to such a platform.
                fcntl = None  # type: ignore[assignment]
            try:
                raw = handle.read()
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReplayDataError(f"cannot read capture log: {type(exc).__name__}") from exc
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayDataError(f"malformed JSONL at line {number}") from exc
        if not isinstance(item, dict):
            raise ReplayDataError(f"non-object record at line {number}")
        _validate_record(item, number)
        records.append(item)
    return records


def _gated_hybrid(core: Any, record: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rerank = record["rerank"]
    top_n = rerank.get("top_n", 0)
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 15
    # Replay uses the same production trace path as host capture.  The trace
    # remains pre-egress until `projection_from_gated` receives the filtered
    # rows below, so a replay digest cannot resurrect a withheld candidate.
    trace_hits, trace = core.hybrid_search_with_trace(
        record["query"], k=record["k"], rerank=bool(rerank.get("requested")),
        rerank_top=top_n or 15, rrf_k=record["rrf_k"],
    )
    hits = [hit.to_dict() for hit in trace_hits]
    max_tier = record.get("max_tier", cls.DEFAULT_MAX_TIER)
    surfaced, _report = egress.apply_gate(hits, str(max_tier))
    redacted_ids = core.annotate_create_safety(record["query"], surfaced, str(max_tier))
    _top, digest = projection_from_gated(
        surfaced, trace=trace, redacted_ids=redacted_ids,
    )
    return surfaced, digest


def _gated_dossier(core: Any, record: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = core.dossier(record["query"], k=record["k"])
    max_tier = record.get("max_tier", cls.DEFAULT_MAX_TIER)
    decisions, _drep = egress.apply_gate(result.get("decisions", []), str(max_tier))
    sources, _srep = egress.apply_gate(result.get("sources", []), str(max_tier))
    surfaced = decisions + sources
    return surfaced, empty_digest([item.get("id", "") for item in surfaced])


def _jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _rank_movement(left: list[str], right: list[str]) -> dict[str, Any]:
    """Describe movement only among IDs that occur in both top-k lists.

    This deliberately says nothing about a newly introduced or disappeared
    item: a real-traffic ledger has no relevance labels, and a drifted vault
    cannot honestly be decomposed into content causes.  The per-result view
    remains useful alongside Jaccard for vault-same ranking/config changes.
    """
    old_rank = {ident: rank for rank, ident in enumerate(left, start=1)}
    new_rank = {ident: rank for rank, ident in enumerate(right, start=1)}
    deltas = [abs(old_rank[ident] - new_rank[ident]) for ident in old_rank.keys() & new_rank.keys()]
    return {
        "overlapping_ids": len(deltas),
        "mean_absolute_delta": _mean([float(delta) for delta in deltas]),
        "max_absolute_delta": max(deltas) if deltas else None,
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _segment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    jaccards = [float(row["jaccard_at_k"]) for row in rows]
    top1 = [1.0 if row["top1_stable"] else 0.0 for row in rows]
    deltas = [float(row["latency_delta_ms"]) for row in rows]
    baseline = [float(row["baseline_latency_ms"]) for row in rows]
    replayed = [float(row["replay_latency_ms"]) for row in rows]
    digest_baseline = [1.0 if row["baseline_digest_present"] else 0.0 for row in rows]
    digest_replay = [1.0 if row["replay_digest_present"] else 0.0 for row in rows]
    movement = [
        float(row["rank_movement"]["mean_absolute_delta"])
        for row in rows
        if row["rank_movement"]["mean_absolute_delta"] is not None
    ]
    movement_max = [
        int(row["rank_movement"]["max_absolute_delta"])
        for row in rows
        if row["rank_movement"]["max_absolute_delta"] is not None
    ]
    return {
        "count": len(rows),
        "jaccard_at_k": _mean(jaccards),
        "top1_stability": _mean(top1),
        "latency_ms": {
            "baseline_mean": _mean(baseline), "replay_mean": _mean(replayed),
            "delta_mean": _mean(deltas),
        },
        "candidate_digest_presence": {
            "baseline_rate": _mean(digest_baseline), "replay_rate": _mean(digest_replay),
        },
        "rank_movement": {
            "queries_with_overlap": len(movement),
            "mean_absolute_delta": _mean(movement),
            "max_absolute_delta": max(movement_max) if movement_max else None,
        },
    }


def replay(
    core: Any,
    against: str | os.PathLike[str],
    *,
    fail_under_top1: float | None = None,
    fail_under_jaccard: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """Replay an existing host ledger and return ``(report, thresholds_failed)``.

    Only the ``vault_same`` segment is comparable enough to enforce thresholds;
    a changed fingerprint is intentionally reported as drift/mixture without
    attempting to diagnose additions, deletions, moves, or supersessions.
    """
    if _is_vm_role(getattr(core, "role", config.ROLE_HOST)):
        # The CLI rejects this before constructing BrainCore; retain the same
        # boundary for programmatic callers so a VM can never use a mounted
        # export path to read raw host queries through this helper.
        raise ReplayDataError("replay is host-only")
    for label, threshold in (("top1", fail_under_top1), ("jaccard", fail_under_jaccard)):
        if threshold is not None and not 0.0 <= threshold <= 1.0:
            raise ValueError(f"--fail-under-{label} must be in [0, 1]")
    source = Path(against).expanduser().resolve()
    records = _read_records(source)
    current_fingerprint = live_index_fingerprint(core.index)
    if current_fingerprint is None:
        raise ReplayDataError(
            "current live-index fingerprint is missing; run host `brain sync` or `brain rebuild` first"
        )
    same: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for ordinal, record in enumerate(records, start=1):
        started = time.perf_counter()
        if record["mode"] in {"hybrid-search", "search"}:
            surfaced, replay_digest = _gated_hybrid(core, record)
        else:
            surfaced, replay_digest = _gated_dossier(core, record)
        replay_latency = round((time.perf_counter() - started) * 1000, 3)
        old_ids = [
            item["id"]
            for item in sorted(record["top"], key=lambda item: item["final_rank"])
        ]
        new_ids = [item["id"] for item in surfaced if isinstance(item.get("id"), str)]
        k = record["k"]
        old_ids, new_ids = old_ids[:k], new_ids[:k]
        category = "vault_same" if _normalise_fingerprint(record["vault_fingerprint"]) == _normalise_fingerprint(current_fingerprint) else "drift_or_mixed"
        row = {
            "record": ordinal,
            "mode": record["mode"],
            "k": k,
            "comparison": category,
            "jaccard_at_k": round(_jaccard(old_ids, new_ids), 6),
            "top1_stable": (old_ids[:1] == new_ids[:1]),
            "rank_movement": _rank_movement(old_ids, new_ids),
            "baseline_latency_ms": round(float(record["latency_ms"]), 3),
            "replay_latency_ms": replay_latency,
            "latency_delta_ms": round(replay_latency - float(record["latency_ms"]), 3),
            "baseline_digest_present": isinstance(record.get("candidate_digest"), dict),
            "replay_digest_present": isinstance(replay_digest, dict),
        }
        results.append(row)
        (same if category == "vault_same" else drift).append(row)
    same_summary = _segment(same)
    drift_summary = _segment(drift)
    breaches: list[str] = []
    # No comparable records is explicitly a successful, non-gating negative
    # control.  Drift may be interesting, but it cannot establish regression.
    if same and fail_under_top1 is not None and (same_summary["top1_stability"] or 0.0) < fail_under_top1:
        breaches.append("top1")
    if same and fail_under_jaccard is not None and (same_summary["jaccard_at_k"] or 0.0) < fail_under_jaccard:
        breaches.append("jaccard")
    report = {
        "version": VERSION,
        "against": str(source),
        "current_vault_fingerprint": current_fingerprint,
        "records": {"total": len(results), "vault_same": len(same), "drift_or_mixed": len(drift)},
        "vault_same": same_summary,
        "drift_or_mixed": drift_summary,
        "target_qrels": "not_applicable",
        "thresholds": {
            "fail_under_top1": fail_under_top1,
            "fail_under_jaccard": fail_under_jaccard,
            "evaluated_records": len(same),
            "breaches": breaches,
        },
        "results": results,
    }
    return report, bool(breaches)



# Parent-namespace binds, deferred past this module's own defs.
from .querylog import VERSION as VERSION  # noqa: E402
from .querylog import ReplayDataError as ReplayDataError  # noqa: E402
from .querylog import _is_vm_role as _is_vm_role  # noqa: E402

_validate_record = _querylog_status._validate_record
