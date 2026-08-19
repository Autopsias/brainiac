"""The Qwen3 embedding leg (decoder embedder)."""
from __future__ import annotations

import os
from typing import Sequence

class QwenEmbedder:
    """Qwen3-Embedding-0.6B via the ``qwen3-embed`` lib (ONNX INT8, Apache-2.0).

    The 2025-2026 best-in-class small multilingual embedder (MMTEB 64.33, 32K
    context, 100+ langs incl. EN/PT/ES, 1024-d, MRL-truncatable). Shipped in S11
    (UPG-02) as the upgrade over ``multilingual-e5-small``.

    ``qwen3-embed`` (n24q02m fork) is a fastembed-compatible lib that loads the
    ONNX INT8 export (~573 MB) — chosen because fastembed ≤ 0.8.0 (the only
    released series as of 2026-06) does NOT catalogue Qwen3, and PR #605 is not
    yet released. Lazy: the ONNX session is created on first ``embed`` so
    constructing the embedder (to read ``model_id``/``dim`` for the meta check)
    is cheap and offline. Raises ``EmbedderUnavailable`` if the lib/onnxruntime
    is not importable.

    Qwen3-Embedding is an instruction-tuned decoder embedder: queries MUST carry
    an instruction prefix (``Instruct: ...\\nQuery: ``) for optimal ranking
    (empirically verified 2026-06-28: the prefix widens the relevant-vs-irrelevant
    cosine margin 0.30 -> 0.40). Passages carry NO prefix. This is the same class
    of asymmetry as e5 ``query:``/``passage:``, and the same class of bug if
    omitted (silent ranking degradation). The prefix is model-specific, so it is
    applied here, INSIDE the adapter — the caller passes raw text.
    """

    # The Qwen3-Embedding default retrieval instruction (from the tech report
    # ablations — the generic web-search instruction is the recommended default).
    _QUERY_INSTRUCT = (
        "Instruct: Given a web search query, retrieve relevant passages that "
        "answer the query.\nQuery: "
    )

    def __init__(
        self,
        model_id: str = "n24q02m/Qwen3-Embedding-0.6B-ONNX",
        dim: int | None = None,
        *,
        cache_dir: str | None = None,
    ) -> None:
        self.model_id = model_id
        # default 1024 (full); MRL-truncate to a smaller dim via BRAIN_EMBED_DIM
        self.dim = int(dim if dim is not None else os.environ.get("BRAIN_EMBED_DIM", 1024))
        self._cache_dir = (
            cache_dir
            or os.environ.get("BRAIN_MODEL_CACHE")
            or os.environ.get("BRAIN_FASTEMBED_CACHE")
        )
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
                from qwen3_embed import TextEmbedding
            except Exception as exc:  # pragma: no cover - exercised when absent
                raise EmbedderUnavailable(
                    "qwen3-embed/onnxruntime not importable; pip install qwen3-embed"
                ) from exc
            self._model = TextEmbedding(
                model_name=self.model_id, cache_dir=self._cache_dir,
                threads=_ort_threads(), providers=_ort_providers(),
            )
        return self._model

    def _encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
        model = self._ensure_model()
        prepared = [(self._QUERY_INSTRUCT + t) if is_query else t for t in texts]
        out: list[list[float]] = []
        for v in _embed_length_sorted(prepared, model.embed):
            # MRL-truncate + renormalise to the declared storage dim if the model
            # emits a wider vector (1024 full -> 256/512 storage). Qwen3-Embedding
            # is MRL-trained, so a prefix is a valid smaller embedding.
            out.append(_l2_normalise(v[: self.dim]) if len(v) >= self.dim else _l2_normalise(v))
        return out

    def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        return self._encode([text], is_query)[0]

    def embed_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        return self._encode(list(texts), is_query)


def is_qwen_model(model_id: str) -> bool:
    """True if the model id names a Qwen3-Embedding model (the qwen3-embed lib path)."""
    low = (model_id or "").lower()
    return "qwen3-embedding" in low or "qwen3-embed" in low or "qwen-embed" in low

# Parent-namespace binds, deferred past this module's own defs.
from .embed import (  # noqa: E402
    EmbedderUnavailable as EmbedderUnavailable,
    _embed_length_sorted as _embed_length_sorted,
    _l2_normalise as _l2_normalise,
    _ort_providers as _ort_providers,
    _ort_threads as _ort_threads,
)
