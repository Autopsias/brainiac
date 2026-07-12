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

## Install

Brainiac is **one engine install + one setup command.** That setup command —
`brain init --full --apply` — is the workhorse: in a single call it creates your
vault, seeds a few sample notes, builds the search index, provisions the audit
signing key, and registers nightly maintenance. Everything else in the docs
(plugins, PowerShell, the pip/uv/npx variants) is just a different way to run
those two steps. Pick the one that matches how you work:

### 1 · Let your AI assistant do it — easiest

Paste this into any assistant that can run commands on your machine (Claude
Code, Codex, Gemini CLI, …). It detects your setup, installs, verifies, and
asks you at most a question or two:

```text
Install Brainiac for me. Fetch and follow this exactly, asking me only what it says to:
https://raw.githubusercontent.com/Autopsias/brainiac/main/docs/install/LLM-INSTALL.md
```

### 2 · Claude Code plugin — one-time, then managed for you

```text
/plugin marketplace add Autopsias/brainiac
/plugin install brainiac-manager@brainiac
/brainiac-install ~/brain
```

### 3 · By hand — any OS

```bash
npx brainiac-install --vault ~/brain      # Node 18+ — installs the engine AND sets up the vault in one shot
```

…or with Python tooling (two commands):

```bash
uv tool install 'brainiac-cli[mcp]'       # or: pipx install 'brainiac-cli[mcp]'  /  pip install --user 'brainiac-cli[mcp]'
BRAIN_VAULT=~/brain/vault brain init --full --apply
```

No `uv`? Download the bootstrap script, run it, then run the `brain init` line above:

```bash
# macOS/Linux
curl -fsSL https://raw.githubusercontent.com/Autopsias/brainiac/main/install.sh -o /tmp/brainiac-install.sh && bash /tmp/brainiac-install.sh
# Windows (PowerShell)
irm https://raw.githubusercontent.com/Autopsias/brainiac/main/install.ps1 -OutFile install.ps1; .\install.ps1
```

**Then search:**

```bash
brain search "welcome" --json
```

**Good to know**

- Your **notes** live in the vault (`~/brain/vault` above — plain Markdown, the
  source of truth). The **index** is a rebuildable cache in your app-data folder
  (`brain rebuild` recreates it any time).
- Semantic search downloads its model (multilingual-e5-small, ~465 MB, one-time)
  on first use — or `brain warmup` up front.
- Pointing `brain init` at a folder that **already has notes**? It won't reindex
  a non-empty vault — run `brain rebuild` once afterward or the first search is empty.

Platform-by-platform detail: [`docs/install/README.md`](docs/install/README.md).
Run `brain --help` any time — the CLI is self-describing.

## Update

One command, whatever you installed with:

```bash
brain update            # add --dry-run to preview; never touches your notes
```

It detects your install channel (uv / pipx / pip / editable), upgrades the
engine, refreshes the Claude Code plugins if present, and verifies with
`brain doctor`. In Claude Code, `/brainiac-update` runs the same thing. Just want
a read-only health check? `brain doctor` prints a ✅/⚠️ table with the exact fix
for anything stale.

## Using it with a new project (second vault, third, ...)

The install is **per machine**; vaults are **per project**. You never
reinstall — you point the same `brain` at a different vault folder, and each
vault automatically gets its own index and audit chain (no configuration):

```bash
export BRAIN_VAULT=~/vaults/my-new-project   # which vault to use
brain init --full --apply                     # once per vault: scaffold + seed + index
```

**Pointing at a folder that already has notes?** `init --apply` seeds and
indexes only an *empty* vault — on a non-empty one it scaffolds but skips
indexing, so run `brain rebuild` once afterwards or the first search comes
back empty:

```bash
export BRAIN_VAULT=~/vaults/existing-notes
brain init --full --apply    # scaffold/validate only (vault not empty)
brain rebuild                # REQUIRED once: index the existing notes
```

Full detail (per-vault overlay, the scheduled-task gotcha):
**`docs/install/second-vault.md`**.

## For technical & security teams

**Read these three, in order** — plain-language, browser-rendered, and kept
in sync with the code:

1. [`docs/architecture-overview.html`](docs/architecture-overview.html) — how
   it's built (components, data flows, trust model).
2. [`docs/security-overview.html`](docs/security-overview.html) — the
   controls, threat model, and an **honest residual-risk list**.
3. [`docs/deployment-authorization-memo.html`](docs/deployment-authorization-memo.html)
   — the conditional-authorize decision + sign-off. Managed rollout steps:
   [`docs/managed-deployment-runbook.html`](docs/managed-deployment-runbook.html).

The one-paragraph version:

- **All data stays on the local disk.** Notes are plain Markdown; the index
  is a local SQLite file. There is no server, no telemetry, no cloud sync,
  and the project holds **no model API keys** — the only egress is whatever
  LLM client the owner already runs.
- **Deny-by-default egress gate.** Every read command filters notes by their
  `classification` tier before printing; an unlabelled note is treated as
  most-restrictive. (Note: on the trusted-host full-vault default this means
  such a note ranks as MNPI and is *surfaced*; it hides only under a narrowed
  cap — see the security overview §2.1.) Scheme: `docs/classification-scheme.md`.
- **Signed audit chain.** Every committed write is Ed25519-signed and
  hash-chained, and now binds a content hash (`verify-audit --check-content`
  detects post-commit edits). Key in the OS secret store, fail-closed.
  Rotation + limits: `SECURITY.md`.
- **Trust split.** Untrusted/sandboxed legs (the Cowork Linux VM) get a
  read-only snapshot and a draft inbox — they can never sign, index, or
  mutate the canonical store. `AGENTS.md` §6.
- **Dependencies + supply chain.** Default runtime deps: `onnxruntime`,
  `tokenizers`, `numpy`, `sqlite-vec`, `huggingface-hub`, `cryptography`,
  `PyYAML`, `regex`, plus the document parsers `pypdf`, `python-docx`,
  `python-pptx`, `openpyxl`, `Pillow` (the main third-party attack surface —
  keep patched). `requirements.lock` is the hash-pinned closure; CI
  (`.github/workflows/supply-chain.yml`) fails on lock drift, runs `pip-audit`
  weekly, and emits a CycloneDX SBOM (the provenance-rich manifest is
  `tools/generate_sbom.py`). For a managed/air-gapped install, follow the
  managed runbook (install from the lock, `$BRAIN_MODEL_CACHE` for the model,
  `$BRAIN_MANAGED=1` to disable self-update + ad-hoc key custody).
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

- **Full documentation map → [`docs/README.md`](docs/README.md)** — every
  doc grouped by what you're doing (install · understand · operate), with the
  audience and whether it's plain or technical.
- **`AGENTS.md`** — the conventions/schema every harness reads at startup:
  note shape, link style, capture rules, the four agent-facing verbs
  (search/get/recent/draft-capture), and the security posture.
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
