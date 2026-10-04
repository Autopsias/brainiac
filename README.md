# Brainiac

A local second brain for any LLM. Your notes stay plain Markdown and YAML on
your own disk. The `brain` command-line engine gives Claude Code, Codex,
Gemini CLI and Claude Desktop fast, sourced search over them. No vendor
lock-in, no cloud index, no server.

## Why

Most retrieval tools ask you to trust one app and its plugin pipeline forever.
Brainiac keeps the substrate as files (`vault/brain/` and `vault/raw/`), keeps
the search index as a cache you can rebuild at any time, and passes every read
through a deny-by-default classification filter before a model sees it. You
decide what an LLM may see, note by note.

## Install

Brainiac is one engine install plus one setup command. The setup command,
`brain init --full --apply`, creates your vault, seeds three sample notes,
builds the search index, provisions the audit signing key and registers the
maintenance task. Pick the path that matches how you work.

**1. Let your AI assistant do it.** Paste this into any assistant that can run
commands on your machine (Claude Code, Codex, Gemini CLI):

```text
Install Brainiac for me. Fetch and follow this exactly, asking me only what it says to:
https://raw.githubusercontent.com/Autopsias/brainiac/main/docs/install/LLM-INSTALL.md
```

**2. Claude Code plugin.** One command installs the plugin, and the plugin
installs the engine:

```text
/plugin install brainiac-manager --marketplace Autopsias/brainiac
/brainiac-install ~/brain
```

The one-command form needs Claude Code 2.1.275 or later. On an older version,
run `/plugin marketplace add Autopsias/brainiac`, then
`/plugin install brainiac-manager@brainiac`. In Claude Desktop, add the
marketplace under Customize › Plugins.

**3. By hand, on any OS.** With `uv`, `pipx` or `pip` already present:

```bash
uv tool install 'brainiac-cli[mcp]'        # or: pipx install 'brainiac-cli[mcp]'
BRAIN_VAULT=~/brain/vault brain init --full --apply
```

Without Python, the bootstrap script fetches `uv` and installs through it.
Run it, then the `brain init` line above:

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh && bash /tmp/brainiac-install.sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/Autopsias/brainiac/main/install.ps1 -OutFile install.ps1; .\install.ps1
$env:BRAIN_VAULT = "$HOME\brain\vault"; brain init --full --apply
```

With Node.js 18 or later, `npx brainiac-install --vault ~/brain` does the engine
install and the vault setup in one command. It needs `uv`, `pipx` or Python on
the machine already.

Every platform, step by step:
[docs/install/README.md](https://github.com/Autopsias/brainiac/blob/main/docs/install/README.md).

## First search

```bash
brain search "welcome" --json
```

Three things to know on day one:

- **Your notes** live in the vault (`~/brain/vault` above). They are the source
  of truth. **The index** is a cache in your app-data folder. `brain rebuild`
  recreates it at any time.
- **Semantic search** downloads its model (bge-m3-int8, about 563 MB) once, on
  first use. `brain warmup` fetches it up front.
- **An existing folder of notes** is not indexed by `brain init`. Run
  `brain rebuild` once, or the first search returns nothing.

`brain --help` lists every command.

On a Codex-only macOS host, Sunday synthesis can use the
[opt-in Codex adapter](https://github.com/Autopsias/brainiac/blob/main/docs/operations/codex-synthesis.md).
It handles new source-derived notes; the stock Claude runner remains the default.

## Update

```bash
brain update            # --dry-run shows the plan; your notes are never touched
```

It detects how you installed the engine (uv, pipx, pip or an editable checkout),
upgrades it, refreshes the Claude Code plugins, restages every registered
Cowork workspace and verifies with `brain doctor`. In Claude Code,
`/brainiac-update` runs the same thing.

You rarely need to run it. The hourly maintenance task applies a newer version
on its own, and keeps it only when `brain doctor` is green and a real query
embed succeeds. `brain doctor` on its own is a read-only health check with the
exact fix for anything stale.

## A second vault

The install is per machine. Vaults are per project. Point the same `brain` at
another folder and it gets its own index and audit chain:

```bash
export BRAIN_VAULT=~/vaults/my-new-project
brain init --full --apply
brain rebuild            # only when the folder already holds notes
```

Details, including the per-vault overlay and the scheduled task:
[docs/install/second-vault.md](https://github.com/Autopsias/brainiac/blob/main/docs/install/second-vault.md).

## How the AI assistants are wired

Every assistant calls the `brain` CLI through its normal shell. No MCP server
is required. `brain connect --client <name>` wires one client (`claude-code`,
`claude-desktop`, `codex` or `gemini`), shows the diff and asks before it
touches a config file. `AGENTS.md` is the conventions file every assistant
reads; `CLAUDE.md` imports its short core.

The one exception is the Claude Desktop **Chat tab**, which cannot run a
command. For that surface, install the `brainiac.mcpb` extension from the
latest release, or run `brain connect --client claude-desktop`. Pick one. The
full matrix is in
[docs/harness-wiring.md](https://github.com/Autopsias/brainiac/blob/main/docs/harness-wiring.md).

## Security, in one paragraph

All data stays on the local disk: notes are Markdown, the index is a local
SQLite file, and the project holds no model API keys. Every read command
filters notes by their classification tier before it prints, and an unlabelled
note counts as the most restrictive tier. Every committed write is
Ed25519-signed and hash-chained, with the key in the OS secret store. A
sandboxed surface such as Cowork gets a read-only snapshot and a draft inbox,
and can never sign or index. Runtime dependencies are pinned in
`requirements.lock`; CI runs `pip-audit` weekly and emits an SBOM.

- Controls, threat model and residual risks:
  [docs/security-overview.html](https://github.com/Autopsias/brainiac/blob/main/docs/security-overview.html)
- Classification tiers:
  [docs/classification-scheme.md](https://github.com/Autopsias/brainiac/blob/main/docs/classification-scheme.md)
- Reporting a vulnerability and rotating the audit key:
  [SECURITY.md](https://github.com/Autopsias/brainiac/blob/main/SECURITY.md)

## Where to go next

- **Every document, grouped by purpose:**
  [docs/README.md](https://github.com/Autopsias/brainiac/blob/main/docs/README.md)
- **What runs where, and why Cowork needs a host:**
  [docs/install/new-owner.md](https://github.com/Autopsias/brainiac/blob/main/docs/install/new-owner.md)
- **The conventions every assistant reads:**
  [AGENTS.md](https://github.com/Autopsias/brainiac/blob/main/AGENTS.md)
- **The words used in these docs:**
  [docs/glossary.md](https://github.com/Autopsias/brainiac/blob/main/docs/glossary.md)
- **Contributing:**
  [CONTRIBUTING.md](https://github.com/Autopsias/brainiac/blob/main/CONTRIBUTING.md)
  (this repository is a release mirror; read that file first)
- **Changes by version:**
  [CHANGELOG.md](https://github.com/Autopsias/brainiac/blob/main/CHANGELOG.md)

## Repository layout

```
AGENTS.md            conventions and the note schema
src/brain/           the brain CLI and engine (index, search, audit)
plugins/             the three Claude Code plugins (kernel, extras, manager)
docs/                install guides, specs, security notes, decision records
tools/validate.py    conventions validator for a vault
vault/               the small sample vault used above
```

## License

Apache-2.0. Built clean-room; see
[docs/clean-room-log.md](https://github.com/Autopsias/brainiac/blob/main/docs/clean-room-log.md).
