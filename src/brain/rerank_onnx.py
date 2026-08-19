"""The fully-open ONNX reranker of record (gte-multilingual-reranker-base)."""
from __future__ import annotations

import os
from typing import Any, Sequence

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


def _reranker_weights_cached(hf_repo: str, onnx_file: str) -> bool:
    """Are this reranker's weights already in the local HF cache?

    Offline-only probe: `local_files_only=True` never opens a socket, so this
    is safe to call on every search. Any failure (not cached, no hub package,
    unreadable cache) answers False — the caller degrades to identity.
    """
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            hf_repo,
            allow_patterns=[onnx_file, onnx_file + "_data", "tokenizer*", "*.json"],
            local_files_only=True,
        )
        return True
    except Exception:
        return False


def warm_reranker_weights() -> dict[str, object]:
    """Download the reranker weights — the ONE place allowed to use the network
    for them. Called by `brain warmup` beside the embedder warm, so a user who
    warms up still gets reranking; a user who does not gets the unreranked
    order instead of a surprise download mid-query."""
    repo = OPEN_DEFAULT_RERANKER_REPO
    onnx_file = OPEN_DEFAULT_RERANKER_ONNX
    if _reranker_weights_cached(repo, onnx_file):
        return {"repo": repo, "downloaded": False, "cached": True}
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo,
        allow_patterns=[onnx_file, onnx_file + "_data", "tokenizer*", "*.json"],
    )
    return {"repo": repo, "downloaded": True, "cached": True}


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
    def available(*, hf_repo: str | None = None, onnx_file: str | None = None) -> bool:
        """Usable RIGHT NOW, WITHOUT the network.

        This used to answer "are onnxruntime and tokenizers importable?", which
        made `get_reranker("auto")` hand back a reranker whose `_ensure()` then
        went and DOWNLOADED the cross-encoder mid-search. Once BR-03 made
        reranking default-on, that turned every first `brain search` on a cold
        machine into a model download — on a path that has no business touching
        the network, including `BRAIN_EMBEDDER=hash` (which asks for no model at
        all) and the Cowork VM (which has no HF egress). Measured 2026-08-07:
        the distribution-matrix "offline retrieval smoke (no network)" job
        downloaded 6 files on both macOS and Windows.

        So availability now also requires the weights to be in the local cache.
        Absent, this returns False, `get_reranker("auto")` returns NoopReranker,
        and RET-02's skippable contract degrades retrieval to the unreranked
        order cleanly — no exception, no per-query warning, no network. The one
        sanctioned way to fetch the weights is `brain warmup`.
        """
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except Exception:
            return False
        if os.environ.get("BRAIN_RERANKER_ONNX_DIR"):
            return True
        return _reranker_weights_cached(hf_repo or OPEN_DEFAULT_RERANKER_REPO,
                                       onnx_file or OPEN_DEFAULT_RERANKER_ONNX)

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

                    # local_files_only: belt AND braces. `available()` already
                    # refuses to hand out this reranker uncached, but a caller
                    # can construct OnnxReranker directly (tests, an explicit
                    # BRAIN_RERANKER_PREFER=onnx), and a search must never open
                    # a socket. Uncached => raises => RET-02 degrades to the
                    # unreranked order. `brain warmup` is the download path.
                    base = snapshot_download(
                        self._hf_repo,
                        allow_patterns=[
                            self._onnx_file,
                            self._onnx_file + "_data",
                            "tokenizer*",
                            "*.json",
                        ],
                        local_files_only=True,
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
                # Truncation alone leaves each passage's encoded length free to
                # differ (the normal case for real, variously-sized note
                # bodies), and encode_batch then returns a RAGGED list of ids
                # per passage -- np.array(..., dtype=np.int64) below cannot
                # build a rectangular array from that and raises ValueError,
                # which the skippable contract downstream silently swallows as
                # "model unavailable". Padding to the batch's longest sequence
                # makes every row equal length; attention_mask still zeroes the
                # pad positions out of self-attention, so the exact pad_id/
                # pad_token (irrelevant to a multilingual vocab) doesn't matter.
                self._tok.enable_padding()
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

# Parent-namespace binds, deferred past this module's own defs (circular-import
# safety, whichever of brain.rerank / brain.rerank_onnx loads first).
from .rerank import RerankerUnavailable as RerankerUnavailable  # noqa: E402
from .rerank import _ort_providers as _ort_providers  # noqa: E402
from .rerank import _ort_threads as _ort_threads  # noqa: E402
