# CLAUDE.md — Profile A `brain` (Claude Code + Claude Desktop Code tab)

@AGENTS.md

> **Single source of truth:** `AGENTS.md` (<=20 KB) is canonical and imported
> directly by `CLAUDE.md` — Codex, Claude Code, the Desktop Code tab, and
> Gemini CLI all read the same file (`.claude/rules/*.md` mirrors are retired).
> Deep specs live in `docs/`; historical rationale and benchmarks live in
> `docs/substrate-history.md`; the full per-client matrix is in
> `docs/harness-wiring.md`.

## Brain usage

Shell-capable host harnesses (Claude Code CLI, Claude Desktop **Code tab**,
Codex, Gemini CLI) call the **`brain` CLI** directly from their native shell —
**never via MCP**. **Headless weekly synthesis** (`scripts/brain-synthesis.sh`)
runs on the host **`brain` CLI** directly (diagnostics, `write`,
`sync --publish`), not via the 17-tool desk broker; its sandboxed model pass
has no Bash/network and gets only a 5-tool read-only, `Internal`-capped MCP
subset (`BRAIN_MAX_EGRESS_TIER=Internal`). Interactive sandboxed or shell-less
surfaces (**relocated Cowork** and **Claude Desktop Chat tab**) reach the vault
through the **17-tool `brain-mcp` desk broker** (`src/brain/mcp_adapter.py`);
co-located Cowork workspaces use `brain --role vm` (`export BRAIN_ROLE=vm`,
reading only the published snapshot and staging drafts via `brain draft-capture`,
see `docs/install/cowork.md` and `docs/cowork-windows-install.md`).

Core read verbs: `brain dossier "<q>" --json` (decision-state + `tensions`),
`brain search "<q>" --json`, `brain get <id> --json`, `brain recent --json`,
`grep`, `bases-query`, `graph-expand`, `diagnose`, `alerts` (compose them;
lexical-first, embed lazily). Capture with `brain draft-capture` / `capture`.
Every read applies the deny-by-default **classification filter** before stdout
(unlabelled ⇒ `MNPI`; host CLI default = full vault; interactive `brain-mcp`
desk broker default = full vault unless `$BRAIN_MAX_EGRESS_TIER` narrows it —
headless synthesis never uses that full-vault default and pins `Internal`;
`--role vm` CLI default = `Internal`; adjust with `--max-tier`,
`$BRAIN_DEFAULT_MAX_TIER`, or `$BRAIN_MAX_EGRESS_TIER`). Run `brain --help` for
the always-current surface.

## Host ops and plan records

- Local live-host ops rulings and launchd checks (untracked): `~/.brain/host-ops.md`
- Historical closed-plan summaries: `docs/closed-plans-history.md`
- Per-plan status index (main checkout): `_plans_index.md`
- Public releases: follow `.claude/skills/publish-release/SKILL.md` (cut in a worktree and fast-forward the main checkout to the release commit before deploy)
