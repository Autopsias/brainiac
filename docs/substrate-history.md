# Substrate history, rationale, and benchmark deltas

> Historical incident post-mortems, ticket IDs, date-stamped rationales,
> superseded-option commentary, and measured benchmark deltas archived out of
> `AGENTS.md` and `src/brain/cli_help.py` (context diet, 2026-11-26) so `AGENTS.md`
> stays a pure `<= 20 KB` normative contract that fits every harness startup
> budget without truncation. Active rules live in [`../AGENTS.md`](../AGENTS.md);
> architectural decisions live in [`adr/`](adr/); closed-plan notes live in
> [`closed-plans-history.md`](closed-plans-history.md).

---

## 1 · Context diet and harness-budget history

- **2026-08-22 (`0b4defc2`, S05 diet):** `AGENTS.md` reached 89,578 bytes; an earlier
  diet split off `AGENTS-core.md` (<=200 lines imported by `CLAUDE.md`) plus 7
  path-scoped `.claude/rules/*.md` mirrors, and moved §7 prose to
  [`substrate-readiness.md`](substrate-readiness.md).
- **2026-09-19 (ADR 0012):** `AGENTS.md` grew to 127,734 bytes (+38,156 bytes in
  28 days, ~1,363 bytes/day), silently truncated by `codex-cli 0.153.4` at its
  default 32,768-byte `project_doc_max_bytes` (byte 32,768 cut inside §4, hiding
  §5–§9), while 5 paragraphs in `.claude/rules/*.md` drifted from `AGENTS.md`.
  Option B temporarily raised `.codex/config.toml` `project_doc_max_bytes = 262144`
  and added copy/fit guards (`tests/test_agents_md_fits_codex.py`,
  `tests/test_agents_md_rules_copies.py`). See [`adr/0012-agents-md-fits-every-harness.md`](adr/0012-agents-md-fits-every-harness.md).
- **2026-11-26 (S13 `diet-01`):** `AGENTS.md` (126,103 bytes) was distilled to a
  pure `<= 20,480`-byte normative contract; historical rationale and benchmark
  tables moved here; all 7 mirrored `.claude/rules/*.md` files were retired;
  `CLAUDE.md` was updated to import `@AGENTS.md` directly (with personal host
  ops notes moved to untracked `~/.brain/host-ops.md`); `AGENTS-core.md` was
  retired to a pointer stub; `.codex/config.toml` restored
  `project_doc_max_bytes = 32768`; and `src/brain/cli_help.py` was trimmed from
  248 lines to a concise CLI reference.

---

## 2 · Substrate layout and indexing-scope exceptions (§1)

- **INT-03 (excluded machine output):** On the reference deployment, 78
  successive revisions of one in-flight draft under `cos-ops/` crowded retrieval
  before `brain.notes.MACHINE_OUTPUT_DIRS` was excluded by an anchored top-level
  rule (`sync`/`rebuild` report `excluded_machine_output`).
- **INT-01 & INT-04 (`<index dir>/cos-approved/` and `cos-attachment-anchors/`):**
  See `docs/cos-ops.md` §2c for the approved queue (only copy between owner
  accept and `brain sync` drain) and host-signed attachment anchors (email-derived
  MNPI floor + byte provenance; missing anchor fails closed until re-accepted).
- **CAP-01 / CAP-02 (`<index dir>/cos-corpus/`):** Added so re-judging message
  text no longer requires a 90-minute live run against discarded ingestion
  ledgers (`docs/cos-ops.md` §2d). Whole-file pruning preserves replay
  denominators; unclosed live files are held rather than guessed.
- **gap-05 (`<index dir>/cos-runs/`):** Run manifests, recorded verdicts, and plan
  bindings lived at `<vault>/.brain/cos/host/runs` until 2026-08-16. Because
  that path sits inside the Cowork workspace, "host-private" held only in VM
  rules and not at the filesystem layer (a VM write there could decide candidate
  claimability and plan dispatch). Moving them off-mount closed that gap;
  migrated records mismatching existing bytes fail to `INCONCLUSIVE`.

---

## 3 · Note shape, templates, and navigation (§2–§3)

- **ADR-0003 Rulings 1–3:** Established `inbox/_quarantine/` (Ruling 1), optional
  bitemporal chains (Ruling 2), and typed entities `person | company | project |
  meeting | decision | concept | daily` plus warn-only lint (`TMP-04`, `TMP-05`,
  Ruling 3).
- **PRV-01 / PRV-02:** Kept `provenance.*` as flat dotted keys matching literal
  `provenance.trust`, marking `provenance.verified: true` as host-parsed from
  archived originals.
- **HYG-03:** Added generated per-zone `catalog.md` views and the State-MOC
  per-section `Updated: YYYY-MM-DD` pattern (90-day staleness threshold inherited
  from ADR-0003 autoresearch convention).

---

## 4 · Capture rules, corpus invariants, and deliverables (§4)

- **2026-07-11 (automatic metadata maintenance):** Owner ruling shifted metadata
  hygiene (`WSP-01` sweep, `VER-01` `…-vN` supersession, `PAR-01` PARA filing,
  `NAV-01` backlinks/catalogs) into nightly `brain maintain`, keeping only prose
  synthesis and `index.md` content in sessions.
- **2026-07-20 (`GRH-01`):** Wednesday `graph_hygiene` excludes generated maps
  (`backlinks.md`, `catalog.md`) from knowledge-layer counts because they
  wikilink every note in their zone by design, which previously hid real orphans
  while registering the maps themselves as noise orphans.
- **`BAK-04` standing linking lane:** The corpus accumulated ~1,300 unlinked
  `raw/` sources before tracking began; `$BRAIN_WEEKLY_LINK_BUDGET` defaults to
  40 based on measured median weekly `raw/` intake (~34) plus headroom.
- **2026-08-10 (`WAT-01` corpus invariants):** Four corpus invariants drifted for
  months before counting (e.g. a 2,132 baseline) because no number tracked them.
  Thresholds use ratcheting absolutes instead of week-over-week percentages
  because percentages divide by zero as backfills succeed.
- **2026-08-19 (`brain.invariant_floors`):** The reference vault reported
  `unlinked_sources = 0` while ~120 wrongly retired documents were absent from
  the denominator; recording `population` alongside each floor prevents a
  shrinking/damaged corpus from lowering a floor (`floors_declined`).
- **2026-08-14 (`ENF-06` operational output exclusion):** A historical drop put
  264 audit/nightly/eval files into `raw/`, where the ingest wrapper overwrote
  their leading `type:` with `type: source`, inflating unlinked-source counts.
  Moving `OPERATIONAL_SOURCE_TYPES` to `inbox/_operational/<type>/` (while keeping
  `report`, `review`, `analysis`, `proposal` eligible when about the work) fixed
  this.
- **2026-08-20 (`emitter_output` and Chief-of-Staff briefs):** An untracked
  log-rotation script dropped 22 daily `Log rotation proposal` (`type: proposal`)
  files counted as unlinked sources. Anchoring `proposed_by:` / `generated_by:`
  (program path) or `provenance: Generated by <program>` avoided false positives
  that a loose `.py` substring match triggered on `decision-draft` and
  `skill-patch` notes (pinned as known negatives in
  `tests/test_corpus_invariants.py`). Separately, the owner ruled the 34
  existing Chief-of-Staff briefs in the reference vault worthless and retired
  them manually.
- **Hand-retirement incident:** A 433-file hand retirement of "machine residue"
  swept ~100 real documents; only the retrieval golden set caught it days later,
  motivating pre-write classification review and moving to
  `inbox/_quarantine/_resolved/<batch>/` rather than deletion.
- **2026-08-12 (`ENF-03` & `ENF-04` cross-tier content & admission guard):**
  `cross_tier_twins` read 0 on filenames while renamed/re-extracted duplicates
  sat across tiers, motivating 5-shingle Jaccard (`>= 0.60`) + `undecided`
  word-set band (`>= 0.60`) verified by `tools/crosstier_coverage.py`. Meanwhile
  `ingest/pipeline.py` declared `Internal` on drop-zone ingests, letting copies
  of higher-tier notes enter at `Internal`. `brain.ingest.tierguard` fixed this
  at write time, including scaling bottom-k screening for bodies above `ENF-01`
  with under 48 distinct words and separating `no_corpus` from `unguarded_ingests`.
- **`CUR-01` & `SUP-01` (2026-08-04 – 2026-11):** Auto-supersession stays
  limited to `DDP-01` (sha256) and `VER-01` (`…-vN`, plus `…-vN-preview`
  renditions). Name/email/format twins (note: a deck's `.pdf` vs `.md` measured
  shingle 0.53) remain proposal-only via `CUR-01`. In `SUP-01` (2026-09-18),
  reference-vault telemetry showed 127 of 2,055 `previous_version` links lacked
  reciprocals vs 0 of 2 `replaces:`, so `supersede_declared_failed` keys on
  `replaces:` alone. Later hardening (`F12`/`F14`, `F32`, `F44`): version-link
  `runs.jsonl` moved off-mount to `<host-private base>/cos-version-links/<vault slug8>/`;
  drop-lane `SUP-01` declarations stage as default-reject owner questions
  (`<=Internal` only, excluded from standing accept-all); broker `supersede`
  requires explicit `replaces:` or name family (ignoring body `[[id]]`
  citations) and refuses `drop-lane-successor`; and `auto_version_chains`
  verifies notes against audit-signed content hashes before linking.
- **2026-08-24 (`DLV-01`, `DLV-04`, `DLV-09`, ADR 0010):** Deliverables use
  `deliverable: true` instead of altering `type:` because `type: decision` is
  exact-equality selected. Drop lane defaults `.classification` to `MNPI` before
  `tierguard` because `tierguard` only raises against existing twins. Shelf uses
  plain copies (no symlinks — broken on Windows, cloud sync, VirtioFS) and
  `brain shelf census --json` (frontmatter scan) because `bases-query` omits
  `deliverable` and caps at `k=50`.

---

## 5 · Retrieval ranking, benchmarks, and security rulings (§5)

### Ranking & reranker calibration history

- **`RET-11` (`RRF_K_FUSE = 3`, 2026-08-05):** Replaced `k=60` fusion (`BRAIN_RRF_K=60`
  rollback) while keeping ADR-0008's calibrated `rrf_k=60` key for the bounded
  exact leg (`BRAIN_EXACT_LEG_ENABLED=0` rollback).
- **`BR-03` rerank default-on (owner ruling 2026-08-04, revised same day):**
  Measured on the 66-query golden set (`eval/FOLLOWUPS.md` #4): candidate window
  20 raised `mrr@10` `0.267 -> 0.411` and `hit@1` `0.212 -> 0.349` at ~5.5s p50 /
  8.2s p95 latency (slowest golden query 10.5s; 30s `$BRAIN_RERANK_TIMEOUT_S` is
  ~2.9x worst case). Window 50 initially shipped on a mislabelled window-20
  latency row, actually costing p50 68.0s and blowing the 30s timeout on 85% of
  queries (`eval/FOLLOWUPS.md` #6), so default reverted to 20 (ceiling 50).
- **`RK-02` adaptive rerank gate (2026-08-04):** Measured on the same 66-query
  set (`eval/FOLLOWUPS.md` #5, `eval/runs/rerank-gate-calibration-2026-08-04.json`):
  on the 7 queries with a unique exact-identity pin, skipping cross-encoder
  reranking scored `+0.0000` delta across `recall@10`, `recall@20`, `mrr@10`, and
  `hit@1` (against both window-20 and window-50 arms) while dropping median
  latency from 6.2s to 200ms.
- **`RET-05` / `RET-05b` multilingual variants (2026-08-09 – 2026-08-12):**
  - Per-variant fetch stays shallow (`per_query_k=20`): widening 20 -> 80 dropped
    fan-out recall `0.736 -> 0.625`.
  - **2026-08-10 (`_decisions/anylang-s05-ship-ruling.md`, `_evidence/anylang/s04-variant-readout.md`):**
    On held-out `s05-2026-08-09-expanded114`, overall recall@10 was `+0.0380`
    (`p = 0.2509` vs pre-registered bar `+0.0890`, `p < 0.05` — NULL), keeping
    variants caller-opt-in rather than an engine default; `cross_lingual_en_pt`
    moved `0.0000 -> 0.2000` (`recall@20 +0.0980`), while Spanish stayed `+0.0000`
    (`es` was 0.65% of classified notes, below census threshold).
  - **2026-08-12 (`_decisions/invariants-s11-ship-ruling.md`, `_evidence/invariants/s10-claim-readout.md`):**
    `--rerank-fused` (default-on for 2+ variants) measured `+0.0643` recall@10
    (`p = 0.0284`, 6 wins / 1 loss / 50 ties) on **TRAIN-HALF ONLY** (held-out
    `s08-2026-08-11-expanded` remained `CLAIM NOT MADE`; `+0.1667` was
    mis-attributed against an un-reranked baseline, 57% from existing reranking).
- **`BR-02` Gate 0 (2026-08-04) & `RET-01` zone-authority prior (2026-08-04 – 2026-08-09):**
  - Corrected earlier misdiagnosis blaming embedder alignment: RRF buries
    dense-only cross-lingual/paraphrase hits below weak multi-leg matches. On
    `0.19.24`, 12 PT golden queries scored recall@10 `0.0` fused vs `0.417` EN
    paraphrases vs `0.500` dense-only (median rank 2 -> exited at rank 52).
    Within English, note paraphrase scored `0.417` vs title wording `1.000`.
  - `$BRAIN_ZONE_WEIGHTS` (`eval/FOLLOWUPS.md` #7 & #9, `eval/zone_prior_calibration.py`)
    measured held-out `mrr@10` `0.198 -> 0.386` (`p=0.011`) and
    `cross_lingual_pt_en` (renamed from `monolingual_pt` on 2026-08-09 when
    22/22 gold docs turned out English) `0.000 -> 0.458` at `W=3.0`. Ships neutral
    because 66 queries cannot pin the constant (train argmax `3.0`, held-out
    argmax `5.0`, MDE `0.199` at 80% power) and `W=3.0` hurt `lexical_identifier`
    (`0.750 -> 0.725`), temporal `mrr@10` (`0.431 -> 0.344`), and
    `cross_lingual_es_en` (`0.250 -> 0.167`), collapsing at `4.0–5.0`. Reference
    vault had 0 of 2,570 notes carrying `source_zone`. `cross_lingual_es_en`
    moved off `0.000` after migrating `e5-small` -> `bge-m3-int8`
    (`eval/FOLLOWUPS.md` #11).
  - Unstamped `eval/FOLLOWUPS.md` item 10 (`d5b2c58`) previously caused a plan to
    re-implement an already-shipped fusion fix, establishing the same-commit
    `[verified <sha|date>]` stamping rule.

### Temporal, decision-layer, and security incidents

- **`TMP-03` G&P benchmark failures (2026-07-11):** Round 3 read the latest 6-pager
  head and asserted a mechanism overturned a month earlier in a `type: decision`
  note; Round 4 read a swept advocacy memo ("doesn't pick the perimeter") and
  recommended re-baselining on an unadopted option — establishing `brain dossier`
  authority and `tensions` surfacing.
- **2026-08-25 (Owner ruling A — hiding retired versions):** Retired versions
  took 29% of top-10 slots across 15 document queries on the reference vault, so
  `search`/`grep`/`recent` now filter `is_latest_version: false` by default
  (unless `--include-retired`).
- **2026-09-02 (`M-8` / A-07 Cowork decision drafts):** Owner refused demoting
  sandbox `type: decision` drafts to `type: note` (loss L7; 32 such notes
  existed across vaults on 2026-09-02; see `docs/security-acceptances.md` A-07).
- **2026-09-04 (`M-3b` concealment & `A-19` 2026-11-17 `own_output`):** Owner
  ruled `clean` is a frontmatter declaration, never an audit-verified provenance
  claim; `A-19` marked drop-lane and weekly-synthesis outputs `own_output: true`.
- **2026-09-10 (Cowork email retro):** Tightened email lookup to one `search`
  `k=5`, no `find` on mount, verbatim body + explicit truncation/missing fields.
- **2026-07-10 & 2026-08 (`VULN-3385`, `VULN-3386`, `VULN-3419`, `A-01`, `A-05`, `A-06`):**
  Host default egress cap moved from `Internal` to full vault (2026-07-10);
  relocated Cowork workspaces closed `VULN-3385` (2026-08); `VULN-3386` replaced
  unsigned VM tier files/env vars with host-signed `vm-egress-tier.signed` +
  `pinned-verify.json`; owner rulings on 2026-08-17 / 2026-08-31 / 2026-09-01
  confirmed full-vault broker default (`docs/security-acceptances.md` A-01/A-05/A-06).
- **`EXC-01` (2026-08-22):** Default-deny `MNPI` mapping on unlabelled notes
  cannot be used as a write-raising target (previously lifted `Internal` pairs
  to `MNPI` when matched with an unlabelled twin).
- **`SEC-07` generated ring (2026-09-01+):** Measured harvesting `concept`
  (`API`/`Adobe`), `company` (`IBM`/`Deloitte`), and `person` (41 first names)
  over-blocked; restricting generated egress keywords to `project` notes
  avoided false positives, and isolating `overlay/keywords-generated/` from
  ingest avoided re-tiering 659 of 670 MNPI docs.
- **`INT-02` (2026-08-07), `A-14` (2026-09-11), `M-7` (2026-09-02):** Moved
  content-drift dispositions off `<vault>/.brain/` so the VM could not mark edits
  `explained`; switched drift and `AuditChain` log parsing to raw bytes (`b"\n"`
  splitting, avoiding `\r`/`\v`/`0x0b`/U+2028/U+2029 ambiguity) and tagged old
  hashes `convention: "text (legacy, unverifiable)"`.

---

## 6 · Host / VM trust split history (§6)

- **`UPD-01` (`stuck-drafts` 48h alert):** Triggered after a skipped duplicate id
  sat unseen in `capture-inbox/` for 11 days.
- **`PRV-10` (2026-08-17):** Added VM `provision-request` -> host
  `provision-drain` flow across registered parent roots.
- **2026-07-20 (Scheduled task curation & `CC-01`/`CC-02`):** Owner ruled against
  fixed task caps in favor of merit-gated umbrella folds (`brain-nightly` +
  `brain-synthesis`), and added off-mount advisory `fcntl.flock` + `BEGIN
  IMMEDIATE` write retries + host-private `ENF-01` supersede crash journal.

---

## 7 · Test-suite & session-memory history (§8–§9)

- **2026-09-10 – 2026-11 (pre-hand-in rule tests, `F41`, and hook timeouts):**
  6 of 31 `agentic_build` sessions failed `gate:pytest-brain-here` on first
  attempt (4–10 min rework each) by skipping repo-wide rule tests
  (`test_client_name_gate_splits_identifiers.py`, `test_export_cleanroom.py`,
  `test_cos_pathguard_census.py`, `test_cowork_skill_verbs.py`). On 2026-11-19,
  three tests failed only at the final gate because changed private signatures
  and shell flags were not checked via `git grep -l '<symbol>' tests` before
  closing; a same-day hand-run release also skipped fast-forwarding the main
  checkout before deploy, establishing the `publish-release` worktree rule.
  In November 2026, `F41` bounded egress-ring reads (file/byte/term caps and
  special-file guards), and Claude Code PreToolUse hook timeouts were corrected
  to be understood as call refusals ("the hook did not finish, so this call was
  not run") rather than fail-open allows.
- **2026-08-13 & 2026-09-05 (parallel test lessons):** Dropping `-x` avoids
  wasted re-runs hiding later failures. On 2026-09-05, concurrent self-hosted
  GitHub Actions runners caused 4 test kills and a 2.6x runtime spread on the
  same clean tree (673s, 353s, 257s for 6,509 tests); `vm_stat` read 68 MB free
  while `memory_pressure` reported 44% free; and one session burned 82 minutes
  over 6 redundant runs (12:51, 18:37, killed, 32:02 at `-n 4`, 10:18, 8:45)
  with the only red failure already explained. Two historical deselects turned
  out to be a date-rotted clock and a live-cwd vault leak
  (`tests/conftest.py::_no_live_cwd_vault`).
- **Session-memory milestones (§9):**
  - **2026-07-13 (PUSH model):** `hot.md` redesigned from a must-clear queue
    (which grew to 32 KB unread) into an append-only fold log, while owner
    decisions moved to `inbox.jsonl`.
  - **2026-08-14 & 2026-08-22 (`EXC-03`):** Extracted `brain alerts` out of a
    Claude Code-only hook so Cowork VM sees regressions; replaced raw
    `inbox.jsonl` reads on VM ("attacker-writable file existence is not
    evidence") with signed `exceptions.json` verified against `pinned-verify.json`.
  - **2026-08-20:** Switched `alerts` from `*.marker` files to
    `notify-sent/current.json` after 3 of 4 alerted lines proved already resolved.
  - **2026-09-02 / 2026-09-04 (`M-6`, `A-08`):** Added dynamic per-run marker
    fencing to `session-start.sh` against triple-backtick escapes while keeping
    free-form synthesis text (refused loss L5; see `docs/security-acceptances.md`
    A-08). Host `alerts` key verification measured ~1s on 2026-11-11.
