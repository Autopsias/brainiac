#!/usr/bin/env python3
"""EM-01 (s06) — BGE-M3 ONNX-CPU speed/feasibility bench (own data, own machine).

Measures: cold model-load time, warm encode throughput (chunks/sec) at
realistic chunk lengths sampled from the REAL live index, peak RSS, and
extrapolates full-corpus (96,568 chunks) rebuild time on this Apple M4 Pro
(12 physical cores) — the same machine class as the corporate CPU fleet this
brain targets. Mirrors `eval/bench_qwen_speed.py`'s methodology.

Usage:
  .venv-embed/bin/python eval/bge_m3_speed_bench.py \
    --model-dir _evidence/pt-bench/bge-m3-model/models--BAAI--bge-m3/snapshots/<hash> \
    --index _workspace/live-vault/.brain/index.sqlite \
    --n-chunks 40 \
    --out _evidence/pt-bench/bge-m3-speed.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bge_m3_embedder as bm  # noqa: E402


def sample_chunks(index_path: str, n: int, seed: int = 20260702) -> list[str]:
    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    rows = conn.execute("SELECT text FROM chunks WHERE text IS NOT NULL").fetchall()
    conn.close()
    texts = [r[0] for r in rows if r[0]]
    rng = random.Random(seed)
    rng.shuffle(texts)
    return texts[:n]


def peak_rss_mb() -> float:
    try:
        import resource

        ru = resource.getrusage(resource.RUSAGE_SELF)
        # macOS ru_maxrss is bytes; Linux is KB.
        val = ru.ru_maxrss
        return val / (1024.0 * 1024.0) if sys.platform == "darwin" else val / 1024.0
    except Exception:
        return -1.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--n-chunks", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--full-corpus-chunks", type=int, default=96568)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    texts = sample_chunks(args.index, args.n_chunks)
    lens = [len(t) for t in texts]
    print(f"sampled {len(texts)} real chunks; char-len mean={sum(lens)/len(lens):.0f} "
          f"min={min(lens)} max={max(lens)}")

    emb = bm.build_embedder(
        args.model_dir, max_length=args.max_length, threads=args.threads,
    )

    # Cold load: first embed() call triggers session+tokenizer construction.
    t0 = time.time()
    emb.embed_batch(texts[:1])
    cold_load_s = time.time() - t0
    print(f"cold model-load (incl. first-batch encode of 1) = {cold_load_s:.2f}s")

    # Warm throughput: fixed batch size, several batches, best-of-N median.
    n_batches = max(1, len(texts) // args.batch_size)
    per_batch_s = []
    total_texts = 0
    t0 = time.time()
    for i in range(n_batches):
        batch = texts[i * args.batch_size : (i + 1) * args.batch_size]
        if not batch:
            continue
        bt0 = time.time()
        emb.embed_batch(batch)
        per_batch_s.append(time.time() - bt0)
        total_texts += len(batch)
    warm_wall_s = time.time() - t0
    ms_per_text = (warm_wall_s / total_texts) * 1000 if total_texts else float("nan")
    chunks_per_sec = total_texts / warm_wall_s if warm_wall_s > 0 else float("nan")

    rss_mb = peak_rss_mb()

    full_build_s = args.full_corpus_chunks / chunks_per_sec if chunks_per_sec > 0 else float("inf")

    out = {
        "session": "s06", "item": "em-01",
        "model": "BAAI/bge-m3", "backend": "onnxruntime (direct, CLS-pool, fp32)",
        "machine": "Apple M4 Pro, 12 physical cores (dev host — same class as corporate CPU fleet)",
        "params_m": 568, "dim": 1024, "onnx_fp32_size_gb": 2.1,
        "max_length_tokens": args.max_length,
        "batch_size": args.batch_size,
        "n_sample_chunks": len(texts),
        "sample_char_len": {"mean": round(sum(lens) / len(lens), 1), "min": min(lens), "max": max(lens)},
        "cold_load_s": round(cold_load_s, 3),
        "warm_ms_per_chunk": round(ms_per_text, 1),
        "warm_chunks_per_sec": round(chunks_per_sec, 3),
        "peak_rss_mb": round(rss_mb, 1) if rss_mb > 0 else None,
        "full_corpus_chunks": args.full_corpus_chunks,
        "extrapolated_full_rebuild": {
            "seconds": round(full_build_s, 1),
            "minutes": round(full_build_s / 60.0, 1),
            "hours": round(full_build_s / 3600.0, 2),
        },
        "per_batch_s_sample": [round(x, 3) for x in per_batch_s[:10]],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
