"""Apply the bounded search reranker."""

from __future__ import annotations

import concurrent.futures
import os
import sys
from typing import Any

from .. import rerank as rerank_mod


def _requested_window(rerank_top: int) -> int:
    configured = os.environ.get("BRAIN_RERANK_TOP")
    if configured:
        try:
            rerank_top = int(configured)
        except ValueError:
            pass
    return rerank_mod.clamp_rerank_top(rerank_top)


def _resolve_reranker(index: Any, supplied: Any | None) -> Any:
    if supplied is not None:
        return supplied
    model_id = rerank_mod._resolve_reranker_model()
    if index._reranker_cache and index._reranker_cache[0] == model_id:
        return index._reranker_cache[1]
    reranker = rerank_mod.get_reranker("auto")
    index._reranker_cache = (model_id, reranker)
    return reranker


def _head_passages(index: Any, head: list[Any]) -> list[str]:
    passages = [
        (index._note_row(index._rowid_of(hit.id)) or {}).get("body", hit.snippet)
        or hit.snippet
        for hit in head
    ]
    return [passage[:2000] for passage in passages]


def _warn_failure(index: Any, reranker: Any, detail: str) -> None:
    if index._rerank_failure_logged:
        return
    index._rerank_failure_logged = True
    print(
        f"brain: WARNING — reranker {reranker.model_id!r} {detail}; falling back "
        "to unreranked order for this and all further queries this session.",
        file=sys.stderr,
    )


def _run_reranker(
    index: Any, reranker: Any, query: str, passages: list[str]
) -> tuple[list[float], bool]:
    applied = not isinstance(reranker, rerank_mod.NoopReranker)
    try:
        if not applied:
            return reranker.rerank(query, passages), False
        if index._rerank_executor is None:
            index._rerank_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="brain-rerank"
            )
        future = index._rerank_executor.submit(reranker.rerank, query, passages)
        return future.result(timeout=rerank_mod.rerank_timeout_seconds()), True
    except concurrent.futures.TimeoutError:
        timeout = rerank_mod.rerank_timeout_seconds()
        _warn_failure(
            index,
            reranker,
            f"exceeded {timeout:.0f}s (the slow call keeps running in the "
            "background; its result is discarded)",
        )
    except Exception as exc:
        if applied:
            _warn_failure(index, reranker, f"raised {type(exc).__name__}: {exc}")
    return rerank_mod.NoopReranker().rerank(query, passages), False


def rerank_hits(
    index: Any,
    query: str,
    hits: list[Any],
    reranker: Any | None,
    rerank_top: int,
    *,
    collect_scores: bool,
) -> list[Any] | tuple[list[Any], dict[str, tuple[float, int]], bool]:
    """Rerank one bounded head while preserving its untouched tail."""
    resolved = _resolve_reranker(index, reranker)
    top_n = _requested_window(rerank_top)
    head, tail = hits[:top_n], hits[top_n:]
    relevance, applied = _run_reranker(
        index, resolved, query, _head_passages(index, head)
    )
    ranked_pairs = sorted(zip(relevance, head), key=lambda pair: pair[0], reverse=True)
    reordered = [hit for _score, hit in ranked_pairs] + tail
    if not collect_scores:
        return reordered
    score_by_id = (
        {
            hit.id: (float(score), rank)
            for rank, (score, hit) in enumerate(ranked_pairs, start=1)
        }
        if applied
        else {}
    )
    return reordered, score_by_id, applied
