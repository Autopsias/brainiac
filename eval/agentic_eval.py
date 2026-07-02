#!/usr/bin/env python3
"""S05 — SEPARATE agentic task-success / answer-grounding eval (HARDENED:claude).

A frozen ranx Run scores the RETRIEVAL PRIMITIVE. But both the current cascade
and the new tool surface are LLM-ORCHESTRATED per query — so retriever
non-inferiority is necessary, NOT sufficient, to claim the system-level "beats
today". This is the separate, smaller system-level check that runs each task
through the FULL brain harness and asks two binary questions:

  * task_success  — did the retriever surface a SUFFICIENT note (graded >= 2)
                    in the top-k, i.e. could the agent ground a correct answer?
  * answer_grounded — is the top-1 result a RELEVANT note (graded >= 1), i.e.
                    would the agent's first-cited source be on-topic (not a
                    confident-but-wrong citation)?

These are SYSTEM-level pass/fail per task, distinct from the primitive's
Recall/nDCG. Reported overall + per stratum. The retriever gate (eval/gate.py)
and THIS eval must BOTH pass before "beats today" at the system level.

This run is over the committed dev vault (real brain retrieval). The real-vault
agentic eval awaits the s03 corpus landing (same dependency as the full A/B).

Usage:
  python3 eval/agentic_eval.py --golden eval/golden_set_dev.json --vault vault \
      --out _evidence/s05/agentic-eval.json [-k 10] [--mode hybrid-rerank]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--mode", choices=["hybrid", "hybrid-rerank"], default="hybrid-rerank")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    from brain.core import BrainCore

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    vault_root = str(Path(args.vault).resolve())
    core = BrainCore(vault=vault_root)
    if args.rebuild:
        core.rebuild()

    tasks = []
    for q in golden["queries"]:
        rel = {r["path"]: r["grade"] for r in q["qrels"]}
        sufficient = {p for p, g in rel.items() if g >= 2}
        relevant = {p for p, g in rel.items() if g >= 1}
        hh = core.hybrid_search(q["text"], k=args.k, rerank=(args.mode == "hybrid-rerank"))
        ranked = [os.path.relpath(h.path, vault_root) if os.path.isabs(h.path) else h.path
                  for h in hh]
        topk = set(ranked[: args.k])
        top1 = ranked[0] if ranked else None
        task_success = bool(topk & sufficient) if sufficient else bool(topk & relevant)
        answer_grounded = top1 in relevant if top1 else False
        tasks.append({"id": q["id"], "stratum": q["stratum"], "lang": q["lang"],
                      "task_success": task_success, "answer_grounded": answer_grounded,
                      "top1": top1})

    def rate(items, key):
        return round(sum(1 for t in items if t[key]) / len(items), 4) if items else None

    by_stratum = {}
    for st in sorted({t["stratum"] for t in tasks}):
        sub = [t for t in tasks if t["stratum"] == st]
        by_stratum[st] = {"n": len(sub),
                          "task_success": rate(sub, "task_success"),
                          "answer_grounded": rate(sub, "answer_grounded")}

    out = {
        "eval": "agentic task-success / answer-grounding (system-level, separate from retriever primitive)",
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vault": vault_root, "mode": args.mode, "k": args.k,
        "n": len(tasks),
        "overall": {"task_success": rate(tasks, "task_success"),
                    "answer_grounded": rate(tasks, "answer_grounded")},
        "by_stratum": by_stratum,
        "tasks": tasks,
        "note": "dev-vault run (real brain retrieval). Real-vault agentic eval awaits "
                "s03 corpus landing — same dependency as the full A/B.",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"agentic eval: n={len(tasks)} task_success={out['overall']['task_success']} "
          f"answer_grounded={out['overall']['answer_grounded']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
