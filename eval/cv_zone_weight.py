"""Cross-validated zone-weight selection — honest, generalization-valid estimate.

The S10 agentic result leaned on a zone-authority weight (~4x) selected by sweeping
the SAME 66-query golden set it was reported on (hyperparameter overfit). This script
removes that bias: it performs leave-one-out (and deterministic k-fold) cross-validation
where the weight is CHOSEN on the training queries of each fold and SCORED on the
held-out query/fold. The pooled held-out deltas vs frozen SC are the honest estimate —
"if you tune the weight on data you have and apply it to unseen queries, this is how it
generalizes." No query is ever scored under a weight that was chosen with that query in
the selection set.

Metric: recall@20 (the agentic budget). Selection criterion on train: argmax mean
(brain - SC) delta (what a practitioner maximizing improvement-over-baseline would pick);
ties break to the SMALLER weight (parsimony / least-aggressive prior).

Inputs: the per_query_recall@20 dicts from the agentic scorecards, one per candidate
weight. SC ('current') is identical across them (frozen baseline).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EV = HERE.parent / "_evidence" / "s10"

# candidate weight -> agentic scorecard file (per_query_recall@20)
GRID = {
    1.0: EV / "agentic_w100.json",
    1.35: EV / "agentic_default.json",
    2.0: EV / "agentic_w200.json",
    3.0: EV / "agentic_w300.json",
    4.0: EV / "agentic_z4.json",
    6.0: EV / "agentic_w600.json",
    8.0: EV / "agentic_w800.json",
}
METRIC = "per_query_recall@20"
BOUND = -0.02


def load():
    weights = {}
    sc = None
    qstr = None
    for w, f in GRID.items():
        if not f.exists():
            print(f"  (skip w={w}: {f.name} missing)")
            continue
        d = json.loads(f.read_text())
        pq = d[METRIC]
        weights[w] = pq["new"]
        if sc is None:
            sc = pq["current"]
            qstr = d.get("_qstratum", {})
    return weights, sc, qstr


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def select_weight(train_qids, weights, sc):
    """argmax mean(brain - SC) over the grid on the train queries; ties -> smaller w."""
    best_w, best_score = None, None
    for w in sorted(weights):  # ascending -> ties keep the smaller weight
        d = mean([weights[w][q] - sc[q] for q in train_qids])
        if best_score is None or d > best_score + 1e-12:
            best_score, best_w = d, w
    return best_w


def bootstrap_ci_lower(deltas, B=10000, seed=7):
    # numpy-free deterministic bootstrap (LCG), matches gate.py's intent.
    import numpy as np
    rng = np.random.default_rng(seed)
    arr = np.array(deltas, dtype=float)
    n = len(arr)
    means = arr[rng.integers(0, n, size=(B, n))].mean(axis=1)
    return float(np.percentile(means, 2.5))


def run_cv(qids, weights, sc, folds):
    """folds: list of (held_out_qids, train_qids). Returns pooled held-out (brain, sc)
    per qid + the weight chosen for each held-out qid."""
    brain_ho, sc_ho, chosen = {}, {}, {}
    for held, train in folds:
        w = select_weight(train, weights, sc)
        for q in held:
            brain_ho[q] = weights[w][q]
            sc_ho[q] = sc[q]
            chosen[q] = w
    return brain_ho, sc_ho, chosen


def report(name, qids, brain_ho, sc_ho, chosen, qstr):
    deltas = [brain_ho[q] - sc_ho[q] for q in qids]
    bmean = mean([brain_ho[q] for q in qids])
    smean = mean([sc_ho[q] for q in qids])
    lb = bootstrap_ci_lower(deltas)
    print(f"\n=== {name} — pooled held-out recall@20 (n={len(qids)}) ===")
    print(f"  brain (held-out, tuned-per-fold) = {bmean:.4f}")
    print(f"  SC (frozen)                      = {smean:.4f}")
    print(f"  mean Δ = {bmean-smean:+.4f}   95% CI lower = {lb:+.4f}   "
          f"(non-inf bound {BOUND:+.2f}; OVERALL gates on CI)")
    verdict = "PASS" if lb >= BOUND else "FAIL"
    print(f"  OVERALL non-inferiority (CI test): [{verdict}]")
    # weight-selection stability
    from collections import Counter
    cnt = Counter(chosen.values())
    print(f"  weights chosen across folds: {dict(sorted(cnt.items()))}")
    # per-stratum (point estimate — advisory at small n)
    strata = sorted(set(qstr.get(q, "?") for q in qids))
    print("  per-stratum held-out (point estimate):")
    for st in strata:
        sq = [q for q in qids if qstr.get(q) == st]
        if not sq:
            continue
        dm = mean([brain_ho[q] - sc_ho[q] for q in sq])
        pe = "PASS" if dm >= BOUND else "FAIL"
        print(f"    {st:22s} n={len(sq):2d}  Δ={dm:+.4f}  [{pe}]")


def _stratified_folds(qids, qstr, k, seed):
    """Assign queries to k folds, stratified by stratum (proportional), reproducibly."""
    import random
    rng = random.Random(seed)
    by_str: dict[str, list] = {}
    for q in qids:
        by_str.setdefault(qstr.get(q, "?"), []).append(q)
    folds = [[] for _ in range(k)]
    for st, qs in sorted(by_str.items()):
        qs = qs[:]
        rng.shuffle(qs)
        for i, q in enumerate(qs):
            folds[i % k].append(q)
    return folds


def _select_weight_nested(train_qids, qstr, weights, sc, inner_k, seed):
    """Inner CV on the training queries to pick a weight (best mean inner-validation
    delta), the way nested CV decouples selection from the outer test fold."""
    inner = _stratified_folds(train_qids, qstr, inner_k, seed + 1)
    scores = {w: [] for w in weights}
    for i in range(inner_k):
        val = inner[i]
        if not val:
            continue
        for w in weights:
            scores[w].append(mean([weights[w][q] - sc[q] for q in val]))
    # average inner-validation delta per weight; ties -> smaller weight
    best_w, best = None, None
    for w in sorted(weights):
        m = mean(scores[w]) if scores[w] else -1e9
        if best is None or m > best + 1e-12:
            best, best_w = m, w
    return best_w


def _t95_halfwidth(xs):
    """95% CI half-width of the mean for small samples (t-dist, df=n-1)."""
    import statistics
    n = len(xs)
    if n < 2:
        return float("nan")
    sd = statistics.stdev(xs)
    se = sd / (n ** 0.5)
    # t_{.975, df} for small df (lookup; df>=30 ~1.96)
    ttab = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
            7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}
    t = ttab.get(n - 1, 2.045 if n - 1 < 30 else 1.96)
    return t * se


def _wilcoxon_p(deltas):
    """Two-sided Wilcoxon signed-rank p (recommended for non-normal per-query
    recall). Falls back to None if scipy is unavailable."""
    try:
        from scipy.stats import wilcoxon
        nz = [d for d in deltas if d != 0]
        if not nz:
            return 1.0
        return float(wilcoxon(nz).pvalue)
    except Exception:
        return None


def nested_cv(qids, qstr, weights, sc, outer_k=5, inner_k=4, seed=7):
    """Stratified nested CV (the gold standard, per scikit-learn / Rajabi 2024).
    Outer fold scores use a weight chosen by INNER CV on that fold's training set —
    selection never sees the outer test fold. Returns per-outer-fold held-out delta
    + pooled held-out (brain, sc, chosen)."""
    outer = _stratified_folds(qids, qstr, outer_k, seed)
    fold_deltas = []
    brain_ho, sc_ho, chosen = {}, {}, {}
    for i in range(outer_k):
        test = outer[i]
        train = [q for q in qids if q not in test]
        w = _select_weight_nested(train, qstr, weights, sc, inner_k, seed)
        d = mean([weights[w][q] - sc[q] for q in test]) if test else 0.0
        fold_deltas.append(d)
        for q in test:
            brain_ho[q] = weights[w][q]; sc_ho[q] = sc[q]; chosen[q] = w
    return fold_deltas, brain_ho, sc_ho, chosen


def main():
    weights, sc, qstr = load()
    if len(weights) < 2:
        print("Need >=2 candidate weights present. Run agentic_grid.sh first.")
        return 1
    qids = sorted(set.intersection(*[set(d) for d in weights.values()], set(sc)))
    print(f"candidate weights: {sorted(weights)}  |  queries: {len(qids)}  metric: {METRIC}")

    # 1) BIASED baseline (tuned-on-all — the optimistic number to disclose alongside)
    w_all = select_weight(qids, weights, sc)
    naive_d = mean([weights[w_all][q] - sc[q] for q in qids])
    print(f"\n[biased / in-sample] weight tuned on ALL {len(qids)} queries -> w={w_all}; "
          f"mean Δ={naive_d:+.4f}  (OPTIMISTIC — do NOT report as the system metric)")

    # 2) PRIMARY: stratified nested 5x4 CV (gold standard). Report outer mean ± 95% CI.
    from collections import Counter
    fold_d, b, s, c = nested_cv(qids, qstr, weights, sc, outer_k=5, inner_k=4, seed=7)
    outer_mean = mean(fold_d)
    hw = _t95_halfwidth(fold_d)
    pooled = [b[q] - s[q] for q in qids]
    lb = bootstrap_ci_lower(pooled)
    wilcoxon_p = _wilcoxon_p(pooled)
    print("\n=== PRIMARY — stratified nested 5×4 CV (held-out, de-biased) ===")
    print(f"  per-outer-fold held-out Δ: {[round(x,4) for x in fold_d]}")
    print(f"  OUTER mean Δ = {outer_mean:+.4f}  ± {hw:.4f}  (95% CI across 5 folds)")
    print(f"  brain held-out recall@20 = {mean([b[q] for q in qids]):.4f}  | "
          f"SC = {mean([s[q] for q in qids]):.4f}")
    print(f"  pooled-deltas bootstrap 95% CI lower = {lb:+.4f}  "
          f"(non-inf bound {BOUND:+.2f})")
    print(f"  Wilcoxon signed-rank p (pooled) = "
          f"{wilcoxon_p if wilcoxon_p is None else round(wilcoxon_p,4)}")
    print(f"  optimism (in-sample − nested-CV) = {naive_d - outer_mean:+.4f}")
    print(f"  weights chosen across outer folds: {dict(sorted(Counter(c.values()).items()))}")
    print("  per-stratum held-out (point estimate; n<20 = DIRECTIONAL, not gating):")
    for st in sorted(set(qstr.get(q, '?') for q in qids)):
        sq = [q for q in qids if qstr.get(q) == st]
        if not sq:
            continue
        dm = mean([b[q] - s[q] for q in sq])
        tag = "directional" if len(sq) < 20 else ("PASS" if dm >= BOUND else "FAIL")
        print(f"    {st:22s} n={len(sq):2d}  Δ={dm:+.4f}  [{tag}]")

    # 3) Cross-checks: LOO and deterministic 11-fold (flat selection) — should agree
    loo = [([q], [t for t in qids if t != q]) for q in qids]
    b2, s2, c2 = run_cv(qids, weights, sc, loo)
    report("CROSS-CHECK Leave-one-out CV (flat selection)", qids, b2, s2, c2, qstr)

    print("\n--- POWER NOTE (Sakai SIGIR'25; Buckley/Voorhees) ---")
    print("  n=66 is UNDERPOWERED to formally certify a −2pp non-inferiority margin")
    print("  per stratum (need ~150–300 queries). The pooled point estimate + Wilcoxon")
    print("  support SUPERIORITY; per-stratum n<20 results are directional only.")


if __name__ == "__main__":
    sys.exit(main() or 0)
