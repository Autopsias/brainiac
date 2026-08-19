"""Finalize identity-aware search results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .search_identity import identity_records
from .search_ranking import FusedRanking


@dataclass
class MaterializedSearch:
    """Map ranking rowids onto lightweight result objects."""

    hits: list[Any]
    row_by_id: dict[str, tuple[int, dict[str, Any]]]


def _source(ranking: FusedRanking, rowid: int) -> str:
    in_lexical = rowid in ranking.candidates.in_lexical
    in_dense = rowid in ranking.candidates.in_dense
    if in_lexical and in_dense:
        return "both"
    if in_lexical:
        return "lexical"
    if in_dense:
        return "semantic"
    return "exact"


def suppress_candidates(
    index: Any, ranking: FusedRanking, trace: Any | None
) -> list[int]:
    """Apply near-duplicate suppression after score ordering."""
    ordered = sorted(ranking.scores, key=lambda rid: (-ranking.scores[rid], rid))
    suppressed: set[int] | None = set() if trace is not None else None
    ordered = index._suppress_near_dups(
        ordered,
        ranking.candidates.best_chunk_rowid,
        ranking.zone,
        ranking.column_zone,
        ranking.candidates.in_lexical,
        ranking.candidates.exact.full_rowids,
        suppressed=suppressed,
    )
    if trace is not None:
        trace.pre_rerank_order = list(ordered)
        for rank, rowid in enumerate(ordered, start=1):
            record = trace.record(rowid)
            record["pre_rerank_rank"] = rank
            record["near_duplicate"] = {
                "exempt": rowid in ranking.candidates.exact.full_rowids,
                "suppressed": rowid in suppressed,
            }
    return ordered


def materialize_hits(
    index: Any,
    ranking: FusedRanking,
    ordered: list[int],
    trace: Any | None,
    hit_factory: Callable[..., Any],
) -> MaterializedSearch:
    """Convert candidate rowids into result objects."""
    hits = []
    row_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
    for rowid in ordered:
        row = index._note_row(rowid)
        if not row:
            continue
        snippet_source = ranking.candidates.best_chunk_text.get(rowid, row["body"])
        row_by_id[row["id"]] = (rowid, row)
        if trace is not None:
            trace._id_by_rowid[rowid] = row["id"]
        hits.append(
            hit_factory(
                id=row["id"],
                title=row["title"],
                classification=row["classification"],
                zone=row["zone"],
                path=row["path"],
                score=ranking.scores[rowid],
                source=_source(ranking, rowid),
                snippet=index._snippet(snippet_source),
                is_latest_version=row.get("is_latest_version", ""),
                date=ranking.valid_date.get(rowid, ""),
                type=row.get("type", ""),
                duplicates=ranking.candidates.collapse.absorbed_ids.get(rowid, []),
            )
        )
    return MaterializedSearch(hits, row_by_id)


def _pin_unique_identity(
    index: Any, exact: Any, hits: list[Any], trace: Any | None
) -> Any | None:
    if exact.unique_full_rowid is None:
        return None
    pinned_id = (
        identity_records(index, {exact.unique_full_rowid})
        .get(exact.unique_full_rowid, {})
        .get("id")
    )
    if not pinned_id:
        return None
    if trace is not None:
        trace.record(exact.unique_full_rowid)["pin"]["eligible"] = True
    for position, hit in enumerate(hits):
        if hit.id != pinned_id:
            continue
        pinned = hits.pop(position)
        if trace is not None:
            trace.record(exact.unique_full_rowid)["pin"]["applied"] = True
        return pinned
    return None


def _record_rerank_gate(
    trace: Any | None, *, gate_on: bool, skipped: bool
) -> None:
    if trace is None:
        return
    trace.rerank_gate = {
        "enabled": gate_on,
        "skipped": skipped,
        "reason": (
            "pinned_unique_identity"
            if skipped
            else "gate_disabled"
            if not gate_on
            else "no_unique_identity_pin"
        ),
    }


def _rerank_unpinned(
    index: Any,
    query: str,
    hits: list[Any],
    materialized: MaterializedSearch,
    *,
    reranker: Any | None,
    rerank_top: int,
    trace: Any | None,
) -> list[Any]:
    if trace is None:
        return index._apply_rerank(query, hits, reranker, rerank_top)
    hits, rerank_scores, applied = index._apply_rerank_with_scores(
        query, hits, reranker, rerank_top
    )
    trace.rerank_applied = applied
    for note_id, (score, rank) in rerank_scores.items():
        rowid, _row = materialized.row_by_id[note_id]
        record = trace.record(rowid)
        record["rerank_score"] = score
        record["rerank_rank"] = rank
    return hits


def finalize_order(
    index: Any,
    query: str,
    ranking: FusedRanking,
    materialized: MaterializedSearch,
    *,
    k: int,
    rerank: bool,
    reranker: Any | None,
    rerank_top: int,
    rerank_gate: bool | None,
    trace: Any | None,
) -> list[Any]:
    """Apply pinning, rerank gating, reranking, collision order, then cap."""
    from ..index import rerank_gate_enabled

    hits = materialized.hits
    exact = ranking.candidates.exact
    pinned = _pin_unique_identity(index, exact, hits, trace)
    gate_on = rerank_gate_enabled(rerank_gate)
    gate_skipped = bool(rerank and hits and gate_on and pinned is not None)
    _record_rerank_gate(trace, gate_on=gate_on, skipped=gate_skipped)
    if rerank and hits and not gate_skipped:
        hits = _rerank_unpinned(
            index,
            query,
            hits,
            materialized,
            reranker=reranker,
            rerank_top=rerank_top,
            trace=trace,
        )
    hits = index._normalize_collision_slots(hits, exact.collision_order)
    final = ([pinned] if pinned is not None else []) + hits[
        : max(0, k - (1 if pinned else 0))
    ]
    if trace is not None:
        trace.final_pre_egress_order = [
            materialized.row_by_id[hit.id][0] for hit in final
        ]
        for rank, hit in enumerate(final, start=1):
            rowid = materialized.row_by_id[hit.id][0]
            trace.record(rowid)["_pre_egress_final_rank"] = rank
    return final


def label_evidence(
    index: Any,
    ranking: FusedRanking,
    materialized: MaterializedSearch,
    final: list[Any],
) -> None:
    """Attach evidence labels to the capped pre-egress results."""
    exact = ranking.candidates.exact
    owner_count = len(exact.owner_rowids)
    dense_rank = {
        rowid: rank for rank, rowid in enumerate(ranking.candidates.dense, start=1)
    }
    for hit in final:
        rowid, row = materialized.row_by_id[hit.id]
        evidence = index._evidence_from_exact(exact, rowid)
        if (
            evidence is None
            and ranking.candidates.keyword_pattern is not None
            and index._literal_keyword_match_cached(
                ranking.candidates.keyword_pattern, rowid, row["title"], row["body"]
            )
        ):
            evidence = "keyword_exact"
        similarity = ranking.candidates.best_dense_score.get(rowid, float("-inf"))
        if evidence is None and dense_rank.get(rowid, 999) <= 3 and similarity >= 0.80:
            evidence = "high_vector_match"
        evidence = evidence or "weak_semantic"
        hit.evidence = evidence
        hit.create_safety = index._create_safety_from_evidence(evidence, owner_count)
