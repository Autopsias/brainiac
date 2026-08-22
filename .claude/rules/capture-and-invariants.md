---
paths:
  - "vault/raw/**"
  - "vault/brain/**"
  - "src/brain/invariants.py"
  - "tools/validate.py"
---

# Capture rules and corpus invariants

> Moved verbatim from `AGENTS.md` during the S05 context diet (2026-08-22).
> `AGENTS.md` remains canonical for Codex/Gemini; this rule is the Claude Code
> path-scoped copy, loaded only when a file under its `paths:` glob is touched.

## 4 · Capture rules

1. **Sources enter `raw/` immutably.** Compute `sha256` of the body at capture;
   write it to frontmatter; never touch the file again.
2. **Insight lives in `brain/`.** When a source matters, write an atomic
   `brain/` note that links back via `source:` and `[[raw/...]]`.
3. **One idea per note.** Split rather than grow. Densely link instead of
   foldering.
4. **The index is maintained, not crawled — and maintenance is AUTOMATIC**
   (owner decision 2026-07-11). The nightly `brain maintain` self-organizes
   the vault's METADATA: it sweeps settled workspace files into `inbox/`
   (WSP-01), stamps supersession chains across explicit `…-vN` version
   families through the audited `supersede` path (VER-01), files brain/
   notes into their PARA zone by frontmatter (`type: project` →
   `projects/`, retired notes → `archive/`; PAR-01), and regenerates
   `backlinks.md` + per-zone `catalog.md` (NAV-01) before republishing the
   snapshot. `tools/validate.py --backlinks --catalogs` remains the manual
   equivalent. A Wednesday-gated `graph_hygiene` branch (GRH-01,
   2026-07-20) adds cheap, no-model wikilink hygiene: knowledge-layer
   (`brain/` zone) orphan/dangling-link/connected-component counts
   (generated maps — `backlinks.md`/per-zone `catalog.md` — excluded from the
   knowledge layer entirely, since they wikilink every note in their zone by
   design and would otherwise both hide real orphans and register as
   constant-noise "orphans" themselves), persisted into
   `maintain-state.json` + `health-history.jsonl` and surfaced via `brain
   health-report`'s "Graph hygiene" section; an orphan-count jump past
   `$BRAIN_GRAPH_ORPHAN_GROWTH_MAX` (default 10) since the last run logs a
   `hot.md` line for the weekly synthesis session to work — never an owner
   ritual, never an inbox item. Only SYNTHESIS (writing/promoting prose
   notes, `index.md` content) stays session work — the folds manage
   metadata and generated views, never note bodies.

   **The standing linking lane (BAK-04).** A `raw/` source nothing cites is
   reachable only by its own text, and the corpus accumulated ~1,300 of
   them before anyone had a number. The daily `corpus_invariants` fold
   (WAT-01, `brain.invariants`) therefore also drops
   `<vault>/.brain/curation/unlinked-sources.json` — the SAME population as
   the `unlinked_sources` metric and the same G12 exclusion set, never
   re-derived — sliced **worst-first: highest classification first
   (unlabelled ranks MNPI, as at the egress gate), then longest-unlinked**,
   capped at `$BRAIN_WEEKLY_LINK_BUDGET` (default **40**, the measured
   median weekly `raw/` intake of ~34 plus headroom). The **weekly Sunday
   synthesis session** works that file: it reads the sources and writes new
   derived `brain/resources/` notes citing them, grouping freely where
   sources genuinely share a subject. **No new scheduled task** — the lane
   rides the existing fold and the existing session (§6). Two rules the
   lane cannot break: a note must say something true its own title does not
   (a title-restating stub is worse than no note), and the body cites the
   source as `[[<bare-id>]]` — the `[[raw/<id>]]` form belongs in `source:`
   frontmatter and creates **no graph edge**, so in a body it would inflate
   the metric while linking nothing. Whatever the session does not reach is
   simply next week's list; the `unlinked_sources` metric is what proves
   the lane keeps working.
5. **Capture under the VM is a *draft*, not a commit** — see §6.
6. **A corpus-invariant fix never ships without its metric in the same
   change (WAT-01, 2026-08-10).** A corpus invariant is a property the whole
   corpus is supposed to hold — every raw source is reachable from some
   note, one document carries one classification, a supersession link joins
   two real documents, every gold document is reachable by some ranking leg.
   Four of those drifted for months each, and the single reason was the same
   every time: **nobody had a number for them, so nothing could notice.** So
   the rule is mechanical — if you fix, backfill, or guard a corpus invariant,
   the SAME change adds (or updates) its count in the `corpus_invariants`
   nightly fold (`src/brain/invariants.py`), which trends it in
   `health-history.jsonl` and renders it under `brain health-report`'s
   "Corpus invariants" section. Thresholds there are **absolute and
   ratcheting** — each metric's threshold is the best value ever recorded, so
   the same rule alerts from a 2,132 baseline and from a zero one; never a
   week-over-week percentage, which dies at zero exactly when a backfill
   finally works.

   **A floor is only earned on a corpus that did not shrink
   (`brain.invariant_floors`, 2026-08-20).** Every one of these metrics counts
   DEFECTS IN A CORPUS, so REMOVING documents improves the score — the ratchet
   can be won by damage. The reference vault read `unlinked_sources = 0` across
   19 consecutive runs on 2026-08-19 while ~120 documents from the wrongful
   433-file hand retirement were still out of the vault; they were reinstated
   the next day (notes 2490 → 2612) and every healthy run since reported a
   REGRESSION against a floor only a damaged corpus could reach. An alarm that
   can never be satisfied is an alarm nobody reads, which is the exact failure
   this fold exists to prevent. So each floor is stored WITH the `population`
   it was recorded against (same shape as `unreachable_gold_labels`), and a
   metric may not set a new floor on a run whose population fell below that
   basis. The guard **only ever declines to LOWER a floor** — it never raises
   one, never suppresses a regression, and never touches the reported value;
   a genuine improvement over the whole corpus ratchets exactly as before, and
   the metrics that report no population (pair and family counts) are
   untouched. A declined floor is reported as `floors_declined`, never
   silently skipped. The fold carries a **dead-man's switch**: its own
   last-successful-run age is a metric, and a row older than
   `$BRAIN_INVARIANTS_MAX_AGE_DAYS` (default 3) — or missing on a vault whose
   other branches run — is DEGRADED in `brain doctor`, in `brain
   health-report`, in the weekly synthesis watchdog, and in the SessionStart
   alerts hook. Sources that are unlinkable BY DESIGN (quarantined,
   superseded, `inbox/`/`overlay/`) are excluded by ONE shared definition
   (`invariants.link_coverage_exclusion`) and **counted separately** — "0
   unlinked" must never quietly mean "0 except the ones we skip".

   **A vault's own output is not a source (ENF-06, 2026-08-14).** Audit
   records, nightly logs, health alerts and eval runs re-ingested into `raw/`
   arrive declaring their kind in their OWN leading frontmatter, which the
   ingest wrapper then overwrites with `type: source` — after which nothing
   tells them apart from a client document. One historical drop of that shape
   put **264** of them into the reference vault, and the link-coverage metric
   counted every one as a source waiting for a note to be written about it.
   Both ends now read `invariants.OPERATIONAL_SOURCE_TYPES`: ingestion sets
   such a file aside under `inbox/_operational/<type>/` as **skipped, never
   quarantined** (nothing failed — it is simply not knowledge), and the metric
   excludes it as `operational_artifact`, counted like every other exclusion.
   The set is explicit and conservative: `report`, `review`, `analysis` and
   `proposal` are NOT in it, because what decides is the SUBJECT, not the
   author. A machine wrote most of this corpus. The test is whether the file
   is a record of THE VAULT RUNNING — an audit of its own chain, a nightly
   log, a health alert, a backfill report, an eval baseline, a tombstone, a QA
   run — or a DOCUMENT ABOUT THE WORK. The second stays reachable however it
   was produced. `BRAIN_INGEST_ALLOW_OPERATIONAL=1` admits one deliberately.

   **When a SCRIPT emits one of the protected types, type is the wrong
   signal (`emitter_output`, 2026-08-20).** The paragraph above is right and
   is not weakened: `proposal`, `report`, `analysis` and `review` stay OUT of
   `OPERATIONAL_SOURCE_TYPES` because a human writes those about the
   business. The gap it leaves is narrower — when `_log_rotation.py` emits
   `type: proposal` once a day, type says "keep" and nothing else disagrees.
   The reference vault carried **22** daily `Log rotation proposal` files
   from one script, each counted as a source awaiting a note. So the second
   signal is ORTHOGONAL, never a wider type list: the file's own frontmatter
   must SAY a program produced it — `proposed_by:`/`generated_by:` whose
   value IS a program path, or `provenance:` that literally BEGINS
   `Generated by <program>`. It is counted under its own
   `emitter_output` reason and rendered per-reason in `brain health-report`,
   because an aggregate "N excluded by design" hides a newly added exclusion
   class completely.

   **The anchoring is load-bearing, and only measurement found it.** A looser
   rule — any of those keys whose value merely MENTIONS a `.py` — matched 27
   of the reference vault's 98 unlinked sources; two of the extra were a
   `type: decision-draft` and a `type: skill-patch` whose prose provenance
   names the scanner that INFORMED them. Excluding a decision document is the
   exact failure the 433-file hand retirement made. Anchored, it matches 25,
   every one machine exhaust, and takes `unlinked_sources` 98 → 73. Both
   false positives are pinned as known negatives in
   `tests/test_corpus_invariants.py`; loosening the pattern fails them.

   **The Chief-of-Staff briefs are the one named exception (owner ruling
   2026-08-20).** They are about the business, so the paragraph above would
   keep them — but the owner has read them and ruled the ones produced so far
   worthless, and the 34 in the reference vault are retired. Nothing automatic
   does that: they declare no type and match no rule here. Retiring them was an
   owner act and stays one.

   **A HAND retirement is not covered by any of this, and it is the one that
   went wrong.** Two sessions retired 433 files from the reference vault as
   "machine residue"; exactly ONE matched the set above, and the batch took
   roughly a hundred documents this paragraph protects, including a
   `type: design` architecture note. Nothing noticed for days — the RETRIEVAL
   GOLDEN SET did, reporting 19 labels pointing at documents no longer in the
   index. So: classify by subject and show the split BEFORE writing, never
   after. Retired files are MOVED to `inbox/_quarantine/_resolved/<batch>/` and
   never deleted, so a reinstatement is a move back into `vault/raw/` plus a
   `brain sync` — the original signatures still cover the bytes, and
   `verify-audit --check-content` confirmed zero drift across all 112 restored
   on 2026-08-20.

   **A duplicate is decided on CONTENT, never on a filename (ENF-03,
   2026-08-12).** `cross_tier_twins` compares one id shape and therefore
   reads 0 while renamed, re-extracted and re-versioned copies of the same
   document sit at two classifications; ENF-02 tried to close that on names
   and was withdrawn whole when its "138 detections" proved to be 138
   filename matches and 0 content matches. So `cross_tier_duplicates` and
   `cross_tier_candidates` compare bodies: 5-word-shingle Jaccard ≥ 0.60
   DECIDES the same document, word-set Jaccard ≥ 0.60 without it is
   **UNDECIDED and reported as such** — a detector that guesses on the
   undecided band is the failure mode, and a metric with no undecided bucket
   (the old one) can never report anything but zero. The ENF-01 body-size
   floor applies first, and the shared exclusion definition applies with one
   documented exception: **superseded notes are counted, not excluded** —
   retiring a note does not remove it from the index, so a retired low twin
   still leaks at its own tier. Coverage of the detector is a fraction with
   its denominator on the report row, and is re-measured against an
   exhaustive all-pairs scan by `tools/crosstier_coverage.py`, never asserted
   from the screening algorithm's own arithmetic.

   **And the tier is decided at ADMISSION, not counted afterwards (ENF-04,
   2026-08-12).** `ingest/pipeline.py` DECLARES `classification: Internal`
   for every drop-zone ingest, so the same document under a different id
   entered below its twin and an Internal-capped reader — the Cowork VM's
   default — reached high-tier substance through the low copy.
   `brain.ingest.tierguard` closes that at the write path: before a source is
   signed, its BODY is compared against the corpus through ENF-03's own
   primitives (imported, never re-implemented — the engine holds ONE notion
   of document identity), and a near-duplicate at a higher tier makes the new
   source enter at the **high-water mark**. Five rules it cannot break: it
   **only ever raises** (nothing outranks MNPI, so the email/attachment lane
   returns before any work); the **undecided band fails CLOSED** to the higher
   tier, because a detector reports a corpus state while a guard must choose
   one, and the costs are asymmetric — over-classification is visible and
   reversible through the audited path, a leak is neither; **below the ENF-01
   floor it refuses to judge** rather than guess; **every verdict is stamped
   on the note** (`classification_guard`, `…_leg`, `…_reason`), so an
   unraised note proves the guard ran and an unexplained tier change cannot
   happen; and its **screen gate scales with the smaller sketch**, because a
   bottom-k sketch of a set smaller than k is the whole set and a body above
   the floor with under 48 distinct words could otherwise never be screened
   in at all. `unguarded_ingests` (metric 5) ratchets on the ONE number that
   should be zero — sources admitted while the guard COULD NOT RUN (no index
   connection, a read error, disabled). A guard that RAN against a corpus
   holding nothing comparable is a separate status, `no_corpus`, reported on
   its own row and never inside that number: a cross-tier leak needs an
   existing higher-tier near-duplicate, so an empty corpus has nothing to leak
   from, and counting it made every new vault's FIRST document trip a
   floor-of-zero ratchet (2026-08-17). Raises
   are trended PER LEG and never alerted on: they are monotone over an
   append-only zone, so a min-ever floor would fire on every firing of a
   working guard, and a per-leg total is the only thing that can tell a clean
   corpus from a dead leg. The invariant the guard SERVES is
   `cross_tier_duplicates`; holding that at its floor is how it is judged.

**Supersession beyond `…-vN` is PROPOSED, never applied (CUR-01,
2026-08-04).** Only two tiers auto-supersede: sha256-identical duplicates
(DDP-01) and explicit `…-vN` id families (VER-01, rule 4 above). Everything
else the nightly deduces — a HOST-VERIFIED email family, or a **name family**
(`Draft`/`Final`, `Rev N`, `vF`, `versão`/`versión N`, or an unmarked
near-duplicate pair sharing one document name) — is staged as a propose-only
candidate and rides the SAME single nightly owner question as the ingestion
lane: one batch, default `reject all`, expiring unanswered after
`$BRAIN_COS_PROPOSAL_TTL_DAYS` (14 days), and a decided pair is never
re-asked. Only an owner ACCEPT reaches `core.supersede`. Every run reports
**curated coverage** — how many indexed notes carry REAL supersession
frontmatter — in `brain health-report`'s "Currency coverage" section, with
family membership counted SEPARATELY (a note sitting in an unanswered
proposal is not a covered note). The per-run engagement line is
`{"event": "version-link-run", …}` in
`<vault>/.brain/cos/host/proposals/version-links/runs.jsonl` (host-only,
gitignored, never indexed).

