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
`graph-expand` (compose them; lexical-first, embed lazily). Every read applies a
**deny-by-default classification filter** before stdout (unlabelled ⇒ Secret ⇒
withheld; elevate with `--max-tier`, the human gate). Capture with
`brain draft-capture` (stages a draft; the host signs + indexes it later). Run
`brain --help` for the always-current contract. On the **Cowork Linux VM** add
`--role vm` (or `export BRAIN_ROLE=vm`): a read + draft surface that reads only
the published read-only snapshot and never signs — see
`docs/cowork-windows-install.md`.
