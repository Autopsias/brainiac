#!/usr/bin/env bash
# UPG-06 quality gate v2 — agentic + rerank-fused (brain_expanded methodology):
# the reranker reranks a WIDE fused pool (fanout), so gte vs jina actually differ.
# (v1 used plain hybrid-rerank; the narrow top-20 pool was the bottleneck, so
#  recall@10 was identical for both rerankers = pure-hybrid. Inconclusive.)
# Same e5-small retrieval + reformulations + zone-weights for both; only the
# reranker differs. jina-v2 used here only as the measurement baseline (CC-BY-NC
# permits research/eval use).
set -uo pipefail
cd "$(dirname "$0")/.."
V=.venv-embed/bin/python
export BRAIN_INDEX_DIR=_evidence/s10/brain-index-e5small
export BRAIN_EMBED_MODEL=intfloat/multilingual-e5-small BRAIN_EMBED_DIM=384
export BRAIN_FASTEMBED_CACHE=.fastembed_cache BRAIN_ZONE_SCOPE=semantic_only
export BRAIN_ZONE_WEIGHTS='{"10 People":3.0,"20 Companies":3.0,"30 Projects":3.0,"60 Concepts":3.0,"70 Decisions":3.0}'
VAULT=/Users/user/Downloads/Example-Vault
GOLDEN=eval/golden_set.json
QRELS=eval/qrels/qrels.json
REFS=eval/_expand/reformulations_new.json
COMMON="--golden $GOLDEN --vault $VAULT --mode agentic --reformulations $REFS --rerank-fused --fused-pool 20 -k 20"

echo "=== [1/3] capture jina-v2 BASELINE (agentic + rerank-fused + w3) ==="
BRAIN_RERANKER_PREFER=gte BRAIN_RERANKER_MODEL=jinaai/jina-reranker-v2-base-multilingual \
  $V eval/capture_brain_run.py $COMMON --system brain-jina-v2-fused \
  --out eval/runs/brain_jina_v2_fused.json 2>&1 | tail -2

echo "=== [2/3] capture gte CANDIDATE (agentic + rerank-fused + w3) ==="
BRAIN_RERANKER_PREFER=onnx \
  $V eval/capture_brain_run.py $COMMON --system brain-gte-fused \
  --out eval/runs/brain_gte_fused.json 2>&1 | tail -2

echo "=== [3/3] gate_s11: gte (candidate) vs jina-v2 (baseline) ==="
$V eval/gate_s11.py \
  --baseline eval/runs/brain_jina_v2_fused.json --candidate eval/runs/brain_gte_fused.json \
  --golden "$GOLDEN" --qrels "$QRELS" \
  --label "UPG-06 gte (Apache) vs jina-v2 (CC-BY-NC) — agentic+rerank-fused" \
  --out _evidence/s11/upg06_gte_vs_jina_gate.json 2>&1 || true
echo "=== done ==="
