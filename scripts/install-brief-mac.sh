#!/usr/bin/env bash
# install-brief-mac.sh — install brain-daily-brief as a macOS launchd LaunchAgent (UX-02)
#
# Usage:
#   BRAIN_VAULT=/path/to/vault bash scripts/install-brief-mac.sh
#
# Prerequisites:
#   - brain binary must be on PATH (test: brain --version)
#   - BRAIN_AUDIT_KEY_PEM must be set OR the key stored in the macOS Keychain
#     under service "profile-a-brain-audit" (security find-generic-password -s profile-a-brain-audit -w)
#
# Uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.profile-a-brain.daily-brief.plist
#   rm ~/Library/LaunchAgents/com.profile-a-brain.daily-brief.plist
set -euo pipefail

VAULT="${BRAIN_VAULT:?BRAIN_VAULT must be set (path to your brain vault)}"
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPTS/brain-brief-mac.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.profile-a-brain.daily-brief.plist"
LOGDIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"

# Resolve the audit signing key.
AUDIT_KEY="${BRAIN_AUDIT_KEY_PEM:-}"
if [ -z "$AUDIT_KEY" ]; then
    # Try macOS Keychain.
    AUDIT_KEY="$(security find-generic-password -s profile-a-brain-audit -a "$USER" -w 2>/dev/null || true)"
fi
if [ -z "$AUDIT_KEY" ]; then
    echo "WARNING: No BRAIN_AUDIT_KEY_PEM and no Keychain entry for 'profile-a-brain-audit'." >&2
    echo "         Captures will not be signed. Set BRAIN_AUDIT_KEY_PEM or run:" >&2
    echo "           security add-generic-password -s profile-a-brain-audit -a \$USER -w '<PEM>'" >&2
    AUDIT_KEY="MISSING_KEY_DRAIN_WILL_SKIP"
fi

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

# Substitute placeholders in the plist template.
sed -e "s|SCRIPTS_DIR|$SCRIPTS|g" \
    -e "s|VAULT_PATH|$VAULT|g" \
    -e "s|HOME_DIR|$HOME|g" \
    -e "s|AUDIT_KEY_PEM_PLACEHOLDER|$AUDIT_KEY|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# (Re)load the agent.
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"

echo "✓ Installed: com.profile-a-brain.daily-brief (daily 07:00)"
echo "  Logs:      $LOGDIR/brief-YYYY-MM-DD.log"
echo "  Status:    launchctl list com.profile-a-brain.daily-brief"
echo "  Dry-run:   launchctl start com.profile-a-brain.daily-brief"
echo "  Uninstall: launchctl unload '$PLIST_DST' && rm '$PLIST_DST'"
