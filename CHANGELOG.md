# Changelog

Versions here track `pyproject.toml`'s `[project].version`. Public releases
are tagged `v<semver>` (e.g. `v0.9.0`) per `docs/adr/0001-publish-via-clean-room-export.md`
(mechanism unchanged; naming refined by `docs/adr/0004-versioning-release.md`
Ruling 3, superseding the earlier opaque `v1, v2, ...` counter).

## [Unreleased]

## [0.19.1] — 2026-07-20
### Fixed
- **Synthesis headless run hardening** (`brain-synthesis.sh`): default
  max-turns raised 60→120 (the 08:00 run died `error_max_turns` at 61),
  allowlist widened to the read-only diagnostics the watchdog prompt
  requires, and an immediate notification ping fires on any non-zero exit
  instead of waiting for the >8-day watchdog.
- **Fold hygiene.** `hot.md` rotation (`rotate_hot_md()`): aged resolved
  blocks archive past 32 KB while open owner questions never rotate.
  Promote-scan findings are deduplicated by a content hash of the candidate
  set instead of the run date, and hot-log entries relativize
  vault-absolute paths.
- **Vendor ABI pin.** `vendor_semantic_deps.py` pins the Cowork VM
  interpreter (`VM_PYTHON=3.10`) and a wheel-tag guard refuses
  non-cp310/abi3/pure wheels (a cp311 wheel caused a 10-run
  EmbedderUnavailable outage); `brain doctor` gains a vendor-ABI row that
  names the mismatch ("vendor is cpXYZ but interpreter is 3.10") on both
  VM and host legs.
- **COS behaviour report** excludes legacy content-rejoin rows and
  verdict-unjoined rows from both sides of the consistency/contradiction
  rates.

## [0.19.0] — 2026-07-19
### Added
- **Chief-of-Staff kernel matured to v5.6.** The mail-triage/commitment
  automation gained a three-lane authority matrix with a 1–30 day anticipation
  horizon (v5.0), P0/P1/P2 priority-chip projection replacing the flat action
  mark (v4.6), an any-sender aged-read lane that archives read, no-action mail
  past a freshness threshold (v4.3/v5.1), a steady-state rot response with an
  inbox-zero closeout (v5.2), recurring-digest supersession that keeps only the
  latest chipped copy and declassifies + archives priors (v5.4), a full-inbox
  chip re-evaluation staleness sweep (v5.5), and a harness-agnostic mail leg
  whose marks/archives gate on browser capability rather than a Claude-specific
  skill (v5.6). Auto-archive ships ON as a drift monitor (owner ruling), behind
  a review gate + owner-approved three-op token set (v4.5), with
  behavioural-grading calibration from revealed preference (v4.2).
- **COS commitment spine + auto-capture** and an owner priority roster feed the
  keeper/spine ledger; overlay-owned COS drafts (`overlay/cos/drafts.md`) give
  owner rulings a home.

### Fixed
- **Security (Codex scan, 2026-07-19).** A VM ingest manifest could import
  guessed host `~/Downloads` files into the signed vault — now bound to a
  host-clock recency floor the VM cannot forge. The egress-starvation hint let a
  VM harness self-elevate `--max-tier` — the VM leg now clamps to an
  operator-set ceiling (`BRAIN_VM_MAX_EGRESS_TIER`, default Internal) and
  suppresses the hint. Vendored wheels could shadow the engine on `PYTHONPATH` —
  the engine now precedes the vendor dir and extraction refuses zip-slip and
  top-level shadowing/auto-exec members.
- **Engine.** Crash-safe index rebuild + staleness-aware re-stage sync.
- **COS reliability.** Mail-leg preflight (persistent pairing retry, fail-loud,
  evening schedule; v5.3); `owner_replied` requires a post-verdict
  `sentDateTime`; the aged-read lane screens "no action" deterministically
  before judgment; keeper name/slug matching folds accents; `kernel_version` no
  longer pins a phantom stamp; the auto-archive freeze pins the classifier, not
  the engine; NOISE/auto-archive vocabulary means "file to Archive"; an
  overlay's `overlay_type` must equal its directory; and the owner-push hook
  watches the correct vault.
- **Release hygiene.** Removed pre-publish contamination the clean-room gate
  flagged (a stray plan-closeout doc + a person name in a code comment).

## [0.18.2] — 2026-07-16
### Fixed
- **The owner's approval gate could be bypassed by the ungated capture path.**
  Field failure 2026-07-16: an MNPI ingestion candidate was signed into
  `brain/resources/` and made retrievable while its own batch sat
  `state: open` / `consumed_at: None` — the owner would have been asked to
  approve a note already authoritative in his vault, and a "reject" would have
  had nothing to reject. Root cause is a **collision of two obeyed rules**, not
  a violation: the COS skill forbids *substituting* `draft-capture` for
  `cos-propose` (Phase 1.6), but Phase 5 separately *requires* `draft-capture`
  for anything the owner must see; a finding that is also an ingestion
  candidate satisfies both and races down the ungated path, which always wins.
  A third prose rule cannot fix a collision between two obeyed ones, so
  `drain_drafts` now refuses to sign any draft whose id is still awaiting the
  owner's accept/reject, and **quarantines** it to `.brain/cos/host/gate-bypass/`
  rather than skipping it in place — a skipped draft would survive a later
  REJECT, after which the id no longer matches an undecided proposal and the
  next drain would sign exactly what the owner rejected.
- **The embedder and the ingest sweeper hid their own causes.** The VM's
  fail-closed embedder error printed a guess-list ("onnxruntime/tokenizers
  missing *or* the model is absent") for **seven consecutive runs** — every one
  silently degraded to lexical-only grounding with every dedup `inconclusive` —
  because `available()` returned a bare `False` and the raise sites discarded
  the exception; a missing package, an ABI mismatch against the vendored cp311
  wheels, and a missing model were indistinguishable, and the VM has no shell
  to probe with. `embedder_unavailable_reason()` now reports the actual import
  error per module plus the interpreter version. Likewise the sweeper's
  `unmatched` list carried bare filenames with no reason, so four runs
  escalated it as "stalled ~32h" when it was behaving correctly — measured on
  the reference deployment, 8 of 9 manifest files had never been downloaded at
  all and the 9th was a stale namesake. `unmatched_reasons` now distinguishes
  absent / stale-namesake / size-mismatch (additive; `unmatched` unchanged).

## [0.18.1] — 2026-07-16
### Fixed
- **The commitment spine ingested nothing — 0.18.0's headline feature was a
  silent no-op in the shape the shipped skill actually emits.** The COS kernel
  writes unquoted date-only frontmatter (`due: 2026-07-17`); YAML parses that
  to a `datetime.date`, so `spine.record_event`'s `json.dumps` of the event
  evidence raised `TypeError`. `consume_answers` catches spine failures by
  design ("never block acceptance"), so every accepted commitment candidate
  signed as a note normally while its spine row silently never appeared —
  leaving SP-01's only wired source ingesting nothing and the brief's
  LATE+RADAR half on pre-v4 heuristics. Fixed at the one boundary every source
  routes through (ingestion candidates, `brain cos-spine record`, future
  calendar/drafts sources): `record_event` now normalizes `date`/`datetime` to
  ISO text. Two independent test holes closed: the fixture used
  `due: ...T00:00:00Z` (re-serialized to a string by the claim path, so it
  dodged the bug), and `test_spine_keeper_commitment_gets_note_and_spine_row`
  asserted only on `accepted` + the signed note — never the spine row its own
  name promises, so a swallowed error still passed. The new date-only
  regression test fails on the pre-fix code and passes after.
- **Nothing scheduled the COS host broker fold.** The hourly task runs
  `brain maintain`, which has no `cos_broker`/spine code path, so the fold that
  claims VM proposal drops, releases holds, runs auto-capture, enqueues the
  owner batch, and renders `shared/spine-summary.md` was only ever invoked by
  hand. Field effect on the reference deployment: proposals stranded ~29h,
  spine summary never rendered. `scripts/brain-brief.sh` now runs
  `brain cos-broker` before `maintain` (so notes it signs are indexed and
  published by the same run's sync), non-fatally so a broker failure can never
  cost the vault its capture-drain floor. Static order guard:
  `tests/test_brief_script_wiring.py`.

## [0.18.0] — 2026-07-16
### Added
- **Chief-of-staff (COS) host engine — the `brain cos-*` verb family.**
  `cos-propose` (VM-side unsigned proposal drop — the only COS verb on the VM
  surface), `cos-broker` (host broker fold: drains proposals through the
  audited write path), `cos-correct` (morning corrections), `cos-evidence`,
  `cos-priority-map` (overlay-driven who/what-matters), `cos-report`
  (shadow-mode calibration report for the 10-round trust gate),
  `cos-ingest-sweep` (host sweeper for VM-manifested browser downloads), and
  `cos-hold` (hold store for held/deferred actions). All host-broker-only
  except `cos-propose`; owner risk-acceptance override for the trifecta
  preflight with a two-layer acceptance check (calendar-write PRESENCE never
  HALTs under a valid acceptance record).
- **Commitment spine (SP-01/SP-02).** New `src/brain/spine.py` event-sourced
  commitment ledger (stable identity independent of due date, deterministic
  reducer) with hybrid capture wired into `consume_answers` (keeper vs
  spine-only). `spine.radar()` renders aging/at-risk (48h) into
  `shared/spine-summary.md` on every broker fold; the COS kernel's late+radar
  phase reads it first.
- **Auto-capture for accepted ingestion patterns (ING-04).** Pattern-level
  auto-capture criteria (min-volume / zero-defect / Wilson-lower-bound,
  bundle_version-scoped) in the broker fold, routing eligible candidates into
  the hold store with a status-surfaced daily digest and one-word revert.
- **Chief-of-staff kernel skill (v1 → v4.0).** Generic, overlay-driven nightly
  COS run shipped via `package_clients` (Cowork upload kit): shadow read-tier
  (ACT/READ/NOISE × P0-P3) with a 10-round calibration gate, guarded
  auto-archive promotion (seven per-row guard conditions incl. a tested undo
  canary), REST-in-page archive execution with proven-DOM fallback,
  verified-batch mutations, ingestion proposal engine (evidence-required,
  classified, secret-scrubbed), and the v4.0 auto-capture + commitment-spine
  phases.
- **PUSH owner-interaction model.** Auto-resolve by default; genuinely
  owner-only decisions land in `inbox.jsonl` (Tier-2, capped, answered via
  `brain inbox`); weekly retro fold self-reports engine failure signatures to
  `.brain/engine-feedback/`.

### Fixed
- **CWD/vault fallback fails closed** instead of creating a phantom vault.
- **Ingestion candidates default to MNPI**, not the generic Internal capture
  default.
- **Maintenance field bugs 1-3:** future-date guard, wikilink runaway,
  move-rebase.
- COS skill frontmatter description kept ≤1024 chars (Cowork upload cap);
  host sweeper accepts kernel manifest field names; onnxruntime + closure
  staged for the Cowork VM (base image lacks it).

## [0.16.1] — 2026-07-12
### Security
- **Pre-release contamination scrub + gate repair.** The clean-room export's
  contamination scanner had been silently passing every release — blank and
  `#`-comment lines in the annotated denylist made `grep -f` emit zero output —
  so a real term reached the 0.16.0 sdist before it was caught by hand during
  the public-repo push (which was never performed). Fixes: repaired the scanner
  (strips comments/blanks first); pruned `tests/` from the sdist (the leak
  channel); excluded corpus-derived artifacts (the test suite, the eval golden
  set, the corpus-migration tools/doc, and owner-cutover design docs) from the
  public export; made the engine's ranking-zone taxonomy configurable
  (`BRAIN_ZONE_WEIGHTS`) instead of hard-coding one vault's folder names; and
  scrubbed owner-specific references from shipped code/docs. **0.16.0 was
  yanked; 0.16.1 is the clean re-release** (all 0.16.0 features included; full
  suite green).

## [0.16.0] — 2026-07-12
### Added
- **SUI-02 `brain connect` — wire an AI client in one command, not four
  hand-copied snippets.** `brain connect --client
  claude-code|claude-desktop|codex|gemini` writes that client's config
  itself (plugin marketplace + kernel skill for Claude Code, the
  `claude_desktop_config.json` MCP stanza for Desktop, a marked block in
  AGENTS.md for Codex, `.gemini/settings.json` for Gemini CLI) — always
  showing a diff and asking before touching your file, idempotent on a
  second run, and reversible with `--client <c> --remove`. Host-only.
- **SUI-01 `npx brainiac-install` — a one-command bootstrap for anyone with
  Node.js, no shell script required.** Installs the engine from PyPI
  (`uv tool` → `pipx` → `pip --user`, same fallback order as `install.sh`),
  verifies it, offers to initialize a vault, and can wire one client in the
  same command (`npx brainiac-install --vault ~/my-brain --client
  claude-code`). Zero runtime dependencies, no telemetry, `--dry-run` prints
  the exact command plan without touching anything.
- **SUI-03 `.mcpb` bundle for the Claude Desktop Chat tab.** The one surface
  that can't run a command gets a double-click install: a thin Node stdio
  shim that spawns your already-installed `brain-mcp` (never vendors or
  reimplements the engine) and exposes the same read-only verb set and
  classification egress gate as the CLI. Fails to start with an actionable
  message if the engine isn't installed yet. `brain doctor` now flags it if
  both this and the `brain connect --client claude-desktop` config-stanza
  route are registered for the same vault — pick one, never both.

### Changed
- **PYP-04 channel switchover — PyPI-first install, clone is dev-only.**
  `install.sh`/`install.ps1` default to `uv tool install` → `pipx install` →
  `pip install --user 'brainiac-cli[mcp]'` (first success wins, each
  attempt visibly reported); `--dev`/`-Dev` keeps the editable-checkout path
  for contributors/offline use. The lifecycle skills (`/brainiac-install`,
  `/brainiac-update`, `/brainiac-uninstall`) and `brain doctor`/`brain
  update` are now channel-aware (`pypi-uv | pipx | pip-user |
  editable-checkout`), each running the right install/upgrade/uninstall
  command for the detected channel. `brain doctor --check-registry` adds an
  opt-in row comparing the repo's latest release tag, the installed
  version, and the latest version actually published on PyPI. Known gap:
  `tools/workspace_registry.py` and Cowork staging (`stage_model.py`,
  `cowork_workspace_install.sh`) are not yet wheel-packaged, so
  `/brainiac-install`'s registry-write step and any Cowork workspace still
  need a read-only checkout of `~/brainiac` for those two things only.
- **PYP-03 PyPI publish runbook.** `docs/release-runbook.md` gains §7.6
  (build → TestPyPI → verify → PyPI, human-run, tokens never in-repo), §7.7
  (a prepared TestPyPI RC checklist — `_evidence/install-plan/testpypi-rc.md`),
  and §7.8 (a Windows pre-release acceptance checklist). The publish step
  must complete and be verified (`brain doctor --check-registry`) BEFORE the
  clean-room export in §8 — otherwise the exported public repo documents a
  `pip install` command that 404s.

## [0.15.0] — 2026-07-11

The G&P benchmark-series release: seven adversarial retrieval rounds
(2026-07-10/11), each fixed same-day, systemically.

### Added
- **RET-10 `brain dossier`** — the one-call retrieval sweep for
  decision-state questions: decision layer vs sources SPLIT, per-decision
  `tensions` (newer sources post-dating a recorded decision), retired
  versions pre-excluded, freshness attached; CLI (VM_ALLOWED) + MCP tool;
  RET-10b targeted BM25 decision probe so the layer survives semantic
  crowding.
- **RET-09 freshness signal** — search responses carry
  `freshness {newer_count, vault_newest, hint}`; hits carry `date` +
  `type` (the decision/source AUTHORITY signal).
- **WSP-01 workspace sweep** — `brain sweep-workspace` + nightly fold:
  settled top-level files flow from configured working/capture folders
  into `inbox/`; per-dir age overrides (`path=N`), `path=0` capture-inbox
  mode (same-day, 15-min write-settle guard), ingest-handler-aware skip.
- **Self-organization folds** — VER-01 auto version-chains (audited
  supersede over explicit `…-vN` families), PAR-01 auto-PARA filing,
  NAV-01 nightly backlinks/catalog regeneration.
- **DEC-01 decision-capture nudge** — decision language (EN+PT) in fresh
  sources queues a once-per-note hot.md candidate + action_required.
- **`brain-synthesis`** — second sanctioned scheduled task (weekly,
  registry-driven headless kb-curator session); installer registration +
  launchd template.
- **MCP `bases_query` tool** (latest_only / as_of temporal routing).

### Changed
- **Host egress default = FULL VAULT (MNPI)** (owner decision 2026-07-10);
  `--role vm` keeps the conservative Internal default; MCP ceiling default
  follows the host. `brain project` + HTML brief/digest stay conservative.
- **Temporal-intent queries** ("latest/current/as of", EN+PT) double the
  recency staleness penalty and halve its half-life.
- **`document_date` derived at ingest** from leading/embedded/trailing
  filename dates — bulk re-ingestions no longer rank as fresh.
- **Maintenance umbrella fires HOURLY** (installer templates updated,
  macOS + Windows); persistence budget formally amended to two host tasks.
- CLI `--help`/AGENTS.md/MCP descriptions carry one shared retrieval
  discipline (decision-layer-authoritative; proposals never self-promote).

### Fixed
- MCP dossier egress-report merge KeyError on conditional casing warnings.
- Decision-capture scan skips retired version-family members.
- Freshness MAX ignores non-ISO `created` values.

### Changed (distribution)
- **Marketplace/plugin rename: `profile-a-marketplace` → `brainiac`, `profile-a-kernel`/`profile-a-extras` → `brainiac-kernel`/`brainiac-extras`.**
  `brainiac-manager` is unchanged. Anyone already installed under the old
  names: `brain doctor` now flags the stale registration; recover with
  `claude plugin marketplace add Autopsias/brainiac && claude plugin install
  brainiac-manager@brainiac`, then run `/brainiac-update`, which detects the
  old names and finishes the migration (install-new-before-remove-old, with a
  recorded-state rollback on any mid-migration failure). See
  `docs/adr/0006-distribution-naming.md`.

## [0.14.1] — 2026-07-10
### Fixed
- **`brain update` re-stages the AGENTS.md contract, not just the engine.** The
  Python re-stage (`stage_engine_and_skills`) copied engine + skills + vendor +
  shim + session prompt but SKIPPED `AGENTS.md` — so an AGENTS.md change (e.g. the
  new retrieval-discipline block) never reached Cowork on update, while `brain
  doctor`'s staged-version check reads the engine stamp and reported the workspace
  "current". It now copies `AGENTS.md` into `<vault>/.brain/` on every re-stage,
  matching the installer's leg (update == install), with a byte-identity
  regression test.

## [0.14.0] — 2026-07-10
### Added
- **Retrieval discipline in AGENTS.md — vault-first, no internal web leaks.** A new
  §5 block gives every harness the standing rule the substrate lacked: exhaust
  `brain` before any web search; a starved result means *elevate the tier*, not
  give up; and **never put a Confidential-or-above topic (deal codename,
  counterparty, project name) into a web search** — the classification gate guards
  reads, but the model's own web-search tool is an ungated outbound channel and the
  query string itself is the leak. Replaces the old Obsidian "five-step retrieval
  cascade" rule for any harness reading the file.

### Fixed
- **Silent egress starvation now nudges instead of misleading (RET-08).** At the
  default `Internal` cap, an internal query surfaces almost nothing (the deal docs
  are Restricted/MNPI) — which reads to an agent as "the vault is empty" and drives
  it to web search. When the gate withholds anything, `brain search`/`grep`/
  `bases-query`/`graph-expand`/`recent` now emit an actionable hint (`egress.hint`
  in `--json`, a `-- N withheld …` line in text): re-run with `--max-tier
  Restricted` rather than treating the vault as empty. Deny-by-default is unchanged;
  the tier stays the human gate — this only signposts it.

## [0.13.0] — 2026-07-10
### Fixed
- **Recency-aware ranking — the fusion is no longer time-blind (RET-07).** The RRF
  fusion ranked purely on text similarity, so a stale version of a document outranked
  its current successor and a "latest developments" query grounded on months-old
  material (measured: on the live corpus a 4-month-old strategy doc outranked the
  current report, and the newest revision of a document fell outside the top 20). `hybrid_search`
  now applies a gentle multiplicative **staleness penalty** using the same valid-time
  date (`effective_date` → `document_date` → `created`) `bases-query` uses — bounded
  ≤1.0 so the fused score never exceeds the RRF ceiling (the same invariant the
  zone-authority prior respects), leaving exact-match relevance intact while the newer
  of two topically-similar hits wins. Tunable via `BRAIN_RECENCY_WEIGHT` (default
  `0.25`, `0` disables) and `BRAIN_RECENCY_HALFLIFE_DAYS` (default `180`); clock
  injectable via `BRAIN_NOW` for deterministic tests.

## [0.12.0] — 2026-07-09
### Added
- **Real semantic search in Cowork, offline (DV-04).** The Cowork VM leg lacked
  `tokenizers` (query tokenisation) and had no network to install it, so `brain search`
  silently ran on the non-semantic hash embedder. The installer and `brain update` now
  VENDOR `tokenizers` + `sqlite-vec` per-arch into `.brain/vendor/<arch>/` (shared
  `tools/vendor_semantic_deps.py`), and the shim + session prompt put them on `PYTHONPATH`
  — so the VM does real e5 retrieval with zero network.
- **`brain mcp-config`.** Prints the paste-ready MCP-client entry to run `brain-mcp`
  against a vault (Claude Desktop / Cowork / Claude Code) — the MCP-on-host path where the
  host runs the engine and every client calls it, no per-vault staging. `install.sh` now
  installs the `[mcp]` extra so `brain-mcp` never ships without its `mcp` dependency.

### Fixed
- **Silent hash-embedder fallback is now loud (DV-03).** `brain doctor` gains a
  live-embedder probe that FAILS (non-zero) when the runtime would degrade to the
  non-semantic HashEmbedder — the one health surface it was blind to. `role=vm` defaults
  `BRAIN_REQUIRE_REAL_EMBEDDER=1`, so the semantic path errors loudly instead of returning
  random results (lexical `grep`/`bases-query` unaffected).
- **sqlite-vec KNN on older sqlite (DV-04).** The `vec0` query now expresses k as an
  `AND k = ?` MATCH constraint, not a bound `LIMIT ?` — older sqlite (the Cowork VM's)
  doesn't push a parameterised LIMIT into vec0's planner and raised "A LIMIT or 'k = ?'
  constraint is required". Stayed hidden because vec0's native lib often isn't loaded on
  the host (brute-force fallback), so the path went unexercised.
- **`brain update` re-stages the FULL offline stack (DV-04).** The workspace re-stage
  copied only engine + skills, so an update shipped the fixed engine but left the VM
  without vendored deps / with a stale shim — semantic search silently stayed on hash. It
  now stages vendor + shim + session prompt too, from the same helper the installer uses
  (update == install).

## [0.11.0] — 2026-07-09
### Added
- **`brain restore-index` — fast index recovery from the snapshot.** Restores the live
  index from the published snapshot in seconds (vs a full re-embed `rebuild`) when the
  index is corrupt or empty — e.g. an interrupted rebuild leaves a half-written DB.
  Guards: refuses a missing/empty snapshot; refuses to clobber a live index holding MORE
  notes than the snapshot without `--force` (data-loss guard); writes a reversible
  `.pre-restore-*.bak`; verifies the note count post-restore. `--dry-run` previews.
- **Daily notes (opt-in).** With `BRAIN_DAILY_NOTE` set, `brain-nightly`'s daily branch
  creates today's `type: daily` note once per day (idempotent, seeded from the morning
  brief, Confidential floor) — parity with the Obsidian daily-note habit. Default off, so
  `maintain`'s note-count invariant is unchanged for vaults that don't want a journal.
  `tools/brain_daily.py` provides the same on demand regardless of the flag.
- **Scanned-PDF OCR in the installer.** `install.sh` installs `ocrmypdf` + `tesseract`
  (+ language packs) best-effort so image-only PDFs ingest out of the box; a missing
  toolchain never blocks install or ingestion (brain quarantines the scan as before).

### Fixed
- **Silent phantom-vault footgun.** `config.py::vault_root` now WARNS when it falls back
  to `./vault` (no `--vault`/`$BRAIN_VAULT`) and `./vault` is not yet a vault, instead of
  silently creating a phantom `./vault/.brain/` in whatever directory `brain` ran from.
  Creation flows (`brain init`, the installer's sample-vault build) are unaffected.

## [0.10.7] — 2026-07-08
### Fixed
- **Cowork skill-refresh guidance was wrong (v0.10.6 regression).** The refresh loop
  was attached to the host-only `/brainiac-update` skill, which refuses `--role vm`, so it
  was unreachable in Cowork and its `brain doctor` verify step couldn't see the Desktop
  store from the VM. Removed the broken in-skill loop; `brain doctor` / `brain update`
  remediation now points Cowork users at `/skill-creator` directly (which does run there),
  keeping the #46844 verify-after-Save-and-Replace guard. A VM-native
  `/brainiac-cowork-skills` skill that automates detect+verify is planned.

## [0.10.6] — 2026-07-08
- **Wired the existing `/skill-creator` skill into the Cowork Desktop-store
  refresh path** — `brain doctor`/`brain update` were left with only a
  generic "verify/update manually" hedge for the always-`manual-required`
  Desktop/Cowork plugin store (surface 11), even though the CLI has no way
  to fix that surface at all. Added a new "Cowork skill refresh (in-session
  only)" section to `/brainiac-update`'s SKILL.md documenting the sanctioned
  detect(`brain doctor`) -> update(`/skill-creator`, reusing its
  "Updating an existing skill" flow against the staged `.brain/skills/*.skill`
  bundles) -> verify(`brain doctor` again) loop — never a parallel installer.
  Reworded the Desktop-store remediation strings in `src/brain/doctor.py`
  (`check_desktop_plugin_store`, now version-aware via the existing
  `_compare` helper) and `src/brain/update.py` (`residual_human_steps`) to
  point at this loop. The verify step is mandatory and cites Anthropic
  #46844/#46836: Cowork's "Save and Replace" can silently no-op, so a skill
  is only ever reported updated after a second `brain doctor` read confirms
  the version actually moved — never off the user's click alone. See
  `docs/adr/0005-update-versioning-ux.md` (2026-07-08 addendum). The
  end-to-end Cowork loop (skill-creator invocation + Save-and-Replace +
  verify) is documentation/prose, not unit-testable here — only the CLI
  detection/remediation-string change is covered by
  `tests/test_doctor.py` / `tests/test_update.py`.

## [0.10.5] — 2026-07-08
- **Fix (`run_update` restaged one-build-stale `.skill` bundles — observed
  live twice, 0.10.2->0.10.3 and 0.10.3->0.10.4):** the engine venv refresh
  (`pip install -e`) does not regenerate the gitignored `dist/COMPAT` +
  `dist/cowork-skills/*.skill` artifacts — only `tools/package_clients.py`
  does. `run_update` now runs the packager (via the injectable `Runner`,
  skipped under `--dry-run`) as its own `dist_rebuild` step, right after the
  engine refresh succeeds and before `restage_workspaces` copies `dist/`
  into any cowork-vm workspace. A non-zero packager exit halts the update
  (`ok: False`) before `restage_workspaces` runs, instead of silently
  proceeding with stale bundles.

## [0.10.4] — 2026-07-08
- **Fix (`brain doctor` crashed on the Cowork VM leg with
  `ModuleNotFoundError: workspace_registry`):** `doctor` is in `VM_ALLOWED`
  but read `tools/workspace_registry.py`, a host-only companion script never
  copied into the staged zero-install engine
  (`cowork_workspace_install.sh` stages `src/brain` only). `brain doctor` is
  now role-aware: `run_doctor()` (host) guards the import so it degrades to
  a `not-detectable` row instead of raising; a new `run_doctor_vm()` covers
  the surfaces the staged VM workspace CAN see (engine version, skill-bundle
  VERSION markers, snapshot schema/generation/age, bundled model cache,
  `brain maintain` heartbeat) and lists every host-only surface as its own
  `not-detectable` row instead of a fake-green or a crash. Role detection
  falls back structurally (`doctor.looks_like_vm_stage()`) when
  `tools/workspace_registry.py` and a `pyproject.toml` SSOT are both absent,
  since the staged VM shim never sets `$BRAIN_ROLE`. See
  `docs/adr/0005-update-versioning-ux.md` (2026-07-07 addendum). Verified
  against the real staged workspace, not just fixtures.

## [0.10.3] — 2026-07-07
- **Fix (`restage_workspaces` skip gate silently dropped the user's own
  workspace — pre-existing since v0.10.0):** the cowork-vm/host re-stage
  decision skipped a registered workspace when `entry["host"] !=
  socket.gethostname()`. On macOS `socket.gethostname()` is unstable — it
  flips between the mDNS `.local` name and the DHCP-assigned name for the
  SAME machine (verified live: an entry staged as
  `oldhost.local` read back as `Mac.lan` on an unchanged
  host) — so a hostname change made `brain update` silently skip the user's
  own workspace and the v0.10.2 cowork-vm re-stage never ran. The gate now
  checks `arch` only (still protects against a genuinely different-arch
  stale entry) and self-heals a stale `host` field via
  `workspace_registry.upsert_entry` instead of hard-skipping.
- **Fix (v0.10.2's cowork-vm re-stage targeted the wrong directory):**
  `restage_workspaces` called `stage_engine_and_skills(engine_src,
  workspace_path)`, but `workspace_path` is the PARENT checkout dir (e.g.
  `.../example-vault`), whose `.brain` is the unrelated HOST stage. The
  Cowork VM reads the registry entry's `vault_path` (e.g.
  `.../example-vault/vault`) — the same dir
  `tools/cowork_workspace_install.sh` stages into as `$VAULT`. Verified
  live: the v0.10.2 fix re-staged `example-vault/.brain` (host, already
  current) and reported `version_ok: True` off that read, while the real
  Cowork VM engine at `example-vault/vault/.brain/engine` stayed on 0.10.1.
  Now stages `vault_path` — the post-stage version/no-op assertion reads
  back from the same directory it staged into, so a wrong-path stage can
  never report `version_ok: True` again.
- **Fix (`brain doctor` gave a false green on the exact surface the two
  bugs above broke):** `check_staged_workspaces`, `check_staged_skill_bundles`,
  and `check_workspace_schema` all read `workspace_path/.brain/...` for
  cowork-vm entries — the same wrong directory as the re-stage bug above,
  which is why the stale Cowork engine was never caught by `brain doctor`
  either. All three now read `vault_path/.brain/...` (falling back to
  `workspace_path` only when `vault_path` is absent), via a shared
  `_cowork_vault_dir` helper, and label the row with the path actually read.
  Follow-up: cut and publish v0.10.3 with this fix.

## [0.10.2] — 2026-07-07
- **Fix (`brain update` never re-staged the Cowork-VM engine/skills — real bug
  found on the host after publishing v0.10.1):** `restage_workspaces`'s
  `cowork-vm` leg shelled out to the whole `tools/cowork_workspace_install.sh`
  installer, but a live `brain update --json` run showed the workspace step
  reporting `ok` off the index sync alone while the workspace's
  `.brain/engine/brain/` stayed at a stale pre-0.10.0 copy with no committed
  `_version.py` stamp (`0.0.0+unknown` inside the Cowork VM — exactly the
  DV-01/ADR-0005 Ruling 1 defect) and `.brain/skills/*.skill` stayed at old
  versions. `restage_workspaces` now re-stages the engine source and refreshes
  the `.skill` bundles directly in Python (new `stage_engine_and_skills`
  helper — re-copies `src/brain` and the current `dist/cowork-skills/*.skill`
  into the workspace, the (a)+(d) legs of the install script) instead of
  shelling into the full installer, then asserts the staged engine's
  `_version.py` matches the SSOT (mirroring the script's own stamp check) and
  reports the workspace step **failed** (never a silent `ok`) on a
  missing/mismatched stamp or on zero skill bundles found to refresh. The
  index sync (`brain sync --publish`) runs unchanged after a successful
  re-stage. `host`-target workspaces are unaffected (still covered by the
  engine venv refresh). Follow-up: cut and publish v0.10.2 with this fix.

## [0.10.1] — 2026-07-07
- **Fix (plugin-reinstall upgrade path used the wrong CLI subcommand):**
  `brain update`'s plugin-reinstall step ran `claude plugin install
  <plugin>@<marketplace>` for the installed-`<`-marketplace (upgrade) case.
  `claude plugin install` on an already-installed plugin is a no-op — it
  prints `✔ Plugin "…" is already installed (scope: user)` and does NOT
  change the installed version — so `brain update` reported the step
  `ok: true` while the CLI plugin stayed on its old version. The upgrade
  path now runs `claude plugin update <plugin>@<marketplace>` (verified live:
  reports `updated from X to Y`). Added a post-action no-op check: the
  installed version is re-read after the update action and, if it did not
  move to the marketplace version, the step is now reported as **failed**
  with a manual-fallback message, instead of a false `ok: true`. The
  preflight capability probe now also requires the `update` subcommand,
  blocking with the manual fallback instead of silently no-op'ing on older
  `claude` builds. Downgrade handling (installed `>` marketplace) is
  unchanged — `uninstall` then `install`, since `update` cannot downgrade.
  Follow-up: cut and publish v0.10.1 with this fix.

## [0.10.0] — 2026-07-07
- **Feat (monotonic-version guard, ADR-0005 Ruling 5, GV-01): a release can
  never be cut backwards again.** `tools/release.py` (`bump` and `set`) and
  `tools/package_clients.py --validate-only` now refuse any target version
  that is not strictly greater than the release baseline (the highest
  semver-shaped local tag `v\d+\.\d+\.\d+`, comparing via
  `packaging.version.Version` — never string order). Legacy opaque export
  tags `v1`/`v2` were renamed to `legacy-export-v1`/`legacy-export-v2` (same
  objects, new names) so no future tooling mistakes them for semver
  releases. No override flag — a deliberate downgrade needs its own ADR.
- **Feat (hardened publish→consume pipeline, GV-02):** `tools/publish_release.py`
  is now the one command for the scriptable half of a release (package
  validate → clean-room export → contamination scan hard gate → optional
  local tag); the human publish step (runbook §8) is unchanged. On the
  consume side, fixed an ordering bug in `brain update`
  (`src/brain/update.py`): the `brain doctor` snapshot that feeds the
  per-plugin downgrade-safe reinstall decision was being captured **before**
  the marketplace refresh, so the comparison could silently run against
  stale marketplace data — the exact "I clicked update and nothing happened"
  trap this session closes. The marketplace refresh now always runs first,
  structurally. Added a clean-room export smoke test
  (`tests/test_cleanroom_export_smoke.py`) that stages a zero-install VM from
  the actual exported tree and asserts `brain --version` matches the
  exported `pyproject.toml` version — the only test that proves the version
  stamp reaches the shipped VM, not just the dev tree. Extended the
  never-touch regression to also assert `.brain/memory/`,
  `maintain-state.json`, `maintain.lock`, and the audit chain survive an
  update byte-identical.
- **Feat (Cowork delivery unification, ADR-0005 Ruling 4 addendum, cw-01/cw-02):**
  empirically confirmed the Claude Desktop / Cowork plugin store has no
  scriptable host-side CLI, config, or import (read-only investigation of
  `~/Library/Application Support/Claude/local-agent-mode-sessions/.../rpm/`
  and `cowork_plugins/`) — the staged `.brain/skills/*.skill` filesystem path
  stays the canonical, host-guaranteed Cowork skill-delivery path; the
  Desktop Plugins tab is documented as an optional in-session convenience.
  `tools/cowork_workspace_install.sh` now rebuilds the `.skill` bundles
  **unconditionally** on every re-stage (previously only when absent — a
  stale-zip trap after a version bump) and verifies the staged bundle
  version matches the staged engine version before finishing. Each `.skill`
  zip now carries a `VERSION` file (`tools/package_clients.py`). `brain
  doctor` reports a new "Staged skill bundles" row per registered Cowork
  workspace, so a stale skill set is visible as `⚠️`. One host command now
  refreshes engine + snapshot + current-version skills together.
- **Fix (version reporting, ADR-0005 Ruling 1): every surface reports its
  real version — including the zero-install Cowork VM.** `brain.__version__`
  now falls back to a **committed** `src/brain/_version.py` stamp before
  `0.0.0+unknown`; `importlib.metadata` stays primary on the pip-installed
  host. The stamp is generated by `tools/package_clients.py` (so
  `tools/release.py` rewrites it in the same act as the pyproject bump) and
  skew is a hard error in `--validate-only`, at clean-room export time
  (asserted against the exported tree), and in tests. The Cowork stager
  verifies the staged engine carries the stamp and aborts otherwise.
  `/brainiac-update` Step 2 now regenerates `dist/COMPAT` (gitignored, so
  `git pull` never refreshed it) via the packager before reading it.

## [0.9.1] — 2026-07-06
- **Fix (nightly task): per-vault launchd/Task-Scheduler label.** The nightly
  maintenance task used one shared label (`com.profile-a-brain.daily-brief`),
  so multiple registered vaults clobbered a single job — only one nightly brief
  actually ran. Labels are now per-vault (`com.brainiac.nightly.<id>`), derived
  from a single source of truth (`brain.config.nightly_label`); a one-time
  migration retires the legacy shared label on first per-vault run. Also fixed
  `/brainiac-uninstall`, which targeted the legacy plist and would have left the
  real per-vault plist behind.

## [0.9.0] — 2026-07-05

- **Version SSOT + reconciliation (ADR-0004, session s06):** `pyproject.toml
  [project].version` is the single source of truth for the engine, the three
  plugin manifests, and every distributed skill's `SKILL_VERSION` stamp. Per
  Ruling 5 (human-confirmed reconciliation), the three plugin.json versions
  (`brainiac-manager` 1.0.0, `profile-a-kernel`/`profile-a-extras` 1.1.0) are
  re-based onto this one engine line — a one-time breaking packaging change.
  `tools/release.py bump [major|minor|patch]` / `set <version>` is the one
  bump command (rolls `[Unreleased]` into a dated section, keep-a-changelog).
  `tools/package_clients.py` now propagates the SSOT version into all three
  `plugin.json` at package time and `--validate-only` hard-fails on any
  version skew (pyproject vs plugin.json vs `SKILL_VERSION` vs `dist/COMPAT`).
  `/brainiac-update` gains a forced-clean-reinstall branch for installs
  predating this reconciliation (installed plugin version > marketplace
  version reads as a downgrade a normal `/plugin` update would refuse).

- **Brainiac parity build (ADR-0003, sessions s01–s13) — eight reference-vault
  capabilities ported kernel-generic, overlay-personalized:**
  1. **Binary ingestion** — `brain ingest` (+ `ingest-transcript`) drains
     `vault/inbox/` (a visible, gitignored drop zone; unknown extensions
     quarantine to `inbox/_quarantine/`) through the audited `write_note` path;
     originals archive immutably to `vault/raw/originals/`. Extraction deps
     join the default full-capacity install. Drains on every host `brain sync`
     plus the daily maintain floor.
  2. **Bitemporal versioning + supersession** — seven optional frontmatter
     keys (`document_date`, `effective_date`, `superseded_date`,
     `is_latest_version`, `superseded_by`, `previous_version`, `replaces`) and
     `brain supersede <old-id> <new-id>` (host-only, both sides of the chain
     via one audited write). `bases-query --latest-only` / `--as-of <date>`
     add temporal filtering, VM-allowed.
  3. **Typed entities + templates** — `type:` gains `person | company |
     project | meeting | decision | concept | daily`; kernel
     `templates/<type>.md` with `<vault>/overlay/templates/<type>.md`
     override; warn-only type-specific lint.
  4. **Session memory** (MEM-01/02) — `.brain/memory/` (`handoff.md`,
     `hot.md`, `lessons.md`, `archive/`), host-only, gitignored, never
     indexed; Claude Code CLI hooks inject/rotate it.
  5. **Richer brief/digest + scheduled curation/promotion** — HTML
     brief/digest renderers (pre-gated at an `Internal` ceiling, pure-render,
     written to `.brain/brief/`); the Sunday maintain branch now also runs
     `curate` (stale-link + PageRank revisit sample) and `promote-scan`; a
     daily recommendations-aging fold ports the reference organization's
     `recommendations-open.jsonl` schema. Date-gates are now due-since-last-run
     (`.brain/maintain-state.json`), not calendar-day-only, with a
     single-runner lock and per-branch crash safety.
  6. **Graphify discovery** — `src/brain/graphify.py` builds a real, bounded
     monthly link-discovery graph (`.brain/graph/graph.json`,
     `authoritative: false`, drift-gated, reuses existing embeddings, capped
     INFERRED edges); ADR-0003 Ruling 6/(a) supersedes the earlier
     "documented only" disposition.
  7. **Voice skill** (`.claude/skills/voice/`) — DRAFT/REWRITE/CHECK against
     the owner's overlay, zero hard-coded owner content, graceful degradation
     when an overlay category is missing.
  8. **Autoresearch cascade** (`.claude/skills/autoresearch/`) — on-invoke,
     bounded one-parameter-change retrieval self-tuning loop over
     `eval/harness_direct.py` + `eval/gate.py`'s non-inferiority gate; every
     run writes an `eval/runs/autoresearch-*.json` evidence artifact.

  All new host-broker verbs (`ingest`, `ingest-transcript`, `supersede`,
  `graphify`) are refused on `role=vm`; the VM_ALLOWED surface is unchanged.
  `routines/manifest.json`'s single-OS-task lock (host 1, VM 0) is untouched —
  every new cadence folds into the existing `brain-nightly` maintain branches.
  `voice` and `autoresearch` join the three-way skill packaging
  (`tools/package_clients.py`: 10 kernel+extras Cowork zips, up from 8).
  AGENTS.md §1/§2/§5/§6/§9 fold in the new tree entries, schema keys, verbs,
  and trust-table rows (ADR-0003 Appendix A).

- **Session memory + Claude Code CLI hooks (MEM-01/MEM-02, ADR-0003 Ruling 4):**
  `<vault>/.brain/memory/` (`handoff.md`, `hot.md`, `lessons.md`, `archive/`)
  is the host-only, gitignored, never-indexed session-memory contract —
  documented in `docs/session-memory.md` and AGENTS.md §9. `.claude/hooks/`
  ships `session-start.sh` (scaffolds + rotates the handoff, injects it as
  sanitized/fenced/labelled data, surfaces a stale-nightly warning),
  `pre-compact.sh` (checkpoint marker), and `block-vault-recursive-scan.py`
  (PreToolUse guard against recursive `find`/`grep` over the vault root),
  wired via `.claude/settings.json`. Ported from the reference vault's pinned
  reference (ADR-0003 Appendix B); auto-commit deliberately not ported (the
  audit chain already owns write provenance; commits stay human-owned).
- **Repo went public (2026-07-04):** `Autopsias/brainiac` is now public;
  `/plugin marketplace add Autopsias/brainiac` (GitHub form, no clone, no
  creds) and Cowork's Plugins-tab sync of the same repo were both verified
  live — see ADR-0002 addendum. Cowork's Plugins tab is now the primary
  skill-delivery path there, with Save-skill zips as fallback.
- **Plugin-first distribution**: install/update/uninstall docs now lead with
  the `brainiac-manager` Claude Code plugin (`/plugin marketplace add
  ~/brainiac` → `/plugin install brainiac-manager@profile-a-marketplace` →
  `/brainiac-install`), backed by four lifecycle skills (`/brainiac-install`,
  `/brainiac-update`, `/brainiac-cowork-setup`, `/brainiac-uninstall`). The
  old giant paste-into-your-AI prompt is kept only as the fallback for agents
  without plugin support (e.g. Codex). Cowork's skill delivery is
  unchanged and stays Save-skill `.skill` zips as primary — see
  `docs/adr/0002-cowork-plugin-skill-delivery.md`.

## [0.3.0] — 2026-07-04 (public tag `v1`)

- **Automatic per-vault index isolation**: `config.index_dir(vault)` now maps
  each resolved vault path to its own `vaults/<name>-<hash8>/` app-data
  subdir; `BrainCore` threads the vault into the index path and default audit
  log. N vaults coexist with no env var (`$BRAIN_INDEX_DIR` still overrides
  completely). Pre-0.3.0 global audit chains stay frozen at the legacy path
  with a one-time notice; the old global index is a dead cache (rebuild).
- **`install.sh`** one-command installer (private venv, PATH symlink, first
  index build) and **`docs/install/ai-install.md`** — a paste-into-your-AI
  prompt so non-terminal users can install via Claude Code / Codex / Cowork.
- **GitHub repo renamed** to `Autopsias/brainiac` (old URLs redirect); repo
  references updated. The pip package name stays `profile-a-brain` until the
  ADR-0001 clean-room export finalises public naming (trademark /
  name-availability check pending); the CLI command remains `brain`.

- **Default install is now full-capacity**: `pip install .` ships the
  direct-ONNX e5-small semantic embedder, sqlite-vec ANN backend, signed
  audit chain, PyYAML, and the ReDoS-guard regex engine — no extras to choose
  at install time. Constrained deployments strip with `pip install --no-deps .`
  (the code still degrades gracefully). The old capability extras remain as
  no-op aliases; `[mcp]`, `[embed]`, `[dev]`, `[eval]`, `[quant-tools]` stay
  truly optional.
- **Rebrand to Brainiac** (docs-level; repo/package slug unchanged until the
  clean-room export — see ADR 0001).
- README rewritten in plain language: first-time install, second-vault setup,
  a "For technical & security teams" section, harness-wiring summary.
- Doc fix: the live index + audit chain live in the per-user app-data dir
  (`config.index_dir()`), not `vault/.brain/` — README/AGENTS.md corrected.

## [0.2.0] — unreleased (internal)

- Pre-rename hardening state: `brain` CLI (search/get/recent/draft-capture +
  host-broker write, audit chain, sync/snapshot), sample vault, conventions
  validator.
- No public export has been cut yet — see ADR 0001 for the export mechanism
  that will produce `v1` when one is ready.
