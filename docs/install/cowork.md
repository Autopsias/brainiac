# Cowork — install and first run

> **Entry point / platform picker: [`./README.md`](./README.md)** — Cowork
> always requires the host install (Path A) first; see the picker if you
> haven't done that yet.

**Role:** `vm` (`BRAIN_ROLE=vm`) — **read + draft ONLY.** No writes, no index
rebuild, no signing key, no OS scheduler. This is the one client whose
capability set genuinely differs from the other two — read this whole page
before assuming Cowork can do what the host clients do. Full matrix:
`AGENTS.md` §6 (Host / VM trust split); the read+draft hard guarantee (code +
tests): `docs/cowork-windows-install.md`.

## Quickstart — the whole thing in 6 steps (plain language)

Cowork can't install anything itself — its VM only sees the folder you give
it. So the engine is staged **from your computer first**, then Cowork just
opens the folder. In order:

1. **On your computer, in Claude Code** (any directory, with the
   `brainiac-manager` plugin installed — see [`ai-install.md`](./ai-install.md)):
   run **`/brainiac-cowork-setup`**. It asks ONE question — which folder will
   be your Cowork workspace — then stages everything into it (engine, search
   model, read-only snapshot, session prompt) and registers the nightly
   maintenance task. It ends by printing the exact things to do in Cowork.
2. **In Claude Desktop**: open **Cowork** and add that same folder as the
   project folder.
3. **Install the skills** (one-time): step 1 already staged current-version
   `.skill` bundles at `<workspace>/vault/.brain/skills/` — upload them via
   Cowork's **Save-skill** flow (kernel first; extras optional, §2). Prefer
   the Plugins tab instead? **Customize → Plugins →** add
   `Autopsias/brainiac` → install **Brainiac — Kernel Skills** (and
   **Extras**) works too and is documented as an optional alternative (§2) —
   just don't install Brainiac Manager in Cowork either way; its skills
   mutate the host and are useless in the VM.
4. **Paste the session prompt**: `/brainiac-cowork-setup` printed the full
   contents of the staged `cowork-session-prompt.md` — paste it into the
   project's instructions (or the first message of each session). Cowork does
   not read AGENTS.md on its own; this prompt is how the agent learns the
   brain exists (§1).
5. **Use it**: in a Cowork session the agent calls `brain --role vm` — search,
   get, recent, and `draft-capture` for new notes. Drafts land in the shared
   folder; your computer's nightly task signs and indexes them, and the next
   snapshot publish makes them searchable in Cowork (see "Remember" below).
6. **Updating later**: on your computer run `/plugin marketplace update` then
   **`/brainiac-update`** — it re-stages every registered workspace, so the
   Cowork folder gets the new engine/prompt/skills automatically.

Everything below is the detail behind those steps.

## 0 — Before you open Cowork: install the runtime into the workspace (HOST side)

Cowork's Linux VM sandbox mounts only the workspace folder, so the brain
ships **into the workspace** from a HOST machine before the Cowork session
starts. Nothing is installed in the VM: the engine is pure Python and runs
straight from the staged source copy via the VM's own `python3` (a `brain`
shim in `.brain/` wraps `python3 -m brain.cli`). No Docker, no compilers.

**Easiest:** if you already have the `brainiac-manager` plugin (see
[`ai-install.md`](./ai-install.md)), just run `/brainiac-cowork-setup` and
answer which folder is your workspace — it does everything below in one
shot and prints the exact instructions to finish in Cowork.

Doing it by hand instead:

```bash
# On the HOST (Mac/Windows), from a clone of this repo:
cd brainiac
python3 packaging/stage_model.py --repo Xenova/multilingual-e5-small --out /tmp/e5 \
  --patterns "onnx/model.onnx" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "config.json"
tools/cowork_workspace_install.sh <workspace>/vault /tmp/e5
```

(Frozen Linux ELFs via `tools/build_brain_binary.sh` remain an optional
fallback for locked-down VMs without `python3` — most users never need
them.)

This one script lands the **full** operational layer in one pass, not just
the engine:

```
<workspace>/vault/.brain/
├── engine/          staged pure-Python engine source (what the `brain` shim runs)
├── vendor/<arch>/   per-architecture semantic deps (onnxruntime, tokenizers, ...)
├── bin/             per-arch frozen Linux ELFs — OPTIONAL fallback for VMs without python3
├── model/           bundled e5-small ONNX model (no HF fetch needed in the VM)
├── snapshot/        read-only index.snapshot.sqlite + manifest
├── skills/          the 10 .skill bundles, REBUILT at the current version every run
└── routines/        routines/manifest.json + the Cowork registrar paste-prompt + a brain-init report
```

...and it also runs `brain init --full` as the VM client (`BRAIN_ROLE=vm`),
so the `overlay/` layer is scaffolded and validated with **no host
mutation** before you ever open the Cowork session.

**One command, both artifacts current (cw-02).** Every run of this script
rebuilds `dist/cowork-skills/*.skill` from the checkout's current version —
never just "if the directory happens to be empty" — and aborts if the
freshly-staged skill bundle's version doesn't match the freshly-staged
engine's version stamp. So re-running this one script after a
`git pull`/version bump refreshes the engine **and** the skills together;
there is no separate "now go reinstall the skills" step. `brain doctor`
(below) reports the staged skill-bundle version per workspace, so a stale
Cowork skill set shows up as a visible `⚠️`, not a silent gap.

Idempotent — re-run it any time to refresh binaries, skills, and republish
the snapshot. The Markdown vault is always the source of truth; `.brain/` is
always rebuildable from it.

## 1 — Teach the agent (project instructions) + per-session bootstrap

Cowork **auto-loads a workspace-root `CLAUDE.md`** at session start (same
loader as Claude Code, but without `@import` expansion — so the installer
stages the full conventions contract **inlined** there). Verify anytime by
sending the message `contract?` — a healthy session answers
`[brain contract loaded] [contract inlined]`.

If the probe gets no markers (older Cowork build, or a workspace staged
before this change), use the **fallback channel**: the installer also stages
`<workspace>/vault/.brain/routines/cowork-session-prompt.md` — a prompt
block that bootstraps the env AND points the agent at the contract. Put it
in the Claude Desktop project's custom instructions (once per project) or
paste it as the first message of each session. Source doc:
`docs/install/cowork-session-prompt.md`.

The env part, for reference — the VM filesystem persists across a session's
lifetime, but the shell environment does **not**, so this runs at the start
of every session (the session prompt contains it):

```bash
export BRAIN_VAULT="$PWD/vault"
export BRAIN_ROLE=vm                                   # read + draft only — hard guarantee
export BRAIN_RUNTIME_DIR="$BRAIN_VAULT/.brain"
export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"    # bundled cache, no network fetch
ln -sf "bin/brain-linux-$(uname -m)" "$BRAIN_RUNTIME_DIR/brain"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
brain status                                           # snapshot generation/age + pending-draft count
```

## 2 — Get the skills into Cowork: one host command (DEFAULT), Plugins tab (OPTIONAL)

**Default: the one-command host path (cw-01/cw-02).** Step 0 above already
staged current-version `.skill` bundles at `<workspace>/vault/.brain/skills/`
— this is the path the host can fully guarantee and re-verify on every run
(ADR-0005 Ruling 4 + the s04 empirical addendum: the Claude Desktop / Cowork
plugin store has **no supported CLI, config, or import** a host script can
drive, so a staged filesystem artifact is the only thing a host command can
promise stays current). Upload them via Cowork's Save-skill flow — use the
`setup-cowork` skill (`.claude/skills/setup-cowork/SKILL.md`) to walk through
this, or do it directly. **Order: kernel first, extras optional** (mirrors
the Claude Code marketplace split in `docs/operations/cutover-s08-evidence.md`):

```
kernel:  kb-curator.skill  promote.skill  vault-ingestion.skill  vault-eval.skill  save-conversation.skill  voice.skill
extras:  curation.skill  improve.skill  task-registrar.skill  autoresearch.skill
```

**Optional: Plugins tab, from inside a live Cowork session.**
`Autopsias/brainiac` is public (flipped 2026-07-04), so Cowork's Customize →
Plugins tab can sync the marketplace directly — verified live the same day
(all three plugins visible: "Brainiac Manager — host lifecycle", "Brainiac
— Kernel Skills", "Brainiac — Extras"). In Cowork: Customize → Plugins →
add marketplace `Autopsias/brainiac` → install `brainiac-kernel` (and
`brainiac-extras` if wanted); `/plugin marketplace update` inside that
session then keeps it current — see the
[ADR-0002 addendum](../adr/0002-cowork-plugin-skill-delivery.md). This is a
genuinely convenient path **when you're already in the session**, but it's a
click-through a human drives, not something `tools/cowork_workspace_install.sh`
can trigger or verify from the host — that's exactly why it stays optional
rather than becoming what a Cowork update depends on. Use it if you like the
one-time setup; skip it and the staged zips above are still enough on their
own, every time.

**Keeping the Plugins-tab install current later:** if you did use this path,
`brain doctor` on the host can tell you when it drifts (`Desktop/Cowork
plugin store (<plugin>)` rows go `manual-required` with an installed-vs-SSOT
detail) but it cannot fix it — the Desktop store has no host-reachable CLI.
Refreshing it is a **Cowork-session-only** step: `/brainiac-update`'s "Cowork
skill refresh" section drives `/skill-creator` to repackage the stale
skill(s) and present them for "Save and Replace", then re-checks `brain
doctor` afterward to confirm the click actually took (Cowork's "Save and
Replace" is known to silently no-op sometimes — Anthropic #46844/#46836 —
so never trust the click alone).

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
stays at **0** — that is `routines/manifest.json`'s locked
budget (`locked_counts.vm_os_scheduled`), and this registration must never
add a cron entry on the VM side.

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
  `tests/test_integration.py`.
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

On the **host**, `brain doctor` reports the staged engine AND staged
skill-bundle version for every registered Cowork workspace — a stale skill
set after a version bump shows up as `⚠️ stale`, telling you to re-run
`tools/cowork_workspace_install.sh` (or `/brainiac-update`) rather than
staying silently out of date.

## Cross-references

- `docs/adr/0005-update-versioning-ux.md` Ruling 4 + s04 addendum — why the
  staged `.skill` path is canonical/host-guaranteed and the Desktop Plugins
  tab is documented as optional (empirical: the Desktop/Cowork plugin store
  has no scriptable host-side CLI/config/import)
- `docs/cowork-windows-install.md` — the full Cowork build/runtime spec (5 load-bearing rules, the capture loop, the install/refresh commands)
- `AGENTS.md` §6 — full access matrix (all clients)
- `docs/operations/cowork-task-registrar-prompt.md` — the exact paste-ready prompt
- `routines/manifest.json` — THE LOCK, `locked_counts` (1 host, 0 VM)
- `.claude/skills/setup-cowork/SKILL.md` — the guided walkthrough of steps 1–3 above
- `.claude/skills/task-registrar/SKILL.md` — the registrar that generates the paste-prompt
