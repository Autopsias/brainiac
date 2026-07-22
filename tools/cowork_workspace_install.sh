#!/usr/bin/env bash
# cowork_workspace_install.sh — assemble the VM-readable runtime into the
# workspace (INT-02 + INS-01). Run on the HOST. Idempotent: re-run to refresh
# binaries + republish the snapshot. The Markdown in <vault> stays the single
# source of truth; everything under <vault>/.brain/ is rebuildable from it.
#
# ONE install flow lands the full operational layer (INS-01):
#   (a) the engine        — per-arch brain ELFs into .brain/bin/
#   (b) the model cache   — bundled Arctic model into .brain/model/
#   (c) the host index    — reconciled (sync when current, full rebuild only on
#                            schema/embedder mismatch or an absent/corrupt index)
#                            + published as the read-only .brain/snapshot/
#   (d) the SKILL payload  — dist/cowork-skills/*.skill into .brain/skills/,
#                            REBUILT at the current version on every run and
#                            version-verified against the engine (s08, cw-02)
#   (e) the task manifest — routines/manifest.json into .brain/routines/ (s07)
#   (f) brain init --full — detect client (VM), scaffold+validate the overlay,
#                           and emit the Cowork task paste-prompt (INS-02)
#
# Egress (PF-02): every artifact landed carries the engine's deny-by-default
# egress + classification + signed-audit posture; the snapshot published in (c)
# IS the immutable dated export record the export-egress gate requires
# (docs/operations/egress-provider-posture.md Leg 3).
#
# Usage:
#   tools/cowork_workspace_install.sh <vault-dir> <model-cache-dir> [dist-dir]
#
#   <vault-dir>        the workspace vault/ (holds brain/ raw/)
#   <model-cache-dir>  a fastembed cache dir already containing the Arctic model
#                      (model.onnx + tokenizer/config); copied into .brain/model/
#   [dist-dir]         where the built ELFs live (default: dist/)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VAULT="${1:?usage: cowork_workspace_install.sh <vault-dir> <model-cache-dir> [dist-dir]}"
MODEL_SRC="${2:?missing <model-cache-dir>}"
DIST="${3:-$REPO/dist}"

VAULT="$(cd "$VAULT" && pwd)"
BRAIN_DIR="$VAULT/.brain"
mkdir -p "$BRAIN_DIR/bin" "$BRAIN_DIR/model" "$BRAIN_DIR/snapshot" \
         "$BRAIN_DIR/capture-inbox" "$BRAIN_DIR/skills" "$BRAIN_DIR/routines"

# (a) the engine, ZERO-INSTALL by default: the brain package is pure Python
# with stdlib-only graceful degradation, so the VM runs it STRAIGHT FROM a
# staged source copy (python3 -m brain.cli) — nothing is installed in the VM.
# Dense query embedding needs the VENDORED wheels this installer stages below
# via tools/vendor_semantic_deps.py (onnxruntime/numpy/tokenizers/sqlite-vec,
# pinned to the VM's Python 3.10 ABI — the VM has NO network and the base
# image ships none of them; in-VM pip is not a thing). Without them the read
# verbs run lexical-first (BM25/grep/frontmatter), which is the design.
echo "[install] engine source -> $BRAIN_DIR/engine/brain/"
rm -rf "$BRAIN_DIR/engine"
mkdir -p "$BRAIN_DIR/engine"
cp -R "$REPO/src/brain" "$BRAIN_DIR/engine/brain"
find "$BRAIN_DIR/engine" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cat > "$BRAIN_DIR/brain" <<'SHIM'
#!/bin/sh
# zero-install shim: run the staged brain engine from source, with the ENGINE
# first on the path and the vendored semantic deps (tokenizers/sqlite-vec,
# staged per-arch below) AFTER it, so real query embedding works offline in the
# VM (DV-04) while an untrusted vendored wheel cannot shadow the engine
# (codex 2026-07-19). Keep in lockstep with tools/vendor_semantic_deps.py.
DIR="$(cd "$(dirname "$0")" && pwd)"
ARCH="$(uname -m)"
PYTHONPATH="$DIR/engine:$DIR/vendor/$ARCH${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m brain.cli "$@"
SHIM
chmod 755 "$BRAIN_DIR/brain"

# Verify the staged engine carries the COMMITTED version stamp (ADR-0005
# Ruling 1). The stamp is git-tracked and written by the release pipeline —
# the cp -R above carries it; if it's missing, the VM would report
# 0.0.0+unknown on every surface, which is exactly the defect DV-01 fixed.
STAGED_VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$BRAIN_DIR/engine/brain/_version.py" 2>/dev/null || true)"
if [ -z "$STAGED_VERSION" ]; then
  echo "[install] ERROR: staged engine has no version stamp" \
       "($BRAIN_DIR/engine/brain/_version.py missing or malformed)." >&2
  echo "          Run tools/package_clients.py in the checkout and retry." >&2
  exit 1
fi
echo "[install] staged engine version: $STAGED_VERSION"

echo "[install] frozen ELFs (optional, for VMs without python3) -> $BRAIN_DIR/bin/"
shipped=0
for arch in x86_64 aarch64; do
  src="$DIST/brain-linux-$arch"
  if [ -f "$src" ]; then
    cp -f "$src" "$BRAIN_DIR/bin/brain-linux-$arch"
    chmod 755 "$BRAIN_DIR/bin/brain-linux-$arch"
    shipped=$((shipped+1))
  fi
done
[ "$shipped" -gt 0 ] || echo "[install] note: no frozen ELFs staged (fine — the VM runs the staged source via .brain/brain; ELFs are only for VMs without python3)"

if [ "$shipped" -gt 0 ]; then
echo "[install] writing SHA256SUMS -> $BRAIN_DIR/bin/SHA256SUMS"
# Supply-chain manifest (hardening pass): tools/cowork_session_bootstrap.sh
# verifies these hashes on the VM leg BEFORE trusting/symlinking a binary.
# Regenerated on every install, so a re-run after rebuilding the ELFs always
# ships a manifest matching what's actually in bin/.
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$BRAIN_DIR/bin" && sha256sum brain-linux-* > SHA256SUMS )
elif command -v shasum >/dev/null 2>&1; then
  ( cd "$BRAIN_DIR/bin" && shasum -a 256 brain-linux-* > SHA256SUMS )
else
  echo "[install] WARN: neither sha256sum nor shasum found — no SHA256SUMS" \
       "manifest written; the VM leg's integrity check will skip-with-warning" >&2
fi
fi

echo "[install] model cache -> $BRAIN_DIR/model/ (bundled; VM has no HF egress)"
# -RL dereferences: an HF-cache snapshot dir is all symlinks into ../../blobs/,
# so copying links verbatim (cp -a) stages a model made of dangling symlinks.
cp -RL "$MODEL_SRC/." "$BRAIN_DIR/model/"
DANGLING="$(find -L "$BRAIN_DIR/model" -type l 2>/dev/null)"
if [ -n "$DANGLING" ]; then
  echo "[install] ERROR: dangling symlinks in staged model cache:" >&2
  echo "$DANGLING" >&2
  exit 1
fi
if [ -z "$(find "$BRAIN_DIR/model" -name 'model*.onnx' -size +1M 2>/dev/null | head -1)" ]; then
  echo "[install] ERROR: no model*.onnx (>1MB) in staged model cache — check <model-cache-dir>" >&2
  exit 1
fi

# Vendored semantic deps (tokenizers/sqlite-vec) are staged below via the shared
# tools/vendor_semantic_deps.py helper — the SAME code `brain update` uses, so
# install and update never drift (DV-04). It runs after HOST_PY is resolved
# (it needs a networked interpreter), just before the snapshot build.

# Build the authoritative index on the host, then publish the read-only snapshot
# the VM reads. The index itself lives in app-data (never in the workspace).
# Use the install venv's interpreter when present: bare system python3 lacks
# onnxruntime/tokenizers, silently falls back to the HASH embedder, and bakes
# non-semantic vectors into the published snapshot — VM semantic search would
# then be garbage regardless of what's installed in the sandbox.
HOST_PY="python3"
if [ -x "$HOME/.brainiac/venv/bin/python3" ]; then
  HOST_PY="$HOME/.brainiac/venv/bin/python3"
else
  echo "[install] WARNING: ~/.brainiac/venv not found — using system python3." >&2
  echo "          If it lacks onnxruntime, the snapshot is built with HASH (non-semantic)" >&2
  echo "          vectors. Run ./install.sh first, then re-run this script." >&2
fi
# (c3) VENDOR the offline semantic deps + refresh the shim via the shared helper
# (DV-04) — the SAME code path `brain update` re-stages with, so the two can't
# drift. Advisory: a networkless host just leaves the arch lexical-only.
echo "[install] vendoring semantic deps (tokenizers, sqlite-vec) + shim -> $BRAIN_DIR/vendor/"
"$HOST_PY" "$REPO/tools/vendor_semantic_deps.py" "$BRAIN_DIR" || \
  echo "[install]   WARNING: vendoring reported an issue — VM may run lexical-only" >&2

echo "[install] reconcile host index + publish snapshot (interpreter: $HOST_PY)"
export BRAIN_VAULT="$VAULT"
export BRAIN_MODEL_CACHE="$BRAIN_DIR/model"
# Staleness-aware (2026-07-16 field report): `sync` is already a full rebuild
# when the index is absent or its schema_version/embed_model meta mismatch the
# live engine (BrainIndex.sync's IDX-01/IDX-03 guard), and an incremental
# path+hash reconcile otherwise — a re-stage against an already-current index
# (the common case) drops from a ~1h full re-embed to seconds/minutes instead
# of always paying the full-rebuild cost. A hard failure (e.g. a corrupt index
# file `sync` can't even open) falls back to an explicit full `rebuild`.
if ! PYTHONPATH="$REPO/src" "$HOST_PY" -m brain.cli sync; then
  echo "[install]   sync failed (index missing/corrupt beyond sync's own guard) — falling back to full rebuild" >&2
  PYTHONPATH="$REPO/src" "$HOST_PY" -m brain.cli rebuild
fi
PYTHONPATH="$REPO/src" "$HOST_PY" -m brain.cli snapshot --dest "$BRAIN_DIR/snapshot" \
  --json | tee "$BRAIN_DIR/snapshot/export-snapshot.json"

# (c2) VERIFY the offline semantic stack the VM will use (DV-03/DV-04). The VM
# queries through the shim's python3 (pinned Python 3.10), which needs the
# vendored onnxruntime + numpy + tokenizers + sqlite-vec staged above (the
# Cowork base image ships NONE of them — field finding 2026-07-13) and the
# staged model. We can't run the VM's python from here, so we verify what the
# installer actually controls — that the vendored deps landed at the right ABI
# (vendor_semantic_deps.py refuses non-cp310/abi3 wheels). `brain doctor` /
# `brain status` on the VM leg confirm the LIVE embedder at session time (and the
# VM fails closed on a dead embedder, never returning random hash results).
echo "[install] verifying vendored semantic deps ..."
missing=""
for arch in aarch64 x86_64; do
  if [ -f "$BRAIN_DIR/vendor/$arch/tokenizers/tokenizers.abi3.so" ] \
     && [ -f "$BRAIN_DIR/vendor/$arch/sqlite_vec/vec0.so" ] \
     && [ -d "$BRAIN_DIR/vendor/$arch/onnxruntime" ] \
     && [ -d "$BRAIN_DIR/vendor/$arch/numpy" ]; then :; else
    missing="$missing $arch"
  fi
done
if [ -z "$missing" ]; then
  echo "[install]   vendored onnxruntime + numpy + tokenizers + sqlite-vec present (aarch64, x86_64)"
else
  echo "[install] WARNING: vendored semantic deps missing for:$missing" >&2
  echo "          The VM runs lexical-only there (grep/bases-query still work) until" >&2
  echo "          re-staged. Re-run this installer on a networked host to fetch them." >&2
  echo "          The VM leg fails closed (BRAIN_REQUIRE_REAL_EMBEDDER), so it errors" >&2
  echo "          loudly rather than returning random hash results." >&2
fi

# (d) SKILL payload (s08, refreshed unconditionally per cw-02). ALWAYS
# rebuild via tools/package_clients.py — not "only if absent" — so a re-stage
# after a version bump ships the CURRENT-version zips, not whatever was left
# over from the last install. package_clients.py is idempotent/fast (stdlib
# zipfile, no network), so this costs nothing on a re-run.
echo "[install] skills -> $BRAIN_DIR/skills/ (rebuilding current-version .skill bundles)"
PYTHONPATH="$REPO/src" python3 "$REPO/tools/package_clients.py" >/dev/null
COWORK_SKILLS="$REPO/dist/cowork-skills"
if ! ls "$COWORK_SKILLS"/*.skill >/dev/null 2>&1; then
  echo "[install] ERROR: tools/package_clients.py ran but produced no .skill zips in $COWORK_SKILLS" >&2
  exit 1
fi
skills_shipped=0
for s in "$COWORK_SKILLS"/*.skill; do
  [ -f "$s" ] || continue
  cp -f "$s" "$BRAIN_DIR/skills/"
  skills_shipped=$((skills_shipped+1))
done
echo "[install] shipped $skills_shipped skill bundle(s)"

# Verify the staged skill bundles carry the SAME version stamp as the staged
# engine (cw-02) — both are written from the same pyproject SSOT in the same
# install pass, so a mismatch here means the packager and the engine copy
# disagree, which should stop the install rather than ship a split version.
SKILL_SAMPLE="$BRAIN_DIR/skills/$(basename "$(ls "$BRAIN_DIR/skills"/*.skill | head -1)")"
SKILL_SAMPLE_NAME="$(basename "$SKILL_SAMPLE" .skill)"
STAGED_SKILL_VERSION="$(python3 -c "
import zipfile, sys
with zipfile.ZipFile(sys.argv[1]) as zf:
    print(zf.read(sys.argv[2] + '/VERSION').decode().strip())
" "$SKILL_SAMPLE" "$SKILL_SAMPLE_NAME" 2>/dev/null || true)"
if [ -z "$STAGED_SKILL_VERSION" ]; then
  echo "[install] ERROR: could not read VERSION from staged skill bundle $SKILL_SAMPLE" >&2
  exit 1
fi
if [ "$STAGED_SKILL_VERSION" != "$STAGED_VERSION" ]; then
  echo "[install] ERROR: staged skill bundle version ($STAGED_SKILL_VERSION) != staged engine version" \
       "($STAGED_VERSION) — packager and engine disagree, aborting." >&2
  exit 1
fi
echo "[install] staged skill bundle version: $STAGED_SKILL_VERSION (matches engine)"

# (e) task manifest (s07) — the host/VM-aware scheduled-task manifest the
# registrar + `brain init` consume.
echo "[install] task manifest -> $BRAIN_DIR/routines/manifest.json"
cp -f "$REPO/routines/manifest.json" "$BRAIN_DIR/routines/manifest.json"

# (e2) the conventions contract + session prompt. Cowork DOES auto-load a
# workspace-root CLAUDE.md at session start (verified 2026-07: Cowork shares
# Claude Code's claudeMd loader; the old "no auto-read from a mounted folder"
# assumption here was stale) — so the contract is staged BOTH ways:
#   - <workspace-root>/CLAUDE.md  → auto-loaded, always-on (primary channel);
#     carries a first-reply marker so every session proves the load fired
#   - $BRAIN_DIR/AGENTS.md + session prompt → the paste-it-yourself fallback
#     if a given Cowork build doesn't auto-load (custom instructions / first
#     message), unchanged from before
echo "[install] conventions contract -> $BRAIN_DIR/AGENTS.md"
cp -f "$REPO/AGENTS.md" "$BRAIN_DIR/AGENTS.md"
WORKSPACE_ROOT="$(cd "$VAULT/.." && pwd)"
# The contract must be INLINED (verified 2026-07-20: Cowork auto-loads a
# workspace-root CLAUDE.md but does NOT expand @-imports). Three cases:
#   1. CLAUDE.md carries a BRAIN-CONTRACT marker block → re-sync just the
#      block from the freshly staged contract (hand-maintained notes outside
#      the block survive every refresh).
#   2. CLAUDE.md exists but is hand-maintained with no marker block → never
#      clobber; tell the operator how to opt in.
#   3. No CLAUDE.md → generate the full file (banner + marker block).
if [ -f "$WORKSPACE_ROOT/CLAUDE.md" ] && \
   grep -q "BEGIN BRAIN-CONTRACT" "$WORKSPACE_ROOT/CLAUDE.md"; then
  echo "[install] re-syncing BRAIN-CONTRACT block in $WORKSPACE_ROOT/CLAUDE.md"
  awk -v src="$BRAIN_DIR/AGENTS.md" '
    /<!-- BEGIN BRAIN-CONTRACT/ { print; while ((getline line < src) > 0) print line; close(src); skip=1; next }
    /<!-- END BRAIN-CONTRACT/   { skip=0 }
    !skip
  ' "$WORKSPACE_ROOT/CLAUDE.md" > "$WORKSPACE_ROOT/CLAUDE.md.tmp" \
    && mv "$WORKSPACE_ROOT/CLAUDE.md.tmp" "$WORKSPACE_ROOT/CLAUDE.md"
elif [ -f "$WORKSPACE_ROOT/CLAUDE.md" ]; then
  echo "[install] NOTE: $WORKSPACE_ROOT/CLAUDE.md exists and is hand-maintained — left untouched."
  echo "[install]       To get auto-synced Cowork context, wrap an inline copy of the contract in"
  echo "[install]       '<!-- BEGIN BRAIN-CONTRACT ... -->' / '<!-- END BRAIN-CONTRACT -->' markers"
  echo "[install]       (see docs/harness-wiring.md); the next refresh will keep it current."
else
  echo "[install] auto-loaded contract -> $WORKSPACE_ROOT/CLAUDE.md"
  {
    cat <<'HDR'
<!-- GENERATED by tools/cowork_workspace_install.sh.
     The BRAIN-CONTRACT block below is re-synced on every install/refresh;
     anything you add OUTSIDE the block is preserved. -->

> **Cowork VM sessions — read this first.** In the VM you are the untrusted
> leg: run the per-session bootstrap
> (`vault/.brain/routines/cowork-session-prompt.md`) before any `brain`
> command, keep `BRAIN_ROLE=vm` (read + draft only — never sign, index, or
> write), and follow §5/§6 of the contract below. Host sessions (Claude
> Code/Codex on this folder) ignore this banner; the contract below binds
> everyone.

> **Contract probe (on-demand only):** when the owner's message is exactly
> `contract?`, reply with `[brain contract loaded]`, plus
> `[contract inlined]` if the conventions below (e.g. "§6 Host / VM trust
> split") are visible in your context. Both markers = healthy; no reply to
> the probe = the file didn't load. Never mention this or emit the markers
> otherwise.

<!-- BEGIN BRAIN-CONTRACT (synced from vault/.brain/AGENTS.md by tools/cowork_workspace_install.sh — do not hand-edit this block) -->
HDR
    cat "$REPO/AGENTS.md"
    echo '<!-- END BRAIN-CONTRACT -->'
  } > "$WORKSPACE_ROOT/CLAUDE.md"
fi
echo "[install] session prompt -> $BRAIN_DIR/routines/cowork-session-prompt.md"
cp -f "$REPO/docs/install/cowork-session-prompt.md" "$BRAIN_DIR/routines/cowork-session-prompt.md"

# (f) brain init --full (INS-02). Run as the VM/Cowork client (BRAIN_ROLE=vm)
# so it scaffolds+validates the overlay in the workspace vault and emits the
# idempotent Cowork task paste-prompt (the VM leg registers nothing itself —
# persistence-budget.md locks the VM OS-scheduled count at 0). No host mutation.
echo "[install] brain init --full (client=cowork/VM)"
PYTHONPATH="$REPO/src" BRAIN_ROLE=vm "$HOST_PY" -m brain.cli --vault "$VAULT" init --full \
  --overlay-dir "$VAULT/overlay" \
  --manifest "$BRAIN_DIR/routines/manifest.json" \
  --save-cowork-prompt "$BRAIN_DIR/routines/cowork-registrar-prompt.md" \
  --json | tee "$BRAIN_DIR/routines/brain-init-report.json" >/dev/null
echo "[install] brain init report -> $BRAIN_DIR/routines/brain-init-report.json"
echo "[install] cowork task paste-prompt -> $BRAIN_DIR/routines/cowork-registrar-prompt.md"

cat <<EOF

[install] done. Full operational layer assembled at:
  $BRAIN_DIR
    bin/       per-arch brain ELFs (the engine)
    model/     bundled Arctic model (VM has no HF egress)
    snapshot/  read-only index snapshot + export-snapshot.json (PF-02 record)
    skills/    $skills_shipped .skill bundle(s), version $STAGED_SKILL_VERSION (matches engine)
    routines/  manifest.json + cowork-registrar-prompt.md + brain-init-report.json
    overlay/   scaffolded + validated (edit voice/brand/keywords/people to personalize)

This ONE command just refreshed the engine AND the skills together (cw-02) —
re-run it any time to pick up a new release; there is no separate skill
reinstall step required.

Next steps:
  1. Skills are current in $BRAIN_DIR/skills/. Upload them via Cowork's
     Save-skill flow if you haven't already (kernel first; extras optional —
     see .claude/skills/setup-cowork). If your workspace already has the
     Cowork Plugins-tab marketplace synced (Customize -> Plugins ->
     Autopsias/brainiac), that stays current on its own via
     /plugin marketplace update — the Save-skill upload here is the
     always-guaranteed fallback that never depends on Cowork's own
     marketplace-sync feature (docs/adr/0005-update-versioning-ux.md
     Ruling 4 + addendum).
  2. Paste $BRAIN_DIR/routines/cowork-registrar-prompt.md into a Cowork chat with
     the scheduled-tasks MCP to register the poke-only task triggers.
  3. The conventions contract is now auto-loaded: Cowork reads the staged
     CLAUDE.md at the workspace root on session start. To verify at any
     time, send the message "contract?" in a Cowork session — the reply
     must be "[brain contract loaded] [contract inlined]". If the probe
     gets no markers, fall back to the old channel: put the prompt block from
     $BRAIN_DIR/routines/cowork-session-prompt.md into the Claude Desktop
     project's CUSTOM INSTRUCTIONS (once per project) — or paste it as the
     first message of each Cowork session. It bootstraps the env and points
     the agent at $BRAIN_DIR/AGENTS.md (the conventions contract).
EOF
