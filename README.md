# Brainiac

A local, any-LLM **second brain**: your notes stay plain Markdown + YAML on
your own disk, and the `brain` CLI gives any LLM harness (Claude Code, Codex,
Gemini CLI, Claude Desktop / Cowork, ...) fast, sourced search over them — no
vendor lock-in, no cloud index, no plugin ecosystem to keep alive.

## Why

Retrieval-augmented note-taking usually means picking a proprietary app and
trusting its plugin/embedding pipeline forever. Brainiac inverts that: the
substrate is just files (`vault/brain/`, `vault/raw/`), the search index is a
disposable cache you can rebuild any time, and every read goes through a
deny-by-default classification filter (the **egress gate** — see
`docs/glossary.md`) before it reaches a model — so you control what an LLM is
allowed to see, note by note. See `AGENTS.md` for the full conventions and
security model.

## Installing Brainiac for the first time

**Installing? → [`docs/install/README.md`](docs/install/README.md) (pick your
platform: Claude Code, Cowork, Codex, or Gemini CLI).**

The single most common path (Claude Code, on your own machine):

```text
claude> /plugin marketplace add Autopsias/brainiac
claude> /plugin install brainiac-manager@profile-a-marketplace
claude> /brainiac-install <path-to-your-vault>
```

**Or the one-command way** (needs Python 3.9+ and git; the repo is public —
no login needed):

```bash
git clone https://github.com/Autopsias/brainiac.git
cd brainiac
./install.sh
```

The installer creates a private Python environment (nothing touches your
system Python), installs the **full-capacity** `brain` CLI — semantic
search, fast vector backend, signed audit chain, no options to pick —
puts `brain` on your PATH, and builds the index for the bundled sample
vault. The first run downloads a small embedding model (a few hundred MB,
one time), so it needs network access once.

Then ask your first question:

```bash
brain search "arctic-embed vs e5" --json
```

Every search hit carries the note's file path and its classification tier,
and an `egress` block reports how many notes were withheld by the
classification filter.

(Prefer plain pip? `pip install -e .` from the repo root does the same full
install without the venv/PATH/first-index conveniences.)

Two things worth knowing about where files live:

- **Your notes** live in the vault (`vault/` by default — plain Markdown, the
  single source of truth).
- **The search index** lives in your per-user app-data folder
  (`~/Library/Application Support/profile-a-brain` on macOS,
  `%LOCALAPPDATA%\profile-a-brain` on Windows, `~/.local/share/...` on
  Linux). It is a derived cache — deleting it loses nothing; `brain rebuild`
  recreates it from the vault.

Per-client walkthroughs (Claude Code vs Codex vs Cowork):
`docs/install/README.md`. Run `brain --help` any time — the CLI is self-describing and is the one
source of truth for what's shipped.

## Updating an existing install

Already installed? Don't re-run the first-time setup — update in place.

**Check where you stand (read-only).** In your terminal:

```
brain doctor
```

prints one health + version table across every surface — engine venv, CLI
plugins, staged Cowork workspaces, marketplace cache, and the Desktop/Cowork
store — each ✅/⚠️ with the exact command to fix anything stale. It changes
nothing and exits non-zero when a required surface is behind. (Available from
v0.10.0 onward.)

**Bring everything current.** In Claude Code on the host:

```
/brainiac-update
```

now **runs** the update instead of printing a checklist: marketplace refresh →
downgrade-safe CLI-plugin reinstall → engine venv reinstall → every registered
Cowork workspace re-staged → a final `brain doctor` verify — then a before→after
version table and one pass/fail. It handles the reconciliation downgrade
(installed newer than the marketplace) automatically and never touches your
notes, audit chain, or runtime state. Prefer the terminal? `brain update` is the
same flow — add `--dry-run` to preview every decision without mutating anything.
Full detail: **`docs/install/README.md`** (§ Updating).

## Using it with a new project (second vault, third, ...)

The install is **per machine**; vaults are **per project**. You never
reinstall — you point the same `brain` at a different vault folder, and each
vault automatically gets its own index and audit chain (no configuration):

```bash
export BRAIN_VAULT=~/vaults/my-new-project   # which vault to use
brain init --full                             # once per vault: scaffold overlay etc.
brain rebuild                                 # build this vault's index
```

Full detail (per-vault overlay, the scheduled-task gotcha):
**`docs/install/second-vault.md`**.

## For technical & security teams

A plain summary of what this repo does and doesn't do, for a corporate
review:

- **All data stays on the local disk.** Notes are plain Markdown; the index
  is a local SQLite file. There is no server, no telemetry, no cloud sync,
  and the project holds **no model API keys** — the only egress is whatever
  LLM client the owner already runs.
- **Deny-by-default egress gate.** Every read command filters notes by their
  `classification` tier before printing; a note with a missing or unknown
  label is treated as most-restrictive and withheld. Scheme:
  `docs/classification-scheme.md`.
- **Signed audit chain.** Every committed write is Ed25519-signed and
  hash-chained; the key lives in the OS secret store, fail-closed (no file
  fallback). Rotation runbook and stated limitations: `SECURITY.md`.
- **Trust split.** Untrusted/sandboxed legs (the Cowork Linux VM) get a
  read-only snapshot and a draft inbox — they can never sign, index, or
  mutate the canonical store. `AGENTS.md` §6.
- **Dependencies.** The default install pulls a small, auditable set
  (onnxruntime, tokenizers, numpy, sqlite-vec, huggingface-hub,
  cryptography, PyYAML, regex — see `pyproject.toml`, which documents why
  each exists). The code degrades gracefully without any of them, so a
  constrained deployment (Intune, air-gapped) can install with
  `pip install --no-deps .` and re-add only what policy allows; an SBOM
  generator ships at `tools/generate_sbom.py`. Offline model provisioning:
  set `$BRAIN_MODEL_CACHE` to a pre-fetched model dir and no download is
  attempted.
- **License & provenance.** Apache-2.0. Built clean-room; the AGPL project
  consulted as a design reference was never forked or vendored — log and
  audit gate: `docs/clean-room-log.md`, `tools/code_origin_audit.py`.
- **Vulnerability reporting:** `SECURITY.md`. Deeper notes:
  `docs/SECURITY_NOTES.md`, `docs/operations/`.

## How the AI assistants are wired

`AGENTS.md` is the canonical instruction file. `CLAUDE.md` imports it
verbatim (`@AGENTS.md`) so Claude Code reads the same contract; Codex reads
`AGENTS.md` natively; Gemini CLI is pointed at it via `.gemini/`. All of them
call the `brain` CLI through their normal shell — **no MCP required**. The
one exception is the Claude Desktop **Chat tab** (the only surface that
can't run a command): for that, `pip install -e ".[mcp]"` adds the optional,
deletable `brain-mcp` bridge. Full matrix: `docs/harness-wiring.md`.

## More

- **`AGENTS.md`** — the conventions/schema every harness reads at startup:
  note shape, link style, capture rules, the four agent-facing verbs
  (search/get/recent/draft-capture), and the security posture.
- **`docs/install/`** — installation, starting at the
  [platform picker](docs/install/README.md) (Claude Code, Cowork — the Claude
  Desktop Linux VM sandbox client — Codex, Gemini CLI) and
  `docs/install/new-owner.md` for the five-minute "what runs where" mental model.
- **`docs/glossary.md`** — one-line definitions for the jargon used across
  these docs (PARA, MNPI, egress gate, Cowork, host-broker, overlay, ...).
- **`SECURITY.md`** — vulnerability reporting, supported versions, audit-key
  rotation.
- **`LICENSE`** — Apache-2.0.

## Layout

```
AGENTS.md            conventions + frontmatter schema
src/brain/           the brain CLI + engine (index, search, audit, ...)
docs/                specs (substrate, classification, install, security notes)
tools/validate.py    conventions validator (stdlib-only; PyYAML optional)
vault/               the tiny sample vault used in the quickstart above
  raw/   immutable captured sources
  brain/ agent-owned atomic notes + index.md + generated backlinks.md
  .brain/  per-vault runtime (published snapshot, capture inbox) — gitignored
```

## Validate

```bash
python3 tools/validate.py vault              # exit 0 = conventions clean
python3 tools/validate.py vault --backlinks  # regenerate brain/backlinks.md
python3 tools/validate.py vault --okf        # + optional OKF lint
```

## Scope note

Substrate readiness is not the same as operational cutover. This repo makes
the substrate *ready* to replace an existing tool (e.g. Obsidian + Smart
Connections) and emits the cutover hooks (`docs/corpus-migration.md`,
`docs/dependency-inventory.md`); actually retiring your old setup is a
separate, owner-specific step. See `AGENTS.md` §7.
