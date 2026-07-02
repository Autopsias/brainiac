#!/usr/bin/env python3
"""PT-02 (s05, follow-on) — near-dup transcript suppression sweep (H34/H19).

On TOP of the s05 zone-authority fix (curated 2.0x / 40 Meetings 0.55x, already
the shipped `_DEFAULT_ZONE_WEIGHTS`), sweep the retrieval-time near-duplicate
suppression threshold (`BrainIndex._suppress_near_dups`, cause #3 of
`docs/eval-bench/pt-diagnosis.md`) and CV-select it on train+dev ONLY (H34).

The suppression pass is retrieval-time-only (H11/H23 — demote near-dup
transcript clones to the tail, never delete from the index). This script
exercises the EXACT production method (`idx._suppress_near_dups`) at each
threshold — no reimplementation, so what CV-selects is what ships.

Efficiency: the expensive retrieval + zone-weighting stage runs ONCE per query;
each threshold is then a pure in-memory re-application of the suppression method
on the same cached candidate list + vectors.

Usage (REAL embedder, on-host):
  BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_INDEX_DIR=_workspace/live-vault/.brain \
  .venv-embed/bin/python eval/pt_dedup_sweep.py \
    --golden _evidence/s01/pt-golden-set.json \
    --split _evidence/s01/pt-split.json \
    --qrels _evidence/s01/qrels_adjudicated.json \
    --vault _workspace/live-vault \
    --map _evidence/cutover-s10/path-map.json \
    --out _evidence/pt-bench/s05-dedup-sweep.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import path_normalize as pn  # noqa: E402
import stats as st  # noqa: E402

# 1.0 disables suppression (production gate is 0 < thr < 1.0) => zone-fix-only
# baseline. The rest span the diagnosis's near-dup bands (>=0.97 "error",
# >=0.90 review/watch, >=0.80 the widest measured band).
THRESHOLDS = [1.0, 0.99, 0.97, 0.95, 0.90, 0.85, 0.80]
DISABLED = 1.0


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _recall_at_k(rel: dict[str, int], ranked: list[str], k: int) -> float:
    rels = {d for d, g in rel.items() if g > 0}
    if not rels:
        return 0.0
    return sum(1 for d in ranked[:k] if d in rels) / len(rels)


def _ndcg_at_k(rel: dict[str, int], ranked: list[str], k: int) -> float:
    dcg = 0.0
    for i, d in enumerate(ranked[:k], start=1):
        g = rel.get(d, 0)
        if g > 0:
            dcg += g / math.log2(i + 1)
    ideal = sorted((g for g in rel.values() if g > 0), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal, start=1))
    return (dcg / idcg) if idcg > 0 else 0.0


def capture_ordered(idx, query_text, n, vault_root, mapping):
    """Replicate hybrid_search's pipeline UP TO (but not including) the dedup
    pass, using the SHIPPED zone weights (default 2.0/0.55). Returns the
    zone-weighted `ordered` note-rowid list + everything `_suppress_near_dups`
    consumes + the rowid->source-path map for scoring."""
    lex = idx._lexical_ranked(query_text, n)
    dense, _txt, best_chunk_rowid = idx._dense_ranked(query_text, n)
    in_lex = set(lex)
    scores: dict[int, float] = {}
    for rank, rid in enumerate(lex, start=1):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (60 + rank)
    for rank, rid in enumerate(dense, start=1):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (60 + rank)
    zmap: dict[int, str] = {}
    col_zone: dict[int, str] = {}
    src_path_of: dict[int, str] = {}
    if scores:
        rids = tuple(scores)
        qmarks = ",".join("?" * len(rids))
        for r, z, p in idx.conn.execute(
            f"SELECT rowid, zone, path FROM notes WHERE rowid IN ({qmarks})", rids
        ):
            rid = int(r)
            col_zone[rid] = z or ""
            zmap[rid] = idx._resolve_zone(z or "", p or "")
            rel = os.path.relpath(p, vault_root) if os.path.isabs(p) else p
            src_path_of[rid] = pn.normalize(rel, mapping)
        scope = os.environ.get("BRAIN_ZONE_SCOPE", "semantic_only").strip().lower()
        for rid in scores:
            if scope == "semantic_only" and rid in in_lex:
                continue
            scores[rid] *= idx._zone_weight(zmap.get(rid, ""))
    ordered = sorted(scores, key=lambda r: (-scores[r], r))
    return ordered, best_chunk_rowid, zmap, col_zone, in_lex, src_path_of


def ranked_paths(idx, cap, thr, k=10):
    """Apply the PRODUCTION suppression method at threshold `thr`, then map to
    unique source paths in the resulting order."""
    ordered, best_chunk_rowid, zmap, col_zone, in_lex, src_path_of = cap
    os.environ["BRAIN_DEDUP_THRESHOLD"] = repr(thr)
    reordered = idx._suppress_near_dups(ordered, best_chunk_rowid, zmap, col_zone, in_lex)
    seen, out = set(), []
    for rid in reordered:
        p = src_path_of.get(rid)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= k:
            break
    return out


def stratified_folds(qids, qstr, k, seed):
    rng = random.Random(seed)
    by_str: dict[str, list] = {}
    for q in qids:
        by_str.setdefault(qstr.get(q, "?"), []).append(q)
    folds = [[] for _ in range(k)]
    for st_name, qs in sorted(by_str.items()):
        qs = qs[:]
        rng.shuffle(qs)
        for i, q in enumerate(qs):
            folds[i % k].append(q)
    return folds


def select_threshold(train_qids, per_thr_recall):
    """argmax mean recall@10 on train; ties -> LARGEST threshold (least
    aggressive suppression = most conservative, closest to zone-fix-only)."""
    best, best_score = None, None
    for thr in sorted(THRESHOLDS, reverse=True):  # desc -> ties keep larger thr
        vals = [per_thr_recall[thr][q] for q in train_qids if q in per_thr_recall[thr]]
        if not vals:
            continue
        m = sum(vals) / len(vals)
        if best_score is None or m > best_score + 1e-12:
            best_score, best = m, thr
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--qrels", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--map", default=None)
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--candidate-factor", type=int, default=8)
    ap.add_argument("--outer-k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260702)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from brain.core import BrainCore

    golden = _load(args.golden)
    split = _load(args.split)["folds"]
    qrels_all = _load(args.qrels)["qrels"]
    mapping = _load(args.map) if args.map else None

    train_dev = sorted((set(split["train"]) | set(split["dev"])))
    forbidden = set(split.get("adoption-validation", [])) | set(split.get("held-out", []))
    qmeta = {q["id"]: q for q in golden["queries"]}
    scope = [q for q in train_dev if q in qmeta]
    assert not (set(scope) & forbidden), "H34 barrier violated"
    qstr = {q: qmeta[q]["stratum"] for q in scope}
    scorable = [q for q in scope if q in qrels_all and qrels_all[q]]
    print(f"train+dev scope: {len(scope)}; scorable: {len(scorable)}")

    vault_root = str(Path(args.vault).resolve())
    core = BrainCore(vault=vault_root)
    idx = core.index
    n = max(args.k * args.candidate_factor, args.k)

    caps = {q: capture_ordered(idx, qmeta[q]["text"], n, vault_root, mapping) for q in scope}
    print(f"captured pre-dedup candidates for {len(caps)} queries")

    per_thr_recall = {thr: {} for thr in THRESHOLDS}
    per_thr_ndcg = {thr: {} for thr in THRESHOLDS}
    for q in scorable:
        rel = qrels_all[q]
        for thr in THRESHOLDS:
            rp = ranked_paths(idx, caps[q], thr, k=args.k)
            per_thr_recall[thr][q] = _recall_at_k(rel, rp, args.k)
            per_thr_ndcg[thr][q] = _ndcg_at_k(rel, rp, args.k)
    os.environ.pop("BRAIN_DEDUP_THRESHOLD", None)

    baseline = per_thr_recall[DISABLED]  # zone-fix only, dedup off

    # Stratified 5-fold CV: select threshold on train, score on held fold.
    folds = stratified_folds(scorable, qstr, args.outer_k, args.seed)
    cv_new, chosen = {}, []
    for i in range(args.outer_k):
        test = folds[i]
        train = [q for q in scorable if q not in test]
        thr = select_threshold(train, per_thr_recall)
        chosen.append(thr)
        for q in test:
            cv_new[q] = per_thr_recall[thr][q]

    # Deltas are measured vs the ZONE-FIX-ONLY baseline (the incremental value
    # of adding dedup on top of s05's zone fix — H19 clean attribution).
    deltas = [cv_new[q] - baseline[q] for q in scorable]
    ci = st.bootstrap_ci(deltas, b=10000, seed=7)
    sd = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
    mde = st.minimum_detectable_effect(len(scorable), sd=sd)

    # Grid means (in-sample, informational) + the deployed pick.
    grid_means = {
        str(thr): round(sum(per_thr_recall[thr][q] for q in scorable) / len(scorable), 4)
        for thr in THRESHOLDS
    }
    deployed = max(sorted(THRESHOLDS, reverse=True),
                   key=lambda thr: sum(per_thr_recall[thr][q] for q in scorable))

    per_stratum = {}
    for stname in sorted(set(qstr.values())):
        sq = [q for q in scorable if qstr.get(q) == stname]
        if not sq:
            continue
        per_stratum[stname] = {
            "n": len(sq),
            "zonefix_only_recall@10": round(sum(baseline[q] for q in sq) / len(sq), 4),
            "cv_recall@10": round(sum(cv_new[q] for q in sq) / len(sq), 4),
            "delta_vs_zonefix": round(sum(cv_new[q] - baseline[q] for q in sq) / len(sq), 4),
            "deployed_recall@10": round(
                sum(per_thr_recall[deployed][q] for q in sq) / len(sq), 4),
        }

    out = {
        "session": "s05", "item": "pt-02", "lever": "near-dup transcript suppression",
        "barrier": "H34 — train+dev only; deltas vs the zone-fix-only baseline",
        "n_scorable": len(scorable),
        "thresholds_swept": THRESHOLDS,
        "outer_k": args.outer_k, "seed": args.seed,
        "cv_chosen_per_fold": chosen,
        "grid_means_recall@10_insample": grid_means,
        "deployed_threshold": deployed,
        "cv_pooled_vs_zonefix": {
            "zonefix_only_mean_recall@10": round(sum(baseline[q] for q in scorable) / len(scorable), 4),
            "cv_mean_recall@10": round(sum(cv_new[q] for q in scorable) / len(scorable), 4),
            "mean_delta": round(sum(deltas) / len(deltas), 4),
            "bootstrap_ci_95": [round(ci.ci_lower, 4), round(ci.ci_upper, 4)],
            "pre_registered_mde_alpha05_power80": round(mde, 4),
        },
        "per_stratum_cv": per_stratum,
        "_qstratum": qstr,
        "_per_query": {
            "zonefix_only_recall@10": {q: round(baseline[q], 6) for q in scorable},
            "cv_recall@10": {q: round(cv_new[q], 6) for q in scorable},
            "deployed_recall@10": {q: round(per_thr_recall[deployed][q], 6) for q in scorable},
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in
                      ("grid_means_recall@10_insample", "cv_chosen_per_fold",
                       "deployed_threshold", "cv_pooled_vs_zonefix")}, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
