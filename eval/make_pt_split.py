#!/usr/bin/env python3
"""EF-02 (s01) — emit a LOCKED, SEEDED, STRATIFIED 4-way split of the PT golden
set (H34/H36/H37). Splits on query id only (stable regardless of final
adjudicated relevance), so it can be produced BEFORE Ricardo locks the qrels.

Four folds:
  * train             — s04/s05 tuning
  * dev               — s04/s05 tuning (held-in dev signal)
  * adoption-validation — s06 A/B, s07 adoption, s09 int8 selection
                          (H37 floor: >=30 queries incl. PT >=15)
  * held-out          — scored EXACTLY ONCE, by s11b, across all metric families;
                        labels/results hidden from every tuning + selection session.

Stratified by (stratum x query-lang) with a fixed seed so the split is
reproducible. PT out-of-fold floor >=40 (H18) is asserted across
train+dev+adoption-validation (the folds available to tuning/selection).

Usage:
  python3 eval/make_pt_split.py \
    --golden _evidence/s01/pt-golden-set.json \
    --out _evidence/s01/pt-split.json \
    --seed 20260701
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

# fold order matters for deterministic round-robin dealing
FOLDS = ["held-out", "adoption-validation", "train", "dev"]
# target proportions (dealt round-robin within each stratum to hit ~these)
WEIGHTS = {"held-out": 0.20, "adoption-validation": 0.30, "train": 0.35, "dev": 0.15}


def deal(items: list[str], rng: random.Random) -> dict[str, list[str]]:
    """Deal items into folds proportional to WEIGHTS, deterministically."""
    rng.shuffle(items)
    out: dict[str, list[str]] = {f: [] for f in FOLDS}
    # largest-remainder allocation of counts, then slice in fold order
    n = len(items)
    raw = {f: WEIGHTS[f] * n for f in FOLDS}
    base = {f: int(raw[f]) for f in FOLDS}
    rem = n - sum(base.values())
    # give leftover to folds with largest fractional part, stable order
    order = sorted(FOLDS, key=lambda f: (-(raw[f] - base[f]), FOLDS.index(f)))
    for f in order[:rem]:
        base[f] += 1
    idx = 0
    for f in FOLDS:
        out[f] = items[idx:idx + base[f]]
        idx += base[f]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20260701)
    args = ap.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qs = golden["queries"]

    # stratify by (stratum, query-lang)
    strata: dict[tuple[str, str], list[str]] = defaultdict(list)
    meta = {q["id"]: q for q in qs}
    for q in qs:
        strata[(q["stratum"], q["lang"])].append(q["id"])

    rng = random.Random(args.seed)
    assign: dict[str, str] = {}
    for key in sorted(strata):
        dealt = deal(sorted(strata[key]), rng)
        for f, ids in dealt.items():
            for qid in ids:
                assign[qid] = f

    folds: dict[str, list[str]] = {f: [] for f in FOLDS}
    for qid, f in assign.items():
        folds[f].append(qid)
    for f in folds:
        folds[f].sort()

    def pt_count(ids: list[str]) -> int:
        return sum(1 for i in ids if "PT" in (meta[i]["lang"], meta[i]["target_lang"]))

    fold_stats = {}
    for f in FOLDS:
        ids = folds[f]
        fold_stats[f] = {
            "n": len(ids),
            "pt_touching": pt_count(ids),
            "by_stratum": dict(sorted(Counter(meta[i]["stratum"] for i in ids).items())),
            "by_lang": dict(sorted(Counter(meta[i]["lang"] for i in ids).items())),
        }

    tuning_pt = pt_count(folds["train"] + folds["dev"] + folds["adoption-validation"])
    av = fold_stats["adoption-validation"]
    checks = {
        "H18_pt_out_of_fold_floor_ge_40": {"value": tuning_pt, "pass": tuning_pt >= 40},
        "H37_adoption_validation_n_ge_30": {"value": av["n"], "pass": av["n"] >= 30},
        "H37_adoption_validation_pt_ge_15": {"value": av["pt_touching"], "pass": av["pt_touching"] >= 15},
    }

    doc = {
        "schema_version": "s01.pt-split.v1",
        "created": "2026-07-01",
        "seed": args.seed,
        "stratified_by": ["stratum", "query_lang"],
        "n_total": len(qs),
        "fold_purpose": {
            "train": "s04/s05 tuning",
            "dev": "s04/s05 tuning (dev signal)",
            "adoption-validation": "s06 A/B, s07 adoption, s09 int8 selection",
            "held-out": "scored ONCE by s11b across all metric families; hidden from tuning+selection",
        },
        "barrier": "LABEL-FIRST (H34): held-out labels/results hidden from every tuning AND selection session; touched exactly once by the final report (s11b).",
        "acceptance": checks,
        "fold_stats": fold_stats,
        "folds": folds,
        "assignment": dict(sorted(assign.items())),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"seed={args.seed}  n={len(qs)}")
    for f in FOLDS:
        s = fold_stats[f]
        print(f"  {f:22s} n={s['n']:3d}  PT-touching={s['pt_touching']:3d}")
    print("acceptance:")
    for k, v in checks.items():
        print(f"  {'PASS' if v['pass'] else 'FAIL'}  {k} = {v['value']}")
    print(f"wrote {args.out}")
    return 0 if all(v["pass"] for v in checks.values()) else 3


if __name__ == "__main__":
    raise SystemExit(main())
