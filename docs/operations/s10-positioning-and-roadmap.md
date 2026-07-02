# S10 — Positioning, FRAMES scoping, and verifiable improvement roadmap

Answers three questions (2026-06-28, MCP-researched): (1) what would running FRAMES entail;
(2) is the framework bleeding-edge / best-practice for a corporate second brain; (3) what are
the verifiable ways to improve it, and are they worth it.

## 1. FRAMES — what it would entail, and whether it's the right test

FRAMES (Google, NAACL 2025): 824 adversarial multi-hop questions over **English Wikipedia**,
2–15 articles/question, metric = answer accuracy via LLM-judge.

**Effort (fully offline-able):** corpus ships pre-scraped (`parasail-ai/frames-benchmark-wikipedia`,
2,524 articles, 33 MB) — no live Wikipedia needed. brain is a pure retriever, so we'd add a
retrieve→generate→judge loop (reuse the answer-grounded harness: subagent generator + judge).
Build ≈ half a day. Run time: oracle / single-shot ≈ 30–35 min on a 100-q sample; multi-step
(k=5 subqueries × 5 iters) ≈ 2 h sample; full 824 multi-step is many hours (LLM answer-gen
dominates). Recommended: 100-q sample, single-shot + oracle.

**Verdict — FRAMES is a CALIBRATION point, NOT a success criterion for this use case.** It
measures the wrong corpus and wrong failure mode: (a) Wikipedia is public, long, hyperlinked —
unlike private EN/PT meeting notes + memos; (b) FRAMES questions are answerable from parametric
LLM knowledge (no-retrieval baseline ≈ 40%), whereas a corporate-vault question has ~0%
parametric fallback — retrieval failure = total failure; (c) FRAMES failures concentrate in
numerical/tabular reasoning; vault failures concentrate in entity resolution, version/temporal
disambiguation, and EN↔PT cross-lingual — none of which FRAMES exercises. The vault
answer-grounded eval we already built is ~10× more diagnostic. Use FRAMES only to sanity-check
the multi-hop retrieval loop + calibrate the judge, not to grade the product.

## 2. Positioning — bleeding edge, best practice, or mid-tier? (calibrated, 3 axes)

Separate the axes — they land differently:

| Axis | Verdict | Basis |
|---|---|---|
| **Architecture** | **Best practice, slightly above median** | Hybrid (dense+BM25 RRF) = 2026 table stakes; cross-encoder rerank = near-universal in serious deployments (~64%); multi-query fan-out = above-standard (TREC-RAG'25 #1 used fan-out). |
| **Component choice** | **Mid-tier, ~18–24 months behind** (deliberately) | e5-small (2022/2024 small tier) sits below BGE-M3 / Arctic-embed-2 / Qwen3-Embedding; jina-reranker-v2 (~69 BEIR) trails Qwen3-Reranker-0.6B (~71, +7 MTEB-R, +27 MLDR). The trade for local/offline/CPU + no-GPU. |
| **Eval rigor** | **Exceptional — ahead of virtually all production** | nested-CV hyperparameter selection, pooled blind graded qrels, answer-grounded LLM-judge (generator≠judge), public-benchmark placement. Most production RAG ships with none of this. |

**Bleeding edge** (which we are NOT) would be: agentic/iterative retrieval (Self-RAG, IRCoT,
Adaptive-RAG), full first-stage ColBERT/PLAID, GraphRAG, LLM-as-reranker. These are research /
niche, not production standard.

**Honest one-liner:** *best-practice architecture + researcher-grade eval, on deliberately
mid-tier local components.* Not bleeding edge; well-engineered, verifiable, and correctly
optimized for the binding corporate constraints (privacy/local, multilingual, provenance). The
eval rigor is the durable edge — it makes every upgrade below measurable, not speculative.

**For the corporate-second-brain objective specifically**, what matters most: (1) local/privacy
(hard ceiling — rules out API embedders; ONNX/CPU path is the enabler); (2) multilingual EN/PT/ES
(the biggest differentiator — and our current weakest component link, since e5-small + BM25 under-
serve PT/ES); (3) provenance/citation (architecture, already handled); (4) per-project scoping
(metadata filter, not a model question); (5) freshness (incremental re-embed).

## 3. Verifiable improvement levers (ranked; all CPU/ONNX-feasible unless noted)

Every lever is verifiable on the harness we built (135-q blind-pooled set + BEIR SciFact +
answer-grounded). That is the point — measure each before/after, keep only what wins.

| # | Lever | Expected lift | Effort | Worth it? |
|---|---|---|---|---|
| 1 | **Embedding → BGE-M3** (dense + sparse; sparse replaces BM25) | +5–10 nDCG dense BEIR; +2–4 more from BGE-M3 sparse vs BM25 on multilingual; strongest PT/ES coverage | Medium — model swap + ~30 min re-index | **YES — #1.** One model replaces embedder+BM25, fixes the weakest (multilingual) link |
| 2 | **Contextual Retrieval** (Anthropic-style: prepend per-chunk doc context at index time) | −35% to −67% retrieval failures (stacked w/ rerank); index-time only, **zero query latency** | Low–Med — one pre-index LLM pass | **YES.** Directly targets atomic-note + cross-lingual misses; our exact failure mode |
| 3 | **Reranker → Qwen3-Reranker-0.6B ONNX** | +7 MTEB-R, +27 MLDR long-doc vs jina-v2 | Low — drop-in cross-encoder swap | **YES.** Free quality on long corporate docs |
| 4 | Arctic-embed-l-v2.0 (alt to #1; stronger EN dense, weaker multilingual) | +9–12 EN BEIR vs e5-small | Medium | A/B vs BGE-M3; BGE-M3 likely wins for EN/PT/ES mix |
| 5 | HyDE query expansion (blend hypothetical-answer embedding) | +2–6 on analytic queries | Medium — +1 LLM call/query | YES for analytic queries; route, don't apply to identifier lookups |
| 6 | NUDGE non-parametric embedding fine-tune on vault | +5–10% nDCG on private corpus | Low — minutes, CPU | YES after #1–3, IF synthetic eval pairs are clean (else can regress) |
| 7 | GPL domain fine-tune of embedder | +5–9 nDCG domain | High — gen pipeline + training | Later; highest payoff/effort ratio is poor until #1–3 done |

**Single highest-ROI:** **BGE-M3 (embedder + sparse) + Qwen3-Reranker-0.6B**, both ONNX/CPU.
Cost to VERIFY: re-index (~30 min) + rerun BEIR SciFact + the 135-q blind eval (~1–2 h total).
Expected: measurable lift concentrated on PT/ES and cross-lingual probes (the weakest link), at
zero loss of the local/offline property. Because the eval is built, "worth it" becomes an
empirical question we can settle in an afternoon rather than a bet.

**Recommended sequence:** (1) BGE-M3 swap → re-eval; (2) Qwen3-Reranker swap → re-eval;
(3) Contextual Retrieval at index time → re-eval; then decide on HyDE/NUDGE from the numbers.
Each step gated by the harness; revert any that doesn't beat baseline on the blind set.
