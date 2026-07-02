#!/usr/bin/env bash
# build_brain_binary.sh — build a self-contained `brain` Linux ELF (INT-02).
#
# The Cowork VM has no Python toolchain, so `brain` ships as a frozen binary.
# This builds the binary for the CURRENT arch via PyInstaller. To get BOTH
# Linux arches (x86_64 + aarch64) that Cowork VMs run, drive this from a CI
# matrix (two runners) or under qemu/buildx — see "Cross-arch" below.
#
# Usage:  tools/build_brain_binary.sh [outdir]      (default outdir: dist/)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUTDIR="${1:-$REPO/dist}"
ARCH="$(uname -m)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
mkdir -p "$OUTDIR"

if ! python3 -c "import PyInstaller" 2>/dev/null; then
  echo "PyInstaller not found. Install build deps in a venv:" >&2
  echo "  python3 -m pip install pyinstaller . '.[vec,audit,embed,yaml]'" >&2
  exit 2
fi

NAME="brain-${OS}-${ARCH}"
echo "[build] PyInstaller one-file: $NAME"
# Entry point: the CLI main(). --collect-all pulls fastembed/onnxruntime/sqlite_vec
# native libs into the bundle so the VM needs nothing pre-installed.
python3 -m PyInstaller \
  --onefile --name "$NAME" \
  --collect-all fastembed --collect-all onnxruntime --collect-all sqlite_vec \
  --paths "$REPO/src" \
  --distpath "$OUTDIR" \
  "$REPO/src/brain/cli.py"

echo "[build] wrote $OUTDIR/$NAME"
echo
echo "Cross-arch: this built only $ARCH. For the other Linux arch, run the SAME"
echo "command on an aarch64 (or x86_64) runner, or under:"
echo "  docker buildx build --platform linux/amd64,linux/arm64 ..."
echo "Ship BOTH dist/brain-linux-x86_64 and dist/brain-linux-aarch64 into the"
echo "workspace via tools/cowork_workspace_install.sh."
