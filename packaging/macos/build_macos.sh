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
# Install the product with the CORPORATE minimal-dep set (DIST-01): direct-ONNX
# e5-small, NO fastembed/PyTorch. The model is bundled inline (DIST-02).
"$PY" -m pip install --quiet -e ".[corporate]"

# DIST-02: stage the e5-small ONNX model inline so the frozen binary is
# offline-first (no HF download at run time). BRAIN_MODEL_CACHE in the spec
# points the OnnxEmbedder at this bundled snapshot.
# Set BRAIN_SKIP_MODEL_BUNDLE=1 (CI / hash-embedder path) to skip staging —
# the binary still builds + runs the offline HashEmbedder fallback.
if [ "${BRAIN_SKIP_MODEL_BUNDLE:-0}" != "1" ]; then
  "$PY" "$REPO/packaging/stage_model.py" --repo Xenova/multilingual-e5-small \
    --out "$REPO/packaging/model_bundle/e5-small" \
    --patterns "onnx/model.onnx" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "config.json" \
    ${BRAIN_EMBED_MODEL_DIR:+--cache "$BRAIN_EMBED_MODEL_DIR"}
fi

BRAIN_MODEL_BUNDLE="$REPO/packaging/model_bundle/e5-small" \
"$PY" -m PyInstaller --clean --noconfirm \
  --distpath "$REPO/dist" --workpath "$REPO/build" \
  "$REPO/packaging/macos/brain-macos.spec"

BIN="$REPO/dist/brain/brain"
[ -x "$BIN" ] || { echo "build failed: $BIN missing" >&2; exit 1; }
echo "Built (UNSIGNED): $BIN"
shasum -a 256 "$BIN"
"$BIN" --help >/dev/null && echo "smoke OK: frozen binary runs"
echo "Next: sign_notarize_macos.sh (PENDING Apple Developer ID)"
