"""The legacy fastembed-catalog embedder adapter."""
from __future__ import annotations

import os
from typing import Sequence

from .embed_backends import select_embedder

class CatalogEmbedder:
    """Any fastembed-CATALOGUED model via ONNX — a real-semantic embedder.

    Design-of-record is Arctic-embed-m-v2.0, but that exact checkpoint is NOT in
    the fastembed catalog (flagged S03). This adapter runs any *catalogued*
    model (``intfloat/multilingual-e5-*``,
    ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``, the
    in-catalog Arctic variants, …) so a real multilingual embedder can be used
    as a transparent proxy until the production checkpoint is bundled.

    Activated ONLY when ``$BRAIN_EMBED_MODEL`` is set (see ``get_embedder``) — so
    default behaviour is unchanged. ``$BRAIN_EMBED_DIM`` (default 384) declares
    the storage dim WITHOUT loading the model (the index reads ``.dim`` before
    any embed call, for the model-change guard). ``$BRAIN_MODEL_CACHE`` /
    ``$BRAIN_FASTEMBED_CACHE`` point fastembed at a local cache (offline-capable).

    e5-family models are asymmetric (``query:`` / ``passage:`` prefixes); the
    paraphrase-MiniLM family is symmetric. The right prefix scheme is selected
    from the model id so cross-lingual retrieval is not silently degraded.
    """

    def __init__(
        self,
        model_id: str,
        dim: int | None = None,
        *,
        cache_dir: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.dim = int(dim if dim is not None else os.environ.get("BRAIN_EMBED_DIM", 384))
        self._cache_dir = (
            cache_dir
            or os.environ.get("BRAIN_MODEL_CACHE")
            or os.environ.get("BRAIN_FASTEMBED_CACHE")
        )
        self._model = None
        low = model_id.lower()
        self._is_e5 = "e5" in low  # e5 family uses query:/passage: prefixes

    @staticmethod
    def available() -> bool:
        return ArcticEmbedder.available()

    # ONNX-only models NOT in the stock fastembed catalog but registerable via
    # add_custom_model (HF repo carries an ONNX export). Lets brain use, e.g.,
    # multilingual-e5-small — the EXACT model Smart Connections uses — for a true
    # apples-to-apples eval. mean-pooled + L2-normalised, query:/passage: prefixed.
    _CUSTOM_MODELS = {
        "intfloat/multilingual-e5-small": {
            "hf": "Xenova/multilingual-e5-small", "dim": 384,
            "model_file": "onnx/model.onnx",
        },
        "intfloat/multilingual-e5-base": {
            "hf": "Xenova/multilingual-e5-base", "dim": 768,
            "model_file": "onnx/model.onnx",
        },
    }

    def _register_custom(self) -> bool:
        spec = self._CUSTOM_MODELS.get(self.model_id)
        if not spec:
            return False
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType
        try:
            TextEmbedding.add_custom_model(
                model=self.model_id, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=spec["hf"]), dim=spec["dim"],
                model_file=spec.get("model_file", "onnx/model.onnx"),
            )
        except Exception:
            # Already registered (idempotent) or registry quirk — fall through and
            # let TextEmbedding() surface a real error if the model truly isn't usable.
            pass
        return True

    def _ensure_model(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except Exception as exc:  # pragma: no cover
                raise EmbedderUnavailable(
                    f"fastembed/onnxruntime not importable for CatalogEmbedder: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._register_custom()
            self._model = TextEmbedding(
                model_name=self.model_id, cache_dir=self._cache_dir,
                threads=_ort_threads(), providers=_ort_providers(),
            )
        return self._model

    def _prep(self, text: str, is_query: bool) -> str:
        if self._is_e5:
            return ("query: " if is_query else "passage: ") + text
        return text

    def _encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
        model = self._ensure_model()
        prepared = [self._prep(t, is_query) for t in texts]
        out: list[list[float]] = []
        for v in _embed_length_sorted(prepared, model.embed):
            # If the model emits a wider vector than declared, MRL-truncate +
            # renormalise to the declared storage dim; if narrower, keep as-is.
            out.append(_l2_normalise(v[: self.dim]) if len(v) >= self.dim else _l2_normalise(v))
        return out

    def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        return self._encode([text], is_query)[0]

    def embed_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        return self._encode(list(texts), is_query)

# Parent-namespace binds, deferred past this module's own defs.
from .embed import (  # noqa: E402
    ARCTIC_FULL_DIM as ARCTIC_FULL_DIM,
    ArcticEmbedder as ArcticEmbedder,
    EmbedderUnavailable as EmbedderUnavailable,
    _ort_providers as _ort_providers,
    _ort_threads as _ort_threads,
    ARCTIC_MODEL_ID as ARCTIC_MODEL_ID,
    _embed_length_sorted as _embed_length_sorted,
    _l2_normalise as _l2_normalise,
)
