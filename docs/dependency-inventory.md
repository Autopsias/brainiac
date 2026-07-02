# Dependency inventory — control-plane surfaces naming Obsidian / Smart Connections

**Purpose (r2-codex hardening):** "migration/cutover IN SCOPE" = corpus
migration (S03) **PLUS emitting operational-cutover hooks** for a separate
follow-on plan. This file is one of those hooks: every control-plane surface in
the current Acme vault that today names Obsidian or Smart Connections, with a
cutover action per row. **Populated by S10 (VAL-04 secondary).**

> **Substrate readiness ≠ operational cutover.** Building Profile A's substrate
> does NOT flip any of these surfaces. This inventory exists so the follow-on
> cutover plan can repoint them deliberately. **None of it is actioned in these
> 10 sessions** — these sessions migrate the CORPUS only. Repointing the live
> Acme control plane is a separate, maintainer-gated follow-on plan.

Locations/line refs captured against the live vault at
`/Users/user/Downloads/Acme-Vault` on 2026-06-27. Line numbers drift
as the vault is edited; treat them as anchors, re-grep before actioning.
Status: `[ ]` open → `[x]` cutover-planned (all open until the follow-on plan runs).

## A. Top-level control plane
- [ ] **`CLAUDE.md` (stable prefix)** — _location:_ `CLAUDE.md` L18 (shared
  "Obsidian, Smart Connections" surface), L30 (`.obsidian/`+`.smart-env/` index
  data, the `smart_env.json` edit exception), L36 (Retrieval Cascade M1:
  Step 1 = `mcp__smart-connections__lookup`, Step 2 = `90 System/Bases/`),
  L42–43 (Multilingual Rule, `Xenova/multilingual-e5-small`), L104 (Bases (8)
  pointer). _asserts:_ SC is the semantic layer; Bases is the structured layer;
  Obsidian/`.smart-env` is the index substrate. _cutover action:_ rewrite the
  "Retrieval Cascade (M1)" + "Multilingual Rule" blocks so Step 1 = `brain
  search` (hybrid RRF), embedding model = Arctic-embed-m-v2.0 (or the bundled
  catalogued proxy); keep grep/lexical Step 0; repoint Bases→`brain bases`/
  frontmatter query. _owner/gate:_ maintainer (trigger-only; `CLAUDE.md` is special
  trigger-only per its own Vault Topology rule).
- [ ] **The 14 P-rules** (`90 System/_operating_guide.md`) — _per-rule rows:_
  **P-3** (L78 "Five-step retrieval cascade…", L93 `Xenova/multilingual-e5-small`
  is the cross-lingual layer, L165/L186 Bases routes, L192 the P-3-EDITABLE
  autoresearch surface) — the load-bearing one: repoint Step 1 to `brain`,
  keep the temporal routing matrix (substrate-agnostic).
  **P-4** (versioning: `Latest Only.base`/`As Of.base`/`Version Chain.base`) —
  repoint to `brain` temporal queries OR keep Bases read-only during dual-run.
  **P-7** (autonomy: names `.obsidian/`/`.smart-env/` as read-only index data) —
  replace the SC-index clause with the `brain` index/snapshot location.
  _owner/gate:_ maintainer (operating-guide edits are trigger-only).

## B. Retrieval layer
- [ ] **Retrieval-cascade rule** (`.claude/rules/retrieval-cascade-discipline.md`)
  — Step 1 `mcp__smart-connections__lookup`, Step 1.5 `_rerank.py`, Step 3.5
  `_ppr/ppr.py`, Step 3.6 graphify. _cutover:_ replace Step 1 with `brain search`
  (RRF hybrid already includes BM25+dense+optional rerank, so Step 1+1.5 collapse
  into one `brain search --rerank`); Step 3/3.5/3.6 map to `brain graph-expand`
  (discovery-only). _owner/gate:_ maintainer (rules are trigger-only control plane).
- [ ] **Retrieval contract / eval / smoke-test** (`90 System/_retrieval_contract.md`,
  `_eval_retrieval.md`, `_smoke_test_retrieval.py`). _cutover:_ the eval CONTRACT
  carries over (this S05/S10 golden set + non-inferiority gate is its successor);
  `_smoke_test_retrieval.py` (loads sentence-transformers against `.smart-env`)
  → replace with `brain status`/`brain selftest`. _owner/gate:_ maintainer.
- [ ] **Multilingual / cross-lingual rule** (model `Xenova/multilingual-e5-small`
  → Arctic-embed-m-v2.0 / catalogued multilingual proxy). _cutover:_ the
  cross-lingual GUARANTEE holds (brain uses a multilingual model too); update the
  named model + drop the "Step 1 non-skippable" SC-specific phrasing.
  **NOTE (S10 finding):** Arctic-embed-m-v2.0 is NOT in the fastembed catalog —
  cutover must either bundle the ONNX checkpoint or adopt a catalogued
  multilingual model (`intfloat/multilingual-e5-*`,
  `paraphrase-multilingual-MiniLM`); see `docs/operations/s10-eval-verdict.md`.

## C. Smart Connections specifics
- [ ] **SC health tripwire** — `session-bootstrap-discipline.md` step 8
  `mcp__smart-connections__stats` (`>7d stale / >2% drift` gate);
  `90 System/_maintenance_automation.md` L90 (Acme-vault-health §9 SC freshness).
  _cutover:_ replace with `brain status` index-health (note count, embed model,
  newest-mtime, dim) — already shipped. _owner/gate:_ maintainer.
- [ ] **SC settings + index data** (`.smart-env/smart_env.json` settings;
  `.smart-env/multi/*.ajson`, `*.embed`, `embedding_models/` index data; CLAUDE.md
  L30). _cutover:_ retire after dual-run sign-off; `brain` index lives in app-data
  (`$BRAIN_INDEX_DIR`), not the vault tree. _owner/gate:_ maintainer + retire-gate C.
- [ ] **Plugin-security baseline** (`.claude/rules/plugin-security-discipline.md`)
  — whitelist `["smart-connections"]`, hash baseline
  `b460d0…fedd069a`, M11 smoke-test gate; `_maintenance_automation.md` L69/L89.
  _cutover:_ SC leaves the whitelist; the `.obsidian/plugins/` hash tripwire stays
  for any remaining Obsidian plugins (Bases is core). _owner/gate:_ maintainer.
- [ ] **SC MCP registration** (`.mcp.json`, `.claude/settings.json`,
  Codex `.codex/config.toml`). _cutover:_ deregister the smart-connections MCP
  server; optionally register the `brain` MCP adapter (shipped, S06). _owner/gate:_
  maintainer (per-client config isolation, CLAUDE.md L18).

## D. Bases (Obsidian core plugin)
- [ ] **The 8 Bases** (`90 System/Bases/`: `Latest Only.base`, `As Of.base`,
  `Version Chain.base`, `Open Items.base`, `Sources.base`, `People.base`, …).
  _cutover:_ `Latest Only`/`As Of`/`Version Chain` → `brain` temporal/frontmatter
  queries; `Open Items`/`Sources`/`People` → `brain bases` structured frontmatter
  view (shipped, RET-04). Decision per-Base: become a `brain` query vs retire.
  _owner/gate:_ maintainer + retire-gate B.
- [ ] **Bases verifier** (`90 System/_bases_verifier.py`; `_maintenance_automation.md`
  L88, Acme-vault-health §7). _cutover:_ retire with Bases, or keep until Bases are.
  _owner/gate:_ maintainer.

## E. Scheduled tasks (10) — one row each (`90 System/_maintenance_automation.md`)
- [ ] **Acme-vault-health** (L21/L90) — calls `mcp__smart-connections__stats` (§9),
  `_bases_verifier.py` (§7), `.obsidian/plugins/` hash (§5/§8), `_smoke_test_retrieval.py`
  (§ smart-env shape). _cutover:_ repoint §9→`brain status`; §-shape→`brain selftest`;
  keep plugin-hash for residual Obsidian.
- [ ] **Acme-vault-integrity-scan** (L22) — §A near-dup uses **SC embeddings**
  (cosine). _cutover:_ repoint to `brain` vectors (sqlite-vec) — the substrate
  exposes embeddings directly, no MCP round-trip.
- [ ] **Acme-vault-inbox-ingest** — ingestion pipeline writes notes SC then indexes.
  _cutover:_ pipeline output feeds `brain sync` (incremental indexer is the drain).
- [ ] **Acme-vault-daily-check** — reads `_index.md`/Bases freshness. _cutover:_
  `_index.md` regen stays (feeds `brain` ingest too); Bases-freshness→`brain status`.
- [ ] **Acme-chief-of-staff-nightly** — retrieval over the cascade. _cutover:_
  swap cascade Step 1 for `brain search` (rides the rule-B change).
- [ ] **Acme-vault-graphify-discovery** — discovery graph (independent of SC).
  _cutover:_ relate to `brain graph-expand`; likely keep as discovery-only.
- [ ] **Acme-vault-recommendations-aging** / **handoff-freshness** /
  **write-audit** / **graph-health** (folded) — no direct SC/Bases dependency
  (audit-chain + filesystem). _cutover:_ no change beyond the shared cascade rule.
- _owner/gate:_ all scheduled-task edits go through `/skill-creator` per the Skill
  Rule + outcomes contract; maintainer-gated deploy. See `cutover-scheduled-tasks.md`.

## F. Bootstrap & session discipline
- [ ] **Session-bootstrap rule** (`.claude/rules/session-bootstrap-discipline.md`)
  — step 6 `_index.md` (freshness gate), step 7 `Open Items.base`, step 8
  `mcp__smart-connections__stats`. _cutover:_ step 7→`brain bases`, step 8→`brain
  status`; step 6 `_index.md` stays (cheap catalog; also a `brain` ingest input).
- [ ] **Retrieval-cascade discipline** Step 0/0.1 `_index.md` + zone catalogs
  (`_build_index.py`). _cutover:_ keep (lexical pre-filter is substrate-agnostic
  and complements `brain` Step 0).

## G. Index / catalog generators
- [ ] **`_build_index.py`**, **`_entity_catalog.py`**, **`_link_matcher.py`** —
  _cutover:_ KEEP. They produce `_index.md`/zone catalogs/`_entity_catalog.json`
  that feed both lexical Step 0 AND `brain` ingest; they are not SC-coupled.
- [ ] **Graphify** (`99 Workspace/_graphify/`) — discovery graph. _cutover:_
  maps to `brain graph-expand` (discovery-only, never authoritative); keep or fold.

## H. Docs / pointers
- [ ] **CLAUDE.md "Pointers — Where Things Live"** (Bases (8) L104; retrieval
  contract/eval/smoke-test pointers) + any doc naming Obsidian/SC/Bases/`.smart-env`
  as the retrieval substrate. _cutover:_ repoint pointers to `brain` equivalents
  in the same edit that rewrites the Retrieval Cascade block. _owner/gate:_ maintainer.

## Completion criterion (for S10) — MET
Every row has a location, an assertion, a cutover action, and an owner/gate.
This filled inventory is the **input to the follow-on operational-cutover plan** —
**not executed here.** Companion hooks: `docs/operations/cutover-command-map.md`,
`docs/operations/cutover-scheduled-tasks.md`,
`docs/operations/cutover-retirement-and-dualrun.md`.
