# Harness wiring — one instruction file, five surfaces (INT-01)

**Goal:** Codex, Claude Code, Gemini CLI, and the Claude Desktop **Code tab** all
discover and call the `brain` engine **the same way** — via their native shell,
**no MCP**. The single source of truth is **`AGENTS.md`**.

## `brain connect` — SUI-02, the automated wirer

Everything below this line is what `brain connect --client <c>` does FOR you —
read it to understand the wiring, but don't hand-copy the four JSON/Markdown
snippets anymore. `brain connect` (host-only; refused on `--role vm` at the
same gate as `supersede`/`ingest`) always shows a unified diff of the exact
change and asks before touching any user config file:

```
brain connect --client claude-code    # runs `claude plugin marketplace add` +
                                       # `claude plugin install brainiac-kernel@brainiac`
                                       # (falls back to printing the two commands,
                                       # guided not one-command, if the `claude`
                                       # plugin CLI isn't detected), then offers
                                       # to append the brain-usage paragraph to
                                       # the target project's CLAUDE.md
brain connect --client claude-desktop # merges the brain-mcp stanza into
                                       # claude_desktop_config.json (never
                                       # replaces mcpServers — only adds/updates
                                       # this vault's entry)
brain connect --client codex          # appends a marked brain-usage block to
                                       # the target project's AGENTS.md if absent
brain connect --client gemini         # merge-writes .gemini/settings.json with
                                       # contextFileName=AGENTS.md, preserving
                                       # every other key
```

Idempotent (a second run reports "already connected"), and every wire path has
an inverse: `brain connect --client <c> --remove` restores the pre-connect
backup for the two JSON-config clients and strips the marked block for the two
Markdown-append clients. `--yes` skips the interactive confirmation for
scripted/CI use; running non-interactively without it prints the diff and
exits non-zero rather than mutating anything. `brain mcp-config` remains the
PRINT-ONLY equivalent for the claude-desktop stanza (paste-it-yourself); the
two never diverge — both build the MCP entry from the same
`connect.mcp_server_entry` builder.

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
`./install.sh` / `./install.ps1` (PyPI-first — `uv tool install` / `pipx
install` / `pip install --user`, whichever succeeds first — installs the
`brain` console script; `--dev`/`-Dev` for a contributor's editable checkout
instead). Cowork VM: the binary ships in the workspace and PATH is
re-exported per session — see `cowork-windows-install.md`.

## The one exception — the pure Chat tab (INT-03)

The Chat tab is the single surface that **cannot run a shell command**, so it
gets a thin, **optional, deletable** MCP bridge: `src/brain/mcp_adapter.py`
(~50 lines) wraps the SAME `BrainCore` + the SAME deny-by-default
`ClassificationFilter` and exposes only the read verbs. **MCP is never the
foundation** — delete the adapter and every other harness still works. It's
already included in a normal PyPI install (`brainiac-cli[mcp]`, what
`install.sh`/`install.ps1` install by default); a contributor's editable
checkout needs `pip install -e '.[mcp]'`. Either way, run it with `brain-mcp`
(or `python -m brain.mcp_adapter`). The packaged delivery for this surface is
the `.mcpb` extension (`docs/install/README.md` Path G) — a thin Node stdio
shim that spawns this same host-installed `brain-mcp`; `brain connect
--client claude-desktop` is the alternative config-stanza route (pick one,
never both — `brain doctor` flags double registration).

## Why no MCP for the command-capable surfaces

The `brain` CLI already returns sourced JSON and applies the egress gate at
stdout. A command-capable harness gets the full, self-describing contract from
`brain --help` for free; adding MCP would mean a second egress path to keep in
sync and a server to run. The CLI is the foundation; MCP is a single-surface
convenience.

## Session hooks — Claude Code CLI only (MEM-02, ADR-0003 Ruling 4)

`.claude/hooks/` (`session-start.sh`, `pre-compact.sh`,
`block-vault-recursive-scan.py`, wired in `.claude/settings.json`) are a
**Claude Code CLI-only** surface — they fire on `SessionStart`/`PreCompact`/
`PreToolUse` hook events that only that harness emits. Codex, Gemini CLI, and
Cowork do not run them; those harnesses simply don't get the automatic
handoff injection or the recursive-scan guardrail. Full contract (file
locations, rotation rule, entry formats): `docs/session-memory.md`.

**Not ported: the reference vault's auto-commit `post-write.sh`.** The reference
vault commits after every qualifying write as a forensic trail. This substrate already has that
provenance mechanism — the Ed25519-signed, hash-chained audit log
(AGENTS.md §6) — for every real `write_note`. A second, git-level auto-commit
would duplicate that trail and take commit authorship out of human hands;
per the git-safety rules this repo operates under, commits stay
human-initiated. So the hook is deliberately not carried over.
