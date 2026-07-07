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

# 2. Private venv + full install
say "Installing Brainiac into $VENV_DIR (full capacity — no options to pick)"
"$PY" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR"

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
