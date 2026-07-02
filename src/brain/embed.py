"""Embedder ADAPTER INTERFACE + the real Arctic-embed embedder + an offline fallback.

Design of record (IDX-01) is Snowflake **Arctic-embed-m-v2.0** (305M, 768-d,
Apache-2.0) run **locally over ONNX Runtime via fastembed — NO PyTorch** (install
footprint + a Windows Defender win). Vectors are truncated to **MRL-256** for
storage (Matryoshka Representation Learning: the first 256 dims of the 768-d
vector carry most of the signal; we re-normalise after truncation).

Two implementations satisfy the ``Embedder`` protocol:

  * ``ArcticEmbedder``  — the real model via ``fastembed.TextEmbedding``. Used
                          when fastembed + onnxruntime are importable AND the
                          model is locally available (bundled / already cached;
                          the Cowork egress allowlist excludes HuggingFace).
  * ``HashEmbedder``    — a deterministic, network-free pseudo-embedder so the
                          index, retrieval, and tests run anywhere with no model
                          download. NOT semantically strong; a stand-in only.

Because both implement the protocol — including ``model_id`` and ``dim`` — the
index stores ``embed_model`` + ``embed_dim`` and forces a **clean rebuild** when
either changes (a HashEmbedder index must never be queried with Arctic vectors,
and vice-versa). Swapping the embedder is a one-line change at the call site.

Canonical task prefixes (IDX-02): Arctic-embed is asymmetric — queries are
embedded with the literal prefix ``query: `` and passages with no prefix. These
canonical prefixes are **never translated** (they are model tokens, not prose);
the in-language *contextual* prefix is a separate, content-level concern handled
in ``brain.chunk``.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, Sequence, runtime_checkable

_TOKEN = re.compile(r"[A-Za-z0-9]+")

# Canonical Arctic-embed task prefix for queries (asymmetric retrieval). Passages
# carry no prefix. NEVER translate these — they are model control tokens.
QUERY_PREFIX = "query: "

# The model of record and its MRL storage dimension.
ARCTIC_MODEL_ID = "snowflake/snowflake-arctic-embed-m-v2.0"
ARCTIC_FULL_DIM = 768
MRL_DIM = 256


def _ort_threads() -> int | None:
    """Intra-op thread count for the ONNX session (S11 speed fix).

    Default: all physical cores (Apple Silicon has no SMT, so logical ==
    physical — on the M4 Pro that is 12). Saturating the batch dimension
    across cores is the biggest ORT knob for bulk embedding throughput.
    Override via ``$BRAIN_EMBED_THREADS``."""
    raw = os.environ.get("BRAIN_EMBED_THREADS")
    if raw and raw.strip().isdigit():
        return int(raw)
    try:
        return os.cpu_count() or None
    except Exception:
        return None


def _ort_providers() -> list[str]:
    """ONNX Runtime execution providers for the embedder (S11 speed fix).

    Default CPU-only (the safe, reproducible path — and the one the eval
    gate ran on). On Apple Silicon, ``$BRAIN_EMBED_PROVIDERS=CoreMLExecutionProvider``
    opts into the Apple Neural Engine / GPU, which can be much faster for
    bulk encode — but CoreML compiles the model on first run and may fall
    back per-op, so it is OPT-IN, not the default. Comma-separate for a
    fallback chain (``CoreMLExecutionProvider,CPUExecutionProvider``)."""
    raw = os.environ.get("BRAIN_EMBED_PROVIDERS")
    if raw and raw.strip():
        return [p.strip() for p in raw.split(",") if p.strip()]
    return ["CPUExecutionProvider"]


def _embed_length_sorted(
    prepared: list[str], embed_fn
) -> list[list[float]]:
    """Run a fastembed-style ``embed_fn(list_of_texts) -> generator of raw
    vectors`` over ``prepared`` in LENGTH-SORTED batches, returning the raw
    vectors in the ORIGINAL input order.

    Why (S11 speed finding): fastembed pads every item in one ``embed()`` call to
    the longest item in that call. On the real vault (mean chunk ~245 tokens,
    long tail to ~845) a single bulk call pads everything to ~845 -> ~3-5x wasted
    compute. Sorting by length and encoding in fixed-size batches means each
    forward pass pads only to its local max -> the waste collapses. Measured
    ~1.5x on the real vault (more on heavier-tailed corpora); the sort itself is
    negligible vs encoding. Applies to EVERY embedder, including the incumbent
    e5-small — a model-independent win. Batch size via ``$BRAIN_EMBED_BATCH``
    (default 64; on CPU, bigger is not better — large batches thrash cache)."""
    n = len(prepared)
    if n <= 1:
        return [list(v) for v in embed_fn(prepared)]
    order = sorted(range(n), key=lambda i: len(prepared[i]))
    batch = int(os.environ.get("BRAIN_EMBED_BATCH", "64"))
    out: list[list[float] | None] = [None] * n
    for i in range(0, n, batch):
        idxs = order[i : i + batch]
        vecs = list(embed_fn([prepared[j] for j in idxs]))
        for j, v in zip(idxs, vecs):
            out[j] = list(v)
    return out  # type: ignore[return-value]


@runtime_checkable
class Embedder(Protocol):
    model_id: str
    dim: int

    def embed(self, text: str, *, is_query: bool = False) -> list[float]: ...
    def embed_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]: ...


def _l2_normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        out = [0.0] * len(vec)
        out[0] = 1.0
        return out
    return [v / norm for v in vec]


def mrl_truncate(vec: Sequence[float], dim: int = MRL_DIM) -> list[float]:
    """Matryoshka truncation: take the first ``dim`` dims and re-normalise.

    Arctic-embed-v2.0 is MRL-trained, so a 256-prefix of the 768-d vector is a
    valid (smaller, faster-to-store) embedding once re-normalised to unit length.
    """
    return _l2_normalise(list(vec[:dim]))


class HashEmbedder:
    """Deterministic bag-of-hashed-tokens embedding, L2-normalised.

    NOT semantically strong — a stand-in that gives stable, reproducible vectors
    for the index/retrieval contract and tests. Lexically-similar texts share
    direction, which is enough to exercise the vector path end to end. ``is_query``
    is accepted (protocol parity) but ignored — there is no asymmetry to model.
    """

    model_id = "hash-v1"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        return _l2_normalise(vec)

    def embed_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        return [self.embed(t, is_query=is_query) for t in texts]


class ArcticEmbedder:
    """Snowflake Arctic-embed-m-v2.0 via fastembed/ONNX — NO PyTorch.

    Lazy: the ONNX model is loaded on first ``embed``/``embed_batch`` so merely
    constructing the embedder (to read ``model_id``/``dim`` for a meta check) is
    cheap and offline. Raises ``EmbedderUnavailable`` if fastembed/onnxruntime
    is not importable.
    """

    def __init__(
        self,
        model_id: str = ARCTIC_MODEL_ID,
        dim: int = MRL_DIM,
        *,
        cache_dir: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.dim = dim  # MRL storage dim
        # Bundled-model path (S06 / INT-02): on the Cowork VM, HuggingFace is NOT
        # on the egress allowlist, so the model is shipped in the workspace and
        # ``$BRAIN_MODEL_CACHE`` points fastembed at that mounted cache dir. ONNX
        # Runtime memory-maps the model file from there — read in place from the
        # mount, never copied to /tmp.
        self._cache_dir = cache_dir or os.environ.get("BRAIN_MODEL_CACHE")
        self._model = None  # lazily created TextEmbedding

    @staticmethod
    def available() -> bool:
        try:
            import fastembed  # noqa: F401
            import onnxruntime  # noqa: F401

            return True
        except Exception:
            return False

    def _ensure_model(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except Exception as exc:  # pragma: no cover - exercised when absent
                raise EmbedderUnavailable(
                    "fastembed/onnxruntime not importable; install the 'embed' "
                    "extra or bundle the model"
                ) from exc
            self._model = TextEmbedding(
                model_name=self.model_id, cache_dir=self._cache_dir,
                threads=_ort_threads(), providers=_ort_providers(),
            )
        return self._model

    def _encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
        model = self._ensure_model()
        prepared = [(QUERY_PREFIX + t) if is_query else t for t in texts]
        return [mrl_truncate(v, self.dim) for v in _embed_length_sorted(prepared, model.embed)]

    def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        return self._encode([text], is_query)[0]

    def embed_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        return self._encode(list(texts), is_query)


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
                    "fastembed/onnxruntime not importable for CatalogEmbedder"
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


class EmbedderUnavailable(RuntimeError):
    """Raised when the requested embedder backend is not importable/usable."""


def get_embedder(prefer: str = "auto") -> Embedder:
    """Adapter selection.

    ``arctic`` selects ``ArcticEmbedder`` (raises ``EmbedderUnavailable`` if the
    runtime/model is absent). ``hash`` forces the offline fallback (tests).
    ``catalog`` selects ``CatalogEmbedder`` (``$BRAIN_EMBED_MODEL`` required).
    ``auto`` honours ``$BRAIN_EMBED_MODEL`` (real catalogued model) when set and
    importable, else prefers Arctic, else HashEmbedder.
    """
    if prefer == "hash":
        return HashEmbedder()
    if prefer == "arctic":
        return ArcticEmbedder()
    cat = os.environ.get("BRAIN_EMBED_MODEL")
    if prefer == "catalog":
        if not cat:
            raise EmbedderUnavailable("prefer='catalog' needs $BRAIN_EMBED_MODEL")
        if is_qwen_model(cat):
            return QwenEmbedder(cat)
        return CatalogEmbedder(cat)
    # auto
    if cat:
        if is_qwen_model(cat) and QwenEmbedder.available():
            return QwenEmbedder(cat)
        if CatalogEmbedder.available():
            return CatalogEmbedder(cat)
    if ArcticEmbedder.available():
        return ArcticEmbedder()
    return HashEmbedder()
