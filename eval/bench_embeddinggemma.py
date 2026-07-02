#!/usr/bin/env python3
"""S11 UPG-06 speed probe — EmbeddingGemma-300M ONNX real-M4 throughput.

Loads onnx-community/embeddinggemma-300m-ONNX (q8 = model_quantized.onnx by
default) directly via ONNX Runtime (CPU, intra-op threads = cores) + the
`tokenizers` lib (no transformers/PyTorch), embeds a sample of REAL vault chunks
with the EmbeddingGemma document prefix, and reports docs/sec + a full-vault
index-time extrapolation. Compare vs Qwen3-0.6B (~5 ch/s) and e5-small.

  .venv-embed/bin/python eval/bench_embeddinggemma.py
  BENCH_ONNX=onnx/model_q4.onnx .venv-embed/bin/python eval/bench_embeddinggemma.py   # q4
  BENCH_ONNX=onnx/model.onnx    .venv-embed/bin/python eval/bench_embeddinggemma.py   # fp32
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODEL_ID = "onnx-community/embeddinggemma-300m-ONNX"
VAULT = Path("/Users/user/Downloads/Example-Vault")
ONNX_REL = os.environ.get("BENCH_ONNX", "onnx/model_quantized.onnx")  # q8 default
N_NOTES = int(os.environ.get("BENCH_NOTES", "30"))
REPEAT = int(os.environ.get("BENCH_REPEAT", "3"))
FULL_VAULT_NOTES = 2254
# EmbeddingGemma canonical prefixes (model control tokens — do NOT translate).
DOC_PREFIX = "title: none | text: "
MAX_LEN = 2048  # EmbeddingGemma context


def load():
    from huggingface_hub import snapshot_download

    onnx_data = ONNX_REL + "_data"
    d = snapshot_download(
        MODEL_ID,
        allow_patterns=[
            ONNX_REL,
            onnx_data,
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "tokenizer.model",
            "config.json",
        ],
    )
    onnx_path = str(Path(d) / ONNX_REL)
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(Path(d) / "tokenizer.json"))
    pad_id = tok.token_to_id("<pad>")
    if pad_id is None:
        pad_id = 0
    tok.enable_padding(pad_id=pad_id, pad_token="<pad>")
    tok.enable_truncation(max_length=MAX_LEN)
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    nthreads = int(os.environ.get("BENCH_EMBED_THREADS", str(os.cpu_count() or 8)))
    so.intra_op_num_threads = nthreads
    sess = ort.InferenceSession(onnx_path, sess_options=so, providers=["CPUExecutionProvider"])
    return d, tok, sess


def main():
    import numpy as np

    d, tok, sess = load()
    in_names = [i.name for i in sess.get_inputs()]
    out_names = [o.name for o in sess.get_outputs()]
    print(
        f"EmbeddingGemma-300M | ONNX={ONNX_REL} | CPU intra_op_threads="
        f"{sess.get_session_options().intra_op_num_threads} | inputs={in_names} "
        f"| outputs={out_names}",
        flush=True,
    )

    from brain.notes import scan_vault
    from brain.chunk import chunk_text, Chunk, detect_language

    groups = []
    for i, note in enumerate(scan_vault(VAULT)):
        if i >= N_NOTES:
            break
        chs = chunk_text(note.body) or [
            Chunk(0, "", note.title or note.id, detect_language(note.title))
        ]
        groups.append([DOC_PREFIX + (ch.text if hasattr(ch, "text") else str(ch)) for ch in chs])
    allc = [c for g in groups for c in g]
    lengths = [len(c) for c in allc]
    print(
        f"sampled {len(groups)} notes, {len(allc)} chunks "
        f"(chars min {min(lengths)} mean {sum(lengths) // len(lengths)} max {max(lengths)})",
        flush=True,
    )

    def embed(texts):
        enc = tok.encode_batch(texts)
        input_ids = np.array([e.ids for e in enc], dtype=np.int64)
        attn = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {}
        for nm in in_names:
            low = nm.lower()
            if "input_id" in low:
                feed[nm] = input_ids
            elif "attention" in low:
                feed[nm] = attn
            elif "token_type" in low:
                feed[nm] = np.zeros_like(input_ids)
        outs = sess.run(None, feed)
        out_map = {o.name: v for o, v in zip(sess.get_outputs(), outs)}
        return out_map.get("sentence_embedding", outs[0])

    t0 = time.perf_counter()
    embed(allc[:32])
    print(f"  load+warmup(32): {time.perf_counter() - t0:.1f}s", flush=True)

    def per_note():
        for g in groups:
            embed(g)

    def bulk():
        embed(allc)

    def timeit(fn):
        best = float("inf")
        for _ in range(REPEAT):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    n = len(allc)
    t_pn = timeit(per_note)
    t_bulk = timeit(bulk)
    rate = n / t_bulk
    avg = n / max(1, len(groups))
    full = int(FULL_VAULT_NOTES * avg)
    print(
        f"  per-note ({len(groups)} calls): {n / t_pn:.1f} ch/s | "
        f"bulk (1 call): {rate:.1f} ch/s | bulk/per-note {(n / t_bulk) / (n / t_pn):.2f}x",
        flush=True,
    )
    print(
        f"  => at {rate:.1f} ch/s, full vault (~{full} chunks) ~= {full / rate / 60:.1f} min to index",
        flush=True,
    )
    print(
        f"  (vs Qwen3-0.6B ~5 ch/s => ~277 min; e5-small baseline for comparison)",
        flush=True,
    )


if __name__ == "__main__":
    main()
