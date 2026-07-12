---
name: brainiac-uninstall
description: Removal path for a Brainiac host install — bootout the shared nightly launchd task (or repoint it when other vaults survive), CHANNEL-AWARE engine removal (uv tool uninstall / pipx uninstall / pip uninstall / the legacy ~/.brainiac/venv), and drop this host's registry entries. Idempotent and safe to re-run if a piece is already gone. Use when the user says "uninstall brainiac", "/brainiac-uninstall", "remove brainiac", or "deregister this vault".
---

# /brainiac-uninstall

Tears down what `/brainiac-install` / `/brainiac-update` persist on **this
host** — nothing else. Safe to re-run: every step checks "already gone?"
before acting.

**NEVER touches:** any vault content, `raw/`, `brain/` notes, the overlay
(`vault/overlay/`), the audit chain, or the **signing key**. This removes
the framework's runtime, not the data. The `~/brainiac` clone and the
`brainiac-manager` plugin itself are left in place — reported as manual
follow-ups, never deleted here (this is a pre-existing checkout the skill
did not create; no unattended `rm -rf` on it).

"Vault content" explicitly includes every ADR-0003 state surface, none of
which any step below reads or writes: `<vault>/.brain/memory/` (session
memory), `<vault>/.brain/maintain-state.json` + `<vault>/.brain/maintain.lock`
(the maintain heartbeat/lock), `<vault>/.brain/brief/` (rendered brief/digest
HTML), `<vault>/.brain/graph/` (graphify build output), `<vault>/inbox/`
+ `inbox/_quarantine/` (the ingestion drop zone), and
`<vault>/.brain/memory/recommendations-open.jsonl` /
`recommendations-log.md`. None of these are ever deleted, moved, or read by
this skill — only `~/.brainiac/` (host-global) and the shared nightly plist
are in scope.

## Modes

- `/brainiac-uninstall` (no args) — full teardown: every registry entry this
  host owns.
- `/brainiac-update --deregister-all` — same effect, invoked via the update
  skill's alias.
- A single vault: ask "which vault?" if not already clear from context, then
  scope every step below to just that entry.

## Step 1 — enumerate what this host owns

```python
import sys
sys.path.insert(0, "$HOME/brainiac/tools")
from workspace_registry import list_entries
import socket, platform

this_host = socket.gethostname()
this_arch = platform.machine()
owned = [e for e in list_entries() if e["host"] == this_host and e["arch"] == this_arch]
```

If `owned` is empty, report "nothing registered for this host — checking
for orphaned launchd labels / venv anyway" and continue (idempotent: a
partial prior failure can leave a venv or a plist without a registry entry).

## Step 2 — the nightly task (PER-VAULT label)

Each registered vault owns its OWN launchd label,
`com.brainiac.nightly.<id>` (`<id>` = the 8-hex per-vault slug; rendered by
`scripts/install-brief-mac.sh`; Windows task `brain-daily-brief-<id>`). Removing
one vault removes only that vault's task — **other vaults are unaffected**, and
there is no shared job to repoint (that hazard existed only under the pre-0.9.0
single-label scheme).

Compute this vault's label from the source of truth and remove just its plist:

```
LABEL="$(BRAIN_VAULT="<vault-being-removed>" "$(dirname "$(command -v brain)")/python3" -c 'import os; from brain.config import nightly_label; print(nightly_label(os.environ["BRAIN_VAULT"]))')"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
```

Also remove the pre-0.9.0 **legacy shared** plist if it still lingers (only on
machines installed before per-vault labels; harmless if already gone):

```
launchctl bootout "gui/$(id -u)/com.profile-a-brain.daily-brief" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.profile-a-brain.daily-brief.plist"
```

`launchctl bootout` on an already-absent label exits non-zero — that's
expected and not a failure; the `|| true` makes this idempotent. Report
`✅ removed`, `✅ already absent`, or `✅ kept (repointed to <vault>)` —
never a bare error.

## Step 3 — remove the engine (channel-aware, PYP-04)

Detect the channel the same way `brain doctor`/`brain update` do — from the
PATH-resolved `brain` binary — and remove via THAT channel's own uninstall,
never a blind `rm -rf`:

```python
import shutil
brain_path = shutil.which("brain")
```

- Path contains `.brainiac/venv` (or `brain` not found and
  `~/.brainiac/venv` exists) → legacy editable checkout:
  ```
  rm -rf "$HOME/.brainiac/venv"
  ```
- Path contains `uv/tools` → PyPI via uv:
  ```
  uv tool uninstall brainiac-cli
  ```
- Path contains `pipx` → PyPI via pipx:
  ```
  pipx uninstall brainiac-cli
  ```
- Otherwise (pip --user) → PyPI via pip:
  ```
  python3 -m pip uninstall -y brainiac-cli
  ```

Always the PACKAGE name (`brainiac-cli`), never the console command
(`brain`) — `uv tool uninstall brain` / `pipx uninstall brain` will not
resolve. If `brain` isn't found on PATH at all AND `~/.brainiac/venv`
doesn't exist, report "already gone" — nothing to remove.

If `--purge-models` was explicitly requested, also:

```
rm -rf "$HOME/.brainiac/models"
```

Otherwise leave `models/` — it's a reusable download cache, not per-install
state. Report ✅/❌ (❌ only on an actual permission error — "already gone"
is a pass) with the detected channel named in the report line.

## Step 4 — drop this host's registry entries

Through the flock helper — never hand-edit the JSON:

```python
import sys
sys.path.insert(0, "$HOME/brainiac/tools")
from workspace_registry import list_entries, REGISTRY_PATH, LOCK_PATH, _locked, _load, _write_atomic
import socket, platform

this_host = socket.gethostname()
this_arch = platform.machine()
with _locked(LOCK_PATH):
    data = _load(REGISTRY_PATH)
    before = len(data["entries"])
    data["entries"] = [e for e in data["entries"] if not (e["host"] == this_host and e["arch"] == this_arch)]
    _write_atomic(REGISTRY_PATH, data)
    removed = before - len(data["entries"])
```

(Single-vault mode: filter additionally on `vault_path`/`workspace_path`
matching the requested vault instead of removing every entry for this
host.)

If the registry is now empty (`entries == []`), remove the whole
`~/.brainiac/` directory (venv already gone in Step 3, so this just clears
`workspaces.json`/`.lock`):

```
[ -f "$HOME/.brainiac/workspaces.json" ] && python3 -c "
import json
d = json.load(open('$HOME/.brainiac/workspaces.json'))
import sys; sys.exit(0 if d.get('entries') else 1)
" && rm -rf "$HOME/.brainiac"
```

## Final report — mandatory template

```
Brainiac uninstall report
------------------------
✅/❌ nightly task: removed | already absent — label: com.brainiac.nightly.<id> (per-vault; + legacy com.profile-a-brain.daily-brief if it lingered)
✅/❌ engine removed — channel: uv tool | pipx | pip --user | editable-checkout (~/.brainiac/venv) | already absent
✅/❌ registry entries dropped: <N> (host: <hostname>)
ℹ️  left in place (delete yourself if you want it gone):
    - ~/brainiac (the code clone)
    - <vault_path> and its data — never touched
    - audit signing key (Keychain/Credential Manager, service:
      profile-a-brain-audit-key) — archive deliberately, don't delete blind
    - to remove the plugin itself: /plugin uninstall brainiac-manager
```

Re-running this skill after a successful teardown reports every step as
"already absent" — that's success, not an error.
