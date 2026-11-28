#!/bin/bash
# SEC-07 PreToolUse egress guard — thin POSIX launcher for `brain.egress_guard`.
# Preserves ~/.claude/hooks/brainiac-egress-guard.sh compatibility and .engine
# pin hardening without trusting the inherited $PATH.
set -uo pipefail
GUARD_INHERITED_PATH="${PATH:-}"
PATH=/usr/bin:/bin:/usr/sbin:/sbin; export PATH
_self="$0"
case "$_self" in */*) : ;; *) _self="./$_self" ;; esac
_pin="${_self%.sh}.engine"
_pypin="${_self%.sh}.python"
_brain=""
if [ -r "$_pin" ]; then
  IFS= read -r _brain < "$_pin" || _brain=""
  case "$_brain" in /*) : ;; *) _brain="" ;; esac
  if [ -z "$_brain" ] || [ ! -x "$_brain" ]; then
    printf '%s\n' \
      "brainiac-egress-guard: the PINNED engine named in $_pin is missing or not executable — REFUSING, fail-closed." >&2
    exit 2
  fi
elif [ -x "$HOME/.brainiac/venv/bin/brain" ]; then
  _brain="$HOME/.brainiac/venv/bin/brain"
else
  for _c in "$HOME/.local/bin/brain" /usr/local/bin/brain /opt/homebrew/bin/brain /usr/bin/brain; do
    if [ -x "$_c" ]; then _brain="$_c"; break; fi
  done
  if [ -z "$_brain" ]; then
    _brain="$(PATH="$GUARD_INHERITED_PATH" command -v brain 2>/dev/null || true)"
    case "$_brain" in /*) : ;; *) _brain="" ;; esac
  fi
fi
[ -n "$_brain" ] || exit 0
_py_ok() { [ -n "$1" ] && [ -x "$1" ] && "$1" -I -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('brain.egress_guard') else 1)" 2>/dev/null; }
_py=""
if [ -r "$_pypin" ]; then
  IFS= read -r _py < "$_pypin" || _py=""
  case "$_py" in /*) _py_ok "$_py" || _py="" ;; *) _py="" ;; esac
fi
if [ -z "$_py" ]; then
  for _c in "${_brain%/*}/python3" "${_brain%/*}/python" "$HOME/.brainiac/venv/bin/python3" \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if _py_ok "$_c"; then _py="$_c"; break; fi
  done
fi
[ -n "$_py" ] || { printf '%s\n' "brainiac-egress-guard: no usable Python interpreter — REFUSING, fail-closed." >&2; exit 2; }
"$_py" -I -m brain.egress_guard --script-path "$_self" --inherited-path "$GUARD_INHERITED_PATH"; _rc=$?
[ "$_rc" -eq 0 ] && exit 0
[ "$_rc" -eq 2 ] || printf '%s\n' "brainiac-egress-guard: guard exited $_rc — REFUSING, fail-closed." >&2
exit 2
