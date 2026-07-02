#!/usr/bin/env python3
"""Capture a NEW-system (brain) ranx Run over a golden set.

Drives the brain retriever IN-PROCESS (core.hybrid_search / core.grep) for each
query, normalises every hit to the canonical source path, times each query, and
writes a run file the harness consumes.

Egress note: the eval measures the RETRIEVAL PRIMITIVE, so we call the core
retriever directly (no deny-by-default egress filter) — otherwise withheld notes
would be dropped from the ranking and recall would be understated relative to SC
(which applies no egress gate). The classification egress gate is a separate
shipped behaviour, tested elsewhere.

Modes (let one corpus produce two comparable runs for the harness self-test):
  hybrid         RRF BM25+dense (the new system's default)
  hybrid-rerank  hybrid + cross-encoder rerank
  grep           lexical-only (a deliberately weaker baseline)

Usage:
  python3 eval/capture_brain_run.py --golden eval/golden_set_dev.json \
      --vault vault --mode hybrid-rerank --system new-brain \
      --out eval/runs/new_brain_dev.json
  # real-subset: pass --map (brain_rel_path -> source_path) + --source-vault for
  # temporal version resolution.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import path_normalize as pn  # noqa: E402


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--vault", required=True, help="brain vault to retrieve over")
    ap.add_argument("--mode", choices=["hybrid", "hybrid-rerank", "agentic", "grep"], default="hybrid")
    ap.add_argument("--system", default="new-brain")
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=20)
    ap.add_argument("--reformulations", default=None,
                    help="JSON {qid: en_reformulation} for --mode agentic (multi-query fan-out)")
    ap.add_argument("--map", default=None, help="JSON {brain_rel_path: source_path}")
    ap.add_argument("--source-vault", default=None,
                    help="real source vault root for temporal version resolution")
    ap.add_argument("--rebuild", action="store_true", help="rebuild index before capture")
    ap.add_argument("--rerank-fused", action="store_true",
                    help="agentic mode: rerank the wide fused pool with the cross-encoder "
                         "(converts fan-out recall@20 into top-k precision)")
    ap.add_argument("--fused-pool", type=int, default=20,
                    help="size of the fused pool reranked when --rerank-fused")
    args = ap.parse_args()

    from brain.core import BrainCore

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qmeta = {q["id"]: q for q in golden["queries"]}
    mapping = json.loads(Path(args.map).read_text(encoding="utf-8")) if args.map else None
    reforms = (json.loads(Path(args.reformulations).read_text(encoding="utf-8"))
               if args.reformulations else {})

    vault_root = str(Path(args.vault).resolve())
    core = BrainCore(vault=vault_root)
    if args.rebuild:
        info = core.rebuild()
        print(f"rebuilt: {info.get('indexed')} notes, backend={info.get('backend')}, "
              f"model={info.get('embed_model')}")

    try:
        status = core.status()
        index_state = {"index": status.get("index"), "mode": args.mode, "vault": vault_root}
    except Exception:
        index_state = {"mode": args.mode, "vault": vault_root}

    runs: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}

    for qid, q in qmeta.items():
        text = q["text"]
        t0 = time.perf_counter()
        if args.mode == "grep":
            items = core.grep(text, k=args.k)
            hits = [(it["path"], 1.0 / (i + 1)) for i, it in enumerate(items)]
        elif args.mode == "agentic":
            # Multi-query fan-out: original query + (if present) its reformulation.
            # Models an agent that reformulates cross-boundary queries before
            # retrieving. Queries with no reformulation degrade to single-query.
            variants = [text]
            rf = reforms.get(qid)
            if rf and rf.strip() and rf.strip() != text.strip():
                variants.append(rf)
            hh = core.search_multi(variants, k=args.k,
                                   rerank_fused=args.rerank_fused,
                                   fused_pool=args.fused_pool)
            hits = [(h.path, float(h.score)) for h in hh]
        else:
            rerank = args.mode == "hybrid-rerank"
            hh = core.hybrid_search(text, k=args.k, rerank=rerank)
            hits = [(h.path, float(h.score)) for h in hh]
        dt = (time.perf_counter() - t0) * 1000.0
        latency[qid] = round(dt, 2)

        doc_scores: dict[str, float] = {}
        for raw, score in hits:
            rel = os.path.relpath(raw, vault_root) if os.path.isabs(raw) else raw
            src = pn.normalize(rel, mapping)
            if q["stratum"] == "temporal":
                vroot = args.source_vault or vault_root
                vstate, _ = pn.resolve_version(src, vroot)
                src = f"{src}#{vstate}"
            # keep best score per doc
            if src not in doc_scores or score > doc_scores[src]:
                doc_scores[src] = score
        runs[qid] = doc_scores

    out = {
        "system": args.system,
        "captured": _iso(),
        "index_state": index_state,
        "k": args.k,
        "runs": runs,
        "latency_ms": latency,
        "scope": {"queries_captured": sorted(runs),
                  "n": len(runs),
                  "egress": "retrieval-primitive (no egress filter)",
                  "mapped": bool(mapping)},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"captured {len(runs)} queries [{args.system}/{args.mode}] -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
