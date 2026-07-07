"""Reranker ADAPTER INTERFACE + a real cross-encoder + an identity fallback (RET-02).

A reranker is an OPTIONAL precision booster: after the fused hybrid_search
(``brain.index.BrainIndex.hybrid_search``) produces a coarse top-N, a
cross-encoder re-scores each (query, passage) pair jointly and re-orders only
the top 10-20 candidates. It is strictly skippable — every retrieval path runs
correctly with reranking switched OFF, and degrades to the identity reranker
(order preserved) whenever the model runtime is unavailable.

Design of record: **Alibaba-NLP/gte-multilingual-reranker-base** (Apache-2.0,
multilingual, ~int8/ONNX). Like the Arctic embedder it is run locally over ONNX
(no PyTorch) via fastembed's ``TextCrossEncoder``; the model is loaded lazily on
first ``rerank`` so merely constructing the reranker is cheap and offline.

Two implementations satisfy the ``Reranker`` protocol:

  * ``GteReranker``  — the real cross-encoder via fastembed/ONNX. Raises
                       ``RerankerUnavailable`` if the runtime/model is absent.
  * ``NoopReranker`` — identity: returns candidates in their incoming order with
                       monotonically-decreasing synthetic scores. Always
                       available, network-free; the guaranteed fallback and the
                       "rerank skipped" path.

Because rerank is bounded to the top 10-20 (``RERANK_TOP_DEFAULT``), its latency
is comparable to today's single rerank step regardless of corpus size.
"""
from __future__ import annotations

import os
from typing import Protocol, Sequence, runtime_checkable


def _ort_threads() -> int | None:
    """Intra-op thread count for the reranker ONNX session (S11 speed fix).

    Default: all physical cores. The reranker cross-encodes the top-N
    (query, passage) pairs in ONE batched forward pass, so saturating the
    batch dimension across cores is what keeps query p95 interactive.
    Override via ``$BRAIN_RERANK_THREADS``."""
    raw = os.environ.get("BRAIN_RERANK_THREADS")
    if raw and raw.strip().isdigit():
        return int(raw)
    try:
        return os.cpu_count() or None
    except Exception:
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
# rerank" recovery for exactly that case, so the ceiling is env-overridable via
# BRAIN_RERANK_MAX (default 20 keeps the conservative latency default). The
# cross-encoder re-scores the full note body, giving brain a whole-note signal
# at rerank time — the incumbent's structural advantage, recovered post-hoc.
RERANK_TOP_DEFAULT = 15
RERANK_TOP_MIN = 10
RERANK_TOP_MAX = 20

# The model of record. The original design named gte-multilingual-reranker-base,
# but that model is NOT in fastembed's TextCrossEncoder catalog (verified
# 2026-06-28 — TextCrossEncoder.list_supported_models()), so it cannot run in the
# chosen ONNX/no-PyTorch runtime. The of-record multilingual cross-encoder is now
# jina-reranker-v2-base-multilingual (in the fastembed catalog, ~1.1 GB int8/ONNX,
# CC-BY-NC for the weights — gate at deploy if commercial use is required). It
# scores cross-lingual (PT-query ↔ EN-passage) pairs correctly with a wide margin
# where the e5-small bi-encoder cannot. Override via BRAIN_RERANKER_MODEL.
GTE_RERANKER_MODEL_ID = "jinaai/jina-reranker-v2-base-multilingual"


def _resolve_reranker_model() -> str:
    """The reranker model id, env-overridable via ``BRAIN_RERANKER_MODEL``."""
    return os.environ.get("BRAIN_RERANKER_MODEL") or GTE_RERANKER_MODEL_ID


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


class GteReranker:
    """gte-multilingual-reranker-base via fastembed/ONNX — NO PyTorch.

    Lazy: the ONNX cross-encoder is created on first ``rerank`` so constructing
    the reranker (to read ``model_id``) is cheap and offline. Raises
    ``RerankerUnavailable`` if fastembed's cross-encoder support or the model is
    not importable/available.
    """

    def __init__(
        self,
        model_id: str | None = None,
        *,
        cache_dir: str | None = None,
    ) -> None:
        self.model_id = model_id or _resolve_reranker_model()
        self._cache_dir = cache_dir or os.environ.get("BRAIN_FASTEMBED_CACHE")
        self._model = None  # lazily created TextCrossEncoder

    @staticmethod
    def available() -> bool:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401

            return True
        except Exception:
            return False

    def _ensure_model(self):
        if self._model is None:
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except Exception as exc:  # pragma: no cover - exercised when absent
                raise RerankerUnavailable(
                    "fastembed cross-encoder support not importable; install the "
                    "'embed' extra or bundle the reranker model"
                ) from exc
            try:
                self._model = TextCrossEncoder(
                    model_name=self.model_id, cache_dir=self._cache_dir,
                    threads=_ort_threads(), providers=_ort_providers(),
                )
            except Exception as exc:  # pragma: no cover - model unavailable offline
                raise RerankerUnavailable(
                    f"reranker model {self.model_id!r} unavailable: {exc}"
                ) from exc
        return self._model

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        model = self._ensure_model()
        return [float(s) for s in model.rerank(query, list(passages))]


class QwenReranker:
    """Qwen3-Reranker-0.6B via the ``qwen3-embed`` lib (ONNX INT8, Apache-2.0).

    The 2025-2026 best-in-class small multilingual reranker (MMTEB-R 66.36).
    Shipped in S11 (UPG-03) as the upgrade over ``jina-reranker-v2`` — BOTH a
    quality gain (largest on long multilingual docs, +27 on MLDR per the tech
    report) AND a licence fix (jina-reranker-v2 is CC-BY-NC-4.0, a corporate-
    deployment blocker; Qwen3 is Apache-2.0). ~573 MB INT8 ONNX.

    Loaded via the same ``qwen3-embed`` lib as the embedder (it exposes a
    ``TextCrossEncoder`` with a fastembed-compatible ``rerank(query, docs)``
    API). Lazy: the ONNX session is created on first ``rerank``.
    """

    def __init__(
        self,
        model_id: str | None = None,
        *,
        cache_dir: str | None = None,
    ) -> None:
        self.model_id = model_id or _resolve_reranker_model()
        self._cache_dir = cache_dir or os.environ.get("BRAIN_FASTEMBED_CACHE")
        self._model = None

    @staticmethod
    def available() -> bool:
        try:
            import qwen3_embed  # noqa: F401
            import onnxruntime  # noqa: F401

            return True
        except Exception:
            return False

    def _ensure_model(self):
        if self._model is None:
            try:
                from qwen3_embed import TextCrossEncoder
            except Exception as exc:  # pragma: no cover
                raise RerankerUnavailable(
                    "qwen3-embed not importable; pip install qwen3-embed"
                ) from exc
            try:
                self._model = TextCrossEncoder(
                    model_name=self.model_id, cache_dir=self._cache_dir,
                    threads=_ort_threads(), providers=_ort_providers(),
                )
            except Exception as exc:  # pragma: no cover - model unavailable offline
                raise RerankerUnavailable(
                    f"reranker model {self.model_id!r} unavailable: {exc}"
                ) from exc
        return self._model

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        model = self._ensure_model()
        return [float(s) for s in model.rerank(query, list(passages))]


def _is_qwen_reranker(model_id: str) -> bool:
    low = (model_id or "").lower()
    return "qwen3-reranker" in low or "qwen-rerank" in low


# --- Fully-open reranker of record (replaces the CC-BY-NC jina-reranker-v2) ---
# gte-multilingual-reranker-base is Apache-2.0, an XLM-R ENCODER cross-encoder
# (~306M). It is the best QUALITY among the latency-affordable fully-open
# multilingual rerankers: an encoder is far faster per pair than the decoder
# rerankers (Qwen3-Reranker, mxbai-rerank-v2) that fail the HP latency gate, and
# it is licence-clean (jina-reranker-v2 — the prior model of record — is CC-BY-NC,
# barred for commercial use in your organization). Loaded DIRECTLY via ONNX Runtime + tokenizers
# (bge-reranker-v2-m3 / gte are NOT in fastembed's catalogue). Latency ~1.65 s /
# top-15 query on an M4 ⇒ ~4 s projected on a corporate HP, so reranking stays
# OPT-IN (the default at the call site remains NoopReranker).
OPEN_DEFAULT_RERANKER_REPO = "onnx-community/gte-multilingual-reranker-base"
OPEN_DEFAULT_RERANKER_ONNX = "onnx/model.onnx"
OPEN_DEFAULT_RERANKER_MODEL_ID = "Alibaba-NLP/gte-multilingual-reranker-base"


class OnnxReranker:
    """Any HuggingFace ONNX cross-encoder reranker loaded DIRECTLY via ONNX
    Runtime — no fastembed-catalog dependency, no PyTorch at runtime.

    Default-of-record: ``gte-multilingual-reranker-base`` (Apache-2.0). Can also
    load a local exported reranker via ``local_dir`` / ``$BRAIN_RERANKER_ONNX_DIR``
    (e.g. an exported bge-reranker-v2-m3). ENCODER cross-encoders only — do not
    point this at a decoder reranker (Qwen3-Reranker / mxbai-rerank-v2); those
    fail the latency gate on corporate-HP CPU.
    """

    def __init__(
        self,
        *,
        hf_repo: str | None = None,
        onnx_file: str | None = None,
        local_dir: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self.model_id = model_id or OPEN_DEFAULT_RERANKER_MODEL_ID
        self._hf_repo = hf_repo or OPEN_DEFAULT_RERANKER_REPO
        self._onnx_file = onnx_file or OPEN_DEFAULT_RERANKER_ONNX
        self._local_dir = local_dir or os.environ.get("BRAIN_RERANKER_ONNX_DIR")
        self._sess = None
        self._tok = None
        self._in_names: list[str] | None = None

    @staticmethod
    def available() -> bool:
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401

            return True
        except Exception:
            return False

    def _ensure(self):
        if self._sess is None:
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except Exception as exc:  # pragma: no cover
                raise RerankerUnavailable(
                    "onnxruntime/tokenizers not importable for OnnxReranker"
                ) from exc
            try:
                if self._local_dir:
                    base = self._local_dir
                    onnx_path = os.path.join(base, "model_quantized.onnx")
                    if not os.path.exists(onnx_path):
                        onnx_path = os.path.join(base, "model.onnx")
                else:
                    from huggingface_hub import snapshot_download

                    base = snapshot_download(
                        self._hf_repo,
                        allow_patterns=[
                            self._onnx_file,
                            self._onnx_file + "_data",
                            "tokenizer*",
                            "*.json",
                        ],
                    )
                    onnx_path = os.path.join(base, self._onnx_file)
                so = ort.SessionOptions()
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                t = _ort_threads()
                if t:
                    so.intra_op_num_threads = t
                self._sess = ort.InferenceSession(
                    onnx_path, sess_options=so, providers=_ort_providers()
                )
                self._in_names = [i.name for i in self._sess.get_inputs()]
                self._tok = Tokenizer.from_file(os.path.join(base, "tokenizer.json"))
                # Clamp (query, passage) pairs to the cross-encoder's context
                # window before run(): an over-long pair makes the position-
                # embedding broadcast fail (same crash class as the embedder's
                # "512 by 620"). Passages are already char-capped upstream
                # (_apply_rerank -> [:2000]), so at the default 1024 this
                # truncation effectively never fires for gte/jina and does NOT
                # change the S11-frozen rerank numbers — it is pure crash
                # insurance. Override via $BRAIN_RERANK_MAX_TOKENS.
                _rmax = 1024
                try:
                    _rt = os.environ.get("BRAIN_RERANK_MAX_TOKENS")
                    if _rt and _rt.strip().isdigit():
                        _rmax = int(_rt)
                except Exception:
                    pass
                self._tok.enable_truncation(max_length=_rmax)
            except RerankerUnavailable:
                raise
            except Exception as exc:  # pragma: no cover - model unavailable offline
                raise RerankerUnavailable(
                    f"ONNX reranker {self.model_id!r} unavailable: {exc}"
                ) from exc
        return self._sess

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        self._ensure()
        import numpy as np

        enc = self._tok.encode_batch([(query, p) for p in passages])
        ii = np.array([e.ids for e in enc], dtype=np.int64)
        am = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {}
        for nm in self._in_names:
            low = nm.lower()
            if "input_id" in low:
                feed[nm] = ii
            elif "attention" in low:
                feed[nm] = am
            elif "token_type" in low:
                feed[nm] = np.zeros_like(ii)
        logits = self._sess.run(None, feed)[0]
        return [float(x[0]) if hasattr(x, "__len__") else float(x) for x in logits]


def get_reranker(prefer: str = "noop") -> Reranker:
    """Adapter selection.

    ``noop`` forces the identity fallback (the DEFAULT — reranking is OPT-IN at
    the call site, because even the open reranker is ~4 s/query on a corporate
    HP). ``onnx`` selects ``OnnxReranker`` — the fully-open model of record
    (gte-multilingual-reranker-base, Apache-2.0; the CC-BY-NC jina-reranker-v2
    replacement). ``gte`` selects the legacy fastembed ``GteReranker`` (the
    CC-BY-NC jina-v2 catalogue path — AVOID for commercial use).
    ``qwen`` selects ``QwenReranker`` (decoder; fails the HP latency gate —
    legacy only). ``auto`` prefers a Qwen reranker when ``$BRAIN_RERANKER_MODEL``
    names one, else the open OnnxReranker (gte), else the legacy fastembed path,
    else noop.
    """
    env = os.environ.get("BRAIN_RERANKER_PREFER", "").strip().lower()
    if env in {"noop", "gte", "qwen", "onnx"}:
        prefer = env  # eval/AB-test override: force a specific reranker
    if prefer == "noop":
        return NoopReranker()
    if prefer == "qwen":
        return QwenReranker()
    if prefer == "gte":
        return GteReranker()
    if prefer == "onnx":
        return OnnxReranker()
    # auto
    rid = _resolve_reranker_model()
    if _is_qwen_reranker(rid) and QwenReranker.available():
        return QwenReranker()
    # OPEN DEFAULT: gte-multilingual-reranker-base via OnnxReranker (Apache-2.0;
    # replaces the CC-BY-NC jina-reranker-v2). Preferred over the fastembed path.
    if OnnxReranker.available():
        return OnnxReranker()
    if GteReranker.available():
        return GteReranker()
    return NoopReranker()


def clamp_rerank_top(n: int) -> int:
    """Clamp the rerank window to [RERANK_TOP_MIN, ceiling], where the ceiling is
    RERANK_TOP_MAX (20) by default but raisable via ``BRAIN_RERANK_MAX`` for the
    wide-candidate cross-encoder pass that recovers cross-lingually buried docs.
    A bad/zero env value falls back to the conservative default."""
    try:
        hi = int(os.environ.get("BRAIN_RERANK_MAX", "") or RERANK_TOP_MAX)
    except ValueError:
        hi = RERANK_TOP_MAX
    hi = max(RERANK_TOP_MAX, hi)  # never below the design floor of 20
    return max(RERANK_TOP_MIN, min(hi, n))
