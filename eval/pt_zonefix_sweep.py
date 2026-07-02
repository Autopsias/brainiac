#!/usr/bin/env python3
"""PT-02 (s05) — train+dev-only, k-fold-CV zone-weight sweep (H34).

Diagnosis (`docs/eval-bench/pt-diagnosis.md`, s04/pt-01): the RET-01b
anti-burial zone-authority prior is DEAD on the migrated index because every
note's indexed `zone` column is flattened to `brain`/`raw`. `src/brain/index.py`
(`BrainIndex._resolve_zone`, s05) re-arms it retrieval-time-only by reading the
original Johnny-Decimal zone off each candidate note's frontmatter
(`source_zone:`) — no re-index, no schema change (H23/H11).

This script answers the SECOND half of the fix: once the prior is alive again,
what weight should it use? `docs/eval-bench/pt-diagnosis.md` §3 (E5) showed a
uniform curated-zone boost trades PT-monolingual/multi-hop away to recover
cross-lingual burials. So the grid here is 2-D: a curated-zone boost AND an
independent damp on `40 Meetings` (the flooding zone per E3b) — selected by
STRATIFIED K-FOLD CV on train+dev ONLY (H34: this session may not inspect
adoption-validation or held-out).

Efficiency: retrieval (embed + lexical/dense search) is the expensive part and
does NOT depend on the zone-weight grid, so it runs EXACTLY ONCE per query
(`_lexical_ranked` + `_dense_ranked`, replicating `BrainIndex.hybrid_search`'s
pre-weighting stage). Every grid candidate is then just an in-memory re-weight
+ re-rank of the SAME cached candidate list — the grid sweep itself needs no
extra embedder calls.

Usage (REAL embedder, on-host):
  BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_INDEX_DIR=_workspace/live-vault/.brain \
  .venv-embed/bin/python eval/pt_zonefix_sweep.py \
    --golden _evidence/s01/pt-golden-set.json \
    --split _evidence/s01/pt-split.json \
    --qrels _evidence/s01/qrels_adjudicated.json \
    --vault _workspace/live-vault \
    --map _evidence/cutover-s10/path-map.json \
    --out _evidence/pt-bench/s05-zonefix-sweep.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import path_normalize as pn  # noqa: E402
import stats as st  # noqa: E402

CURATED = ["10 People", "20 Companies", "30 Projects", "60 Concepts", "70 Decisions"]
DAMPED_ZONE = "40 Meetings"

CURATED_GRID = [1.0, 1.35, 2.0, 3.0, 4.0]
DAMP_GRID = [1.0, 0.85, 0.70, 0.55]
GRID = [(c, d) for c in CURATED_GRID for d in DAMP_GRID]


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _weights(curated: float, damp: float) -> dict[str, float]:
    w = {z: curated for z in CURATED}
    w[DAMPED_ZONE] = damp
    return w


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


def capture_candidates(core, query_text: str, n: int, vault_root: str, mapping: dict):
    """Replicates BrainIndex.hybrid_search's PRE-weighting stage (lexical +
    dense RRF fusion), returning enough state to re-weight/re-rank offline for
    every grid candidate without a second retrieval call. Read-only against
    the index; does not touch `_zone_weights`/env at all here."""
    idx = core.index
    lex = idx._lexical_ranked(query_text, n)
    dense, best_chunk_text, _best_chunk_rowid = idx._dense_ranked(query_text, n)
    in_lex = set(lex)
    scores: dict[int, float] = {}
    for rank, rid in enumerate(lex, start=1):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (60 + rank)
    for rank, rid in enumerate(dense, start=1):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (60 + rank)
    if not scores:
        return {}, {}, set()
    rids = tuple(scores)
    qmarks = ",".join("?" * len(rids))
    rows = idx.conn.execute(
        f"SELECT rowid, zone, path FROM notes WHERE rowid IN ({qmarks})", rids
    ).fetchall()
    zone_of: dict[int, str] = {}
    src_path_of: dict[int, str] = {}
    for r, z, p in rows:
        rid = int(r)
        zone_of[rid] = idx._resolve_zone(z or "", p or "")
        rel = os.path.relpath(p, vault_root) if os.path.isabs(p) else p
        src_path_of[rid] = pn.normalize(rel, mapping)
    return scores, zone_of, in_lex, src_path_of


def rank_for(scores, zone_of, in_lex, src_path_of, weights, k=10) -> list[str]:
    final: dict[int, float] = {}
    for rid, base in scores.items():
        if rid in in_lex:
            final[rid] = base  # semantic_only scope — lexical hits untouched
        else:
            final[rid] = base * float(weights.get(zone_of.get(rid, ""), 1.0))
    ordered = sorted(final, key=lambda r: (-final[r], r))
    seen, out = set(), []
    for rid in ordered:
        p = src_path_of.get(rid)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= k:
            break
    return out


def select_grid(train_qids, per_cand_recall, k_metric="recall@10"):
    """argmax mean recall@10 over the grid on train_qids; ties -> smallest
    (curated, damp) in ascending order (parsimony)."""
    best, best_score = None, None
    for cand in sorted(GRID):
        vals = [per_cand_recall[cand][q] for q in train_qids if q in per_cand_recall[cand]]
        if not vals:
            continue
        m = sum(vals) / len(vals)
        if best_score is None or m > best_score + 1e-12:
            best_score, best = m, cand
    return best


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

    # H34 barrier: THIS session tunes on train+dev ONLY. adoption-validation
    # and held-out are never touched, not even for reporting.
    train_dev = sorted((set(split["train"]) | set(split["dev"])))
    forbidden = set(split.get("adoption-validation", [])) | set(split.get("held-out", []))

    qmeta = {q["id"]: q for q in golden["queries"]}
    scope = [q for q in train_dev if q in qmeta]
    assert not (set(scope) & forbidden), "H34 barrier violated"
    qstr = {q: qmeta[q]["stratum"] for q in scope}
    scorable = [q for q in scope if q in qrels_all and qrels_all[q]]
    print(f"train+dev scope: {len(scope)} queries; scorable (has locked-rel): {len(scorable)}")

    vault_root = str(Path(args.vault).resolve())
    core = BrainCore(vault=vault_root)
    n = max(args.k * args.candidate_factor, args.k)

    # 1) Single retrieval pass per query (expensive: embed + search).
    cand_by_q = {}
    for q in scope:
        text = qmeta[q]["text"]
        scores, zone_of, in_lex, src_path_of = capture_candidates(
            core, text, n, vault_root, mapping)
        cand_by_q[q] = (scores, zone_of, in_lex, src_path_of)
    print(f"captured retrieval candidates for {len(cand_by_q)} queries (1 embed call each)")

    # 2) In-memory grid sweep: recall@10 / ndcg@10 per (candidate weights, query).
    per_cand_recall: dict[tuple, dict[str, float]] = {c: {} for c in GRID}
    per_cand_ndcg: dict[tuple, dict[str, float]] = {c: {} for c in GRID}
    baseline_recall: dict[str, float] = {}  # BRAIN_ZONE_SOURCE_MODE=column equivalent
    for q in scorable:
        scores, zone_of, in_lex, src_path_of = cand_by_q[q]
        rel = qrels_all[q]
        # "before" baseline: prior dead (every zone key misses -> weight 1.0 always)
        ranked_base = rank_for(scores, {}, in_lex, src_path_of, {}, k=args.k)
        baseline_recall[q] = _recall_at_k(rel, ranked_base, args.k)
        for cand in GRID:
            w = _weights(*cand)
            ranked = rank_for(scores, zone_of, in_lex, src_path_of, w, k=args.k)
            per_cand_recall[cand][q] = _recall_at_k(rel, ranked, args.k)
            per_cand_ndcg[cand][q] = _ndcg_at_k(rel, ranked, args.k)

    # 3) Stratified 5-fold CV: select weight on train portion, score on held fold.
    folds = stratified_folds(scorable, qstr, args.outer_k, args.seed)
    fold_deltas = []
    cv_new: dict[str, float] = {}
    cv_base: dict[str, float] = {}
    chosen_per_fold = []
    for i in range(args.outer_k):
        test = folds[i]
        train = [q for q in scorable if q not in test]
        cand = select_grid(train, per_cand_recall)
        chosen_per_fold.append(cand)
        for q in test:
            cv_new[q] = per_cand_recall[cand][q]
            cv_base[q] = baseline_recall[q]
        d = (sum(per_cand_recall[cand][q] for q in test) / len(test)
             - sum(baseline_recall[q] for q in test) / len(test)) if test else 0.0
        fold_deltas.append(d)

    import statistics as _statistics
    pooled_deltas = [cv_new[q] - cv_base[q] for q in scorable]
    ci = st.bootstrap_ci(pooled_deltas, b=10000, seed=7)
    sd = _statistics.stdev(pooled_deltas) if len(pooled_deltas) > 1 else 0.0
    mde = st.minimum_detectable_effect(len(scorable), sd=sd)

    # 4) "Deployed" candidate: selected on ALL of train+dev (disclosed as in-sample).
    deployed = select_grid(scorable, per_cand_recall)

    baseline_ndcg: dict[str, float] = {}
    for q in scorable:
        scores, zone_of, in_lex, src_path_of = cand_by_q[q]
        ranked = rank_for(scores, {}, in_lex, src_path_of, {}, k=args.k)
        baseline_ndcg[q] = _ndcg_at_k(qrels_all[q], ranked, args.k)

    per_stratum = {}
    for stname in sorted(set(qstr.values())):
        sq = [q for q in scorable if qstr.get(q) == stname]
        if not sq:
            continue
        per_stratum[stname] = {
            "n": len(sq),
            "baseline_recall@10": round(sum(baseline_recall[q] for q in sq) / len(sq), 4),
            "cv_recall@10": round(sum(cv_new[q] for q in sq) / len(sq), 4),
            "delta": round(sum(cv_new[q] - baseline_recall[q] for q in sq) / len(sq), 4),
            "deployed_cand_recall@10": round(
                sum(per_cand_recall[deployed][q] for q in sq) / len(sq), 4),
        }

    out = {
        "session": "s05",
        "item": "pt-02",
        "barrier": "H34 — train+dev only; adoption-validation and held-out untouched",
        "n_scope": len(scope),
        "n_scorable": len(scorable),
        "grid": {"curated": CURATED_GRID, "meetings_damp": DAMP_GRID},
        "outer_k": args.outer_k,
        "seed": args.seed,
        "cv_chosen_per_fold": [list(c) for c in chosen_per_fold],
        "cv_pooled": {
            "baseline_mean_recall@10": round(sum(baseline_recall[q] for q in scorable) / len(scorable), 4),
            "cv_mean_recall@10": round(sum(cv_new[q] for q in scorable) / len(scorable), 4),
            "mean_delta": round(sum(pooled_deltas) / len(pooled_deltas), 4),
            "bootstrap_ci_95": [round(ci.ci_lower, 4), round(ci.ci_upper, 4)],
            "pre_registered_mde_alpha05_power80": round(mde, 4),
        },
        "per_outer_fold_delta": [round(d, 4) for d in fold_deltas],
        "per_stratum_cv": per_stratum,
        "deployed_candidate": {"curated_weight": deployed[0], "meetings_damp": deployed[1]},
        "deployed_vs_baseline_by_stratum": {
            stname: {
                "n": v["n"],
                "baseline": v["baseline_recall@10"],
                "deployed": v["deployed_cand_recall@10"],
                "delta": round(v["deployed_cand_recall@10"] - v["baseline_recall@10"], 4),
            }
            for stname, v in per_stratum.items()
        },
        # Raw per-query values (qid-keyed only — NOT query text/paths; egress-safe
        # to keep alongside the aggregates for downstream stats scripts, but this
        # whole file lives under gitignored _evidence/ regardless, per H24).
        "_qstratum": qstr,
        "_per_query": {
            "baseline_recall@10": {q: round(baseline_recall[q], 6) for q in scorable},
            "cv_recall@10": {q: round(cv_new[q], 6) for q in scorable},
            "deployed_recall@10": {q: round(per_cand_recall[deployed][q], 6) for q in scorable},
            "deployed_ndcg@10": {q: round(per_cand_ndcg[deployed][q], 6) for q in scorable},
            "baseline_ndcg@10": {q: round(baseline_ndcg[q], 6) for q in scorable},
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out["cv_pooled"], indent=2))
    print(f"deployed candidate: {out['deployed_candidate']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
