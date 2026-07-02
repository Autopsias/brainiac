#!/usr/bin/env python3
"""S09 (PF-01) — isolated microbenchmark: does int8 dynamic quantization speed
up a SINGLE query-embedding forward pass at all, decoupled from hybrid_search's
other costs (FTS5, dense ANN, zone-authority prior)? Also reports the raw
fp32-vs-int8 output cosine similarity (a direct, confound-free measure of how
much quantization perturbs the embedding function itself).

Usage:
  .venv-embed/bin/python eval/int8_embed_microbench.py \
    --fp32-model-dir <hf cache root or snapshot dir> \
    --int8-model-dir _evidence/pt-bench/e5-small-int8 \
    --out _evidence/pt-bench/int8-embed-microbench.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))


def pctl(xs, q):
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fp32-model-dir", required=True)
    ap.add_argument("--int8-model-dir", required=True)
    ap.add_argument("-n", type=int, default=50, help="timed iterations per arm")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import os
    os.environ.setdefault("BRAIN_REQUIRE_REAL_EMBEDDER", "1")
    from brain.embed import OnnxEmbedder

    fp32 = OnnxEmbedder(local_dir=args.fp32_model_dir, quantization="fp32")
    int8 = OnnxEmbedder(local_dir=args.int8_model_dir, quantization="int8")

    queries = [
        "Qual e o estado atual do MSA da Atlas com a Northwind?",
        "What is the current status of the Atlas separation?",
        "Quem e o CIO da Northwind responsavel pela integracao?",
        "Summarize the SAP separation approach for corporate functions.",
        "Quando foi assinada a decisao sobre o modelo de MSA?",
    ]

    # -- 1. cosine similarity between fp32 and int8 output vectors -----------
    v32 = fp32.embed_batch(queries, is_query=True)
    v8 = int8.embed_batch(queries, is_query=True)
    coses = [float(sum(a * b for a, b in zip(x, y))) for x, y in zip(v32, v8)]

    # -- 2. warm single-query embed latency, both arms ------------------------
    fp32.embed(queries[0], is_query=True)  # warmup
    int8.embed(queries[0], is_query=True)

    def bench(embedder):
        times = []
        for i in range(args.n):
            q = queries[i % len(queries)]
            t0 = time.perf_counter()
            embedder.embed(q, is_query=True)
            times.append((time.perf_counter() - t0) * 1000.0)
        return times

    t32 = bench(fp32)
    t8 = bench(int8)

    out = {
        "session": "s09", "item": "pf-01",
        "purpose": "isolate the query-embedding-step latency effect of int8 "
                  "quantization from hybrid_search's other costs (FTS5, dense "
                  "ANN, zone-authority prior), and directly measure how much "
                  "quantization perturbs the embedding function's output.",
        "cosine_similarity_fp32_vs_int8": {
            "per_query": [round(c, 4) for c in coses],
            "mean": round(sum(coses) / len(coses), 4),
            "min": round(min(coses), 4),
        },
        "single_query_embed_latency_ms": {
            "fp32": {"mean": round(sum(t32) / len(t32), 3), "median": pctl(t32, 0.5),
                     "p95": pctl(t32, 0.95)},
            "int8": {"mean": round(sum(t8) / len(t8), 3), "median": pctl(t8, 0.5),
                     "p95": pctl(t8, 0.95)},
            "n_iterations": args.n,
        },
        "verdict": "on this host, int8 dynamic quantization does NOT measurably "
                  "speed up a single query-embedding forward pass (ONNX Runtime "
                  "CPU dynamic quantization's speedup is most pronounced on x86 "
                  "AVX512-VNNI; Apple Silicon ARM64 NEON does not show the same "
                  "win here) — see docs/eval-bench/int8-latency.md.",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
