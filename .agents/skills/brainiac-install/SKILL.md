---
name: brainiac-install
description: One-command host install of Brainiac — clone/locate the repo, run install.sh, verify search, register the nightly maintenance task, provision the audit signing key (idempotent), record the vault in the workspace registry, and print an explicit pass/fail report. Use when the user says "install brainiac", "/brainiac-install", or has just added the profile-a-marketplace and wants the full host setup done in one shot.
---

# /brainiac-install

Runs the whole host install. Imperative steps — execute them in order, do not
ask permission between steps except where a human gate is named explicitly.

## Step 0 — sandbox self-check (refuse, don't silently degrade)

If this shell does not run directly on the user's own machine (e.g. you are
Cowork, a cloud/remote agent, or any ephemeral VM/sandbox) — STOP. Do not run
install.sh there; it would be wiped and never reach the user's PATH. Instead
print exactly this block for the user to paste into their own terminal, then
wait for them to report back the output before continuing:

```
cd "$HOME/brainiac" 2>/dev/null || gh repo clone Autopsias/brainiac "$HOME/brainiac"
cd "$HOME/brainiac" && ./install.sh
```

Cowork has its own read+draft setup path (`docs/install/cowork.md` /
`/brainiac-cowork-setup`) — never conflate the two.

## Step 1 — resolve the canonical checkout

Canonical code copy is always `~/brainiac` (never the plugin's own
`${CLAUDE_PLUGIN_ROOT}` — that cache dir is ephemeral and gets pruned).

```
if [ -d "$HOME/brainiac/.git" ]; then
  git -C "$HOME/brainiac" pull --ff-only
else
  git clone https://github.com/Autopsias/brainiac.git "$HOME/brainiac"
fi
```

`Autopsias/brainiac` is public, so a plain anonymous `git clone` (or
`gh repo clone`) works with no credentials. If it still fails (network
issue), tell the user the exact error and stop.

## Step 2 — run the installer

```
cd "$HOME/brainiac" && ./install.sh
```

Report ✅/❌ for this step based on exit code. On failure, show the exact
error and stop — do not proceed to later steps against a broken install.

## Step 3 — verify search

```
cd "$HOME/brainiac" && brain search "arctic-embed vs e5" --json
```

Confirm the JSON output has: results with `classification` tiers present, an
egress/filter block, and **no embedder warning** in stderr/stdout. Report
✅/❌.

## Step 4 — normalize the vault layout, then register the nightly task

**Layout rule (must match `/brainiac-cowork-setup` and the Cowork session
prompt, which hardcodes `BRAIN_VAULT="$PWD/vault"`): the vault always lives
at `<workspace>/vault`.** Resolve the path the user gave BEFORE running
anything:

- Path does not exist, or exists but is not yet a vault (no `brain/`, `raw/`,
  or top-level `overlay/` inside) → it is the **workspace**; the vault is
  `<path>/vault` (create it).
- Path already IS a vault (has `brain/`, `raw/`, or `overlay/`) → use it as
  the vault; its **parent** is the workspace. If its basename is not `vault`,
  tell the user Cowork setup will need a `<workspace>/vault` layout and ask
  before proceeding.

Never scaffold a vault directly into the folder the user named — that is
what causes a second, split-brain vault when Cowork setup later creates
`<path>/vault`.

```
BRAIN_VAULT=<workspace>/vault brain init --full --apply
```

This registers the nightly task — a **per-vault** launchd label
`com.brainiac.nightly.<id>` (`<id>` = the vault's 8-hex slug; Windows:
`brain-daily-brief-<id>`), daily 07:00. It is the ONE sanctioned host
scheduled task PER VAULT (AGENTS.md §6). Each vault owns its own plist, so
registering a second vault never disturbs the first — the pre-0.9.0
single-shared-label repoint hazard is gone, and the installer retires the
legacy `com.profile-a-brain.daily-brief` plist on first per-vault run.
**Idempotent:** "already registered" for the same vault = success, not
failure.

Report the exact label + schedule + which vault it points at in the final
report.

## Step 5 — audit signing key (idempotent — PRESERVE, never rotate)

Step 4's `brain init --full --apply` already provisions the key
automatically (engine-side `provision_signing_key()` — create-if-absent,
**never rotates**; stored in the macOS Keychain / Windows Credential Manager
under service `profile-a-brain-audit-key`). Read the `audit_key` field of its
report: `present` / `created` are both ✅. Nothing else to do in the common
case.

**Do NOT touch the key yourself** — no `security` / keychain commands in any
form (agent-side credential-store access is blocked by safety policy, and
correctly so). If the report says `unavailable`, run `brain audit-key --json`
once (same idempotent engine path, useful after the user fixes whatever
blocked it) or relay the reported error to the user; a macOS Keychain
approval prompt may need their click.

Tell the user in one sentence what the key does (signs every committed note
so the audit chain is tamper-evident). NEVER print, persist, or regenerate
key material.

Report ✅ "audit key: present (preserved)" / ✅ "created" / ❌ "unavailable —
<reason>" (and then the install is NOT done).

## Step 6 — workspace registry write

Record this vault in `~/.brainiac/workspaces.json` through the shared helper
— never hand-write the file or reinvent the lock:

```
python3 "$HOME/brainiac/tools/workspace_registry.py"   # sanity self-check only
```

then, from Python (import, don't shell out to a one-off script):

```python
import sys
sys.path.insert(0, "<repo>/tools")
from workspace_registry import upsert_entry
upsert_entry(vault_path="<vault-path>", target="host")
```

This upserts by `(host, arch, target, realpath(vault_path),
realpath(workspace_path))` — re-running `/brainiac-install` against the same
vault updates the existing entry in place rather than duplicating it.

## Step 7 — Cowork offer (optional, one question only)

Ask ONE question: "Do you also use Claude Desktop's Cowork and want this
brain available there?" If no, skip to the final report. If yes, hand off to
`/brainiac-cowork-setup` (or, if that skill isn't installed yet, follow
`docs/install/cowork.md` step-by-step) — do not inline its logic here.

## Final report — mandatory template

Always end with this exact shape, one line per check, ✅ or ❌ (never
omitted, never replaced with prose):

```
Brainiac install report
------------------------
✅/❌ install (./install.sh)
✅/❌ search verify (tiers + egress block, no embedder warning)
✅/❌ brain-nightly registered — label: com.brainiac.nightly.<id> (per-vault), vault: <workspace>/vault, schedule: <time>
✅/⏳/❌ audit key: present (preserved) | created (by user) | pending user command
✅/❌ workspace registry: recorded <vault_path> (target: host)
```

If ANY line is ❌, say so plainly and do not claim the install is done.
`/brainiac-install` is safely re-runnable — if the user hits a failure, tell
them to fix the reported error and re-run it; every step above is idempotent
by design (re-running never double-registers the nightly task, never
duplicates a registry entry, never rotates an existing key).

To pull a NEW Brainiac release later, the user runs `/brainiac-update` (after
`/plugin marketplace update`) — not `/brainiac-install`. This skill is for
first-time setup and registry entries; `/brainiac-update` handles version
skew, the conditional index rebuild, snapshot republish, and workspace
refresh. Point the user there for routine updates.

<!-- SKILL_VERSION: 0.10.3 (generated by tools/package_clients.py — do not hand-edit) -->
