#!/usr/bin/env bash
# Brainiac one-command installer (macOS / Linux).
#   ./install.sh              — PyPI-first (default): tries, in order,
#                                `uv tool install` -> `pipx install` ->
#                                `pip install --user`, first success wins.
#   ./install.sh --dev        — contributor/offline path: editable install
#                                from THIS checkout ($REPO_DIR) into a
#                                private venv, exactly the pre-PyPI behavior.
#                                Use this if you're hacking on brainiac
#                                itself, or have no PyPI access.
#   ./install.sh --with-ocr   — also install the OCR toolchain (visible, opt-in)
#   bash install.sh           — same thing
#
# Every channel installs the `[mcp]` extra so `brain-mcp` (the host-side MCP
# server for Claude Desktop/Cowork/Code) works out of the box — the console
# script is defined unconditionally, so without the extra it would exist but
# crash on a missing `mcp` import.
#
# ... and `[ocr]`, so a scanned PDF or a picture-only deck is READ rather than
# quarantined. That extra is only the pytesseract binding (ADR-0003 keeps a
# system-binary binding out of the CORE deps); the `tesseract` binary itself
# is a system package, and `brain doctor`'s "Ingestion capability" row says so
# when it is missing instead of letting scans fail silently.
set -euo pipefail

WITH_OCR=0
DEV_MODE=0
UV_BOOTSTRAP=1
for arg in "$@"; do
  case "$arg" in
    --with-ocr) WITH_OCR=1 ;;
    --dev|--offline) DEV_MODE=1 ;;
    --no-uv-bootstrap) UV_BOOTSTRAP=0 ;;
    *) printf 'ERROR: unknown option %s (supported: --dev, --with-ocr, --no-uv-bootstrap)\n' "$arg" >&2; exit 1 ;;
  esac
done

# Managed Python version requested when uv has to supply one. Only used on the
# no-system-Python path; a machine with a usable python3 keeps whatever it has.
UV_MANAGED_PYTHON="3.12"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${BRAINIAC_HOME:-$HOME/.brainiac}/venv"
BIN_DIR="$HOME/.local/bin"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# 1. Python check (>=3.9).
#
# This used to be a hard failure for EVERY path, which was wrong on the default
# one: uv and pipx each manage their own interpreter, so Python is only truly
# needed for the `pip --user` last resort and for --dev's editable venv. A
# machine with no Python was told "install Python 3.9+ first" and stopped —
# and npx, the path that looked like it covered that case, only printed the
# same advice (2026-08-20 distribution review).
#
# uv closes it properly: it is a self-contained binary that needs no Python to
# run and can fetch a managed CPython of its own. So when there is no usable
# python3 AND no uv, fetch uv rather than giving up. Skip with
# --no-uv-bootstrap on a machine whose policy forbids the download.
PY="$(command -v python3 || true)"
if [ -n "$PY" ] && ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  say "Found $("$PY" --version 2>&1), which is below the 3.9 this needs — ignoring it."
  PY=""
fi

NEED_MANAGED_PYTHON=0
if [ -z "$PY" ]; then
  if [ "$DEV_MODE" = "1" ]; then
    fail "--dev needs a real python3 3.9+ on PATH to build the editable venv.
  Install Python 3.9+ (macOS: 'brew install python3' or https://python.org), then re-run."
  fi
  if command -v uv >/dev/null 2>&1; then
    NEED_MANAGED_PYTHON=1
    say "No Python 3.9+ on PATH — uv is here and will supply its own ($UV_MANAGED_PYTHON)."
  elif [ "$UV_BOOTSTRAP" = "0" ]; then
    fail "No Python 3.9+ and no uv on PATH, and --no-uv-bootstrap forbids fetching uv.
  Install either one, then re-run:
    uv:     curl -LsSf https://astral.sh/uv/install.sh | sh
    Python: https://www.python.org/downloads/"
  else
    say "No Python 3.9+ and no uv on PATH."
    say "Fetching uv from https://astral.sh/uv/install.sh — a self-contained binary"
    say "that needs no Python, and that will then download its own CPython $UV_MANAGED_PYTHON."
    say "(Skip this with --no-uv-bootstrap and install Python or uv yourself.)"
    curl -LsSf https://astral.sh/uv/install.sh | sh \
      || fail "fetching uv failed. Install uv or Python 3.9+ by hand, then re-run."
    # The uv installer drops the binary here and only edits shell profiles,
    # which this shell has already read — so put it on PATH for this run.
    for cand in "${XDG_BIN_HOME:-}" "${CARGO_HOME:-$HOME/.cargo}/bin" "$HOME/.local/bin"; do
      [ -n "$cand" ] && [ -x "$cand/uv" ] && PATH="$cand:$PATH" && export PATH && break
    done
    command -v uv >/dev/null 2>&1 \
      || fail "uv installed but is not on PATH in this shell. Open a new shell and re-run this script."
    NEED_MANAGED_PYTHON=1
    say "uv $(uv --version 2>/dev/null | awk '{print $2}') is ready."
  fi
fi

# 1a. venv module preflight (CS-03), only load-bearing for --dev (the
#     editable venv) — stock Debian/Ubuntu ships `python3` but NOT the
#     stdlib `venv`/`ensurepip` modules (split into a separate apt package).
if [ "$DEV_MODE" = "1" ] && ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  fail "python3's venv module is missing. On Debian/Ubuntu, install it with:
      sudo apt install python3-venv python${PYVER}-venv
  Then re-run ./install.sh --dev."
fi

# 1b. OCR toolchain — OPT-IN via --with-ocr (CS-02). `brain ingest` only needs
#     ocrmypdf + tesseract for image-only PDFs; without it, a scanned PDF just
#     quarantines as `pdf_no_text_layer` instead of failing anything. Default
#     install never touches brew/apt (no silent multi-minute install, no sudo
#     invoked BY this script) — pass --with-ocr for VISIBLE output; on Linux
#     this PRINTS the sudo command for you to run rather than invoking sudo
#     itself. tesseract-lang pulls all language packs since the corpus is
#     multilingual.
if [ "$WITH_OCR" = "1" ]; then
  if command -v ocrmypdf >/dev/null 2>&1; then
    say "OCR toolchain present ($(ocrmypdf --version 2>/dev/null | head -1))"
  elif command -v brew >/dev/null 2>&1; then
    say "Installing OCR toolchain (ocrmypdf + tesseract + tesseract-lang) via brew"
    brew install ocrmypdf tesseract tesseract-lang
  elif command -v apt-get >/dev/null 2>&1; then
    say "Run this to install the OCR toolchain (this script never invokes sudo for you):"
    echo "    sudo apt-get install -y ocrmypdf tesseract-ocr tesseract-ocr-por tesseract-ocr-spa"
  else
    say "No brew/apt found — install ocrmypdf + tesseract (with the language packs you need) manually."
  fi
else
  say "OCR toolchain: skipped (default). Enable later with: ./install.sh --with-ocr"
fi

# ==============================================================================
# 2. Engine install — PyPI-first by default (PYP-04); --dev keeps the
#    editable-checkout path (contributors / offline / no PyPI access).
# ==============================================================================
INSTALLED_CHANNEL=""

if [ "$DEV_MODE" = "1" ]; then
  say "Installing Brainiac into $VENV_DIR (--dev: editable install from this checkout)"
  "$PY" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR[mcp,ocr]"
  INSTALLED_CHANNEL="editable-checkout"
  BRAIN_BIN="$VENV_DIR/bin/brain"
  mkdir -p "$BIN_DIR"
  ln -sf "$BRAIN_BIN" "$BIN_DIR/brain"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "NOTE: $BIN_DIR is not on your PATH. Add this line to your shell profile:"
       echo "    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac
else
  say "Installing brainiac-cli from PyPI — trying uv tool install, then pipx, then pip --user"

  if command -v uv >/dev/null 2>&1; then
    # --python only on the no-system-Python path: it makes uv fetch a managed
    # CPython. Passing it always would override a perfectly good interpreter
    # the machine already has.
    UV_PY_ARG=""
    if [ "$NEED_MANAGED_PYTHON" = "1" ]; then
      UV_PY_ARG="--python $UV_MANAGED_PYTHON"
      say "Attempt 1/3: uv tool install --python $UV_MANAGED_PYTHON 'brainiac-cli[mcp,ocr]' (uv downloads CPython $UV_MANAGED_PYTHON first)"
    else
      say "Attempt 1/3: uv tool install 'brainiac-cli[mcp,ocr]'"
    fi
    # shellcheck disable=SC2086  # UV_PY_ARG is a controlled two-token flag or empty
    if uv tool install $UV_PY_ARG 'brainiac-cli[mcp,ocr]'; then
      INSTALLED_CHANNEL="uv tool"
    elif [ "$NEED_MANAGED_PYTHON" = "1" ]; then
      fail "uv tool install failed, and there is no system Python to fall back to.
  Read the error above; then either re-run, or install Python 3.9+ and re-run."
    else
      say "uv tool install failed — falling back to pipx"
    fi
  else
    say "Attempt 1/3: uv not found on PATH — skipping (install from https://docs.astral.sh/uv/ for the fastest channel)"
  fi

  if [ -z "$INSTALLED_CHANNEL" ]; then
    if command -v pipx >/dev/null 2>&1; then
      say "Attempt 2/3: pipx install 'brainiac-cli[mcp,ocr]'"
      if pipx install 'brainiac-cli[mcp,ocr]'; then
        INSTALLED_CHANNEL="pipx"
      else
        say "pipx install failed — falling back to pip --user"
      fi
    else
      say "Attempt 2/3: pipx not found on PATH — skipping (https://pipx.pypa.io/)"
    fi
  fi

  if [ -z "$INSTALLED_CHANNEL" ] && [ -z "$PY" ]; then
    # Only reachable if uv was present, its install failed, and pipx was too —
    # `"$PY" -m pip` with PY empty would run `-m pip` on nothing.
    fail "uv and pipx both failed and there is no system Python for the pip fallback.
  Read the errors above, then re-run."
  fi

  if [ -z "$INSTALLED_CHANNEL" ]; then
    say "Attempt 3/3: python3 -m pip install --user 'brainiac-cli[mcp,ocr]'"
    if "$PY" -m pip install --user --quiet 'brainiac-cli[mcp,ocr]'; then
      INSTALLED_CHANNEL="pip --user"
    else
      fail "Every install channel failed (uv tool install / pipx install / pip install --user).
  Fixes to try:
    - Check network access to pypi.org (brainiac-cli may also not be published yet — see README).
    - Install uv (https://docs.astral.sh/uv/) or pipx (https://pipx.pypa.io/) for a more isolated install.
    - Contributors/offline: re-run as './install.sh --dev' to install editable from this checkout instead."
    fi
  fi

  say "Installed via: $INSTALLED_CHANNEL"

  # PATH wiring — each channel manages its own bin dir; only pip --user needs
  # a hint here (uv/pipx both print their own PATH guidance on first install,
  # and 'uv tool update-shell' / 'pipx ensurepath' are the fix if not).
  if [ "$INSTALLED_CHANNEL" = "pip --user" ] && ! command -v brain >/dev/null 2>&1; then
    USER_BASE="$("$PY" -m site --user-base 2>/dev/null || true)"
    say "NOTE: 'brain' isn't on PATH yet. Add this line to your shell profile:"
    echo "    export PATH=\"$USER_BASE/bin:\$PATH\""
  fi
fi

# ==============================================================================
# 3. Verify + next steps
# ==============================================================================
if [ "$DEV_MODE" = "1" ]; then
  # First index build against the checkout's own sample vault — CS-01:
  # lexical-only, no network. BRAIN_EMBEDDER=hash forces the offline
  # deterministic embedder (explicit, no warning — see src/brain/embed.py)
  # so grep/bases-query/FTS work immediately. `brain search`'s dense leg
  # self-detects this placeholder (BrainIndex.model_matches) and degrades to
  # FTS-only with a notice until a real rebuild applies the real model.
  say "Building a lexical-only index for the sample vault (no model download)"
  (cd "$REPO_DIR" && BRAIN_EMBEDDER=hash "$VENV_DIR/bin/brain" rebuild)

  say "Done. Try it:"
  echo "    cd $REPO_DIR"
  echo "    brain grep \"arctic-embed\""
  echo "    brain --help"
else
  say "Done. Verify it:"
  echo "    brain --version"
  echo "    brain --help"
  echo ""
  echo "Next: point it at your vault (creates <workspace>/vault if it doesn't exist yet):"
  echo "    BRAIN_VAULT=<workspace>/vault brain init --full --apply"
fi

echo ""
echo "Semantic search downloads its model (bge-m3-int8, ~563 MB) on first real use, with a"
echo "progress line on stderr — or run \`brain warmup\` now, then \`brain sync\`"
echo "to apply it to the index (\`brain status\` shows embedder: ready|pending)."
echo ""
echo "Already installed and want the latest? Re-run this script, or in Claude Code: /brainiac-update"
