"""Embedder ADAPTER INTERFACE + the shipped ONNX embedders + an offline fallback.

**Shipped default (`auto`): `intfloat/multilingual-e5-small`** run locally via
direct ONNX Runtime (`OnnxEmbedder`, Xenova ONNX export, ~465 MB one-time
download, 384-d) — NO PyTorch, NO fastembed in the core install. The original
design-of-record (IDX-01) was Snowflake Arctic-embed-m-v2.0 (305M, 768-d,
MRL-256 truncation) via fastembed; `ArcticEmbedder` remains available behind
the `[embed]` extra, but e5-small is what `get_embedder("auto")` resolves.

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
import sys
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


# --- Direct-ONNX embedder of record (DIST-01: eliminates fastembed) ---
# intfloat/multilingual-e5-small (Apache-2.0) is the S10/S11-closed model of
# record. The ONNX export at Xenova/multilingual-e5-small is a standard BERT-
# style encoder: inputs (input_ids, attention_mask, token_type_ids), output
# last_hidden_state [batch, seq, 384]. Embedding = MEAN-POOL over the attention
# mask + L2-normalise. Loaded DIRECTLY via onnxruntime + tokenizers — NO
# fastembed, NO PyTorch. This is the minimal-dep path the corporate build uses;
# it mirrors OnnxReranker's approach.
E5_SMALL_ONNX_REPO = "Xenova/multilingual-e5-small"
E5_SMALL_ONNX_FILE = "onnx/model.onnx"
E5_SMALL_MODEL_ID = "intfloat/multilingual-e5-small"
E5_SMALL_DIM = 384
# Pinned HF revision (commit SHA) for the default model download — supply-chain
# hardening: a bare `snapshot_download(repo)` resolves whatever `main` points at
# TODAY, so the model bytes are not reproducible. Pin the exact commit so every
# machine fetches the same artifact; override with $BRAIN_EMBED_REVISION (e.g.
# to an internally-mirrored pin), or bypass downloads entirely with a staged
# $BRAIN_MODEL_CACHE. Only applied to the known repo above; a custom hf_repo
# keeps its own revision semantics.
E5_SMALL_ONNX_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"

# int8-quantized variant (S09/PF-01 — latency optimization). Produced OFFLINE
# by ``eval/int8_quantize_e5.py`` (onnxruntime.quantization.quantize_dynamic,
# weight-only dynamic int8, QInt8, per-channel MatMul) from the SAME
# Xenova/multilingual-e5-small fp32 export — never downloaded pre-quantized,
# so provenance is fully reproducible in-repo. Same tokenizer, same 384-d
# mean-pooled + L2-normalised output contract; ONLY the ONNX weights/ops
# differ. Kept behind an explicit opt-in (constructor arg or
# ``$BRAIN_EMBED_QUANT=int8``) — the IMPLICIT default stays fp32 (KILL-SWITCH:
# omit the flag, or set it to ``fp32``, to get the unchanged production path).
E5_SMALL_INT8_ONNX_FILE = "onnx/model_int8.onnx"
_VALID_QUANTIZATIONS = ("fp32", "int8")


class OnnxEmbedder:
    """multilingual-e5-small loaded DIRECTLY via ONNX Runtime — no fastembed.

    This is the model-of-record embedder for the corporate, minimal-dependency
    build (DIST-01). e5-small is an asymmetric encoder: queries carry the
    ``query: `` prefix, passages carry ``passage: ``. Output is mean-pooled over
    the attention mask and L2-normalised to a 384-d vector.

    Lazy: the ONNX session + tokenizer are created on first ``embed`` so
    constructing the embedder (to read ``model_id``/``dim`` for the index
    model-change guard) is cheap and offline. Raises ``EmbedderUnavailable``
    if onnxruntime/tokenizers is not importable or the model is unavailable.

    Offline-first: set ``$BRAIN_MODEL_CACHE`` (or pass ``local_dir``) to point
    at a bundled/snapshot model dir so NO HuggingFace download is attempted.

    ``quantization`` (S09/PF-01): ``"fp32"`` (default — the shipped production
    weights, UNCHANGED behaviour) or ``"int8"`` (opt-in — loads
    ``onnx/model_int8.onnx`` from the same ``local_dir`` instead, and reports a
    distinct ``model_id`` suffixed ``-int8`` so the index's model-change guard
    forces a clean rebuild before an int8-embedded index is ever queried
    against fp32 vectors, or vice versa — same contract as any other embedder
    swap). Resolution order: explicit ``quantization=`` arg >
    ``$BRAIN_EMBED_QUANT`` > ``"fp32"``. This is the non-destructive
    kill-switch: production callers that never set the env var or pass the
    arg get exactly the pre-S09 fp32 behaviour.
    """

    def __init__(
        self,
        *,
        hf_repo: str | None = None,
        onnx_file: str | None = None,
        local_dir: str | None = None,
        model_id: str | None = None,
        dim: int | None = None,
        cache_dir: str | None = None,
        quantization: str | None = None,
    ) -> None:
        self.quantization = (
            quantization or os.environ.get("BRAIN_EMBED_QUANT") or "fp32"
        ).strip().lower()
        if self.quantization not in _VALID_QUANTIZATIONS:
            raise ValueError(
                f"OnnxEmbedder: quantization={self.quantization!r} not in "
                f"{_VALID_QUANTIZATIONS!r}"
            )
        is_int8 = self.quantization == "int8"
        default_onnx_file = E5_SMALL_INT8_ONNX_FILE if is_int8 else E5_SMALL_ONNX_FILE
        default_model_id = (E5_SMALL_MODEL_ID + "-int8") if is_int8 else E5_SMALL_MODEL_ID
        self.model_id = model_id or default_model_id
        self.dim = int(dim if dim is not None else os.environ.get("BRAIN_EMBED_DIM", E5_SMALL_DIM))
        self._hf_repo = hf_repo or E5_SMALL_ONNX_REPO
        # Pin the download to a reproducible revision for the known repo
        # (overridable via $BRAIN_EMBED_REVISION); a custom repo pins nothing
        # unless the caller sets the env var.
        self._revision = (
            os.environ.get("BRAIN_EMBED_REVISION")
            or (E5_SMALL_ONNX_REVISION if self._hf_repo == E5_SMALL_ONNX_REPO else None)
        )
        self._onnx_file = onnx_file or default_onnx_file
        self._local_dir = (
            local_dir
            or cache_dir
            or os.environ.get("BRAIN_MODEL_CACHE")
            or os.environ.get("BRAIN_EMBED_ONNX_DIR")
        )
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
                raise EmbedderUnavailable(
                    "onnxruntime/tokenizers not importable for OnnxEmbedder"
                ) from exc
            try:
                onnx_path, base = self._resolve_model_files()
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
                # Truncate to the model's context window BEFORE padding. e5-small
                # is a BERT encoder with max_position_embeddings=512; feeding a
                # longer sequence makes the position-embedding Add node fail to
                # broadcast ("512 by 620") and crashes the whole rebuild. The
                # char-based chunk ceiling (chunk.MAX_CHARS) does NOT guarantee
                # <=512 tokens (dense PT/ES or code text tokenises >2x), so the
                # embedder MUST clamp — the same default fastembed/sentence-
                # transformers apply. Override via $BRAIN_EMBED_MAX_TOKENS.
                _max_tok = 512
                try:
                    _mt = os.environ.get("BRAIN_EMBED_MAX_TOKENS")
                    if _mt and _mt.strip().isdigit():
                        _max_tok = int(_mt)
                except Exception:
                    pass
                self._tok.enable_truncation(max_length=_max_tok)
                # Pad to the longest in a batch so encode_batch returns a
                # rectangular [batch, seq] int matrix (not ragged lists).
                self._tok.enable_padding(pad_id=0, pad_token="<pad>")
            except EmbedderUnavailable:
                raise
            except Exception as exc:  # pragma: no cover - model unavailable offline
                raise EmbedderUnavailable(
                    f"ONNX embedder {self.model_id!r} unavailable: {exc}"
                ) from exc
        return self._sess

    def _resolve_model_files(self) -> tuple[str, str]:
        """Resolve (onnx_path, base_dir) across three offline/online layouts:

        1. ``local_dir`` is a SNAPSHOT dir containing ``onnx/model.onnx`` +
           ``tokenizer.json`` directly (the bundled / vendored layout, and the
           resolved-snapshot layout). Preferred — no HF dep at runtime.
        2. ``local_dir`` is an HF-style cache ROOT (contains
           ``models--Xenova--multilingual-e5-small/``): resolve the latest
           snapshot via ``huggingface_hub.snapshot_download(cache_dir=...)``.
        3. No ``local_dir``: download from HF (online) via ``snapshot_download``.
        """
        pat = [
            self._onnx_file,
            self._onnx_file + "_data",
            "tokenizer*",
            "*.json",
        ]
        if self._local_dir:
            direct = os.path.join(self._local_dir, self._onnx_file)
            if os.path.exists(direct) and os.path.exists(
                os.path.join(self._local_dir, "tokenizer.json")
            ):
                return direct, self._local_dir
            # Direct .onnx file path as local_dir.
            if os.path.isfile(self._local_dir) and self._local_dir.endswith(".onnx"):
                return self._local_dir, os.path.dirname(self._local_dir)
            # HF cache root: contains models--<org>--<name>/.
            repo_dir = "models--" + self._hf_repo.replace("/", "--")
            if os.path.isdir(os.path.join(self._local_dir, repo_dir)):
                from huggingface_hub import snapshot_download

                base = snapshot_download(
                    self._hf_repo, cache_dir=self._local_dir, allow_patterns=pat,
                    revision=self._revision,
                )
                return os.path.join(base, self._onnx_file), base
            raise EmbedderUnavailable(
                f"local_dir {self._local_dir!r} is neither a snapshot dir nor an "
                f"HF cache root for {self._hf_repo!r}"
            )
        from huggingface_hub import snapshot_download

        base = snapshot_download(self._hf_repo, allow_patterns=pat, revision=self._revision)
        return os.path.join(base, self._onnx_file), base

    def _encode_raw(self, texts: list[str]) -> list[list[float]]:
        """Mean-pool last_hidden_state over the attention mask + L2-normalise."""
        import numpy as np

        self._ensure()
        enc = self._tok.encode_batch(texts)
        ii = np.array([e.ids for e in enc], dtype=np.int64)
        am = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed: dict[str, object] = {}
        for nm in self._in_names:
            low = nm.lower()
            if "input_id" in low:
                feed[nm] = ii
            elif "attention" in low:
                feed[nm] = am
            elif "token_type" in low:
                feed[nm] = np.zeros_like(ii)
        hidden = self._sess.run(None, feed)[0]  # [batch, seq, dim] float32
        # Mean pool: sum(hidden * mask) / sum(mask), per item.
        mask = am.astype(hidden.dtype)[:, :, None]  # [batch, seq, 1]
        summed = (hidden * mask).sum(axis=1)  # [batch, dim]
        counts = mask.sum(axis=1)
        counts = np.maximum(counts, 1.0)  # avoid div-by-zero
        pooled = summed / counts
        out: list[list[float]] = []
        for v in pooled:
            d = v[: self.dim] if v.shape[0] >= self.dim else v
            out.append(_l2_normalise(list(d)))
        return out

    def _encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
        # e5-family asymmetry: query: / passage: prefixes (model control tokens;
        # never translated). mean-pool + L2-norm happens in _encode_raw.
        prepared = [("query: " if is_query else "passage: ") + t for t in texts]
        return [v for v in _embed_length_sorted(prepared, self._encode_raw)]

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


# The implicit ``auto``-path degrade to the non-semantic ``HashEmbedder`` is the
# single most dangerous silent failure in the stack: retrieval keeps "working"
# but answers with garbage vectors, and ``brain status`` still reports the
# INDEX's recorded embed_model (metadata), not the live embedder actually in use.
# Discovered S11 (dual-run parity) when a venv without ``onnxruntime`` silently
# ran the whole integrity scan on hash vectors and found zero near-dups. So the
# implicit fallback is now LOUD, and fail-closable for production/clean-machine.
_HASH_FALLBACK_MSG = (
    "brain: WARNING — no real semantic embedder is available "
    "(onnxruntime/tokenizers not importable or the e5-small ONNX model is "
    "missing), so retrieval is FALLING BACK to the non-semantic HashEmbedder. "
    "Search/near-dup quality will be effectively random. Install the 'corporate' "
    "extras (onnxruntime + tokenizers) or the bundled model. Set "
    "BRAIN_REQUIRE_REAL_EMBEDDER=1 to fail closed instead of degrading; set "
    "BRAIN_EMBEDDER=hash to select the hash embedder explicitly and silence this."
)


def _implicit_hash_fallback() -> Embedder:
    """Return HashEmbedder for the IMPLICIT auto-path, but never silently:
    fail closed when the operator demanded a real embedder, else warn loudly."""
    if os.environ.get("BRAIN_REQUIRE_REAL_EMBEDDER"):
        raise EmbedderUnavailable(
            "BRAIN_REQUIRE_REAL_EMBEDDER is set but no real semantic embedder is "
            "available (onnxruntime/tokenizers missing or e5-small model absent). "
            "Refusing to degrade to the non-semantic HashEmbedder."
        )
    print(_HASH_FALLBACK_MSG, file=sys.stderr, flush=True)
    return HashEmbedder()


def get_embedder(prefer: str = "auto") -> Embedder:
    """Adapter selection.

    ``hash`` forces the offline fallback (tests, CI) — EXPLICIT, no warning.
    ``onnx`` selects ``OnnxEmbedder`` — the direct-ONNX model-of-record
    (e5-small, Apache-2.0); this is the MINIMAL-DEPENDENCY path the corporate
    build uses (DIST-01: no fastembed, no PyTorch). ``onnx-int8`` (S09/PF-01)
    selects the SAME ``OnnxEmbedder`` with ``quantization="int8"`` — an
    explicit opt-in only; ``onnx``/``auto`` are UNCHANGED and stay fp32 unless
    ``$BRAIN_EMBED_QUANT=int8`` is also set (the non-destructive kill-switch:
    default behaviour never changes). ``arctic``/``catalog``/``qwen`` select
    the legacy fastembed/qwen3-embed paths (kept for A/B only; NOT in the
    corporate build). ``auto`` honours ``$BRAIN_EMBED_MODEL`` when it names a
    Qwen model, else prefers the direct-ONNX ``OnnxEmbedder`` (e5-small,
    fp32 unless ``$BRAIN_EMBED_QUANT=int8``), else degrades to HashEmbedder —
    but that IMPLICIT degrade is never silent (S11): it warns to stderr, or
    fails closed under ``$BRAIN_REQUIRE_REAL_EMBEDDER``.
    """
    if prefer == "hash":
        return HashEmbedder()
    if prefer == "onnx":
        return OnnxEmbedder()
    if prefer == "onnx-int8":
        return OnnxEmbedder(quantization="int8")
    if prefer == "arctic":
        return ArcticEmbedder()
    cat = os.environ.get("BRAIN_EMBED_MODEL")
    if prefer == "catalog":
        if not cat:
            raise EmbedderUnavailable("prefer='catalog' needs $BRAIN_EMBED_MODEL")
        if is_qwen_model(cat):
            return QwenEmbedder(cat)
        return CatalogEmbedder(cat)
    if prefer == "qwen":
        cat = cat or "n24q02m/Qwen3-Embedding-0.6B-ONNX"
        return QwenEmbedder(cat)
    # auto — the default real-semantic path is now direct-ONNX e5-small.
    if cat and is_qwen_model(cat) and QwenEmbedder.available():
        return QwenEmbedder(cat)
    if OnnxEmbedder.available():
        return OnnxEmbedder()
    if cat and CatalogEmbedder.available():
        return CatalogEmbedder(cat)
    if ArcticEmbedder.available():
        return ArcticEmbedder()
    return _implicit_hash_fallback()


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


# Approximate download size for the install/warmup UX hint ONLY (never a perf
# or capability claim) — the plan's own "~300 MB" figure for the fp32 e5-small
# ONNX export. Not read anywhere that affects behaviour.
ONNX_MODEL_SIZE_HINT = "~300 MB"


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
        snapshot_download(
            e._hf_repo, cache_dir=local_dir, allow_patterns=pat,
            local_files_only=True,
        )
        return True
    except Exception:
        return False
