> ⚠ **SUPERSEDED 2026-06-28 — the AUTHORITATIVE verdict is `expanded_result.md`.**
> The n=66 RED/ABORT result below was the FIRST real eval (proxy model
> `paraphrase-multilingual-MiniLM-L12-v2`). It was subsequently **OVERTURNED**:
> a re-run on an expanded 135-query golden set (pooled blind judging,
> generator=Haiku / judge=Sonnet) plus `e5-small` / `e5-large` / `mpnet`
> variants and a BEIR scifact sanity check shows `brain` is statistically
> **SUPERIOR** to Smart Connections — recall@10 +0.074 (95% CI lo +0.019 > 0),
> success@5 +0.156 (p=0.0004), 6/7 strata by recall / 7/7 by success@5. The
> VAL-04 non-inferiority gate is **GREEN (superior)**. The body below is
> retained VERBATIM as the dated historical record of the n=66 run; **do NOT
> act on its ABORT recommendation.** Authoritative files:
> `_evidence/s10/expanded_result.md` + `scorecard_expanded.json` +
> `bench/result_beir_scifact_test.json`. Reconciliation also recorded in the
> plan's `_closeouts/s10.json` `_reconciliation` block and `PLAN-REVIEW-LOG.md`.

# S10 — REAL retrieval A/B verdict (VAL-04)

## Headline: GATE FAIL → ABORT BRANCH. HALT, stay on Obsidian + Smart Connections.

**This is a REAL comparison on the REAL Example Corp corpus** (not machinery-only). With
the embedding model available in-sandbox, the new `brain` retriever is
**materially INFERIOR** to the incumbent Smart Connections — driven by a
collapse on **monolingual PT and ES**. Per the pre-registered ship gate
(`eval/gate.py`), the defined outcome is **HALT + stay on Obsidian + SC; do NOT
decommission the incumbent; do NOT carry the build forward.** Session result:
**PARTIAL/BLOCKED**, surfaced to the human checkpoint.

> Numbers were NOT massaged to pass. An honest "inferior — here is the data" is
> the correct outcome of this gate. The result is also well-diagnosed: the gap is
> almost entirely the **embedding model** (a proxy, see below), not the
> architecture — which is the single most important thing for the abort decision.

## What was run (real, disclosed)
- **Corpus:** the real Example Corp vault (`/Users/user/Downloads/Example-Vault`,
  read-only — never modified). Indexed into `brain` app-data
  (`_evidence/s10/brain-index/`, NOT in the vault). 2250 frontmatter-bearing
  notes; all sensitive content stayed local (no egress).
- **New = brain hybrid** (RRF BM25+dense), `-k 20`. **Embedding model: a REAL,
  catalogued, locally-cached multilingual PROXY —
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-d).**
  The design-of-record **Arctic-embed-m-v2.0 is NOT in the fastembed catalog**
  (confirmed S03 + S10), so it could not be tested in-sandbox; the proxy is the
  sanctioned stand-in.
- **Current = frozen Smart Connections** (`Xenova/multilingual-e5-small`, 384-d,
  3532 notes), captured by driving the live SC `lookup` MCP over the golden set
  and frozen to `eval/runs/current_sc.frozen.json`. **n = 66/66** golden queries
  (full set; 0 missing from either system). All 104 qrels doc-refs verified
  present on disk.
- **Metrics:** `eval/harness.py` (ranx). **Gate:** `eval/gate.py` — bootstrap
  95% CI lower bound on per-query Recall@10 delta ≥ −2pp, overall + per
  gate-power segment; p95 not worse.

## Scorecard (Recall@10), real corpus, n=66
| segment | n | power | SC (cur) | brain (new) | Δ | verdict |
|---|--:|---|--:|--:|--:|---|
| **overall** | 66 | gate | 0.609 | 0.427 | **−0.182** | **FAIL** (95% CI lo −0.308) |
| lang:EN | 46 | gate | 0.525 | 0.547 | **+0.022** | FAIL on the strict bound (CI lo −0.101) but brain is *better* on the mean |
| lang:PT | 14 | gate | 0.714 | 0.071 | **−0.643** | **FAIL — catastrophic** |
| lang:ES | 6 | smoke | 1.000 | 0.333 | −0.667 | not gating (smoke) but same collapse |
| class:lexical_identifier | 12 | gate | 1.000 | 0.958 | −0.042 | ~tied (BM25+dense strong on identifiers) |
| class:monolingual_pt | 12 | gate | 0.750 | 0.083 | **−0.667** | **FAIL** |
| class:monolingual_es | 6 | smoke | 1.000 | 0.333 | −0.667 | not gating |
| class:cross_lingual_en_pt | 10 | marginal | 0.050 | 0.300 | **+0.250** | brain **better** (not gating) |
| class:cross_lingual_en_es | 6 | smoke | 0.250 | 0.167 | −0.083 | not gating |
| class:multi_hop | 10 | marginal | 0.617 | 0.317 | −0.300 | not gating |
| class:temporal | 10 | marginal | 0.500 | 0.650 | **+0.150** | brain **better** (not gating) |

Fisher superiority p = 0.998 (brain is significantly *worse* overall, not better).
nDCG@10 / MRR@10 track the same shape (see `real-ab-scorecard.json`/`.md`).
**Reranker variant** (`real-ab-scorecard-rerank.json`): **identical Recall@10** —
reordering cannot recover a relevant doc the embedding never retrieved into the
candidate set, confirming the gap is at the **embedding-retrieval** stage.

## Latency
brain p50 = 174 ms, p95 = 279 ms (in-process, over the real index). SC latency
is **not measurable** through the MCP (the tool returns no timing), so the
latency leg is **reported, not gated** (gate SKIPs it). brain's latency is well
within an interactive budget.

## Diagnosis (why it failed — and why it's the model, not the architecture)
Spot-checks (`real-ab-scorecard.md` + transcript) show the scoring is **fair**:
brain correctly hits `60 Concepts/NewERP.md` for `NewERP`, so path normalization
works. On PT queries brain returns **plausible but wrong-granularity** notes —
e.g. for "estratégia de IT da UnitA" it surfaces UnitA IT *meeting
transcripts* instead of the canonical `30 Projects/UnitA IT Strategy.md` that
SC nails. The proxy model (MiniLM) is simply **weaker on monolingual PT/ES** than
SC's e5-small. Meanwhile brain is **competitive-or-better on EN, EN→PT
cross-lingual, temporal, and tied on lexical identifiers** — so the hybrid RRF
architecture is sound; the embedding is the gating variable.

## Residual gaps to a full production-fidelity run
1. **Embedding model (THE gating variable).** The proxy
   `paraphrase-multilingual-MiniLM-L12-v2` is NOT the design-of-record
   Arctic-embed-m-v2.0 and is weaker on PT/ES. **Before any cutover decision,
   re-run THIS EXACT A/B with either (a) the bundled production Arctic-embed-m-v2.0
   ONNX checkpoint, or (b) a same-family / stronger catalogued model — most
   directly `intfloat/multilingual-e5-large` (same e5 family as SC's e5-small,
   1024-d), which is the fair "can brain at least match SC's own model family"
   check.** The gate must go GREEN on the real corpus before retirement.
2. **SC capture `k`.** Part of the SC baseline was captured at `limit=10`
   (note-level). Recall@10 — the gate metric — is fully covered; only SC's
   Recall@20 is capped (slightly *understates* SC@20, i.e. conservative for the
   incumbent; does not affect the FAIL).
3. **Index breadth.** brain indexed 2250 frontmatter-bearing notes vs SC's 3532
   (SC also indexes frontmatter-less notes). All 72 qrels targets carry
   frontmatter, so both systems *can* retrieve every graded doc; the difference
   is non-target corpus, not a handicap on the scored set.
4. **Egress posture.** The eval calls the core retriever directly (unfiltered) to
   measure the **retrieval primitive**; the production deny-by-default egress gate
   is a separate shipped behaviour (validated in VAL-03).

## Recommendation
**HALT. Stay on Obsidian + Smart Connections.** Do NOT cut over; do NOT
decommission the incumbent. The architecture is promising (EN parity-plus,
cross-lingual EN→PT and temporal *better*, identifiers tied), but monolingual
PT/ES recall with the available proxy model is a hard blocker for a bilingual
EN/PT (+ES) vault. **Next action before re-deciding:** obtain/bundle the
production embedding model (Arctic-embed-m-v2.0) or test `multilingual-e5-large`,
re-run this A/B, and require a GREEN gate. This verdict + scorecard go to
maintainer's final accept/abort checkpoint.
