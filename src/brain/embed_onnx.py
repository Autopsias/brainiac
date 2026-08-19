"""Direct-ONNX embedders of record and their model-spec resolution (DIST-01)."""
from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Sequence

# --- Direct-ONNX models (DIST-01: eliminates fastembed) ---------------------
# Each model owns its pooling, prefixes, dimension and exact file. Keeping the
# contract here prevents a future model swap from inheriting another model's
# silent assumptions. Loaded DIRECTLY via onnxruntime + tokenizers — NO
# fastembed, NO PyTorch.
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
E5_SMALL_INT8_MODEL_ID = E5_SMALL_MODEL_ID + "-int8"

BGE_M3_ONNX_REPO = "Xenova/bge-m3"
BGE_M3_INT8_ONNX_FILE = "onnx/model_int8.onnx"
BGE_M3_INT8_MODEL_ID = "BAAI/bge-m3-int8"
BGE_M3_DIM = 1024
# Exact snapshot used by the 2026-08-04/05 measured arm. Pinning is load-bearing:
# ``model_int8.onnx`` and ``model_quantized.onnx`` in this repo are distinct
# files with measurably different retrieval quality.
BGE_M3_ONNX_REVISION = "4de13258303883538bd53b696b452bf8099f0858"

_VALID_QUANTIZATIONS = ("fp32", "int8")


@dataclass(frozen=True)
class OnnxModelSpec:
    """The complete retrieval contract for one direct-ONNX model artifact."""

    model_id: str
    hf_repo: str
    revision: str
    onnx_file: str
    dim: int
    pooling: str
    query_prefix: str
    passage_prefix: str
    quantization: str
    batch_size: int


_ONNX_MODEL_SPECS = {
    BGE_M3_INT8_MODEL_ID: OnnxModelSpec(
        model_id=BGE_M3_INT8_MODEL_ID,
        hf_repo=BGE_M3_ONNX_REPO,
        revision=BGE_M3_ONNX_REVISION,
        onnx_file=BGE_M3_INT8_ONNX_FILE,
        dim=BGE_M3_DIM,
        pooling="cls",
        query_prefix="",
        passage_prefix="",
        quantization="int8",
        # Dynamic-int8 inference is materially batch-shape-sensitive for this
        # export: the same 32 rows embedded at 32 match the adoption probe to
        # 1e-7; at 64 their median cosine to those vectors is only 0.992 and PT
        # loses one top-10 + one top-1. Correctness, not throughput tuning.
        batch_size=32,
    ),
    E5_SMALL_MODEL_ID: OnnxModelSpec(
        model_id=E5_SMALL_MODEL_ID,
        hf_repo=E5_SMALL_ONNX_REPO,
        revision=E5_SMALL_ONNX_REVISION,
        onnx_file=E5_SMALL_ONNX_FILE,
        dim=E5_SMALL_DIM,
        pooling="mean",
        query_prefix="query: ",
        passage_prefix="passage: ",
        quantization="fp32",
        batch_size=64,
    ),
    E5_SMALL_INT8_MODEL_ID: OnnxModelSpec(
        model_id=E5_SMALL_INT8_MODEL_ID,
        hf_repo=E5_SMALL_ONNX_REPO,
        revision=E5_SMALL_ONNX_REVISION,
        onnx_file=E5_SMALL_INT8_ONNX_FILE,
        dim=E5_SMALL_DIM,
        pooling="mean",
        query_prefix="query: ",
        passage_prefix="passage: ",
        quantization="int8",
        batch_size=64,
    ),
}
_ONNX_MODEL_ALIASES = {
    "bge-m3-int8": BGE_M3_INT8_MODEL_ID,
    "e5-small": E5_SMALL_MODEL_ID,
    "e5-small-int8": E5_SMALL_INT8_MODEL_ID,
}
DEFAULT_ONNX_MODEL_ID = BGE_M3_INT8_MODEL_ID


def _resolve_onnx_model_spec(
    model_id: str | None = None,
    quantization: str | None = None,
) -> OnnxModelSpec:
    requested = (
        model_id
        or os.environ.get("BRAIN_EMBED_MODEL")
        or DEFAULT_ONNX_MODEL_ID
    ).strip()
    requested = _ONNX_MODEL_ALIASES.get(requested, requested)

    quant = quantization or os.environ.get("BRAIN_EMBED_QUANT")
    if quant is not None:
        quant = quant.strip().lower()
        if quant not in _VALID_QUANTIZATIONS:
            raise ValueError(
                f"OnnxEmbedder: quantization={quant!r} not in "
                f"{_VALID_QUANTIZATIONS!r}"
            )
        if requested in (E5_SMALL_MODEL_ID, E5_SMALL_INT8_MODEL_ID):
            requested = (
                E5_SMALL_INT8_MODEL_ID if quant == "int8" else E5_SMALL_MODEL_ID
            )
        elif requested == BGE_M3_INT8_MODEL_ID and quant != "int8":
            raise ValueError(
                "BAAI/bge-m3-int8 has no fp32 artifact in the shipped model "
                "spec; select BRAIN_EMBED_MODEL=intfloat/multilingual-e5-small "
                "for the rollback"
            )

    try:
        return _ONNX_MODEL_SPECS[requested]
    except KeyError as exc:
        known = ", ".join(sorted(_ONNX_MODEL_SPECS))
        raise ValueError(
            f"OnnxEmbedder: unknown direct-ONNX model {requested!r}; known: {known}"
        ) from exc


def is_direct_onnx_model(model_id: str | None) -> bool:
    """Whether ``model_id`` names a model with a complete shipped ONNX spec."""
    if not model_id:
        return False
    return _ONNX_MODEL_ALIASES.get(model_id, model_id) in _ONNX_MODEL_SPECS


class OnnxEmbedder:
    """A model-specified encoder loaded DIRECTLY via ONNX Runtime.

    The shipped default is bge-m3-int8 (CLS pooling, no task prefixes, 1024-d).
    Setting ``BRAIN_EMBED_MODEL=intfloat/multilingual-e5-small`` selects the
    rollback model (mean pooling, query/passage prefixes, 384-d). The caller
    always supplies raw text; this adapter applies the resolved model contract.

    Lazy: the ONNX session + tokenizer are created on first ``embed`` so
    constructing the embedder (to read ``model_id``/``dim`` for the index
    model-change guard) is cheap and offline. Raises ``EmbedderUnavailable``
    if onnxruntime/tokenizers is not importable or the model is unavailable.

    Offline-first: set ``$BRAIN_MODEL_CACHE`` (or pass ``local_dir``) to point
    at a bundled/snapshot model dir so NO HuggingFace download is attempted.

    ``quantization`` remains as a compatibility selector for e5-small's fp32 /
    int8 pair. The production rollback is the model env hook above; model ids
    and dimensions remain distinct so the index mismatch guard forces a clean
    rebuild across every swap.
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
        spec = _resolve_onnx_model_spec(model_id, quantization)
        self._model_spec = spec
        self.quantization = spec.quantization
        self.model_id = spec.model_id
        self.dim = int(dim if dim is not None else spec.dim)
        self._pooling = spec.pooling
        self._query_prefix = spec.query_prefix
        self._passage_prefix = spec.passage_prefix
        self._batch_size = spec.batch_size
        self._hf_repo = hf_repo or spec.hf_repo
        # Pin the download to a reproducible revision for the known repo
        # (overridable via $BRAIN_EMBED_REVISION); a custom repo pins nothing
        # unless the caller sets the env var.
        self._revision = (
            os.environ.get("BRAIN_EMBED_REVISION")
            or (spec.revision if self._hf_repo == spec.hf_repo else None)
        )
        self._onnx_file = onnx_file or spec.onnx_file
        self._local_dir = (
            local_dir
            or cache_dir
            or os.environ.get("BRAIN_MODEL_CACHE")
            or os.environ.get("BRAIN_EMBED_ONNX_DIR")
            # Zero-config fallback for the staged VM runtime: locate the bundled
            # model beside the engine, so offline embedding works even when a
            # Cowork session's bootstrap forgot to export BRAIN_MODEL_CACHE.
            # No-op on a pip install (no sibling model dir) → normal HF path.
            or self._staged_model_dir()
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
                    f"onnxruntime/tokenizers not importable for OnnxEmbedder: "
                    f"{type(exc).__name__}: {exc}"
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
                # Truncate to the engine's calibrated chunk window BEFORE padding.
                # The e5 rollback has max_position_embeddings=512; feeding a
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

    def _staged_model_dir(self) -> str | None:
        """Zero-config model location for the staged VM runtime — NO env var.

        Returns the first candidate dir that holds ``self._onnx_file`` +
        ``tokenizer.json`` (the case-1 no-HF snapshot layout), else None.
        Candidates: ``$BRAIN_RUNTIME_DIR/model`` if set, then a ``model/``
        dir beside the staged engine (``.brain/engine/brain/embed.py`` →
        ``.brain/model``). On a pip install there is no sibling ``model/``, so
        this returns None and the normal huggingface_hub path is unchanged —
        this is why blocking hf_hub on the host has no effect, but a Cowork VM
        (no hf_hub) still embeds offline regardless of its bootstrap.
        """
        cands: list[str] = []
        rt = os.environ.get("BRAIN_RUNTIME_DIR")
        if rt:
            cands.append(os.path.join(rt, "model"))
        here = os.path.dirname(os.path.abspath(__file__))          # .brain/engine/brain
        cands.append(os.path.join(os.path.dirname(os.path.dirname(here)), "model"))  # .brain/model
        for d in cands:
            if os.path.isfile(os.path.join(d, self._onnx_file)) and os.path.isfile(
                os.path.join(d, "tokenizer.json")
            ):
                return d
        return None

    def _resolve_model_files(self) -> tuple[str, str]:
        """Resolve (onnx_path, base_dir) across three offline/online layouts:

        1. ``local_dir`` is a SNAPSHOT dir containing ``onnx/model.onnx`` +
           ``tokenizer.json`` directly (the bundled / vendored layout, and the
           resolved-snapshot layout). Preferred — no HF dep at runtime.
        2. ``local_dir`` is an HF-style cache ROOT (contains the resolved
           ``models--<org>--<model>/``): resolve the pinned snapshot
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
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            # No local model was found (no env var, no staged model beside the
            # engine) AND huggingface_hub isn't installed to download one. Name
            # the real cause instead of a bare "No module named 'huggingface_hub'"
            # — the latter sent a whole diagnosis down a vendoring rabbit hole.
            raise EmbedderUnavailable(
                f"no local model found for {self.model_id!r} and huggingface_hub is "
                f"not installed to download one. In an offline/staged runtime "
                f"(e.g. a Cowork VM) set $BRAIN_MODEL_CACHE to a dir containing "
                f"{self._onnx_file!r} + tokenizer.json (the staged .brain/model/), "
                f"or run `brain warmup` on a host with network access."
            ) from exc

        base = snapshot_download(self._hf_repo, allow_patterns=pat, revision=self._revision)
        return os.path.join(base, self._onnx_file), base

    def _encode_raw(self, texts: list[str]) -> list[list[float]]:
        """Apply the resolved model's pooling and L2-normalise."""
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
        if self._pooling == "cls":
            pooled = hidden[:, 0, :]
        else:
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
        prefix = self._query_prefix if is_query else self._passage_prefix
        prepared = [prefix + t for t in texts]
        return [
            v for v in _embed_length_sorted(
                prepared, self._encode_raw, default_batch=self._batch_size
            )
        ]

    def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        return self._encode([text], is_query)[0]

    def embed_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        return self._encode(list(texts), is_query)

# Parent-namespace binds, deferred past this module's own defs.
from .embed import (  # noqa: E402
    EmbedderUnavailable as EmbedderUnavailable,
    _embed_length_sorted as _embed_length_sorted,
    _l2_normalise as _l2_normalise,
    _ort_providers as _ort_providers,
    _ort_threads as _ort_threads,
)
