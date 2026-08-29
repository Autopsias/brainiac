---
name: improve
description: Runs a retrospective on a session working against a brain-substrate repo. Reviews the current conversation plus the project's AGENTS.md/CLAUDE.md, installed skills (.claude/skills/), and recent evidence artifacts, then surfaces improvements one at a time via a structured accept/reject/modify question and applies approved edits. Use whenever the user says "improve", "retrospective", "retro", "review my setup", "what should I change", "audit my skills", "what went wrong this session", "tighten my AGENTS.md", or "propose changes so this doesn't happen again". Also trigger on args like "/improve focus on tone". Trigger any time the user expresses friction, says they had to correct Claude repeatedly, or asks Claude to look back over a chat and propose config changes — even if they do not literally say "retrospective". Do NOT trigger for code review, document editing, or knowledge-base maintenance (kb-curator owns brain/ hygiene).
---

# improve — session retrospective (brain-substrate kernel)

<!-- BRAINIAC-DESK-BLOCK v1 -- BYTE-IDENTICAL in all 14 Cowork bundles.
     Canonical copy: docs/operations/cowork-desk-block.md. Pinned by
     tests/test_cowork_skill_bundles.py against the LIVE broker
     (brain.mcp_adapter.TOOLS). Edit the canonical copy and every bundle in one
     commit, never one bundle alone, and never to name a tool the broker does
     not serve. -->

## Cowork (VM leg) — the vault is served by the desk, not by this sandbox

Reach the vault ONLY through the **desk**: the host-run `brainiac` MCP server,
whose tools Claude Desktop announces into this session. Call the tool. Do not
shell out for note content, and do not open a note file.

| the CLI verb you know | the desk tool to call |
|---|---|
| `search`, `hybrid-search` | `search`, `hybrid_search` |
| `get`, `read` | `get`, `read` |
| `recent` | `recent` |
| `dossier` | `dossier` |
| `bases-query` | `bases_query` |
| `grep` | `grep` |
| `graph-expand` | `graph_expand` |
| `vault-languages` | `vault_languages` |
| `diagnose` | `diagnose` |
| `alerts`, `exceptions`, `inbox` | `alerts`, `exceptions`, `inbox` |
| `draft-capture` (stage an unsigned note) | `capture` |

Those fifteen names are the whole desk, and they are the only route to note
CONTENT this session may use. **"Not on the desk" is not the same as "refused
here"**, and conflating the two is how a session talks itself into a workaround:
measured 2026-08-28, nine of the CLI's 22 VM-allowed verbs have no desk tool
(`brief`, `cos-propose`, `digest`, `doctor`, `draft-capture`, `init`,
`mcp-config`, `provision-request`, `status`), and two desk tools (`inbox`,
`vault_languages`) are not VM-allowed on the CLI at all. Of those nine, only
`cos-propose` — the COS ingress nothing on the desk serves — and the staging
probes below are still invoked locally by a shipped bundle, both deliberately.
Every verb that COMMITS or rebuilds — `write`, `sync`, `maintain`, `rebuild`,
`ingest`, `curate`, `health`, `integrity`, `verify-audit`, `graph-report` — is a
HOST-broker privilege and is not available here at all. **Unless a line is
explicitly marked as a Cowork instruction, a shell command anywhere else in this
skill is a HOST-lane instruction**, correct on the owner's Mac and not this
session's route.

**Do NOT bootstrap a vault path, do NOT export one onto `PATH`, do NOT hunt for
a staged binary to retry with, and do NOT open a note under `brain/` or `raw/`
with `cat`, `ls`, `Read` or a glob.** That is a RULE, and until the vault
actually moves off the mount it is the only thing standing between this session
and the bypass this whole change exists to close. Stated exactly, because the
two halves are true at different times:

- **While a published snapshot is still on the mount** (every workspace
  installed before the cutover), a staged engine CAN answer a local retrieval
  verb from `<vault>/.brain/snapshot/` — measured 2026-08-28: a local `search`
  at role `vm` returned the note body and exit 0 with the host index deleted,
  because the
  snapshot path is independent of it. Content that arrives that way skipped the
  desk, and so skipped the classification egress record the desk writes. A read
  that works is not a read you were allowed to make.
- **Once the vault is off the mount**, the local retrieval verbs fail rather
  than returning less — measured 2026-08-28: `search`, `get`, `recent` and
  `grep` exit 3 with `OperationalError: unable to open database file`, and
  `status --json` carries that same error in its `index` block. That is the desk
  telling you to use it, not a broken install and not a host-only refusal.
  **Not every surface is that loud**, so never read a quiet answer as an
  answer: an EMPTY result and a `not-computed` status are also what you get from
  a vault you cannot see. Only a hit you obtained through the desk is evidence.

Either way the answer is the same: call the desk. And the desk itself has no
offline substitute — it speaks stdio to the host process Claude Desktop spawned,
so there is no endpoint in this sandbox to dial and nothing to fall back to.

**The one local exception, and it returns no note.** If this workspace still
carries a staged engine, its three staging probes — `brain --version`, `brain
--role vm doctor`, `brain --role vm status --json` — read `.brain/` staging
metadata: engine stamp, model cache, vendored-deps ABI, snapshot presence. Keep
`--role vm` on them; without it the shell runs at role `host`, whose egress cap
is the whole vault. Use them for staging questions only. Their absence is not a
fault to fix, and none of the three is a way to reach a note.

If you need something the desk does not serve, say so and stop. That is a
finding for the engine, not a gap to work around.

Review the current conversation, cross-reference against the project's
config files (`AGENTS.md`/`CLAUDE.md`, `.claude/skills/`, recent evidence
artifacts), and present improvements one at a time. Apply approved changes
with Edit/Write.

This skill is **substrate-agnostic by design** — it has no
Smart-Connections/Bases coupling to repoint; it reads configuration and conversation, not the retrieval
index. The only change from the legacy version is *what* it scans: brain
conventions instead of an Obsidian/Cowork vault layout.

**Announce on start:** "Starting retrospective..."

> **Canonical layer note.** Some deployments keep a shared references
> directory acting as the single source of truth across multiple repos
> (this repo's own `docs/` plays that role for `AGENTS.md`/CLAUDE.md
> content via `@AGENTS.md` import — see the top of `CLAUDE.md`). In a
> canonical-aware project, propose edits to the canonical file, not a
> downstream copy that imports it.

**Targeted feedback wins.** If the user passes args (`/improve tone`,
`/improve skills`), treat those as highest priority but always run a full
sweep — never silence the rest of the audit because of args.

## Phase 0 — load prior learnings

Look for `_evidence/_improve_learnings.md` (or this repo's equivalent
learnings log) before anything else. It tracks acceptance rates by category
and modify signals from prior runs.

- Deprioritise finding categories consistently rejected.
- Boost categories consistently accepted.
- Adapt rule-writing style based on modify signals (e.g. if the user
  repeatedly softens NEVER → Avoid, propose softer language for
  non-critical rules).
- If the file doesn't exist, proceed normally — create it at the end of
  this run.

## Phase 1 — scope selection

Ask one question: "What scope should this retrospective cover?"

- **Current conversation only** — analyse just this session's signals.
- **Current conversation + project artifacts (recommended)** — also scan
  `AGENTS.md`, `CLAUDE.md`, `.claude/skills/*/SKILL.md`, `docs/`, and
  recent `_evidence/` output.
- **Project artifacts only** — skip live conversation analysis; useful for
  a config audit without recent friction.

## Phase 2 — discovery (only if scope includes project artifacts)

Catalog, concise (under 300 words), grouped by purpose with line counts and
last-modified dates:

- **`AGENTS.md`** — the canonical conventions file (every harness reads
  this; `CLAUDE.md` just imports it via `@AGENTS.md`).
- **`.claude/skills/*/SKILL.md`** and any `commands/*.md` in the same dirs.
- **`docs/`** — specs (`docs/substrate-spec.md`, `docs/classification-
  scheme.md`, `docs/operations/` if this is a cutover-in-progress repo).
- **`_evidence/`** — recent run artifacts, by mtime.
- **`tools/validate.py`** output — a clean/dirty validator run is itself a
  signal about whether the conventions in `AGENTS.md` are actually being
  followed.

If the discovery pass returns empty or fails, fall back to a direct Glob for
the most common paths (`**/AGENTS.md`, `**/SKILL.md`, `_evidence/**/*.md`).
Always say what was skipped and why — never silently proceed with
incomplete data.

## Phase 3 — current conversation analysis (foreground, unless scope = project-only)

**Announce:** "Analyzing current conversation for patterns, feedback, and
techniques..."

| Signal | What to look for |
|---|---|
| Corrections | "no", "don't", "stop", "not that", "wrong", "actually", "instead" — and any redo request |
| Praise | "yes", "perfect", "exactly", "great" — and silent acceptance after a contested point |
| Friction | Multiple attempts, back-and-forth to land the same task, repeated clarifications |
| Substrate misses | A retrieval call that should have used `grep` first and went straight to `search` (or vice versa), a `write` with no following `sync`, a near-dup `brain integrity` finding that got silently merged without review |

## Phase 4 — synthesise findings

Group findings by category: **conventions** (AGENTS.md/CLAUDE.md drift),
**skills** (a kernel skill's documented command doesn't match what
`brain --help` actually exposes — this is the single highest-value check
post-cutover, since a stale verb reference silently breaks a skill),
**memory/evidence hygiene**, **process**.

For each finding, draft a specific, minimal proposed edit — not a vague
"consider tightening X".

## Phase 5 — present one at a time

For each finding, present exactly one structured question: Accept / Reject
/ Modify. Apply Edit/Write only on Accept. On Modify, ask what to change,
re-draft, re-present once.

## Phase 6 — close out

Append to `_evidence/_improve_learnings.md`: what was proposed, what was
accepted/rejected/modified, one line each. This is what Phase 0 of the next
run reads.

## Hard guardrails

- **Never propose a skill edit that contradicts `brain --help`'s live
  output without checking first** — the CLI is the canonical, always-current
  contract (AGENTS.md §5 "Self-discovery"); a skill doc that drifted from it
  is the bug, not the CLI.
- **Never silently merge a near-dup pair flagged by `brain integrity`** —
  that's kb-curator's job and it's explicitly a human decision there too.
- **No content edits to `vault/raw/`** — immutable, never in scope here.

## Cross-references

- `AGENTS.md` — the canonical conventions file this skill audits against
- `brain --help` — the live verb contract to check skill docs against
