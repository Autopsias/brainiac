# Claude Code CLI — install and first run

**Role:** `host` (default) — full read + write + maintenance. **Reach path:**
native shell (`Bash` tool calls `brain …` directly — no MCP, no plugin, no
daemon). Full matrix: `docs/cutover/client-access-model.md`.

## 1 — Clone

```bash
git clone <this-repo-url> profile-a-brain
cd profile-a-brain
```

## 2 — Put `brain` on `PATH`

```bash
pip install -e .                              # minimal — just the CLI
# OR, for the full stack (real ONNX embedder, sqlite-vec, audit signing):
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[corporate,eval,audit]
```

`corporate` extras = `onnxruntime` + `tokenizers` + `sqlite-vec` +
`cryptography` + `huggingface-hub` + `PyYAML` (`pyproject.toml`). Without it,
`get_embedder("auto")` degrades to a non-semantic `HashEmbedder` — set the
guard rail in the next step so that degradation is never silent.

Prebuilt binary alternative (no venv): `dist/brain/brain` (macOS arm64
Mach-O) or invoke via `PYTHONPATH=src python3 -c "from brain.cli import main; main()"`
during development.

## 3 — Skills auto-load — nothing to do

`.claude/skills/` (9 skills: `kb-curator`, `promote`, `vault-ingestion`,
`vault-eval`, `save-conversation`, `curation`, `improve`, `task-registrar`,
`setup-cowork`) is discovered automatically by Claude Code on clone. No
install step. Confirm with:

```bash
ls .claude/skills/
```

Optional — the versioned marketplace (for a user not working inside a clone
of this repo, or a team that wants the extras plugin one command away):

```bash
/plugin marketplace add Autopsias/profile-a-brain
/plugin install profile-a-kernel@profile-a-marketplace     # always-useful daily skills
/plugin install profile-a-extras@profile-a-marketplace     # optional: curation/improve/task-registrar
```

## 4 — Point at your vault and enforce the real embedder

```bash
export BRAIN_VAULT=/path/to/your/vault
export BRAIN_REQUIRE_REAL_EMBEDDER=1     # fail loud, not silent, if the ONNX embedder is unavailable
```

Without this env var, a partial install can silently answer every query with
`HashEmbedder` (a deterministic hash, not semantic search) while `brain
status`/`brain health` still *report* the intended model name — see
`docs/cutover/dual-run-parity.md`'s "Environment note" for the exact failure
mode this guards against.

## 5 — First run: `brain init --full`

```bash
brain init --full
```

This (per `docs/cutover/cutover-s09-evidence.md` INS-02):
1. Detects the client from the trust role (`host` here).
2. Scaffolds the generic `overlay/{voice,brand,keywords,people}/` layer from
   the shipped template — **idempotent**, never clobbers a category you've
   already filled.
3. Validates the overlay shape.
4. Registers the single sanctioned host OS task (`brain-nightly`) via the
   `task-registrar` skill — read-only probe by default; add `--apply` to
   actually create/update the `launchd` (macOS) or Task Scheduler (Windows)
   entry:

```bash
brain init --full --apply
```

Use `--no-register-tasks` to scaffold+validate the overlay only, without
touching the scheduler (handy for a dry look before committing to a
scheduled entry).

## 6 — Verify

```bash
brain status --json
```

Expect: your note/chunk counts, `vector_backend: sqlite-vec`,
`embed_model: intfloat/multilingual-e5-small`, and (once the s11 hardening
lands in your build) a `live_embedder` block with `is_hash_fallback: false`.

```bash
brain search "<something in your vault>" --json
```

Expect valid JSON, egress-gated at the default `--max-tier Internal`.

## Cross-references

- `docs/cutover/client-access-model.md` — full access matrix (all clients)
- `docs/cutover/brain-cli-verbs.md` — every verb, VM-allowlist, egress tiers
- `docs/harness-wiring.md` — why no MCP for shell-capable clients
- `overlay/README.md` + `docs/cutover/overlay-migration.md` — the personalization layer
- `.claude/skills/task-registrar/SKILL.md` — the registrar this step drives
