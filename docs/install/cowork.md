# Cowork — install and first run

**Role:** `vm` (`BRAIN_ROLE=vm`) — **read + draft ONLY.** No writes, no index
rebuild, no signing key, no OS scheduler. This is the one client whose
capability set genuinely differs from the other two — read this whole page
before assuming Cowork can do what the host clients do. Full matrix:
`docs/cutover/client-access-model.md`; the read+draft hard guarantee (code +
tests): `docs/cowork-windows-install.md`.

## 0 — Before you open Cowork: install the runtime into the workspace (HOST side)

Cowork's Linux VM sandbox mounts only the workspace folder — there is no
package install, no HuggingFace network egress, and no host toolchain inside
the VM. So the brain ships **into the workspace** as a self-contained,
arch-matched build, from a HOST machine, before the Cowork session starts:

```bash
# On the HOST (Mac/Windows), from a clone of this repo:
cd profile-a-brain
tools/build_brain_binary.sh                          # builds both Linux ELFs (x86_64 + aarch64), once
tools/cowork_workspace_install.sh <workspace>/vault /path/to/model.onnx
```

This one script (per `docs/operations/cutover-s09-evidence.md` INS-01) lands
the **full** operational layer in one pass, not just the engine:

```
<workspace>/vault/.brain/
├── bin/            per-arch Linux ELFs + a symlink resolved by uname -m
├── model/          bundled e5-small ONNX model (no HF fetch needed in the VM)
├── snapshot/        read-only index.snapshot.sqlite + manifest
├── skills/          the 8 .skill bundles (from dist/cowork-skills/, built if absent)
└── routines/        routines/manifest.json + the Cowork registrar paste-prompt + a brain-init report
```

...and it also runs `brain init --full` as the VM client (`BRAIN_ROLE=vm`),
so the `overlay/` layer is scaffolded and validated with **no host
mutation** before you ever open the Cowork session.

Idempotent — re-run it any time to refresh binaries and republish the
snapshot. The Markdown vault is always the source of truth; `.brain/` is
always rebuildable from it.

## 1 — Per-session bootstrap (paste once per Cowork session)

The VM filesystem persists across a session's lifetime, but the shell
environment does **not** — re-export this at the start of every Cowork
session (or source `tools/cowork_session_bootstrap.sh`):

```bash
export BRAIN_VAULT="$PWD/vault"
export BRAIN_ROLE=vm                                   # read + draft only — hard guarantee
export BRAIN_RUNTIME_DIR="$BRAIN_VAULT/.brain"
export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"    # bundled cache, no network fetch
ln -sf "bin/brain-linux-$(uname -m)" "$BRAIN_RUNTIME_DIR/brain"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
brain status                                           # snapshot generation/age + pending-draft count
```

## 2 — Upload the skill bundles (Save-skill flow)

Use the `setup-cowork` skill (`.claude/skills/setup-cowork/SKILL.md`) to walk
through this, or do it directly: upload the 8 `.skill` zips from
`dist/cowork-skills/` via Cowork's Save-skill flow. **Order: kernel first,
extras optional** (mirrors the Claude Code marketplace split in
`docs/operations/cutover-s08-evidence.md` SKL-04):

```
kernel:  kb-curator.skill  promote.skill  vault-ingestion.skill  vault-eval.skill  save-conversation.skill
extras:  curation.skill  improve.skill  task-registrar.skill
```

## 3 — Register the on-invoke Cowork triggers (paste-ready prompt)

Paste `docs/operations/cowork-task-registrar-prompt.md` (or the file written
by `--save-cowork-prompt`, or the one already staged at
`<workspace>/vault/.brain/routines/`) into a Cowork chat session that has the
scheduled-tasks MCP tools. It registers exactly **3 poke-only triggers** —
`brain-promotion-scan`, `brain-autoresearch-cascade`,
`brain-ingestion-digest-weekly` — following a strict list → create-if-absent
/ update-if-present → **never delete** sequence, and never sets a cron
expression on any of them:

```bash
# Regenerate the prompt fresh at any time (read-only, no mutation):
cd profile-a-brain
python3 scripts/register_tasks.py --dry-run --client cowork --save-cowork-prompt /tmp/cowork-prompt.txt
```

**Verify after pasting:** re-run `list_scheduled_tasks` and confirm all 3
triggers appear with no schedule expression (heed the
[#29022](https://github.com/anthropics/claude-code/issues/29022) caveat —
`create_scheduled_task` sometimes silently no-ops; fall back to the Cowork
Schedule UI if a trigger didn't take). Confirm the VM OS-scheduled count
stays at **0** — that is `docs/cutover/persistence-budget.md`'s locked
budget, and this registration must never add a cron entry on the VM side.

## Remember: Cowork is read+draft — its tasks are brief/digest, not host maintain

This is the single most important thing to internalize about the Cowork
client, and the reason its onboarding differs structurally from Claude Code
CLI / Codex:

- **No `brain-nightly` here.** The one sanctioned OS-scheduled task lives
  only on the **host** (`launchd` / Task Scheduler) — the VM has no
  scheduler and registers nothing autonomous, ever.
- **The 3 Cowork triggers are poke-only** (fired by a human, by name, via
  the Cowork "run now" equivalent) — convenience aliases for retyping a
  prompt, not cron entries.
- **Verbs available to `BRAIN_ROLE=vm`:** `search`, `hybrid-search`, `grep`,
  `bases-query`, `graph-expand`, `get`, `read`, `recent`, `status`,
  `draft-capture`, `capture`, `brief`, `digest`, `init` — every host-broker
  write/maintenance verb (`write`, `rebuild`, `sync`, `snapshot`, `project`,
  `verify-audit`, `anchor`, `verify-anchor`, `backup`, `restore`) is refused
  **before** the index is even opened (`{"error": "role_forbidden"}`, exit
  4) — this is enforced in code, not just documented, and proven by
  `tests/test_s06_integration.py`.
- **Captures are unsigned DRAFTS.** `brain draft-capture` stages a draft in
  `capture-inbox/`; it is drained + Ed25519-signed only by the next **host**
  `brain sync` (which the host's `brain-nightly` task already runs daily).
  There is no capture daemon and no VM-side signing key — ever.
- **Snapshot staleness is visible, not silent.** `brain status` on the VM
  reports the snapshot's generation and age, so a session can tell whether
  its read-only view is fresh or waiting on the next host drain.

## Verify

```bash
brain status                          # snapshot gen/age, pending drafts
brain search "<something>" --json     # egress-gated read
brain write foo.md                    # MUST fail: role_forbidden, exit 4
```

## Cross-references

- `docs/cowork-windows-install.md` — the full Cowork build/runtime spec (5 load-bearing rules, the capture loop, the install/refresh commands)
- `docs/cutover/client-access-model.md` — full access matrix (all clients)
- `docs/operations/cowork-task-registrar-prompt.md` — the exact paste-ready prompt
- `docs/cutover/persistence-budget.md` — THE LOCK (1 host, 0 VM)
- `.claude/skills/setup-cowork/SKILL.md` — the guided walkthrough of steps 1–3 above
- `.claude/skills/task-registrar/SKILL.md` — the registrar that generates the paste-prompt
