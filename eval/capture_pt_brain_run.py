#!/usr/bin/env python3
"""EF-03 (s02) — capture the brain's NEW-system ranx Run over the ef-02 PT
golden set (non-held-out scope only, per eval/pt_scope.py / H37).

This is a near-copy of eval/capture_brain_run.py with ONE deliberate
divergence: it does NOT apply the temporal `#<version_state>` suffix branch.
That branch targets the OLDER cutover golden set's canonical-key schema
(`source_path#version_state`); the ef-02 golden set's canonical key
(`_evidence/s01/pt-golden-set.json`["canonical_key"]) is the plain
vault-relative source path with NO version suffix (confirmed: its temporal-
stratum qrels in `_evidence/s01/qrels_adjudicated.json` carry no `#` suffix).
Reusing capture_brain_run.py unmodified would silently suffix every temporal
query's doc ids and zero out that class's Recall@k against this qrels file —
a schema mismatch, not a retrieval bug. Everything else (path normalisation
via eval/path_normalize.py, in-process core.hybrid_search, run-file schema)
matches capture_brain_run.py exactly so eval/harness.py consumes it unchanged.

Usage (REAL embedder, on-host):
  BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_INDEX_DIR=_workspace/live-vault/.brain \
  .venv-embed/bin/python eval/capture_pt_brain_run.py \
    --golden _evidence/s01/pt-golden-set.json \
    --split _evidence/s01/pt-split.json \
    --vault _workspace/live-vault \
    --map _evidence/cutover-s10/path-map.json \
    --mode hybrid --system "brain (hybrid, shipped default)" -k 20 \
    --out _evidence/pt-bench/brain-quality-run.json
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True, help="pt-split.json — enforces the H37 held-out barrier")
    ap.add_argument("--vault", required=True, help="brain vault to retrieve over")
    ap.add_argument("--mode", choices=["hybrid", "hybrid-rerank", "grep"], default="hybrid")
    ap.add_argument("--system", default="brain")
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=20)
    ap.add_argument("--map", default=None, help="JSON {brain_rel_path: source_path}")
    args = ap.parse_args()

    from brain.core import BrainCore

    golden = load_scope(args.golden, args.split)
    mapping = json.loads(Path(args.map).read_text(encoding="utf-8")) if args.map else None

    vault_root = str(Path(args.vault).resolve())
    core = BrainCore(vault=vault_root)
    try:
        status = core.status()
        index_state = {"index": status.get("index"), "mode": args.mode, "vault": vault_root}
    except Exception:
        index_state = {"mode": args.mode, "vault": vault_root}

    runs: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}

    for q in golden["queries"]:
        qid, text = q["id"], q["text"]
        t0 = time.perf_counter()
        if args.mode == "grep":
            items = core.grep(text, k=args.k)
            hits = [(it["path"], 1.0 / (i + 1)) for i, it in enumerate(items)]
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
        "scope": {
            "queries_captured": sorted(runs),
            "n": len(runs),
            "egress": "retrieval-primitive (no egress filter)",
            "mapped": bool(mapping),
            "golden_scope": golden["_scope"],
            "canonical_key": "plain vault-relative source path (ef-02 schema, no version suffix)",
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"captured {len(runs)} queries [{args.system}/{args.mode}] -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
