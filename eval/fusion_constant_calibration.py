#!/usr/bin/env python3
"""RET-11 — measure the fusion constant change through the PRODUCTION path.

Two arms, same index, same queries, one search pass each:

  * ``before`` — ``BRAIN_RRF_K=60``, the pre-RET-11 ranking, byte-identical to
    what shipped;
  * ``after``  — the environment variable unset, so the legs fuse at
    ``brain.index.RRF_K_FUSE``.

It reports two independent bodies of evidence, and keeps them separate:

  1. **The five measured burial cases** (``eval/runs/
     real-question-language-probe-2026-08-04/results.json``) — real questions
     whose answer a leg had at rank 1-17 and the fused ranking threw away.
     These are NOT in the golden set, so they are not a target the constant
     could be fitted to.
  2. **The 66-query golden set** as the regression GUARD, per stratum, reported
     in QUERIES as well as points — n is 6-12 per stratum, so one query is
     8-17 percentage points and a "-2pp" claim is finer than the set resolves.

Read-only. Emits ranks and metric values, never note bodies.

    python3 eval/fusion_constant_calibration.py \
        --vault <V> --index-db <D> --name-to-id-map <M> \
        --qrel-overrides eval/runs/zone-prior-qrel-overrides.json \
        --out eval/runs/fusion-constant-<date>.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

STRATA = ["monolingual_pt", "monolingual_es", "cross_lingual_en_pt",
          "cross_lingual_en_es", "lexical_identifier", "multi_hop", "temporal"]

SCHEMA = "ret11-fusion-constant.v1"

# The five cases the fix exists for, and the leg ranks recorded for each on
# 2026-08-04 (engine 0.19.26, same index). `lang` selects which of the paired
# EN/PT phrasings of that row is the buried one.
CASES = [("B1", "en"), ("D1", "en"), ("J", "en"), ("B2", "pt"), ("D2", "pt")]


def m_recall(ranks, k):
    return sum(1 for r in ranks if r and r <= k) / len(ranks) if ranks else 0.0


def m_mrr(ranks, k=10):
    best = min([r for r in ranks if r] or [10 ** 9])
    return 1.0 / best if best <= k else 0.0


def m_hit(ranks, k=10):
    return 1.0 if min([r for r in ranks if r] or [10 ** 9]) <= k else 0.0


def score(cells: list[list]) -> dict:
    n = len(cells) or 1
    return {
        "n": len(cells),
        "recall@10": round(sum(m_recall(c, 10) for c in cells) / n, 4),
        "recall@20": round(sum(m_recall(c, 20) for c in cells) / n, 4),
        "mrr@10": round(sum(m_mrr(c) for c in cells) / n, 4),
        "hit@10": round(sum(m_hit(c, 10) for c in cells) / n, 4),
        # Queries with at least one gold document in the top 10 — the unit the
        # per-stratum deltas are honestly reported in.
        "queries_hit@10": sum(int(m_hit(c, 10)) for c in cells),
    }


def load_queries(args):
    from pt_stratum_diagnosis import _slug, _vault_notes
    from zone_prior_calibration import (
        _load_name_map,
        _load_overrides,
        refuse_unmappable,
        resolve_gold,
    )

    vault = Path(args.vault).resolve()
    notes = _vault_notes(vault)
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    name_map = _load_name_map(args.name_to_id_map)
    overrides = _load_overrides(args.qrel_overrides)

    queries, unmappable = [], {}
    for q in golden["queries"]:
        gold, unresolved = resolve_gold(q["qrels"], notes, name_map, overrides, _slug)
        if unresolved:
            unmappable[q["id"]] = unresolved
        queries.append({"id": q["id"], "kind": "golden", "stratum": q["stratum"],
                        "held_out": bool(q.get("held_out")), "text": q["text"],
                        "gold": gold})
    # An unresolved qrel scores as a permanent miss in BOTH arms, which is a
    # fabricated retrieval failure. Fail closed (zone_prior_calibration's rule).
    refuse_unmappable(unmappable, args.allow_unmappable_qrels)

    probe = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    by_qid = {r["qid"]: r for r in probe["results"]}
    for qid, lang in CASES:
        r = by_qid[qid]
        queries.append({
            "id": f"case_{qid}", "kind": "case", "stratum": f"case_{lang}",
            "held_out": False, "text": r[lang], "gold": [],
            "target_note_id": r["target"],
            "recorded_2026_08_04": {"lex": r[f"{lang}_lex"],
                                    "dense": r[f"{lang}_dense"],
                                    "fused": r[f"{lang}_fused"]},
        })
    return vault, queries


def run_arm(args, queries, vault, rrf_k_env: str | None) -> dict:
    from brain.embed import get_embedder
    from brain.index import BrainIndex
    from brain.vectors import get_backend

    if rrf_k_env is None:
        os.environ.pop("BRAIN_RRF_K", None)
    else:
        os.environ["BRAIN_RRF_K"] = rrf_k_env
    index = BrainIndex(db_path=Path(args.index_db).resolve(),
                       backend=get_backend("sqlite-vec"),
                       embedder=get_embedder("auto"), read_only=True)
    out = {"stats": index.stats(), "ranks": {}, "cases": {},
           "rerank_applied": {}, "rerank_gate_skipped": {}, "secs": {}}
    for q in queries:
        k = args.case_k if q["kind"] == "case" else args.k
        t0 = time.time()
        hits, trace = index.hybrid_search_with_trace(
            q["text"], k=k, rerank=args.rerank, rerank_top=args.rerank_top)
        out["secs"][q["id"]] = round(time.time() - t0, 2)
        # `rerank_applied` alone cannot separate an RK-02 gate skip from an
        # absent model or a fired timeout (AGENTS.md §5) — keep both.
        out["rerank_applied"][q["id"]] = bool(trace.rerank_applied)
        out["rerank_gate_skipped"][q["id"]] = bool(
            (trace.rerank_gate or {}).get("skipped"))
        if q["kind"] == "case":
            order, seen = [], set()
            for h in hits:
                if h.id not in seen:
                    seen.add(h.id)
                    order.append(h.id)
            out["cases"][q["id"]] = (order.index(q["target_note_id"]) + 1
                                     if q["target_note_id"] in order else None)
            continue
        order, seen = [], set()
        for h in hits:
            rel = (str(Path(h.path).relative_to(vault))
                   if Path(h.path).is_absolute() else h.path)
            if rel not in seen:
                seen.add(rel)
                order.append(rel)
        out["ranks"][q["id"]] = [(order.index(g) + 1 if g in order else None)
                                 for g in q["gold"]]
    if hasattr(index, "close"):
        index.close()
    os.environ.pop("BRAIN_RRF_K", None)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index-db", required=True)
    ap.add_argument("--golden", default=str(HERE / "golden_set.json"))
    ap.add_argument("--cases", default=str(
        HERE / "runs/real-question-language-probe-2026-08-04/results.json"))
    ap.add_argument("--name-to-id-map")
    ap.add_argument("--qrel-overrides")
    ap.add_argument("--allow-unmappable-qrels", action="store_true")
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--case-k", type=int, default=128,
                    help="the depth the 2026-08-04 probe measured the burial at "
                         "(129+ exceeds sqlite-vec's knn limit on this vault)")
    ap.add_argument("--rerank", action="store_true",
                    help="measure through the SHIPPED rerank path (slow)")
    ap.add_argument("--rerank-top", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from brain.index import RRF_K_FUSE

    vault, queries = load_queries(args)
    golden = [q for q in queries if q["kind"] == "golden"]
    print(f"{len(golden)} golden queries + {len(CASES)} burial cases; "
          f"k={args.k} case_k={args.case_k} rerank={args.rerank}")

    arms = {}
    for label, env in (("before", "60"), ("after", None)):
        t0 = time.time()
        arms[label] = run_arm(args, queries, vault, env)
        print(f"  {label:<7} ({'BRAIN_RRF_K=60' if env else f'fuse_k={RRF_K_FUSE}'}) "
              f"{time.time() - t0:.0f}s")

    report = {
        "probe": SCHEMA,
        "captured": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fuse_k_after": RRF_K_FUSE,
        "arm": {"k": args.k, "case_k": args.case_k, "rerank": args.rerank,
                "rerank_top": args.rerank_top if args.rerank else None,
                "zone_weights": os.environ.get("BRAIN_ZONE_WEIGHTS", "(unset)"),
                "index": arms["before"]["stats"]},
        "cases": {}, "overall": {}, "by_stratum": {}, "by_half": {},
        "per_query_mrr10": {}, "per_query_ranks": {}, "rerank": {},
    }

    for qid, _lang in CASES:
        key = f"case_{qid}"
        rec = next(q for q in queries if q["id"] == key)
        report["cases"][qid] = {
            "target": rec["target_note_id"],
            "recorded_2026_08_04": rec["recorded_2026_08_04"],
            "before": arms["before"]["cases"][key],
            "after": arms["after"]["cases"][key],
        }

    def cells(arm, qs):
        return [arms[arm]["ranks"][q["id"]] for q in qs]

    report["overall"] = {a: score(cells(a, golden)) for a in arms}
    for s in STRATA:
        qs = [q for q in golden if q["stratum"] == s]
        report["by_stratum"][s] = {a: score(cells(a, qs)) for a in arms}
    for half, qs in (("train", [q for q in golden if not q["held_out"]]),
                     ("held_out", [q for q in golden if q["held_out"]])):
        report["by_half"][half] = {a: score(cells(a, qs)) for a in arms}
    for q in golden:
        report["per_query_mrr10"][q["id"]] = {
            a: round(m_mrr(arms[a]["ranks"][q["id"]]), 4) for a in arms}
        # Every gold document's rank, not just the best one: 30 of the 66
        # queries have more than one, and keeping only the minimum lets an
        # aggregate gain hide the loss of the rest (zone_prior_calibration's
        # second integrity rule).
        report["per_query_ranks"][q["id"]] = {
            "stratum": q["stratum"], "held_out": q["held_out"],
            **{a: arms[a]["ranks"][q["id"]] for a in arms}}
    for a in arms:
        report["rerank"][a] = {
            "applied": sum(1 for v in arms[a]["rerank_applied"].values() if v),
            "gate_skipped": sum(1 for v in arms[a]["rerank_gate_skipped"].values() if v),
            "queries": len(arms[a]["rerank_applied"]),
        }

    print("\n=== the five measured burial cases (fused rank of the answer) ===")
    print(f"{'case':<5} {'leg ranks (lex/dense)':<24} {'before':>7} {'after':>7}")
    for qid, c in report["cases"].items():
        r = c["recorded_2026_08_04"]
        print(f"{qid:<5} {str(r['lex']) + '/' + str(r['dense']):<24} "
              f"{str(c['before']):>7} {str(c['after']):>7}")

    print("\n=== golden set (regression guard), overall ===")
    b, a = report["overall"]["before"], report["overall"]["after"]
    for m in ("recall@10", "recall@20", "mrr@10", "hit@10"):
        print(f"  {m:<10} {b[m]:.4f} -> {a[m]:.4f}  ({a[m] - b[m]:+.4f})")
    print(f"  queries with a gold doc in the top 10: "
          f"{b['queries_hit@10']} -> {a['queries_hit@10']} of {b['n']}")

    print("\n=== per stratum — QUERIES hit@10, then recall@10 / mrr@10 ===")
    print(f"{'stratum':<22} {'n':>3} {'queries':>12}   {'recall@10':>19} "
          f"{'mrr@10':>19}")
    for s in STRATA:
        b, a = report["by_stratum"][s]["before"], report["by_stratum"][s]["after"]
        print(f"{s:<22} {b['n']:>3} "
              f"{b['queries_hit@10']:>5} -> {a['queries_hit@10']:<5} "
              f"{b['recall@10']:>8.3f} -> {a['recall@10']:<8.3f} "
              f"{b['mrr@10']:>8.3f} -> {a['mrr@10']:<8.3f}")

    moved = [(q, v["before"], v["after"])
             for q, v in report["per_query_mrr10"].items()
             if v["before"] != v["after"]]
    print(f"\n=== per query (mrr@10): {sum(1 for _, b, a in moved if a > b)} better, "
          f"{sum(1 for _, b, a in moved if a < b)} worse, "
          f"{len(golden) - len(moved)} unchanged ===")
    for q, before, after in sorted(moved, key=lambda t: t[2] - t[1]):
        r = report["per_query_ranks"][q]
        print(f"  {'WORSE' if after < before else 'better'} {q:<10} "
              f"{before:.4f} -> {after:.4f}   ranks {r['before']} -> {r['after']}")

    print("\n=== the pre-registered split (descriptive; not a second primary) ===")
    for half in ("train", "held_out"):
        b, a = report["by_half"][half]["before"], report["by_half"][half]["after"]
        print(f"  {half:<9} n={b['n']:<3} mrr@10 {b['mrr@10']:.4f} -> {a['mrr@10']:.4f} "
              f"| recall@10 {b['recall@10']:.4f} -> {a['recall@10']:.4f}")
    if args.rerank:
        print(f"\nrerank: {report['rerank']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
