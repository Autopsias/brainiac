#!/usr/bin/env python3
"""
PreToolUse guardrail: never switch branches in the MAIN checkout.

Fires before every Bash tool call in Claude Code CLI.

WHY: the main checkout holds another session's files and is the tree the
nightly runs its code from. `git switch <branch>` / `git checkout <branch>` /
`git checkout -b` there silently changes both. Code fixes start with
`git worktree add`; switching branches inside a linked worktree is fine.

RULE: block when a command segment switches branch (git switch ..., git
checkout <branch|ref>, git checkout -b/-B/--orphan/--detach/-) AND, in the
directory that segment runs in, `git rev-parse --git-dir` equals
`--git-common-dir` (that is the main checkout, not a linked worktree).

NOT blocked: `git checkout -- <file>`, `git checkout <sha> -- path`,
`git checkout <existing-file>`, `git switch` in a worktree, `git worktree add`,
and anything the hook cannot parse. Conservative: when in doubt -> ALLOW.

Exit 0 = allow (silent).  Exit 2 = block (stderr shown to the agent).

Registered in .claude/settings.json PreToolUse["Bash"].
"""

import json
import os
import re
import shlex
import subprocess
import sys

_SPLIT = re.compile(r"&&|\|\||[;|\n&]")
_GIT_GLOBAL_WITH_ARG = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_SWITCHING_FLAGS = frozenset({"-b", "-B", "--orphan", "--detach", "-d"})
_WRAPPERS = frozenset({"sudo", "env", "command", "time"})

_DENY = """\
GUARDRAIL: branch switch in the MAIN checkout blocked.

Another session's files live in the main checkout and the nightly runs its
code. Do not change its branch. Work in a worktree instead:

  git worktree add ../<name> -b <branch> origin/main

(`git switch` / `git checkout <branch>` inside a worktree is fine, and so are
`git checkout -- <file>` and `git checkout <sha> -- <path>`.)

Hook: .claude/hooks/block-main-checkout-branch-switch.py
"""


def _git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _is_main_checkout(cwd: str) -> bool:
    out = _git(cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    if not out:
        return False                    # not a repo / git failed -> ALLOW
    lines = out.splitlines()
    return len(lines) == 2 and os.path.realpath(lines[0]) == os.path.realpath(lines[1])


def _checkout_switches(args: list[str], cwd: str) -> bool:
    if "--" in args:
        return False                    # path-restore form
    if any(a in _SWITCHING_FLAGS or a.startswith("-b") for a in args) or "-" in args:
        return True
    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        return False
    target = positional[0]
    for ref in (f"refs/heads/{target}", f"refs/remotes/{target}"):
        if _git(cwd, "rev-parse", "--verify", "--quiet", ref) is not None:
            return True
    if os.path.exists(os.path.join(cwd, target)):
        return False                    # a file/dir: path restore
    # sha / tag / other ref -> detached switch; unknown -> ALLOW (maybe a pathspec)
    return _git(cwd, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}") is not None


def _segment_switch(tokens: list[str], cwd: str) -> tuple[bool, str]:
    """(switches, cwd_git_runs_in) for one simple command's tokens."""
    i = 0
    while i < len(tokens) and (re.match(r"^[A-Za-z_]\w*=", tokens[i]) or tokens[i] in _WRAPPERS):
        i += 1
    if i >= len(tokens) or tokens[i].rsplit("/", 1)[-1] != "git":
        return False, cwd
    i += 1
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] == "-C" and i + 1 < len(tokens):
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(tokens[i + 1])))
        i += 2 if tokens[i] in _GIT_GLOBAL_WITH_ARG else 1
    if i >= len(tokens):
        return False, cwd
    sub, args = tokens[i], tokens[i + 1:]
    if sub == "switch":
        return not (set(args) & {"-h", "--help"}), cwd
    if sub == "checkout":
        return _checkout_switches(args, cwd), cwd
    return False, cwd


def _blocked(cmd: str, cwd: str) -> bool:
    if "git" not in cmd:
        return False
    cur = cwd
    for seg in _SPLIT.split(cmd):
        try:
            tokens = shlex.split(seg.strip().lstrip("("))
        except ValueError:
            continue
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) > 1:
            cur = os.path.normpath(os.path.join(cur, os.path.expanduser(tokens[1])))
            continue
        switches, run_cwd = _segment_switch(tokens, cur)
        if switches and _is_main_checkout(run_cwd):
            return True
    return False


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not cmd or not cmd.strip():
        sys.exit(0)
    cwd = data.get("cwd") or os.getcwd()
    if _blocked(cmd, cwd):
        sys.stderr.write(_DENY)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:               # fail-open on the hook's OWN bug
        sys.exit(0)
