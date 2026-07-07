# ADR-0003 — Brainiac parity build: placement, schemas, and trust rulings

- **Status:** Accepted (plan `_plans/brainiac-parity-build-2026-07-04/`, session s01)
- **Date:** 2026-07-05
- **Owner decisions locked upstream:** (a) kernel-generic engine + overlay-personalized
  data; (b) the single-OS-task lock stands — every new cadence folds into
  `brain maintain` date-gates (`routines/manifest.json` `locked_counts`: host 1, VM 0);
  (c) host/VM split and deny-by-default egress are inviolable.
- **Cites:** ADR-0001 (clean-room export — anything committed to this repo is
  publish-bound), ADR-0002 (Cowork skill delivery stays zip-first; new skills join the
  three-way packaging sync), AGENTS.md §5–§6, `docs/substrate-spec.md`,
  `src/brain/egress.py` (SEC-01 chokepoint), `src/brain/cli.py` `VM_ALLOWED`.
- **Settled context (authoritative, do not resurrect older shapes):** the plugin-first
  distribution plan is COMPLETE — `docs/install/ai-install.md`, `docs/install/cowork.md`,
  `docs/install/plugin-distribution.md`, `tools/workspace_registry.py`, the
  `brainiac-manager` plugin. Per-vault nightly **registration is OWNED by
  `/brainiac-install`** (per-vault launchd labels, canonical audit service name
  `profile-a-brain-audit-key`). **This plan changes what the nightly RUNS, never how it
  is registered.** Startup sanity check for this session:
  `python3 tools/package_clients.py --validate-only` → all three client packages built +
  validated OK (2026-07-05).

## Context

Eight reference-vault capabilities are being ported: binary ingestion, bitemporal
versioning/supersession, typed entities + templates, session memory, richer
brief/digest + scheduled curation/promotion, graphify discovery, the voice skill, and
the autoresearch cascade. Each touches the substrate contract, the trust split, or the
scheduler. This ADR decides placement once so the thirteen build sessions cannot
contradict each other. The reference vault is pinned by sha256 in Appendix B —
later sessions port from that fingerprint, not from the mutable
`<REFERENCE_VAULT>` folder.

---

## Ruling 1 — Ingestion: kernel package, visible drop zone, immutable originals, full-capacity deps

**Decision.**

- **Module:** new kernel package `src/brain/ingest/` (dispatcher keyed by extension +
  a `Handler` interface + `handlers/` submodule). Host-only verb `brain ingest`.
- **Drop zone:** `<vault>/inbox/` — a visible, top-level sibling of `raw/` and
  `brain/`. Gitignored; never indexed (`scan_vault` and `tools/validate.py` skip it
  the way they skip `.brain/`). A hidden runtime dir is hostile to the actual use
  case ("drop a PDF in a folder"); the reference vault's `00 Inbox` proves the
  visible-folder shape.
- **Original archival:** the untouched binary is moved to
  `<vault>/raw/originals/<YYYY-MM-DD>-<slug>/<original-filename>` **before**
  extraction output is committed. `raw/` immutability semantics apply (write-once,
  never edited or deleted). Originals are non-`.md` files, so the frontmatter
  validator ignores them by construction; the extracted Markdown source note lands in
  `raw/` beside them with `origin:` pointing at the archived original path and
  `sha256` of both body and original.
- **Write path:** extraction output routes through the existing audited host commit
  path (`write_note`: validate → sha256 → Ed25519-sign → WAL → index). No second
  write path. Unknown extensions quarantine to `<vault>/inbox/_quarantine/` with a
  report entry — never silently dropped.
- **Cadence: drain-on-invoke + nightly floor** (amended at the s01 checkpoint —
  see Amendments). The inbox drain runs as an early step of **every host
  `brain sync`** — exactly the existing capture-inbox drain-on-invoke pattern —
  so a dropped file is ingested the next time any host session touches the
  brain, typically minutes, not a day. The **daily** branch of `brain maintain`
  remains the guaranteed floor (zero new OS entries; THE LOCK untouched).
  Standalone `brain ingest` stays on-invoke for immediate manual runs. The
  drain is idempotent and cheap when the inbox is empty (a directory listing).
- **Optional-deps shape — ruling (g):** the extraction libraries **join the default
  full-capacity install** (they move into `[project] dependencies`), matching the
  ratified pyproject philosophy ("DEFAULT INSTALL = FULL CAPACITY", owner decision
  2026-07-04). An `[ingest]` extra is kept as a **subset alias** — exactly the
  existing `vec`/`audit`/`yaml` pattern — so error-message hints stay valid.
  Constraint: pip-installable, pure-ish Python only (pypdf/pdfminer, python-docx,
  python-pptx, openpyxl, beautifulsoup4/lxml or html-to-md equivalent). System
  binaries (tesseract for local OCR) are **never** pip deps: image OCR degrades
  gracefully when the binary is absent. No cloud OCR ever.
  **`/brainiac-install` and `/brainiac-update` need zero change:** both already
  `pip install` the project at full capacity into `~/.brainiac/venv`, so a normal
  update run picks the new deps up. Constrained environments keep the documented
  `pip install --no-deps .` escape hatch.

**Rejected:** overlay-only tooling outside the kernel (leaves the kernel without a
front door and skips the signed write path); porting the reference vault's pipeline
verbatim as scripts (same signed-path problem); drop zone under `.brain/` (hidden from the human
who has to use it); archiving originals under `.brain/` (runtime is disposable —
originals are canonical evidence and belong in the immutable zone); a genuinely
optional `[ingest]` extra (contradicts the ratified default-install philosophy and
makes "I dropped a PDF and nothing happened" the first-run experience).

## Ruling 2 — Bitemporal frontmatter keys + validator semantics

**Decision.** Seven **optional** keys join the AGENTS.md §2 schema (existing notes
stay valid; `created`/`updated` unchanged):

| Key | Type | Meaning |
|---|---|---|
| `document_date` | date | when the underlying document was produced (transaction time of the source) |
| `effective_date` | date | when the content takes effect (valid time) |
| `superseded_date` | date | when this note lost its claim to currency |
| `is_latest_version` | bool | false ⇒ a successor exists |
| `superseded_by` | wikilink/id | the successor note |
| `previous_version` | wikilink/id | the predecessor note |
| `replaces` | wikilink/id | alias of `previous_version` for capture-time ergonomics |

**Validator semantics** (`tools/validate.py`, when keys are present):

- Type checks are **errors**: dates must be ISO-8601, booleans real booleans, link
  keys must resolve to an existing note id.
- Consistency **errors**: `is_latest_version: false` requires `superseded_by`;
  `superseded_date` requires `superseded_by`; a note may not supersede itself; chains
  may not cycle.
- Consistency **warnings**: `superseded_by` without the reciprocal
  `previous_version`/`replaces` on the successor; `superseded_by` present while
  `is_latest_version` is absent (should be explicit `false`).

**Ruling (b) — edited vs superseded, the precise rule.**

> An `updated:`-bump edit is the **same logical note**: same `id`, same claim,
> refined or corrected in place. **Supersession is a NEW note id replacing an old
> note's claim to currency**: the old note keeps its text forever and gains
> `superseded_by` + `superseded_date` + `is_latest_version: false`; the new note
> carries `previous_version` (or `replaces`) and is the only member of the chain
> that may be `is_latest_version: true`.

Identity test: *if the claim is the same and you are improving how it is stated,
edit. If the world changed — the old claim was true-then and is not true-now, and
that history matters — supersede.*

Examples:

- Fixing a typo, adding a citation, tightening prose in
  `arctic-embed-choice` → **edit**, bump `updated:`.
- The team switches from Arctic-embed to e5-small → **supersede**: write
  `e5-small-choice` with `replaces: "[[arctic-embed-choice]]"`; retire
  `arctic-embed-choice` with `superseded_by` + `superseded_date` +
  `is_latest_version: false`. Both remain retrievable; `--latest-only` hides the old
  one; `--as-of 2026-03-01` still returns it.
- `raw/` is **never edited**, so supersession is the *only* change mechanism in
  `raw/`: a corrected source is a new capture that `replaces` the old one.

Both sides of a supersession are written through the audited host path
(`brain supersede <old-id> <new-id>`, host-only — see Ruling 8).

**Rejected:** single-clock versioning (loses "what did we believe in March", the
whole point of the reference vault's As-Of Bases); full event-sourced history per note
(over-engineered for Markdown truth — git already keeps the byte history);
required-on-every-note keys (would invalidate the entire existing corpus).

## Ruling 3 — Typed entities: kernel enum extension, kernel templates, overlay override

**Decision.**

- **Mechanism: extend the kernel `type` enum** — not a user-defined profile. The
  accepted vocabulary becomes `note | index | moc | source-derived` (existing) plus
  `person | company | project | meeting | decision | concept | daily`. `source`
  remains the `raw/`-zone type and does **not** join the brain/ entity vocabulary.
  The core four remain the default; nothing forces typed entities on a vault.
- **Templates:** kernel `templates/<type>.md` (new top-level repo dir, shipped
  generic and placeholder-only per Ruling e). **Overlay override:**
  `<vault>/overlay/templates/<type>.md` wins when present — identity-laden defaults
  (owner sign-offs, house sections) belong to the owner, not the kernel. Resolution
  mirrors `overlay_dir` precedence.
- Type-specific lint (tmp-05: concept ⇒ counter-arguments section, decision ⇒
  source-anchored claims) keys off this closed vocabulary, **warn-only**.

**Rationale.** The validator, `bases-query --where type=…`, templates, and the type
lint all need one shared closed vocabulary; overlay-defined types would fragment
queries across vaults and make the kernel lint unable to say anything. Personalization
lives in the template *content* (overlay override), not the type *system*.

**Rejected:** tags-as-entities (loses per-type templates and lint — the exact value
the reference organization derives from types); fully user-defined types via overlay
config (kernel skills and bases-queries could no longer be written generically; a
config surface for a vocabulary that has been stable in the reference vault for
months is speculative flexibility).

## Ruling 4 — Session memory: `.brain/memory/`, gitignored, host-only

**Decision.**

- **Location:** `<vault>/.brain/memory/` — `handoff.md`, `hot.md` (judgment-call
  queue), `lessons.md`, `archive/` (rotation target). File contracts + size/staleness
  rules are mem-01's deliverable; this ADR fixes only placement and trust.
- **Gitignore posture:** already covered — `.brain/` is gitignored wholesale. Session
  memory is machine-local operational state, not knowledge. Anything durable earns
  promotion to a real `brain/` note through the normal audited path; the memory files
  are never the archive of record.
- **Not indexed, not surfaced:** `.brain/` is never scanned, so memory content can
  never leak through `search`/`get`/`recent`/`graph-expand`. No egress-gate change
  needed — the gate never sees it.
- **VM visibility:** the VirtioFS mount makes `.brain/` physically visible to the VM,
  but the **contract is host-only**: VM sessions read the published snapshot and
  nothing else (AGENTS.md §6 already states this for WAL; memory joins that list).
  Session hooks (mem-02) that inject `handoff.md` run on host harnesses only. For
  genuinely untrusted legs, physical containment remains `brain project` (which
  already omits `.brain/`). Content written into memory files obeys the same
  Internal ceiling as brief composition (Ruling c) — memory is a generated artifact,
  not a classified note.

**Rejected:** memory as committed vault notes (pollutes the knowledge substrate with
operational churn and forces classification ceremony on scratch state); a location
outside the vault (`~/.brainiac/`) — memory is per-vault by nature and must travel
with the vault; snapshot-published memory for VM continuity (a VM session has no
business resuming host work; the draft-capture channel already covers "the VM learned
something").

## Ruling 5 — Maintain branch layout under the single-task lock

**Decision.** All new cadences fold into `brain maintain` date-gates
(`src/brain/maintenance.py maintain_branches`). `locked_counts` unchanged: host 1,
VM 0. Final layout:

| Branch | When due | Runs |
|---|---|---|
| `daily` | every run | sync (drain drafts + **ingest drop-zone drain** — the drain also fires on every host `brain sync`, not only nightly; see Ruling 1) + brief (HTML, Ruling c) + **recommendations-aging scan** (new, unconditional cheap file scan — no separate branch) + snapshot publish |
| `health` | Monday | health + **framework-sync drift audit** (hyg-02 fold) |
| `integrity` | Tuesday | integrity (audit verify + near-dup scan) |
| `digest` | Sunday | digest (HTML) + **curate** (stale-link + revisit sample) + **promote-scan** (new Sunday folds, aut-02) |
| `graphify` | 1st of month | **real graphify build** (Ruling 6/a — supersedes "documented only") |

Curation and autoresearch keep their on-invoke interactive skill surfaces; the folds
are the scheduled floor, not a replacement. `routines/manifest.json` rows for
curation/promotion-scan/graphify-discovery are updated by the sessions that land each
fold (aut-02, grf-02, hyg-04); the umbrella `brain-nightly` row's `folds` list grows
accordingly.

**Ruling (d) — date-gate catch-up semantics.** Calendar-day-only gating silently
skips a branch forever on a laptop that is off every Sunday or on the 1st.
**Rule: due-since-last-run with per-branch last-run markers.**

- State: `.brain/maintain-state.json` mapping branch → `last_run` date, written by
  the host after a successful branch run (runtime state, gitignored, disposable —
  a missing file means "everything is due", which is safe because branches are
  idempotent).
- A branch is **due** when `today >= next_trigger(last_run)`, where `next_trigger`
  is the first calendar trigger date strictly after `last_run` (weekly branches: the
  next Monday/Tuesday/Sunday; monthly: the next 1st). A digest missed on Sunday
  fires on Monday's run — once, not seven times.
- `daily` is due at most once per calendar day (a second manual `maintain` the same
  day re-runs nothing destructive; sync/ingest are idempotent anyway).
- `maintain_branches` stays a pure function: it now takes `(today, last_runs)` and
  returns the due list; I/O for the state file lives in `BrainCore.maintain`.

**Rejected:** calendar-day-only (the status quo; silent permanent skips); a
launchd `StartCalendarInterval` per cadence (breaches THE LOCK); running every
missed occurrence on catch-up (a month-old laptop would replay four digests into
one run for zero value).

## Ruling 6 — Graphify build design, and Ruling (a) — superseding the ratified disposition

**Ruling (a): ADR-0003 explicitly SUPERSEDES task-disposition row 7.**
`src/brain/core.py:1037-1041` (graphify branch returns `invoked: false`) and the
`routines/manifest.json` graphify row ("documented only — BY DESIGN") implemented a
ratified decision whose two grounds were: (1) no clean fold into a retrieval verb
existed, and (2) the nightly runtime budget — an uncapped graph build inside the one
sanctioned task risks turning the 07:00 heartbeat into a multi-minute embedding job.
This ADR adjudicates both grounds as **resolved by design**, and flips the
disposition:

1. *"No clean fold"* — grf-01 builds a real `src/brain/graphify.py` with a defined
   output contract; the fold is now clean (a maintain branch invoking one module
   function, exactly like `health`/`integrity`).
2. *"Runtime budget"* — three caps make the monthly branch bounded:
   - **Drift gate:** a corpus manifest keyed by `(path, content-hash)`; if unchanged
     since the last build, the branch is a no-op in milliseconds (`skipped: unchanged`).
   - **Embedding reuse:** INFERRED edges come from the vectors **already in the
     index** — the build never re-embeds the corpus.
   - **Wall-clock budget:** the build self-reports duration; grf-01's tests assert
     the reference corpus builds inside a fixed budget (target ≤ 60 s for the
     current corpus scale; the branch logs `action_required` instead of silently
     ballooning if it ever exceeds 5 min).
   Either way the fold **breaches no `locked_counts`** — it is a branch inside the
   existing single task, monthly, with a skip-fast path.

Sessions grf-01/grf-02 therefore flip `invoked` to true and update manifest row
graphify-discovery citing this ruling. The old disposition is not erased — this ADR
is the documented reversal.

**Build design (grf-01):**

- **Module:** `src/brain/graphify.py`, host-only (reads index + vectors, writes
  runtime artifacts).
- **Manifest:** `.brain/graph/manifest.json` — per-note content hashes + build
  generation + built_at; the drift gate compares against it.
- **Output:** `.brain/graph/graph.json` — nodes = note ids (+ type,
  classification); edges = explicit wikilinks (`kind: WIKILINK`) plus
  embedding-neighbour proposals (`kind: INFERRED`, with score). Stamped
  `authoritative: false` — same doctrine as `graph-expand` (discovery-only, curated
  notes win on conflict).
- **INFERRED caps:** top-k neighbours with k ≤ 5 per node, cosine score ≥ a fixed
  threshold, and a **global cap of INFERRED ≤ 2 × explicit-edge count** — a
  discovery layer that outweighs the real graph is noise, not discovery.
- **Human loop:** proposed new links surface as `action_required` items (via curate /
  the brief), accepted by a human editing the note — graphify never writes a
  wikilink into a note.
- **Egress:** any surfacing of node/edge candidates (CLI verb, curate finding, brief
  section) routes through `egress.apply_gate` before assembly, exactly as
  `graph-expand` candidates do today — a withheld note never leaks via the graph
  surface.

**Rejected:** keeping "documented only" (the plan's entire grf category exists
because the owner wants the build real; the motivating budget objection is answered
above, on the record); a separate scheduled graph task (breaches THE LOCK); letting
graphify auto-insert links (derived data must never mutate curated notes).

## Ruling 7 — Voice + autoresearch skill contracts against the overlay

**Voice skill (hyg-01)** — kernel skill `.claude/skills/voice/`, three verbs:
**DRAFT** (compose in the owner's voice), **REWRITE** (transform given text into it),
**CHECK** (lint a draft against it, report deviations). Contract:

- **Zero hard-coded owner content** — the kernel/overlay contract. The skill reads
  `<vault>/overlay/{voice,brand,keywords,people}/` at invocation; reference-organization-
  specific rules from `_voice_profile.md`/`_writing_craft.md` are ported as **overlay
  content for the owner's vault**, never into the skill.
- **Graceful degradation:** a missing overlay category ⇒ the verb still runs in a
  neutral register and tells the user which overlay files to fill (pointer to
  `overlay/README.md`), never errors.
- **Egress:** output honours Ruling (e) — overlay content may inform interactive
  drafting and local artifacts, never committed/publish-bound files.

**Autoresearch skill (aut-04)** — kernel skill, **on-invoke only** (respects THE
LOCK; the manifest documents a quarterly poke as prose, not a schedule). Contract:
one parameter change per run; re-measure with the existing `eval/` harness; a kept
change must pass the non-inferiority gate; every run writes an evidence artifact
under `eval/runs/`; external web egress stays human-initiated (manifest
autoresearch-cascade row posture, unchanged). It reads `overlay/keywords/` to build
vault-relevant probe queries but commits no overlay content into `eval/runs/`
artifacts beyond query strings the owner initiated (evidence files are repo-committed
⇒ Ruling e applies: no people/brand content in them).

**Rejected:** voice as an overlay-shipped skill (skills are kernel-distributed three
ways via `package_clients.py`; the overlay is data, not code — shipping code in the
overlay breaks the packaging model and ADR-0002's delivery paths); autoresearch as a
maintain fold (self-tuning with an eval gate is an analyst job needing human review
of kept changes, and it is far too heavy for the nightly heartbeat).

## Ruling 8 — Trust table: every new verb/surface

| Verb / surface | Role | Egress coverage | Audit chain |
|---|---|---|---|
| `brain ingest` (+ daily fold) | **HOST-ONLY** (writes `raw/`, signs) — refused on `role=vm` before BrainCore construction | its report lists promoted note ids + classifications ⇒ joins `CONTENT_RETURNING_SUBCOMMANDS`, routed through `apply_gate` (curate/integrity precedent) | every promoted source is a signed `write_note` commit; original archival recorded in the entry |
| `brain supersede <old> <new>` | **HOST-ONLY** | status-only output ⇒ `NON_CONTENT_SUBCOMMANDS` | two signed `write_note` commits (both sides of the chain) — never a raw file edit |
| `bases-query --latest-only` / `--as-of` | existing verb, stays **VM_ALLOWED** | unchanged — same `apply_gate` after filtering; temporal filters run on already-gated rows | none (read) |
| graphify build (maintain branch / `brain graphify`) | **HOST-ONLY** | candidate/edge surfacing gated via `apply_gate` before assembly; if a CLI verb ships it joins `CONTENT_RETURNING_SUBCOMMANDS` | none (writes only derived `.brain/graph/` artifacts, `authoritative: false`) |
| brief / digest (verbs) | stay **VM_ALLOWED** (cli.py H-1 decision upheld — read-only, gated, no key) | unchanged stdout gate | none |
| brief / digest **HTML files** | written by **HOST** maintain only | **Ruling (c)** below — pre-gated data, pure renderers, Internal ceiling | none |
| session memory files | **HOST-ONLY by contract** (VM never reads/writes `.brain/memory/`) | never indexed ⇒ never reaches any egress surface | none; durable content promotes via the normal signed path |
| ingest drop zone `<vault>/inbox/` | humans + host; VM may *see* the mount but has no verb that reads it | not indexed, not retrievable | enters the chain only at host promotion |
| snapshot schema compatibility | **Ruling (f)** below | — | — |

**No VM_ALLOWED additions.** Every new verb is host-broker. The VM surface remains
exactly: `init, search, hybrid-search, grep, bases-query, graph-expand, get, read,
recent, status, draft-capture, capture, brief, digest` — read + draft, no signing key,
unchanged.

**Ruling (c) — HTML brief/digest files are a new egress surface.** The
deny-by-default gate fires at stdout; a file written to disk bypasses it. Rule:

1. **Tier ceiling at composition, default `Internal`.** The data structures fed to
   the HTML renderers are filtered through `egress.apply_gate(max_tier="Internal")`
   *before* rendering. Raising the ceiling requires the explicit human
   `--max-tier` flag on the invoking command — same human gate as stdout.
2. **Renderers are pure-render.** `brief.py` HTML functions take an
   already-gated structure and never query the index, read notes, or touch the
   overlay beyond brand assets. A renderer that can fetch is a second ungated
   egress path; a renderer that can only format is not.
3. **Output location:** `.brain/brief/` (gitignored, local, snapshot-adjacent).
   Never committed, never published into the snapshot.
4. Test obligation (aut-01/aut-03): a Restricted/MNPI note must not appear in the
   rendered HTML under the default ceiling.

**Ruling (e) — overlay-data egress tier.** Overlay content is owner identity;
treat all of it as **Internal**. Consequences:

- **May appear** in: interactive drafting output; the local brief/digest HTML
  (Internal ceiling — owner display name, brand styling, people names in
  “pending items” context are all fine in a local, gitignored artifact).
- **Must NEVER appear** in: anything **repo-committed or publish-bound** — committed
  fixtures, tests, `_evidence/` samples, `eval/runs/` artifacts, docs, `templates/`
  defaults, `dist/` packages. ADR-0001 makes the repo publish-bound via clean-room
  export, so committed = effectively Public. Committed artifacts use placeholder
  data only (the `overlay/template/` scaffold pattern).
- `people/` and `keywords/` bodies never leave the overlay into any generated FILE
  artifact except the local brief/digest above.

**Ruling (f) — snapshot compatibility (old published snapshot + new CLI schema).**
The snapshot carries a `schema_version`. Rule, in trust-table terms:

- **VM (reader): degrade, then refuse.** Minor skew (new columns absent — e.g. no
  `is_latest_version` column yet): the VM CLI treats the fields as absent, disables
  the dependent filters (`--latest-only`/`--as-of` return an explicit
  `unsupported_by_snapshot` error, not silently-wrong results), and `brain status`
  surfaces the skew. Major skew (core read contract broken): **refuse** with a
  "host must republish" message. The VM never rebuilds or republishes anything.
- **Host (writer): auto-republish.** `brain sync`/`brain maintain` detect
  snapshot-vs-CLI schema skew and republish the snapshot in the same run —
  staleness is repaired at the next heartbeat, silently correct rather than
  silently wrong.

---

## Consequences

- Sessions s02–s13 implement against these rulings; a session that needs to deviate
  amends **this ADR first** (one line in a new "Amendments" section), then codes.
- `routines/manifest.json` graphify row and `core.py`'s `invoked: false` stanza are
  updated by grf-02 **citing Ruling (a)** — the reversal is documented, not silent.
- hyg-04 folds Appendix A into AGENTS.md proper and re-runs the three-way packaging
  sync (voice + autoresearch skills join the eight existing kernel/extras skills;
  ADR-0002's zip-primary Cowork path applies to them unchanged).
- No change to install/registration surfaces: `/brainiac-install` remains the owner
  of nightly registration and audit-key provisioning; the full-capacity default
  install absorbs the ingestion deps with zero skill edits.

---

## Amendments

- **2026-07-05 (session s09, AUT-01/AUT-03):** HTML brief/digest land per
  Ruling c — `brain brief --html` / `brain digest --html` (host-only, refused
  on `role=vm` BEFORE any file write) render a self-contained, overlay-branded
  page to `.brain/brief/{brief,digest}-<date>.html` + a stable `-latest.html`
  copy; the daily maintain branch writes the brief, the Sunday `digest` branch
  writes the digest. Branding is two NEW optional `brand/*.md` frontmatter
  keys (`owner_name`, `accent_color`) resolved by `overlay.resolve_brand()` —
  additive to Ruling 3's existing `overlay_type/title/updated`, neutral
  fallback when absent (`overlay/README.md` documents the shape). One
  forward-compatible convention this session FIXES for aut-04 (session s11,
  not yet built): the quarterly-poke visibility line reads the newest
  `eval/runs/autoresearch-*.json` file's top-level `captured` ISO timestamp
  (same field name the existing `eval/runs/*.json` artifacts already use) —
  aut-04 MUST write its evidence artifacts under that filename pattern with
  that field, or the brief's staleness line never lights up. No artifact
  existing yet is treated as `never_run` (>90 days stale), not an error.

- **2026-07-05 (owner, s01 checkpoint):** Ruling 1 cadence upgraded from
  nightly-only to **drain-on-invoke + nightly floor** — the inbox drain joins
  every host `brain sync` (same pattern as the capture-inbox drain), closing
  the latency gap vs. the reference vault's session-hook pipeline. No new OS task; THE LOCK
  and `locked_counts` unchanged. Sessions ing-* implement the drain inside
  `sync`, not only inside the daily maintain branch.

- **2026-07-05 (session s08, MEM-03/AUT-02):** Ruling 5's date-gate table
  gains one more unconditional daily item — a **recommendations-aging scan**
  (MEM-03: `.brain/memory/recommendations-open.jsonl` lifecycle, ported
  the reference organization's schema/pattern only per Appendix B) — and the Sunday `digest` branch
  now ALSO runs `curate` (stale-wikilink-target detection + an age x
  centrality revisit sample) and `promote-scan`, queuing findings into
  `.brain/memory/hot.md`. This is the concrete implementation of Ruling
  (d)'s catch-up promise and closes three things Ruling 5/(d) left open:
  - **One file, two jobs.** `.brain/maintain-state.json` serves BOTH the
    per-branch catch-up `last_run` markers Ruling (d) specified AND a
    heartbeat (`last_attempt`/`status`/`consecutive_failures` per branch) —
    the same file the s07 session-start hook already reads for its
    stale-nightly line, and `brain status`'s new `maintain_heartbeat` block
    reads for the same signal on demand.
  - **Single-runner lock.** `.brain/maintain.lock` makes a concurrent
    `brain maintain` skip (never block/race) another live run; a lock older
    than a generous stale-after window is treated as an abandoned crash and
    broken automatically.
  - **Crash-safety.** Each branch runs in its own try/except; a branch's
    `last_run` marker advances to `today` ONLY on success, so a crash leaves
    it due next run without aborting branches that already succeeded. Every
    hot-queue write (recommendations, curate, promote-scan) is guarded by an
    idempotency-key marker in `hot.md` so a retry after a crash never
    duplicates a finding.
  - **G3 (whole-corpus centrality) closed, not deferred.** The revisit
    sample reuses `src/brain/graph.py`'s existing Personalized PageRank
    (seeded with every node — i.e. standard whole-corpus PageRank) rather
    than adding a new ranking module. This SUPERSEDES the curation skill's
    previously-documented "global centrality is gap G3, age-only fallback"
    framing — `.claude/skills/curation/SKILL.md` is updated accordingly and
    stays the on-invoke deeper surface (orphans/contradictions/callouts,
    still no brain equivalent). Reused, not reinvented, per the ladder: the
    module already existed from `graph-expand` (RET-03).
  No `locked_counts` change — everything above is inside the existing
  `daily`/`digest` branches of the one sanctioned `brain-nightly` task.

---

## Appendix A — AGENTS.md addendum draft (folded in by hyg-04, not before)

> ### §1 tree additions
> ```
> ├── templates/           ← kernel note templates, one per type (placeholder-only);
> │                           overlay override: <vault>/overlay/templates/
> └── vault/
>     ├── inbox/           ← ingestion DROP ZONE (gitignored, never indexed);
>     │                       unknowns quarantine to inbox/_quarantine/
>     ├── raw/originals/   ← immutable archived binaries (write-once, non-md)
>     └── .brain/memory/   ← session memory: handoff.md, hot.md, lessons.md, archive/
>                             (host-only; VM never reads it; never indexed)
> ```
>
> ### §2 frontmatter — optional bitemporal keys
> `document_date`, `effective_date`, `superseded_date`, `is_latest_version`,
> `superseded_by`, `previous_version`, `replaces` — all OPTIONAL; validated for
> type + chain consistency when present. **Edit vs supersede:** an `updated:` bump
> is the same logical note; supersession is a NEW id replacing an old id's claim to
> currency (old note gains `superseded_by`/`superseded_date`/`is_latest_version:
> false`; only chain heads are latest). `raw/` is never edited — supersession is the
> only change mechanism there.
>
> ### §2 type vocabulary
> `type:` accepts `note | index | moc | source-derived | person | company | project
> | meeting | decision | concept | daily` in `brain/`; `source` remains the
> `raw/`-only type. Templates: `templates/<type>.md`, overridden by
> `<vault>/overlay/templates/<type>.md`.
>
> ### §5 — temporal-intent routing (agentic tool surface)
> When a query is about time ("latest", "as of June", "previous version"), probe the
> temporal surface first: `brain bases-query --latest-only` / `--as-of <date>` /
> version-chain fields on `get`, before semantic search.
>
> ### §5/§6 — new host-broker verbs
> `ingest` (drop-zone → extracted, signed `raw/` sources; originals archived
> immutably) and `supersede <old-id> <new-id>` (writes both sides of a version chain
> via the audited path) are **host-broker only**, refused on `role=vm`. The
> agent-facing VM surface is UNCHANGED (still read + draft: the four verbs plus the
> gated read tools). Brief/digest HTML artifacts are composed from pre-gated data at
> a default `Internal` ceiling and live in `.brain/brief/` — renderers are
> pure-render. Session memory (`.brain/memory/`) is host-session state: never
> indexed, never surfaced, never read by a VM leg.

## Appendix B — reference vault fingerprint (pinned 2026-07-05)

Port from these exact contents; if `<REFERENCE_VAULT>` drifts, these hashes are
the reference of record. Base: `<REFERENCE_VAULT>/` (set via `$BRAIN_REFERENCE_VAULT`).

| File (relative) | sha256 |
|---|---|
| `90 System/_ingestion_pipeline/ingest.py` | `4d0cee86a4f95d1a2145f4a1090c74f7eb5d043af8e35133503a622212d01ea7` |
| `90 System/_ingestion_pipeline/config.yaml` | `373312fb8af9e01b2a9a6976519535bf6511e74ccac1e944111c5f028ef2e45f` |
| `90 System/_ingestion_pipeline/handlers/pdf.py` | `182f9435ca360ca4645ef71838089ff39f0fdcb6558038ad6cd8a47516460b7e` |
| `90 System/_ingestion_pipeline/handlers/docx.py` | `344fe0e30fc752eb6d08bbfd129ad358e4a62076c775217b74330424e68d0d61` |
| `90 System/_ingestion_pipeline/handlers/pptx.py` | `f5ba02e6e59a1dc1df63320ca0c55de5a280bb0910f1bdce71f2515951ffb0ad` |
| `90 System/_ingestion_pipeline/handlers/xlsx.py` | `7d0c08b9776a0a9d2668c3cfb31dac2354f097747f262231d99904af239f935c` |
| `90 System/_ingestion_pipeline/handlers/image.py` | `1393c855bef631b149d193c3e109484d0cfb3955492b5c648beacef3e4911d8e` |
| `90 System/_ingestion_pipeline/handlers/email.py` | `c61140ba1860dbc6e5420c70030da9ff88f6be7c3b267b45838336edec9eac2b` |
| `90 System/_ingestion_pipeline/handlers/html.py` | `d33e4d58eee53fd0e7a4a1cd5c020281f7dbba4f392848d9e1f29609a650d3b2` |
| `90 System/_ingestion_pipeline/handlers/links.py` | `307891983b8b1d3e6eb7636eac628ecd77cce33ea3e746fa3d928b95c83c0265` |
| `90 System/_ingestion_pipeline/handlers/text.py` | `481ef9f338fa4cc357b9845322e5eb40139f15d41ddccaea95796d339f4d5626` |
| `.claude/hooks/session-start.sh` | `0690a40ac36b2229fa2b6c2dbafea7def04ee9da45c8ab7f6cea69cb241bd7e2` |
| `.claude/hooks/pre-compact.sh` | `8c6e59990127b29f2eb12b13e30b1792ed195c6d0f6ab808898894fcedf27026` |
| `.claude/hooks/block-vault-recursive-scan.py` | `69c34dc0e5a47cfa5b72238fabc173690832a748c2a047c96c25d27128778ce6` |
| `90 System/_memory_landscape.md` | `c6ca610dd13e9bcb16eb5228abeb85fdfd7b83bb7000323a327761e04882cc5e` |
| `90 System/_voice_profile.md` | `fd542244f5c67fc5855fea0edaa7e55c9b4dc76dd72953d3e4b0a0e886def66e` |
| `90 System/_writing_craft.md` | `a183514e430bb10ba36dda931cb3237eb303313f9aaf110dd621e65688442615` |
| `90 System/_operating_guide.md` | `9c2da38089fea10a359ad7ae40e438b59c7f754b3bb54861e2ef6eaa3dace9ff` |
| `90 System/_promotion_quality_guide.md` | `ee46c21439369c54670c20ae52e797451c67514fc8860eca820cc8b6b920f24a` |
| `90 System/_scheduled_task_outcomes_contract.md` | `8f0d88ba1925cd6c32cf1746228be34067b24d23475108c83855ba3b0dda53e3` |
| `90 System/Templates/person.md` | `b37f320e8d7fdc25dfdc09b560db09bc54a41c153d9cc3ba5560eace92b46f3d` |
| `90 System/Templates/company.md` | `185a1f6064652879199c51cc7d4a48743ada3d33c6fdb30ff3ae663b86d89cf2` |
| `90 System/Templates/project.md` | `362d1879284d08506de843133df0e205b643febe73cf6a18e84a6c474cc91ad1` |
| `90 System/Templates/meeting.md` | `0fa19fa597183d51b2e5e2c586010acf2baef880101cecc8e3fd9290c6a4b13f` |
| `90 System/Templates/decision.md` | `f767670a462f50c04f12aee4f1756f5c3b1c66c9cc3efeeb7363f5c147b4b59d` |
| `90 System/Templates/concept.md` | `d48b4ac8abdf89b6a0735183ae658791c21905486a4abf95e5c31e35eae203b9` |
| `90 System/Templates/daily.md` | `2e357cf8ae57b641ea07f6639d1e788d310483a7acb48b758dd74bf8b7d2eab6` |
| `90 System/Templates/source.md` | `cd9cace7b7827bcbad09a36e41ac62d3b1ace0a19e6ad276c1a076ed8a73292f` |
| `90 System/_ppr/ppr.py` | `c79b0c2bb3693d5e318ce3a826149996b46909e9a48d3b92783aee3ce00c1dae` |
| `99 Workspace/_recommendations_log.md` | `d4a3b3abef3af43017e4f49fb7fca08ed91b7c221746954594ecb43829135326` |
| `90 System/Bases/Latest Only.base` | `6ed779f908a7609fcb0a50efa84f17682c857bacd02c51a771f9b541b5f7aabd` |
| `90 System/Bases/As Of.base` | `abe001ac5adaeb8683ee29da3ffc771dae8ce5fbb2c45f4e5f45c831423d785c` |
| `90 System/Bases/Version Chain.base` | `3f3688e2f53d04170009efc24be7b2cff4367f8934969b87a728f2ddee904f95` |
| `99 Workspace/_pilot_procedural_memory.md` | `2c738ae9dec6ea33aacbfbb97e3ae291af1015796674734872673600bd77b53f` |

`99 Workspace/_recommendations_open.jsonl` is live state (28,794 bytes at pin time)
— port its **schema/pattern**, never its content (Ruling e: reference-organization
content is owner-Internal and never enters this repo).

Sessions porting a file MUST re-verify its hash against this table first; a mismatch
means the reference moved — stop and re-pin via a new ADR amendment rather than
porting silently drifted code.
