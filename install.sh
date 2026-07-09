#!/usr/bin/env bash
# Brainiac one-command installer (macOS / Linux).
#   ./install.sh          — from a clone
#   bash install.sh       — same thing
# Creates a private venv (no system Python pollution), installs the full
# brain CLI into it, links `brain` onto your PATH, and builds the index for
# the sample vault so the first search works immediately.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${BRAINIAC_HOME:-$HOME/.brainiac}/venv"
BIN_DIR="$HOME/.local/bin"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# 1. Python check (>=3.9)
PY="$(command -v python3 || true)"
[ -n "$PY" ] || fail "python3 not found. Install Python 3.9+ first (macOS: 'brew install python3' or https://python.org)."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "Python 3.9+ required; found $("$PY" --version 2>&1)."

# 1b. OCR toolchain (OPTIONAL — enables scanned/image-only PDF ingestion).
#     `brain ingest` needs ocrmypdf + tesseract only for image-only PDFs; a
#     missing toolchain NEVER blocks install or ingestion (brain just quarantines
#     the scan as `pdf_no_text_layer` instead). Best-effort + idempotent, so
#     re-running install.sh is safe. tesseract-lang pulls all language packs
#     (por/spa/eng/…) since the corpus is multilingual.
if command -v ocrmypdf >/dev/null 2>&1; then
  say "OCR toolchain present ($(ocrmypdf --version 2>/dev/null | head -1))"
else
  say "Installing OCR toolchain (ocrmypdf + tesseract) — optional, for scanned PDFs"
  if command -v brew >/dev/null 2>&1; then
    brew install ocrmypdf tesseract tesseract-lang >/dev/null 2>&1 \
      && say "OCR toolchain installed" \
      || printf 'NOTE: OCR install skipped/failed (non-fatal). Scanned PDFs quarantine until you run:\n      brew install ocrmypdf tesseract tesseract-lang\n'
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y ocrmypdf tesseract-ocr tesseract-ocr-por tesseract-ocr-spa >/dev/null 2>&1 \
      && say "OCR toolchain installed" \
      || printf 'NOTE: OCR install skipped (non-fatal). For scanned PDFs run:\n      sudo apt-get install ocrmypdf tesseract-ocr tesseract-ocr-por\n'
  else
    printf 'NOTE: no brew/apt found — OCR is optional. To enable scanned-PDF ingestion,\n      install ocrmypdf + tesseract (with the language packs you need).\n'
  fi
fi

# 2. Private venv + full install (incl. the [mcp] extra so `brain-mcp` — the
#    host-side MCP server for Claude Desktop/Cowork/Code — works out of the box;
#    the console script is defined unconditionally, so without the extra it
#    would exist but crash on a missing `mcp` import).
say "Installing Brainiac into $VENV_DIR (full capacity — no options to pick)"
"$PY" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR[mcp]"

# 3. Put `brain` on PATH
mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/brain" "$BIN_DIR/brain"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "NOTE: $BIN_DIR is not on your PATH. Add this line to your shell profile:"
     echo "    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# 4. First index build against the sample vault (downloads the embedding
#    model once, a few hundred MB — needs network this one time).
say "Building the search index for the sample vault (one-time model download)"
(cd "$REPO_DIR" && "$VENV_DIR/bin/brain" rebuild)

say "Done. Try it:"
echo "    cd $REPO_DIR"
echo "    brain search \"arctic-embed vs e5\" --json"
echo "    brain --help"
echo ""
echo "Already installed and want the latest? Re-run this script, or in Claude Code: /brainiac-update"
