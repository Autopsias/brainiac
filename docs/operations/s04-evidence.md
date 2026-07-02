# S04 evidence — Retrieval pipeline (RET-01 / RET-02 / RET-03 / RET-04)

**Session:** S04 · **Date:** 2026-06-27 · **Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Builds on:** S02 core (`brain` engine, sqlite-vec/FTS5 adapter, Ed25519 audit) + S03 (chunked Arctic/ONNX embeddings, incremental sync).
**Model note:** executed on Opus.

Every artifact listed exists on disk and is non-empty.

## What shipped (design v5 §4)

### RET-01 — Hybrid RRF(k=60) BM25 + dense
`src/brain/index.py`. `BrainIndex.hybrid_search` fuses two ranked lists —
FTS5 **BM25** (lexical, note-level, `ORDER BY rank`) and the dense vector list
(semantic, chunk-level folded to best-chunk-per-note) — into ONE ranking via
**Reciprocal Rank Fusion**, `score(note) = Σ 1/(rrf_k + rank)` with `rrf_k=60`.
RRF needs only each item's *rank* in each list, so BM25 and cosine scales never
have to be reconciled — the property that makes the fusion at-least-as-good-as
either across languages (non-inferiority ship gate, not a promised uplift).
`search()` now delegates to `hybrid_search()` (back-compat).

**Adapter seam (HARDENED:codex):** the dense list is obtained through the
`VectorBackend` adapter (`brain.vectors`), never a hard-wired sqlite-vec call —
a pre-v1 sqlite-vec change cannot force a retrieval rewrite, and the brute-force
fallback fuses identically. (We deliberately did NOT build a fragile single-SQL
vec0+FTS5 statement; the adapter seam overrides the "one SQL query" phrasing.)

### RET-02 — Skippable gte-multilingual reranker (top 10-20, int8)
`src/brain/rerank.py`. `Reranker` protocol + `GteReranker`
(**Alibaba-NLP/gte-multilingual-reranker-base**, Apache-2.0, fastembed/ONNX,
lazy-load, no PyTorch) + `NoopReranker` identity fallback. `hybrid_search(...,
rerank=True)` re-orders ONLY the top `rerank_top` (clamped to **10-20**) and
leaves the tail untouched — latency bounded regardless of corpus size. Strictly
**skippable**: off by default; `_apply_rerank` catches any runtime/model failure
and degrades to identity (the "degrades to no-op if absent" contract). CLI flag:
`brain search --rerank [--rerank-top N]`.

### RET-03 — Wikilink-BFS + PPR on-demand; GraphRAG discovery-only
`src/brain/graph.py`. On-demand (no schema change, no persisted edge table):
`build_graph` parses `[[target]]` / `[[t|alias]]` / `[[t#heading]]` from note
bodies into a directed graph with an id/stem/title resolver; `wikilink_bfs`
(undirected, hop-bounded) and `personalized_pagerank` (random-walk-with-restart
to the seed set, dangling-node teleport). `graph_expand` combines both for
multi-entity / multi-hop queries. **DISCOVERY-ONLY:** every result is tagged
`authoritative: false` / `provenance: "graph-derived (discovery-only)"` — it
nominates candidate ids to confirm on the cited note; curated notes + the hybrid
ranking win on any conflict.

### RET-04 — Agentic tool surface (search/grep/bases/graph/read)
`src/brain/cli.py` + `core.py`. Retrieval is a small set of composable read
subcommands the frontier model orchestrates (NOT a rigid cascade):
`hybrid-search` + `grep` + `bases-query` + `graph-expand` + `read` (plus the
existing `search`/`get`/`recent`). **Lexical-first, embed lazily:** `grep` and
`bases-query` never embed; the query vector is computed only inside the dense leg
of `hybrid-search` on semantic escalation. All surfaces honour the same
deny-by-default classification egress gate at stdout — including `graph-expand`
candidates (a withheld note never leaks via the graph surface). `brain --help`
self-documents the whole surface.

## Tests
`tests/test_retrieval.py` — 23 new tests: RRF fusion (scale/ordering, `rrf_k`
weighting, lexical-only fusion, search↔hybrid delegation); reranker
(clamp band, noop identity, off==noop-on, a fake reranker reorders the head);
graph (wikilink parsing variants, edge build, BFS depth, PPR neighbour ranking,
discovery-only flags, unresolved seeds); CLI tool surface
(grep/bases-query/graph-expand JSON + egress gating, read alias, `--rerank`
flag, `--help` lists the tools).

Full suite: **81 passed** (was 58 pre-S04). No regressions.

## Artifacts
- `_evidence/s04/pytest-summary.txt` — full-suite green (81 passed)
- `_evidence/s04/cli-smoke.txt` — live CLI run of all five agentic tools on `vault/`
- `src/brain/rerank.py`, `src/brain/graph.py` — new modules
- `src/brain/index.py`, `src/brain/core.py`, `src/brain/cli.py` — RRF + tool wiring
- `tests/test_retrieval.py` — RET-01..04 tests
