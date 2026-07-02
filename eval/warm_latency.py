#!/usr/bin/env python3
"""S10 LV-02 — steady-state (warm) query-latency re-measure for the shipped
hybrid retriever. Runs one throwaway warmup query BEFORE timing to exclude the
one-time ONNX session load + first-query index warmup (the cold capture's first
query carries that cost). Emits _evidence/<session>/warm-latency.json.

METHODOLOGY NOTE: run this ALONE — a concurrent embedding job (e.g. the
integrity re-baseline) saturates all cores and inflates the numbers (observed:
p95 198ms isolated vs 477ms under contention). Isolate before trusting.

Usage: python3 eval/warm_latency.py <brain-vault-root> <golden-set.json>
Needs BRAIN_EMBEDDER=onnx + BRAIN_MODEL_CACHE + BRAIN_INDEX_DIR.
"""
import json, sys, time
sys.path.insert(0, "src")
from brain.core import BrainCore

golden = json.load(open(sys.argv[2]))
queries = [q["text"] for q in golden["queries"]]
core = BrainCore(vault=sys.argv[1])

# Explicit warmup: one throwaway query to load the ONNX session + warm caches.
_ = core.hybrid_search("warmup query to load the model", k=20, rerank=False)

lat = []
for t in queries:
    t0 = time.perf_counter()
    core.hybrid_search(t, k=20, rerank=False)
    lat.append((time.perf_counter() - t0) * 1000.0)

def pctl(xs, q):
    xs = sorted(xs); k = (len(xs)-1)*q; lo=int(k); hi=min(lo+1,len(xs)-1)
    return round(xs[lo] + (xs[hi]-xs[lo])*(k-lo), 2)

out = {
    "config": "hybrid (shipped default, rerank off)",
    "warmup_excluded": True,
    "n_queries": len(lat),
    "p50_ms": pctl(lat, 0.50),
    "p95_ms": pctl(lat, 0.95),
    "min_ms": round(min(lat), 2),
    "max_ms": round(max(lat), 2),
    "note": "Steady-state: one throwaway warmup query run BEFORE timing to exclude "
            "one-time ONNX session load + first-query index warmup. Compare to the "
            "cold capture (live_new.json) whose first query carries the warmup cost.",
}
json.dump(out, open("_evidence/cutover-s10/warm-latency.json", "w"), indent=2)
print("WARM LATENCY:", json.dumps(out))
