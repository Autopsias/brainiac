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
  echo "  python3 -m pip install pyinstaller .   # 0.3.0+: core deps are already full-capacity" >&2
  exit 2
fi

NAME="brain-${OS}-${ARCH}"
VERSION="$(python3 -c 'import sys; sys.path.insert(0, "'"$REPO"'/src"); from brain._version import __version__; print(__version__)')"
echo "[build] PyInstaller one-file: $NAME (version $VERSION)"
# Entry point: packaging/brain_entry.py — the package-aware shim. NEVER freeze
# src/brain/cli.py directly: it uses package-relative imports, so a frozen
# copy crashes at import time ("attempted relative import with no known
# parent package") — found in the wild 2026-07-04. --collect-all pulls
# onnxruntime/sqlite_vec native libs into the bundle so the VM needs nothing
# pre-installed.
BUILDTMP="$(mktemp -d)"
trap 'rm -rf "$BUILDTMP"' EXIT
python3 -m PyInstaller \
  --onefile --name "$NAME" \
  --collect-all onnxruntime --collect-all sqlite_vec \
  --collect-all tokenizers \
  --paths "$REPO/src" \
  --distpath "$OUTDIR" \
  --workpath "$BUILDTMP/build" --specpath "$BUILDTMP" \
  "$REPO/packaging/brain_entry.py"

# The version marker is what makes a stale ELF VISIBLE. The host cannot execute
# a Linux binary to ask its version, so without this file `brain doctor` can
# only check the ELF's integrity (SHA256SUMS) — which passes happily on a
# binary that is three releases behind, exactly the 0.17.0-under-0.20.12 skew
# found 2026-08-16. Written next to the ELF and staged with it.
printf '%s\n' "$VERSION" > "$OUTDIR/$NAME.version"

echo "[build] wrote $OUTDIR/$NAME (version $VERSION)"
echo
echo "Cross-arch: this built only $ARCH. For the other Linux arch, run the SAME"
echo "command on an aarch64 (or x86_64) runner, or under:"
echo "  docker buildx build --platform linux/amd64,linux/arm64 ..."
echo "Ship BOTH dist/brain-linux-x86_64 and dist/brain-linux-aarch64 into the"
echo "workspace via tools/cowork_workspace_install.sh."
