#!/usr/bin/env bash
# install-brief-mac.sh — install brain-daily-brief as a macOS launchd LaunchAgent (UX-02)
#
# Usage:
#   BRAIN_VAULT=/path/to/vault bash scripts/install-brief-mac.sh
#
# Prerequisites:
#   - brain binary must be on PATH (test: brain --version)
#   - BRAIN_AUDIT_KEY_PEM must be set OR the key stored in the macOS Keychain
#     under service "profile-a-brain-audit-key" (security find-generic-password -s profile-a-brain-audit-key -w)
#
# Uninstall (per-vault label — the script prints the exact paths on install):
#   launchctl unload ~/Library/LaunchAgents/com.brainiac.nightly.<id>.plist
#   rm ~/Library/LaunchAgents/com.brainiac.nightly.<id>.plist
set -euo pipefail

VAULT="${BRAIN_VAULT:?BRAIN_VAULT must be set (path to your brain vault)}"
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPTS/brain-brief-mac.plist"

# Per-vault launchd label (SINGLE SOURCE OF TRUTH: brain.config.nightly_label),
# so two registered vaults never install to one shared label and clobber each
# other's nightly job. Prefer the installed package; fall back to a shell
# reimplementation of vault_slug8 (sha256 of the resolved vault path, first 8).
LABEL=""
BRAIN_BIN="$(command -v brain || true)"
if [ -n "$BRAIN_BIN" ]; then
    VENV_PY="$(dirname "$BRAIN_BIN")/python3"
    [ -x "$VENV_PY" ] || VENV_PY="python3"
    LABEL="$(BRAIN_VAULT="$VAULT" "$VENV_PY" - <<'PY' 2>/dev/null || true
import os
from brain.config import nightly_label
print(nightly_label(os.environ["BRAIN_VAULT"]))
PY
)"
fi
if [ -z "$LABEL" ]; then
    _vr="$(python3 -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$VAULT" 2>/dev/null || echo "$VAULT")"
    _h="$(printf '%s' "$_vr" | shasum -a 256 | cut -c1-8)"
    LABEL="com.brainiac.nightly.$_h"
fi

LEGACY_LABEL="com.profile-a-brain.daily-brief"
LEGACY_PLIST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"

# Audit signing key: the drain resolves it from the macOS Keychain AT RUNTIME
# (src/brain/audit.py resolve_signing_key(), env -> keychain fallthrough), so
# the plist does NOT carry the key material. Baking the PEM into the plist
# (the old behavior) left the private key in a plaintext file under
# ~/Library/LaunchAgents. We only bake a value when the operator explicitly
# injected BRAIN_AUDIT_KEY_PEM (custom custody / unattended box) — and the
# plist is chmod 600 below either way.
KEYCHAIN_SERVICE="${BRAIN_AUDIT_KEYCHAIN_SERVICE:-profile-a-brain-audit-key}"
AUDIT_KEY="${BRAIN_AUDIT_KEY_PEM:-}"
if [ -z "$AUDIT_KEY" ]; then
    if ! security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$USER" >/dev/null 2>&1; then
        echo "WARNING: No BRAIN_AUDIT_KEY_PEM and no Keychain entry for '$KEYCHAIN_SERVICE'." >&2
        echo "         Captures will not be signed (drain fails closed). Run 'brain audit-key'" >&2
        echo "         (create-if-absent, never rotates) — no reinstall needed after." >&2
    fi
fi

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

# Render the plist to a scratch file first so we can diff against whatever is
# already installed — the nightly-diff contract (docs/install/plugin-distribution.md
# §3): reinstall/reload iff the rendered body actually changed, otherwise report
# unchanged and touch nothing (idempotent re-runs, no needless launchctl churn).
PLIST_NEW="$(mktemp "${TMPDIR:-/tmp}/brain-brief-mac.plist.XXXXXX")"
trap 'rm -f "$PLIST_NEW"' EXIT

sed -e "s|LABEL_PLACEHOLDER|$LABEL|g" \
    -e "s|SCRIPTS_DIR|$SCRIPTS|g" \
    -e "s|VAULT_PATH|$VAULT|g" \
    -e "s|HOME_DIR|$HOME|g" \
    -e "s|AUDIT_KEY_PEM_PLACEHOLDER|$AUDIT_KEY|g" \
    -e "s|SWEEP_DIRS_PLACEHOLDER|${BRAIN_WORKSPACE_SWEEP_DIRS:-}|g" \
    "$PLIST_SRC" > "$PLIST_NEW"

# ponytail: real launchctl talks to the login launchd domain by uid, not by
# $HOME — a sandboxed HOME does NOT sandbox launchctl. BRAIN_LAUNCHD_DRY_RUN=1
# (or a non-default HOME, detected below) skips the real bootstrap/load calls
# so test/sandbox runs never touch the real user's launchd domain.
if [ -z "${BRAIN_LAUNCHD_DRY_RUN:-}" ] && [ "$HOME" != "$(eval echo ~"$USER")" ]; then
    BRAIN_LAUNCHD_DRY_RUN=1
fi

# One-time migration off the legacy SHARED label: if the old single-label plist
# exists (and isn't this vault's per-vault plist), unload + remove it so the two
# host vaults stop competing for one launchd job. Idempotent — absent after the
# first migrated install.
if [ "$LEGACY_PLIST" != "$PLIST_DST" ] && [ -f "$LEGACY_PLIST" ]; then
    if [ "${BRAIN_LAUNCHD_DRY_RUN:-0}" = "1" ]; then
        echo "migration DRY-RUN: would retire legacy shared label $LEGACY_LABEL"
    else
        launchctl unload "$LEGACY_PLIST" 2>/dev/null || true
        rm -f "$LEGACY_PLIST"
        echo "migrated: retired legacy shared nightly label $LEGACY_LABEL (now per-vault)"
    fi
fi

if [ -f "$PLIST_DST" ] && cmp -s "$PLIST_NEW" "$PLIST_DST"; then
    echo "nightly: unchanged (label: $LABEL) — nothing to reload"
    echo "  Status: launchctl list $LABEL"
    exit 0
fi

cp "$PLIST_NEW" "$PLIST_DST"
chmod 600 "$PLIST_DST"

if [ "${BRAIN_LAUNCHD_DRY_RUN:-0}" = "1" ]; then
    echo "DRY-RUN (sandboxed HOME or BRAIN_LAUNCHD_DRY_RUN=1) — plist written, launchd NOT reloaded. Would run:"
    echo "  launchctl unload '$PLIST_DST'"
    echo "  launchctl load -w '$PLIST_DST'"
    echo "  To load it for real, re-run this from a normal (non-sandboxed) terminal, or run the two commands above."
else
    # (Re)load the agent.
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load -w "$PLIST_DST"
fi

echo "✓ Installed: $LABEL (hourly umbrella, vault: $VAULT)"

# --- brain-synthesis (the SECOND sanctioned task; THE LOCK host=2) ---------
# One per HOST (label has no vault id): the script iterates every registered
# vault in ~/.brainiac/workspaces.json, so re-running this installer for a
# second vault just refreshes the same host-level agent. Weekly Sun 08:00.
SYNTH_SRC="$SCRIPTS/brain-synthesis-mac.plist"
SYNTH_DST="$HOME/Library/LaunchAgents/com.brainiac.synthesis.plist"
if [ -f "$SYNTH_SRC" ]; then
    SYNTH_NEW="$(mktemp "${TMPDIR:-/tmp}/brain-synthesis-mac.plist.XXXXXX")"
    sed -e "s|SCRIPTS_DIR|$SCRIPTS|g" -e "s|HOME_DIR|$HOME|g" \
        "$SYNTH_SRC" > "$SYNTH_NEW"
    if ! cmp -s "$SYNTH_NEW" "$SYNTH_DST" 2>/dev/null; then
        cp "$SYNTH_NEW" "$SYNTH_DST"
        chmod 600 "$SYNTH_DST"
        if [ "${BRAIN_LAUNCHD_DRY_RUN:-0}" = "1" ]; then
            echo "DRY-RUN — synthesis plist written, launchd NOT reloaded."
        else
            launchctl unload "$SYNTH_DST" 2>/dev/null || true
            launchctl load -w "$SYNTH_DST"
        fi
        echo "✓ Installed: com.brainiac.synthesis (weekly Sun 08:00, all registered vaults)"
    fi
    rm -f "$SYNTH_NEW"
fi
echo "  Logs:      $LOGDIR/brief-YYYY-MM-DD.log"
echo "  Status:    launchctl list $LABEL"
echo "  Dry-run:   launchctl start $LABEL"
echo "  Uninstall: launchctl unload '$PLIST_DST' && rm '$PLIST_DST'"
