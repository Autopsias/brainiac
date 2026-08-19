"""Embedder ADAPTER INTERFACE + the shipped ONNX embedders + an offline fallback.

**Shipped default (`auto`): `BAAI/bge-m3-int8`** run locally via direct ONNX
Runtime (`OnnxEmbedder`, Xenova ONNX export, ~563 MB staged, 1024-d) — NO
PyTorch, NO fastembed in the core install. `intfloat/multilingual-e5-small`
remains selectable through ``$BRAIN_EMBED_MODEL`` as the rollback model.

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

Pooling and task prefixes belong to the resolved model specification, never to
``OnnxEmbedder`` itself: bge-m3 uses CLS pooling with no prefixes; e5-small uses
mean pooling with literal ``query: `` / ``passage: `` prefixes. Prefix tokens
are never translated. The in-language *contextual* prefix is a separate,
content-level concern handled in ``brain.chunk``.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from .embed_backends import select_embedder

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
    prepared: list[str], embed_fn, *, default_batch: int = 64,
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
    (the model spec supplies the default; on CPU, bigger is not always better)."""
    n = len(prepared)
    if n <= 1:
        return [list(v) for v in embed_fn(prepared)]
    order = sorted(range(n), key=lambda i: len(prepared[i]))
    batch = int(os.environ.get("BRAIN_EMBED_BATCH", str(default_batch)))
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
                    f"fastembed/onnxruntime not importable; install the 'embed' "
                    f"extra or bundle the model: {type(exc).__name__}: {exc}"
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



class EmbedderUnavailable(RuntimeError):
    """Raised when the requested embedder backend is not importable/usable."""


# The implicit ``auto``-path degrade to the non-semantic ``HashEmbedder`` is the
# single most dangerous silent failure in the stack: retrieval keeps "working"
# but answers with garbage vectors, and ``brain status`` still reports the
# INDEX's recorded embed_model (metadata), not the live embedder actually in use.
# Discovered S11 (dual-run parity) when a venv without ``onnxruntime`` silently
# ran the whole integrity scan on hash vectors and found zero near-dups. So the
# implicit fallback is now LOUD, and fail-closable for production/clean-machine.
_HASH_FALLBACK_MSG = (
    "brain: WARNING — no real semantic embedder is available "
    "(onnxruntime/tokenizers not importable or the bge-m3-int8 ONNX model is "
    "missing), so retrieval is FALLING BACK to the non-semantic HashEmbedder. "
    "Search/near-dup quality will be effectively random. Install the 'corporate' "
    "extras (onnxruntime + tokenizers) or the bundled model. Set "
    "BRAIN_REQUIRE_REAL_EMBEDDER=1 to fail closed instead of degrading; set "
    "BRAIN_EMBEDDER=hash to select the hash embedder explicitly and silence this."
)


def embedder_unavailable_reason() -> str:
    """Probe WHY no real embedder resolved and report the ACTUAL error.

    The old message guessed ("onnxruntime/tokenizers missing OR the model is
    absent") because both ``available()`` and the raise sites discard the
    exception. Field cost (2026-07-16): the Cowork VM reported the identical
    opaque line for SEVEN consecutive runs — every one grounding on lexical
    search only, every dedup `inconclusive` — and no operator could tell a
    missing package from an ABI mismatch from a missing model without shell
    access to the VM. An error that cannot distinguish its own causes cannot
    be fixed remotely; that is the defect this repairs.

    Carries the interpreter version deliberately: the vendored VM wheels are
    built for one CPython minor (`vendor_semantic_deps.py` pins cp311), so an
    image whose python moved yields `undefined symbol` / `No module named` —
    indistinguishable from "never installed" unless the version is printed.
    """
    parts: list[str] = [f"python {sys.version.split()[0]}"]
    for mod in ("onnxruntime", "tokenizers"):
        try:
            __import__(mod)
        except Exception as exc:  # noqa: BLE001 — the reason IS the payload
            parts.append(f"{mod}: {type(exc).__name__}: {exc}")
        else:
            parts.append(f"{mod}: ok")
    return "; ".join(parts)


def _implicit_hash_fallback() -> Embedder:
    """Return HashEmbedder for the IMPLICIT auto-path, but never silently:
    fail closed when the operator demanded a real embedder, else warn loudly."""
    if os.environ.get("BRAIN_REQUIRE_REAL_EMBEDDER"):
        raise EmbedderUnavailable(
            "BRAIN_REQUIRE_REAL_EMBEDDER is set but no real semantic embedder is "
            "available. Refusing to degrade to the non-semantic HashEmbedder. "
            f"Probe: {embedder_unavailable_reason()}"
        )
    print(_HASH_FALLBACK_MSG, file=sys.stderr, flush=True)
    return HashEmbedder()


def get_embedder(prefer: str = "auto") -> Embedder:
    """Adapter selection.

    ``hash`` forces the offline fallback (tests, CI) — EXPLICIT, no warning.
    ``onnx`` selects ``OnnxEmbedder`` — the direct-ONNX model-of-record
    (bge-m3-int8, Apache-2.0); this is the MINIMAL-DEPENDENCY path the
    corporate build uses (DIST-01: no fastembed, no PyTorch). ``onnx-int8`` is
    retained as a compatibility spelling for that same shipped int8 path.
    ``arctic``/``catalog``/``qwen`` select the legacy fastembed/qwen3-embed
    paths (kept for A/B only; NOT in the corporate build). ``auto`` honours
    ``$BRAIN_EMBED_MODEL``: the known direct-ONNX specs include the shipped
    bge model and the e5-small rollback. Otherwise it follows the optional
    catalogue paths, then degrades to HashEmbedder — but that IMPLICIT degrade
    is never silent (S11): it warns to stderr, or fails closed under
    ``$BRAIN_REQUIRE_REAL_EMBEDDER``.
    """
    return select_embedder(
        prefer,
        model_name=os.environ.get("BRAIN_EMBED_MODEL"),
        hash_factory=HashEmbedder,
        onnx_factory=OnnxEmbedder,
        arctic_factory=ArcticEmbedder,
        catalog_factory=CatalogEmbedder,
        qwen_factory=QwenEmbedder,
        onnx_available=OnnxEmbedder.available,
        arctic_available=ArcticEmbedder.available,
        catalog_available=CatalogEmbedder.available,
        qwen_available=QwenEmbedder.available,
        direct_model=is_direct_onnx_model,
        qwen_model=is_qwen_model,
        unavailable_error=EmbedderUnavailable,
        implicit_hash_fallback=_implicit_hash_fallback,
    )


def probe_auto_embedder() -> tuple[str, str]:
    """Read-only classification of which embedder the live runtime WOULD use,
    for ``brain doctor``'s liveness probe (DV-03, 2026-07-09). Returns
    ``(state, backend)`` where ``state`` is one of:

      * ``"real"``          — a real semantic embedder is available;
      * ``"explicit-hash"`` — ``$BRAIN_EMBEDDER=hash`` was chosen deliberately
                              (tests/CI) — NOT a failure; must never gate/alarm;
      * ``"implicit-hash"`` — the auto-path found no real embedder and would
                              SILENTLY degrade to the non-semantic HashEmbedder
                              (the dangerous case — semantic search goes random,
                              the exact silent failure DV-03 hardens against).

    Mirrors the auto-selection in ``get_embedder`` but WITHOUT constructing a
    HashEmbedder or emitting the fallback warning, so it is safe to call from
    the read-only doctor. ponytail: it duplicates the ``.available()`` chain
    rather than calling ``get_embedder`` precisely to avoid that function's
    stderr warning + HashEmbedder construction side effects.
    """
    forced = os.environ.get("BRAIN_EMBEDDER", "auto").strip().lower()
    if forced == "hash":
        return ("explicit-hash", "hash (BRAIN_EMBEDDER=hash)")
    cat = os.environ.get("BRAIN_EMBED_MODEL")
    if forced in ("onnx", "onnx-int8"):
        return ("real", "onnx") if OnnxEmbedder.available() else ("implicit-hash", "onnx-unavailable")
    if forced == "arctic":
        return ("real", "arctic") if ArcticEmbedder.available() else ("implicit-hash", "arctic-unavailable")
    if forced == "qwen":
        return ("real", "qwen") if QwenEmbedder.available() else ("implicit-hash", "qwen-unavailable")
    if forced == "catalog":
        ok = bool(cat) and (QwenEmbedder.available() if (cat and is_qwen_model(cat)) else CatalogEmbedder.available())
        return ("real", "catalog") if ok else ("implicit-hash", "catalog-unavailable")
    # auto (or unset / unrecognised) — mirror get_embedder's discovery order.
    if cat and is_qwen_model(cat) and QwenEmbedder.available():
        return ("real", "qwen")
    if OnnxEmbedder.available():
        return ("real", "onnx")
    if cat and CatalogEmbedder.available():
        return ("real", "catalog")
    if ArcticEmbedder.available():
        return ("real", "arctic")
    return ("implicit-hash", "no-real-embedder")


# Approximate download/staging size for the install/warmup UX hint ONLY (never a
# performance claim). Measured from the exact pinned bge-m3-int8 snapshot used
# in the adoption probe; the selected model file is ``model_int8.onnx`` (never
# the distinct, worse ``model_quantized.onnx`` artifact).
ONNX_MODEL_SIZE_HINT = "~563 MB"


def model_cache_ready(embedder: "Embedder | None" = None) -> bool | None:
    """Non-network probe (S02/CS-01): are the resolved embedder's model weights
    ALREADY present on disk, i.e. would a warmup/first embed call run offline?

    Returns ``True`` (cached, ready), ``False`` (would trigger a download —
    pending), or ``None`` when the question doesn't apply (the explicit
    HashEmbedder never downloads anything, or the embedder shape is unknown).
    NEVER downloads and NEVER constructs an ONNX session — safe to call from
    ``brain status`` on every invocation.
    """
    e = embedder if embedder is not None else get_embedder(
        os.environ.get("BRAIN_EMBEDDER", "auto")
    )
    if isinstance(e, HashEmbedder):
        return None
    if not isinstance(e, OnnxEmbedder):
        return None  # Arctic/Catalog/Qwen: not the S02 auto-default; not probed
    local_dir = e._local_dir
    onnx_file = e._onnx_file
    if local_dir:
        # Bundled/staged/VM layout (S06/INT-02): files are expected directly
        # on disk, no HF cache semantics.
        direct = os.path.join(local_dir, onnx_file)
        if os.path.exists(direct) and os.path.exists(
            os.path.join(local_dir, "tokenizer.json")
        ):
            return True
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        return None
    pat = [onnx_file, onnx_file + "_data", "tokenizer*", "*.json"]
    try:
        # The revision MUST match the one the download pinned. Without it,
        # huggingface_hub resolves the default "main", which needs a `refs/main`
        # file the cache only has when something downloaded BY BRANCH NAME.
        # Pinning to a commit SHA writes `snapshots/<sha>` and no ref, so an
        # unpinned probe raised LocalEntryNotFoundError on a fully populated
        # cache — every fresh install reported `embedder: pending` forever, and
        # the remedy it printed (`warmup` then `sync`) could never clear it.
        # Only a legacy cache carrying a stale `refs/main` masked this.
        snapshot_download(
            e._hf_repo, cache_dir=local_dir, allow_patterns=pat,
            revision=e._revision, local_files_only=True,
        )
        return True
    except Exception:
        return False

# The fastembed catalog adapter, the direct-ONNX embedders of record, and the
# Qwen leg live in their own modules since the 2026-08-16 size ratchet;
# re-exported so every `brain.embed.<name>` caller is unchanged.
from .embed_catalog import CatalogEmbedder as CatalogEmbedder  # noqa: E402,F401
from .embed_onnx import (  # noqa: E402,F401  (facade re-export)
    BGE_M3_DIM as BGE_M3_DIM,
    BGE_M3_INT8_MODEL_ID as BGE_M3_INT8_MODEL_ID,
    BGE_M3_INT8_ONNX_FILE as BGE_M3_INT8_ONNX_FILE,
    BGE_M3_ONNX_REPO as BGE_M3_ONNX_REPO,
    BGE_M3_ONNX_REVISION as BGE_M3_ONNX_REVISION,
    DEFAULT_ONNX_MODEL_ID as DEFAULT_ONNX_MODEL_ID,
    E5_SMALL_DIM as E5_SMALL_DIM,
    E5_SMALL_INT8_MODEL_ID as E5_SMALL_INT8_MODEL_ID,
    E5_SMALL_INT8_ONNX_FILE as E5_SMALL_INT8_ONNX_FILE,
    E5_SMALL_MODEL_ID as E5_SMALL_MODEL_ID,
    E5_SMALL_ONNX_FILE as E5_SMALL_ONNX_FILE,
    E5_SMALL_ONNX_REPO as E5_SMALL_ONNX_REPO,
    E5_SMALL_ONNX_REVISION as E5_SMALL_ONNX_REVISION,
    OnnxEmbedder as OnnxEmbedder,
    OnnxModelSpec as OnnxModelSpec,
    _ONNX_MODEL_ALIASES as _ONNX_MODEL_ALIASES,
    _ONNX_MODEL_SPECS as _ONNX_MODEL_SPECS,
    _VALID_QUANTIZATIONS as _VALID_QUANTIZATIONS,
    _resolve_onnx_model_spec as _resolve_onnx_model_spec,
    is_direct_onnx_model as is_direct_onnx_model,
)
from .embed_qwen import QwenEmbedder as QwenEmbedder  # noqa: E402,F401
from .embed_qwen import is_qwen_model as is_qwen_model  # noqa: E402,F401

