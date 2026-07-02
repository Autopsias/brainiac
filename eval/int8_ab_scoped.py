#!/usr/bin/env python3
"""S09 (PF-01) — the CLEAN int8-vs-fp32 e5-small ablation: BOTH arms run the
IDENTICAL scoped brute-force cosine dense search (`ScopedInt8Index`) over the
IDENTICAL chunk universe (`_evidence/pt-bench/bge-m3-scope-corpus.json`,
reused from EM-01); the ONLY variable is which weights encoded the vectors —
fp32 (`e5-small-fp32-scope-vectors.npz`) vs int8
(`e5-small-int8-scope-vectors.npz`). This isolates the pure quantization
effect on retrieval quality, with no confound from comparing a full-corpus
(approximate sqlite-vec) search against a scoped (exact brute-force) search
— which `eval/int8_ab.py` (fp32-FULL-production vs int8-SCOPED) does carry,
same as EM-01's BGE-M3-vs-e5 comparison did. This script is the PRIMARY
evidence for the PT-specific quality gate (H9/H36); `int8_ab.py` is reported
as a secondary, confounded directional data point (full-corpus realism, but
NOT an apples-to-apples quantization ablation).

H36: adoption-validation split only; H4-style: lexical FTS5 + zone-authority
prior are IDENTICAL for both arms (same sqlite file, `ScopedInt8Index`
overrides ONLY `_dense_ranked`).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import path_normalize as pn  # noqa: E402
import stats as st  # noqa: E402
from int8_ab import make_scoped_int8_index_cls  # noqa: E402

NI_BOUND = -0.02


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _recall_at_k(rel, ranked, k):
    rels = {d for d, g in rel.items() if g > 0}
    if not rels:
        return 0.0
    return sum(1 for d in ranked[:k] if d in rels) / len(rels)


def _ndcg_at_k(rel, ranked, k):
    dcg = 0.0
    for i, d in enumerate(ranked[:k], start=1):
        g = rel.get(d, 0)
        if g > 0:
            dcg += g / math.log2(i + 1)
    ideal = sorted((g for g in rel.values() if g > 0), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal, start=1))
    return (dcg / idcg) if idcg > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--qrels", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--map", default=None)
    ap.add_argument("--fp32-model-dir", required=True)
    ap.add_argument("--fp32-vectors", required=True)
    ap.add_argument("--int8-model-dir", required=True)
    ap.add_argument("--int8-vectors", required=True)
    ap.add_argument("--scope-meta", required=True)
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from brain.embed import OnnxEmbedder

    os.environ.setdefault("BRAIN_REQUIRE_REAL_EMBEDDER", "1")

    golden = _load(args.golden)
    split = _load(args.split)["folds"]
    qrels_all = _load(args.qrels)["qrels"]
    mapping = _load(args.map) if args.map else None
    scope_meta = _load(args.scope_meta)

    av_qids = split["adoption-validation"]
    forbidden = set(split.get("held-out", []))
    assert not (set(av_qids) & forbidden), "H36 barrier: must not touch held-out"

    qmeta = {q["id"]: q for q in golden["queries"]}
    scope = [q for q in av_qids if q in qmeta]
    scorable = [q for q in scope if q in qrels_all and qrels_all[q]]
    print(f"adoption-validation scope: {len(scope)} queries; scorable: {len(scorable)}")

    vault_root = str(Path(args.vault).resolve())
    chunk_text_by_rowid = {c["chunk_rowid"]: c["text"] for c in scope_meta["chunks"]}
    ScopedCls = make_scoped_int8_index_cls()

    def build_arm(model_dir: str, vectors_path: str, quant: str):
        npz = np.load(vectors_path)
        vectors = npz["vectors"]
        chunk_rowids = npz["chunk_rowids"]
        note_rowids = npz["note_rowids"]
        emb = OnnxEmbedder(local_dir=model_dir, quantization=quant)
        idx = ScopedCls(db_path=Path(args.index), read_only=True, embedder=emb)
        idx.set_scope(vectors, chunk_rowids, note_rowids, chunk_text_by_rowid, emb)
        return idx, emb

    fp32_index, fp32_emb = build_arm(args.fp32_model_dir, args.fp32_vectors, "fp32")
    int8_index, int8_emb = build_arm(args.int8_model_dir, args.int8_vectors, "int8")
    print(f"fp32 arm: {fp32_emb.model_id}; int8 arm: {int8_emb.model_id}; "
          f"both over the IDENTICAL {scope_meta['total_scope_chunks']}-chunk scope")

    def _canon(p: str) -> str:
        rel = os.path.relpath(p, vault_root) if os.path.isabs(p) else p
        return pn.normalize(rel, mapping) if mapping else rel

    per_query: dict[str, dict] = {}
    for q in scorable:
        text = qmeta[q]["text"]
        rel = qrels_all[q]
        ranked_fp32 = [_canon(h.path) for h in fp32_index.hybrid_search(text, k=args.k)]
        ranked_int8 = [_canon(h.path) for h in int8_index.hybrid_search(text, k=args.k)]
        per_query[q] = {
            "lang": qmeta[q].get("lang"), "stratum": qmeta[q].get("stratum"),
            "fp32": {"recall@10": _recall_at_k(rel, ranked_fp32, args.k),
                     "ndcg@10": _ndcg_at_k(rel, ranked_fp32, args.k)},
            "int8": {"recall@10": _recall_at_k(rel, ranked_int8, args.k),
                     "ndcg@10": _ndcg_at_k(rel, ranked_int8, args.k)},
        }

    def _agg(metric, arm, qids):
        vals = [per_query[q][arm][metric] for q in qids if q in per_query]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _delta_ci(metric, qids):
        deltas = [per_query[q]["int8"][metric] - per_query[q]["fp32"][metric]
                  for q in qids if q in per_query]
        if not deltas:
            return None
        ci = st.bootstrap_ci(deltas, b=10000, seed=7)
        mean = round(sum(deltas) / len(deltas), 4)
        return {
            "n": len(deltas), "mean_delta": mean,
            "bootstrap_ci_95": [round(ci.ci_lower, 4), round(ci.ci_upper, 4)],
            "ni_bound": NI_BOUND, "ni_verdict_point_test": mean >= NI_BOUND,
            "kind": "descriptive_effect_size_interval (H19); point-test convention "
                    "for small n, per eval/gate.py's own n=12-14 caveat",
        }

    langs = sorted({qmeta[q].get("lang", "?") for q in scorable})
    by_lang = {}
    for lang in langs:
        qids = [q for q in scorable if qmeta[q].get("lang") == lang]
        by_lang[lang] = {
            "n": len(qids),
            "fp32": {"recall@10": _agg("recall@10", "fp32", qids),
                     "ndcg@10": _agg("ndcg@10", "fp32", qids)},
            "int8": {"recall@10": _agg("recall@10", "int8", qids),
                     "ndcg@10": _agg("ndcg@10", "int8", qids)},
            "delta_recall@10": _delta_ci("recall@10", qids),
            "delta_ndcg@10": _delta_ci("ndcg@10", qids),
        }

    overall = {
        "n": len(scorable),
        "fp32": {"recall@10": _agg("recall@10", "fp32", scorable),
                 "ndcg@10": _agg("ndcg@10", "fp32", scorable)},
        "int8": {"recall@10": _agg("recall@10", "int8", scorable),
                 "ndcg@10": _agg("ndcg@10", "int8", scorable)},
        "delta_recall@10": _delta_ci("recall@10", scorable),
        "delta_ndcg@10": _delta_ci("ndcg@10", scorable),
    }

    pt_gate = by_lang.get("PT", {}).get("delta_recall@10")
    pt_verdict = "PASS" if (pt_gate and pt_gate["ni_verdict_point_test"]) else (
        "FAIL" if pt_gate else "NO_PT_QUERIES")

    out = {
        "session": "s09", "item": "pf-01",
        "design": "CLEAN ablation — both arms are ScopedInt8Index (identical brute-force "
                  "cosine dense search + identical FTS5/zone-authority code) over the "
                  "IDENTICAL scoped chunk universe; ONLY the vectors' precision (fp32 vs "
                  "int8-quantized weights) differs. No full-corpus-vs-scoped confound "
                  "(unlike int8_ab.py's fp32-FULL-production arm).",
        "barrier": "H36 — adoption-validation ONLY; held-out untouched",
        "fp32_arm": {"model_id": fp32_emb.model_id, "dim": fp32_emb.dim},
        "int8_arm": {"model_id": int8_emb.model_id, "dim": int8_emb.dim},
        "scope_summary": {k: v for k, v in scope_meta.items()
                          if k not in ("chunks", "gold_note_paths", "scope_note_rowids")},
        "overall": overall,
        "by_language": by_lang,
        "pt_specific_quality_gate": {
            "rule": f"H9/H36 — PT-specific no-material-recall-loss (NI bound {NI_BOUND:+.2f}), "
                    "adoption-validation split, locked qrels, CLEAN scoped-vs-scoped ablation",
            "verdict": pt_verdict, "detail": pt_gate,
        },
        "_qrels_lock": "_evidence/s01/qrels_adjudicated.json (kappa=0.873, 83/102 usable)",
        "_per_query": per_query,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(overall, indent=2))
    print(json.dumps(by_lang, indent=2, ensure_ascii=False))
    print(f"PT gate verdict (clean scoped-vs-scoped): {pt_verdict}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
