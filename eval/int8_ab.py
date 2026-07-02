#!/usr/bin/env python3
"""S09 (PF-01) — int8 vs fp32 multilingual-e5-small A/B on the SAME s05-fixed
hybrid retrieval pipeline, mirroring `eval/bge_m3_ab.py` (EM-01)'s structure.
H36: scored on the ADOPTION-VALIDATION split only (`_evidence/s01/pt-split.json`)
— never held-out (that split is s11b's single touch). H4: both arms go through
`BrainIndex.hybrid_search` UNMODIFIED (RRF fusion + zone-authority prior +
near-dup suppression, off by default) — the ONLY thing that differs between
arms is where the DENSE candidate list comes from:

  * fp32 arm  — `BrainIndex(read_only=True)` pointed at the LIVE production
    index (`_workspace/live-vault/.brain/index.sqlite`); dense leg = the real
    sqlite-vec search over the full 96,568-chunk corpus, embedder =
    `intfloat/multilingual-e5-small` (fp32, the shipped production weights).
  * int8 arm  — `ScopedInt8Index` (subclasses `BrainIndex`, overrides ONLY
    `_dense_ranked`) pointed at the SAME sqlite file (so lexical FTS5 + zone
    metadata are IDENTICAL), backed by a brute-force cosine search over the
    SAME scoped chunk corpus reused from EM-01
    (`_evidence/pt-bench/bge-m3-scope-corpus.json`), encoded with the int8-
    quantized weights (`eval/int8_encode_scope.py`). Both the scope's chunk
    vectors AND the query vector are int8-encoded here — a fair int8-vs-int8
    comparison (unlike the latency test, which swaps only the query side
    against the fp32-embedded production index).

PT-specific quality gate (H9/H36, orchestrator addendum): PASS iff the PT
Recall@10 delta (int8 - fp32) on the PT-language adoption-validation queries
does not cross the ef-04 -2pp non-inferiority margin. n is small (PT~11), so
this uses gate.py's 'point' test convention for small strata (mean delta >=
bound; bootstrap CI reported as ADVISORY, per eval/gate.py's own documented
n=12-14 caveat) rather than requiring the CI lower bound to clear the bound.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import path_normalize as pn  # noqa: E402
import stats as st  # noqa: E402

NI_BOUND = -0.02  # -2pp, same margin as eval/gate.py DEFAULT_BOUND


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


def make_scoped_int8_index_cls():
    from brain.index import BrainIndex

    class _ScopedInt8Index(BrainIndex):
        """`BrainIndex` with ONLY `_dense_ranked` overridden — every other
        method (lexical FTS5, zone-authority prior, near-dup suppression, RRF
        fusion in `hybrid_search`) runs the UNMODIFIED production code against
        the SAME sqlite file (H4)."""

        def set_scope(self, vectors: np.ndarray, chunk_rowids: np.ndarray,
                      note_rowids: np.ndarray, chunk_text_by_rowid: dict[int, str],
                      int8_embedder) -> None:
            self._scope_vectors = vectors  # (N, 384) float32, L2-normalised
            self._scope_chunk_rowids = chunk_rowids
            self._scope_note_rowids = note_rowids
            self._scope_chunk_text = chunk_text_by_rowid
            self._int8_embedder = int8_embedder

        def _dense_ranked(self, query: str, n: int):
            qvec = np.asarray(self._int8_embedder.embed(query, is_query=True), dtype=np.float32)
            sims = self._scope_vectors @ qvec  # cosine (both sides L2-normalised)
            take = min(n * 4, sims.shape[0])
            top_idx = np.argpartition(-sims, take - 1)[:take]
            top_idx = top_idx[np.argsort(-sims[top_idx])]

            best: dict[int, float] = {}
            best_chunk_text: dict[int, str] = {}
            best_chunk_rowid: dict[int, int] = {}
            for i in top_idx:
                nrid = int(self._scope_note_rowids[i])
                crid = int(self._scope_chunk_rowids[i])
                score = float(sims[i])
                if score > best.get(nrid, -1.0):
                    best[nrid] = score
                    best_chunk_rowid[nrid] = crid
                    best_chunk_text[nrid] = self._scope_chunk_text.get(crid, "")
            order = sorted(best, key=lambda r: best[r], reverse=True)[:n]
            return order, best_chunk_text, best_chunk_rowid

    return _ScopedInt8Index


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--qrels", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--map", default=None)
    ap.add_argument("--int8-model-dir", required=True)
    ap.add_argument("--int8-vectors", required=True)
    ap.add_argument("--scope-meta", required=True)
    ap.add_argument("--speed-bench", default=None)
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

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
    assert not (set(scorable) & forbidden), "H36 barrier violated"

    vault_root = str(Path(args.vault).resolve())

    # -- fp32 arm: the real live production index, unmodified. ---------------
    from brain.index import BrainIndex
    from brain.embed import OnnxEmbedder

    os.environ.setdefault("BRAIN_REQUIRE_REAL_EMBEDDER", "1")
    fp32_index = BrainIndex(db_path=Path(args.index), read_only=True)
    print(f"fp32 arm embedder resolved: {fp32_index.embedder.model_id} "
          f"(dim={fp32_index.embedder.dim})")

    # -- int8 arm: same sqlite file (lexical + zone metadata identical), -----
    # -- dense leg swapped for the scoped int8 cosine searcher. --------------
    npz = np.load(args.int8_vectors)
    vectors = npz["vectors"]
    chunk_rowids = npz["chunk_rowids"]
    note_rowids = npz["note_rowids"]
    encode_seconds = float(npz["encode_seconds"][0]) if "encode_seconds" in npz else None
    chunk_text_by_rowid = {c["chunk_rowid"]: c["text"] for c in scope_meta["chunks"]}

    int8_embedder = OnnxEmbedder(local_dir=args.int8_model_dir, quantization="int8")
    ScopedCls = make_scoped_int8_index_cls()
    int8_index = ScopedCls(db_path=Path(args.index), read_only=True, embedder=int8_embedder)
    int8_index.set_scope(vectors, chunk_rowids, note_rowids, chunk_text_by_rowid, int8_embedder)
    print(f"int8 arm: {vectors.shape[0]} scoped chunk vectors "
          f"({scope_meta['total_scope_notes']} notes, "
          f"{scope_meta['coverage_pct_chunks']}% of full corpus chunks); "
          f"embedder model_id={int8_embedder.model_id}")

    # -- run both arms over the SAME scorable queries. -----------------------
    def _canon(p: str) -> str:
        rel = os.path.relpath(p, vault_root) if os.path.isabs(p) else p
        return pn.normalize(rel, mapping) if mapping else rel

    per_query: dict[str, dict] = {}
    per_query_latency_ms = {"fp32": [], "int8": []}
    for q in scorable:
        text = qmeta[q]["text"]
        rel = qrels_all[q]

        t0 = time.time()
        hits_fp32 = fp32_index.hybrid_search(text, k=args.k)
        dt_fp32 = time.time() - t0
        ranked_fp32 = [_canon(h.path) for h in hits_fp32]

        t0 = time.time()
        hits_int8 = int8_index.hybrid_search(text, k=args.k)
        dt_int8 = time.time() - t0
        ranked_int8 = [_canon(h.path) for h in hits_int8]

        per_query_latency_ms["fp32"].append(dt_fp32 * 1000.0)
        per_query_latency_ms["int8"].append(dt_int8 * 1000.0)

        per_query[q] = {
            "lang": qmeta[q].get("lang"),
            "stratum": qmeta[q].get("stratum"),
            "fp32": {
                "recall@10": _recall_at_k(rel, ranked_fp32, args.k),
                "ndcg@10": _ndcg_at_k(rel, ranked_fp32, args.k),
            },
            "int8": {
                "recall@10": _recall_at_k(rel, ranked_int8, args.k),
                "ndcg@10": _ndcg_at_k(rel, ranked_int8, args.k),
            },
        }

    def _agg(metric: str, arm: str, qids: list[str]) -> float | None:
        vals = [per_query[q][arm][metric] for q in qids if q in per_query]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _delta_ci(metric: str, qids: list[str]):
        deltas = [per_query[q]["int8"][metric] - per_query[q]["fp32"][metric]
                  for q in qids if q in per_query]
        if not deltas:
            return None
        ci = st.bootstrap_ci(deltas, b=10000, seed=7)
        mean = round(sum(deltas) / len(deltas), 4)
        return {
            "n": len(deltas), "mean_delta": mean,
            "bootstrap_ci_95": [round(ci.ci_lower, 4), round(ci.ci_upper, 4)],
            "ni_bound": NI_BOUND,
            "ni_verdict_point_test": mean >= NI_BOUND,
            "kind": "descriptive_effect_size_interval (H19 — NOT the primary "
                    "confirmatory significance test; adoption-validation fold, "
                    "not held-out; s11b owns the one confirmatory paired test). "
                    "PT gate uses the 'point' convention (mean delta >= bound, "
                    "CI advisory) per eval/gate.py's own n=12-14 caveat, since "
                    "PT n here is smaller still.",
        }

    langs = sorted({qmeta[q].get("lang", "?") for q in scorable})
    strata = sorted({qmeta[q].get("stratum", "?") for q in scorable})

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

    by_stratum = {}
    for st_name in strata:
        qids = [q for q in scorable if qmeta[q].get("stratum") == st_name]
        by_stratum[st_name] = {
            "n": len(qids),
            "fp32": {"recall@10": _agg("recall@10", "fp32", qids),
                     "ndcg@10": _agg("ndcg@10", "fp32", qids)},
            "int8": {"recall@10": _agg("recall@10", "int8", qids),
                     "ndcg@10": _agg("ndcg@10", "int8", qids)},
            "delta_recall@10": _delta_ci("recall@10", qids),
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

    speed = _load(args.speed_bench) if args.speed_bench and Path(args.speed_bench).exists() else None

    out = {
        "session": "s09", "item": "pf-01",
        "barrier": "H36 — scored on adoption-validation ONLY; held-out untouched (s11b's single touch)",
        "pipeline": "H4 — both arms via BrainIndex.hybrid_search UNMODIFIED "
                    "(RRF k=60, zone-authority prior scope=semantic_only, "
                    "near-dup suppression OFF/default); ONLY the dense-leg "
                    "candidate source + query embedder differ between arms",
        "fp32_arm": {
            "model_id": fp32_index.embedder.model_id, "dim": fp32_index.embedder.dim,
            "index": "live production index (full 96,568-chunk corpus)",
        },
        "int8_arm": {
            "model_id": int8_embedder.model_id, "dim": int8_embedder.dim,
            "index": "scoped candidate corpus (reused from EM-01 bge-m3-scope-corpus.json "
                     "— scope selection is embedder-independent)",
            "scope_summary": {
                k: v for k, v in scope_meta.items()
                if k not in ("chunks", "gold_note_paths", "scope_note_rowids")
            },
            "encode_seconds_measured": encode_seconds,
        },
        "overall": overall,
        "by_language": by_lang,
        "by_stratum": by_stratum,
        "pt_specific_quality_gate": {
            "rule": "H9/H36 orchestrator addendum — gate on PT-specific no-material-"
                    f"recall-loss (NI bound {NI_BOUND:+.2f}), adoption-validation split, "
                    "locked qrels; default keep-fp32 if PT recall drops past the bound",
            "verdict": pt_verdict,
            "detail": pt_gate,
        },
        "latency_ms_per_query_scoped_arm_only": {
            "fp32": {
                "mean": round(sum(per_query_latency_ms["fp32"]) / len(per_query_latency_ms["fp32"]), 1),
                "note": "fp32 arm here is the FULL production index (96,568 chunks) — "
                        "NOT a scoped comparison; this number is not the primary latency "
                        "evidence (see docs/eval-bench/int8-latency.md / int8-warm-latency-*.json "
                        "for the controlled 58-query warm-latency A/B)",
            },
            "int8": {
                "mean": round(sum(per_query_latency_ms["int8"]) / len(per_query_latency_ms["int8"]), 1),
                "note": "int8 arm dense leg searches only the "
                        f"{vectors.shape[0]}-chunk SCOPED corpus (brute-force numpy cosine) — "
                        "not comparable 1:1 to a full-corpus deployment latency number",
            },
        },
        "feasibility_speed_bench": speed,
        "_qrels_lock": "_evidence/s01/qrels_adjudicated.json (kappa=0.873, 83/102 usable)",
        "_per_query": per_query,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(overall, indent=2))
    print(json.dumps(by_lang, indent=2, ensure_ascii=False))
    print(f"PT gate verdict: {pt_verdict}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
