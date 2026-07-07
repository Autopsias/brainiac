---
name: brainiac-cowork-setup
description: Stage the Brainiac read+draft engine (model + zero-install runtime) into a Cowork workspace, register the vault's nightly host maintenance task, record it in the workspace registry, and print the exact folder/session-prompt/skills instructions to finish in Cowork. Use when the user says "set up cowork", "/brainiac-cowork-setup", "add brainiac to a new cowork workspace", or answers yes to the Cowork question at the end of /brainiac-install.
---

# /brainiac-cowork-setup

Runs entirely on the HOST (never inside Cowork's sandbox — nothing installed
there survives). Ask **ONE** question up front and then execute without
further prompts except an explicit human gate: "Which folder is your Cowork
workspace?" (the folder Cowork will mount; the vault lives at
`<workspace>/vault`).

This skill is standalone — reusable any time, not just as a branch of
`/brainiac-install`. It assumes the host install already happened
(`~/brainiac` cloned, `./install.sh` run); if `~/brainiac` doesn't exist, tell
the user to run `/brainiac-install` first and stop.

## Step 0 — split-brain guard (run BEFORE staging anything)

Check whether `<workspace>` itself was previously scaffolded as a vault
(top-level `brain/`, `raw/`, or `overlay/` directly inside it, or a
`target: host` entry in `~/.brainiac/workspaces.json` whose `vault_path` is
`<workspace>` itself rather than `<workspace>/vault`). If so, **STOP and
reconcile before creating `<workspace>/vault`** — otherwise you produce two
divergent vaults (one at the workspace root, one at `/vault`) with the
registry and the nightly task pointing at different ones. Reconcile by
moving the vault content (`brain/ raw/ overlay/`) into `<workspace>/vault/`
and fixing the registry's host entry to `<workspace>/vault`, confirming with
the user first. Only then continue.

## Step 1 — stage the embedding model

```bash
cd "$HOME/brainiac"
python3 packaging/stage_model.py --repo Xenova/multilingual-e5-small --out /tmp/e5 \
  --patterns "onnx/model.onnx" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "config.json"
```

Report ✅/❌ on exit code.

## Step 2 — install the zero-install runtime into the workspace

```bash
tools/cowork_workspace_install.sh "<workspace>/vault" /tmp/e5
```

No Docker, no compilers — this stages the pure-Python engine + a `brain` shim
that runs on the sandbox's own `python3`. Report ✅/❌.

## Step 3 — register the vault's nightly host maintenance task

```bash
BRAIN_VAULT="<workspace>/vault" brain init --full --apply
```

This registers the nightly task — a **per-vault** launchd label
`com.brainiac.nightly.<id>` (Windows: `brain-daily-brief-<id>`), the ONE
sanctioned host scheduled task per vault (AGENTS.md §6). Each vault owns its
own plist, so registering this vault never disturbs another's drain (the
pre-0.9.0 shared-label repoint hazard is gone; the installer retires the
legacy `com.profile-a-brain.daily-brief` plist on first per-vault run).
**Report its status explicitly**:
✅ registered (show label + schedule), ✅ already present (idempotent
re-run — not a failure), or ❌ with the exact reported reason (e.g. missing
signing key) and what the user must decide next — never silently leave
maintenance unregistered.

## Step 4 — workspace registry

Import the shared helper (never hand-write the file or reinvent the lock):

```python
import sys
sys.path.insert(0, "<repo>/tools")
from workspace_registry import upsert_entry
upsert_entry(vault_path="<workspace>/vault", workspace_path="<workspace>",
             target="cowork-vm", model_dir="/tmp/e5")
```

`upsert_entry` keys on `(host, arch, target, realpath(vault_path),
realpath(workspace_path))` and stamps host/arch itself — re-running this
skill against the same workspace updates the existing entry rather than
duplicating it.

## Final report — mandatory template

```
Brainiac Cowork setup report
-----------------------------
✅/❌ model staged (/tmp/e5)
✅/❌ workspace install (tools/cowork_workspace_install.sh)
✅/❌ brain-nightly registered — label: com.brainiac.nightly.<id> (per-vault), vault: <workspace>/vault, schedule: <time>
✅/❌ workspace registry: recorded <vault_path> (target: cowork-vm)
```

If ANY line is ❌, say so plainly and do not claim setup is done.

Then print, in the chat, exactly these three things:

**(a) The folder to add in Cowork** — `<workspace>` (the folder Cowork must
mount; the vault + staged runtime live inside it at `<workspace>/vault`).

**(b) The full contents** of
`<workspace>/vault/.brain/routines/cowork-session-prompt.md` — paste the
entire file verbatim so the user can copy it straight from this conversation
into the Cowork project's custom instructions (or as their first message
there).

**(c) Skills — default vs optional, do not blur the two (ADR-0005 Ruling 4 +
s04 addendum: the Desktop/Cowork plugin store has no scriptable host-side
CLI/config/import, so the host can only guarantee the staged path):**
- **Default (this command already did it):** the install just staged
  current-version `.skill` zips in `<workspace>/vault/.brain/skills/` —
  upload them via Cowork's Save-skill upload flow (kernel first, extras
  optional — see `docs/install/cowork.md` step 2 for the exact order). This
  is the one path a host re-run can always refresh and verify (cw-02);
  re-running this skill later keeps it current with zero extra steps.
- **Optional, from inside a live Cowork session:** Customize → Plugins → add
  marketplace `Autopsias/brainiac` → install `profile-a-kernel` (and
  `profile-a-extras` if wanted). `Autopsias/brainiac` is public, and this
  sync was verified live 2026-07-04 — see
  `docs/adr/0002-cowork-plugin-skill-delivery.md` addendum. Convenient if
  you're already there, but it's a manual click-through the host cannot
  trigger or verify, so it stays optional rather than the thing an update
  depends on.

Mention in one line (don't walk through them) that there are 3 optional
poke-only Cowork triggers (`brain-promotion-scan`,
`brain-autoresearch-cascade`, `brain-ingestion-digest-weekly`) the user can
register later via the `task-registrar` skill if they want.

The user's only remaining manual work: add the folder in Cowork, paste the
session-prompt block, and upload the skill zips. Everything else above is
already done.
