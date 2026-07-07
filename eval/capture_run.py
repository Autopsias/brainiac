#!/usr/bin/env python3
"""AUT-04 — capture ONE brain hybrid-search run over the golden set, with the
four knobs the autoresearch skill (.claude/skills/autoresearch/) tunes exposed
as first-class flags: ``--rrf-k``, ``--rerank``/``--no-rerank``,
``--rerank-top``, ``-k``.

Trimmed, parameterised sibling of ``_archive/eval/capture_brain_run.py``
(session s05's frozen-SC-vs-brain A/B) — same run-file schema (so
``eval/harness_direct.py`` + ``eval/gate.py`` consume the output unchanged),
same ``BrainCore.hybrid_search`` call, but only the hybrid/hybrid-rerank path
survives (grep/agentic modes belonged to the retired SC comparison, not
parameter self-tuning) and rrf_k/rerank_top are runtime flags instead of
whatever ``BrainCore.hybrid_search``'s own defaults happen to be.

Usage (one capture per parameter set — call it once for "current", once for
"candidate" each autoresearch iteration):

    python3 eval/capture_run.py --golden eval/golden_set.json \\
        --vault "${BRAIN_VAULT:-vault}" --rrf-k 60 --rerank-top 15 -k 20 \\
        --system baseline --out eval/runs/autoresearch-current.json

Pass --rebuild once per vault session if the index hasn't been built with
vector support yet (``no such table: vec_index`` means this).
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
    ap.add_argument("--vault", default=os.environ.get("BRAIN_VAULT", "vault"),
                    help="vault to retrieve over (default: $BRAIN_VAULT or ./vault)")
    ap.add_argument("--system", required=True, help="run label, e.g. 'current' or 'rrf40'")
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=20,
                    help="agentic-budget default (matches gate.py --metric recall@20)")
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--rerank", dest="rerank", action="store_true", default=False)
    ap.add_argument("--no-rerank", dest="rerank", action="store_false")
    ap.add_argument("--rerank-top", type=int, default=15)
    ap.add_argument("--map", default=None,
                    help="optional JSON {brain_rel_path: canonical_source_path}")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the index before capture (needed once if the "
                         "vault's index predates vector support)")
    args = ap.parse_args()

    from brain.core import BrainCore

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qmeta = {q["id"]: q for q in golden["queries"]}
    mapping = json.loads(Path(args.map).read_text(encoding="utf-8")) if args.map else None

    vault_root = str(Path(args.vault).resolve())
    core = BrainCore(vault=vault_root)
    if args.rebuild:
        info = core.rebuild()
        print(f"rebuilt: {info.get('indexed')} notes, backend={info.get('backend')}, "
              f"model={info.get('embed_model')}")

    params = {"rrf_k": args.rrf_k, "rerank": args.rerank, "rerank_top": args.rerank_top}
    try:
        status = core.status()
        index_state = {"index": status.get("index"), "params": params, "vault": vault_root}
    except Exception:
        index_state = {"params": params, "vault": vault_root}

    runs: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}

    for qid, q in qmeta.items():
        t0 = time.perf_counter()
        hits = core.hybrid_search(q["text"], k=args.k, rerank=args.rerank,
                                  rerank_top=args.rerank_top, rrf_k=args.rrf_k)
        latency[qid] = round((time.perf_counter() - t0) * 1000.0, 2)

        doc_scores: dict[str, float] = {}
        for h in hits:
            rel = os.path.relpath(h.path, vault_root) if os.path.isabs(h.path) else h.path
            src = pn.normalize(rel, mapping)
            if q.get("stratum") == "temporal":
                vstate, _ = pn.resolve_version(src, vault_root)
                src = f"{src}#{vstate}"
            if src not in doc_scores or h.score > doc_scores[src]:
                doc_scores[src] = h.score
        runs[qid] = doc_scores

    out = {
        "system": args.system,
        "captured": _iso(),
        "index_state": index_state,
        "k": args.k,
        "runs": runs,
        "latency_ms": latency,
        "scope": {"queries_captured": sorted(runs), "n": len(runs),
                  "egress": "retrieval-primitive (no egress filter)",
                  "mapped": bool(mapping)},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print(f"captured {len(runs)} queries [{args.system}] rrf_k={args.rrf_k} "
          f"rerank={args.rerank} rerank_top={args.rerank_top} k={args.k} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
