---
name: brainiac-update
description: One-command refresh of an existing Brainiac install — self-executes the marketplace refresh, the downgrade-safe CLI-plugin reinstall, a CHANNEL-AWARE engine reinstall (uv tool / pipx / pip --user / editable-checkout, whichever is actually live), and every registered Cowork workspace re-stage, then runs `brain doctor` to verify and prints a before→after version table. Use when the user says "update brainiac", "/brainiac-update", "refresh brainiac", or the marketplace/plugin just pulled a new brainiac-manager version.
---

# /brainiac-update

The fix for every stale-artifact failure, and it RUNS the fix rather than
printing a to-do list: marketplace refresh → downgrade-safe CLI-plugin
reinstall → **channel-aware** engine reinstall → every registered Cowork
workspace re-staged → `brain doctor` verify, one before→after version table,
one pass/fail. Re-runnable, idempotent, safe to run with nothing changed.

**Channel-aware (PYP-04, 2026-07-11):** the engine step self-detects which of
the four channels is actually live on this host — `uv tool install`, `pipx`,
`pip --user`, or the legacy editable dev checkout (`~/.brainiac/venv`) — and
runs THAT channel's own upgrade command (`uv tool upgrade brainiac-cli` /
`pipx upgrade brainiac-cli` / `pip install --user --upgrade
'brainiac-cli[mcp]'` / `git pull` + `pip install -e`). Never assumes the
pre-PyPI editable-venv shape. `brain doctor`'s "Host engine venv" row reports
the detected channel; relay it verbatim rather than guessing.

This skill is a thin driver over `brain update` (ADR-0005 Ruling 3, UP-01/
UP-02 — `src/brain/update.py`, dispatched by `src/brain/cli.py`). The engine
does the actual work; this skill's job is to re-resolve the checkout, invoke
`brain update`, and relay its report. It never reimplements the version
compare, the reinstall decision, or the doctor verify — those are tested
Python (`tests/test_update.py`, `tests/test_doctor.py`), not skill prose.

**Never-touch list (hard, ADR-0004 Ruling 4 — audited against every state
surface the parity build created):** `vault/brain/` notes, `vault/raw/`
(immutable, incl. `raw/originals/`), the audit chain + its WAL, the **signing
key** (never regenerate/rotate — a separate human procedure), the owner
overlay (`vault/overlay/`), and anything in `index.sqlite` beyond what
`brain sync` itself does. This also covers every ADR-0003 state surface a
refresh must leave alone: `.brain/memory/` (handoff/hot/lessons/archive,
incl. `recommendations-open.jsonl` + `recommendations-log.md`),
`.brain/maintain-state.json` + `.brain/maintain.lock` (maintain
heartbeat/lock), `.brain/brief/` (rendered HTML), `.brain/graph/` (graphify
output), `vault/inbox/` + `inbox/_quarantine/` (ingestion drop zone),
`capture-inbox/` (the VM-facing unsigned-draft drop zone under
`.brain/runtime/`), and the vault's content generally. The
`cowork_workspace_install.sh` stager (invoked by `brain update`'s workspace
re-stage step) only ever `rm -rf`s its own `.brain/engine/` staging dir and
`mkdir -p`s `bin/ model/ snapshot/ skills/ routines/` — it never touches any
of the paths above. Also invariant, never widened by an update: the
single-OS-task lock (`routines/manifest.json` host 1 / VM 0 — the nightly
task step only ever refreshes the existing task in place, never adds one)
and the host/VM trust split (`VM_ALLOWED` is never extended and the VM is
never granted a key by this skill — `brain update` is host-broker only and
is refused outright if invoked with `--role vm`).

## Step 0 — detect and migrate an old-name install (NAM-03)

Anyone who installed before the 2026-07-11 rename (`profile-a-marketplace` →
`brainiac`, `profile-a-kernel`/`profile-a-extras` → `brainiac-kernel`/
`brainiac-extras` — `docs/adr/0006-distribution-naming.md`) has the old names
registered in `~/.claude/plugins/known_marketplaces.json` and/or
`installed_plugins.json`. Detect this BEFORE Step 1, using the already-tested
detector rather than re-deriving it in prose:

```
brain doctor --json | python3 -c \
  'import json,sys; r=json.load(sys.stdin); \
   row=[x for x in r["rows"] if x["surface"]=="Stale-name plugin/marketplace install"][0]; \
   print(row["status"])'
```

If the row is `not-detectable`, skip straight to Step 1 — nothing to
migrate. If it is `stale`, run the migration below. **Order is
install-new-before-remove-old, never the reverse** — this is the single most
destructive step in this skill, more so than the reinstall in Step 2, because
a failure partway through must never strand the operator with zero lifecycle
skills:

1. **Record prior state.** Copy the two state files verbatim to a dated
   snapshot before touching anything:
   ```
   ts=$(date +%Y%m%dT%H%M%S)
   mkdir -p "$HOME/.brainiac/migration-state"
   cp "$HOME/.claude/plugins/known_marketplaces.json" \
      "$HOME/.brainiac/migration-state/known_marketplaces-$ts.json" 2>/dev/null
   cp "$HOME/.claude/plugins/installed_plugins.json" \
      "$HOME/.brainiac/migration-state/installed_plugins-$ts.json" 2>/dev/null
   ```
2. **Add the NEW marketplace** (idempotent — a no-op if already added):
   ```
   claude plugin marketplace add Autopsias/brainiac
   ```
3. **Install the new-name plugins** — mirror whichever of the old
   `profile-a-kernel@profile-a-marketplace` / `profile-a-extras@profile-a-marketplace`
   / `brainiac-manager@profile-a-marketplace` were actually installed (read
   from the recorded `installed_plugins-$ts.json`, not assumed):
   ```
   claude plugin install brainiac-manager@brainiac
   claude plugin install brainiac-kernel@brainiac    # only if profile-a-kernel was installed
   claude plugin install brainiac-extras@brainiac    # only if profile-a-extras was installed
   ```
4. **Verify the new install resolves BEFORE removing anything old** — confirm
   `brainiac-manager@brainiac` (and any other installed new-name plugin) is
   present in `installed_plugins.json` and that `/brainiac-install`,
   `/brainiac-update`, `/brainiac-uninstall`, `/brainiac-cowork-setup` are
   listed as available skills in this session. **If verification fails, STOP
   here — do not remove the old marketplace/plugins.** The old install is
   still intact (nothing destructive has happened yet), so the operator loses
   nothing; report the recorded-state path
   (`~/.brainiac/migration-state/*-$ts.json`) and the exact commands from
   steps 2-3 so a human can retry or investigate.
5. **Only now, remove the old marketplace/plugins:**
   ```
   claude plugin uninstall brainiac-manager@profile-a-marketplace 2>/dev/null
   claude plugin uninstall profile-a-kernel@profile-a-marketplace 2>/dev/null
   claude plugin uninstall profile-a-extras@profile-a-marketplace 2>/dev/null
   claude plugin marketplace remove profile-a-marketplace
   ```
6. Re-run the Step-0 detector; report `not-detectable` as the migration
   succeeding. Continue to Step 1 either way.

**Plugin-independent recovery** (works even if this skill itself is what's
broken, e.g. its own plugin is stuck mid-migration): the exact 2-command
add-new sequence (step 2 + step 3's `brainiac-manager` line) is also printed
verbatim by `brain doctor`'s stale-name remediation, and documented in
README's "Updating an existing install" section and the CHANGELOG rename
entry — none of those three surfaces depend on a working plugin install.

## Step 1 — re-resolve the checkout (only needed for Cowork re-stage)

**PYP-04: a checkout is no longer required for the engine step itself** — the
channel-aware engine refresh (below) upgrades in place via `uv`/`pipx`/`pip`,
no clone involved. A checkout is still needed for exactly one thing:
`brain update`'s dist-rebuild + Cowork workspace re-stage legs
(`tools/package_clients.py`, `dist/cowork-skills/*.skill`) — those aren't
wheel-packaged yet. `brain update` **auto-skips both gracefully** (reports
`ok: true, skipped`) when no checkout is present, so a host-only PyPI install
with no Cowork workspaces never needs this step at all.

If the user registered any Cowork workspace (`tools/workspace_registry.py`
target `cowork-vm` — ask, or just try Step 2 first and read its
`workspace_restage` results), resolve/refresh the checkout first:

```
if [ -d "$HOME/brainiac/.git" ]; then
  git -C "$HOME/brainiac" pull --ff-only
else
  gh repo clone Autopsias/brainiac "$HOME/brainiac"
fi
```

If the pull fails (diverged, auth, network) — report the exact error and
stop; do not run `brain update --engine-src ~/brainiac` against a checkout
you couldn't refresh. If there's no Cowork workspace, skip this step
entirely and go straight to Step 2 (omit `--engine-src`).

## Step 2 — run `brain update`

This ONE call does everything Steps 0–5 of the old print-only flow used to
tell a human to do by hand. It self-executes, in order (`src/brain/update.py`
`run_update()`):

1. **Preflight capability probe** — confirms the `claude plugin` CLI surface
   (`marketplace`, `list`, `uninstall`, `install`, `update`) still exists
   before any destructive call; BLOCKS with the exact manual fallback
   commands if the surface doesn't match (Claude Code's plugin cache layout
   is undocumented-fast-moving — pruning gh#69626, non-atomic auto-update
   gh#40153 — never drive it blind).
2. **Marketplace refresh FIRST** — `claude plugin marketplace update
   <marketplace>` — structurally kills the stale-cache no-op; the version
   compare below is only meaningful against a fresh marketplace state.
3. **Per-plugin downgrade-safe reinstall decision** (ADR-0005 Ruling 3,
   amending ADR-0004 Ruling 5's "print exactly that instruction" migration
   prose): for each of the three kernel plugins, compare installed vs
   marketplace version via `packaging.version.Version` (never a string
   compare — `0.9.1 < 0.10.0` numerically, backwards stringwise):
   - installed `>` marketplace (the one-time reconciliation downgrade,
     e.g. 1.1.0 → 0.9.x) → **automatic clean reinstall**: `claude plugin
     uninstall` then `claude plugin install`, no user hand-steps (`update`
     cannot downgrade, so a clean reinstall is the only path here);
   - installed `<` marketplace → `claude plugin update` (**not** `install`
     — `claude plugin install` on an already-installed plugin is a no-op:
     it prints "already installed" and leaves the old version in place;
     `claude plugin update` is the subcommand that actually moves the
     version forward);
   - equal → skip.

   No-op detection: after the `update` action runs, the installed version
   is re-read and checked against the marketplace version. If it did not
   move — a no-op `update` call, e.g. an older `claude` binary silently
   falling back — the step is reported **failed**, never a false `ok:
   true`, with the manual recovery command (`/plugin update
   <plugin>@<marketplace>`, then restart).

   Rollback safety (uninstall-then-install is the single most destructive
   step here): if uninstall succeeds but the following install fails, the
   plugin is now **absent** — worse than the downgraded start. `brain
   update` detects this exact half-applied state, stops immediately, and
   reports the precise recovery command (`claude plugin install
   <plugin>@<marketplace>`) rather than a green report over a broken
   install.
4. **Engine reinstall, channel-aware** — detects the live channel (via the
   PATH-resolved `brain` binary) and runs that channel's own upgrade:
   `uv tool upgrade brainiac-cli`, `pipx upgrade brainiac-cli`, `pip install
   --user --upgrade 'brainiac-cli[mcp]'`, or (editable-checkout only) `pip
   install --upgrade -e '<engine-src>[mcp]'` against `~/.brainiac/venv`.
   Never assumes the pre-PyPI editable shape. Captures old → new
   `brain --version`.
5. **dist-rebuild + workspace re-stage (Cowork only, auto-skipped if no
   checkout)** — if a checkout is available: `tools/package_clients.py`
   rebuilds `dist/COMPAT` + the `.skill` bundles, then every entry in
   `tools/workspace_registry.py` is re-staged, branched by `target`: `host`
   entries rely on the engine reinstall (Step above) plus the nightly task
   refresh; `cowork-vm` entries re-run `cowork_workspace_install.sh` then
   `brain sync --publish` so the VM's next read sees the just-committed
   notes and the new schema. Same host/arch guard and missing-folder
   skip-don't-delete behavior as before. If NO checkout is available (the
   common PyPI-first host-only case), both steps report `ok: true, skipped`
   — never a failure.
6. **`brain doctor` verify** — the final pass/fail. Every surface from
   ADR-0005 Ruling 2's table is re-checked; the process exit code (and this
   skill's final report) is `0`/PASS only when every scriptable REQUIRED
   surface reads `current`. The Desktop/Cowork plugin store (surface 11) is
   always `manual-required` and never gates this — see Step 3 below.

Invoke it and relay its output verbatim — do not re-derive the report by
hand. `brain` is on PATH regardless of channel (uv tool / pipx / pip --user
all put it there), so just call it directly — no `~/.brainiac/venv/bin/`
prefix needed unless this host is on the legacy editable-checkout channel:

```
brain update                                    # no Cowork workspace, or don't know
brain update --engine-src "$HOME/brainiac"      # Cowork workspace registered (Step 1 ran)
```

(`--dry-run` runs every read/decision step for real but skips every mutating
call — useful to preview what would happen; `--json` for a machine-readable
report; `--marketplace <name>` if the marketplace isn't
`brainiac`.)

If `brain update` reports `BLOCKED` (capability probe failed) or
`INCOMPLETE` (half-applied reinstall), **stop and relay its `notes` field
verbatim** — it already contains the exact manual commands needed. Do not
paper over either state with a green summary.

## Step 3 — the one residual step is now OPTIONAL, not required

The Claude Desktop / Cowork plugin store has no external CLI to script
against (ADR-0005 Ruling 4, confirmed empirically by the s04 addendum: no
supported CLI, config, or import reaches it from outside the app) —
`brain update`'s report used to name a required manual click here. Since
cw-02, it doesn't: this same run already re-staged current-version `.skill`
bundles alongside the engine for every registered Cowork workspace, and
`brain doctor`'s "Staged skill bundles" row per workspace confirms it with a
real version-matched `current`, not a guess. Opening Cowork/Desktop →
Plugins → check for update, or re-uploading the `.skill` packages
(`dist/cowork-skills/*.skill`) via Cowork's Save-skill flow, is only needed
for a workspace's first-ever skill upload or if the analyst prefers the
in-session Plugins-tab convenience — never because this host step left
something stale.

## Cowork Desktop-store skills — this host-only skill cannot refresh them

**This skill is host-only** (`brain update` refuses `--role vm` by design), so
invoking `/brainiac-update` inside a Cowork session is correctly refused — it
has no Cowork branch. `brain doctor`'s Desktop-store rows (surface 11,
`check_desktop_plugin_store`) are best-effort and always `manual-required`: the
CLI **detects** skew but structurally cannot fix it (a Python process can't
invoke a Claude slash-command skill), and it can only see the store from the
host anyway.

To bring the Desktop-tab skills current, do it **in a Cowork session with
`/skill-creator` directly** (not `/brainiac-update`): for each stale skill, hand
`/skill-creator` the current version — the workspace already has it staged at
`.brain/skills/<name>.skill` — let it repackage and present the `.skill`, then
click **Save and Replace**.

**Verify after the click, always.** Cowork's "Save and Replace" can silently
no-op — it may package from a stale host-mounted path or accept the upload
without overwriting the installed skill on disk, while still showing a success
toast (Anthropic #46844 P0 / #46836). So re-read the installed skill's version
and never trust the click alone; if it didn't move, re-open the `.skill`
(confirm it packaged from the VM working copy) and re-install, or remove and
re-add the plugin.

> A dedicated **VM-native** `/brainiac-cowork-skills` skill that automates this
> detect → `/skill-creator` → verify loop (comparing the *loaded* skill version
> against the staged bundle) is planned — it belongs in its own Cowork-safe
> skill, not bolted onto this host-only one.

Step 3 above's staged-bundle canonical path is unchanged and remains the
host-recognized source of truth regardless of what the Desktop store shows.

## Teardown

Full removal (venv, nightly labels, registry entries this host owns) is a
separate command: `/brainiac-uninstall`. This skill never deletes the venv
or deregisters entries on its own.

<!-- SKILL_VERSION: 0.19.0 (generated by tools/package_clients.py — do not hand-edit) -->
