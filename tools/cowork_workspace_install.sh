#!/usr/bin/env bash
# cowork_workspace_install.sh — assemble the VM-readable runtime into the
# workspace (INT-02). Run on the HOST. Idempotent: re-run to refresh binaries +
# republish the snapshot. The Markdown in <vault> stays the single source of
# truth; everything under <vault>/.brain/ is rebuildable from it.
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
mkdir -p "$BRAIN_DIR/bin" "$BRAIN_DIR/model" "$BRAIN_DIR/snapshot" "$BRAIN_DIR/capture-inbox"

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

echo "[install] model cache -> $BRAIN_DIR/model/ (bundled; VM has no HF egress)"
cp -a "$MODEL_SRC/." "$BRAIN_DIR/model/"

# Build the authoritative index on the host, then publish the read-only snapshot
# the VM reads. The index itself lives in app-data (never in the workspace).
echo "[install] rebuild host index + publish snapshot"
export BRAIN_VAULT="$VAULT"
export BRAIN_MODEL_CACHE="$BRAIN_DIR/model"
PYTHONPATH="$REPO/src" python3 -m brain.cli rebuild
PYTHONPATH="$REPO/src" python3 -m brain.cli snapshot --dest "$BRAIN_DIR/snapshot"

cat <<EOF

[install] done. Workspace runtime assembled at:
  $BRAIN_DIR
Per Cowork session, paste the bootstrap (see docs/cowork-windows-install.md):
  export BRAIN_VAULT="\$PWD/vault"; export BRAIN_ROLE=vm
  export BRAIN_RUNTIME_DIR="\$BRAIN_VAULT/.brain"
  export BRAIN_MODEL_CACHE="\$BRAIN_RUNTIME_DIR/model"
  ln -sf "bin/brain-linux-\$(uname -m)" "\$BRAIN_RUNTIME_DIR/brain"
  export PATH="\$BRAIN_RUNTIME_DIR:\$PATH"
  brain status
EOF
