#!/usr/bin/env bash
# cowork_workspace_install.sh — assemble the VM-readable runtime into the
# workspace (INT-02 + INS-01). Run on the HOST. Idempotent: re-run to refresh
# binaries + republish the snapshot. The Markdown in <vault> stays the single
# source of truth; everything under <vault>/.brain/ is rebuildable from it.
#
# ONE install flow lands the full operational layer (INS-01):
#   (a) the engine        — per-arch brain ELFs into .brain/bin/
#   (b) the model cache   — bundled Arctic model into .brain/model/
#   (c) the host index    — rebuilt + published as the read-only .brain/snapshot/
#   (d) the SKILL payload  — dist/cowork-skills/*.skill into .brain/skills/ (s08)
#   (e) the task manifest — routines/manifest.json into .brain/routines/ (s07)
#   (f) brain init --full — detect client (VM), scaffold+validate the overlay,
#                           and emit the Cowork task paste-prompt (INS-02)
#
# Egress (PF-02): every artifact landed carries the engine's deny-by-default
# egress + classification + signed-audit posture; the snapshot published in (c)
# IS the immutable dated export record the export-egress gate requires
# (docs/cutover/export-egress-gate.md Leg 3).
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

echo "[install] binaries -> $BRAIN_DIR/bin/"
shipped=0
for arch in x86_64 aarch64; do
  src="$DIST/brain-linux-$arch"
  if [ -f "$src" ]; then
    cp -f "$src" "$BRAIN_DIR/bin/brain-linux-$arch"
    chmod 755 "$BRAIN_DIR/bin/brain-linux-$arch"
    shipped=$((shipped+1))
  else
    echo "[install] WARN missing $src — build it with tools/build_brain_binary.sh" >&2
  fi
done
[ "$shipped" -gt 0 ] || { echo "[install] ERROR: no ELFs found in $DIST" >&2; exit 2; }

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

echo "[install] model cache -> $BRAIN_DIR/model/ (bundled; VM has no HF egress)"
cp -a "$MODEL_SRC/." "$BRAIN_DIR/model/"

# Build the authoritative index on the host, then publish the read-only snapshot
# the VM reads. The index itself lives in app-data (never in the workspace).
echo "[install] rebuild host index + publish snapshot"
export BRAIN_VAULT="$VAULT"
export BRAIN_MODEL_CACHE="$BRAIN_DIR/model"
PYTHONPATH="$REPO/src" python3 -m brain.cli rebuild
PYTHONPATH="$REPO/src" python3 -m brain.cli snapshot --dest "$BRAIN_DIR/snapshot" \
  --json | tee "$BRAIN_DIR/snapshot/export-snapshot.json"

# (d) SKILL payload (s08). Build the Cowork .skill zips if absent, then land
# them read-only for the analyst to upload via Cowork's Save-skill flow.
echo "[install] skills -> $BRAIN_DIR/skills/"
COWORK_SKILLS="$DIST/cowork-skills"
[ -d "$COWORK_SKILLS" ] || COWORK_SKILLS="$REPO/dist/cowork-skills"
if ! ls "$COWORK_SKILLS"/*.skill >/dev/null 2>&1; then
  echo "[install] no .skill zips found — building via tools/package_clients.py"
  PYTHONPATH="$REPO/src" python3 "$REPO/tools/package_clients.py" >/dev/null
  COWORK_SKILLS="$REPO/dist/cowork-skills"
fi
skills_shipped=0
for s in "$COWORK_SKILLS"/*.skill; do
  [ -f "$s" ] || continue
  cp -f "$s" "$BRAIN_DIR/skills/"
  skills_shipped=$((skills_shipped+1))
done
echo "[install] shipped $skills_shipped skill bundle(s)"

# (e) task manifest (s07) — the host/VM-aware scheduled-task manifest the
# registrar + `brain init` consume.
echo "[install] task manifest -> $BRAIN_DIR/routines/manifest.json"
cp -f "$REPO/routines/manifest.json" "$BRAIN_DIR/routines/manifest.json"

# (f) brain init --full (INS-02). Run as the VM/Cowork client (BRAIN_ROLE=vm)
# so it scaffolds+validates the overlay in the workspace vault and emits the
# idempotent Cowork task paste-prompt (the VM leg registers nothing itself —
# persistence-budget.md locks the VM OS-scheduled count at 0). No host mutation.
echo "[install] brain init --full (client=cowork/VM)"
PYTHONPATH="$REPO/src" BRAIN_ROLE=vm python3 -m brain.cli --vault "$VAULT" init --full \
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
    skills/    $skills_shipped .skill bundle(s) to upload via Cowork Save-skill
    routines/  manifest.json + cowork-registrar-prompt.md + brain-init-report.json
    overlay/   scaffolded + validated (edit voice/brand/keywords/people to personalize)

Next steps:
  1. Upload the skill bundles in $BRAIN_DIR/skills/ via Cowork's Save-skill flow
     (kernel first; extras optional — see .claude/skills/setup-cowork).
  2. Paste $BRAIN_DIR/routines/cowork-registrar-prompt.md into a Cowork chat with
     the scheduled-tasks MCP to register the poke-only task triggers.
  3. Per Cowork session, paste the bootstrap (see docs/cowork-windows-install.md):
       export BRAIN_VAULT="\$PWD/vault"; export BRAIN_ROLE=vm
       export BRAIN_RUNTIME_DIR="\$BRAIN_VAULT/.brain"
       export BRAIN_MODEL_CACHE="\$BRAIN_RUNTIME_DIR/model"
       ln -sf "bin/brain-linux-\$(uname -m)" "\$BRAIN_RUNTIME_DIR/brain"
       export PATH="\$BRAIN_RUNTIME_DIR:\$PATH"
       brain status
EOF
