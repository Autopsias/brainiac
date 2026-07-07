# New owner — the short version

You've installed Brainiac on your platform (see the
[platform picker](./README.md)). This page is the five-minute
mental model — what actually happens, what runs where, and the one thing
that still doesn't work. (Unfamiliar term? `docs/glossary.md` has one-line
definitions for PARA, MNPI, egress gate, Cowork, host-broker, overlay, etc.)

## `brain init` — what it actually does

`brain init --full` is the one first-run command every client (host or VM)
runs. It never opens the index or constructs `BrainCore` — it's pure
filesystem + subprocess, so it works before an index even exists. It does
three things, in order:

1. **Detects the client** from the trust role (`host` → Claude Code CLI /
   Codex; `vm` → Cowork).
2. **Scaffolds `overlay/`** — fills only the *empty* personalization
   categories from the shipped template; a category you've already filled is
   never clobbered. Then validates the shape.
3. **Registers scheduled tasks** for that client — host registers the one
   sanctioned OS task directly (or dry-run probes it); Cowork/VM only ever
   prints a paste-ready, poke-only prompt (never mutates anything itself —
   see the host/VM split below).

Bare `brain init` (no `--full`) still supports the older, narrower
`--validate-overlay` slice; `--full` is the one to run for a genuinely new
install.

## The personalization overlay

`overlay/{voice, brand, keywords, people}/` is the layer that makes the
substrate *yours* — the generic engine plus your specific voice, brand
language, keyword glossary, and roster of people. It lives at `<vault>/overlay/`
(a sibling of `vault/raw/` and `vault/brain/`), separate from the kernel
skills (which never know whose vault they're running against). One starting
point ships in the repo:

- `overlay/template/` — empty starter scaffold for a brand-new owner, so you
  can see the expected shape before writing your own.

Full schema + how-to: `overlay/README.md`.

## The host/VM split — what runs where

This is the single most load-bearing fact about the whole system. There is
**no plugin, no daemon, no always-on server** — every client is either
`host` (full capability) or `vm` (read + draft only, Cowork specifically).

| | Host (Claude Code CLI, Codex, Desktop Code tab, Gemini) | VM (Cowork Linux sandbox) |
|---|---|---|
| Reads (search/get/bases-query/graph-expand/…) | Yes | Yes (from a read-only published snapshot) |
| Writes a note | Yes (`brain write`, audited, Ed25519-signed) | **No** — refused before the index even opens (`role_forbidden`, exit 4) |
| Rebuilds/syncs the index | Yes (`brain rebuild` / `brain sync`) | **No** |
| Resolves a signing key | Yes | **Never** — the VM `BrainCore` never constructs an audit chain at all |
| Owns the one OS-scheduled task | Yes — `brain-nightly` (`launchd`/Task Scheduler), the sole persistence entry the whole system uses | **No** — 0 OS-scheduled entries, locked (`routines/manifest.json` `locked_counts`) |
| Captures a note | Signed, committed immediately | Stages an **unsigned DRAFT** in `capture-inbox/`; drained + signed only by the next host `brain sync` |

`brain-nightly` (host-only) is the irreducible heartbeat: it syncs the index,
drains and signs any VM-staged drafts, republishes the read-only snapshot the
VM reads, and emits the morning brief — all in one date-gated run, so weekly
(health/integrity) and monthly (graphify) cadences ride the same single OS
entry instead of each claiming their own. Full justification:
`routines/manifest.json` `locked_counts`.

## Where the kernel skills + manifest live

| What | Where | Notes |
|---|---|---|
| Kernel skills (canonical copy) | `.claude/skills/<name>/SKILL.md` | 11 skills (10 kernel+extras + `setup-cowork`); this is the one copy you ever hand-edit |
| Codex mirror | `.agents/skills/<name>/SKILL.md` | Auto-synced by `tools/package_clients.py`; identical set minus `setup-cowork` |
| Cowork bundles | `dist/cowork-skills/<name>.skill` | 10 zips, one per kernel+extras skill, ready for Cowork's Save-skill upload |
| Claude Code marketplace (optional) | `.claude-plugin/marketplace.json` + `plugins/profile-a-kernel/` + `plugins/profile-a-extras/` | For installing without cloning the repo |
| The task manifest | `routines/manifest.json` | Single source of truth for every scheduled/on-invoke task across host + VM; consumed by `scripts/register_tasks.py` and the `task-registrar` skill |

**Never hand-edit a mirror.** If a kernel skill changes, edit
`.claude/skills/<name>/SKILL.md` and re-run
`python3 tools/package_clients.py` to re-sync the Codex mirror, the
marketplace plugins, and the Cowork zips in one pass — it also re-validates
every artifact it touches.

## The one thing that still doesn't work: claude.ai web chat

The browser-hosted **claude.ai web chat tab** has no access to `brain` at
all — **this is an open, acknowledged gap**, not an oversight. Neither
access path works for it:

- Native shell `brain …` — impossible, a browser tab can't run shell commands.
- The `brain-mcp` stdio adapter (the one MCP exception, built for the
  Desktop **Chat tab**) — also impossible, because stdio needs a local
  process the client spawns/pipes, and the web app runs in Anthropic's
  cloud, not on your machine.

Closing this gap would require a remote (HTTP/SSE) MCP endpoint — which
reintroduces a network listener and an authn/egress surface the rest of this
design deliberately avoids. That's a separate security + hosting decision,
not something folded into this cutover. **If you only have the claude.ai web
chat available, you do not yet have KB access from it** — use one of the
three clients in this directory instead.

## Starting a second vault?

This page assumes your first vault. Adding a second vault/project on the
same install (same binary, different `$BRAIN_VAULT`) has its own gotchas —
notably the search index and nightly task are NOT vault-scoped by default.
See `docs/install/second-vault.md`.

## Cross-references

- `docs/install/README.md` — pick your client (Claude Code / Codex / Cowork)
- `AGENTS.md` §6 — the full per-client access matrix (this page summarizes it above)
- `overlay/README.md` — the personalization layer's full schema
- `routines/manifest.json` `locked_counts` — the locked 1-host/0-VM scheduling budget
- `brain --help` — every CLI verb, with the VM-allowlist called out
- `AGENTS.md` §7 + `docs/dependency-inventory.md` — the not-yet-written cutover-retirement follow-on plan
