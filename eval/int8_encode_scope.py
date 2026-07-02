#!/usr/bin/env python3
"""S09 (PF-01) — encode the S06/EM-01 scoped chunk corpus with the int8-
quantized e5-small (`brain.embed.OnnxEmbedder(quantization="int8")`).

Reuses the SAME scoped chunk universe `eval/bge_m3_scope_corpus.py` built for
the adoption-validation split (`_evidence/pt-bench/bge-m3-scope-corpus.json`
— all gold-note chunks for every adoption-validation query + a stratified
distractor sample; scope selection is embedder-independent, so it is valid to
reuse verbatim here). Writes a compact `.npz` (float32 vectors + parallel
chunk_rowid/note_rowid arrays + measured encode_seconds) for the brute-force
cosine searcher used by `eval/int8_ab.py` — same contract as
`eval/bge_m3_encode_scope.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", required=True, help="bge_m3_scope_corpus.py-format output json")
    ap.add_argument("--model-dir", required=True,
                    help="snapshot dir (fp32: HF cache root or onnx/model.onnx dir; "
                         "int8: dir with onnx/model_int8.onnx + tokenizer.json)")
    ap.add_argument("--quant", default="int8", choices=["fp32", "int8"],
                    help="which OnnxEmbedder weights to load (default int8; pass "
                         "fp32 to encode the SAME scope with the production weights "
                         "for a controlled per-chunk encode-throughput comparison)")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--out", required=True, help="output .npz path")
    args = ap.parse_args()

    import os

    os.environ.setdefault("BRAIN_REQUIRE_REAL_EMBEDDER", "1")
    from brain.embed import OnnxEmbedder

    scope = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    chunks = scope["chunks"]
    texts = [c["text"] for c in chunks]
    chunk_rowids = np.array([c["chunk_rowid"] for c in chunks], dtype=np.int64)
    note_rowids = np.array([c["note_rowid"] for c in chunks], dtype=np.int64)
    print(f"encoding {len(texts)} scoped chunks with {args.quant} e5-small "
          f"(batch={args.batch_size}) ...", flush=True)

    emb = OnnxEmbedder(local_dir=args.model_dir, quantization=args.quant)
    print(f"embedder: model_id={emb.model_id} dim={emb.dim} quant={emb.quantization}")

    t0 = time.time()
    vecs: list[list[float]] = []
    n = len(texts)
    bs = args.batch_size
    for i in range(0, n, bs):
        batch = texts[i : i + bs]
        vecs.extend(emb.embed_batch(batch, is_query=False))
        done = min(i + bs, n)
        if done % (bs * 10) == 0 or done == n:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (n - done) / rate if rate > 0 else float("nan")
            print(f"  {done}/{n} encoded, {elapsed:.0f}s elapsed, "
                  f"{rate:.2f} chunks/s, ETA {eta:.0f}s", flush=True)

    mat = np.array(vecs, dtype=np.float32)
    total_s = time.time() - t0
    print(f"done: {n} chunks in {total_s:.1f}s ({total_s/n*1000:.1f} ms/chunk)")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        args.out, vectors=mat, chunk_rowids=chunk_rowids, note_rowids=note_rowids,
        encode_seconds=np.array([total_s]),
    )
    print(f"wrote {args.out} (vectors shape={mat.shape})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
