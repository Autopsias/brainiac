"""Embedder ADAPTER INTERFACE + the shipped ONNX embedders + an offline fallback.

**Shipped default (`auto`): `BAAI/bge-m3-int8`** run locally via direct ONNX
Runtime (`OnnxEmbedder`, Xenova ONNX export, ~563 MB staged, 1024-d) — NO
PyTorch, NO fastembed in the core install. `intfloat/multilingual-e5-small`
remains selectable through ``$BRAIN_EMBED_MODEL`` as the rollback model.

Two implementations satisfy the ``Embedder`` protocol:

  * ``OnnxEmbedder``    — the real model via direct ONNX Runtime (see
                          ``brain.embed_onnx``), used when onnxruntime +
                          tokenizers are importable and the model is staged.
  * ``HashEmbedder``    — a deterministic, network-free pseudo-embedder so the
                          index, retrieval, and tests run anywhere with no model
                          download. NOT semantically strong; a stand-in only.

Because both implement the protocol — including ``model_id`` and ``dim`` — the
index stores ``embed_model`` + ``embed_dim`` and forces a **clean rebuild** when
either changes (a HashEmbedder index must never be queried with real-model vectors,
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
from dataclasses import dataclass as dataclass
from typing import Protocol, Sequence, runtime_checkable

from .embed_backends import REMOVED_BACKENDS, select_embedder

_TOKEN = re.compile(r"[A-Za-z0-9]+")

# MRL storage dimension for ``mrl_truncate``.
MRL_DIM = 256


# Measured 2026-10-19: 4 threads match the all-cores wall time at ~40% of the CPU.
_DEFAULT_THREADS = 4


def _ort_threads() -> int | None:
    """Intra-op thread count for the ONNX session (S11 speed fix).

    Default: 4 threads (capped at the core count). More threads spin without
    cutting wall time; raise it for bulk work.
    Override via ``$BRAIN_EMBED_THREADS``."""
    raw = os.environ.get("BRAIN_EMBED_THREADS")
    if raw and raw.strip().isdigit():
        return int(raw)
    try:
        return min(_DEFAULT_THREADS, os.cpu_count() or _DEFAULT_THREADS)
    except Exception:  # fail-open: thread count is a tuning hint; None means library default
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

    For an MRL-trained model a prefix of the vector is a valid (smaller,
    faster-to-store) embedding once re-normalised to unit length.
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
    # True ONLY when auto-selection degraded here because no real embedder was
    # importable. An EXPLICIT `BRAIN_EMBEDDER=hash` leaves it False. The index
    # reads this to decide whether stamping `hash-v1` is a choice or an
    # accident (see `_SchemaMixin._create_schema`).
    implicit_fallback = False

    def __init__(self, dim: int = 384, *, implicit_fallback: bool = False) -> None:
        self.dim = dim
        self.implicit_fallback = implicit_fallback

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
    return HashEmbedder(implicit_fallback=True)


def get_embedder(prefer: str = "auto") -> Embedder:
    """Adapter selection.

    ``hash`` forces the offline fallback (tests, CI) — EXPLICIT, no warning.
    ``onnx`` selects ``OnnxEmbedder`` — the direct-ONNX model-of-record
    (bge-m3-int8, Apache-2.0); this is the MINIMAL-DEPENDENCY path the
    corporate build uses (DIST-01: no fastembed, no PyTorch). ``onnx-int8`` is
    retained as a compatibility spelling for that same shipped int8 path.
    ``auto`` honours ``$BRAIN_EMBED_MODEL``: the known direct-ONNX specs include
    the shipped bge model and the e5-small rollback. With a model named and no
    ONNX runtime it RAISES; with none named it degrades to HashEmbedder — but
    that IMPLICIT degrade is never silent (S11): it warns to stderr, or fails
    closed under ``$BRAIN_REQUIRE_REAL_EMBEDDER``.
    """
    return select_embedder(
        prefer,
        model_name=os.environ.get("BRAIN_EMBED_MODEL"),
        hash_factory=HashEmbedder,
        onnx_factory=OnnxEmbedder,
        onnx_available=OnnxEmbedder.available,
        direct_model=is_direct_onnx_model,
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

    Mirrors ``get_embedder`` but WITHOUT constructing a HashEmbedder or
    emitting the fallback warning, so it is safe to call from the read-only
    doctor.
    """
    forced = os.environ.get("BRAIN_EMBEDDER", "auto").strip().lower()
    if forced == "hash":
        return ("explicit-hash", "hash (BRAIN_EMBEDDER=hash)")
    if forced in REMOVED_BACKENDS:
        return ("implicit-hash", f"{forced}-removed")
    if OnnxEmbedder.available():
        return ("real", "onnx")
    return ("implicit-hash", "onnx-unavailable" if forced in ("onnx", "onnx-int8") else "no-real-embedder")


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
        return None  # unknown embedder shape; not probed
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
    except Exception:  # fail-open: optional dependency probe; None means cannot check
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
    except Exception:  # fail-open: offline cache probe; False means not cached
        return False

# The direct-ONNX embedders of record live in their own module since the
# 2026-08-16 size ratchet; re-exported so every `brain.embed.<name>` caller is
# unchanged.
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

