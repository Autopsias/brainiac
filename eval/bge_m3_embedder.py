#!/usr/bin/env python3
"""EM-01 (s06) — a minimal-dependency ONNX embedder for BAAI/bge-m3, used ONLY
for the A/B eval bench (this module is NOT wired into `src/brain/embed.py` —
production embedder swap is em-02, human-gated).

BGE-M3 (568M params, XLM-RoBERTa-large backbone, Apache-2.0, 1024-d dense
output, 100+ languages, 8192 context) ships an ONNX export at
`BAAI/bge-m3/onnx/model.onnx` (+ external-data `model.onnx_data`, ~2.1 GB
fp32). Loaded DIRECTLY via onnxruntime + tokenizers (same minimal-dependency
pattern as `brain.embed.OnnxEmbedder` for e5-small) — no fastembed (BGE-M3 is
not in the fastembed 0.8.0 catalog), no PyTorch, no FlagEmbedding.

Pooling: confirmed via `BAAI/bge-m3/1_Pooling/config.json` —
`pooling_mode_cls_token: true` (all other modes false). So the dense
embedding is the FIRST token's (`<s>`, XLM-R's CLS-equivalent) last-hidden-
state, L2-normalised. NOT mean-pooled (unlike e5-small). No confirmed
query/passage prefix requirement for BGE-M3 dense retrieval (the official
usage examples pass raw text symmetrically for both queries and passages) —
this differs from e5's asymmetric `query:`/`passage:` control tokens.

This module deliberately does NOT implement BGE-M3's sparse (lexical) or
ColBERT (multi-vector) heads — only the dense head, so the A/B is a clean
embedder-for-embedder swap against the incumbent's dense leg.
"""
from __future__ import annotations

import os
import time
from typing import Sequence

import numpy as np


class EmbedderUnavailable(RuntimeError):
    pass


def _l2_normalise_rows(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return mat / norms


class BgeM3OnnxEmbedder:
    """BAAI/bge-m3 dense embedding via direct ONNX Runtime (CLS pooling)."""

    model_id = "BAAI/bge-m3"
    dim = 1024

    def __init__(
        self,
        *,
        onnx_path: str,
        tokenizer_path: str,
        max_length: int = 512,
        threads: int | None = None,
        providers: list[str] | None = None,
    ) -> None:
        self._onnx_path = onnx_path
        self._tokenizer_path = tokenizer_path
        self._max_length = max_length
        self._threads = threads or (os.cpu_count() or None)
        self._providers = providers or ["CPUExecutionProvider"]
        self._sess = None
        self._tok = None
        self._in_names: list[str] | None = None

    @staticmethod
    def available() -> bool:
        try:
            import onnxruntime  # noqa: F401
            from tokenizers import Tokenizer  # noqa: F401

            return True
        except Exception:
            return False

    def _ensure(self):
        if self._sess is not None:
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except Exception as exc:  # pragma: no cover
            raise EmbedderUnavailable(
                "onnxruntime/tokenizers not importable for BgeM3OnnxEmbedder"
            ) from exc
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if self._threads:
            so.intra_op_num_threads = self._threads
        self._sess = ort.InferenceSession(
            self._onnx_path, sess_options=so, providers=self._providers
        )
        self._in_names = [i.name for i in self._sess.get_inputs()]
        self._tok = Tokenizer.from_file(self._tokenizer_path)
        self._tok.enable_truncation(max_length=self._max_length)
        self._tok.enable_padding(pad_id=1, pad_token="<pad>")  # XLM-R pad_token_id=1

    def _encode_raw(self, texts: list[str]) -> np.ndarray:
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
        hidden = self._sess.run(None, feed)[0]  # [batch, seq, 1024]
        cls = hidden[:, 0, :]  # CLS-token pooling (confirmed via 1_Pooling/config.json)
        return _l2_normalise_rows(cls)

    def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        return self.embed_batch([text], is_query=is_query)[0]

    def embed_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        # No confirmed prefix requirement for BGE-M3 dense (symmetric encoder,
        # unlike e5's query:/passage:); is_query kept for protocol parity only.
        out = self._encode_raw(list(texts))
        return [list(map(float, row)) for row in out]


def resolve_model_paths(model_dir: str) -> tuple[str, str]:
    """``model_dir`` is a HF snapshot dir containing ``onnx/model.onnx`` +
    ``onnx/tokenizer.json`` (the layout `snapshot_download` produces for
    BAAI/bge-m3 with allow_patterns=['onnx/*'])."""
    onnx_path = os.path.join(model_dir, "onnx", "model.onnx")
    tok_path = os.path.join(model_dir, "onnx", "tokenizer.json")
    if not os.path.isfile(onnx_path):
        raise EmbedderUnavailable(f"missing {onnx_path}")
    if not os.path.isfile(tok_path):
        raise EmbedderUnavailable(f"missing {tok_path}")
    return onnx_path, tok_path


def build_embedder(model_dir: str, **kw) -> BgeM3OnnxEmbedder:
    onnx_path, tok_path = resolve_model_paths(model_dir)
    return BgeM3OnnxEmbedder(onnx_path=onnx_path, tokenizer_path=tok_path, **kw)


if __name__ == "__main__":
    import sys

    model_dir = sys.argv[1] if len(sys.argv) > 1 else None
    if not model_dir:
        print("usage: bge_m3_embedder.py <snapshot_dir>", file=sys.stderr)
        raise SystemExit(2)
    emb = build_embedder(model_dir)
    texts = [
        "Que plataforma suporta o Indirect Spend na Contoso e como comunica com o SAP?",
        "Which platform supports Indirect Spend at Contoso and how does it talk to SAP?",
        "O deep dive do ERP descreve o Supply4Contoso (S4G/Gamma) como a ferramenta de Indirect Spend.",
    ]
    t0 = time.time()
    vecs = emb.embed_batch(texts)
    dt = time.time() - t0
    print(f"encoded {len(texts)} texts in {dt:.3f}s ({dt/len(texts)*1000:.1f} ms/text)")
    print("dim:", len(vecs[0]))
    a, b, c = vecs
    import math

    def cos(x, y):
        return sum(p * q for p, q in zip(x, y)) / (
            math.sqrt(sum(p * p for p in x)) * math.sqrt(sum(q * q for q in y))
        )

    print("cos(PT_query, EN_query) =", round(cos(a, b), 4))
    print("cos(PT_query, PT_passage) =", round(cos(a, c), 4))
    print("cos(EN_query, PT_passage) =", round(cos(b, c), 4))
