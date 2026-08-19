"""Generate fused hybrid-search rankings."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class SearchCandidates:
    """Carry the three candidate legs through fusion."""

    lexical: list[int]
    dense: list[int]
    best_chunk_text: dict[int, str]
    best_chunk_rowid: dict[int, int]
    best_dense_score: dict[int, float]
    exact: Any
    keyword_pattern: Any
    collapse: Any

    @property
    def in_lexical(self) -> set[int]:
        return set(self.lexical)

    @property
    def in_dense(self) -> set[int]:
        return set(self.dense)

    @property
    def in_exact(self) -> set[int]:
        return set(self.exact.ranked)


@dataclass
class FusedRanking:
    """Carry fused scores with ranking metadata."""

    candidates: SearchCandidates
    scores: dict[int, float]
    zone: dict[int, str]
    column_zone: dict[int, str]
    valid_date: dict[int, str]


def generate_candidates(
    index: Any, query: str, limit: int, rrf_k: int, trace: Any | None
) -> SearchCandidates:
    """Run each ranking leg then collapse duplicate families."""
    lexical = index._lexical_ranked(query, limit)
    dense_result = index._dense_ranked(query, limit)
    if len(dense_result) == 3:
        dense, best_text, best_rowid = dense_result
        best_score: dict[int, float] = {}
    else:
        dense, best_text, best_rowid, best_score = dense_result
    exact = index._exact_leg(query, rrf_k)
    collapse = index._collapse_duplicate_families(lexical, dense, exact)
    if collapse.canonical_of:
        lexical = collapse.fold(lexical)
        dense, best_text, best_rowid, best_score = collapse.fold_dense(
            dense, best_text, best_rowid, best_score
        )
    if trace is not None:
        from ..index import _family_collapse_enabled

        trace.family_collapse = {
            "enabled": _family_collapse_enabled(),
            "collapsed": collapse.collapsed,
            "declined": collapse.declined,
        }
        trace.lexical_order = list(lexical)
        trace.dense_order = list(dense)
        trace.exact_order = list(exact.ranked)
    return SearchCandidates(
        lexical=lexical,
        dense=dense,
        best_chunk_text=best_text,
        best_chunk_rowid=best_rowid,
        best_dense_score=best_score,
        exact=exact,
        keyword_pattern=index._literal_keyword_pattern(query),
        collapse=collapse,
    )


def _add_standard_leg(
    order: list[int],
    name: str,
    scores: dict[int, float],
    fuse_k: int,
    trace: Any | None,
    *,
    best_chunk_rowid: dict[int, int] | None = None,
    best_dense_score: dict[int, float] | None = None,
) -> None:
    for rank, rowid in enumerate(order, start=1):
        contribution = 1.0 / (fuse_k + rank)
        scores[rowid] = scores.get(rowid, 0.0) + contribution
        if trace is None:
            continue
        details: dict[str, Any] = {"rank": rank, "contribution": contribution}
        if name == "dense":
            details.update(
                {
                    "best_chunk_rowid": (best_chunk_rowid or {}).get(rowid),
                    "similarity": (best_dense_score or {}).get(rowid),
                }
            )
        record = trace.record(rowid)
        record[name] = details
        record["raw_rrf_score"] += contribution


def _add_exact_leg(
    index: Any,
    candidates: SearchCandidates,
    scores: dict[int, float],
    fuse_k: int,
    trace: Any | None,
) -> None:
    owner_count = len(candidates.exact.owner_rowids)
    for rank, rowid in enumerate(candidates.exact.ranked, start=1):
        weight = index._exact_weight(candidates.exact.tiers[rowid], owner_count)
        contribution = weight / (fuse_k + rank)
        scores[rowid] = scores.get(rowid, 0.0) + contribution
        if trace is not None:
            record = trace.record(rowid)
            record["exact"] = {
                "tier": candidates.exact.tiers[rowid],
                "rank": rank,
                "weight": weight,
                "contribution": contribution,
            }
            record["raw_rrf_score"] += contribution


def fuse_candidates(
    index: Any, candidates: SearchCandidates, rrf_k: int, trace: Any | None
) -> dict[int, float]:
    """Fuse lexical, dense, and exact legs at RET-11's constant."""
    fuse_k = index._fusion_k(rrf_k)
    scores: dict[int, float] = {}
    _add_standard_leg(candidates.lexical, "lexical", scores, fuse_k, trace)
    _add_standard_leg(
        candidates.dense,
        "dense",
        scores,
        fuse_k,
        trace,
        best_chunk_rowid=candidates.best_chunk_rowid,
        best_dense_score=candidates.best_dense_score,
    )
    _add_exact_leg(index, candidates, scores, fuse_k, trace)
    return scores


def _load_ranking_metadata(
    index: Any, scores: dict[int, float]
) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    zone: dict[int, str] = {}
    column_zone: dict[int, str] = {}
    valid_date: dict[int, str] = {}
    if not scores:
        return zone, column_zone, valid_date
    rowids = tuple(scores)
    placeholders = ",".join("?" * len(rowids))
    date_expr = "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created)"
    for rowid, stored_zone, path, date in index.conn.execute(
        f"SELECT rowid, zone, path, {date_expr} FROM notes "  # nosec B608
        f"WHERE rowid IN ({placeholders})",
        rowids,
    ):
        rid = int(rowid)
        column_zone[rid] = stored_zone or ""
        zone[rid] = index._resolve_zone(stored_zone or "", path or "")
        valid_date[rid] = date or ""
    return zone, column_zone, valid_date


def _apply_zone_prior(
    index: Any,
    candidates: SearchCandidates,
    scores: dict[int, float],
    zone: dict[int, str],
    trace: Any | None,
) -> None:
    scope = index._zone_scope()
    in_lexical = candidates.in_lexical
    in_exact = candidates.in_exact
    for rowid in scores:
        applies = not (
            scope == "semantic_only" and (rowid in in_lexical or rowid in in_exact)
        )
        factor = index._zone_weight(zone.get(rowid, "")) if applies else 1.0
        scores[rowid] *= factor
        if trace is not None:
            trace.record(rowid)["zone"] = {
                "scope": scope,
                "factor": factor,
                "applied": applies,
            }


def _apply_recency_prior(
    index: Any,
    query: str,
    scores: dict[int, float],
    valid_date: dict[int, str],
    trace: Any | None,
) -> None:
    from ..index import _TEMPORAL_INTENT_RE, _env_float, _recency_factor

    temporal = bool(_TEMPORAL_INTENT_RE.search(query))
    weight = _env_float("BRAIN_RECENCY_WEIGHT", 0.5 if temporal else 0.25)
    if not math.isfinite(weight):
        weight = 0.0
    weight = min(1.0, max(0.0, weight))
    if weight <= 0:
        return
    half_life = _env_float("BRAIN_RECENCY_HALFLIFE_DAYS", 90.0 if temporal else 180.0)
    if not math.isfinite(half_life) or half_life <= 0:
        half_life = 90.0 if temporal else 180.0
    today = index._today_for_search()
    for rowid in scores:
        factor = _recency_factor(valid_date.get(rowid, ""), today, weight, half_life)
        scores[rowid] *= factor
        if trace is not None:
            trace.record(rowid)["staleness"] = {"factor": factor}


def apply_ranking_priors(
    index: Any,
    query: str,
    candidates: SearchCandidates,
    scores: dict[int, float],
    trace: Any | None,
) -> FusedRanking:
    """Apply zone authority then temporal staleness."""
    zone, column_zone, valid_date = _load_ranking_metadata(index, scores)
    _apply_zone_prior(index, candidates, scores, zone, trace)
    _apply_recency_prior(index, query, scores, valid_date, trace)
    if trace is not None:
        for rowid, score in scores.items():
            trace.record(rowid)["pre_rerank_score"] = score
    return FusedRanking(candidates, scores, zone, column_zone, valid_date)
