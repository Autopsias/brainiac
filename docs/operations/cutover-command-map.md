# Cutover command map — Obsidian + Smart Connections → `brain`

**Hook for the follow-on operational-cutover plan (NOT executed in these 10
sessions).** Maps each retrieval-cascade / Bases / SC-health operation in the
live Example Corp control plane to its `brain` CLI equivalent. Verbs verified against
`src/brain/cli.py` (S10). Egress note: every `brain` read verb applies the
deny-by-default classification gate at the CLI boundary (`--max-tier`); the
in-process core is unfiltered by design (real containment = `brain project`
projection + host/VM split, not the CLI filter).

| Today (Obsidian / SC) | Cutover (`brain`) | Notes |
|---|---|---|
| Cascade Step 0 — grep/ripgrep lexical | `brain grep <pattern> [--regex]` | lexical-first, no embedding (RET-04); the vault-side `_index.md`/zone-catalog pre-filter STAYS |
| Cascade Step 1 — `mcp__smart-connections__lookup` (Sources) | `brain search "<q>" -k 20` | fused RRF(60) BM25+dense (RET-01); cross-lingual via the multilingual model |
| Cascade Step 1 (Blocks mode) | `brain search` (chunk-level index already block-aware) | brain indexes per-chunk; no separate Blocks toggle |
| Cascade Step 1.5 — `_rerank.py` cross-encoder | `brain search "<q>" --rerank [--rerank-top 15]` | Step 1+1.5 collapse into ONE call; reranker is skippable (degrades to no-op if absent) |
| Cascade Step 2 — `90 System/Bases/*.base` | `brain bases-query --filter <col>=<val>` | structured frontmatter view over indexed columns (RET-04) |
| Cascade Step 3 — wikilink BFS | `brain graph-expand --seeds <id...> --depth 2` | DISCOVERY-ONLY (RET-03), never authoritative |
| Cascade Step 3.5 — `_ppr/ppr.py` | `brain graph-expand … --use-ppr` | PPR tie-break folded into graph-expand |
| Cascade Step 3.6 — graphify discovery | `brain graph-expand` (discovery) | keep graphify as a separate discovery graph if desired |
| Cascade Step 4 — deep-read anchored section | `brain get <id>` / `brain read <id>` | full-note read by id |
| P-4 versioning — `Latest Only.base` | `brain bases-query --filter is_latest_version=true` | temporal "current state" |
| P-4 — `As Of.base` / `Version Chain.base` | `brain` frontmatter query on `document_date` | point-in-time / chain |
| Bootstrap step 7 — `Open Items.base` | `brain bases-query --filter status=open` | stale-first ordering via `recent`/sort |
| Bootstrap step 8 / health §9 — `mcp__smart-connections__stats` | `brain status` | note count, embed model+dim, newest-mtime, backend |
| `_smoke_test_retrieval.py` (smart-env shape) | `brain selftest` / `brain status` | substrate-shape insurance |
| `_smoke_test_retrieval.py` (full fixture) | `eval/harness.py` + `eval/gate.py` (this golden set) | the eval contract is the successor |
| integrity-scan §A near-dup (SC cosine) | `brain` sqlite-vec vectors directly | no MCP round-trip; query the vector backend |
| recent / "what changed" | `brain recent` / `brain digest --days 7` | UX-02 |
| SC re-index on edit | `brain sync` (incremental, content-hash) / `brain rebuild` | sync is the capture drain too |
| n/a (new) — write a note | `brain capture` / `brain write` (host, audited, fails closed) | Ed25519 audit chain (CORE-03) |
| n/a (new) — sensitive-tier-free copy for a PENDING harness | `brain project --dest <dir> --max-tier Internal` | the default posture for un-VERIFIED harnesses (see allowlist) |

**Not a 1:1 replacement, by design:** `brain search` already fuses BM25+dense
(+optional rerank), so the SC cascade's Step 1 + Step 1.5 become a single verb.
The vault-side lexical pre-filter (`_index.md`, zone catalogs, `_build_index.py`)
is substrate-agnostic and is KEPT — it complements `brain grep`.
