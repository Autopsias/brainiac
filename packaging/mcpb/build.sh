#!/usr/bin/env bash
# SUI-03 — build + validate + handshake-gate the Brainiac .mcpb bundle for
# Claude Desktop's Chat tab. Deterministic output: dist/brainiac.mcpb.
#
# Route B (Node stdio shim spawning the HOST-installed brain-mcp engine) —
# see packaging/mcpb/server/index.js's header comment for why: Claude
# Desktop only bundles Node.js on macOS/Windows, not Python, and a
# python-type .mcpb would have to vendor a full venv including compiled
# deps (pydantic-core, required by the `mcp` SDK) that don't bundle
# portably across platforms. The `uv` server type exists in the v0.4 spec
# but is still new/less-trodden than plain node; node is what Desktop
# actually ships, so it's the default here.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUNDLE_DIR="$REPO_ROOT/packaging/mcpb"
DIST_DIR="$REPO_ROOT/dist"
OUT="$DIST_DIR/brainiac.mcpb"

cd "$REPO_ROOT"

# --- 1. Stamp manifest.json's version from the pyproject.toml SSOT --------
SSOT_VERSION="$(python3 -c "
import re
text = open('pyproject.toml').read()
m = re.search(r'(?m)^version\s*=\s*\"([^\"]+)\"', text)
print(m.group(1) if m else '0.0.0')
")"
python3 -c "
import json
p = '$BUNDLE_DIR/manifest.json'
d = json.load(open(p))
d['version'] = '$SSOT_VERSION'
json.dump(d, open(p, 'w'), indent=2)
open(p, 'a').write('\n')
"
echo "== manifest.json stamped to version $SSOT_VERSION (SSOT: pyproject.toml) =="

# --- 2. mcpb CLI: validate then pack ---------------------------------------
MCPB="npx --yes @anthropic-ai/mcpb"
echo "== mcpb validate =="
$MCPB validate "$BUNDLE_DIR/manifest.json"

mkdir -p "$DIST_DIR"
rm -f "$OUT"
echo "== mcpb pack -> $OUT =="
$MCPB pack "$BUNDLE_DIR" "$OUT"
echo "built: $OUT ($(du -h "$OUT" | cut -f1))"

# --- 3. Handshake gate: real MCP initialize -> list_tools against the shim -
# Needs a Python with the `mcp` package (client side: ClientSession +
# stdio_client) importable. The repo dev venv (`pip install -e '.[mcp]'`)
# qualifies out of the box. Off that fast path, do NOT guess with a bare
# `python3` — an isolated-venv engine install (uv tool / pipx / pip --user)
# almost never puts `mcp` on system python3's import path, which used to
# abort the build with a spurious ImportError even though the shipped
# engine works fine. Instead resolve the interpreter that installed
# `brain-mcp` itself: its own shebang line names that interpreter, and since
# `brain-mcp` needs the `mcp` package to run as a server, that same
# interpreter always has `mcp` (client + server live in one package)
# importable too — true for every channel (uv tool, pipx, pip --user,
# editable checkout) with no per-channel special-casing needed.
if [ -x "$REPO_ROOT/.venv/bin/python3" ] && [ -x "$REPO_ROOT/.venv/bin/brain-mcp" ]; then
    PY_CMD_ARR=("$REPO_ROOT/.venv/bin/python3")
    BRAIN_MCP_BIN_DIR="$REPO_ROOT/.venv/bin"
    RESOLVED_FROM=".venv/bin/brain-mcp"
else
    BRAIN_MCP_PATH="$(command -v brain-mcp || true)"
    if [ -z "$BRAIN_MCP_PATH" ]; then
        echo "ERROR: no brain-mcp found (.venv/bin/brain-mcp, or on PATH)." >&2
        echo "       install the engine first: pip install 'brainiac-cli[mcp]' (or uv tool / pipx)." >&2
        exit 1
    fi
    BRAIN_MCP_BIN_DIR="$(dirname "$BRAIN_MCP_PATH")"
    SHEBANG_LINE="$(head -1 "$BRAIN_MCP_PATH")"
    # shellcheck disable=SC2206 -- deliberate word-split: shebang may be a
    # single interpreter path OR "prog arg" (e.g. "/usr/bin/env python3").
    PY_CMD_ARR=(${SHEBANG_LINE#\#!})
    RESOLVED_FROM="$BRAIN_MCP_PATH's shebang"
fi
# Shared guard, both paths: `brain-mcp` is an ungated console_script but
# `mcp` ships only with the `[mcp]` extra, so a plain `pip install -e .`
# (no extra) can have a valid brain-mcp binary yet still lack the `mcp`
# package -- verify importability here, once, before smoke_handshake.py
# ever runs, so a missing extra always fails with this clean message
# instead of a raw ModuleNotFoundError traceback out of the handshake.
if [ "${#PY_CMD_ARR[@]}" -eq 0 ] || ! "${PY_CMD_ARR[@]}" -c "import mcp" >/dev/null 2>&1; then
    echo "ERROR: resolved interpreter '${PY_CMD_ARR[*]:-<none>}' (from $RESOLVED_FROM)" >&2
    echo "       cannot import the mcp package -- reinstall with the [mcp] extra." >&2
    exit 1
fi
echo "== handshake gate: ${PY_CMD_ARR[*]}, brain-mcp dir: $BRAIN_MCP_BIN_DIR =="
"${PY_CMD_ARR[@]}" "$BUNDLE_DIR/smoke_handshake.py" \
    "$BUNDLE_DIR/server/index.js" \
    "$BRAIN_MCP_BIN_DIR" \
    "$REPO_ROOT/vault"

echo "== SUI-03 build complete: $OUT =="
