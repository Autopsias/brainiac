---
name: setup-cowork
description: "Onboard this Profile A brain-substrate vault into Claude's Cowork environment, which cannot read a repo folder or auto-load .claude/skills/ the way Claude Code and Codex do — skills only arrive one at a time through Cowork's Save-skill upload flow. Walks through: confirming the Cowork-Windows workspace install (tools/cowork_workspace_install.sh) has placed the brain binaries + bundled embed model + first snapshot into vault/.brain/, which of the dist/cowork-skills/*.skill zips to upload and in what order, the per-session PATH/role bootstrap one-liner (the VM filesystem persists but the shell environment does not), and the read+draft-only VM role constraint (BRAIN_ROLE=vm — no write/sign/WAL; drafts need a host `brain sync` to become real notes). Triggers: 'set up Cowork', 'set up cowork for this vault', 'onboard this vault to Cowork', 'how do I get these skills into Cowork', 'cowork setup', 'upload the skills to Cowork', 'get the brain skills working in Cowork', 'I'm starting a new Cowork session for this vault', or any question about what to upload/paste/export for a Cowork session against this repo. Use this even if the user only asks about ONE piece (e.g. just 'what do I paste for Cowork') — the checklist at the end catches missing prerequisites they may not have mentioned. Not for Claude Code or Codex setup (those auto-load from .claude/skills/ and .agents/skills/ on clone, no upload needed) and not for the underlying binary build itself (that's docs/cowork-windows-install.md's own install/refresh section, which this skill points at rather than duplicates)."
---

# setup-cowork

Cowork is the one client in this repo's three-client packaging (`AGENTS.md`
"Where the kernel skills live per client", s08 SKL-02/03/04) that **cannot**
read this repo's folders directly. Claude Code auto-loads `.claude/skills/`
on clone; Codex auto-loads the mirrored `.agents/skills/`; Cowork gets
neither — a human has to upload each skill's `.skill` zip through Cowork's
own Save-skill UI, one at a time. This skill is the checklist that makes
that upload step (and the runtime it depends on) go smoothly instead of
producing a Cowork session with skills but no working `brain` binary
underneath them, or vice versa.

Walk through the four steps below in order. Don't skip straight to "upload
the zips" — a skill with no `brain` binary in the workspace is just
instructions with nothing to execute against, and the most common failure
mode is a user who uploads skills before running the workspace install.

## Step 1 — confirm the Cowork-Windows workspace install has run

The skills call the `brain` CLI. For that to exist inside the Cowork VM, the
**workspace install** must have already placed the runtime into
`vault/.brain/` — arch-matched Linux binaries (`brain-linux-x86_64` /
`brain-linux-aarch64`), the bundled Arctic-embed model (so the VM never
needs a HuggingFace fetch it isn't allowed), and a first published snapshot.
Full layout and rules: `docs/cowork-windows-install.md`.

Ask (or check) whether this has run. If not, it's a HOST-side step, run
**before** anything below:

```bash
# On the HOST (not inside Cowork):
tools/build_brain_binary.sh                       # → both Linux ELFs
tools/cowork_workspace_install.sh <workspace>/vault /path/to/model.onnx
```

`cowork_workspace_install.sh` is idempotent — re-running it refreshes the
binaries and republishes the snapshot, so it's always safe to re-run if
unsure. If the user is asking this skill's question from *inside* a Cowork
session, they can't run this themselves (Cowork has no access to the host
filesystem outside the mounted workspace) — tell them plainly it's a
one-time host-side setup step and point them at the doc.

## Step 2 — upload the skill zips, kernel first

Once the workspace runtime exists, upload the skills via Cowork's
**Save-skill** button, one `.skill` zip at a time, from
`dist/cowork-skills/` in this repo. Each zip already contains
`<name>/SKILL.md` at its root — that's what Cowork expects.

Upload in this order — kernel first (always-useful daily skills), extras
after (optional maintenance/admin):

**Kernel (upload these first):**
1. `dist/cowork-skills/kb-curator.skill`
2. `dist/cowork-skills/promote.skill`
3. `dist/cowork-skills/vault-ingestion.skill`
4. `dist/cowork-skills/vault-eval.skill`
5. `dist/cowork-skills/save-conversation.skill`
6. `dist/cowork-skills/voice.skill`

**Extras (optional — upload if the user wants maintenance/admin skills too):**
7. `dist/cowork-skills/curation.skill`
8. `dist/cowork-skills/improve.skill`
9. `dist/cowork-skills/task-registrar.skill`
10. `dist/cowork-skills/autoresearch.skill`

If a zip is missing or looks stale, it's built (and re-validated) by:

```bash
python3 tools/package_clients.py
```

run from the repo root on the host — this is the single source-of-truth
sync script for all three clients (see `AGENTS.md`), so re-running it after
any kernel-skill edit refreshes `dist/cowork-skills/` along with the Codex
mirror and the Claude Code marketplace plugins.

## Step 3 — the per-session bootstrap (run this EVERY Cowork session)

The Cowork VM's filesystem persists across sessions, but its shell
environment does not — `PATH`, `BRAIN_VAULT`, `BRAIN_ROLE`, etc. reset each
time. Paste this once at the start of every session, before using any
uploaded skill:

```bash
export BRAIN_VAULT="$PWD/vault"
export BRAIN_ROLE=vm                                   # read + draft only
export BRAIN_RUNTIME_DIR="$BRAIN_VAULT/.brain"
export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"    # bundled cache, no HF fetch
ln -sf "bin/brain-linux-$(uname -m)" "$BRAIN_RUNTIME_DIR/brain"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
brain status                                           # snapshot gen/age + pending drafts
```

A working `brain status` that prints a snapshot generation + age is the
signal the session is ready. If it errors, re-check Step 1 (the workspace
install) before re-checking this step.

## Step 4 — the VM role constraint (read this before the user is surprised)

`BRAIN_ROLE=vm` is a **hard guarantee**, not a suggestion: the Cowork VM can
only `search`, `get`, `recent`, and `draft-capture` — it can never `write`,
sign the audit chain, or open the index in write mode. This is enforced in
code, not just documented (`tests/test_integration.py`).

Practically, this means every uploaded kernel skill that captures or writes
something (`kb-curator`, `promote`, `vault-ingestion`, `save-conversation`)
will **stage a draft** on the VM, not commit a real note. The full loop —
VM draft → host `brain sync` (drains + Ed25519-signs + indexes) → host
republishes the snapshot → the VM can now retrieve it — is documented in
`docs/cowork-windows-install.md`'s "Capture loop" section. Tell the user:
drafts land in `vault/.brain/capture-inbox/` and become real, retrievable
notes only after a host-side `brain sync`. There is no capture daemon; the
host drains on invoke, and the guaranteed daily floor is the scheduled
morning-brief task (per the same doc) — so an unattended draft is picked up
by the next brief at the latest, but a user who wants it sooner should say
so and run `brain sync` on the host directly.

## Self-check — confirm before declaring the session ready

Before telling the user Cowork is set up, walk this checklist explicitly
(don't just assume — ask or verify each line):

- [ ] **Workspace installed** — `vault/.brain/` exists with binaries + model
      + a snapshot (Step 1). If unsure, `brain status` in Step 3 is the
      fastest proof: it fails loudly if the runtime isn't there.
- [ ] **Skills uploaded** — at minimum the 6 kernel `.skill` zips are saved
      into this Cowork account/workspace (Step 2).
- [ ] **Bootstrap run this session** — the Step 3 one-liner was pasted in
      *this* Cowork session (it does not persist across sessions).
- [ ] **`brain status` returns a snapshot generation** — the concrete,
      checkable proof that the VM can read the second brain.
- [ ] **Contract auto-loaded** — the workspace root has a `CLAUDE.md`
      (staged by the install/refresh script), and sending the message
      `contract?` in the session answers
      `[brain contract loaded] [contract inlined]`. Both markers ⇒ the
      conventions contract (retrieval doctrine, VM role limits) is
      always-on; no markers ⇒ Cowork didn't auto-load it — fall back to
      pasting `vault/.brain/routines/cowork-session-prompt.md` (custom
      instructions or first message), which points the agent at
      `vault/.brain/AGENTS.md`.

If any box is unchecked, point the user back at the corresponding step
rather than declaring the setup done — a half-finished Cowork setup (skills
uploaded but no runtime, or runtime present but bootstrap not run this
session) is the most common source of "the skill doesn't do anything"
confusion.
