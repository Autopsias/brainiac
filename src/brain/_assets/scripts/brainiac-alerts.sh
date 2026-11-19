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

# THE ENVIRONMENT IS NOT AN INPUT, `$PATH` INCLUDED (F79). Committed project
# settings define the environment a hook inherits, so a repo could put its own
# directory first on PATH and have a forged `brain` (or `python3`) run as the
# user at every session start. Same answer as brainiac-egress-guard.sh: replace
# PATH with the fixed system directories, then resolve the engine from places
# no repository writes:
#   1. the pin beside this script (`brainiac-alerts.engine`, written by
#      `brain install-hook` and every `brain update`). A pin naming an engine
#      that is gone is a broken install and reads as "cannot check".
#   2. no pin: the managed venv, then the guard's absolute install locations.
# Unlike the guard there is NO last step against the inherited PATH: for the
# guard a missing engine means "allow", here it means the cannot-check banner,
# so a PATH lookup would buy a forged `brain` code execution and nothing else.
ALERTS_INHERITED_PATH="$PATH"
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

# WINDOWS (Git Bash / MSYS / Cygwin) KEEPS THE PRE-F79 LOOKUP. There the pin
# would be a native `C:\...` path this script cannot use, so install() writes
# no alerts pin and this branch restores the inherited PATH: no worse than
# before F79. The platform is `${BASH_VERSINFO[5]}`, the machine type compiled
# into bash, which no variable can change. NOT `uname`, `$OSTYPE` or
# `$MACHTYPE`: the environment sets all three (UNAME_SYSNAME=MINGW64_NT fakes
# `uname -s` on macOS), which would flip a Mac into this unhardened branch.
WINDOWS=""
case "${BASH_VERSINFO[5]}" in *msys*|*cygwin*|*mingw*) WINDOWS=1 ;; esac

_self="$0"
case "$_self" in */*) : ;; *) _self="./$_self" ;; esac
ALERTS_PIN="${_self%.sh}.engine"

BRAIN_BIN=""
PIN_BAD=""
if [ -n "$WINDOWS" ]; then
  PATH="$ALERTS_INHERITED_PATH"
  export PATH
  BRAIN_BIN="$(command -v brain 2>/dev/null || true)"
elif [ -r "$ALERTS_PIN" ]; then
  IFS= read -r BRAIN_BIN < "$ALERTS_PIN" || BRAIN_BIN=""
  case "$BRAIN_BIN" in /*) : ;; *) BRAIN_BIN="" ;; esac
  if [ -z "$BRAIN_BIN" ] || [ ! -x "$BRAIN_BIN" ]; then PIN_BAD=1; fi
elif [ -x "$HOME/.brainiac/venv/bin/brain" ]; then
  BRAIN_BIN="$HOME/.brainiac/venv/bin/brain"
else
  for _cand in "$HOME/.local/bin/brain" /usr/local/bin/brain \
               /opt/homebrew/bin/brain /usr/bin/brain; do
    if [ -x "$_cand" ]; then BRAIN_BIN="$_cand"; break; fi
  done
fi

if [ -n "$PIN_BAD" ]; then
  text="BRAINIAC ALERTS: cannot check — the engine pinned in $ALERTS_PIN is missing or not executable, so vault health is UNKNOWN (not healthy). Re-run \`brain install-hook\`."
elif [ -z "$BRAIN_BIN" ] && [ -n "$WINDOWS" ]; then
  text="BRAINIAC ALERTS: cannot check — \`brain\` is not on PATH for this session, so vault health is UNKNOWN (not healthy). Fix PATH or reinstall the engine."
elif [ -z "$BRAIN_BIN" ]; then
  text="BRAINIAC ALERTS: cannot check — no \`brain\` engine was found beside this hook, in the managed venv or in a standard install location, so vault health is UNKNOWN (not healthy). Re-run \`brain install-hook\` or reinstall the engine."
elif ! banner=$("$BRAIN_BIN" alerts --one-line 2>/dev/null); then
  text="BRAINIAC ALERTS: cannot check — \`brain alerts\` failed, so vault health is UNKNOWN (not healthy). Run \`brain alerts\` by hand to see why."
else
  # Empty banner is the all-clear contract: nothing to say, say nothing.
  [ -z "$banner" ] && exit 0
  text="$banner"
fi

# `python3` is a PATH lookup too. First the interpreter beside the engine (a
# venv engine's own, as trusted as the engine), then the fixed places package
# managers put one. None found -> the text as-is, not a 127 from `exec`.
# Windows keeps the bare `python3` lookup, for the reason given above.
PYTHON=""
if [ -n "$WINDOWS" ]; then
  PYTHON=python3
else
  for _cand in "${BRAIN_BIN:+${BRAIN_BIN%/*}/python3}" /opt/homebrew/bin/python3 \
               /usr/local/bin/python3 /usr/bin/python3; do
    if [ -n "$_cand" ] && [ -x "$_cand" ]; then PYTHON="$_cand"; break; fi
  done
fi
[ -n "$PYTHON" ] || { printf '%s\n' "$text"; exit 0; }

TEXT="$text" exec "$PYTHON" -c '
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
