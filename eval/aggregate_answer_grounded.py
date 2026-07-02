"""Aggregate the answer-grounded agentic eval — brain vs SC, end to end.

Reads the blinded judge outputs + the keymap, un-blinds to brain/sc, and reports:
  - RAG-triad rubric (faithfulness / completeness / precision, 0-1) per system, mean ± CI
  - composite answer score (mean of the three)
  - blinded pairwise win/tie/loss + win-rate (brain over SC)
  - per-stratum composite deltas (directional at small n)
  - INSUFFICIENT-CONTEXT rate per system (did the retriever surface enough to answer at all)
Generator = Haiku, Judge = Sonnet (generator != judge -> no self-preference).
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EV = HERE.parent / "_evidence" / "s10"
ANS = EV / "answers"
DIMS = ["faithfulness", "completeness", "precision"]


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def boot_ci(xs, B=10000, seed=7):
    import numpy as np
    if not xs:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    a = np.array(xs, float)
    m = a[rng.integers(0, len(a), size=(B, len(a)))].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main() -> int:
    out: dict[str, dict] = {}
    for f in sorted(ANS.glob("judge_out_*.json")):
        out.update(json.loads(f.read_text()))
    keymap = json.loads((EV / "judge_keymap.json").read_text())
    ctxs = json.loads((EV / "answer_contexts.json").read_text())
    gen: dict[str, dict] = {}
    for f in sorted(ANS.glob("gen_*.json")):
        gen.update(json.loads(f.read_text()))

    # per-system per-dim scores + composite, un-blinded
    sys_scores = {"brain": {d: [] for d in DIMS}, "sc": {d: [] for d in DIMS}}
    sys_comp = {"brain": {}, "sc": {}}
    pair = {"brain": 0, "sc": 0, "tie": 0}
    insuff = {"brain": 0, "sc": 0}
    qstr = {}
    for qid, j in out.items():
        km = keymap[qid]
        qstr[qid] = ctxs[qid]["stratum"]
        for slot in ("ans1", "ans2"):
            system = km[slot]
            sc_obj = j.get(slot, {})
            comp = []
            for d in DIMS:
                v = float(sc_obj.get(d, 0.0))
                sys_scores[system][d].append(v)
                comp.append(v)
            sys_comp[system][qid] = mean(comp)
        # pairwise preference (blinded slot -> system)
        pref = str(j.get("pref", "tie")).lower()
        if pref in ("1", "ans1"):
            pair[km["ans1"]] += 1
        elif pref in ("2", "ans2"):
            pair[km["ans2"]] += 1
        else:
            pair["tie"] += 1
        # insufficient-context rate (from generator answers directly)
        g = gen.get(qid, {})
        if g.get("brain_answer", "").strip().upper().startswith("INSUFFICIENT"):
            insuff["brain"] += 1
        if g.get("sc_answer", "").strip().upper().startswith("INSUFFICIENT"):
            insuff["sc"] += 1

    n = len(out)
    print("=" * 72)
    print(f"ANSWER-GROUNDED AGENTIC EVAL — brain vs SC (n={n} queries)")
    print("  generator=Haiku, judge=Sonnet (blinded slots, generator!=judge)")
    print("=" * 72)
    print("\nRAG-triad rubric (0-1), per system:")
    for d in DIMS:
        bm, sm = mean(sys_scores["brain"][d]), mean(sys_scores["sc"][d])
        print(f"  {d:14s}  brain={bm:.3f}   sc={sm:.3f}   Δ={bm-sm:+.3f}")
    bcomp = [sys_comp["brain"][q] for q in sys_comp["brain"]]
    scomp = [sys_comp["sc"][q] for q in sys_comp["sc"]]
    bm, sm = mean(bcomp), mean(scomp)
    deltas = [sys_comp["brain"][q] - sys_comp["sc"][q] for q in sys_comp["brain"]]
    lo, hi = boot_ci(deltas)
    print(f"\n  COMPOSITE      brain={bm:.3f}   sc={sm:.3f}   "
          f"Δ={bm-sm:+.3f}  (95% CI [{lo:+.3f}, {hi:+.3f}])")
    print(f"\nBlinded pairwise preference: brain={pair['brain']}  sc={pair['sc']}  "
          f"tie={pair['tie']}")
    decided = pair["brain"] + pair["sc"]
    if decided:
        print(f"  brain win-rate (decided only) = {pair['brain']/decided:.1%}")
    print(f"\nINSUFFICIENT-CONTEXT rate (retriever failed to surface an answer):")
    print(f"  brain={insuff['brain']}/{n} ({insuff['brain']/n:.1%})   "
          f"sc={insuff['sc']}/{n} ({insuff['sc']/n:.1%})")

    print("\nPer-stratum composite Δ (brain − sc; n<20 directional):")
    for st in sorted(set(qstr.values())):
        sq = [q for q in sys_comp["brain"] if qstr[q] == st]
        if not sq:
            continue
        dm = mean([sys_comp["brain"][q] - sys_comp["sc"][q] for q in sq])
        print(f"  {st:22s} n={len(sq):2d}  Δ={dm:+.3f}")

    summary = {
        "n": n,
        "rubric": {d: {"brain": mean(sys_scores["brain"][d]),
                       "sc": mean(sys_scores["sc"][d])} for d in DIMS},
        "composite": {"brain": bm, "sc": sm, "delta": bm - sm, "ci": [lo, hi]},
        "pairwise": pair,
        "insufficient_context": insuff,
    }
    (EV / "answer_grounded_summary.json").write_text(json.dumps(summary, indent=1))
    print(f"\nwrote {EV / 'answer_grounded_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
