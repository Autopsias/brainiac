# CLAUDE.md — Profile A `brain` (Claude Code + Claude Desktop Code tab)

@AGENTS.md

> **AGENTS.md is canonical.** The line above imports it verbatim — Claude Code
> expands `@AGENTS.md` at startup, so there is ONE source of truth for the note
> shape, link style, capture rules, the four interactions, and the security
> posture. Do not duplicate that content here; edit `AGENTS.md` and every harness
> (Codex, Claude Code, Gemini CLI, the Desktop Code tab) sees the change.

## Brain usage (one paragraph)

Retrieval, capture, and indexing are owned by the **`brain` CLI** — call it from
your native shell, **never via MCP**. Read tools: `brain search "<q>" --json`,
`brain get <id> --json`, `brain recent --json`, plus `grep` / `bases-query` /
`graph-expand` (compose them; lexical-first, embed lazily). Every read applies the
**classification filter** before stdout (unlabelled ⇒ ranked MNPI; host
default cap = full vault, `--role vm` default = Internal; narrow with
`--max-tier` / `$BRAIN_DEFAULT_MAX_TIER`). Capture with
`brain draft-capture` (stages a draft; the host signs + indexes it later). Run
`brain --help` for the always-current contract. On the **Cowork Linux VM** add
`--role vm` (or `export BRAIN_ROLE=vm`): a read + draft surface that reads only
the published read-only snapshot and never signs — see
`docs/cowork-windows-install.md`.

**Active plans:**
- [Corpus Invariants — Permanent Maintenance System](_plans/corpus-invariants-2026-08-10/PLAN.html) · 12 sessions · run via `/plan-execute _plans/corpus-invariants-2026-08-10 --auto` · hardened 2026-08-10 (72 hardenings, verify-approved@r3; audit trail in `PLAN-REVIEW-LOG.md`, grill Q+A in `GRILL-LOG.md`). Three owner rulings of 2026-08-10 are BAKED INTO the session prompts as `[OWNER RULING 2026-08-10]` blocks: (1) the cross-tier fix must keep the chief-of-staff's grounding working — "accept the availability loss" is not a checkpoint option; (2) bulk linking runs with the s06→s07 go/no-go gate; (3) the corpus-invariants watchdog carries a mandatory dead-man's switch checked from lanes outside the nightly. A fresh session needs NO context beyond the plan directory.
- [COS Nightly Repair](_plans/cos-nightly-repair-2026-08-08/PLAN.html) · 8 sessions · s07 AWAITS_REVIEW, s08 TODO · resume via `/plan-execute _plans/cos-nightly-repair-2026-08-08 --resume`

See `_plans_index.md` for all other plans (complete, halted-pending-closure, or awaiting owner review — the index carries per-plan status).
