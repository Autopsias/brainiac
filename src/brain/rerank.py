"""Reranker ADAPTER INTERFACE + a real cross-encoder + an identity fallback (RET-02).

A reranker is a precision booster: after the fused hybrid_search
(``brain.index.BrainIndex.hybrid_search``) produces a coarse top-N, a
cross-encoder re-scores each (query, passage) pair jointly and re-orders only
the top candidates (default window 20, ceiling 50). It is strictly skippable — every retrieval path runs
correctly with reranking switched OFF (``--no-rerank`` / ``BRAIN_RERANK_DISABLED=1``),
and degrades to the identity reranker (order preserved) whenever the model
runtime is unavailable OR a call exceeds its timeout budget (see
``rerank_timeout_seconds`` below).

BR-03 (owner ruling 2026-08-04): the CLI ships rerank ON by default at
``search``/``hybrid-search``. A second owner ruling the same day set the
default candidate window to **20** (ceiling still 50) — see
``RERANK_TOP_DEFAULT`` below, plus ``eval/FOLLOWUPS.md`` #4 (the ranking
measurement) and #6 (the latency measurement that moved the window).

Design of record: **Alibaba-NLP/gte-multilingual-reranker-base** (Apache-2.0,
multilingual, ~int8/ONNX). Like the Arctic embedder it is run locally over ONNX
(no PyTorch) via fastembed's ``TextCrossEncoder``; the model is loaded lazily on
first ``rerank`` so merely constructing the reranker is cheap and offline.

Two implementations satisfy the ``Reranker`` protocol:

  * ``OnnxReranker`` — the real cross-encoder via direct ONNX Runtime (see
                       ``brain.rerank_onnx``). Raises ``RerankerUnavailable`` if
                       the runtime/model is absent.
  * ``NoopReranker`` — identity: returns candidates in their incoming order with
                       monotonically-decreasing synthetic scores. Always
                       available, network-free; the guaranteed fallback and the
                       "rerank skipped" path.

Because rerank is bounded to a small window (``RERANK_TOP_DEFAULT``, 20), its
latency is comparable to today's single rerank step regardless of corpus size.
The bound is what makes that true: cost is strongly super-linear in the window
on real note bodies (eval/FOLLOWUPS.md #6), not merely proportional.
"""
from __future__ import annotations

import os
from typing import Protocol, Sequence, runtime_checkable


# Measured 2026-10-19: 4 threads match the all-cores wall time at ~40% of the CPU.
_DEFAULT_THREADS = 4


def _ort_threads() -> int | None:
    """Intra-op thread count for the reranker ONNX session (S11 speed fix).

    Default: 4 threads (capped at the core count). More threads spin without
    cutting wall time; raise it for bulk work.
    Override via ``$BRAIN_RERANK_THREADS``."""
    raw = os.environ.get("BRAIN_RERANK_THREADS")
    if raw and raw.strip().isdigit():
        return int(raw)
    try:
        return min(_DEFAULT_THREADS, os.cpu_count() or _DEFAULT_THREADS)
    except Exception:  # fail-open: thread count is a tuning hint; None means library default
        return None


def _ort_providers() -> list[str]:
    """Execution providers for the reranker (S11 speed fix). Default CPU
    (the path the eval gate ran on); opt into Apple CoreML (ANE/GPU) via
    ``$BRAIN_RERANK_PROVIDERS=CoreMLExecutionProvider``. Comma-separate for
    a fallback chain."""
    raw = os.environ.get("BRAIN_RERANK_PROVIDERS")
    if raw and raw.strip():
        return [p.strip() for p in raw.split(",") if p.strip()]
    return ["CPUExecutionProvider"]

# Bound the rerank window: only the coarse top-N is re-scored, never the whole
# candidate set. Clamped to [RERANK_TOP_MIN, RERANK_TOP_MAX] at call sites.
#
# RERANK_TOP_MAX is the latency-vs-recall lever. The original [10,20] band
# assumed the bi-encoder already had the right doc near the top — TRUE for
# same-language queries, FALSE for CROSS-LINGUAL ones, where a relevant
# EN-content note can sit at fused rank 40+ (buried under same-language
# transcript chunks) and never enter a top-20 rerank window. A wide-candidate
# cross-encoder pass (top 100–200) is the standard agentic "retrieve broad →
# rerank" recovery for exactly that case, so the ceiling was once env-overridable (that
# variable was removed 2026-11-14). The cross-encoder re-scores the full note body, giving
# brain a whole-note signal at rerank time — the incumbent's structural
# advantage, recovered post-hoc.
#
# BR-03 (owner ruling 2026-08-04) raised the ceiling 20 -> 50 AND moved the
# default window to 50 with it. The CEILING stands. The DEFAULT was moved back
# to 20 the same day, by a second owner ruling, once the latency the first one
# rested on turned out to be wrong:
#
#   BR-03 chose 50 over 20 on "essentially the same clean latency, p50 5.6s vs
#   5.5s". That 5.6s/8.8s pair is, to the tenth of a second, the WINDOW-20 row
#   -- a window-20 sample labelled as window 50. The committed arms actually
#   measure window 50 at p50 68.0s / p95 188.4s, with 55 of 65 golden queries
#   (85%) exceeding the 30s caller timeout and therefore returning the BARE
#   pre-rerank ordering after a 30-second wait. Reproduced clean on two idle
#   machines (window 20: 8.2s / 11.9s; window 50: 74.6s / 28.1s), so contention
#   is not the explanation -- cost is strongly SUPER-linear in the window, and
#   the "the 50-candidate ONNX batch amortizes almost as well" premise is false
#   on this corpus. Full evidence: eval/FOLLOWUPS.md #6.
#
# Owner ruling 2026-08-04 (second): default window 20. It is the latency
# actually accepted (~5.5s p50 / 8.2s p95, zero timeouts), it reranks EVERY
# query instead of timing out on most of them, and quality you receive beats
# quality that expires. Window 50's better paper numbers (mrr@10 0.499 vs
# 0.411, hit@1 0.439 vs 0.349, recall@20 0.542 vs 0.423 -- see
# eval/FOLLOWUPS.md #4, still the correct RANKING analysis) are only reachable
# if a search is allowed to take a minute or more.
#
# The ceiling stays 50; the old BRAIN_RERANK_TOP / BRAIN_RERANK_MAX overrides
# were removed 2026-11-14.
RERANK_TOP_DEFAULT = 20


def rerank_enabled(requested: bool | None = None) -> bool:
    """Resolve the production rerank default and its global kill switch.

    ``None`` means the caller did not make a per-request choice: reranking is
    enabled unless ``$BRAIN_RERANK_DISABLED`` says otherwise.  An explicit
    boolean always wins.  Keeping this outside the CLI prevents another
    production surface (notably MCP) from silently falling back to bare
    retrieval while the CLI follows BR-03's quality-first default.
    """
    if requested is not None:
        return requested
    raw = os.environ.get("BRAIN_RERANK_DISABLED", "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}
RERANK_TOP_MIN = 10
RERANK_TOP_MAX = 50

# The model of record. The original design named gte-multilingual-reranker-base,
# but that model is NOT in fastembed's TextCrossEncoder catalog (verified
# 2026-06-28 — TextCrossEncoder.list_supported_models()), so it cannot run in the
# chosen ONNX/no-PyTorch runtime. The of-record multilingual cross-encoder is now
# jina-reranker-v2-base-multilingual (in the fastembed catalog, ~1.1 GB int8/ONNX,
# CC-BY-NC for the weights — gate at deploy if commercial use is required). It
# scores cross-lingual (PT-query ↔ EN-passage) pairs correctly with a wide margin
# where the e5-small bi-encoder cannot.
GTE_RERANKER_MODEL_ID = "jinaai/jina-reranker-v2-base-multilingual"


def _resolve_reranker_model() -> str:
    """The reranker model id."""
    return GTE_RERANKER_MODEL_ID


@runtime_checkable
class Reranker(Protocol):
    model_id: str

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """Return one relevance score per passage (higher = more relevant),
        aligned by index with ``passages``."""


class RerankerUnavailable(RuntimeError):
    """Raised when the requested reranker backend is not importable/usable."""


class NoopReranker:
    """Identity reranker: preserves the incoming order.

    Emits descending synthetic scores so a stable sort by score is a no-op on the
    incoming order. This is the value returned whenever reranking is skipped or
    the real model runtime is unavailable — callers never special-case it.
    """

    model_id = "noop"

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        n = len(passages)
        # Descending, position-preserving scores: n, n-1, ... 1.
        return [float(n - i) for i in range(n)]


# The ONNX-Runtime-direct reranker adapter (gte-multilingual-reranker-base,
# the fully-open OPEN_DEFAULT_RERANKER of record) lives in ``brain.rerank_onnx``
# — its own module since it carries the tokenizer/session plumbing that is
# unrelated to the fastembed-catalog adapters above. Imported here (deferred
# past ``RerankerUnavailable``/``_ort_threads``/``_ort_providers`` above, which
# ``rerank_onnx`` itself imports back from this module — see the comment at
# the bottom of ``rerank_onnx.py``) and re-exported so every existing
# ``from brain.rerank import OnnxReranker`` (etc.) call site keeps working
# unchanged.
from .rerank_onnx import (  # noqa: E402
    OPEN_DEFAULT_RERANKER_MODEL_ID as OPEN_DEFAULT_RERANKER_MODEL_ID,
    OPEN_DEFAULT_RERANKER_ONNX as OPEN_DEFAULT_RERANKER_ONNX,
    OPEN_DEFAULT_RERANKER_REPO as OPEN_DEFAULT_RERANKER_REPO,
    OnnxReranker as OnnxReranker,
    _reranker_weights_cached as _reranker_weights_cached,
    warm_reranker_weights as warm_reranker_weights,
)


def get_reranker(prefer: str = "noop") -> Reranker:
    """Adapter selection.

    ``noop`` forces the identity fallback (the DEFAULT — reranking is OPT-IN at
    the call site, because even the open reranker is ~4 s/query on a corporate
    HP). ``onnx`` selects ``OnnxReranker`` — the fully-open model of record
    (gte-multilingual-reranker-base, Apache-2.0). ``auto`` uses the
    OnnxReranker when its weights are available, else noop.
    """
    env = os.environ.get("BRAIN_RERANKER_PREFER", "").strip().lower()
    if env in {"noop", "onnx"}:
        prefer = env  # eval/AB-test override: force a specific reranker
    if prefer == "noop":
        return NoopReranker()
    if prefer == "onnx":
        return OnnxReranker()
    # auto
    if OnnxReranker.available():
        return OnnxReranker()
    return NoopReranker()


def clamp_rerank_top(n: int) -> int:
    """Clamp the rerank window to [RERANK_TOP_MIN, RERANK_TOP_MAX]."""
    return max(RERANK_TOP_MIN, min(RERANK_TOP_MAX, n))


def rerank_timeout_seconds() -> float:
    """Per-call wall-clock BUDGET FOR THE CALLER, not the compute (BR-03
    circuit breaker). An ONNX forward pass cannot be interrupted mid-call, so
    a caller that times out here has NOT stopped the reranker -- the worker
    thread keeps running to completion in the background and its result is
    simply discarded; only the CALLER's wait is bounded. See
    ``BrainIndex._rerank_impl`` for where this is enforced (a persistent
    single-worker executor + ``Future.result(timeout=...)`` -- no process
    pool, the existing skippable contract already covers the discard).

    Default: 30s, and unchanged by the 2026-08-04 window ruling -- the window
    moving 50 -> 20 is exactly what makes 30s a safety valve again. At the
    shipped default window (rerank_top=20) the 66-query golden set measures
    p50 5.5s / p95 8.2s, worst case 10.5s, with ZERO of 65 queries past even
    20s (eval/FOLLOWUPS.md #6). So 30s is ~5.4x the median and ~2.9x the
    SLOWEST query measured: it catches genuine degradation -- CPU contention,
    a stalled model, a pathological note -- rather than sitting on the routine
    path. (It emphatically was NOT a safety valve at window 50, where 85% of
    the same queries blew it and silently fell back to the bare ordering.
    That is the defect the window ruling fixed, not this constant.) A
    wider candidate pass would need this raised with it. Override via ``$BRAIN_RERANK_TIMEOUT_S``."""
    raw = os.environ.get("BRAIN_RERANK_TIMEOUT_S")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 30.0
