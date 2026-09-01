#!/usr/bin/env bash
# SEC-07 — Claude Code PreToolUse guard: refuse a tool call whose arguments
# carry a term this vault classifies Confidential or above.
#
# WHY THIS EXISTS. The classification gate decides what the model may READ. It
# says nothing about what the model then does. A web search is an outbound
# channel and nobody reviews a query string, so an injected instruction can put
# a codename into one and the term has left before any result comes back.
# `AGENTS.md` retrieval rule 4 has always said so; this script is what enforces
# it.
#
# IT SHIPS NO TERMS. `brain check-egress` reads them from the vault's own
# `overlay/keywords/` decoder ring. A vault with no ring maps nothing, this
# guard allows everything, and `brain check-egress` says so in its own words
# rather than reporting a cheerful pass.
#
# WHAT IT DOES NOT DO. It matches DECLARED terms on word boundaries. It cannot
# see a paraphrase, an undeclared codename, or a fact the model states in its
# own words. Treat it as a floor, not a seal.
#
# INSTALL (Claude Code). Place this file at ~/.claude/hooks/ and register it:
#
#   "hooks": {
#     "PreToolUse": [
#       { "matcher": "WebSearch|WebFetch",
#         "hooks": [ { "type": "command",
#                      "command": "~/.claude/hooks/brainiac-egress-guard.sh",
#                      "timeout": 5 } ] }
#     ]
#   }
#
# Add your own web-tool names to the matcher — an MCP search tool is matched by
# its full `mcp__<server>__<tool>` name.
#
# ON A HARNESS-MANAGED ~/.claude (one a deploy repo owns, per
# `brain.session_hook.harness_managed`) the settings.json entry belongs to that
# repo, not to this engine. Author it there.

set -uo pipefail

BRAIN_BIN="${BRAIN_BIN:-brain}"
command -v "$BRAIN_BIN" >/dev/null 2>&1 || exit 0   # no engine here, no opinion

payload="$(cat)"

# The tool arguments, as one string. `jq` is not assumed: fall back to the raw
# payload, which is a superset of the arguments and so can only over-refuse,
# never under-refuse. Over-refusing is the safe direction for a guard.
if command -v jq >/dev/null 2>&1; then
  text="$(printf '%s' "$payload" | jq -r '.tool_input // empty | tostring' 2>/dev/null)"
  [ -z "$text" ] && text="$payload"
else
  text="$payload"
fi

verdict="$(printf '%s' "$text" | "$BRAIN_BIN" check-egress 2>/dev/null)"
status=$?

# Exit 6 is the ONLY refusal. Any other non-zero status is this guard failing,
# not the text failing, and a broken guard must not block the session — it
# would be indistinguishable from a real refusal and would be switched off
# within the day. A failure is reported on stderr and allowed through.
if [ "$status" -eq 6 ]; then
  printf '%s\n' "$verdict" >&2
  exit 2            # Claude Code: block the tool call, show stderr to the model
fi
if [ "$status" -ne 0 ]; then
  printf 'brainiac-egress-guard: check failed (exit %s) — ALLOWING, not blocking\n' \
    "$status" >&2
fi
exit 0
