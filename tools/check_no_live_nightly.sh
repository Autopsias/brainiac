#!/usr/bin/env bash
# Refuse to commit the COS nightly script while a night is running.
#
# WHY. Bash reads a script INCREMENTALLY from disk. Editing `cos_nightly.sh`
# while an instance is executing shifts the byte offsets underneath it, and the
# running night dies part-way through with a syntax error on a line that is
# perfectly valid in the file. Measured 2026-08-23: run176 died at 11:42 with
# "syntax error near unexpected token `then'" at line 768 after a commit landed
# a guard near line 360. `bash -n` passed on the file the whole time. A night is
# 20-40 minutes of paid model work, so the loss is real and the cause is
# invisible from the error.
#
# The commit is the only cheap moment to catch it: the edit itself is harmless
# until it reaches disk, and by then the night is already reading it.
set -euo pipefail
if pgrep -f 'cos_nightly\.sh' >/dev/null 2>&1; then
  echo "a COS night is RUNNING and this commit touches the script it is reading." >&2
  echo "Bash reads the file incrementally: committing now kills that night with a" >&2
  echo "syntax error on a valid line. Wait for it to finish, or stop it first." >&2
  pgrep -fl 'cos_nightly\.sh' >&2
  exit 1
fi
