# Codex CLI — install and first run

**Role:** `host` (default) — full read + write + maintenance, identical
capability set to Claude Code CLI. **Reach path:** native shell — Codex calls
`brain …` directly, no MCP. Full matrix: `docs/cutover/client-access-model.md`.

## 1 — Clone

```bash
git clone <this-repo-url> profile-a-brain
cd profile-a-brain
```

## 2 — Put `brain` on `PATH`

Identical to the Claude Code CLI step — same binary, same install path:

```bash
pip install -e .                              # minimal — just the CLI
# OR, for the full stack:
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[corporate,eval,audit]
```

## 3 — Skills auto-detect — nothing to do

Codex natively scans `$REPO_ROOT/.agents/skills/` on a **trusted** project —
no config entry is needed to enable auto-load (verified against the
published Codex Agent Skills docs, per `docs/operations/cutover-s08-evidence.md`
SKL-03). `.agents/skills/` mirrors `.claude/skills/` **minus** the
Cowork-only `setup-cowork` skill (correctly excluded — Codex never runs in
the Cowork VM):

```bash
ls .agents/skills/
# curation  improve  kb-curator  promote  save-conversation  task-registrar  vault-eval  vault-ingestion
```

`AGENTS.md` (Codex's native startup convention file) is already canonical —
read at session start, no separate action needed. `.codex/config.toml`
carries project-scoped sandbox/approval defaults (loaded only once the
project is trusted); it does **not** control skill loading — that part is
automatic per the point above.

## 4 — Point at your vault and enforce the real embedder

```bash
export BRAIN_VAULT=/path/to/your/vault
export BRAIN_REQUIRE_REAL_EMBEDDER=1
```

Same rationale as the Claude Code guide — a missing `onnxruntime` silently
degrades to `HashEmbedder` otherwise (`docs/cutover/dual-run-parity.md`).

## 5 — First run: `brain init --full`

Same command, same behaviour as the Claude Code CLI guide (the engine is
identical — Codex and Claude Code CLI share the same shell reach path):

```bash
brain init --full --apply
```

Registers the one sanctioned host OS task (`brain-nightly`) via
`task-registrar`. If you already ran this from Claude Code CLI on the same
machine, re-running from Codex is safe and idempotent — the registrar
re-points the existing registration rather than creating a duplicate.

## 6 — Verify

```bash
brain status --json
brain search "<something in your vault>" --json
```

Same expectations as the Claude Code guide (note/chunk counts,
`vector_backend: sqlite-vec`, `is_hash_fallback: false`, egress-gated JSON).

## Cross-references

- `docs/cutover/client-access-model.md` — full access matrix (all clients)
- `docs/cutover/brain-cli-verbs.md` — every verb, VM-allowlist, egress tiers
- `docs/harness-wiring.md` §"Per-harness discovery" — Codex row + why no MCP
- `AGENTS.md` — the canonical conventions file Codex reads at startup
