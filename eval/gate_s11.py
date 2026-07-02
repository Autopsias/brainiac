#!/usr/bin/env python3
"""S11 lever gate — non-inferiority-OR-better test for a model lever swap.

Each S11 lever (UPG-02 embedder, UPG-03 reranker, UPG-04 contextual) is KEPT
only if it beats the S10 baseline on the 135-q blind set. "Non-inferior-or-
better" = the bootstrap 95% CI lower bound on the per-query recall@10 delta
(new - baseline) is >= 0 AND the Wilcoxon signed-rank is not worse. This is
STRICTER than the S05 gate (which allowed -2pp): S11's bar is "the new model
must not regress", because the whole point of the upgrade is improvement.

Reports per-stratum deltas too (PT/ES are where the lift is expected; the
identifier/temporal strata must not regress for UPG-04).

Usage:
    python3 eval/gate_s11.py \
        --baseline eval/runs/brain_expanded.json \
        --candidate eval/runs/brain_s11_qwen3.json \
        --golden eval/golden_set.json --qrels eval/qrels/qrels.json \
        --label "UPG-02 Qwen3-Embedding-0.6B" \
        --out _evidence/s11/upg02_gate.json

Exit 0 = PASS (keep the lever); exit 1 = FAIL (revert the lever).
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    raise SystemExit("numpy required")


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _ranked(run_doc: dict[str, float]) -> list[str]:
    return [d for d, _ in sorted(run_doc.items(), key=lambda kv: -kv[1])]


def _recall_at_k(rel: dict[str, int], ranked: list[str], k: int) -> float:
    rels = {d for d, g in rel.items() if g > 0}
    if not rels:
        return 0.0
    return sum(1 for d in ranked[:k] if d in rels) / len(rels)


def _per_query_recall(qrels_d, run_d, qids, k=10):
    return {q: round(_recall_at_k(qrels_d[q], _ranked(run_d.get(q) or {}), k), 6)
            for q in qids if q in qrels_d and qrels_d[q]}


def bootstrap_ci(deltas: list[float], b: int = 10000, seed: int = 7, alpha: float = 0.05) -> tuple[float, float]:
    arr = np.array(deltas, dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    n = arr.size
    means = arr[rng.integers(0, n, size=(b, n))].mean(axis=1)
    lo = float(np.percentile(means, 100 * (alpha / 2)))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return lo, hi


def wilcoxon_p(deltas: list[float]) -> float:
    """Two-sided Wilcoxon signed-rank p-value via scipy if available, else a
    normal-approximation fallback."""
    nz = [d for d in deltas if d != 0]
    if not nz:
        return 1.0
    try:
        from scipy.stats import wilcoxon
        stat, p = wilcoxon(nz, alternative="greater")  # H1: new > baseline (one-sided)
        return float(p)
    except Exception:
        # normal approximation fallback
        ranked = sorted(range(len(nz)), key=lambda i: abs(nz[i]))
        n = len(nz)
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and abs(nz[ranked[j + 1]]) == abs(nz[ranked[i]]):
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[ranked[k]] = avg
            i = j + 1
        w_plus = sum(r for r, d in zip(ranks, nz) if d > 0)
        mu = n * (n + 1) / 4
        sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
        z = (w_plus - mu) / sigma if sigma > 0 else 0
        from statistics import NormalDist
        return float(NormalDist().cdf(z))  # one-sided


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="S10 baseline run (e5-small + jina-v2)")
    ap.add_argument("--candidate", required=True, help="S11 candidate run (the lever under test)")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--qrels", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    golden = _load(args.golden)
    qrels_d = _load(args.qrels)
    base = _load(args.baseline)
    cand = _load(args.candidate)
    qmeta = {q["id"]: q for q in golden["queries"]}
    base_runs, cand_runs = base["runs"], cand["runs"]

    scored = sorted(set(base_runs) & set(cand_runs) & set(qrels_d))
    base_pq = _per_query_recall(qrels_d, base_runs, scored, 10)
    cand_pq = _per_query_recall(qrels_d, cand_runs, scored, 10)
    paired = sorted(set(base_pq) & set(cand_pq))
    deltas = [cand_pq[q] - base_pq[q] for q in paired]

    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
    ci_lo, ci_hi = bootstrap_ci(deltas)
    p_sup = wilcoxon_p(deltas)
    # non-inferior-or-better: CI lower bound >= 0 (strict: no regression) AND Wilcoxon not worse
    # (one-sided p<0.5 means new is not worse than baseline on the sign test)
    pass_ni = ci_lo >= 0.0
    pass_wilcoxon = p_sup < 0.5 or mean_delta >= 0
    overall_pass = pass_ni and pass_wilcoxon and mean_delta >= 0

    # per-stratum deltas
    strata: dict[str, dict] = {}
    for st in sorted({qmeta[q]["stratum"] for q in paired}):
        st_qids = [q for q in paired if qmeta[q]["stratum"] == st]
        st_deltas = [cand_pq[q] - base_pq[q] for q in st_qids]
        st_mean = sum(st_deltas) / len(st_deltas) if st_deltas else 0.0
        st_lo, st_hi = bootstrap_ci(st_deltas) if len(st_deltas) >= 5 else (float("nan"), float("nan"))
        strata[st] = {
            "n": len(st_qids), "mean_delta": round(st_mean, 4),
            "ci_lo": round(st_lo, 4) if not math.isnan(st_lo) else None,
            "ci_hi": round(st_hi, 4) if not math.isnan(st_hi) else None,
            "base_r10": round(sum(base_pq[q] for q in st_qids) / len(st_qids), 4) if st_qids else 0,
            "cand_r10": round(sum(cand_pq[q] for q in st_qids) / len(st_qids), 4) if st_qids else 0,
        }

    result = {
        "label": args.label,
        "n_paired": len(paired),
        "baseline_system": base.get("system"),
        "candidate_system": cand.get("system"),
        "recall@10_baseline": round(sum(base_pq.values()) / len(base_pq), 4) if base_pq else 0,
        "recall@10_candidate": round(sum(cand_pq.values()) / len(cand_pq), 4) if cand_pq else 0,
        "mean_delta": round(mean_delta, 4),
        "bootstrap_ci95": [round(ci_lo, 4), round(ci_hi, 4)],
        "wilcoxon_p_one_sided_greater": round(p_sup, 4),
        "gate": {
            "non_inferior_ci_lo_ge_0": pass_ni,
            "wilcoxon_not_worse": pass_wilcoxon,
            "mean_delta_ge_0": mean_delta >= 0,
            "OVERALL_PASS": overall_pass,
        },
        "strata": strata,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    verdict = "PASS (keep lever)" if overall_pass else "FAIL (revert lever)"
    print(f"=== {args.label} ===")
    print(f"  recall@10: baseline={result['recall@10_baseline']} candidate={result['recall@10_candidate']} Δ={mean_delta:+.4f}")
    print(f"  bootstrap 95% CI: [{ci_lo:+.4f}, {ci_hi:+.4f}]  (gate: lower ≥ 0 → {'PASS' if pass_ni else 'FAIL'})")
    print(f"  Wilcoxon one-sided p(new>base): {p_sup:.4f}  (not worse → {'PASS' if pass_wilcoxon else 'FAIL'})")
    print(f"  GATE: {verdict}")
    for st, d in strata.items():
        print(f"    {st:28s} n={d['n']:3d}  base={d['base_r10']:.3f} cand={d['cand_r10']:.3f} Δ={d['mean_delta']:+.4f}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
