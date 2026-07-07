# S10 — Public-benchmark placement (BEIR SciFact)

**Question:** how does brain compare to industry-standard retrieval systems? Our 135-query
result is on a *private* corpus with our own qrels, so its absolute numbers cannot sit on a
public leaderboard. To get an apples-to-apples number we ran brain's **exact** stack over a
standard public benchmark with the benchmark's own queries + qrels.

## Method
`eval/benchmark_public.py` — load BEIR via `ir_datasets`, materialize the corpus as markdown
notes, `brain rebuild`, run every benchmark query through the real pipeline, score the canonical
metric (nDCG@10) against BEIR's qrels. Flat corpus ⇒ no typed zones ⇒ the zone-authority prior
is a no-op: this measures the **core retrieval stack**, not our vault-specific tuning. Fan-out
omitted (needs an LLM to make variants; not standard for BEIR). Dataset: **BEIR SciFact /test**
(5,183 docs, 300 queries). Embedder intfloat/multilingual-e5-small; reranker
jinaai/jina-reranker-v2-base-multilingual. Run 2026-06-28.

## Result

| stage | nDCG@10 | recall@10 | recall@100 |
|---|---|---|---|
| dense-only | **0.673** | 0.802 | 0.915 |
| + hybrid (BM25 RRF) | **0.699** | 0.832 | 0.943 |
| + cross-encoder rerank | **0.762** | 0.862 | 0.943 |

## Why this matters — three things it proves

1. **Our integration is correct and lands on the public scale.** Published mE5-small SciFact
   nDCG@10 = **0.677** (Wang et al. 2024, arXiv:2402.05672). Our dense-only = **0.673** —
   reproduces the leaderboard figure within **0.4 pt** (chunked+folded-to-doc, so a hair under
   is expected). This is the validation that our numbers are *real* and comparable, not an
   artifact of a friendly private corpus.
2. **Our hybrid lift matches the literature.** +0.026 (dense→hybrid) sits inside the published
   RRF range of **+1 to +5 nDCG@10**.
3. **Our rerank lift matches the literature.** +0.063 (hybrid→+rerank), **+0.089 over dense**,
   inside the published cross-encoder range of **+4 to +10 nDCG@10**. The final **0.762**
   *exceeds mE5-large's published SciFact dense-only (0.704)* — i.e. our small-model + hybrid +
   rerank stack beats a 3× larger embedding model used alone, on a standard benchmark.

## What we CAN and CANNOT claim (honest)

**CAN:**
- Components sit at known, verifiable leaderboard positions: e5-small MTEB-EN 57.9, MIRACL-avg
  nDCG@10 60.8 (ES 51.2); jina-reranker-v2 BEIR 53.17 — matching the 2×-larger bge-reranker-v2-m3.
  A deliberate *local/offline/efficient* tier (below BGE-M3 ~63 and 2026 SOTA 74+).
- On a standard public benchmark, our stack reproduces the published component number and our
  architectural lifts (hybrid, rerank) match published ranges → the design is industry-validated,
  not just locally tuned.
- Final SciFact nDCG@10 0.762 is in the strong-system band for that dataset.

**CANNOT:**
- Claim our *private-corpus* recall@10 (~0.59) is comparable to any leaderboard number — corpus
  scale, query type, and pooled-vs-complete qrels are incompatible. (This is exactly why we ran
  SciFact instead of quoting the vault number.)
- Claim a *Portuguese* benchmark position — PT is absent from BEIR and MIRACL. PT quality rests
  on cross-lingual transfer evidence (the S10 PT root-cause work + the expanded-set PT strata),
  not a public leaderboard.
- Claim SciFact exercises our *multilingual* differentiator — it's English scientific-claim
  retrieval. It validates the stack mechanics and scale; the multilingual edge is covered by the
  component MIRACL-ES position + our private bilingual eval.

## Caveats on the run
- SciFact has shallow judgments (~1.1 rel docs/query); nDCG@10 is the right, standard metric.
- brain chunks + folds to doc; published e5 numbers embed the whole passage. SciFact abstracts
  are short so most fit one chunk — the 0.4-pt gap vs published is consistent with that.

## Reproduce
```
BRAIN_EMBED_MODEL=intfloat/multilingual-e5-small BRAIN_EMBED_DIM=384 \
BRAIN_FASTEMBED_CACHE=.fastembed_cache \
BRAIN_RERANKER_MODEL=jinaai/jina-reranker-v2-base-multilingual \
python eval/benchmark_public.py beir/scifact/test
```
Result: `_evidence/s10/bench/result_beir_scifact_test.json`.
