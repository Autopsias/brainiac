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

**Active plans:** none. The Deliverables Shelf plan closed 2026-08-24.

Deliverables Shelf (closed 2026-08-24, merged as `e24d251`), Self-Healing Vault
(closed 2026-08-22) and Corpus Invariants (closed 2026-08-12) are complete. The
shelf itself is live: a generated folder OUTSIDE the vault
(`<vault>/../brain-deliverables`, [ADR 0010](docs/adr/0010-deliverables-shelf-outside-the-vault.md))
holding the latest version of every produced output, grouped by project and
refreshed by the nightly fold — see `docs/deliverables-shelf.md`. Known cosmetic
gap the owner deferred: the drop lane titles its anchor from the source filename,
so 41 of the 117 shelf rows read as filenames.

See `_plans_index.md` for all other plans (complete, halted-pending-closure, or
awaiting owner review — the index carries per-plan status).
