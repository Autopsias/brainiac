# Starting a second vault (returning-user path)

You already have `brain` installed and working against one vault. Now you
want a second, independent vault — a different project, a different owner
context, a demo sandbox, whatever. This page is what the other install docs
don't cover: **the install is one binary; the vault is not singular.**

Ground truth below is cited to the actual code, not assumed.

## 1. The install is per-machine, the vault is per-`$BRAIN_VAULT`

`brain` is a single binary/pip install on your machine — you do **not**
reinstall it per project. Which vault a given `brain` invocation talks to is
resolved fresh, every call, from:

> explicit `--vault` arg > `$BRAIN_VAULT` env var > `./vault` (cwd)
> — `src/brain/config.py:98-104` (`vault_root()`)

So the whole second-vault story is: **run `brain` from a different directory,
or export a different `$BRAIN_VAULT`.** Two terminals, two `BRAIN_VAULT`
values, same binary, two vaults. No second install.

```bash
# vault 1 (existing)
export BRAIN_VAULT=~/vaults/work
brain recent --json

# vault 2 (new) — separate shell/session, or just re-export
export BRAIN_VAULT=~/vaults/personal
brain recent --json
```

`--vault` is a **top-level** flag (`brain --vault ~/vaults/personal rebuild`,
not `brain rebuild --vault ...`) — see `brain --help`.

## 2. Yes, run `brain init --full` again — once per vault

`brain init --full` never opens the index or constructs `BrainCore`
(`src/brain/init.py:1-30`) — it only scaffolds the overlay and (on host)
registers the scheduled task. It is **not** a one-time-per-machine step; it
is a **per-vault** first-run step, because everything it touches
(`overlay/`, the routines manifest copy) is resolved under `<vault>/...`:

```bash
export BRAIN_VAULT=~/vaults/personal
brain init --full
```

This is idempotent and safe even though you already ran it for vault 1 — it
only fills empty overlay categories and only touches this vault's paths.

## 3. Overlay: one set per vault, automatically

`overlay_dir()` resolves as: explicit `--overlay-dir` > `$BRAIN_OVERLAY_DIR`
> `<vault>/overlay` (`src/brain/overlay.py:36-51`). As long as you don't set
`$BRAIN_OVERLAY_DIR` globally, each vault gets its own `overlay/` — vault 2's
`brain init --full` scaffolds a **fresh, empty** overlay from
`overlay/template/`, independent of vault 1's filled-in voice/brand/keywords/
people. Fill it in for the new context; it never reads or clobbers the other
vault's overlay.

If you *did* export `$BRAIN_OVERLAY_DIR` for some reason, unset it before
working in a second vault — it would otherwise force both vaults onto the
same overlay.

## 4. `.brain/` runtime dir AND the index: one per vault, automatically

Most per-vault runtime state already lives under `<vault>/.brain/`
(`brain_runtime_dir()`, `src/brain/config.py`): the published snapshot, the
capture inbox, the routines manifest copy. Two vaults get two independent
`.brain/` trees for free.

Since 0.3.0 the derived **search index** and the **audit-chain log** are
per-vault too, automatically: `config.index_dir(vault)` maps each resolved
vault path to its own `vaults/<name>-<hash8>/` subdirectory under the
per-user app-data base (`~/Library/Application Support/profile-a-brain` on
macOS, etc.), and `BrainCore` threads the active vault into both the index
path and the default audit log. Any number of vaults coexist with **no env
var to remember** — just point `$BRAIN_VAULT`/`--vault` at the vault and go.

`$BRAIN_INDEX_DIR` still overrides completely (returned as-is, no per-vault
nesting) for tests and constrained deployments that must pin the location.

**Upgrading from pre-0.3.0:** the old single global `index.sqlite` at the
app-data base is a dead cache — the first `brain rebuild` per vault creates
the new per-vault index; delete the old file whenever. The old global
`audit_chain.jsonl` is NOT deleted or moved: it stays frozen and verifiable
at the legacy path, new writes start a fresh per-vault chain, and `brain`
prints a one-time notice saying exactly that (same two-chain model as a key
rotation — see `SECURITY.md`).

## 5. The nightly scheduled task: genuinely needs a second registration

This is the one place two vaults **cannot** share the existing tooling
as-is. The maintenance task is registered under a **per-vault** label /
task name derived from the vault path (single source of truth:
`brain.config.nightly_label`):

- macOS: `com.brainiac.nightly.<vault-id>`, written to
  `~/Library/LaunchAgents/com.brainiac.nightly.<vault-id>.plist`
  (`scripts/install-brief-mac.sh`).
- Windows: task name `brain-daily-brief-<vault-id>`
  (`scripts/install-brief-windows.ps1`).

So registering vault 2 **adds a second task alongside vault 1's** — nothing
is overwritten, and each vault's hourly maintenance runs independently. The
installer also migrates any pre-0.19 install off the old single shared label
(`com.profile-a-brain.daily-brief` / `brain-daily-brief`) the first time it
runs; if you still see that legacy name in your LaunchAgents or Task
Scheduler, re-run the installer for the vault it points at.

## Summary table

| Thing | Per-vault by default? | Action for vault 2 |
|---|---|---|
| `brain` install/binary | N/A — one install | none |
| Vault selection | `$BRAIN_VAULT` / `--vault` | export/point at new path |
| `brain init --full` | must be re-run | run it once for the new vault |
| `overlay/` | yes (`<vault>/overlay`) | fill in fresh, nothing to do |
| `.brain/snapshot`, `capture-inbox`, routines copy | yes (`<vault>/.brain/`) | nothing to do |
| Search index + audit chain | yes (per-vault app-data subdir since 0.3.0) | nothing to do — `brain rebuild` once |
| Nightly scheduled task | yes — per-vault label (`com.brainiac.nightly.<id>`) | none — registered automatically by `brain init --full` |

## Cross-references

- `docs/install/new-owner.md` — first-vault mental model (`brain init`,
  overlay, host/VM split)
- `docs/install/README.md` — pick your client
- `AGENTS.md` §6 — host/VM trust split and the four verbs
- `brain --help` — always-current CLI contract
