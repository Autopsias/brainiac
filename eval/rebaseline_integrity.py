#!/usr/bin/env python3
"""S10 LV-02 — integrity near-dup band re-baseline for the e5-small ONNX embedder.

Runs BrainIndex.near_dup over a migrated corpus at a low score floor (0.80) to
capture the full pair-score distribution, then buckets it against the old
Smart-Connections bands (error>=0.97, review 0.95-0.97) so the integrity-scan
ritual's thresholds can be re-set for the new embedder (task-disposition row 3).

Emits _evidence/<session>/integrity-rebaseline.json. All scores are coerced
float()  -> the backend/embedder return numpy float32 which is not JSON
serializable (S10 fix).

Usage: python3 eval/rebaseline_integrity.py <brain-vault-root>
Needs BRAIN_EMBEDDER=onnx + BRAIN_MODEL_CACHE (offline e5-small) + BRAIN_INDEX_DIR.
"""
import json, sys, time
sys.path.insert(0, "src")
from brain.core import BrainCore

def f(x):  # coerce numpy float32/64 -> python float for JSON
    return float(x)

t0 = time.time()
core = BrainCore(vault=sys.argv[1])
idx = core.index
pairs = idx.near_dup(min_score=0.80, k=5)
elapsed = round(time.time() - t0, 1)

def band(s):
    if s >= 0.97: return "error"
    if s >= 0.95: return "review"
    if s >= 0.90: return "watch"
    return "below"

from collections import Counter
bands = Counter(band(f(p["score"])) for p in pairs)
hist = Counter(round(f(p["score"]), 2) for p in pairs)

out = {
    "embedder": idx.embedder.model_id,
    "embed_dim": int(idx.embedder.dim),
    "backend": type(idx.backend).__name__,
    "vault": sys.argv[1],
    "scan_floor_min_score": 0.80,
    "k": 5,
    "total_pairs_ge_0.80": len(pairs),
    "elapsed_s": elapsed,
    "old_sc_bands": {"error_band": 0.97, "review_band": 0.95,
                     "note": "SC e5/bge-residue baseline (integrity-scan output-2026-06-30)"},
    "e5small_onnx_band_counts": {k: int(v) for k, v in bands.items()},
    "score_histogram_0.01": {str(k): int(v) for k, v in sorted(hist.items(), reverse=True)},
    "top_30_pairs": [
        {"score": round(f(p["score"]), 6), "a": p["a"]["path"], "b": p["b"]["path"],
         "a_class": p["a"]["classification"], "b_class": p["b"]["classification"]}
        for p in sorted(pairs, key=lambda x: -f(x["score"]))[:30]
    ],
}
json.dump(out, open("_evidence/cutover-s10/integrity-rebaseline.json", "w"), indent=2, ensure_ascii=False)
print("REBASELINE DONE:", len(pairs), "pairs >=0.80;", dict(bands), "elapsed", elapsed, "s")
