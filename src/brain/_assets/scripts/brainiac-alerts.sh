#!/bin/bash
# SessionStart hook: surface Brainiac health alerts in every session.
#
# Used by BOTH harnesses that have a hook surface — Claude Code
# (~/.claude/settings.json) and Codex (~/.codex/hooks.json), same schema, same
# script. Only the Claude Code side is REGISTERED by `brain install-hook`; a
# Codex hook stays inert until the owner trusts it once interactively, so
# writing ~/.codex/hooks.json from an installer would buy nothing. Cowork has
# no hook mechanism; it follows the AGENTS.md §9 line instead. See
# docs/harness-wiring.md in profile-a-brain.
#
# This is a THIN CALLER. The logic lives in the engine as `brain alerts`
# (VM_ALLOWED, pure file reads, ~0.4s) so every harness reads the same findings
# from the same code. It used to live inline here, which is exactly why Codex
# and Cowork were blind to it: the inputs are five plain files, and nothing
# about reading them was ever Claude-Code-specific.
#
# Owner ask 2026-07-19: failures must not hide in hot.md / launchd logs /
# ephemeral banners until someone remembers to run a health check.
#
# Silent (no output) when all healthy — but NEVER silent when it could not
# look. A check whose all-clear equals "no input" is worse than no check.
#
# The pre-0.20.7 INLINE implementation of the whole digest lived on HERE until
# 2026-08-20, long after the engine verb replaced it — and nothing installed
# either copy, so the wiring table's "hard" Claude Code channel was real on one
# machine only. `brain install-hook` places this file and registers it, and
# every `brain update` re-places it, which is how a host still carrying the
# inline copy gets fixed. See src/brain/session_hook.py.

if ! command -v brain >/dev/null 2>&1; then
  text="BRAINIAC ALERTS: cannot check — \`brain\` is not on PATH for this session, so vault health is UNKNOWN (not healthy). Fix PATH or reinstall the engine."
elif ! banner=$(brain alerts --one-line 2>/dev/null); then
  text="BRAINIAC ALERTS: cannot check — \`brain alerts\` failed, so vault health is UNKNOWN (not healthy). Run \`brain alerts\` by hand to see why."
else
  # Empty banner is the all-clear contract: nothing to say, say nothing.
  [ -z "$banner" ] && exit 0
  text="$banner"
fi

TEXT="$text" exec python3 -c '
import json, os

text = os.environ["TEXT"]
print(json.dumps({
    "systemMessage": text,
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text + "\n(Surface these to the user; fix or delegate when asked.)",
    },
}))
'
