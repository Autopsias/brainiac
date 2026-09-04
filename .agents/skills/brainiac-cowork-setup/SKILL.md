---
name: brainiac-cowork-setup
description: Stage the Brainiac read+draft engine (model + zero-install runtime) into a Cowork workspace, register the vault's nightly host maintenance task, record it in the workspace registry, and print the exact folder/session-prompt/skills instructions to finish in Cowork. Use when the user says "set up cowork", "/brainiac-cowork-setup", "add brainiac to a new cowork workspace", or answers yes to the Cowork question at the end of /brainiac-install.
---

# /brainiac-cowork-setup

Runs entirely on the HOST (never inside Cowork's sandbox — nothing installed
there survives). Ask **TWO** questions up front and then execute without
further prompts except an explicit human gate: "Which folder is your Cowork
workspace?" (the folder Cowork will mount) and "Where does the vault live?"
(the directory holding `brain/ raw/ overlay/`).

Both questions matter because the vault and the workspace are two different
places: a vault one level inside the attached folder puts every note body on
the Cowork mount (VULN-3385). Keep them separate even when today they happen
to be the same tree.

This skill is standalone — reusable any time, not just as a branch of
`/brainiac-install`. (A vault can also be born FROM a Cowork session with no
host session at all: `brain --role vm provision-request` stages a marker the
host's hourly nightly completes — PRV-10, see AGENTS.md §6. This skill is
the host-side path for wiring an existing vault into a workspace.) It assumes
the host install already happened (`brain --version` succeeds); if `brain`
is not on PATH, tell the user to run `/brainiac-install` first and stop.

## Step 0 — split-brain guard (run BEFORE wiring anything)

Check whether `<workspace>` itself was previously scaffolded as a vault
(top-level `brain/`, `raw/`, or `overlay/` directly inside it, or a
`target: host` entry in `~/.brainiac/workspaces.json` whose `vault_path` is
`<workspace>` itself rather than `<workspace>/vault`). If so, **STOP and
reconcile before wiring `<workspace>/vault`** — otherwise you produce two
divergent vaults (one at the workspace root, one at `/vault`) with the
registry and the nightly task pointing at different ones. Reconcile by
moving the vault content (`brain/ raw/ overlay/`) into `<workspace>/vault/`
and fixing the registry's host entry to `<workspace>/vault`, confirming with
the user first. Only then continue.

## Step 1 — the one command

```bash
brain provision-local "<vault>" --workspace "<workspace>"
```

This single host-broker command replaces the old six-step hand-paste
sequence (`brain init --full`, `stage_model.py` + `cowork_workspace_install.sh`
by hand, a hand-typed Python registry-upsert snippet for the workspace registry, a
second registry write for the `cowork-vm` row, the Claude Desktop MCP entry,
and the deliverables sweep-dir wire) — `brain provision-local` runs all six as
one check-then-act pass and reports one status per wire (`already` / `done` /
`skipped` / `pending` / `failed`). Re-running it is safe: it changes nothing on
a fully wired vault and repairs only what's missing on a half-wired one.

**First vault on this machine (no model staged anywhere yet)?** Pass
`--model-dir`, or wire 2 (the Cowork staging) fails until you do:

```bash
python3 packaging/stage_model.py --repo Xenova/bge-m3 --out /tmp/bge-m3-int8 \
  --patterns "onnx/model_int8.onnx" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "config.json"
brain provision-local "<vault>" --workspace "<workspace>" --model-dir /tmp/bge-m3-int8
```

A machine that already has at least one Cowork vault wired needs no
`--model-dir` — `provision-local` finds an existing staged model
automatically.

Run with `--json` and read each wire's `status` for the report below. Any
`failed` wire names what blocked it and, once wire 6 (the deliverables sweep)
needs a launchd reload, the exact `launchctl bootout … bootstrap … print`
line to run — that line is the ONE manual step this command cannot take for
you (macOS will not let an unattended process reload another job into the
login domain). `bootstrap` alone can answer `Load failed: 5` on a job that is
registered but stopped — that's why the printed line runs `bootout` first.

## Final report — mandatory template

```
Brainiac Cowork setup report
-----------------------------
✅/❌/⏳ wire 1 — init (scaffold, audit key, nightly registration)
✅/❌/⏳ wire 2 — Cowork workspace staging (engine + model + skills + snapshot)
✅/❌/⏳ wire 3 — host registry row
✅/❌/⏳ wire 4 — cowork-vm registry row
✅/❌/⏳ wire 5 — Claude Desktop brain-mcp entry
✅/❌/⏳ wire 6 — deliverables sweep dir (nightly plist)
```

`already` and `done` are both ✅. `skipped` (wire 6 off macOS) is ✅ with the
reported reason. `pending` or `failed` is ❌/⏳ — say which wire, quote its
`detail`, and if it carries a `reload` line, print that line verbatim for the
user to run themselves. If ANY wire is ❌, say so plainly and do not claim
setup is done.

**If wire 5 (the Claude Desktop `brain-mcp` entry) keeps reverting** — Claude
Desktop re-writes its own config file on every launch and can drop an entry
`provision-local` just added — the documented escape hatch is to lock the
file against Desktop's own rewrite after wiring it once:
`chflags uchg ~/Library/Application\ Support/Claude/claude_desktop_config.json`
(macOS). Tell the user this trades away Desktop's own config UI for that
entry until they `chflags nouchg` it back.

Then print, in the chat, exactly these three things:

**(a) The folder to add in Cowork** — `<workspace>` (the folder Cowork must
mount). The staged runtime lives inside it; the vault does not, unless the
two are still co-located.

**(b) The full contents** of `<workspace>/vault/.brain/routines/cowork-session-prompt.md`
— paste the entire file verbatim so the user can copy it straight from this
conversation into the Cowork project's custom instructions (or as their
first message there).

**(c) Skills — default vs optional, do not blur the two (ADR-0005 Ruling 4 +
s04 addendum: the Desktop/Cowork plugin store has no scriptable host-side
CLI/config/import, so the host can only guarantee the staged path):**
- **Default (wire 2 already did it):** current-version `.skill` zips are
  staged at `<workspace>/vault/.brain/skills/` — upload them via Cowork's
  Save-skill upload flow (kernel first, extras optional — see
  `docs/install/cowork.md` step 2 for the exact order). This is the one path
  a host re-run can always refresh and verify (cw-02); re-running
  `brain provision-local` later keeps it current with zero extra steps.
- **Optional, from inside a live Cowork session:** Customize → Plugins → add
  marketplace `Autopsias/brainiac` → install `brainiac-kernel` (and
  `brainiac-extras` if wanted). `Autopsias/brainiac` is public — see
  `docs/adr/0002-cowork-plugin-skill-delivery.md` addendum. Convenient if
  you're already there, but it's a manual click-through the host cannot
  trigger or verify, so it stays optional rather than the thing an update
  depends on.

Mention in one line (don't walk through them) that there are 3 optional
poke-only Cowork triggers (`brain-promotion-scan`,
`brain-autoresearch-cascade`, `brain-ingestion-digest-weekly`) the user can
register later via the `task-registrar` skill if they want.

The user's only remaining manual work: add the folder in Cowork, paste the
session-prompt block, upload the skill zips, and — only if wire 6 printed a
`reload` line — run that one `launchctl` line themselves. Everything else
above is already done.

<!-- SKILL_VERSION: 0.20.36 (generated by tools/package_clients.py — do not hand-edit) -->
