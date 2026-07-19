---
name: brainiac-install
description: One-command host install of Brainiac — install the `brain` CLI engine from PyPI (uv tool / pipx / pip --user, first success wins), verify search, register the nightly maintenance task, provision the audit signing key (idempotent), record the vault in the workspace registry, and print an explicit pass/fail report. Use when the user says "install brainiac", "/brainiac-install", or has just added the brainiac and wants the full host setup done in one shot.
---

# /brainiac-install

Runs the whole host install. Imperative steps — execute them in order, do not
ask permission between steps except where a human gate is named explicitly.

**Channel (PYP-04, 2026-07-11):** the engine installs from **PyPI**
(`brainiac-cli` — import package + console command both stay `brain`), never
a clone, unless the user is a contributor or has no PyPI/network access. A
clone of `~/brainiac` is now **dev/offline-only**, never the default path.

## Step 0 — sandbox self-check (refuse, don't silently degrade)

If this shell does not run directly on the user's own machine (e.g. you are
Cowork, a cloud/remote agent, or any ephemeral VM/sandbox) — STOP. Do not run
the installer there; anything it puts on PATH would be wiped and never reach
the user's real terminal. Instead print exactly this block for the user to
paste into their own terminal, then wait for them to report back the output
before continuing:

```
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh
bash /tmp/brainiac-install.sh
```

Cowork has its own read+draft setup path (`docs/install/cowork.md` /
`/brainiac-cowork-setup`) — never conflate the two.

## Step 1 — run the installer (PyPI-first)

```
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh
bash /tmp/brainiac-install.sh
```

`install.sh` itself tries, in order, `uv tool install 'brainiac-cli[mcp]'` →
`pipx install 'brainiac-cli[mcp]'` → `python3 -m pip install --user
'brainiac-cli[mcp]'` — first success wins, and it prints which one it used.
Report ✅/❌ for this step based on exit code, and relay the "Installed via:
<channel>" line verbatim into the final report. On failure, show the exact
error and stop — do not proceed to later steps against a broken install.

**No network / no PyPI access / contributing to Brainiac itself (dev/offline
fallback — never the default):**

```
git -C "$HOME/brainiac" pull --ff-only 2>/dev/null || git clone https://github.com/Autopsias/brainiac.git "$HOME/brainiac"
cd "$HOME/brainiac" && ./install.sh --dev
```

This is an **editable install from the clone** — only use it when the human
explicitly asks for dev/offline mode, or Step 1's PyPI attempt fails and the
user confirms they want the clone fallback instead of fixing network/PyPI
access. Never clone silently as a first resort.

## Step 2 — verify the engine

```
brain --version
```

Confirm it prints a version with no error. Report ✅/❌. If `brain` is not
found, the install likely succeeded into a PATH the current shell doesn't
see yet (uv/pipx both manage their own PATH wiring, sometimes needing a new
shell) — check `install.sh`'s own PATH-hint output from Step 1 and relay it
to the user rather than guessing.

## Step 3 — normalize the vault layout, register the nightly task, verify search

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
failure. Since `vault/brain/` is genuinely empty on a first-time install,
`brain init --full` also seeds 3 generic sample notes — so this same call
gives you content to verify search against, right here:

```
brain search "arctic-embed vs e5" --json
```

Confirm the JSON output has: results with `classification` tiers present, an
egress/filter block, and **no embedder warning** in stderr/stdout. If the
vault was NOT empty (an existing vault the user pointed at), search against
whatever term is likely to hit their own content instead. Report ✅/❌.

Report the exact label + schedule + which vault it points at in the final
report.

## Step 4 — audit signing key (idempotent — PRESERVE, never rotate)

Step 3's `brain init --full --apply` already provisions the key
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

## Step 5 — workspace registry write

Record this vault in `~/.brainiac/workspaces.json` through the shared helper
— never hand-write the file or reinvent the lock.

**Known gap (S07, tracked, not yet closed):** `tools/workspace_registry.py`
is **not yet wheel-packaged** — unlike `scripts/register_tasks.py` and the
installer scripts, it isn't in `pyproject.toml`'s engine-asset mirror
(`src/brain/_assets/`), so a pure PyPI-first install has no local copy of it
to import. Until that's closed, this step still needs a **read-only**
checkout purely to reach this one file:

```
git -C "$HOME/brainiac" pull --ff-only 2>/dev/null || git clone https://github.com/Autopsias/brainiac.git "$HOME/brainiac"
```

then, from Python (import, don't shell out to a one-off script):

```python
import sys
sys.path.insert(0, "$HOME/brainiac/tools")
from workspace_registry import upsert_entry
upsert_entry(vault_path="<vault-path>", target="host")
```

This upserts by `(host, arch, target, realpath(vault_path),
realpath(workspace_path))` — re-running `/brainiac-install` against the same
vault updates the existing entry in place rather than duplicating it. This
clone is **read-only tooling access**, not the install itself (the engine
the user actually runs is still the PyPI install from Step 1) — don't
conflate it with the dev/offline `--dev` fallback in your report to the
user.

## Step 6 — Cowork offer (optional, one question only)

Ask ONE question: "Do you also use Claude Desktop's Cowork and want this
brain available there?" If no, skip to the final report. If yes, hand off to
`/brainiac-cowork-setup` (or, if that skill isn't installed yet, follow
`docs/install/cowork.md` step-by-step) — do not inline its logic here.
Cowork setup still needs a full checkout (`stage_model.py` +
`cowork_workspace_install.sh`) even on a PyPI-first host install — clone
`~/brainiac` there if it isn't already present; that skill's own prompt
covers this.

## Final report — mandatory template

Always end with this exact shape, one line per check, ✅ or ❌ (never
omitted, never replaced with prose):

```
Brainiac install report
------------------------
✅/❌ install — channel: uv tool | pipx | pip --user | editable-checkout (--dev)
✅/❌ engine verify (brain --version)
✅/❌ search verify (tiers + egress block, no embedder warning)
✅/❌ brain-nightly registered — label: com.brainiac.nightly.<id> (per-vault), vault: <workspace>/vault, schedule: <time>
✅/⏳/❌ audit key: present (preserved) | created (by user) | pending user command
✅/❌ workspace registry: recorded <vault_path> (target: host)
```

If ANY line is ❌, say so plainly and do not claim the install is done.
`/brainiac-install` is safely re-runnable — if the user hits a failure, tell
them to fix the reported error and re-run it; every step above is idempotent
by design (re-running never double-registers the nightly task, never
duplicates a registry entry, never rotates an existing key). Re-running
Step 1 on an already-current install is a fast no-op on every channel (`uv
tool install` / `pipx install` / `pip install --user` are all idempotent).

To pull a NEW Brainiac release later, the user runs `/brainiac-update` (after
`/plugin marketplace update`) — not `/brainiac-install`. This skill is for
first-time setup and registry entries; `/brainiac-update` handles version
skew (channel-aware — it detects which of the four channels above is live
and runs that channel's own upgrade command), the conditional index rebuild,
snapshot republish, and workspace refresh. Point the user there for routine
updates.

<!-- SKILL_VERSION: 0.19.1 (generated by tools/package_clients.py — do not hand-edit) -->
