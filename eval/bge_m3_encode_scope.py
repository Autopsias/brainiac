#!/usr/bin/env python3
"""EM-01 (s06) — encode the scoped chunk corpus with BGE-M3 (dense head only).

Reads `bge_m3_scope_corpus.py`'s output, encodes every chunk's text with
`BgeM3OnnxEmbedder`, and writes a compact `.npz` (float32 vectors + parallel
chunk_rowid/note_rowid arrays) for the brute-force cosine searcher used by
`bge_m3_ab.py`. This is the slow step (measured ~441 ms/chunk warm on this
Apple M4 Pro) — run it once, in the background, and reuse the .npz.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bge_m3_embedder as bm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", required=True, help="bge_m3_scope_corpus.py output json")
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--out", required=True, help="output .npz path")
    args = ap.parse_args()

    scope = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    chunks = scope["chunks"]
    texts = [c["text"] for c in chunks]
    chunk_rowids = np.array([c["chunk_rowid"] for c in chunks], dtype=np.int64)
    note_rowids = np.array([c["note_rowid"] for c in chunks], dtype=np.int64)
    print(f"encoding {len(texts)} scoped chunks with BGE-M3 "
          f"(max_length={args.max_length}, batch={args.batch_size}) ...", flush=True)

    emb = bm.build_embedder(args.model_dir, max_length=args.max_length)

    t0 = time.time()
    vecs: list[list[float]] = []
    n = len(texts)
    bs = args.batch_size
    for i in range(0, n, bs):
        batch = texts[i : i + bs]
        vecs.extend(emb.embed_batch(batch))
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
