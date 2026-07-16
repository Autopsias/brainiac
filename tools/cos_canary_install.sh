#!/usr/bin/env bash
# cos_canary_install.sh — install a NEW engine version into an ISOLATED,
# versioned venv scoped to a canary vault, with a pre-migration backup and an
# explicit rollback path. NEVER touches the globally-installed `brain`
# (HARDENED:codex-verify-r3: no unvalidated global CLI swap under other
# vaults — validate the canary + workspace_registry health first, then swap
# by hand).
#
# Usage: tools/cos_canary_install.sh <version> [canary-vault-dir]
#   e.g. tools/cos_canary_install.sh 0.17.0 ~/Brainiac-Vault
#
# Produces:
#   dist/engines/brainiac-<version>/            the isolated venv
#   dist/engines/brainiac-<version>/bin/brain   the canary CLI (call explicitly)
#   dist/engines/pre-migration-<stamp>/         the rollback bundle:
#       VERSIONS.txt          global brain --version + pip freeze reference
#       inbox.jsonl           the canary vault's owner queue (if any)
#       maintain-state.json   the canary vault's maintain state (if any)
#
# Rollback: the global CLI was never changed — just delete the versioned venv
# (rm -rf dist/engines/brainiac-<version>) and, if desired, restore the two
# state files from the pre-migration bundle. See docs/cos-ops.md §7.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:?usage: cos_canary_install.sh <version> [canary-vault-dir]}"
CANARY_VAULT="${2:-}"
ENGINES="$REPO/dist/engines"
VENV="$ENGINES/brainiac-$VERSION"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ENGINES/pre-migration-$STAMP"
WHEEL="$REPO/dist/brainiac_cli-$VERSION-py3-none-any.whl"

[ -f "$WHEEL" ] || { echo "ERROR: wheel not found: $WHEEL (build it first)" >&2; exit 2; }

mkdir -p "$BACKUP"
{
  echo "pre-migration backup $STAMP"
  echo "global brain: $(command -v brain || echo '<none>')"
  echo "global version: $(brain --version 2>/dev/null || echo '<none>')"
} > "$BACKUP/VERSIONS.txt"
if [ -n "$CANARY_VAULT" ]; then
  for f in memory/inbox.jsonl maintain-state.json; do
    src="$CANARY_VAULT/.brain/$f"
    [ -f "$src" ] && cp "$src" "$BACKUP/$(basename "$f")"
  done
fi
echo "[canary] pre-migration backup -> $BACKUP"

python3 -m venv "$VENV"
"$VENV/bin/pip" -q install "$WHEEL"
INSTALLED="$("$VENV/bin/brain" --version)"
echo "[canary] isolated install: $INSTALLED -> $VENV/bin/brain"
[ "$INSTALLED" = "brain $VERSION" ] || { echo "ERROR: version mismatch: $INSTALLED" >&2; exit 3; }

echo "[canary] global CLI untouched: $(brain --version 2>/dev/null || echo '<none>')"
echo "[canary] next: run the canary vault against $VENV/bin/brain, check"
echo "         'brain doctor' + tools/workspace_registry.py health across every"
echo "         registered workspace, and only then swap the global executable."
