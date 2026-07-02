---
name: improve
description: Runs a retrospective on a session working against a brain-substrate repo. Reviews the current conversation plus the project's AGENTS.md/CLAUDE.md, installed skills (.claude/skills/), and recent evidence artifacts, then surfaces improvements one at a time via a structured accept/reject/modify question and applies approved edits. Use whenever the user says "improve", "retrospective", "retro", "review my setup", "what should I change", "audit my skills", "what went wrong this session", "tighten my AGENTS.md", or "propose changes so this doesn't happen again". Also trigger on args like "/improve focus on tone". Trigger any time the user expresses friction, says they had to correct Claude repeatedly, or asks Claude to look back over a chat and propose config changes — even if they do not literally say "retrospective". Do NOT trigger for code review, document editing, or knowledge-base maintenance (kb-curator owns brain/ hygiene).
---

# improve — session retrospective (brain-substrate kernel)

Review the current conversation, cross-reference against the project's
config files (`AGENTS.md`/`CLAUDE.md`, `.claude/skills/`, recent evidence
artifacts), and present improvements one at a time. Apply approved changes
with Edit/Write.

This skill is **substrate-agnostic by design** — per
`docs/cutover/repoint-map.md` §7 it has no Smart-Connections/Bases coupling
to repoint; it reads configuration and conversation, not the retrieval
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
  scheme.md`, `docs/cutover/` if this is a cutover-in-progress repo).
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

- `docs/cutover/repoint-map.md` §7 — confirms this skill is KEEP/substrate-agnostic
- `AGENTS.md` — the canonical conventions file this skill audits against
- `docs/cutover/brain-cli-verbs.md` — the live verb contract to check skill docs against
