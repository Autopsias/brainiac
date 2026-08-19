"""Orchestrate production hybrid search."""

from __future__ import annotations

from typing import Any, Callable

from .search_finalization import (
    finalize_order,
    label_evidence,
    materialize_hits,
    suppress_candidates,
)
from .search_ranking import apply_ranking_priors, fuse_candidates, generate_candidates


def run_hybrid_search(
    index: Any,
    query: str,
    *,
    k: int,
    rrf_k: int,
    candidate_factor: int,
    rerank: bool,
    reranker: Any | None,
    rerank_top: int,
    rerank_gate: bool | None,
    trace: Any | None,
    hit_factory: Callable[..., Any],
) -> list[Any]:
    """Execute the fixed retrieval-stage sequence."""
    if k <= 0:
        return []
    candidate_limit = max(k * candidate_factor, k)
    candidates = generate_candidates(index, query, candidate_limit, rrf_k, trace)
    scores = fuse_candidates(index, candidates, rrf_k, trace)
    ranking = apply_ranking_priors(index, query, candidates, scores, trace)
    ordered = suppress_candidates(index, ranking, trace)
    materialized = materialize_hits(index, ranking, ordered, trace, hit_factory)
    final = finalize_order(
        index,
        query,
        ranking,
        materialized,
        k=k,
        rerank=rerank,
        reranker=reranker,
        rerank_top=rerank_top,
        rerank_gate=rerank_gate,
        trace=trace,
    )
    label_evidence(index, ranking, materialized, final)
    return final
