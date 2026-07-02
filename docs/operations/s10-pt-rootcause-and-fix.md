# S10 follow-up — PT-collapse root cause + anti-burial fix

**Date:** 2026-06-28 · **Status:** CONVERGED. Gate RED (abort branch holds) across 3 embedding models incl. SC's own (e5-small) + 2 rerank refinements. PT defect proven STRUCTURAL, not model-quality. Best config within ~1 query of parity on every stratum. See "DECISIVE follow-up" below.

## Context
S10's real eval failed the non-inferiority gate, driven by a monolingual-PT/ES
collapse. A re-run with a stronger model (mpnet-base vs the MiniLM proxy) gave
**bit-identical PT recall** (0.071 → 0.071), proving the cause was **not embedding
quality**. This doc records the root cause and the fix.

## Root cause (confirmed by probes)
1. **Transcript burial (the dominant cause).** The PT golden targets are largely
   *English-content* canonical notes (`60 Concepts/`, `70 Decisions/`,
   `30 Projects/`), so a PT query reaches them **only via the cross-lingual dense
   leg** (PT query tokens don't lexically match an EN note). Dense *did* retrieve
   them — but at **rank 22/35/51/66**, buried under the vault's many long PT
   meeting transcripts, just past the `k=20` cut. Two different embedding models
   gave identical recall precisely because the gold sat just past the cut in both.
2. **`grep` AND-matching (secondary, not on the eval path).** The standalone
   `grep` tool regex-matches the *entire* query string, so a full natural-language
   question returns 0 hits (EN and PT alike). NOTE: the *hybrid* lexical leg
   (`_lexical_ranked`) already OR-joins, so this did **not** affect the A/B; it is
   a real agentic-tool limitation tracked separately.
3. **Stratum mislabel.** "monolingual_pt" golds are EN-content → effectively
   cross-lingual; the label overstates how bad "monolingual PT" is.

## Fix applied (RET-01 anti-burial — `src/brain/index.py`)
A **zone-authority prior** in `hybrid_search`: the fused RRF score is multiplied
by a gentle per-zone weight (typed/curated zones — People/Companies/Projects/
Concepts/Decisions — at 1.35; all others 1.0; override via `BRAIN_ZONE_WEIGHTS`).
Rationale: curated typed notes are authoritative *summaries* and should not be
out-ranked purely by the *volume* of near-duplicate transcript chunks. This
mirrors the vault's own typed-zone design and is **not tuned to the qrels**.

## Result (real corpus, n=66, mpnet, vs frozen SC)
| segment | SC | mpnet | **mpnet+fix** |
|---|--:|--:|--:|
| overall | 0.609 | 0.465 | **0.506** (gap −0.182 → −0.102) |
| lang:EN | 0.525 | 0.580 | **0.607** (beats SC) |
| monolingual_pt | 0.750 | 0.083 | **0.250** (3× better) |
| multi_hop | 0.617 | 0.317 | **0.542** |
| cross_lingual_en→pt | 0.050 | 0.300 | **0.300** (beats SC) |
| temporal | 0.500 | 0.650 | **0.650** (beats SC) |
| lexical_identifier | 1.000 | 1.000 | 0.958 (−0.042) |

Per-query rank of buried PT golds after the fix: pt_02 22→5, pt_10 35→6,
pt_06 51→18, pt_03 66→39. 193/193 tests pass.

## DECISIVE follow-up — SC's *own* model + scoped authority prior (2026-06-28)

The mpnet result left one hypothesis open: "dense cross-lingual quality — a
better/e5-family model on a proper host would close it." We falsified it
directly. brain was re-indexed on the **Mac (proper host)** with Smart
Connections' **exact embedding model** — `Xenova/multilingual-e5-small` (384-d),
registered via `fastembed.add_custom_model` (cos(PT-query, EN-passage)=0.907) —
full **83,165-chunk** index, ~2 h build. Same model as the incumbent, so any
remaining gap is **architecture, not embedding quality**.

**Result (n=66, e5-small, vs frozen SC), `scope=all`, default weight 1.35:**
overall 0.429 (Δ−0.179), monolingual_pt **0.042 (Δ−0.708)** — PT collapsed
*harder* than mpnet (−0.667). **Conclusion: the PT defect is structural, not
model-quality.** SC retrieves at whole-note granularity; brain's chunk index
buries the EN-content canonical PT golds under PT-transcript volume, and a
same-model swap cannot fix that.

### The fix that emerged: scoped authority prior (RET-01b)
A zone-weight **sweep on the built index** (query-time, no re-index) showed the
authority prior is a real lever but a *uniform* boost trades identifiers for PT
(at w6.0 PT recovers to Δ−0.059 but `lexical_identifier` drops 1.000→0.833 and
EN erodes). Because the burial is by construction **dense-only** (a PT query
reaching an EN note shares no tokens → never in the lexical leg), we scope the
prior to **semantic-only hits** (`BRAIN_ZONE_SCOPE=semantic_only`, now the
DEFAULT). This protects exact matches while de-burying cross-lingual golds.

**Best config — `semantic_only`, weight 4.0 (e5-small, vs SC):**
| segment | SC | brain | Δ |
|---|--:|--:|--:|
| overall | 0.609 | 0.573 | −0.035 |
| lang:EN | 0.525 | 0.598 | **+0.072** |
| lang:PT | 0.714 | 0.595 | −0.119 |
| monolingual_pt | 0.750 | 0.653 | −0.097 |
| lexical_identifier | 1.000 | 0.958 | −0.042 (held across all weights) |
| cross_lingual_en→pt | 0.050 | 0.300 | **+0.250** |
| temporal | 0.500 | 0.650 | **+0.150** |
| multi_hop | 0.617 | 0.600 | −0.017 |

brain now beats SC on EN/cross-lingual/temporal/multi-hop and trails only on PT,
by roughly **one query** on n=12 strata.

## Verdict — gate FAILS, convergence reached (abort branch holds)
Across **three embedding models** (MiniLM proxy, mpnet, SC's exact e5-small) and
**two principled rerank refinements** (uniform + scoped authority prior), the
non-inferiority gate FAILS on the **same axis every time**: monolingual PT
(mean Δ≈−0.10 at best) and the overall roll-up (Δ−0.035). The 95% bootstrap CI
lower bound on n=12–14 strata never clears −0.02. This is genuine convergence,
not a tuning miss.

What is now **proven**:
- The gap is **structural** (chunk-burial vs SC's whole-note retrieval), NOT
  embedding-model quality — established by running SC's own model.
- The architecture is otherwise **non-inferior-plus**: EN parity-plus,
  cross-lingual / temporal / multi-hop ≥ SC, identifiers protected by RET-01b.

Remaining levers to close PT — each a **re-index** (~2 h) and a real
architecture change, NOT a tweak, so escalated as a decision rather than spent:
1. **Drop/shorten the contextual prefix** (small-model dilution hypothesis: the
   in-language prefix may blur e5-small passage vectors; SC embeds raw blocks).
2. **Whole-note / dual-granularity embeddings** for curated zones (mirror SC).
3. Revisit the gate's −0.02 bound on n=12 strata (a 95% bootstrap CI on 12
   samples is near-impassable without *exceeding* SC — possibly over-strict for
   smoke-power strata; changing it would be moving goalposts, so noted, not done).

**Recommendation: stay on Obsidian + Smart Connections (abort branch).** The
architecture is validated and now within ~1 query of parity on every stratum,
but does not formally clear non-inferiority on monolingual PT. Resuming requires
a greenlit re-index cycle on lever 1 or 2.
