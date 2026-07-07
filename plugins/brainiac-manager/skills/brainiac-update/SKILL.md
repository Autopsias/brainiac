---
name: brainiac-update
description: One-command refresh of an existing Brainiac install — self-executes the marketplace refresh, the downgrade-safe CLI-plugin reinstall, the engine venv reinstall, and every registered Cowork workspace re-stage, then runs `brain doctor` to verify and prints a before→after version table. Use when the user says "update brainiac", "/brainiac-update", "refresh brainiac", or the marketplace/plugin just pulled a new brainiac-manager version.
---

# /brainiac-update

The fix for every stale-artifact failure, and it RUNS the fix rather than
printing a to-do list: marketplace refresh → downgrade-safe CLI-plugin
reinstall → engine venv reinstall → every registered Cowork workspace
re-staged → `brain doctor` verify, one before→after version table, one
pass/fail. Re-runnable, idempotent, safe to run with nothing changed.

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

## Step 1 — re-resolve the canonical checkout

The plugin's own `${CLAUDE_PLUGIN_ROOT}` is never the code copy — it is
ephemeral and gets pruned/re-cloned. `~/brainiac` is the canonical checkout:

```
if [ -d "$HOME/brainiac/.git" ]; then
  git -C "$HOME/brainiac" pull --ff-only
else
  gh repo clone Autopsias/brainiac "$HOME/brainiac"
fi
```

If the pull fails (diverged, auth, network) — report the exact error and
stop; do not run `brain update` against a checkout you couldn't refresh.

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
4. **Engine venv reinstall** — `pip install --upgrade -e <engine-src>`
   against the `~/.brainiac/venv`, engine source resolved from an explicit
   override / `$BRAINIAC_ENGINE_SRC` / the checkout's own root — never a
   hardcoded path. Captures old → new `brain --version`.
5. **Workspace re-stage** — every entry in `tools/workspace_registry.py`,
   branched by `target` exactly as before: `host` entries rely on the venv
   reinstall (Step above) plus the nightly task refresh; `cowork-vm` entries
   re-run `cowork_workspace_install.sh` then `brain sync --publish` so the
   VM's next read sees the just-committed notes and the new schema. Same
   host/arch guard and missing-folder skip-don't-delete behavior as before.
6. **`brain doctor` verify** — the final pass/fail. Every surface from
   ADR-0005 Ruling 2's table is re-checked; the process exit code (and this
   skill's final report) is `0`/PASS only when every scriptable REQUIRED
   surface reads `current`. The Desktop/Cowork plugin store (surface 11) is
   always `manual-required` and never gates this — see Step 3 below.

Invoke it and relay its output verbatim — do not re-derive the report by
hand:

```
"$HOME/.brainiac/venv/bin/brain" update --engine-src "$HOME/brainiac"
```

(`--dry-run` runs every read/decision step for real but skips every mutating
call — useful to preview what would happen; `--json` for a machine-readable
report; `--marketplace <name>` if the marketplace isn't
`profile-a-marketplace`.)

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

## Teardown

Full removal (venv, nightly labels, registry entries this host owns) is a
separate command: `/brainiac-uninstall`. This skill never deletes the venv
or deregisters entries on its own.

<!-- SKILL_VERSION: 0.10.5 (generated by tools/package_clients.py — do not hand-edit) -->
