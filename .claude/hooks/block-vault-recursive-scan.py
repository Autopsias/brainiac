#!/usr/bin/env python3
"""
PreToolUse guardrail: block recursive bash scans over the vault root.

Fires before every Bash tool call in Claude Code CLI.

WHY: bash `find <vault-root>` / `grep -r` over vault content (which now
includes ``.brain/model`` and ``.brain/engine`` — a bundled ONNX model +
engine copy) can stall and lock the next shell call. This hook is the
deterministic backstop; the Grep/Glob TOOLS are DOM-level fast and never
stall, so this hook steers the agent to those instead of prose alone.

Ported from the reference vault's block-vault-recursive-scan.py (sha256
69c34dc0e5a47cfa5b72238fabc173690832a748c2a047c96c25d27128778ce6 —
ADR-0003 Appendix B, session s07). Adapted:
  - VAULT_ROOTS is resolved dynamically at runtime ($BRAIN_VAULT env var,
    else "$CLAUDE_PROJECT_DIR/vault", else "<tool-call cwd>/vault") instead
    of a hardcoded absolute path — this kernel repo ships to many
    vaults, not one.
  - the Obsidian-plugins carve-out is dropped (no Obsidian in this substrate).
  - the denial message points at this repo's docs instead of a
    reference-vault-only rule file.
The single-simple-command detection, grep/find argument parsing, and the
conservative "when in doubt, ALLOW" design are otherwise unchanged — that
logic is already hardened by three prior reference-vault regressions.

DESIGN — NARROW, single-simple-command only:

  Governing principle: CONSERVATIVE. A rare MISS is acceptable; a
  FALSE-BLOCK of a benign command is the worst outcome. When in doubt -> ALLOW.

  1. Bail to ALLOW on ANY shell complexity: a pipe `|`, `&&`/`||`/`&`, `;`, a
     newline, command substitution `$(...)` or backticks, a leading subshell
     `(`, process substitution `<(`/`>(`, or a leading `cd`.
  2. shlex.split the now-simple command. If it raises -> ALLOW.
  3. Only inspect `grep`/`egrep`/`fgrep`/`zgrep`/... and `find` as the
     command word. Anything else (including `python ... rglob`) -> ALLOW.
  4. grep: deny only a clean recursive grep whose path operand(s) are inside
     the vault (or absent -> cwd=vault). Ambiguous combined flag -> ALLOW.
  5. find: deny only when a search root resolves inside the vault (or
     absent -> cwd=vault). Non-vault root -> ALLOW.

KNOWN LIMITATIONS (accepted, conservative-by-design -- a miss beats a false-block):
  - Compound/pipelined/subshell/`cd`/command-substitution forms are out of
    scope (ALLOW).
  - A search root written AFTER a predicate in non-standard order collapses
    to the cwd default.
  - `python ... rglob` over the vault is not inspected (ALLOW; rare).

Exit 0 = allow (silent).  Exit 2 = block (stderr shown to the agent).

Registered in .claude/settings.json PreToolUse["Bash"].
"""

import json
import os
import re
import shlex
import sys

# Any of these in the raw command means "not a single simple command" -> ALLOW.
_COMPLEXITY = ("|", "&", ";", "\n", "$(", "`", "<(", ">(")

# Leading words that precede the real command word (env, sudo, wrappers).
_SKIP_LEADING = frozenset({"sudo", "time", "nice", "env", "command", "builtin", "exec", "\\"})

# grep aliases treated as a grep command.
_GREP_CMDS = frozenset({"grep", "egrep", "fgrep", "rgrep", "bzgrep", "zgrep", "xzgrep"})

# grep short flags that CONSUME the next token (so a combined cluster holding
# one of these is ambiguous -> ALLOW rather than risk mis-splitting pattern/path).
_GREP_ARG_SHORT = frozenset({"A", "B", "C", "m", "e", "f", "d", "D"})

# Long grep flags that are self-contained booleans (no next-token consumption).
_GREP_BOOL_LONG = frozenset(
    {
        "--recursive", "--dereference-recursive", "--line-number",
        "--no-filename", "--with-filename", "--files-with-matches",
        "--files-without-match", "--ignore-case", "--invert-match",
        "--word-regexp", "--line-regexp", "--count", "--null", "--byte-offset",
        "--extended-regexp", "--fixed-strings", "--basic-regexp",
        "--perl-regexp", "--only-matching", "--no-messages", "--text",
        "--quiet", "--silent", "--version", "--help",
    }
)

# find global options that PRECEDE the search roots (bare, no argument).
_FIND_GLOBAL_BARE = frozenset({"-H", "-L", "-P"})
_FIND_PREDICATE_BREAK = frozenset({"!", "(", ")", "-o", "-a", "-and", "-or", "-not", r"\(", r"\)"})

# A token (POST-shlex) that is a redirect operator -- possibly with the target
# attached (`>/tmp/out`, `2>>log`) or detached (`<` `...`).
_REDIRECT_TOK = re.compile(r"^([0-9]*&?>>?|<<?<?)(.*)$")

_DENY = """\
GUARDRAIL: recursive scan over the vault root blocked.

Use the Grep TOOL (content search) or Glob TOOL (path/name discovery)
instead. These are DOM-level fast and never stall.

bash find/grep -r over the vault (which includes .brain/model + .brain/engine)
can stall and lock the next shell call.

  Grep tool  -> content search across vault files (multiline: true for cross-paragraph)
  Glob tool  -> file path/name discovery

If your target is OUTSIDE the vault, this hook should not have fired -- re-run
naming the explicit non-vault path.

Hook: .claude/hooks/block-vault-recursive-scan.py
Docs: docs/session-memory.md
"""


# -- vault-root resolution ----------------------------------------------------

def _vault_roots(cwd: str) -> tuple[str, ...]:
    """Resolve vault root(s): $BRAIN_VAULT > $CLAUDE_PROJECT_DIR/vault > cwd/vault.

    Mirrors ``brain.config.vault_root``'s precedence (explicit > env > CWD/vault),
    substituting "explicit" with $CLAUDE_PROJECT_DIR/vault since this hook has no
    CLI arg to receive an explicit override.
    """
    roots = []
    env_vault = os.environ.get("BRAIN_VAULT")
    if env_vault:
        roots.append(os.path.normpath(os.path.expanduser(env_vault)))
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        roots.append(os.path.normpath(os.path.join(os.path.expanduser(proj), "vault")))
    roots.append(os.path.normpath(os.path.join(cwd, "vault")))
    # de-dup, keep order
    seen: set[str] = set()
    out = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return tuple(out)


# -- path helpers --------------------------------------------------------------

def _drop_redirects(tokens: list[str]) -> list[str]:
    """Remove redirect operators + their targets from an already-shlex'd token list.

    Operates on tokens (not the raw string), so a literal `<`/`>` inside a quoted
    pattern (e.g. `grep -rn "less <than" ...`) stays intact -- shlex already grouped
    it into one operand token that does not start with a redirect operator.
    """
    out: list[str] = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        m = _REDIRECT_TOK.match(t)
        if m:
            if m.group(2):          # target attached, e.g. `>/tmp/out` -> drop token
                continue
            skip_next = True        # detached, e.g. `<` `...` -> drop operator + target
            continue
        out.append(t)
    return out


def _resolve(path: str, cwd: str) -> str:
    """Resolve a (possibly relative / dot) path to a normalised absolute path."""
    p = path if path else "."
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def _under(abs_path: str, root: str) -> bool:
    return abs_path == root or abs_path.startswith(root + os.sep)


def _is_under_vault(abs_path: str, vault_roots: tuple[str, ...]) -> bool:
    return any(_under(abs_path, r) for r in vault_roots)


# -- grep -----------------------------------------------------------------------

def _check_grep(args: list[str], cwd: str, vault_roots: tuple[str, ...]) -> tuple[bool, str]:
    """
    Deny only a CLEAN recursive grep whose path operand(s) are inside the vault.
    Ambiguous combined flags, unrecognised bare long flags -> ALLOW.
    """
    recursive = False
    pattern_seen = False
    end_opts = False
    paths: list[str] = []

    for tok in args:
        if end_opts:
            if pattern_seen:
                paths.append(tok)
            else:
                pattern_seen = True   # first operand after -- is the PATTERN
            continue

        if tok == "--":
            end_opts = True
            continue

        if tok.startswith("--"):
            base = tok.split("=", 1)[0]
            if base in ("--recursive", "--dereference-recursive"):
                recursive = True
                continue
            if "=" in tok:          # self-contained, e.g. --include=*.md
                continue
            if base in _GREP_BOOL_LONG:
                continue
            return False, ""        # unknown bare long flag -> ambiguous -> ALLOW

        if tok.startswith("-") and len(tok) > 1:
            letters = tok[1:]
            if any(c in _GREP_ARG_SHORT for c in letters):
                return False, ""    # arg-taking letter in cluster -> ambiguous -> ALLOW
            if "r" in letters or "R" in letters:
                recursive = True
            continue

        # Operand.
        if not pattern_seen:
            pattern_seen = True     # this is the PATTERN
        else:
            paths.append(tok)

    if not recursive:
        return False, ""

    targets = paths if paths else ["."]   # no path -> grep defaults to cwd
    for tgt in targets:
        if _is_under_vault(_resolve(tgt, cwd), vault_roots):
            return True, (
                f"recursive grep over vault path '{tgt}' "
                "(can stall + lock the next shell call)"
            )
    return False, ""                # all explicit paths outside vault -> ALLOW


# -- find -------------------------------------------------------------------------

def _check_find(args: list[str], cwd: str, vault_roots: tuple[str, ...]) -> tuple[bool, str]:
    """
    Deny only a find that recursively descends a vault DIRECTORY.

    Conservative bail-outs (-> ALLOW), each one a benign command the design must
    not false-block:
      - -maxdepth 0|1     -> shallow, never descends .brain/model etc.
      - leading global option (-H/-L/-P/-D arg/-O[n]) before the path.
      - a root that is a single FILE (find on a file cannot recurse a tree).
      - a non-existent / non-directory root (find errors fast, no stall).
    """
    # Shallow searches never hit the deep-descent stall.
    for j, t in enumerate(args):
        if t == "-maxdepth" and j + 1 < len(args) and args[j + 1] in ("0", "1"):
            return False, ""

    # Skip leading global options so an explicit non-vault root after them is seen.
    i = 0
    while i < len(args):
        t = args[i]
        if t in _FIND_GLOBAL_BARE:
            i += 1
            continue
        if t == "-D":                       # -D debugopts (takes an arg)
            i += 2
            continue
        if t.startswith("-O"):              # -O<n> (attached) or -O <n>
            i += 2 if t == "-O" else 1
            continue
        break

    # Collect the search roots (operands up to the first predicate/option).
    roots: list[str] = []
    while i < len(args):
        t = args[i]
        if t in _FIND_PREDICATE_BREAK or t.startswith("-"):
            break                           # expression begins; roots end
        roots.append(t)
        i += 1

    targets = roots if roots else ["."]     # no root -> find defaults to cwd
    for tgt in targets:
        abs_t = _resolve(tgt, cwd)
        if not _is_under_vault(abs_t, vault_roots):
            continue                        # explicit non-vault root -> ALLOW
        if any(c in tgt for c in "*?["):    # an UNEXPANDED glob under the vault
            return True, (                  # bash expands it to real dirs -> would recurse
                f"`find {tgt}` globs a vault path "
                "(expands to vault dirs -> can stall)"
            )
        if not os.path.isdir(abs_t):
            continue                        # a file / nonexistent root cannot recurse -> ALLOW
        return True, (
            f"`find {tgt}` searches a vault directory "
            "(descends .brain/model etc -> can stall + lock next call)"
        )
    return False, ""                        # all roots outside vault / non-dir -> ALLOW


# -- main -----------------------------------------------------------------------

def _deny(reason: str) -> None:
    sys.stderr.write(_DENY + f"\nBlocked because: {reason}\n")
    sys.exit(2)


def _command_word(tokens: list[str]) -> tuple[str | None, int]:
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):   # VAR=value
            i += 1
            continue
        if t in _SKIP_LEADING:
            i += 1
            continue
        return t, i
    return None, len(tokens)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)                 # unparseable input -> ALLOW

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (data.get("tool_input") or {}).get("command", "")
    if not cmd or not cmd.strip():
        sys.exit(0)

    # (1) Bail to ALLOW on ANY shell complexity -> single simple command only.
    stripped = cmd.strip()
    if stripped.startswith("(") or stripped.startswith("cd ") or stripped == "cd":
        sys.exit(0)
    if any(marker in cmd for marker in _COMPLEXITY):
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()
    vault_roots = _vault_roots(cwd)

    # (2) Tokenise FIRST (shlex handles quotes correctly); failure -> ALLOW.
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        sys.exit(0)
    # Then drop redirect operators + targets, so a `> /tmp/out` / `< /dev/null`
    # is never mistaken for a search path.
    tokens = _drop_redirects(tokens)
    if not tokens:
        sys.exit(0)

    word, idx = _command_word(tokens)
    if word is None:
        sys.exit(0)
    base = word.rsplit("/", 1)[-1]

    # (3) Only grep and find are in scope.
    if base in _GREP_CMDS:
        blocked, reason = _check_grep(tokens[idx + 1:], cwd, vault_roots)
        if blocked:
            _deny(reason)
    elif base == "find":
        blocked, reason = _check_find(tokens[idx + 1:], cwd, vault_roots)
        if blocked:
            _deny(reason)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:               # fail-open on the hook's OWN bug
        sys.exit(0)
