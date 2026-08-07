#!/usr/bin/env python3
"""Rank-correct capture for the rerank arms of BL-02's re-baseline.

``eval/capture_run.py`` records ``doc_scores[doc] = hit.score`` -- but
reranking in ``src/brain/index.py`` REORDERS the returned ``hits`` list
without changing each ``Hit.score`` (the RRF score and the rerank score are
deliberately kept on separate scales -- see ``_rerank_impl``'s comment).
Every downstream scorer (``eval/harness_direct.py``'s ``_ranked()``, and this
session's own ``eval/rebaseline_report.py``) reconstructs rank by re-sorting
on the recorded score, which silently discards a rerank-only reordering:
two runs with IDENTICAL scores but DIFFERENT list order score identically.
Confirmed on the real corpus: after fixing the padding bug so reranking
genuinely applies (verified via ``trace.rerank_applied``), the capture_run.py
artifacts for rerank15/rerank20 still showed only 1/66 and 5/66 queries with
a changed top-10 -- i.e. tie-break noise, not the real reordering.

This script is capture_run.py's derived-index-only path with ONE change: the
recorded per-doc value is a rank-derived synthetic score (``len(hits) -
position``), so sorting by score descending reproduces the ACTUAL returned
order, reranked or not. Same output schema, same fingerprint helper (reused
directly from capture_run.py), same path-normalize/temporal handling
(reused from eval/path_normalize.py) -- a bare capture with this script
reproduces eval/capture_run.py's bare-arm numbers exactly (both reduce to
"rank order" when nothing reorders by tie).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import path_normalize as pn  # noqa: E402
from capture_run import _fingerprint, _iso  # noqa: E402


def _sha256_file(path: str | None) -> str | None:
    import hashlib
    if not path:
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--map", default=None)
    ap.add_argument("--index-db", required=True)
    ap.add_argument("--system", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=20)
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--rerank-top", type=int, required=True)
    ap.add_argument("--embedder", default="auto")
    ap.add_argument("--vector-backend", default="sqlite-vec")
    ap.add_argument("--source-root", default=str(REPO / "src"))
    ap.add_argument("--resume", action="store_true",
                    help="continue from <out>.partial instead of recapturing")
    args = ap.parse_args()

    sys.path.insert(0, args.source_root)
    from brain.index import BrainIndex, rerank_gate_enabled
    from brain.vectors import get_backend
    from brain.embed import get_embedder

    vault_root = str(Path(args.vault).resolve())
    idx = BrainIndex(
        db_path=Path(args.index_db).resolve(),
        backend=get_backend(args.vector_backend),
        embedder=get_embedder(args.embedder),
        read_only=True,
    )

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qmeta = {q["id"]: q for q in golden["queries"]}
    mapping = None
    if args.map:
        mapping_doc = json.loads(Path(args.map).read_text(encoding="utf-8"))
        mapping = mapping_doc.get("mapping", mapping_doc)

    verified_applied = []
    runs: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}
    # A wide-window arm is ~50 min of cross-encoder work. Writing only at the
    # end means a death loses everything and shows nothing while alive (it did,
    # 2026-08-04, on the rerank-50 arm). So: progress to stderr per query, and
    # a partial checkpoint that --resume picks up.
    partial_path = Path(str(args.out) + ".partial")
    if args.resume and partial_path.exists():
        prev = json.loads(partial_path.read_text(encoding="utf-8"))
        runs.update(prev.get("runs", {}))
        latency.update(prev.get("latency_ms", {}))
        verified_applied.extend(prev.get("verified_applied", []))
        print(f"resumed: {len(runs)} queries already captured", file=sys.stderr, flush=True)

    def _checkpoint():
        partial_path.write_text(json.dumps(
            {"runs": runs, "latency_ms": latency, "verified_applied": verified_applied},
            ensure_ascii=False), encoding="utf-8")

    total = len(qmeta)
    for done, (qid, q) in enumerate(qmeta.items(), start=1):
        if qid in runs:
            continue
        t0 = time.perf_counter()
        hits, trace = idx.hybrid_search_with_trace(
            q["text"], k=args.k, rerank=True, rerank_top=args.rerank_top, rrf_k=args.rrf_k,
        )
        latency[qid] = round((time.perf_counter() - t0) * 1000.0, 2)
        verified_applied.append(bool(trace.rerank_applied))

        doc_rank_score: dict[str, float] = {}
        n = len(hits)
        for position, h in enumerate(hits):  # position 0 = best
            rel = os.path.relpath(h.path, vault_root) if os.path.isabs(h.path) else h.path
            src = pn.normalize(rel, mapping)
            if q.get("stratum") == "temporal":
                vstate, _ = pn.resolve_version(rel, vault_root)
                src = f"{src}#{vstate}"
            synthetic_score = float(n - position)  # preserves ACTUAL returned order
            if src not in doc_rank_score or synthetic_score > doc_rank_score[src]:
                doc_rank_score[src] = synthetic_score
        runs[qid] = doc_rank_score
        print(f"[{done}/{total}] {qid} {latency[qid]:.0f}ms "
              f"rerank_applied={bool(trace.rerank_applied)}", file=sys.stderr, flush=True)
        _checkpoint()

    index_stats = idx.stats()
    index_state = {
        "index": index_stats,
        "prebuilt_index": args.index_db,
        "read_only_index": True,
        "fingerprint": _fingerprint(index_stats, args.golden),
        "params": {"rrf_k": args.rrf_k, "rerank": True, "rerank_top": args.rerank_top,
                   # RK-02: this script never disables the gate, so it captures
                   # the SHIPPED path. Stamp the resolved value anyway — a run
                   # file that does not say which rerank path produced it cannot
                   # be read back as evidence of one.
                   "rerank_gate": rerank_gate_enabled(None)},
        "vault": vault_root,
        "source_root": args.source_root,
        "rerank_applied_verified": {
            "true": sum(verified_applied), "false": sum(1 for x in verified_applied if not x),
            "n": len(verified_applied),
        },
        "score_semantics": "rank-derived (n - position), NOT the raw RRF/Hit.score -- "
                            "see this script's module docstring",
        # WHICH map, not just "mapped: true". Measured 2026-08-06: the same
        # engine, index and queries scored overall recall@10 0.523 under a
        # 3,354-entry vault migration map and 0.580 under the 72-entry
        # golden-scoped map, because the doc_key a hit is emitted under decides
        # whether it matches a qrel at all. A run that does not name its map
        # cannot be compared to another run.
        "map": ({"path": args.map, "sha256": _sha256_file(args.map),
                 "entries": len(mapping)} if mapping else None),
    }

    out = {
        "system": args.system,
        "captured": _iso(),
        "index_state": index_state,
        "k": args.k,
        "runs": runs,
        "latency_ms": latency,
        "timing": {"warmup_per_query": 0, "samples_per_query": 1, "statistic": "single"},
        "scope": {"queries_captured": sorted(runs), "n": len(runs),
                  "egress": "retrieval-primitive (no egress filter)", "mapped": bool(mapping)},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"captured {len(runs)} queries [{args.system}] rerank_top={args.rerank_top} "
          f"rerank_applied={sum(verified_applied)}/{len(verified_applied)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
