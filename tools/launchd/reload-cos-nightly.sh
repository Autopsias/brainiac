#!/usr/bin/env bash
# Reload the rendered cos-nightly job and fire it once. The OWNER runs this
# (launchctl is an attended action); the assistant only renders the plist.
set -eu

PLIST="$HOME/Library/LaunchAgents/com.brainiac.cos-nightly.plist"
LABEL="gui/$(id -u)/com.brainiac.cos-nightly"

launchctl bootout "$LABEL" 2>/dev/null || true   # tolerate "not loaded"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart "$LABEL"
echo "cos-nightly reloaded from $PLIST and kicked off."
