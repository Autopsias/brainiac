# Harness wiring — one instruction file, six surfaces (INT-01)

**Goal:** Every harness shares one conventions contract (**`AGENTS.md`**) and
one classification/egress boundary (`BrainCore` + `ClassificationFilter`).
Shell-capable host harnesses (Codex, Claude Code CLI, Claude Desktop **Code
tab**, Gemini CLI) call the **`brain` CLI** directly; sandboxed and
shell-less surfaces (**Cowork**, **headless weekly synthesis**, and **Claude
Desktop Chat tab**) reach the vault through the **17-tool `brain-mcp` desk
broker** (`src/brain/mcp_adapter.py`).

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
CLAUDE.md                ← `@AGENTS.md` (Claude Code expands the import directly at
                           startup — `AGENTS.md` fits in <=20 KB, so `.claude/rules/`
                           mirrors and `AGENTS-core.md` are retired)
.gemini/settings.json    ← { "contextFileName": "AGENTS.md" }
<cowork-ws>/CLAUDE.md    ← INLINE copy of AGENTS.md in a marked BRAIN-CONTRACT
                           block, re-synced by tools/cowork_workspace_install.sh
                           on every refresh. Inline, never an @import — VERIFIED
                           2026-07-20 on a live Cowork workspace: Cowork
                           auto-loads a workspace-root CLAUDE.md but does NOT
                           expand @-imports (canary session showed
                           "[brain contract loaded]" without the imported body)
```

`AGENTS.md` carries the one-paragraph brain-usage note (self-discovery: §5
"Self-discovery — the `brain` CLI is the one interface"). Every harness below
reaches that same paragraph; none re-states it.

## Per-harness discovery (CLI vs `brain-mcp` desk)

| Harness | Reads | Transport | How it calls brain |
|---|---|---|---|
| **Codex** | `AGENTS.md` natively (its startup convention) | Host shell CLI | `brain search … --json` |
| **Claude Code (CLI)** | `CLAUDE.md` → `@AGENTS.md` import | Host shell CLI | Bash tool: `brain …` |
| **Claude Desktop — Code tab** | `CLAUDE.md` → `@AGENTS.md` (same repo file) | Host shell CLI | its shell: `brain …` |
| **Gemini CLI** | `.gemini/settings.json` sets `contextFileName=AGENTS.md` | Host shell CLI | shell: `brain …` |
| **Cowork (Desktop VM)** | workspace-root `CLAUDE.md` auto-loaded at session start; contract INLINED in the marked BRAIN-CONTRACT block (@imports don't expand in Cowork). Probe: send `contract?` → `[brain contract loaded] [contract inlined]` = healthy; session-prompt paste = fallback | **Desk MCP (`brain-mcp`)** | Host-run 17-tool `brain-mcp` stdio broker (see below); local CLI only for `cos-propose` and optional staging probes |
| **Headless weekly synthesis (`scripts/brain-synthesis.sh`)** | Host-generated strict settings + MCP config | **Desk MCP (`brain-mcp`)** | Fail-closed OS sandbox (no Bash, no network); 5 read-only, `Internal`-capped MCP tools (`search`, `get`, `recent`, `bases_query`, `dossier`) over `brain-mcp` (`BRAIN_ROLE=vm`, `BRAIN_MAX_EGRESS_TIER=Internal`). (Opt-in Codex synthesis uses host-prepared packets with no MCP — see `docs/operations/codex-synthesis.md`.) |
| **Claude Desktop — Chat tab** | (cannot run a command) | **Desk MCP (`brain-mcp`)** | Host-run 17-tool `brain-mcp` stdio broker via `.mcpb` extension (`docs/install/README.md` Path G) or `brain connect --client claude-desktop` |

**Prereq for all shell harnesses:** `brain` must be on `PATH`. Local/host:
`./install.sh` / `./install.ps1` (PyPI-first — `uv tool install` / `pipx
install` / `pip install --user`, whichever succeeds first — installs the
`brain` and `brain-mcp` console scripts; `--dev`/`-Dev` for a contributor's
editable checkout instead).

## The 17-tool `brain-mcp` desk transport

Three surfaces cannot or must not read the vault through a local shell:
**Cowork** (the Desktop VM, whose skills are pinned by
`docs/operations/cowork-desk-block.md` and `tests/test_cowork_skill_bundles.py`),
**headless weekly synthesis** (`scripts/brain-synthesis.sh`, which runs Claude
Code with Bash and network denied and exposes a 5-tool read-only subset), and
the **Claude Desktop Chat tab** (which has no shell).

All three speak stdio to the host-run **`brain-mcp`** broker
(`src/brain/mcp_adapter.py`, `python -m brain.mcp_adapter`). It wraps the same
`BrainCore` and deny-by-default `ClassificationFilter` as the CLI and enforces
two independent boundaries (`src/brain/mcp_tools/__init__.py`,
`src/brain/mcp_mediation.py`):

1. **Sealed registration seam (`src/brain/mcp_tools/`):** Five modules load in
   fixed order — `core_read`, `retrieval`, `housekeeping`, `capture`,
   `supersession` — then `BrokerServer` seals registration and refuses any
   late `add_tool`, `add_resource`, `resource()`, `add_prompt`, `completion()`,
   or `custom_route()`.
2. **Mediation boundary (`BrokerServer.call_tool` → `mcp_mediation.mediate`):**
   Every tool call resets the per-call egress tally and flushes a fail-closed
   SEC-06 read-log record (raising `UngatedToolError` if a non-`BODYLESS_TOOLS`
   handler returns without hitting `egress.apply_gate`).

The broker exposes **17 tools** (`14 READ_TOOLS + 3 WRITE_TOOLS`). It is
included in a normal PyPI install (`brainiac-cli[mcp]`, what
`install.sh`/`install.ps1` install by default); a contributor's editable
checkout needs `pip install -e '.[mcp]'`. For Claude Desktop, use either the
`.mcpb` extension (`docs/install/README.md` Path G, a thin Node stdio shim
spawning the host-installed `brain-mcp`) or `brain connect --client
claude-desktop` — pick one, never both (`brain doctor` flags double
registration).

**The variant contract reaches this surface too (CON-01).** `search` takes a
`variants` list — the MCP twin of the CLI's repeatable `--variant` — routed to
the same `core.search_multi`, and `vault_languages` publishes the derived
language census so desk callers can tell whether AGENTS.md §5 rule 3 applies.
Full entry-point matrix: `docs/query-variant-entry-points.md`.

### CLI `VM_ALLOWED` (23 verbs) vs `brain-mcp` desk (`17` tools) parity table

Authority in code: `brain.cli.VM_ALLOWED` (23 CLI verbs) and
`brain.mcp_adapter.TOOLS` (`14 READ_TOOLS` + `3 WRITE_TOOLS` = 17 MCP tools).
Every host-only commit/rebuild verb (`write`, `sync`, `maintain`, `rebuild`,
`ingest`, `curate`, `health`, `integrity`, `verify-audit`, `graph-report`,
etc.) is absent from both.

| CLI `VM_ALLOWED` verb | Desk MCP tool | Desk class | Seam module | Notes |
|---|---|---|---|---|
| `search` | `search` | `READ` | `core_read` | Supports `variants=[...]` (CON-01); in synthesis 5-tool subset |
| `hybrid-search` | `hybrid_search` | `READ` | `retrieval` | Reciprocal-rank hybrid retrieval |
| `get` | `get` | `READ` | `core_read` | Fetch note by id/path; in synthesis 5-tool subset |
| `read` | `read` | `READ` | `core_read` | Alias of `get` |
| `recent` | `recent` | `READ` | `core_read` | Recently updated notes; in synthesis 5-tool subset |
| `bases-query` | `bases_query` | `READ` | `core_read` | Structured frontmatter query; in synthesis 5-tool subset |
| `dossier` | `dossier` | `READ` | `retrieval` | Decision-state + tensions sweep; in synthesis 5-tool subset |
| `grep` | `grep` | `READ` | `retrieval` | Literal/regex search across gated notes |
| `graph-expand` | `graph_expand` | `READ` | `retrieval` | Neighborhood expansion from wikilink graph |
| `diagnose` | `diagnose` | `READ` | `retrieval` | Retrieval/ranking explanation for a query |
| `alerts` | `alerts` | `READ` | `housekeeping` | Degradation digest (`BODYLESS_TOOLS`, no note bodies) |
| `exceptions` | `exceptions` | `READ` | `housekeeping` | Owner exception/inbox queue (`BODYLESS_TOOLS`) |
| `capture` | `capture` | `WRITE` | `capture` | Stages unsigned draft (`status: draft`, `provenance.trust: untrusted`); refuses duplicate ids |
| *(not in `VM_ALLOWED`)* | `inbox` | `READ` | `housekeeping` | Alias of `exceptions` (`BODYLESS_TOOLS`) |
| *(not in `VM_ALLOWED`)* | `vault_languages` | `READ` | `core_read` | Derived language census (`BODYLESS_TOOLS`, CON-01) |
| *(not in `VM_ALLOWED`)* | `supersede` | `WRITE` | `supersession` | Owner-declared version retirement (unrelated pair is proposed, not applied; SUP-01) |
| *(not in `VM_ALLOWED`)* | `unsupersede` | `WRITE` | `supersession` | Restore retired note version (SUP-01) |
| `brief` | *(no desk tool)* | CLI-only | — | Local CLI only |
| `check-egress` | *(no desk tool)* | CLI-only | — | Local egress check |
| `cos-propose` | *(no desk tool)* | CLI-only | — | COS ingress; invoked locally by COS Cowork bundle |
| `digest` | *(no desk tool)* | CLI-only | — | Local CLI only |
| `doctor` | *(no desk tool)* | CLI-only | — | Local staging probe only (see `/vm-doctor`) |
| `draft-capture` | *(use `capture`)* | CLI-only | — | Legacy CLI spelling; desk serves `capture` |
| `init` | *(no desk tool)* | CLI-only | — | Local vault/overlay initialization |
| `mcp-config` | *(no desk tool)* | CLI-only | — | Prints Claude Desktop MCP config stanza |
| `provision-request` | *(no desk tool)* | CLI-only | — | Local provisioning request |
| `status` | *(no desk tool)* | CLI-only | — | Local staging probe only (see `/vm-doctor`) |

Summary counts: **13** verbs are on both CLI `VM_ALLOWED` and the desk,
**10** CLI `VM_ALLOWED` verbs have no desk tool, and **4** desk tools (`inbox`,
`vault_languages`, `supersede`, `unsupersede`) are not in CLI `VM_ALLOWED`
(13 + 10 = 23 CLI; 13 + 4 = 17 MCP).

## Alert surfacing — `brain alerts`, and why it is not a hook

A degraded vault has to reach whichever harness the owner happens to open.
Until 2026-08-14 the degradation digest existed ONLY as a Claude Code
SessionStart hook, so Codex and Cowork were blind to it by construction —
the inputs are five plain files, and nothing about reading them is
Claude-Code-specific. `brain alerts` is that logic in the engine (`VM_ALLOWED`
and served on the desk as `alerts`), and each harness reaches it by whatever
mechanism it has:

| Harness | Mechanism | Guarantee |
|---|---|---|
| **Claude Code (CLI)** | `SessionStart` hook in `~/.claude/settings.json` → `~/.claude/hooks/brainiac-alerts.sh` → `brain alerts --one-line`, placed and registered by `brain install-hook` (and re-placed by every `brain update`) | **hard** — the harness injects it |
| **Codex** | `SessionStart` in `~/.codex/hooks.json` (same schema as Claude Code) → the same script | **hard** — the harness injects it |
| **Cowork (Desktop VM)** | AGENTS.md §9 instruction, inlined into the workspace `CLAUDE.md` BRAIN-CONTRACT block → desk `alerts` tool | **soft** — Cowork has no hook mechanism; the model must follow the line |
| **Gemini CLI / Desktop Code tab** | AGENTS.md §9 instruction | **soft** |

Two consequences worth stating plainly. **Cowork cannot be made hard** — there
is no hook surface to register, so the contract line is the mechanism, and a
session that ignores it sees nothing. **The VM sees a subset when reading
locally, and says so:** the current-findings feed, the owner inbox and the
engine-feedback backlog live on the shared mount, while
`~/.brainiac/update-state.json` and `~/.brain/synthesis-state.json` live in
host home — calling the host-run desk `alerts` tool reads all five; local CLI
`brain --role vm alerts` lists unreachable host-home files under `unreachable`
so "no alerts" on the VM can never quietly mean "could not look".

## Why host shell harnesses stay on the CLI

The `brain` CLI already returns sourced JSON and applies the egress gate at
stdout. A command-capable host harness gets the full, self-describing contract
from `brain --help` (including host-broker verbs that the desk deliberately
excludes). The CLI remains the foundation for host shells; `brain-mcp` is the
bounded desk transport for sandboxed and shell-less surfaces.

## Session hooks — Claude Code CLI and Codex (MEM-02, ADR-0003 Ruling 4)

Both **Claude Code CLI** (`.claude/settings.json` + `.claude/hooks/`) and
**Codex** (`.codex/hooks.json` + `.codex/hooks/`) wire the same three hook
events (`SessionStart`, `PreCompact`, `PreToolUse` on `Bash`). Gemini CLI and
Cowork do not run repo hooks; those harnesses rely on `AGENTS.md` instructions.
Full memory contract (file locations, rotation rule, entry formats):
`docs/session-memory.md`.

### Repo hook contract (`.claude/settings.json` and `.codex/hooks.json`)

The canonical hook scripts live under `.claude/hooks/` (3 events, 4 hook commands):

| Hook event | Matcher | Script | Purpose |
|---|---|---|---|
| `SessionStart` | `startup\|resume\|compact\|clear` | `.claude/hooks/session-start.sh` | Injects session handoff, hot queue, and lessons (resolves repo root via `$CLAUDE_PROJECT_DIR` in Claude Code or `git rev-parse --show-toplevel` in Codex) |
| `PreCompact` | `manual\|auto` | `.claude/hooks/pre-compact.sh` | Preserves handoff state before context compaction |
| `PreToolUse` | `Bash` | `.claude/hooks/block-vault-recursive-scan.py` | Blocks recursive vault scans that bypass `brain` retrieval |
| `PreToolUse` | `Bash` | `.claude/hooks/block-main-checkout-branch-switch.py` | Blocks destructive branch switches in the shared main checkout |

`.codex/hooks.json` mirrors `.claude/settings.json` with the exact same 3 events, matchers, and 4 hook commands, sharing the canonical implementations without drift:
- All four `.codex/hooks/` entries (`session-start.sh`, `pre-compact.sh`, `block-vault-recursive-scan.py`, `block-main-checkout-branch-switch.py`) are symlinks to `../../.claude/hooks/<script>` (rather than copied scripts).
- `.codex/hooks.json` invokes each via `$(git rev-parse --show-toplevel)/.codex/hooks/...` — including both `block-vault-recursive-scan.py` and `block-main-checkout-branch-switch.py` on `PreToolUse` (`Bash`), matching `.claude/settings.json`.

**Not ported: the reference vault's auto-commit `post-write.sh`.** The reference
vault commits after every qualifying write as a forensic trail. This substrate already has that
provenance mechanism — the Ed25519-signed, hash-chained audit log
(AGENTS.md §6) — for every real `write_note`. A second, git-level auto-commit
would duplicate that trail and take commit authorship out of human hands;
per the git-safety rules this repo operates under, commits stay
human-initiated. So the hook is deliberately not carried over.
