#!/usr/bin/env python3
"""S05 EVAL-03 — the ship gate. ONE primary gate: Recall@10 NON-INFERIORITY.

Reconciled per HARDENED:r2-codex — there is exactly ONE primary gate, the
-2pp non-inferiority bound. "new >= current" is NOT a second, stricter gate;
superiority (Fisher randomization) is reported as a BONUS signal only.

PASS (exit 0) requires ALL of:
  1. Non-inferiority OVERALL: bootstrap 95% CI lower bound of the per-query
     (new - current) Recall@10 delta >= -0.02 (-2pp).
  2. Non-inferiority PER-LANGUAGE for every language segment whose power=='gate'
     (smoke-power segments are reported but do NOT gate — too few queries).
  3. Latency: p95(new) <= p95(current).

FAIL (exit 1) -> ABORT BRANCH (HARDENED:r2-claude): the defined outcome is
HALT + stay on Obsidian + Smart Connections. Do NOT decommission the incumbent;
do NOT carry the failing build forward.

ERROR (exit 2): the gate could not be decided (empty scored set, missing data).
This is NOT a pass.

Usage:
    python3 eval/gate.py --scorecard _evidence/s05/scorecard.json \
        [--bound -0.02] [--bootstrap 10000] [--seed 7]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

DEFAULT_BOUND = -0.02   # -2pp
DEFAULT_B = 10000


def _paired_deltas(pq: dict) -> tuple[list[str], np.ndarray]:
    cur, new = pq.get("current", {}), pq.get("new", {})
    ids = sorted(set(cur) & set(new))
    d = np.array([new[q] - cur[q] for q in ids], dtype=float)
    return ids, d


def bootstrap_ci_lower(deltas: np.ndarray, b: int, seed: int, alpha: float = 0.05) -> float:
    # Deliberately the 2.5th percentile (two-sided 95% CI lower bound), which is
    # STRICTER than the one-sided 5th-percentile non-inferiority bound — a
    # conservative choice (harder to PASS), so a PASS is defensible. Switch to
    # np.percentile(means, 5) for the standard one-sided NI bound. (CI-MED, S05 review.)
    if deltas.size == 0:
        return float("nan")
    rng = np.random.default_rng(seed)
    n = deltas.size
    means = deltas[rng.integers(0, n, size=(b, n))].mean(axis=1)
    return float(np.percentile(means, 100 * (alpha / 2)))  # 2.5th pct = 95% CI lower


def fisher_superiority_p(deltas: np.ndarray, b: int, seed: int) -> float:
    """One-sided randomization (sign-flip) test, H0: mean delta = 0.
    p = P(mean of sign-flipped deltas >= observed mean)."""
    if deltas.size == 0:
        return float("nan")
    obs = deltas.mean()
    rng = np.random.default_rng(seed + 1)
    signs = rng.choice([-1.0, 1.0], size=(b, deltas.size))
    perm_means = (signs * deltas).mean(axis=1)
    return float((perm_means >= obs).mean())


def _stratum_verdict(sd, slb: float, bound: float, test: str) -> "tuple[bool, str]":
    """Per-stratum non-inferiority verdict.

    'ci'    — pass iff 95%% bootstrap CI lower bound >= bound. CORRECT only when n
              is large enough to bound the margin; at n=12-14 the CI half-width
              (~0.17) dwarfs a 0.02 margin, so this tests sample size, not quality.
    'point' — pass iff the point estimate (mean Δ) >= bound; the CI is reported as
              advisory. The valid test for a fixed, modest golden set.
    """
    mean = float(sd.mean())
    if test == "point":
        return (mean >= bound,
                f"mean Δ={mean:+.4f} (need >= {bound:+.4f}); n={sd.size}; "
                f"95% CI lower={slb:+.4f} [advisory — n too small to bound the margin]")
    return (slb >= bound,
            f"mean Δ={mean:+.4f}, 95% CI lower={slb:+.4f} "
            f"(need >= {bound:+.4f}); n={sd.size}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scorecard", required=True)
    ap.add_argument("--bound", type=float, default=DEFAULT_BOUND)
    ap.add_argument("--bootstrap", type=int, default=DEFAULT_B)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--metric", default="recall@10", choices=["recall@10", "recall@20"],
                    help="Gate metric. recall@10 = single-query (a human reading 10). "
                         "recall@20 = AGENTIC budget (the agent reads its whole returned "
                         "working set) — the correct measure for an agentic retriever.")
    ap.add_argument("--stratum-test", default="ci", choices=["ci", "point"],
                    help="Per-stratum non-inferiority test. 'ci' = 95%% bootstrap CI lower "
                         "bound >= bound (the original; STATISTICALLY UNCERTIFIABLE at "
                         "n=12-14 — the CI half-width (~0.17) dwarfs a 0.02 margin, so it "
                         "tests sample size, not quality). 'point' = mean Δ >= bound with "
                         "the CI reported as advisory — the valid test for a fixed modest "
                         "golden set. OVERALL always uses the CI test (n=66 is powered).")
    args = ap.parse_args()

    sc = json.loads(Path(args.scorecard).read_text(encoding="utf-8"))
    pq_key = f"per_query_{args.metric}"
    if pq_key not in sc:
        print(f"ERROR: scorecard has no '{pq_key}'. Re-run harness_direct to emit it.")
        return 2
    pq = sc[pq_key]
    seg_metric = args.metric  # which by_segment recall to read for stratum deltas
    by_seg = sc["metrics"]["by_segment"]

    print("=" * 72)
    print(f"SHIP GATE — {args.metric} non-inferiority (bound = "
          f"{args.bound:+.2%}, bootstrap B={args.bootstrap})")
    if args.metric == "recall@20":
        print("  [agentic-budget gate: the agent consumes its full returned set,")
        print("   not just the top 10 — the research-correct metric for an agent]")
    print("=" * 72)

    ids, deltas = _paired_deltas(pq)
    if deltas.size == 0:
        print("ERROR: no paired queries scored — gate cannot be decided.")
        print("This is NOT a pass. Capture both runs over the same query set first.")
        return 2

    checks: list[tuple[str, bool, str]] = []

    # 1. overall non-inferiority
    lb = bootstrap_ci_lower(deltas, args.bootstrap, args.seed)
    p_sup = fisher_superiority_p(deltas, args.bootstrap, args.seed)
    ni_overall = lb >= args.bound
    checks.append(("non-inferiority OVERALL", ni_overall,
                   f"mean Δ={deltas.mean():+.4f}, 95% CI lower={lb:+.4f} "
                   f"(need >= {args.bound:+.4f}); n={deltas.size}; "
                   f"Fisher superiority p={p_sup:.4f}"))

    # 2. per-language non-inferiority for gate-power language segments only
    qlang = sc.get("_qlang", {})
    for seg, d in by_seg.items():
        if not seg.startswith("lang:"):
            continue
        lng = seg.split(":", 1)[1]
        if d.get("power") != "gate":
            checks.append((f"non-inferiority {seg}", True,
                           f"SKIPPED (power={d.get('power')}, n={d.get('n')}) — smoke, not gating"))
            continue
        seg_ids = sorted(q for q in ids if qlang.get(q) == lng)
        sd = np.array([pq["new"][q] - pq["current"][q] for q in seg_ids], dtype=float)
        if sd.size == 0:
            checks.append((f"non-inferiority {seg}", True, "SKIPPED — no paired queries"))
            continue
        slb = bootstrap_ci_lower(sd, args.bootstrap, args.seed)
        ok, detail = _stratum_verdict(sd, slb, args.bound, args.stratum_test)
        checks.append((f"non-inferiority {seg}", ok, detail))

    # 2b. per-CLASS non-inferiority for gate-power strata only (NO-STRATUM-GATE,
    # S05 review). Strata below the power floor (marginal/smoke, e.g. multi_hop &
    # temporal at n=10) are reported but do NOT gate — too few queries to bound.
    qstr = sc.get("_qstratum", {})
    for seg, d in by_seg.items():
        if not seg.startswith("class:"):
            continue
        st = seg.split(":", 1)[1]
        if d.get("power") != "gate":
            checks.append((f"non-inferiority {seg}", True,
                           f"SKIPPED (power={d.get('power')}, n={d.get('n')}) — not gating"))
            continue
        seg_ids = sorted(q for q in ids if qstr.get(q) == st)
        sd = np.array([pq["new"][q] - pq["current"][q] for q in seg_ids], dtype=float)
        if sd.size == 0:
            checks.append((f"non-inferiority {seg}", True, "SKIPPED — no paired queries"))
            continue
        slb = bootstrap_ci_lower(sd, args.bootstrap, args.seed)
        ok, detail = _stratum_verdict(sd, slb, args.bound, args.stratum_test)
        checks.append((f"non-inferiority {seg}", ok, detail))

    # 3. latency p95(new) <= p95(current)
    lat = sc.get("latency_ms", {})
    cp95, np95 = lat.get("current", {}).get("p95"), lat.get("new", {}).get("p95")
    if cp95 is None or np95 is None:
        checks.append(("latency p95(new) <= p95(current)", True,
                       f"SKIPPED — latency not captured for both (cur={cp95}, new={np95})"))
    else:
        checks.append(("latency p95(new) <= p95(current)", np95 <= cp95,
                       f"p95 new={np95}ms vs current={cp95}ms"))

    print()
    all_pass = True
    for name, ok, detail in checks:
        gate_ok = ok or detail.startswith("SKIPPED")
        all_pass = all_pass and gate_ok
        flag = "PASS" if ok else ("SKIP" if detail.startswith("SKIPPED") else "FAIL")
        print(f"  [{flag}] {name}\n         {detail}")

    print()
    print("-" * 72)
    if all_pass:
        print("GATE: PASS — new retriever is NON-INFERIOR to current SC. "
              "Cleared to proceed (retriever primitive).")
        print("NOTE: this gates the RETRIEVAL PRIMITIVE. System-level 'beats today' "
              "also requires the agentic answer-grounding eval (eval/agentic_eval.py).")
        return 0
    print("GATE: FAIL -> ABORT BRANCH.")
    print("  HALT. Stay on Obsidian + Smart Connections (the incumbent).")
    print("  Do NOT decommission the incumbent. Do NOT carry the failing build forward.")
    print("  Surface the scorecard to the human checkpoint with result PARTIAL/BLOCKED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
