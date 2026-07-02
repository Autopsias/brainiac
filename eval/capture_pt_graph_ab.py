#!/usr/bin/env python3
"""S10 KG-01 — capture the FLAT vs GRAPH-AUGMENTED A/B over the ef-02 PT golden
set (non-held-out scope only, H37) in ONE process against ONE loaded index.

Both runs share the same ``BrainCore`` / index / vault / path-map so the A/B is
confounder-free (no index drift between the "before" and "after" capture). The
only difference per query is the retrieval call:

  * ``flat``  -> ``core.hybrid_search`` (the shipped default, s05-fixed pipeline)
  * ``graph`` -> ``core.hybrid_search_graph`` (RET-06; gated multi-hop expansion)

Run files are written in the exact schema ``eval/harness.py`` consumes, so the
downstream A/B scorecard + gate are unchanged.

Usage (REAL embedder, on-host):
  BRAIN_REQUIRE_REAL_EMBEDDER=1 \
  .venv-embed/bin/python eval/capture_pt_graph_ab.py \
    --golden _evidence/s01/pt-golden-set.json \
    --split _evidence/s01/pt-split.json \
    --vault _workspace/live-vault \
    --map _evidence/cutover-s10/path-map.json \
    --graph-weight 0.5 --depth 2 -k 20 \
    --out-flat _evidence/pt-bench/kg-flat-run.json \
    --out-graph _evidence/pt-bench/kg-graph-run.json
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
from pt_scope import load_scope  # noqa: E402


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_hits(hits, vault_root, mapping):
    doc_scores: dict[str, float] = {}
    for h in hits:
        raw = h.path
        rel = os.path.relpath(raw, vault_root) if os.path.isabs(raw) else raw
        src = pn.normalize(rel, mapping)
        if src not in doc_scores or float(h.score) > doc_scores[src]:
            doc_scores[src] = float(h.score)
    return doc_scores


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--map", default=None)
    ap.add_argument("-k", type=int, default=20)
    ap.add_argument("--graph-weight", type=float, default=0.5)
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--seed-flat-top", type=int, default=3)
    ap.add_argument("--flat-pool", type=int, default=30)
    ap.add_argument("--out-flat", required=True)
    ap.add_argument("--out-graph", required=True)
    ap.add_argument("--trace", default=None, help="optional per-query gate trace JSON")
    args = ap.parse_args()

    from brain.core import BrainCore

    golden = load_scope(args.golden, args.split)
    mapping = json.loads(Path(args.map).read_text(encoding="utf-8")) if args.map else None
    vault_root = str(Path(args.vault).resolve())
    core = BrainCore(vault=vault_root)
    try:
        status = core.status()
        index_state = {"index": status.get("index"), "vault": vault_root}
    except Exception:
        index_state = {"vault": vault_root}

    flat_runs: dict[str, dict[str, float]] = {}
    graph_runs: dict[str, dict[str, float]] = {}
    flat_lat: dict[str, float] = {}
    graph_lat: dict[str, float] = {}
    traces: dict[str, dict] = {}
    n_fired = 0

    for q in golden["queries"]:
        qid, text = q["id"], q["text"]

        t0 = time.perf_counter()
        fh = core.hybrid_search(text, k=args.k)
        flat_lat[qid] = round((time.perf_counter() - t0) * 1000.0, 2)
        flat_runs[qid] = _run_hits(fh, vault_root, mapping)

        t0 = time.perf_counter()
        gh, trace = core.hybrid_search_graph(
            text, k=args.k, depth=args.depth, graph_weight=args.graph_weight,
            seed_flat_top=args.seed_flat_top, flat_pool=args.flat_pool,
            return_trace=True,
        )
        graph_lat[qid] = round((time.perf_counter() - t0) * 1000.0, 2)
        graph_runs[qid] = _run_hits(gh, vault_root, mapping)
        traces[qid] = {"stratum": q.get("stratum"), **trace}
        if trace.get("fired"):
            n_fired += 1

    scope = {
        "queries_captured": sorted(flat_runs),
        "n": len(flat_runs),
        "golden_scope": golden["_scope"],
        "canonical_key": "plain vault-relative source path (ef-02 schema)",
        "mapped": bool(mapping),
    }
    common = {"captured": _iso(), "index_state": index_state, "k": args.k, "scope": scope}

    Path(args.out_flat).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_flat).write_text(json.dumps({
        "system": "brain (hybrid, flat — shipped default)",
        **common, "runs": flat_runs, "latency_ms": flat_lat,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    Path(args.out_graph).write_text(json.dumps({
        "system": f"brain (hybrid + graph multi-hop, w={args.graph_weight}, depth={args.depth})",
        **common, "runs": graph_runs, "latency_ms": graph_lat,
        "graph_params": {"graph_weight": args.graph_weight, "depth": args.depth,
                         "seed_flat_top": args.seed_flat_top, "flat_pool": args.flat_pool,
                         "queries_fired": n_fired},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.trace:
        Path(args.trace).write_text(json.dumps(traces, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")

    print(f"captured {len(flat_runs)} queries; graph gate FIRED on {n_fired} "
          f"(w={args.graph_weight}, depth={args.depth})")
    print(f"  flat  -> {args.out_flat}")
    print(f"  graph -> {args.out_graph}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
