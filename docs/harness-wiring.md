# Harness wiring — one instruction file, five surfaces (INT-01)

**Goal:** Codex, Claude Code, Gemini CLI, and the Claude Desktop **Code tab** all
discover and call the `brain` engine **the same way** — via their native shell,
**no MCP**. The single source of truth is **`AGENTS.md`**.

## The canonical file + the imports

```
AGENTS.md                ← CANONICAL conventions + the brain-usage paragraph (§5)
CLAUDE.md                ← `@AGENTS.md` (Claude Code expands the import at startup)
.gemini/settings.json    ← { "contextFileName": "AGENTS.md" }
```

`AGENTS.md` carries the one-paragraph brain-usage note (self-discovery: §5
"Self-discovery — the `brain` CLI is the one interface"). Every harness below
reaches that same paragraph; none re-states it.

## Per-harness discovery (all native shell, no MCP)

| Harness | Reads | How it calls brain |
|---|---|---|
| **Codex** | `AGENTS.md` natively (its startup convention) | shell: `brain search … --json` |
| **Claude Code (CLI)** | `CLAUDE.md` → `@AGENTS.md` import | Bash tool: `brain …` |
| **Claude Desktop — Code tab** | `CLAUDE.md` → `@AGENTS.md` (same repo file) | its shell: `brain …` |
| **Gemini CLI** | `.gemini/settings.json` sets `contextFileName=AGENTS.md` | shell: `brain …` |
| **Claude Desktop — Chat tab** | (cannot run a command) | OPTIONAL thin MCP adapter — see below |

**Prereq for all shell harnesses:** `brain` must be on `PATH`. Local/host:
`pip install -e .` (installs the `brain` console script). Cowork VM: the binary
ships in the workspace and PATH is re-exported per session — see
`cowork-windows-install.md`.

## The one exception — the pure Chat tab (INT-03)

The Chat tab is the single surface that **cannot run a shell command**, so it
gets a thin, **optional, deletable** MCP bridge: `src/brain/mcp_adapter.py`
(~50 lines) wraps the SAME `BrainCore` + the SAME deny-by-default
`ClassificationFilter` and exposes only the read verbs. **MCP is never the
foundation** — delete the adapter and every other harness still works. Run it
with `pip install -e '.[mcp]'` then `brain-mcp` (or `python -m brain.mcp_adapter`).

## Why no MCP for the command-capable surfaces

The `brain` CLI already returns sourced JSON and applies the egress gate at
stdout. A command-capable harness gets the full, self-describing contract from
`brain --help` for free; adding MCP would mean a second egress path to keep in
sync and a server to run. The CLI is the foundation; MCP is a single-surface
convenience.
