#!/usr/bin/env python3
"""S09 (PF-01) — warm/steady-state query-latency re-measure for the int8-vs-fp32
e5-small A/B, using the IDENTICAL methodology as `eval/warm_latency.py` (S10
LV-02) and `eval/sc_latency.py`: one throwaway warmup query excluded, then the
SAME 58 live-golden queries, `hybrid_search(k=20, rerank=False)`, per-query
wall-time. A thin, output-path-parameterized fork of `warm_latency.py` so this
session's runs land under `_evidence/pt-bench/` WITHOUT overwriting the S10
cutover baseline (`_evidence/cutover-s10/warm-latency.json`).

Which embedder (fp32 vs int8) is loaded is controlled ENTIRELY by env vars at
the call site (`$BRAIN_EMBEDDER=onnx`, `$BRAIN_EMBED_QUANT=fp32|int8`,
`$BRAIN_MODEL_CACHE=<snapshot dir for that arm>`) — this script does not know
or care which arm it is measuring; that keeps the two runs byte-identical in
every other respect (H4-style: only the embedder backing changes).

METHODOLOGY NOTE (inherited from warm_latency.py): run ALONE — a concurrent
CPU-heavy job inflates the numbers. `--label` is embedded in the output JSON
for provenance; it does not affect the measurement.

Usage: python3 eval/int8_warm_latency.py <brain-vault-root> <golden-set.json> \
           --out <path> --label <fp32|int8>
Needs BRAIN_EMBEDDER=onnx + BRAIN_MODEL_CACHE + BRAIN_INDEX_DIR (+ optionally
BRAIN_EMBED_QUANT=int8).
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "src")
from brain.core import BrainCore  # noqa: E402


def pctl(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def main() -> int:
    import time

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vault")
    ap.add_argument("golden")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", required=True, help="arm label, e.g. fp32 | int8")
    args = ap.parse_args()

    golden = json.load(open(args.golden))
    queries = [q["text"] for q in golden["queries"]]
    core = BrainCore(vault=args.vault)
    embedder_model_id = core.index.embedder.model_id
    embedder_quant = getattr(core.index.embedder, "quantization", "n/a")

    # Explicit warmup: one throwaway query to load the ONNX session + warm caches.
    _ = core.hybrid_search("warmup query to load the model", k=20, rerank=False)

    lat = []
    for t in queries:
        t0 = time.perf_counter()
        core.hybrid_search(t, k=20, rerank=False)
        lat.append((time.perf_counter() - t0) * 1000.0)

    out = {
        "session": "s09", "item": "pf-01",
        "label": args.label,
        "embedder_model_id": embedder_model_id,
        "embedder_quantization": embedder_quant,
        "config": "hybrid (shipped default, rerank off)",
        "warmup_excluded": True,
        "n_queries": len(lat),
        "p50_ms": pctl(lat, 0.50),
        "p95_ms": pctl(lat, 0.95),
        "min_ms": round(min(lat), 2),
        "max_ms": round(max(lat), 2),
        "note": "Steady-state: one throwaway warmup query run BEFORE timing to exclude "
                "one-time ONNX session load + first-query index warmup. SAME index/vault "
                "and SAME 58 live-golden queries as the fp32 arm — only the query-side "
                "embedder differs (see embedder_model_id/embedder_quantization). Corpus "
                "(document) vectors are UNCHANGED fp32 in every run here — this isolates "
                "the query-embedding-side latency effect of quantization; it does not "
                "measure a full int8-re-embedded corpus (see docs/eval-bench/int8-latency.md).",
    }
    json.dump(out, open(args.out, "w"), indent=2)
    print(f"WARM LATENCY [{args.label}]: {json.dumps(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
