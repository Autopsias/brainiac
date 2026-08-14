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
- [COS v7 — Four Chips and a Grounded Judge](_plans/cos-v7-four-chips-grounded-judge-2026-08-14/PLAN.html) · **8 sessions** · run via `/plan-execute _plans/cos-v7-four-chips-grounded-judge-2026-08-14` · built 2026-08-14, **HARDENED 2026-08-14** (`/plan-harden`: 55 edits, 9 plan-killers closed — see `PLAN-HARDEN-SUMMARY.md`, `PLAN-REVIEW-LOG.md`, `HARDEN-FINDINGS.md`, `GRILL-LOG.md` in the plan dir). Three further owner rulings of 2026-08-14 are baked in: (A) the run-integrity bar is **VALID, or VALID_DEGRADED whose ONLY degraded control is `candidate_stamps`** — plain "VALID" was unreachable, since that control degrades on every night that stages candidates without dropping a proposal; (B) `triage.noise_signal_required` **gains a new typed signal** so the archive widening actually ships (the retained sender≥3 rule alone would archive nothing new), with a before/after truth table required; (C) the plan **splits into two increments** — new session **s02b** validates the chips-only change on its own attended run before grounding exists, and s03 depends on it. **s02b has NOT been adversarially reviewed** (created after Phase 2) — re-run `/plan-harden --from-phase 2` before executing past s02. Standing correction: `check_self_eval` currently discards the PASS/FAIL/N/A token and compares ids only, so an all-FAIL report scores PASS — s02 must fix the verifier itself. Four owner rulings of 2026-08-14 are BAKED INTO the session prompts as [OWNER RULING] blocks: (1) chips = P0 · Now / P1 · Today / P2 · This week / P3 · Read, read mail below P3 archived; (2) the UNREAD SHIELD stays — unread mail is never auto-archived; (3) vault grounding at FULL tier (MNPI), residual accepted; (4) the nightly stays DISARMED at plan end — re-arm is a manual owner action. Code sessions run in the COS worktree (`.claude/worktrees/cos-workflow-rebuild`, branch `plan/cos-workflow-rebuild-2026-08-10`); live mailbox runs happen in the MAIN conversation at the s06 checkpoint, never in a dispatched session.
- [Corpus Invariants — Permanent Maintenance System](_plans/corpus-invariants-2026-08-10/PLAN.html) · 12 sessions · run via `/plan-execute _plans/corpus-invariants-2026-08-10 --auto` · hardened 2026-08-10 (72 hardenings, verify-approved@r3; audit trail in `PLAN-REVIEW-LOG.md`, grill Q+A in `GRILL-LOG.md`). Three owner rulings of 2026-08-10 are BAKED INTO the session prompts as `[OWNER RULING 2026-08-10]` blocks: (1) the cross-tier fix must keep the chief-of-staff's grounding working — "accept the availability loss" is not a checkpoint option; (2) bulk linking runs with the s06→s07 go/no-go gate; (3) the corpus-invariants watchdog carries a mandatory dead-man's switch checked from lanes outside the nightly. A fresh session needs NO context beyond the plan directory.
- [COS Nightly Repair](_plans/cos-nightly-repair-2026-08-08/PLAN.html) · 8 sessions · s07 AWAITS_REVIEW, s08 TODO · resume via `/plan-execute _plans/cos-nightly-repair-2026-08-08 --resume`

See `_plans_index.md` for all other plans (complete, halted-pending-closure, or awaiting owner review — the index carries per-plan status).
