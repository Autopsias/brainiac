#!/usr/bin/env bash
# build_macos.sh — build the `brain` one-dir bundle on macOS (PKG-02).
# Produces an UNSIGNED bundle under dist/brain/. Developer-ID signing +
# notarization are a SEPARATE step (sign_notarize_macos.sh), PENDING maintainer's
# Apple Developer ID (no Apple ID is held by the build agent).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
echo "== brain macOS one-dir build =="
"$PY" --version
"$PY" -m pip install --quiet --upgrade pip pyinstaller
# Install the product so the module graph resolves `brain` (+ shipped extras).
"$PY" -m pip install --quiet -e ".[vec,audit,embed,yaml]"

"$PY" -m PyInstaller --clean --noconfirm \
  --distpath "$REPO/dist" --workpath "$REPO/build" \
  "$REPO/packaging/macos/brain-macos.spec"

BIN="$REPO/dist/brain/brain"
[ -x "$BIN" ] || { echo "build failed: $BIN missing" >&2; exit 1; }
echo "Built (UNSIGNED): $BIN"
shasum -a 256 "$BIN"
"$BIN" --help >/dev/null && echo "smoke OK: frozen binary runs"
echo "Next: sign_notarize_macos.sh (PENDING Apple Developer ID)"
