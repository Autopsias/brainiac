#!/usr/bin/env bash
# S05 EVAL-03 — CI ship gate. Exit 0 = cleared to ship; exit 1 = ABORT (halt,
# stay on Obsidian+SC). Wire this as a required CI check.
#
# What it does, in order (any failure -> non-zero exit, blocks the merge):
#   1. Rebuild + VALIDATE the golden set (paths real, cross-lingual disjoint,
#      temporal qrels versioned, per-stratum/lang floors recorded).
#   2. Regenerate ranx qrels from the golden set.
#   3. Run the harness self-test on the committed dev vault (captures TWO real
#      brain runs over the SAME corpus) -> a real scorecard.
#   4. Run the non-inferiority gate on that scorecard -> exit 0/1.
#
# PRODUCTION SWAP (once the s03 corpus migration has LANDED the real vault into
# the new substrate AND pooled relevance judgments exist): replace the dev
# capture in step 3 with
#     capture_sc_baseline.py (frozen) + capture_brain_run.py (real corpus)
# and point the gate at that scorecard. The frozen SC baseline is the committed
# 'today' incumbent (eval/runs/current_sc.frozen.json); the gate NEVER calls a
# live SC MCP (HARDENED:consensus).
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${PY:-.venv/bin/python3}"
EV=_evidence/s05

echo "== [1/4] build + validate golden set =="
"$PY" eval/build_golden_set.py

echo "== [2/4] regenerate qrels =="
"$PY" eval/make_qrels.py --golden eval/golden_set_dev.json --out eval/qrels/qrels_dev.json

echo "== [3/4] capture two real brain runs over the dev vault + score =="
"$PY" eval/capture_brain_run.py --golden eval/golden_set_dev.json --vault vault \
    --mode hybrid-rerank --system current-config --out eval/runs/dev_cur_ci.json --rebuild
"$PY" eval/capture_brain_run.py --golden eval/golden_set_dev.json --vault vault \
    --mode hybrid --system new-config --out eval/runs/dev_new_ci.json
"$PY" eval/harness.py --golden eval/golden_set_dev.json --qrels eval/qrels/qrels_dev.json \
    --current eval/runs/dev_cur_ci.json --new eval/runs/dev_new_ci.json \
    --out "$EV/ci-scorecard.json" --md "$EV/ci-scorecard.md"

echo "== [4/4] non-inferiority ship gate =="
"$PY" eval/gate.py --scorecard "$EV/ci-scorecard.json"
echo "CI GATE: PASS (exit 0)"
