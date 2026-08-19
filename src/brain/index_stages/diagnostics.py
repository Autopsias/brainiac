"""Explain target participation in production search."""

from __future__ import annotations

from typing import Any

from .. import classification as cls


def _missing_target(target_id: str, max_tier: str, trace: Any) -> dict[str, Any]:
    if max_tier != cls.TIERS[-1]:
        return {"target": "withheld", "verdict": "withheld"}
    return {
        "target": target_id,
        "verdict": "candidate-miss",
        "trace": {
            "candidate_limit": trace.candidate_limit,
            "result_limit": trace.result_limit,
            "stages": {
                "lexical": {
                    "rank": None,
                    "candidate": False,
                    "matched": False,
                    "cutoff": trace.candidate_limit,
                },
                "dense": {
                    "rank": None,
                    "candidate": False,
                    "matched": False,
                    "cutoff": trace.candidate_limit,
                },
                "exact": {
                    "rank": None,
                    "candidate": False,
                    "matched": False,
                    "cutoff": 0,
                },
            },
            "first_missed_cutoff": None,
            "attribution": None,
        },
    }


def _stage(
    order: list[int], rowid: int, *, rank: int | None, cutoff: int, matched: bool
) -> dict[str, Any]:
    try:
        production_rank = order.index(rowid) + 1
    except ValueError:
        production_rank = None
    return {
        "rank": rank,
        "candidate": production_rank is not None,
        "matched": matched,
        "cutoff": cutoff,
    }


def _target_stages(
    index: Any,
    query: str,
    rowid: int,
    trace: Any,
    *,
    identity_redacted: bool,
) -> dict[str, dict[str, Any]]:
    lexical_rank = index._diagnose_lexical_rank(query, rowid)
    dense_rank = index._diagnose_dense_rank(query, rowid)
    stages = {
        "lexical": _stage(
            trace.lexical_order,
            rowid,
            rank=lexical_rank,
            cutoff=trace.candidate_limit,
            matched=lexical_rank is not None,
        ),
        "dense": _stage(
            trace.dense_order,
            rowid,
            rank=dense_rank,
            cutoff=trace.candidate_limit,
            matched=dense_rank is not None,
        ),
    }
    if identity_redacted:
        stages["exact"] = {
            "rank": None,
            "candidate": None,
            "matched": None,
            "cutoff": None,
        }
    else:
        exact_rank, exact_cutoff, exact_matched = index._diagnose_exact_rank(
            query, rowid, trace.exact_leg_enabled
        )
        stages["exact"] = _stage(
            trace.exact_order,
            rowid,
            rank=exact_rank,
            cutoff=exact_cutoff,
            matched=exact_matched,
        )
    return stages


def _target_verdict(record: dict[str, Any] | None, identity_redacted: bool) -> str:
    if record is None:
        return "candidate-miss"
    if identity_redacted:
        return "organic-candidate"
    if record["pin"]["applied"]:
        return "exact-identity-pinned"
    if record["exact"] and record["exact"]["tier"] == "partial_title":
        return "partial-title-bounded"
    if record["exact"]:
        return "exact-identity-collision"
    return "organic-candidate"


def _first_missed_cutoff(
    record: dict[str, Any] | None,
    stages: dict[str, dict[str, Any]],
    *,
    identity_redacted: bool,
    final_rank: int | None,
    result_limit: int,
) -> dict[str, Any] | None:
    if record is not None:
        return (
            {"stage": "final_result", "cutoff": result_limit}
            if final_rank is None
            else None
        )
    names = ("lexical", "dense") if identity_redacted else ("lexical", "dense", "exact")
    for name in names:
        details = stages[name]
        if details["matched"] and not details["candidate"]:
            return {"stage": name, "cutoff": details["cutoff"]}
    return None


def diagnose_target(
    index: Any,
    query: str,
    target_id: str,
    *,
    max_tier: str,
    trace: Any,
    final_rank: int | None,
) -> dict[str, Any]:
    """Project the production trace through target-safe egress rules."""
    row = index.conn.execute(
        "SELECT rowid, classification FROM notes WHERE id=?", (target_id,)
    ).fetchone()
    if not row:
        return _missing_target(target_id, max_tier, trace)
    rowid, classification = int(row[0]), str(row[1] or "")
    if not cls.ClassificationFilter(max_tier=max_tier).allows(classification):
        return {"target": "withheld", "verdict": "withheld"}
    identity_redacted = target_id in index.identity_egress_redacted_ids(
        query, max_tier
    )
    stages = _target_stages(
        index, query, rowid, trace, identity_redacted=identity_redacted
    )
    record = trace._records.get(rowid)
    return {
        "target": target_id,
        "verdict": _target_verdict(record, identity_redacted),
        "trace": {
            "candidate_limit": trace.candidate_limit,
            "result_limit": trace.result_limit,
            "stages": stages,
            "first_missed_cutoff": _first_missed_cutoff(
                record,
                stages,
                identity_redacted=identity_redacted,
                final_rank=final_rank,
                result_limit=trace.result_limit,
            ),
            "attribution": trace.explain_for(
                rowid, final_rank, redact_identity=identity_redacted
            ),
        },
    }
