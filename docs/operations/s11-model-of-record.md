> ⚠ **SUPERSEDED 2026-06-29 — the Qwen3 model-of-record recommendation below was OVERTURNED by the S10/S11 closure decision.**
>
> **Final model-of-record (fully open, post-closure):**
> - **Embedder:** `intfloat/multilingual-e5-small` (Apache-2.0) — the CPU-viable choice; 300M+ embedders (Qwen3-Embedding-0.6B, EmbeddingGemma-300M, BGE-M3) are all hardware-capped on locked corporate-HP CPU (~hours to index the 83k-chunk vault). **+ length-bucketed embed batching** (`_embed_length_sorted`, commit `ffe3182`) — ~1.5× over pad-to-batch-max.
> - **Reranker (opt-in):** `gte-multilingual-reranker-base` (Apache-2.0) via `OnnxReranker` (commit `74a7875`) — **replaces the CC-BY-NC `jina-reranker-v2`**, accepting a measured **−2.7 pp recall@10** on the opt-in rerank pass (gate commit `4375313`). Opt-in (default `NoopReranker`) given ~4 s/query on HP.
> - **Ruled out:** Qwen3-Embedding-0.6B + Qwen3-Reranker-0.6B (decoder, ~5 ch/s / ~4 s-per-pair on CPU), EmbeddingGemma-300M (~6 ch/s), BGE-M3 (568M, slower), bge-reranker-v2-m3 (ONNX export failed + slower) — all CPU/latency-dead on the corporate-HP fleet, which is the binding constraint.
>
> The Qwen3 analysis below is retained VERBATIM as the dated UPG-01 historical
> record. Authoritative closure: vault `PLAN-REVIEW-LOG.md` → "S10/S11 CLOSED —
> 2026-06-29". Plan core goal (retriever-superior-to-SC) was achieved at S10.

# S11 — Model-of-record decision note (UPG-01)

> Session S11 (Retrieval-Quality Upgrade, Phase 2). This note finalises UPG-01
> (model selection + dependency + licences) and is the model-of-record reference
> for UPG-02..05. Supersedes the e5-small / jina-v2 baseline notes from S01–S10.

## Decision (research-backed, 2026-06-28)

| Role | Baseline (S01–S10) | Model of record (S11) | Licence | Why |
|---|---|---|---|---|
| Embedder | `intfloat/multilingual-e5-small` (384-d, fastembed) | **`Qwen/Qwen3-Embedding-0.6B`** via `n24q02m/Qwen3-Embedding-0.6B-ONNX` (1024-d, INT8 ONNX) | **Apache-2.0** | MMTEB 64.33 (vs e5-small ~58); 32K ctx (vs 8K); 100+ langs; MRL-truncatable; best-in-class small multilingual embedder. |
| Reranker | `jinaai/jina-reranker-v2-base-multilingual` | **`Qwen/Qwen3-Reranker-0.6B`** via `n24q02m/Qwen3-Reranker-0.6B-ONNX` (INT8 ONNX) | **Apache-2.0** | MMTEB-R 66.36 (best small multilingual reranker); largest gain on long multilingual docs (+27 MLDR vs jina-v2). **ALSO fixes a licence blocker** (see below). |
| Context prefix | template-based (`brain.chunk.contextual_prefix`) | + index-time LLM doc-context (UPG-04, Anthropic Contextual Retrieval) | n/a (local CPU LLM) | Targets atomic-note + cross-lingual failure modes. |

Both ONNX checkpoints are ~573 MB INT8, CPU-only, no GPU, no PyTorch — runs on
Apple-silicon MacBooks AND ordinary Windows laptops offline (the binding constraint).

## Dependency resolution (verified, not asserted)

**Finding (2026-06-28):** the context bundle's "fastembed ≥ v0.9.0 has Qwen3
(PR #605)" path is **NOT available** — fastembed's latest PyPI release is
**0.8.0**, which does not catalogue Qwen3 (verified:
`TextEmbedding.list_supported_models()` returns 0 Qwen entries; same for
`TextCrossEncoder`). PR #605 is merged but unreleased.

**Resolved path:** the **`qwen3-embed` lib** (v1.11.1, n24q02m fork) — a
fastembed-compatible API (`TextEmbedding`, `TextCrossEncoder`) that loads the
ONNX INT8 exports directly. Installed into `.venv-embed`; confirmed it uses
onnxruntime (CPUExecutionProvider) with no PyTorch. Wired into brain via two
new adapters (`QwenEmbedder` in `brain.embed`, `QwenReranker` in `brain.rerank`)
that route automatically when `BRAIN_EMBED_MODEL` / `BRAIN_RERANKER_MODEL` name
a Qwen3 model.

## Licence action (UPG-01 + UPG-03) — the corporate blocker

**`jinaai/jina-reranker-v2-base-multilingual` is CC-BY-NC-4.0 (non-commercial).**
This is a **corporate-deployment blocker** for Example Corp independent of any quality
question. Moving to `Qwen3-Reranker-0.6B` (Apache-2.0) is BOTH a quality upgrade
AND a licence fix — the two compound. (For completeness: `jina-embeddings-v3`
is also CC-BY-NC, so it was never a candidate; `mxbai-rerank-v2` is weak
multilingual and GPU-centric.)

Qwen3-Embedding-0.6B and Qwen3-Reranker-0.6B are both **Apache-2.0** (confirmed
via the Qwen3-Embedding tech report arXiv:2506.05176 and the official blog
qwen.ai/blog?id=qwen3-embedding — "open-sourced under the Apache 2.0 license").

## CPU load verification (UPG-01 gate — macOS, real)

Verified 2026-06-28 on macOS (Darwin 25.5.0, Apple silicon), CPUExecutionProvider,
no GPU:

| Model | One-time load | Per-doc inference | Output | Cross-lingual sanity |
|---|---|---|---|---|
| Qwen3-Embedding-0.6B-ONNX | 47 s (model download + ONNX session) | ~29 ms/doc (3 docs in 86 ms) | 1024-d float32 | cos(EN-query, PT-doc) = 0.43 — semantically related |
| Qwen3-Reranker-0.6B-ONNX | 61 s (model download + ONNX session) | ~310 ms/pair (3 pairs in 935 ms) | float relevance score | PT-query → correct EN-doc ranked #1 at 0.997, irrelevant at 0.0 |

Both load and run on CPU. The embedder is well within an interactive query budget
(~29 ms/doc + vector search). The reranker at ~310 ms/pair is the latency risk
for a wide rerank window (top-20 → ~6 s); UPG-03 gates on p95 interactive and
reverts if it cannot meet the latency bar.

## Query prefixing (the "silent degradation" trap)

Qwen3-Embedding is an **instruction-tuned decoder embedder** — queries MUST carry
an instruction prefix for optimal ranking. Empirically verified (2026-06-28):
applying the default retrieval instruction widens the relevant-vs-irrelevant
cosine margin from **0.30 → 0.40** (a 33% wider separation), primarily by
suppressing the irrelevant-doc score. Passages carry NO prefix. This is the same
class of asymmetry as e5 `query:`/`passage:` and the same class of bug if omitted.

The prefix is applied INSIDE the `QwenEmbedder` adapter (`_QUERY_INSTRUCT`), so
the caller passes raw text — no prefix leakage at the call site.

## A/B backup embedder

`Snowflake/snowflake-arctic-embed-m-v2.0` (305M, 768-d, Apache-2.0) — retained as
the A/B backup, benchmarked ONLY on the PT/ES strata if Qwen3 underwhelms there.
Note: it is NOT in the fastembed catalog and needs a manual ONNX export (S03
finding), so it is the fallback, not the primary.

## Traps avoided (per the context bundle)

- `Qwen3-Embedding-4B/8B` — fp32 saturates 16 GB; INT8 slow/interactive-unfriendly. ✗
- `jina-embeddings-v3` — CC-BY-NC. ✗
- `mxbai-rerank-v2` — weak multilingual (28.56), no ONNX, GPU-centric. ✗
- `BGE-M3` — its dense+sparse+ColBERT edge is redundant (we have FTS5 BM25 for the sparse leg); Qwen3 beats it on MMTEB + ctx length. (Future: if a session adopts native dense+sparse+ColBERT, BGE-M3 is the one model that does all three.)

## Sources

- Qwen3-Embedding tech report: arXiv:2506.05176
- Qwen3-Embedding blog: qwen.ai/blog?id=qwen3-embedding
- Qwen3-Reranker-0.6B ONNX: huggingface.co/n24q02m/Qwen3-Reranker-0.6B-ONNX
- `qwen3-embed` lib: pypi.org/project/qwen3-embed
- Arctic-embed-m-v2.0: huggingface.co/Snowflake/snowflake-arctic-embed-m-v2.0, arXiv:2412.04506
- fastembed PRs #602 (BGE-M3) / #605 (Qwen3): merged but NOT in any released fastembed as of 2026-06
- MIRACL excludes Portuguese (ES is the proxy); no public PT retrieval benchmark → gate on the vault blind set.
