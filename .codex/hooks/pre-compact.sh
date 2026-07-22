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
  {
    echo ""
    echo "<!-- pre-compact checkpoint $TS - context was compacting; in-flight state may be partial -->"
  } >> "$HANDOFF"
fi
exit 0
