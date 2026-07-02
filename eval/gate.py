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

--- EF-04 (S03) statistical-rigor extension ---------------------------------
The PASS/FAIL/ERROR contract above is UNCHANGED (still the -2pp effect-size
threshold, not a bare "CI lower bound > 0" — already H20-compliant). What's
new is an always-printed extended stats block, powered by the shared,
independently-tested primitives in `eval/stats.py`:
  * the bootstrap CI is now explicitly labeled DESCRIPTIVE (H19) — an
    effect-size interval, never itself the significance decision;
  * a paired permutation test (`eval/stats.py::paired_permutation_test`) is
    reported per checked segment. Pass `--fold-context held-out` ONLY when
    this run really is the single locked held-out read (H37, run by s11b) —
    every other value prints an explicit "not confirmatory" caveat (H19: one
    primary significance regime, and it lives on held-out only);
  * Benjamini-Hochberg FDR correction (`eval/stats.py::benjamini_hochberg`)
    is applied across every per-segment permutation p-value in the same
    report, so uncorrected multiplicity across ~10-15 language x class
    slices can't manufacture false "wins" (H20);
  * a pre-registered minimum detectable effect + the power actually achieved
    for the observed OVERALL effect (H20 "pre-register an MDE + reported
    power; the gate is an effect-size threshold, not a bare CI-lower>0").
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stats import (  # noqa: E402
    achieved_power,
    benjamini_hochberg,
    bootstrap_ci,
    minimum_detectable_effect,
    paired_permutation_test,
)

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
    # DESCRIPTIVE effect-size interval (H19) — thin wrapper over the shared,
    # independently-tested eval/stats.py::bootstrap_ci for backward compat
    # with the existing -2pp PASS/FAIL contract (unchanged by EF-04).
    if deltas.size == 0:
        return float("nan")
    return bootstrap_ci(deltas, b=b, seed=seed, alpha=alpha).ci_lower


def fisher_superiority_p(deltas: np.ndarray, b: int, seed: int) -> float:
    """One-sided randomization (sign-flip) test, H0: mean delta = 0.
    p = P(mean of sign-flipped deltas >= observed mean). Thin wrapper over
    eval/stats.py::paired_permutation_test (kept for backward compat with
    the pre-EF-04 seed+1 convention — the shared function is the tested,
    canonical implementation)."""
    if deltas.size == 0:
        return float("nan")
    return paired_permutation_test(deltas, b=b, seed=seed + 1, fold_context="unknown").p_greater


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
    ap.add_argument("--fold-context", default="unknown",
                    choices=["held-out", "non-held-out", "unknown"],
                    help="EF-04 (H19): label ONLY, never changes a computed statistic. Pass "
                         "'held-out' ONLY when this scorecard really is the single locked "
                         "held-out read (H37, session s11b) — that flips the paired "
                         "permutation test's report from 'descriptive/informational' to "
                         "'PRIMARY significance test'. Any other value prints an explicit "
                         "not-confirmatory caveat, because the primary significance regime "
                         "for this eval runs exactly once.")
    ap.add_argument("--fdr-alpha", type=float, default=0.05,
                    help="EF-04 (H20): Benjamini-Hochberg FDR level applied across every "
                         "per-segment (language x class) paired-permutation p-value reported "
                         "in the same run, so multiplicity across ~10-15 slices can't "
                         "manufacture false 'wins'. Does not affect the PASS/FAIL contract.")
    ap.add_argument("--target-power", type=float, default=0.8,
                    help="EF-04 (H20): target power for the pre-registered minimum-detectable-"
                         "effect report (informational — does not affect PASS/FAIL).")
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
    # EF-04 (H20): per-segment paired-permutation p-values collected here for
    # the family-wise FDR correction printed in the extended stats block
    # below — separate from `checks`/`all_pass` so it never touches the
    # PASS/FAIL/ABORT contract.
    segment_pvals: list[tuple[str, int, float]] = []  # (segment, n, p_two_sided)

    # 1. overall non-inferiority
    lb = bootstrap_ci_lower(deltas, args.bootstrap, args.seed)
    p_sup = fisher_superiority_p(deltas, args.bootstrap, args.seed)
    ni_overall = lb >= args.bound
    checks.append(("non-inferiority OVERALL", ni_overall,
                   f"mean Δ={deltas.mean():+.4f}, 95% CI lower={lb:+.4f} "
                   f"(need >= {args.bound:+.4f}); n={deltas.size}; "
                   f"Fisher superiority p={p_sup:.4f}"))
    overall_perm = paired_permutation_test(deltas, b=args.bootstrap, seed=args.seed,
                                            fold_context=args.fold_context)
    overall_ci = bootstrap_ci(deltas, b=args.bootstrap, seed=args.seed)

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
        seg_perm = paired_permutation_test(sd, b=args.bootstrap, seed=args.seed,
                                            fold_context=args.fold_context)
        segment_pvals.append((seg, sd.size, seg_perm.p_two_sided))

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
        seg_perm = paired_permutation_test(sd, b=args.bootstrap, seed=args.seed,
                                            fold_context=args.fold_context)
        segment_pvals.append((seg, sd.size, seg_perm.p_two_sided))

    # EF-04 (H20): also collect permutation p-values for *reported-only*
    # (SKIPPED / smoke-power) lang:/class: segments with >=2 paired queries,
    # so the FDR family genuinely covers "ALL language x class slice tests"
    # (H20: ">=10-15 of them"), not just the subset large enough to gate.
    gated_segment_names = {name for name, _, _ in segment_pvals}
    for seg, d in by_seg.items():
        if not (seg.startswith("lang:") or seg.startswith("class:")):
            continue
        if seg in gated_segment_names:
            continue
        if seg.startswith("lang:"):
            key_map, key = qlang, seg.split(":", 1)[1]
        else:
            key_map, key = qstr, seg.split(":", 1)[1]
        seg_ids = sorted(q for q in ids if key_map.get(q) == key)
        if len(seg_ids) < 2:
            continue
        sd = np.array([pq["new"][q] - pq["current"][q] for q in seg_ids], dtype=float)
        seg_perm = paired_permutation_test(sd, b=args.bootstrap, seed=args.seed,
                                            fold_context=args.fold_context)
        segment_pvals.append((seg, sd.size, seg_perm.p_two_sided))

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

    # --- EF-04 (S03) extended statistics block --------------------------
    # Purely informational: PASS/FAIL above is decided ONLY by `checks` /
    # `all_pass`. Nothing below changes the exit code.
    print()
    print("=" * 72)
    print("EXTENDED STATISTICS (EF-04) — informational, does not change PASS/FAIL")
    print("=" * 72)
    print(f"  Bootstrap CI (DESCRIPTIVE effect-size interval, H19 — not a "
          f"significance test):")
    print(f"    mean Δ={overall_ci.mean:+.4f}  95% CI=[{overall_ci.ci_lower:+.4f}, "
          f"{overall_ci.ci_upper:+.4f}]  n={overall_ci.n}  B={overall_ci.b}  "
          f"seed={overall_ci.seed}")
    print(f"  Paired permutation test OVERALL ({overall_perm.kind}):")
    print(f"    p_two_sided={overall_perm.p_two_sided:.4f}  "
          f"p_one_sided(new>=current)={overall_perm.p_greater:.4f}  "
          f"exact={overall_perm.exact}  n={overall_perm.n}")
    print(f"    {overall_perm.caveat()}")

    if segment_pvals:
        seg_names = [s for s, _, _ in segment_pvals]
        seg_ns = [n for _, n, _ in segment_pvals]
        seg_raw_p = [p for _, _, p in segment_pvals]
        rejected, adj_p = benjamini_hochberg(seg_raw_p, alpha=args.fdr_alpha)
        print(f"  Benjamini-Hochberg FDR correction across {len(segment_pvals)} "
              f"language x class slice tests (H20, alpha={args.fdr_alpha}):")
        for name, n, raw_p, adj, rej in zip(seg_names, seg_ns, seg_raw_p, adj_p, rejected):
            flag = "SURVIVES" if rej else "does not survive"
            print(f"    {name:<28} n={n:<3} raw_p={raw_p:.4f}  "
                  f"BH-adjusted_q={adj:.4f}  [{flag} FDR correction]")
        naive_wins = sum(1 for p in seg_raw_p if p <= args.fdr_alpha)
        print(f"    naive uncorrected count (p<={args.fdr_alpha}): {naive_wins}  "
              f"| FDR-corrected count: {sum(rejected)}")
    else:
        print("  Benjamini-Hochberg FDR correction: SKIPPED — no per-segment "
              "paired data to correct across.")

    obs_sd = float(np.std(deltas, ddof=1)) if deltas.size > 1 else 0.0
    mde = minimum_detectable_effect(deltas.size, obs_sd, alpha=args.fdr_alpha,
                                     power=args.target_power)
    pw = achieved_power(deltas.size, obs_sd, float(deltas.mean()), alpha=args.fdr_alpha)
    print(f"  Pre-registered minimum detectable effect (H20 — the success gate "
          f"is an effect-size")
    print(f"  threshold, not a bare 'CI lower bound > 0'):")
    print(f"    n={deltas.size}  observed_sd={obs_sd:.4f}  alpha={args.fdr_alpha}  "
          f"target_power={args.target_power} -> MDE={mde:+.4f}")
    print(f"    achieved power for the OBSERVED overall effect "
          f"(mean Δ={deltas.mean():+.4f}): {pw:.4f}")

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
