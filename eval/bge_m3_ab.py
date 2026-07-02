#!/usr/bin/env python3
"""EM-01 (s06) — BGE-M3 vs multilingual-e5-small A/B on the SAME s05-fixed
hybrid retrieval pipeline. H36: scored on the ADOPTION-VALIDATION split only
(`_evidence/s01/pt-split.json`) — never held-out (that split is s11b's single
touch). H4: both arms go through `BrainIndex.hybrid_search` UNMODIFIED (RRF
fusion + zone-authority prior + near-dup suppression, which is OFF by
default and left off here) — the ONLY thing that differs between arms is
where the DENSE candidate list comes from:

  * e5-small arm — `BrainIndex(read_only=True)` pointed at the LIVE
    production index (`_workspace/live-vault/.brain/index.sqlite`); dense
    leg = the real sqlite-vec search over the full 96,568-chunk corpus.
  * BGE-M3 arm   — `ScopedBgeM3Index` (subclasses `BrainIndex`, overrides
    ONLY `_dense_ranked`) pointed at the SAME sqlite file (so lexical FTS5 +
    zone metadata are IDENTICAL and embedder-independent); dense leg = a
    brute-force cosine search over the pre-encoded SCOPED chunk corpus
    (`bge_m3_scope_corpus.py` + `bge_m3_encode_scope.py` — see H9 feasibility
    note there for why full-corpus BGE-M3 indexing is out of scope).

Usage (REAL embedders, on-host):
  BRAIN_REQUIRE_REAL_EMBEDDER=1 \
  .venv-embed/bin/python eval/bge_m3_ab.py \
    --golden _evidence/s01/pt-golden-set.json \
    --split _evidence/s01/pt-split.json \
    --qrels _evidence/s01/qrels_adjudicated.json \
    --vault _workspace/live-vault \
    --index _workspace/live-vault/.brain/index.sqlite \
    --map _evidence/cutover-s10/path-map.json \
    --bge-model-dir _evidence/pt-bench/bge-m3-model/models--BAAI--bge-m3/snapshots/<hash> \
    --bge-vectors _evidence/pt-bench/bge-m3-scope-vectors.npz \
    --scope-meta _evidence/pt-bench/bge-m3-scope-corpus.json \
    --speed-bench _evidence/pt-bench/bge-m3-speed.json \
    --out _evidence/pt-bench/bge-m3-ab.json
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
import bge_m3_embedder as bm  # noqa: E402


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


def make_scoped_bge_index_cls():
    from brain.index import BrainIndex

    class _ScopedBgeM3Index(BrainIndex):
        """`BrainIndex` with ONLY `_dense_ranked` overridden — every other
        method (lexical FTS5, zone-authority prior, near-dup suppression,
        RRF fusion in `hybrid_search`) runs the UNMODIFIED production code
        against the SAME sqlite file (H4)."""

        def set_scope(self, vectors: np.ndarray, chunk_rowids: np.ndarray,
                      note_rowids: np.ndarray, chunk_text_by_rowid: dict[int, str],
                      bge_embedder) -> None:
            self._scope_vectors = vectors  # (N, 1024) float32, L2-normalised
            self._scope_chunk_rowids = chunk_rowids
            self._scope_note_rowids = note_rowids
            self._scope_chunk_text = chunk_text_by_rowid
            self._bge_embedder = bge_embedder

        def _dense_ranked(self, query: str, n: int):
            qvec = np.asarray(self._bge_embedder.embed(query, is_query=True), dtype=np.float32)
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

    return _ScopedBgeM3Index


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--qrels", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--map", default=None)
    ap.add_argument("--bge-model-dir", required=True)
    ap.add_argument("--bge-vectors", required=True)
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

    # -- e5-small arm: the real live production index, unmodified. ----------
    from brain.index import BrainIndex

    os.environ.setdefault("BRAIN_REQUIRE_REAL_EMBEDDER", "1")
    e5_index = BrainIndex(db_path=Path(args.index), read_only=True)
    print(f"e5-small arm embedder resolved: {e5_index.embedder.model_id} "
          f"(dim={e5_index.embedder.dim})")

    # -- BGE-M3 arm: same sqlite file (lexical + zone metadata identical), --
    # -- dense leg swapped for the scoped BGE-M3 cosine searcher. -----------
    npz = np.load(args.bge_vectors)
    vectors = npz["vectors"]
    chunk_rowids = npz["chunk_rowids"]
    note_rowids = npz["note_rowids"]
    encode_seconds = float(npz["encode_seconds"][0]) if "encode_seconds" in npz else None
    chunk_text_by_rowid = {c["chunk_rowid"]: c["text"] for c in scope_meta["chunks"]}

    bge_embedder = bm.build_embedder(args.bge_model_dir, max_length=512)
    ScopedCls = make_scoped_bge_index_cls()
    bge_index = ScopedCls(db_path=Path(args.index), read_only=True, embedder=bge_embedder)
    bge_index.set_scope(vectors, chunk_rowids, note_rowids, chunk_text_by_rowid, bge_embedder)
    print(f"BGE-M3 arm: {vectors.shape[0]} scoped chunk vectors "
          f"({scope_meta['total_scope_notes']} notes, "
          f"{scope_meta['coverage_pct_chunks']}% of full corpus chunks)")

    # -- run both arms over the SAME scorable queries. -----------------------
    def _canon(p: str) -> str:
        rel = os.path.relpath(p, vault_root) if os.path.isabs(p) else p
        return pn.normalize(rel, mapping) if mapping else rel

    per_query: dict[str, dict] = {}
    t_e5_total = 0.0
    t_bge_total = 0.0
    per_query_latency_ms = {"e5_small": [], "bge_m3": []}
    for q in scorable:
        text = qmeta[q]["text"]
        rel = qrels_all[q]

        t0 = time.time()
        hits_e5 = e5_index.hybrid_search(text, k=args.k)
        dt_e5 = time.time() - t0
        t_e5_total += dt_e5
        ranked_e5 = [_canon(h.path) for h in hits_e5]

        t0 = time.time()
        hits_bge = bge_index.hybrid_search(text, k=args.k)
        dt_bge = time.time() - t0
        t_bge_total += dt_bge
        ranked_bge = [_canon(h.path) for h in hits_bge]

        per_query_latency_ms["e5_small"].append(dt_e5 * 1000.0)
        per_query_latency_ms["bge_m3"].append(dt_bge * 1000.0)

        per_query[q] = {
            "lang": qmeta[q].get("lang"),
            "stratum": qmeta[q].get("stratum"),
            "e5_small": {
                "recall@10": _recall_at_k(rel, ranked_e5, args.k),
                "ndcg@10": _ndcg_at_k(rel, ranked_e5, args.k),
            },
            "bge_m3": {
                "recall@10": _recall_at_k(rel, ranked_bge, args.k),
                "ndcg@10": _ndcg_at_k(rel, ranked_bge, args.k),
            },
        }

    def _agg(metric: str, arm: str, qids: list[str]) -> float | None:
        vals = [per_query[q][arm][metric] for q in qids if q in per_query]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _delta_ci(metric: str, qids: list[str]):
        deltas = [per_query[q]["bge_m3"][metric] - per_query[q]["e5_small"][metric]
                  for q in qids if q in per_query]
        if not deltas:
            return None
        ci = st.bootstrap_ci(deltas, b=10000, seed=7)
        return {
            "n": len(deltas), "mean_delta": round(sum(deltas) / len(deltas), 4),
            "bootstrap_ci_95": [round(ci.ci_lower, 4), round(ci.ci_upper, 4)],
            "kind": "descriptive_effect_size_interval (H19 — NOT the primary "
                    "confirmatory significance test; adoption-validation fold, "
                    "not held-out; s11b owns the one confirmatory paired test)",
        }

    langs = sorted({qmeta[q].get("lang", "?") for q in scorable})
    strata = sorted({qmeta[q].get("stratum", "?") for q in scorable})

    by_lang = {}
    for lang in langs:
        qids = [q for q in scorable if qmeta[q].get("lang") == lang]
        by_lang[lang] = {
            "n": len(qids),
            "e5_small": {"recall@10": _agg("recall@10", "e5_small", qids),
                         "ndcg@10": _agg("ndcg@10", "e5_small", qids)},
            "bge_m3": {"recall@10": _agg("recall@10", "bge_m3", qids),
                       "ndcg@10": _agg("ndcg@10", "bge_m3", qids)},
            "delta_recall@10": _delta_ci("recall@10", qids),
            "delta_ndcg@10": _delta_ci("ndcg@10", qids),
        }

    by_stratum = {}
    for st_name in strata:
        qids = [q for q in scorable if qmeta[q].get("stratum") == st_name]
        by_stratum[st_name] = {
            "n": len(qids),
            "e5_small": {"recall@10": _agg("recall@10", "e5_small", qids),
                         "ndcg@10": _agg("ndcg@10", "e5_small", qids)},
            "bge_m3": {"recall@10": _agg("recall@10", "bge_m3", qids),
                       "ndcg@10": _agg("ndcg@10", "bge_m3", qids)},
            "delta_recall@10": _delta_ci("recall@10", qids),
        }

    overall = {
        "n": len(scorable),
        "e5_small": {"recall@10": _agg("recall@10", "e5_small", scorable),
                     "ndcg@10": _agg("ndcg@10", "e5_small", scorable)},
        "bge_m3": {"recall@10": _agg("recall@10", "bge_m3", scorable),
                   "ndcg@10": _agg("ndcg@10", "bge_m3", scorable)},
        "delta_recall@10": _delta_ci("recall@10", scorable),
        "delta_ndcg@10": _delta_ci("ndcg@10", scorable),
    }

    speed = _load(args.speed_bench) if args.speed_bench and Path(args.speed_bench).exists() else None

    out = {
        "session": "s06", "item": "em-01",
        "barrier": "H36 — scored on adoption-validation ONLY; held-out untouched (s11b's single touch)",
        "pipeline": "H4 — both arms via BrainIndex.hybrid_search UNMODIFIED "
                    "(RRF k=60, zone-authority prior scope=semantic_only, "
                    "near-dup suppression OFF/default); ONLY the dense-leg "
                    "candidate source differs between arms",
        "e5_small_arm": {
            "model_id": e5_index.embedder.model_id, "dim": e5_index.embedder.dim,
            "index": "live production index (full 96,568-chunk corpus)",
        },
        "bge_m3_arm": {
            "model_id": "BAAI/bge-m3", "dim": 1024,
            "index": "scoped candidate corpus (see scope-meta)",
            "scope_summary": {
                k: v for k, v in scope_meta.items()
                if k not in ("chunks", "gold_note_paths", "scope_note_rowids")
            },
            "encode_seconds_measured": encode_seconds,
        },
        "overall": overall,
        "by_language": by_lang,
        "by_stratum": by_stratum,
        "latency_ms_per_query": {
            "e5_small": {
                "mean": round(sum(per_query_latency_ms["e5_small"]) / len(per_query_latency_ms["e5_small"]), 1),
                "p95": round(sorted(per_query_latency_ms["e5_small"])[int(len(per_query_latency_ms["e5_small"]) * 0.95) - 1], 1),
                "note": "in-process hybrid_search wall time incl. query embed + FTS5 + dense search; "
                        "e5 dense leg searches the FULL 96,568-chunk index",
            },
            "bge_m3": {
                "mean": round(sum(per_query_latency_ms["bge_m3"]) / len(per_query_latency_ms["bge_m3"]), 1),
                "p95": round(sorted(per_query_latency_ms["bge_m3"])[int(len(per_query_latency_ms["bge_m3"]) * 0.95) - 1], 1),
                "note": "same wall-time definition; BGE-M3 dense leg searches only the "
                        f"{vectors.shape[0]}-chunk SCOPED corpus (brute-force numpy cosine) — "
                        "query-time latency here is NOT comparable 1:1 to a full-corpus BGE-M3 "
                        "deployment; see speed-bench for the load-bearing feasibility number "
                        "(per-chunk INDEXING throughput, which does scale with corpus size)",
            },
        },
        "feasibility_speed_bench": speed,
        "licence_and_storage": {
            "bge_m3": {"licence": "MIT (BAAI/bge-m3 model card)", "dim": 1024,
                       "params_m": 568, "onnx_fp32_size_gb": 2.1},
            "e5_small": {"licence": "MIT (intfloat/multilingual-e5-small model card)",
                         "dim": 384, "params_m": 118, "onnx_fp32_size_mb": 470},
            "storage_delta": "1024/384 = 2.67x wider vectors -> ~2.67x larger vector "
                              "index on disk/RAM for the SAME corpus, before any int8/MRL "
                              "mitigation (BGE-M3 is NOT MRL-trained, so no free truncation "
                              "like Arctic/Qwen3 — a narrower stored dim would need PCA/other "
                              "lossy reduction, unverified here)",
        },
        "_qrels_lock": "_evidence/s01/qrels_adjudicated.json (kappa=0.873, 83/102 usable)",
        "_per_query": per_query,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(overall, indent=2))
    print(json.dumps(by_lang, indent=2, ensure_ascii=False))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
