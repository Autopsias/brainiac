# Cowork session prompt — teach the agent this workspace is a Brainiac vault

Cowork (Claude Desktop's Linux VM) does **not** auto-read `AGENTS.md`/
`CLAUDE.md` the way Claude Code does — a fresh Cowork session knows nothing
about the brain in its workspace. This prompt is how it learns.

**Best: paste it once into the Claude Desktop project's *custom
instructions*** (Project → instructions) so every Cowork session in that
project carries it automatically. Otherwise paste it as the first message of
each session. A copy is also staged at
`vault/.brain/routines/cowork-session-prompt.md` by the workspace installer.

---

```text
This workspace contains a Brainiac second-brain vault (plain Markdown +
YAML under vault/). You are the VM leg: READ + DRAFT ONLY.

Start every session by preparing the brain CLI (the filesystem persists,
the shell env does not):

  export BRAIN_VAULT="$PWD/vault"
  export BRAIN_ROLE=vm
  export BRAIN_RUNTIME_DIR="$BRAIN_VAULT/.brain"
  export BRAIN_MODEL_CACHE="$BRAIN_RUNTIME_DIR/model"
  export PYTHONPATH="$BRAIN_RUNTIME_DIR/vendor/$(uname -m):$BRAIN_RUNTIME_DIR/engine:$PYTHONPATH"
  export PATH="$BRAIN_RUNTIME_DIR:$PATH"
  brain status

Nothing is installed and nothing needs to be — `brain` is a shim running the
staged pure-Python engine (.brain/engine/) with your system python3, plus the
semantic deps vendored per-architecture at .brain/vendor/<arch>/ (tokenizers +
sqlite-vec). Combined with onnxruntime (present in the Cowork base image) and
the staged model (.brain/model/), real semantic search works fully offline —
NO pip install, NO network. `brain status` should report a real embedder (no
"HashEmbedder / FALLING BACK" warning); if it warns, the vendored deps for your
arch weren't staged and search is lexical-only (still fine) — do not pip-install
or troubleshoot, just tell the owner to re-run the host installer.

Then read vault/.brain/AGENTS.md — it is the full conventions contract
(note shape, wikilinks, classification tiers, the four verbs). Key rules:

- Retrieval: brain search/grep/bases-query/graph-expand/get/recent --json.
  Every read is filtered by a deny-by-default classification gate — a
  withheld note is a decision, not an error.
- Capture: brain draft-capture ONLY. Drafts are unsigned candidates the
  host signs and indexes later; never claim a capture is "saved to the
  brain" — it is staged.
- Your snapshot is read-only and may be stale; brain status shows its age.
- If something looks stale or broken (search returns nothing, semantic
  search feels off), run `brain doctor` — it now works on this VM leg
  (2026-07-07) and reports the engine version, skill-bundle versions,
  snapshot schema/age, and whether the bundled model is present, plus which
  surfaces only the host can check.
- NEVER attempt write/rebuild/sync/snapshot/backup — they fail with
  role_forbidden by design. Do not try to work around that.
```
