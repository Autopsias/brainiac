# Changelog

Versions here track `pyproject.toml`'s `[project].version`. Public releases
are tagged `v<semver>` (e.g. `v0.9.0`) per `docs/adr/0001-publish-via-clean-room-export.md`
(mechanism unchanged; naming refined by `docs/adr/0004-versioning-release.md`
Ruling 3, superseding the earlier opaque `v1, v2, ...` counter).

## [Unreleased]

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
