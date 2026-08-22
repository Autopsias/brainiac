# CLAUDE.md — Profile A `brain` (Claude Code + Claude Desktop Code tab)

@AGENTS-core.md

> **AGENTS.md is canonical; AGENTS-core.md is its ≤200-line Claude Code core.**
> Codex, Gemini CLI, and the Desktop Code tab all still read the full,
> unmodified `AGENTS.md` — that stays ONE source of truth for the note shape,
> link style, capture rules, the four interactions, and the security posture.
> Claude Code alone imports the smaller `AGENTS-core.md` instead, to keep base
> context down; the rest of AGENTS.md's content loads CONDITIONALLY from
> `.claude/rules/*.md` (path-scoped — see AGENTS-core.md's pointer table) only
> when a matching file is touched. Do not duplicate content across
> `AGENTS.md`/`AGENTS-core.md`/`.claude/rules/`; edit `AGENTS.md` for the
> canonical text, then mirror any structural change into the split (context
> diet, s05, 2026-08-22).

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
- [Self-Healing Vault](_plans/self-healing-vault-2026-08-20/PLAN.html) · 11 sessions · run via `/plan-execute _plans/self-healing-vault-2026-08-20 --auto` · hardened 2026-08-20 (33 hardenings, converged@r2; audit trail in `PLAN-REVIEW-LOG.md`, grill Q+A in `GRILL-LOG.md`). Owner checkpoints before s01 (disposition-table ruling + WAT-01 alert-surface change + corpus-invariants coordination) and s07 (bulk-rule the opening exception backlog). s05 stays blocked while the corpus-invariants plan has open sessions touching `invariants.py`. A fresh session needs NO context beyond the plan directory.
- [Deliverables Shelf](_plans/deliverables-shelf-2026-08-20/PLAN.html) · 9 sessions · run via `/plan-execute _plans/deliverables-shelf-2026-08-20` · a generated shelf folder OUTSIDE the vault (`<vault>/../brain-deliverables`, [ADR 0010](docs/adr/0010-deliverables-shelf-outside-the-vault.md)): latest version of every produced output, grouped by project, maintained by the nightly, and absorbing the hand-made `deliverables/` folder ([owner ruling 2026-08-20](_plans/deliverables-shelf-2026-08-20/PLAN-REVIEW-LOG.md)). Hardened 2026-08-20 over 5 review passes (`GRILL-LOG.md`, `PLAN-REVIEW-LOG.md`). Owner checkpoints before s07 (live reference-vault writes) and s08 (host engine deploy).
- [Corpus Invariants — Permanent Maintenance System](_plans/corpus-invariants-2026-08-10/PLAN.html) · 12 sessions · run via `/plan-execute _plans/corpus-invariants-2026-08-10 --auto` · hardened 2026-08-10 (72 hardenings, verify-approved@r3; audit trail in `PLAN-REVIEW-LOG.md`, grill Q+A in `GRILL-LOG.md`). Three owner rulings of 2026-08-10 are BAKED INTO the session prompts as `[OWNER RULING 2026-08-10]` blocks: (1) the cross-tier fix must keep the chief-of-staff's grounding working — "accept the availability loss" is not a checkpoint option; (2) bulk linking runs with the s06→s07 go/no-go gate; (3) the corpus-invariants watchdog carries a mandatory dead-man's switch checked from lanes outside the nightly. A fresh session needs NO context beyond the plan directory.
- [COS Nightly Repair](_plans/cos-nightly-repair-2026-08-08/PLAN.html) · 8 sessions · s07 AWAITS_REVIEW, s08 TODO · resume via `/plan-execute _plans/cos-nightly-repair-2026-08-08 --resume`

See `_plans_index.md` for all other plans (complete, halted-pending-closure, or awaiting owner review — the index carries per-plan status).
