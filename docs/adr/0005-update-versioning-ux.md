# ADR-0005 — Update + versioning UX architecture

- **Status:** Proposed — **Rulings 1 and 5 require explicit human
  confirmation** (linchpin rulings; the s01 checkpoint gates both this prose
  and the exported-tree evidence at `_evidence/update/version-from-source.txt`).
- **Date:** 2026-07-06
- **Cites:** ADR-0001 (clean-room export — `tools/export_cleanroom.py` ships
  **only git-tracked files** via `git ls-files`), ADR-0002 (zip-first Cowork
  delivery), ADR-0003 (host/VM trust split, snapshot skew Ruling f), ADR-0004
  (version SSOT = `pyproject.toml [project].version`; Ruling 4 update ordering
  + never-touch contract; Ruling 5 single version line), `src/brain/__init__.py`,
  `tools/release.py`, `tools/package_clients.py`,
  `tools/cowork_workspace_install.sh`, `tools/workspace_registry.py`,
  `_plans/brainiac-v09-release-2026-07-05/`.

## Context — the defect this ADR exists to kill

`brain.__version__` is read via `importlib.metadata.version("profile-a-brain")`,
which requires a pip-installed package. The zero-install Cowork VM runs the
engine **straight from a staged source copy** (`PYTHONPATH` only — see
`tools/cowork_workspace_install.sh` step (a)), so every VM surface reports the
`0.0.0+unknown` fallback. Separately, `dist/COMPAT` is gitignored, so
`git pull` on the canonical checkout never refreshes it — it sat at 0.3.0
while the code was 0.9.1, poisoning the update skill's Step-2 gate.

Invariants held throughout: the single-OS-task lock (host 1 / VM 0) and the
host/VM trust split (no `VM_ALLOWED` widening, no VM keys) are untouched by
everything below.

---

## Ruling 1 — Version stamping: a COMMITTED `src/brain/_version.py`, written by the release pipeline, read as the fallback before `0.0.0+unknown`  *(HUMAN CONFIRMATION REQUIRED)*

**Decision.** Every surface reports its real version through a two-tier read
in `brain/__init__.py`:

1. **Primary (pip-installed host):** `importlib.metadata.version("profile-a-brain")`
   — unchanged. It reports what is *actually installed*, which is the signal
   the host skew checks depend on.
2. **Fallback (zero-install VM / raw checkout / clean-room export):** a
   **git-committed** `src/brain/_version.py` carrying `__version__ = "X.Y.Z"`.
3. **Last resort:** `0.0.0+unknown` (now reachable only on a tree with the
   stamp deleted — i.e. never in any shipped artifact).

**Who writes the stamp.** `tools/package_clients.py` generates
`src/brain/_version.py` from the pyproject SSOT in its version-marker step —
the same step that writes `dist/COMPAT` and the plugin.json versions.
`tools/release.py` already invokes the packager as part of every bump, so the
stamp is rewritten **in the same act as the `pyproject.toml` version bump**,
inside the release commit that gets tagged. The exported tree therefore always
carries the exact released version; staleness is structurally impossible.

**Skew is a hard error at three gates:**

- `package_clients.py --validate-only` fails if the stamp ≠ pyproject
  (same contract as the plugin.json lockstep, ADR-0004 Ruling 5);
- `tools/export_cleanroom.py` asserts, **against the exported tree**, that
  the exported stamp == the exported pyproject version — a stale committed
  stamp can never ship;
- `tests/test_version_stamp.py` proves a from-source import (metadata denied,
  git-ls-files copy of `src/brain`) reports the pyproject version.

The Cowork stager (`cowork_workspace_install.sh`) needs no new write step —
`cp -R src/brain` already carries the committed stamp into
`.brain/engine/brain/` — but it now **verifies** the staged stamp exists and
aborts if it is missing, so a broken staging can never silently ship an
unversioned engine.

**Rationale (why committed, not generated-at-stage-time — HARDEN:consensus-CRITICAL).**
`export_cleanroom.py` ships **only git-tracked files** (`git ls-files`). A
stamp generated at stage/package time and left uncommitted is silently
**dropped from the export**, so the shipped VM would keep reporting
`0.0.0+unknown` — the headline defect unfixed while the dev tree's tests pass
(false assurance). The committed stamp is the only horn that survives the
clean-room export; binding its write to the release commit is what removes
the staleness risk that makes hand-committed literals worse than the fallback.

**Rejected:**

- **Stage-time-only generated stamp** (a `_version.py` or `VERSION` file
  written by `cowork_workspace_install.sh` / `package_clients.py` but never
  committed) — dropped by the git-ls-files export; forbidden for the VM path.
- **Hand-maintained committed literal** ("bump it when you remember") — goes
  stale and then reports a confidently *wrong* version, which is worse than
  an honest `0.0.0+unknown`. The packager-writes/release-commits binding is
  the load-bearing difference.
- **Parsing `pyproject.toml` at runtime** — the staged VM copy is
  `src/brain/` alone; there is no pyproject beside it, and adding one widens
  what ships into the workspace for no gain.
- **Making the stamp primary everywhere** — on the host,
  `importlib.metadata` reports the *installed* version; replacing it with the
  checkout's stamp would mask exactly the venv-vs-checkout skew that
  `brain status` / `/brainiac-update` Step 2 exist to detect.
- **A `VERSION` text file beside the staged source** — same commit-vs-stage
  analysis applies, plus it adds a second, non-Python read path and a file
  ADR-0004 Ruling 1 already rejected at the SSOT level.

## Ruling 2 — `brain doctor` surface list and status classes

**Decision.** `brain doctor` (implemented in a later session of this plan)
inspects the version surfaces below and classifies **every** surface with
exactly one status from: **`current | stale | unmanaged | manual-required |
not-detectable | unknown`**. Only **scriptable REQUIRED** surfaces may
hard-fail (non-zero exit); `manual-required` and `not-detectable` surfaces
are reported, never fatal — otherwise `brain update` could never pass while
an unscriptable surface stays stale (HARDEN:codex-CRITICAL).

| # | Surface | How read | Required? | `stale` means |
|---|---|---|---|---|
| 1 | Version SSOT | `pyproject.toml [project].version` in the canonical checkout | yes (scriptable) | n/a — this is the reference value |
| 2 | Committed stamp | `src/brain/_version.py` | yes (scriptable) | stamp ≠ SSOT |
| 3 | Host engine venv | `~/.brainiac/venv/bin/brain --version` | yes (scriptable) | installed ≠ SSOT |
| 4 | `dist/COMPAT` | checkout `dist/COMPAT` | yes (scriptable) | marker ≠ SSOT (gitignored — regenerate, don't just report; see Context) |
| 5 | Plugin manifests | 3× `plugins/*/.claude-plugin/plugin.json` | yes (scriptable) | any ≠ SSOT (ADR-0004 Ruling 5) |
| 6 | Distributed `SKILL_VERSION` stamps | `.agents/skills/` + `plugins/*/skills/` copies | yes (scriptable) | any stamp ≠ SSOT |
| 7 | Installed Claude Code plugins | installed-plugin metadata, when locatable | best-effort | installed < marketplace (`stale`) or installed > marketplace (**downgrade condition → Ruling 3**); `not-detectable` when the install dir can't be found |
| 8 | Cowork workspaces (per `workspace_registry.py` entry) | staged `.brain/engine/brain/_version.py` + snapshot `schema_version` | yes per registered entry (scriptable) | staged stamp ≠ SSOT, or snapshot schema skew (ADR-0003 Ruling f) |
| 9 | Nightly task | per-vault launchd/Task-Scheduler payload bytes | yes (scriptable) | payload bytes differ from the checkout's (refresh-in-place only, ADR-0004 Ruling 4) |
| 10 | Index / snapshot schema | `brain status` `schema_version` vs binary `SCHEMA_VERSION` | yes (scriptable) | directional skew per ADR-0004 Ruling 2 |
| 11 | Desktop / Cowork plugin-skill store | not scriptable (pending s05 empirical check) | **no — never hard-fail** | always `manual-required` (with the exact manual step printed); upgraded to scriptable only if s05 proves otherwise |

`unmanaged` = a surface present on disk but not created by this tooling (e.g.
a hand-copied engine dir not in the registry): reported, never mutated.
`unknown` = the probe itself errored (permission, parse failure): reported
with the error, counts as a warning, not a version verdict.

**Rejected:** a boolean healthy/unhealthy doctor (loses the
manual-vs-scriptable distinction that makes the exit code meaningful);
hard-failing on surface 11 (deadlocks `brain update` behind a surface no
script can fix); probing network/marketplace state (deny-by-default egress —
ADR-0004 Ruling 8 already rejected network version checks).

## Ruling 3 — Downgrade auto-handling: marketplace-refresh-first, then automatic clean reinstall

**Decision.** `/brainiac-update` handles the ADR-0004 Ruling 5 reconciliation
downgrade (installed plugin version > marketplace version, e.g. 1.1.0 > 0.9.x)
**automatically**, in this order:

1. **Marketplace refresh first:** `/plugin marketplace update` — the
   comparison is only meaningful against a fresh marketplace state.
2. **Detect:** for each of the three kernel plugins, compare installed
   version vs marketplace version (semver compare, not string compare).
3. **Clean reinstall automatically** when installed > marketplace:
   `/plugin uninstall` → `/plugin install` for each affected plugin, then
   re-verify, then continue the normal update flow. No user hand-steps.

This **amends ADR-0004 Ruling 5's migration paragraph**, which had the update
skill *print the instruction* and stop; the empirical risk assessment is that
plugin state is only the plugin files themselves (ADR-0004 Ruling 4's
never-touch list — all vault/engine state lives outside plugin dirs), so the
uninstall/reinstall cycle is losslessly safe to run unattended, and a printed
instruction is where users fall off. The one-time-cost property is unchanged:
after the lines merge at 0.9.x this branch can never fire again under
Ruling 5's monotonic guard.

**Rejected:** print-instructions-and-stop (the previous ruling — safe but
measurably worse UX for a mechanically safe operation); a version-epoch jump
(re-base plugins to 2.x so the marketplace sees an "upgrade" — poisons the
single version line forever to dodge a one-time event); leaving the in-place
update to fail silently (the exact trap the reconciliation analysis found).

## Ruling 4 — Cowork delivery: staged-zip filesystem path is canonical; the Desktop store is best-effort, `manual-required`

**Decision (intended ruling; empirical check deferred to s05).** The
canonical delivery path for Cowork skills is the **filesystem staging path**:
`package_clients.py` builds `dist/cowork-skills/*.skill`,
`cowork_workspace_install.sh` lands them in the workspace `.brain/skills/`,
and the analyst uploads them via Cowork's Save-skill flow (ADR-0002). The
Claude Desktop / Cowork plugin store is treated as **unscriptable until
proven otherwise**: it is surface 11 of Ruling 2, always `manual-required`,
never a hard-fail, and the update report prints the exact upload steps with
the staged zip paths.

s05 performs the empirical scriptability check. Outcomes: (a) store is
scriptable → surface 11 upgrades to scriptable best-effort (still not
required — the staged path remains canonical because it is the only path the
tooling can *guarantee*); (b) store is not scriptable → this ruling stands
as written, no amendment needed. Only outcome (a) touches this ADR.

**Rejected:** making the Desktop store canonical (an update path can't
guarantee freshness through a surface it can't drive — the headline "trust
what you see" goal dies there); skipping VM skill delivery ("just use host
skills") — the VM session is the product surface ADR-0002/0003 exist for;
blocking this ADR on the empirical check (the staged path is canonical under
*both* outcomes, so the ruling is decision-stable and s05 only refines
surface 11's class).

## Ruling 5 — Monotonic versioning governance: versions only ever increase; the release tool refuses a non-greater version  *(HUMAN CONFIRMATION REQUIRED)*

**Decision.** `tools/release.py` (both `bump` and `set`) gains a guard that
**refuses any target version not strictly greater than the release baseline**:

- **Baseline** = the highest **semver-shaped local tag** matching
  `^v\d+\.\d+\.\d+$` — legacy opaque tags (`v1`, `v2`, which point at
  0.3.0-era exports) do **not** match and are structurally ignored
  (HARDEN:consensus). If no such tag exists, baseline falls back to the
  current pyproject version.
- **Comparison** via `packaging.version.Version` when available, else an
  integer-tuple compare of the regex groups (identical semantics on the
  constrained `X.Y.Z` shape) — **never string order**: `0.10.0 > 0.9.1`.
- **Scope:** this guards **local re-tagging-downward** — the actual
  mechanism by which the v0.9 reconciliation could have been replayed
  backwards. Publication is a human act with no local marker (ADR-0004
  Ruling 7 step 7) and is explicitly **out of scope** for this guard; the
  human publisher remains the last gate on what lands publicly.
- **Legacy tag reconciliation:** the guard session renames `v1`/`v2` to
  `legacy-export-v1`/`legacy-export-v2` (create new name on the same object,
  delete old) so no future tooling that lists `v*` tags can mistake them for
  semver releases. Their objects and provenance are preserved.
- There is no override flag. A deliberate downgrade is by definition a
  history-rewriting event that needs its own ADR, not a `--force`.

**Rationale.** The whole update UX (Rulings 2–3, ADR-0004 Ruling 4 skew
tables) assumes "higher version = newer code". One non-monotonic release
breaks every comparison in the system at once — skill gates, plugin
downgrade detection, snapshot skew. The guard makes the assumption a
mechanical invariant instead of a convention.

**Rejected:** comparing against the remote/marketplace (deny-by-default
egress; and the remote is deliberately push-disabled per ADR-0001); using
CHANGELOG headings as the baseline (prose, editable, already proven
driftable); guarding publication instead of tagging (publication has no
scriptable local surface to hook — see scope note above); string comparison
(fails at exactly the next release, 0.9.x → 0.10.0).

---

## Consequences

- **Implemented with this ADR (dv-01):** `src/brain/_version.py` (committed
  stamp), the `__init__.py` two-tier read, `package_clients.py` stamp
  write + lockstep validation, the `export_cleanroom.py` exported-tree
  assertion, the stager's staged-stamp verification, `dist/COMPAT`
  regeneration in `/brainiac-update` Step 2 (run the packager after the
  pull, before reading COMPAT), and `tests/test_version_stamp.py`.
  Evidence from a git-ls-files **exported** tree (not the dev checkout):
  `_evidence/update/version-from-source.txt`.
- **Later sessions of this plan:** `brain doctor` per Ruling 2's table;
  Ruling 3's automatic clean-reinstall in `/brainiac-update` (amending the
  ADR-0004 Ruling 5 migration prose); s05's empirical Desktop-store
  check per Ruling 4.
- **Implemented by s05 (GV-01):** the Ruling 5 guard —
  `tools/release.py`'s `monotonic_baseline()` / `assert_monotonic()`
  (both `bump` and `set` route through `apply_release()`), and
  `tools/package_clients.py --validate-only`'s `validate_monotonic_version()`
  (imports and reuses `release.py`'s guard rather than duplicating the rule).
  The legacy `v1`/`v2` tags were renamed to `legacy-export-v1`/
  `legacy-export-v2` (same objects, new names) so no future `v*` tag listing
  can mistake them for semver releases. Regression tests:
  `tests/test_release.py` (mixed-scheme tag sets, string-vs-numeric compare,
  both CLI entry points). Evidence:
  `_evidence/update/monotonic-guard.txt`.
- **Implemented by s05 (GV-02):** `tools/publish_release.py` — the
  one-command scriptable publish-prep flow (package validate → clean-room
  export → contamination scan hard gate → optional local tag), never
  touching the disabled remote. Fixed an ordering bug in
  `src/brain/update.py` `run_update()`: the `brain doctor` snapshot feeding
  the per-plugin reinstall decision was captured before the marketplace
  refresh, making the comparison silently stale — now the refresh always
  runs first, structurally (regression: `tests/test_update.py`'s ordering +
  static source-order tests). Added `tests/test_cleanroom_export_smoke.py`
  (stages a zero-install VM from the actual exported tree, asserts
  `brain --version` == the exported pyproject version — this becomes a
  release gate in s06). Extended the never-touch regression
  (`tests/test_migration_verification.py`) to assert `.brain/memory/`,
  `maintain-state.json`, `maintain.lock`, and the audit chain survive an
  update byte-identical. Full loop documented in
  `docs/release-runbook.md` §7.5 and cross-checked end-to-end via
  `tools/publish_release.py --check` / `--denylist` dry-runs against the
  real repo.
- **ADR-0004 amendments carried by this ADR:** Ruling 3 here supersedes the
  "print exactly that instruction" sentence of ADR-0004 Ruling 5's migration
  paragraph; Ruling 1 here extends ADR-0004 Ruling 1's surface table with
  the committed-stamp row (derived at release, validated lockstep at
  package + export + test time).
- **No trust-surface change:** `locked_counts` (host 1 / VM 0) and
  `VM_ALLOWED` untouched; nothing here widens VM capability — the VM gains
  only the ability to *read its own version from a file it already ships*.

## Addendum (s04, 2026-07-06) — Ruling 4 empirical check: outcome (b), staged path stays canonical; cw-02 unifies engine + skills into one command

**Empirical investigation (read-only).** Inspected the live Claude Desktop
app-support tree on this host:
`~/Library/Application Support/Claude/local-agent-mode-sessions/<session-uuid>/<sub-uuid>/`.
Two plugin-store surfaces exist there:

- **`rpm/plugin_<opaque-id>/`** — a per-session materialized-plugin cache
  (this is surface 11 in Ruling 2's table, read by
  `check_desktop_plugin_store`). Directory names are server-assigned opaque
  IDs with no stable pointer from outside the session; contents are files
  copied in by the app itself.
- **`cowork_plugins/`** — the marketplace/install registry
  (`known_marketplaces.json`, `installed_plugins.json`,
  `.install-manifests/*.json`, `marketplaces/<name>/`, `cache/<name>/`).
  On this host it holds exactly two marketplaces: `local-desktop-app-uploads`
  (a `"source": "directory"` marketplace — the Save-skill upload target,
  `installLocation` pointed at a path *inside* this same session tree) and
  `knowledge-work-plugins` (a `"source": "github"` marketplace,
  `anthropics/knowledge-work-plugins`). **`Autopsias/brainiac` is not present
  in either marketplace list on this host** — it has never been added here,
  which is itself informative: nothing outside the app wrote it in.

**No scriptable write path found.** Checked, all negative:

- No `claude` CLI binary ships inside `/Applications/Claude.app` (only the
  app binary itself, `disclaimer`, and `chrome-native-host` helpers) — there
  is no bundled command-line tool that manages plugins/marketplaces.
  Cross-check: `docs/adr/0002-cowork-plugin-skill-delivery.md`'s own
  investigation independently confirms Cowork's marketplace-add is a
  **server-side, unauthenticated GitHub fetch** with no local git-credential
  path — i.e. even the in-app "Add marketplace" flow doesn't run through
  anything on this machine that a host script could drive.
- The `claude://` URL scheme (`CFBundleURLSchemes` in `Info.plist`) exists
  but is undocumented for plugin/marketplace verbs — no public API surface.
- The `rpm/plugin_*` cache and `cowork_plugins/.install-manifests/*.json`
  manifests are per-file-SHA256 integrity records the app writes for itself;
  nothing documents (or was found to support) writing a new entry from
  outside and having Cowork pick it up. The directory-source
  `local-desktop-app-uploads` marketplace *looks* filesystem-driven, but its
  `installLocation` sits inside the session-scoped app-support tree
  (regenerated per session), not a stable path a host script could target
  before the relevant Cowork session even exists.

**Conclusion: outcome (b).** The Desktop/Cowork plugin store cannot be
refreshed or installed into from outside the app by any supported CLI,
config file, or import mechanism. Ruling 4's staged-zip path stands as
written, unamended — this confirms rather than revises it. Surface 11 in
Ruling 2's table stays `manual-required` (never upgraded to "scriptable
best-effort").

**Reconciling with the ADR-0002 addendum (2026-07-04) — no contradiction.**
That addendum records that once `Autopsias/brainiac` went **public**, the
Cowork **Plugins tab's own in-app sync** (Customize → Plugins → add
`Autopsias/brainiac` → install) started working and became primary *for
that specific marketplace, driven from inside the Cowork session by the
user*. This addendum's finding is narrower and orthogonal: there is still no
way to drive that same sync **from a host script** — a human still clicks
through the Plugins tab inside a live Cowork session; nothing here
contradicts ADR-0002. The ADR-0002 addendum's "Plugins tab primary" framing
governs *which in-app action* a Cowork user should take; this ADR's Ruling 4
governs *what a host command can guarantee* — and a host command can only
guarantee the staged-zip path, because that's the only leg it can drive
without a person in the Cowork session. **Ruling: the staged
`.brain/skills/*.skill` path (uploaded via Cowork's Save-skill flow, or
synced automatically if the workspace already has the Plugins-tab
marketplace connected) remains the canonical, host-guaranteed Cowork
skill-delivery path.** The Desktop Plugins tab (either the public-marketplace
sync or a manual Save-skill upload) is documented as the in-Cowork-session
completion step, not something the host can drive end-to-end alone.

**Consequence — cw-02.** Because the staged-zip path is the one lever the
host fully controls, `tools/cowork_workspace_install.sh` now rebuilds
`dist/cowork-skills/*.skill` **unconditionally** on every run (previously
only when the directory was empty — a stale-zip trap after a version bump)
and verifies the staged skill bundle's version marker matches the staged
engine's `_version.py` stamp before finishing, aborting on any mismatch. Each
`.skill` zip now carries a `VERSION` file written by
`tools/package_clients.py` at build time (same pyproject SSOT as the plugin
manifests and the committed stamp). `brain doctor` reports a new
`Staged skill bundles (<workspace>)` row per registered Cowork workspace —
`current` when the bundle version matches SSOT, `stale` when it doesn't
(remediation: re-run the install script), `not-detectable` when no
`.brain/skills/` dir exists yet. Like surface 8 (the engine stamp row), this
new row is a **scriptable, gating** surface — the host wrote it, the host can
verify it, so a mismatch is a real `stale`, unlike the always-manual
surface-11 Desktop-store row. Evidence: `_evidence/update/cowork-restage.txt`
(one host command producing matching engine + skill version stamps);
regression test: `tests/test_cowork_restage.py`.

**Rejected:** treating the presence of `local-desktop-app-uploads` as
evidence of scriptability (it is a directory the APP manages for itself,
inside a session-scoped path — not a stable host-writable target); building
a "refresh the rpm cache" script against the opaque per-session ID (no
external identifier maps to "the currently open Cowork session" from a host
shell, and the IDs are server-assigned, not derivable); re-opening Ruling 4
to make surface 11 scriptable (the empirical result is negative — nothing to
upgrade).

## Addendum (2026-07-07) — v0.10.0 code-review finding: stale `importlib.metadata` on a dev/pip host (non-blocking) — no code change

**Finding under review.** A code-review gate for v0.10.0 flagged (non-blocking)
that on a host whose venv carries a **stale editable-install `dist-info`**
(reproduced locally: a stray `profile_a_brain-0.3.0.dist-info` made
`importlib.metadata.version("profile-a-brain")` return `0.3.0` while the
committed stamp — `src/brain/_version.py` — said `0.9.1`/`0.10.0`), `brain
--version` / `brain.__version__` reports the stale metadata version, since
Ruling 1's read order puts `importlib.metadata` first. The question: does
`brain/__init__.py`'s resolution order need hardening against this case?

**Decision: no code change to the version-resolution path.** The current
posture — metadata primary, doctor detects, update remediates — is judged
sufficient, for four reasons:

1. **This is not a new failure mode; it is the failure mode Ruling 2 surface 3
   was built to catch.** `check_host_venv()` in `src/brain/doctor.py` runs
   `<venv>/bin/brain --version` (which resolves through the very same
   `importlib.metadata` call this finding is about) and compares the result
   against the pyproject SSOT. Any mismatch — stale-low *or*
   stale-looking-high, direction doesn't matter — returns `STALE`, a status
   inside `_GATING_STATUSES`, so it fails `brain doctor`'s exit code and
   prints the `/brainiac-update` remediation. Nothing about the reproduction
   above evades this row: a stray old `.dist-info` in the venv is exactly
   "installed ≠ SSOT".
2. **`brain update`'s `pip install --upgrade -e .` is the correct fix**, and
   it works precisely because the defect lives in the venv's package
   metadata, not in the source tree — reinstalling regenerates the
   `.dist-info` and the stale entry stops shadowing the real one.
3. **Any in-code heuristic to "prefer or annotate based on which is
   trustworthy" would recreate the exact thing Ruling 1 already rejected**
   ("Making the stamp primary everywhere") — just gated behind a comparison
   instead of unconditional. `brain/__init__.py` has no signal at import time
   for *which* of the two disagreeing values is correct beyond a semver
   compare, and semver-picking a winner would mean `importlib.metadata`
   sometimes loses even though it is, by definition, reporting what is
   *actually installed* — masking exactly the skew `brain doctor` exists to
   surface, on the one host (pip-installed) where that signal matters most.
   Annotating `__version__`/`brain --version`'s output on disagreement would
   also change the shape of a machine-read surface — an ADR-0004 Ruling 2
   MAJOR-bar change — to patch a case a dedicated, tested, non-fatal-by-default
   surface already owns.
4. **Blast radius is already bounded.** The stale-metadata condition only
   arises on a dev/pip host with a corrupted or double-installed venv; the
   zero-install VM path (no `importlib.metadata` answer at all) is unaffected
   and continues through the committed-stamp fallback exactly as Ruling 1
   specifies; the clean-room export path is unaffected (proven by
   `tests/test_cleanroom_export_smoke.py`, which denies package metadata
   entirely).

**Rejected:** inverting or conditionally inverting the metadata/stamp
precedence in `brain/__init__.py` (masks the exact skew signal Ruling 1's
primary ordering exists to expose — rejected on the same grounds as Ruling
1's original "Making the stamp primary everywhere" rejection, now considered
again under the stale-editable-dist-info framing and rejected again);
annotating `__version__`/CLI `--version` output with a disagreement marker
(changes a machine-read surface's shape for a case `brain doctor` already
reports structurally, non-fatally by default, with a working remediation);
adding a new doctor surface or test for this exact reproduction (surface 3 /
`check_host_venv()` already is that surface — a stray stale `.dist-info` is
indistinguishable, from `check_host_venv`'s point of view, from any other
"installed ≠ SSOT" cause, and it is already covered without new code).

**No files changed as a result of this addendum** beyond this ADR entry.
`tests/test_version_stamp.py` and `tests/test_cleanroom_export_smoke.py`
remain green, unmodified — neither test exercises `check_host_venv()`'s venv
subprocess path, so no regression test was added; the stale-editable-metadata
case is a `doctor.py`-surface concern, not a `brain/__init__.py`-resolution
concern, and doctor's existing STALE-classification behavior for surface 3
already generalizes to it without needing a new fixture.

## Addendum (2026-07-07): role-aware VM leg — real crash, not theoretical

`brain doctor` is in `VM_ALLOWED` (s02 decided read-only inspection is safe
on the Cowork VM leg), but a real Cowork session hit
`ModuleNotFoundError: workspace_registry` running it. Root cause:
`run_doctor()`'s staged-workspace surfaces (7-10) import
`tools/workspace_registry.py` to enumerate registered workspaces — a
host-only companion script `cowork_workspace_install.sh` never copies into
the staged zero-install engine (`.brain/engine/brain` is `src/brain` only).
The same gap applies to every other host-only input `run_doctor()` reads
(pyproject SSOT, `~/.brainiac` venv, `~/.claude` plugins, the marketplace
clone, the Desktop store) — none exist on the VM's mounted `.brain/` tree.

**Ruling:** `brain doctor` is now role-aware.

1. `run_doctor()` (host mode, unchanged surfaces) guards the
   `workspace_registry` import — unavailable now degrades to a
   `not-detectable` row instead of raising, regardless of which role asked
   for it (defense in depth: role=host on a machine without `tools/` must
   still not crash).
2. `run_doctor_vm()` (new) covers only what the staged workspace itself
   carries: the running engine's own version (`brain.__version__` — on the
   real VM process this transitively IS the staged copy's stamp, via the
   existing fallback chain in `brain/__init__.py`), the `.brain/skills/*.skill`
   VERSION markers (cw-02 lockstep), the snapshot's schema vs. this binary's
   `SCHEMA_VERSION` (Ruling 2 above), the snapshot's generation/age, the
   bundled model cache (no HF egress on the VM — a missing model silently
   downgrades semantic search to hash embeddings), and the `brain maintain`
   heartbeat file (VM-readable even though only the host runs it). Every
   host-only surface (venv, SSOT, CLI plugins, marketplace cache, Desktop
   store, the registry script itself) is listed as its own `not-detectable`
   row naming `brain doctor` on the host as the remediation — never silently
   dropped, never faked green.
3. **Role detection has a structural fallback.** The staged VM shim
   (`.brain/brain`) runs `python3 -m brain.cli "$@"` directly and does not set
   `$BRAIN_ROLE` — so `doctor` additionally treats the process as VM-postured
   whenever `tools/workspace_registry.py` AND a `pyproject.toml` SSOT are both
   structurally absent (`doctor.looks_like_vm_stage()`), even if `--role`
   was never passed. Every other VM-gated command keeps requiring an
   explicit `--role vm`/`$BRAIN_ROLE`; this fallback is scoped to `doctor`
   only, which is read-only regardless of role.
4. Exit-code gating: only VM-checkable required surfaces (stale engine
   stamp, skill-version mismatch, schema skew, missing model, stale/failing
   maintain heartbeat) fail the VM leg's exit code. Host-only rows are always
   `not-detectable` and never gate, on either leg.

Verified against the real staged workspace (not just fixtures): the
unmodified pre-fix engine crashes with `ModuleNotFoundError` running
`PYTHONPATH=<vault>/.brain/engine BRAIN_VAULT=<vault> python3 -m brain.cli
--role vm doctor`; the fixed engine, copied into the same staged location,
reports `OK: all required surfaces current` under both `--role vm` and no
`--role` at all (the real shim's invocation shape).

**No version bump in this addendum** (0.10.4 cut is the follow-up that ships
this fix); see `CHANGELOG.md` `[Unreleased]`.

## Addendum (2026-07-08) — Cowork Desktop-store refresh: detect(doctor) -> update(skill-creator) -> verify(doctor)

Ruling 4's Desktop-store surface (11) has been best-effort/`manual-required`
since s04: `brain doctor` can **detect** skew there (installed version read
from the per-session `rpm/plugin_*/.claude-plugin/plugin.json` cache vs. the
SSOT) but structurally cannot **fix** it — a Python CLI cannot invoke a
Claude slash-command skill. That gap sat undocumented as "verify/update
manually"; this addendum names the sanctioned closed loop instead of leaving
it to ad hoc clicking:

1. **Detect** — `brain doctor --json`'s Desktop-store rows, unchanged
   mechanism, now carry a remediation string that names the actual fix
   instead of a generic "looks stale?" hedge once `_compare(installed, ssot)
   < 0` (`check_desktop_plugin_store`, `src/brain/doctor.py`); `brain
   update`'s `residual_human_steps` entry was reworded the same way
   (`src/brain/update.py`).
2. **Update** — this is a **Cowork-session-only** loop, documented as its own
   section in `/brainiac-update`'s SKILL.md ("Cowork skill refresh
   (in-session only)"), never in the host `brain update` path (the Desktop
   store isn't reachable from the host at all). It reuses the existing
   `/skill-creator` "Updating an existing skill" flow against the skill's own
   `.brain/skills/<name>.skill` bundle already staged in the session — no
   parallel installer.
3. **Verify** — mandatory, never inferred from the user's click alone: after
   "Save and Replace", re-run `brain doctor --json` and confirm the same row
   now reads the SSOT version. This is not defensive boilerplate — Cowork's
   "Save and Replace" is known to silently no-op (Anthropic #46844, P0, and
   #46836): it can package from a stale host-mounted path, or accept the
   upload without actually overwriting the installed skill on disk, while
   still showing success. A skill is only ever reported "updated" off that
   second `doctor` read.

**No code change to the Desktop-store detection mechanism itself or its
`manual-required` classification** — surface 11 still never gates the exit
code (Ruling 2/4 stand as written). This addendum only (a) sharpens the
remediation strings both `doctor` and `update` already print, and (b)
documents, in the Cowork-only branch of `/brainiac-update`, the concrete
detect→update→verify loop an analyst runs to act on them. Regression:
`tests/test_doctor.py` / `tests/test_update.py` assert the new wording.

**Correction (2026-07-08, v0.10.7).** The shipment above put the Cowork loop
*inside* `/brainiac-update` — which is **host-only** (`brain update` refuses
`--role vm`). A real Cowork run correctly refuses the whole skill before ever
reaching that section, so the loop was unreachable; and its "re-run `brain
doctor` to verify" step can't work in the VM leg anyway (the Desktop store is
host-only → `not-detectable` there). v0.10.7 removes the broken in-skill loop
and corrects the `doctor`/`update` remediation strings to point at
**`/skill-creator` directly** (which does run in Cowork), keeping the #46844
verify-after-click discipline. The *automated* detect→verify loop belongs in a
future **VM-native** `/brainiac-cowork-skills` skill (comparing the *loaded*
skill version against the staged bundle), never bolted onto this host-only one.
This was caught by an actual Cowork run refusing `/brainiac-update` — the
anti-silent-failure posture working against our own mis-design.

**No version bump in this addendum** (a v0.10.6 cut is the follow-up); see
`CHANGELOG.md` `[Unreleased]`.
