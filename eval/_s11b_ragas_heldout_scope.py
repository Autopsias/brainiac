#!/usr/bin/env python3
"""S11B — thin scope-shim so the s08/GQ-01 `eval/ragas_eval.py` pipeline
(retrieve / render-generation / render-judge / render-codex-judge /
aggregate) can be reused BYTE-FOR-BYTE, unmodified in its logic, against the
**held-out** fold instead of adoption-validation.

This is the H36 SINGLE held-out read for the generation-quality metric
family. It is deliberately a monkeypatch of `ragas_eval.load_scope` (not an
edit to ragas_eval.py itself) so the s08 committed pipeline stays untouched
and auditable; only the query-scope selection changes.

Usage: identical subcommands to eval/ragas_eval.py, e.g.
  .venv-embed/bin/python eval/_s11b_ragas_heldout_scope.py retrieve \
    --vault _workspace/live-vault --out _evidence/pt-bench/heldout-ragas-contexts.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ragas_eval as R  # noqa: E402


def load_scope_heldout() -> tuple[list[dict], dict, dict]:
    golden = json.loads(R.GOLDEN.read_text(encoding="utf-8"))
    split = json.loads(R.SPLIT.read_text(encoding="utf-8"))
    qrels_doc = json.loads(R.QRELS.read_text(encoding="utf-8"))
    qrels_locked = qrels_doc["qrels"]

    held = set(split["folds"]["held-out"])
    nonheld = (set(split["folds"]["train"]) | set(split["folds"]["dev"])
               | set(split["folds"]["adoption-validation"]))
    usable = set(qrels_locked.keys())
    scope_ids = sorted(held & usable)
    leaked = [q for q in scope_ids if q in nonheld]
    if leaked:
        raise AssertionError(f"H36 barrier violated: {leaked} appear outside held-out")

    qmap = {q["id"]: q for q in golden["queries"]}
    queries = [qmap[i] for i in scope_ids if i in qmap]

    qrels_graded: dict[str, dict[str, int]] = {}
    for qid in scope_ids:
        if qid not in qmap:
            continue
        original_grades = {r["path"]: r["grade"] for r in qmap[qid].get("qrels", [])}
        locked_paths = qrels_locked.get(qid, {})
        qrels_graded[qid] = {path: original_grades.get(path, 2) for path in locked_paths}

    scope_info = {
        "fold": "held-out",
        "held_out_n": len(held),
        "usable_qrels_n_total": len(usable),
        "usable_qrels_agreement_rule": qrels_doc.get("agreement_rule"),
        "usable_qrels_adjudicator": qrels_doc.get("adjudicator"),
        "scope_n": len(queries),
        "excluded_held_out_no_usable_qrels": sorted(held - usable),
        "barrier": ("H36 — the SINGLE held-out read (session s11b), across every metric "
                    "family (retrieval, generation-quality, multi-hop); per H37 label-first "
                    "design this fold was never consulted by any earlier session"),
    }
    return queries, qrels_graded, scope_info


R.load_scope = load_scope_heldout  # monkeypatch: same pipeline, held-out scope

if __name__ == "__main__":
    raise SystemExit(R.main())
