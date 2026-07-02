#!/usr/bin/env python3
"""EmbeddingGemma-300M q8 — length-bucketed batching benchmark (the fair test).

The naive bench padded every chunk to the batch's longest (~845 tokens) when the
mean chunk is ~150 → ~5.6x wasted compute. Real production embedders sort chunks
by length and batch similar lengths together so padding waste is near-zero. This
script measures the TRUE ceiling that way, and also reports the padding-waste
multiplier (sorted vs unsorted) so the win is quantified.

  .venv-embed/bin/python eval/bench_eg_bucketed.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

MODEL_ID = "onnx-community/embeddinggemma-300m-ONNX"
ONNX_REL = os.environ.get("BENCH_ONNX", "onnx/model_quantized.onnx")
VAULT = Path("/Users/user/Downloads/Example-Vault")
DOC_PREFIX = "title: none | text: "
MAX_LEN = 2048
FULL = 83165  # real full-vault chunk count (S11 agent)


def load():
    from huggingface_hub import snapshot_download
    from tokenizers import Tokenizer
    import onnxruntime as ort

    d = snapshot_download(
        MODEL_ID,
        allow_patterns=[ONNX_REL, ONNX_REL + "_data", "tokenizer.json", "tokenizer_config.json",
                        "special_tokens_map.json", "tokenizer.model", "config.json"],
    )
    return d, Tokenizer.from_file(str(Path(d) / "tokenizer.json"))


def main():
    import numpy as np
    d, tok = load()
    tok.enable_truncation(max_length=MAX_LEN)  # truncate only; no padding yet

    from brain.notes import scan_vault
    from brain.chunk import chunk_text, Chunk, detect_language

    chunks = []
    for i, note in enumerate(scan_vault(VAULT)):
        if i >= 40:
            break
        chs = chunk_text(note.body) or [Chunk(0, "", note.title or note.id, detect_language(note.title))]
        chunks += [DOC_PREFIX + (c.text if hasattr(c, "text") else str(c)) for c in chs]

    enc = tok.encode_batch(chunks)  # TRUE per-chunk lengths (no padding)
    lens = [len(e.ids) for e in enc]
    print(
        f"EmbeddingGemma-300M q8 | {len(chunks)} chunks | "
        f"tokens min {min(lens)} mean {sum(lens)//len(lens)} median {sorted(lens)[len(lens)//2]} max {max(lens)} | "
        f"p95 {sorted(lens)[int(len(lens)*0.95)]}",
        flush=True,
    )

    pad_id = tok.token_to_id("<pad>") or 0
    tok.enable_padding(pad_id=pad_id, pad_token="<pad>")  # now pads to batch max

    # reload session fresh (threads)
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = int(os.environ.get("BENCH_EMBED_THREADS", str(os.cpu_count() or 8)))
    sess = ort.InferenceSession(str(Path(d) / ONNX_REL), sess_options=so, providers=["CPUExecutionProvider"])
    in_names = [i.name for i in sess.get_inputs()]

    def embed(texts):
        e = tok.encode_batch(texts)
        ii = np.array([x.ids for x in e], dtype=np.int64)
        am = np.array([x.attention_mask for x in e], dtype=np.int64)
        feed = {}
        for nm in in_names:
            low = nm.lower()
            if "input_id" in low:
                feed[nm] = ii
            elif "attention" in low:
                feed[nm] = am
        sess.run(None, feed)

    # warmup
    embed(chunks[:32])

    def run(batch, order):
        groups = [order[i:i + batch] for i in range(0, len(order), batch)]
        t0 = time.perf_counter()
        for g in groups:
            embed(g)
        return len(order) / (time.perf_counter() - t0)

    order_sorted = [chunks[i] for i in sorted(range(len(chunks)), key=lambda k: lens[k])]
    order_random = chunks[:]  # as-sampled (mixed lengths) = the naive bulk case

    print(f"\n{'batch':>6} {'naive ch/s':>11} {'sorted ch/s':>13} {'sorted×naive':>13} {'full-vault (sorted)':>20}")
    for B in [16, 32, 64]:
        naive = run(B, order_random)
        srt = run(B, order_sorted)
        print(f"{B:>6} {naive:>11.1f} {srt:>13.1f} {srt/naive:>12.2f}x {FULL/srt/60:>17.1f} min", flush=True)


if __name__ == "__main__":
    main()
