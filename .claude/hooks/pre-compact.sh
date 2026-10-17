#!/usr/bin/env bash
# Claude Code PreCompact hook — MEM-02 (ADR-0003 Ruling 4, session s07).
#
# Appends a checkpoint marker to the live session handoff before context
# compaction, so a resumed session can see that compaction happened mid-flight.
#
# Ported from the reference vault's pre-compact.sh (sha256
# 8c6e59990127b29f2eb12b13e30b1792ed195c6d0f6ab808898894fcedf27026 —
# ADR-0003 Appendix B), adapted: vault root resolved dynamically
# ($BRAIN_VAULT > $CLAUDE_PROJECT_DIR/vault) instead of a hardcoded path.
set -e
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
VAULT_DIR="${BRAIN_VAULT:-$PROJECT_DIR/vault}"
MEMORY_DIR="$VAULT_DIR/.brain/memory"
HANDOFF="$MEMORY_DIR/handoff.md"
TS="$(date -u +%Y-%m-%dT%H:%MZ)"

mkdir -p "$MEMORY_DIR"
if [ -f "$HANDOFF" ]; then
  # Keep ONE marker: drop earlier checkpoint lines (and the blank line each
  # older version wrote before it), then append the new one. Appending forever
  # left a handoff of 19 markers and no state (2026-10-17).
  TMP="$HANDOFF.tmp.$$"
  awk '/<!-- pre-compact checkpoint /{held=0; next}
       /^$/{if (held) print ""; held=1; next}
       held{print ""; held=0}
       {print}
       END{if (held) print ""}' "$HANDOFF" > "$TMP"
  echo "<!-- pre-compact checkpoint $TS - context was compacting; in-flight state may be partial -->" >> "$TMP"
  mv "$TMP" "$HANDOFF"
fi
exit 0
