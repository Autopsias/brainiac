"""Narrow-grammar shell analyzer for the SEC-07 PreToolUse egress guard.

Explicit grammar scope (Bash / POSIX shell):
- Top-level command sequences separated by ``|``, ``||``, ``&&``, ``;``, ``&``,
  and newlines, tokenized with ``shlex(..., posix=True)``.
- Leading ``NAME=VALUE`` environment prefixes and wrapper commands (``env``,
  ``command``, ``builtin``, ``nohup``, ``nice``, ``time``, ``timeout``,
  ``exec``, ``xargs``, ``find -exec``, ``bash/sh/zsh/dash -c``).
- Multiline heredocs (``<<EOF`` / ``<<'EOF'`` / ``<<"EOF"`` / ``<<-EOF``):
  benign prose bodies are separated before tokenizing so apostrophes do not
  trigger malformed-quote false positives, while heredoc content is scanned
  when piped/fed to outbound tools, external redirects, or interpreters.
- Outbound binaries, ``git`` global options + outbound subcommands + risky
  ``-c`` overrides (``core.sshCommand``, ``credential.helper``, ``http.proxy``),
  inline ``python``/``node``/etc. HTTP, literal URLs, external output redirects
  (including ``tee``), and bounded ``@file`` / ``< file`` payload attachments.

Unhandled shell constructs — FAIL CLOSED with a clear deny reason:
- Malformed or unterminated quoting (``shlex`` ``ValueError``)
- ANSI-C quoting (``$'...'``)
- Command substitution (``$(...)`` and backticks)
- Process substitution (``<(...)`` and ``>(...)``)
- Dynamic evaluation (``eval``)
- Unterminated heredocs

Explicit grammar scope (PowerShell):
- Selected by tool name (``PowerShell``), NEVER ``sys.platform``, so Git Bash /
  WSL on Windows still uses the Bash analyzer.
- Fails closed on backtick obfuscation, ``$(...)`` subexpressions, unbalanced
  quotes, ``Invoke-Expression``/``iex``, ``-EncodedCommand``/``-enc``,
  ``Invoke-Command``/``icm``, and script-block invocation (``& {`` / ``. {``).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Sequence
from typing import NamedTuple

from ._egress_guard_lexer import (
    _IGNORED_REDIRECT_TARGETS,
    _check_forbidden_bash,
    _check_forbidden_heredoc,
    _extract_heredoc_items,
    _extract_redirects,  # noqa: F401 - re-exported for PS/tests
    _extract_redirects_ops,
    _tok_pair,
    _tokenize_bash,  # noqa: F401 - re-exported for tests
    _tokenize_bash_ops,  # noqa: F401 - re-exported for tests
    _tokenize_bash_ops_with_seps,
    _unwrap_group_tokens,
)
from ._egress_guard_paths import (
    is_abs_path,
    is_contained_write,
    read_attachment_bounded,
    resolve_attachment_path,
)

_MAX_UNWRAP_DEPTH = 8

_OUTBOUND_BINS = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "ftp",
        "lftp",
        "httpie",
        "http",
        "xh",
        "gh",
        "glab",
        "aws",
        "az",
        "gcloud",
        "gsutil",
        "s3cmd",
        "rclone",
        "kubectl",
        "mail",
        "mailx",
        "sendmail",
        "msmtp",
        "mutt",
        "osascript",
        "socat",
        "dig",
        "nslookup",
        "host",
        "whois",
    }
)
_GIT_OUTBOUND_SUBS = frozenset(
    {
        "push",
        "clone",
        "fetch",
        "pull",
        "remote",
        "ls-remote",
        "send-email",
        "submodule",
    }
)
_GIT_RISKY_CONFIG_KEYS = frozenset(
    {
        "core.sshcommand",
        "core.fsmonitor",
        "credential.helper",
        "http.proxy",
        "https.proxy",
    }
)
_GIT_GLOBAL_TAKES_ARG = frozenset(
    {
        "-c",
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--config-env",
        "--super-prefix",
    }
)
_INTERPRETERS = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "nodejs",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
    }
)
_SHELL_BINS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_UPLOAD_FLAGS = frozenset(
    {
        "-T",
        "--upload-file",
        "-K",
        "--config",
        "--post-file",
        "--body-file",
        "--netrc-file",
    }
)
_UPLOAD_EQ_PREFIXES = (
    "--post-file=",
    "--body-file=",
    "--upload-file=",
    "--config=",
    "--netrc-file=",
)
_DATA_FLAGS = frozenset(
    {
        "-d",
        "--data",
        "--data-binary",
        "--data-urlencode",
        "--data-ascii",
        "--data-raw",
        "--json",
        "-F",
        "--form",
        "--form-string",
    }
)
_INLINE_NET_RE = re.compile(
    r"(?:https?|ftp|sftp|ssh)://|\b(?:urllib|http\.client|requests|httpx|"
    r"aiohttp|socket|smtplib|ftplib|xmlrpc|XMLHttpRequest|WebSocket|"
    r"net\.connect|dgram|fetch\s*\(|Net\.WebClient|System\.Net\.Http)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?:https?|ftp|sftp|ssh)://", re.IGNORECASE)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class ShellAnalysis(NamedTuple):
    outbound: bool
    deny_reason: str | None
    extra_text: str
    resolved_paths: tuple[str, ...] = ()


class _SegResult(NamedTuple):
    outbound: bool
    deny: str | None = None
    atts: tuple[tuple[str, str | None], ...] = ()
    extra: str = ""
    cand_cwd: str | None = None
    cd_changed: bool = False
    exits_branch: bool = False
    resolved: tuple[str, ...] = ()


def _bin_name(token: str) -> str:
    base = os.path.basename(token.replace("\\", "/")).lower()
    return base[:-4] if base.endswith(".exe") else base


def _strip_env_prefixes(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and _ENV_ASSIGN_RE.match(argv[i]):
        i += 1
    return argv[i:]


def _resolve_dir_change(cur_cwd: str | None, target: str) -> str | None:
    """Resolve a `cd`/`env -C` target against ``cur_cwd``; return ``None`` if dynamic."""
    if not target:
        return os.path.expanduser("~")
    if target == "-" or "$" in target:
        return None
    if (
        target == "~"
        or target.startswith("~/")
        or (os.name == "nt" and target.startswith("~\\"))
    ):
        return os.path.normpath(os.path.expanduser("~") + target[1:])
    if is_abs_path(target):
        return os.path.normpath(target)
    if cur_cwd is None:
        return None
    return os.path.normpath(os.path.join(cur_cwd, target))


def _unwrap_env(args: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(remaining_argv, chdir_targets)`` for an `env` wrapper."""
    i = 0
    chdirs: list[str] = []
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return (args[i + 1 :], chdirs)
        if tok in ("-C", "--chdir") and i + 1 < len(args):
            chdirs.append(args[i + 1])
            i += 2
            continue
        if tok.startswith("--chdir="):
            chdirs.append(tok[8:])
            i += 1
            continue
        if tok.startswith("-C") and len(tok) > 2:
            chdirs.append(tok[2:])
            i += 1
            continue
        if tok in ("-u", "--unset"):
            i += 2
            continue
        if tok.startswith("-") or _ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        return (args[i:], chdirs)
    return ([], chdirs)


def _unwrap_nice(rest: list[str]) -> list[str]:
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            return rest[i + 1 :]
        if tok in ("-n", "--adjustment"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return rest[i:]
    return []


def _unwrap_timeout(rest: list[str]) -> list[str]:
    """Unwrap `timeout [OPTS] DURATION COMMAND [ARGS...]` consuming DURATION once."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            i += 1
            break
        if tok in ("-s", "--signal", "-k", "--kill-after"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return rest[i + 1 :] if i < len(rest) else []


def _unwrap_xargs(args: list[str], attachments: list[str]) -> list[str]:
    i = 0
    takes_arg = {"-I", "-L", "-n", "-P", "-d", "-s", "-E", "-J"}
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[i + 1 :]
        if tok == "-a" and i + 1 < len(args):
            attachments.append(args[i + 1])
            i += 2
            continue
        if tok.startswith("-a") and len(tok) > 2:
            attachments.append(tok[2:])
            i += 1
            continue
        i += 2 if tok in takes_arg else (1 if tok.startswith("-") else 0)
        if not tok.startswith("-"):
            return args[i:]
    return []


def _extract_find_execs(args: list[str]) -> list[list[str]]:
    """Collect ALL `-exec`/`-execdir`/`-ok`/`-okdir` sub-commands from `find` args."""
    cmds: list[list[str]] = []
    i, n = 0, len(args)
    while i < n:
        if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            cmd: list[str] = []
            i += 1
            while i < n and args[i] not in (";", "+"):
                cmd.append(args[i])
                i += 1
            if cmd:
                cmds.append(cmd[:256])
        i += 1
    return cmds


def _extract_shell_c(args: list[str]) -> str | None:
    for idx, tok in enumerate(args):
        if tok == "--":
            return None
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            return args[idx + 1] if idx + 1 < len(args) else None
        if not tok.startswith("-"):
            return None
    return None


def _unwrap_once(
    name: str,
    rest: list[str],
    attachments: list[str],
) -> tuple[list[str] | None, str | None, list[str]]:
    if name == "env":
        rem, chdirs = _unwrap_env(rest)
        return (rem, None, chdirs)
    if name in ("command", "builtin", "nohup", "exec", "time"):
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 1
        return (rest[i:], None, [])
    if name == "nice":
        return (_unwrap_nice(rest), None, [])
    if name == "timeout":
        return (_unwrap_timeout(rest), None, [])
    if name == "xargs":
        return (_unwrap_xargs(rest, attachments), None, [])
    if name in _SHELL_BINS:
        return (None, _extract_shell_c(rest), [])
    return (None, None, [])


def _unwrap_command(
    argv: list[str],
    attachments: list[str],
    seg_cwd: str | None,
    *,
    max_steps: int = _MAX_UNWRAP_DEPTH,
) -> tuple[list[str], str | None, str | None, str | None]:
    cur = _strip_env_prefixes(argv)
    eff_cwd = seg_cwd
    for _ in range(max(0, max_steps)):
        if not cur:
            return ([], None, eff_cwd, None)
        next_argv, shell_c, chdirs = _unwrap_once(
            _bin_name(cur[0]), cur[1:], attachments
        )
        for d in chdirs:
            eff_cwd = _resolve_dir_change(eff_cwd, d)
        if shell_c is not None:
            return (cur, shell_c, eff_cwd, None)
        if next_argv is None:
            return (cur, None, eff_cwd, None)
        cur = _strip_env_prefixes(next_argv)
    if not cur:
        return ([], None, eff_cwd, None)
    return (
        [],
        None,
        eff_cwd,
        "wrapper nesting depth exceeded — REFUSING, fail-closed.",
    )


def _is_git_outbound(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-c" and i + 1 < len(args):
            kv = args[i + 1].split("=", 1)[0].strip().lower()
            if kv in _GIT_RISKY_CONFIG_KEYS or kv.startswith(("url.", "alias.")):
                return True
            i += 2
            continue
        if tok.startswith("-c") and len(tok) > 2:
            kv = tok[2:].split("=", 1)[0].strip().lower()
            if kv in _GIT_RISKY_CONFIG_KEYS or kv.startswith(("url.", "alias.")):
                return True
            i += 1
            continue
        if tok in _GIT_GLOBAL_TAKES_ARG:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok.lower() in _GIT_OUTBOUND_SUBS
    return False


def _is_inline_interpreter_outbound(args: list[str], heredoc_text: str) -> bool:
    if heredoc_text and _INLINE_NET_RE.search(heredoc_text):
        return True
    for idx, tok in enumerate(args):
        if tok in ("-c", "-e", "--eval") and idx + 1 < len(args):
            if _INLINE_NET_RE.search(args[idx + 1]):
                return True
    return False


def _at_path(
    val: str, *, allow_form_lt: bool = False, allow_name_at: bool = False
) -> str | None:
    if val.startswith("@") and val != "@-":
        return val[1:].split(";", 1)[0] if allow_form_lt else val[1:]
    if "=@" in val:
        cand = val.split("=@", 1)[1]
        return cand.split(";", 1)[0] if allow_form_lt else cand
    if allow_name_at and "@" in val:
        prefix, cand = val.split("@", 1)
        if "=" not in prefix and cand and cand != "-":
            return cand
    if allow_form_lt and "=<" in val:
        return val.split("=<", 1)[1].split(";", 1)[0]
    return None


def _collect_attachments(name: str, args: list[str]) -> list[str]:
    files: list[str] = []
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        if tok in _UPLOAD_FLAGS and i + 1 < n:
            if args[i + 1] != "-":
                files.append(args[i + 1])
            i += 2
        elif tok in _DATA_FLAGS and i + 1 < n:
            if p := _at_path(
                args[i + 1],
                allow_form_lt=tok in ("-F", "--form"),
                allow_name_at=(tok == "--data-urlencode"),
            ):
                files.append(p)
            i += 2
        elif name == "curl" and tok in ("-H", "--header") and i + 1 < n:
            if p := _at_path(args[i + 1]):
                files.append(p)
            i += 2
        else:
            if p := _extract_inline_attachment(name, tok):
                files.append(p)
            i += 1
    return files


def _extract_inline_attachment(name: str, tok: str) -> str | None:
    if "=" in tok:
        _, rhs = tok.split("=", 1)
        if tok.startswith(_UPLOAD_EQ_PREFIXES):
            return rhs if rhs and rhs != "-" else None
        is_form = tok.startswith(("-F", "--form="))
        is_urlencode = tok.startswith("--data-urlencode=")
        return _at_path(rhs, allow_form_lt=is_form, allow_name_at=is_urlencode) or (
            rhs[1:].split(";", 1)[0]
            if is_form and rhs.startswith("<") and len(rhs) > 1
            else None
        )
    if name == "curl" and tok.startswith(("-d@", "-F@", "-H@")) and len(tok) > 3:
        cand = tok[3:]
        return cand if cand and cand != "-" else None
    if name == "curl" and tok.startswith(("-T", "-K")) and len(tok) > 2:
        cand = tok[2:]
        return cand if cand and cand != "-" else None
    return tok[1:] if tok.startswith("@") and tok != "@-" else None


def _is_command_outbound(
    unwrapped: list[str],
    *,
    cmd_cwds: list[str | None],
    trusted_cwd: str,
    guard_vault: str,
    heredoc_text: str,
) -> bool:
    name, args = _bin_name(unwrapped[0]), unwrapped[1:]
    if name in _OUTBOUND_BINS:
        return True
    if name == "git" and _is_git_outbound(args):
        return True
    if name in _INTERPRETERS and _is_inline_interpreter_outbound(args, heredoc_text):
        return True
    if name == "tee" and any(
        c is None
        or not is_contained_write(
            a, trusted_cwd, os.getcwd(), guard_vault, rel_base=c
        )
        for a in args
        if not a.startswith("-") and a not in _IGNORED_REDIRECT_TARGETS
        for c in cmd_cwds
    ):
        return True
    return any(_URL_RE.search(t) for t in unwrapped)


def _extract_cd_target(args: list[str]) -> str:
    for tok in args:
        if tok == "--":
            continue
        if tok.startswith("-") and tok != "-":
            continue
        return tok
    return ""


def _classify_find_execs(
    exec_cmds: list[list[str]],
    *,
    cmd_cwd: str | None,
    trusted_cwd: str,
    guard_vault: str,
    heredoc_text: str,
    deadline: float,
    depth: int,
) -> tuple[bool, str | None, list[tuple[str, str | None]], str, list[str]]:
    if depth >= _MAX_UNWRAP_DEPTH:
        return (
            False,
            "wrapper nesting depth exceeded — REFUSING, fail-closed.",
            [],
            "",
            [],
        )
    outbound = False
    atts: list[tuple[str, str | None]] = []
    extras: list[str] = []
    resolved: list[str] = []
    for sub_argv in exec_cmds:
        res = _classify_argv(
            sub_argv,
            shell_cwds=[cmd_cwd],
            trusted_cwd=trusted_cwd,
            guard_vault=guard_vault,
            heredoc_text=heredoc_text,
            deadline=deadline,
            depth=depth + 1,
        )
        if res.deny:
            return (False, res.deny, [], "", [])
        outbound = outbound or res.outbound
        atts.extend(res.atts)
        if res.extra:
            extras.append(res.extra)
        resolved.extend(res.resolved)
    return (outbound, None, atts, "\n".join(extras), resolved)


def _classify_shell_c(
    shell_c: str,
    *,
    atts: tuple[tuple[str, str | None], ...],
    shell_cwd: str | None,
    cmd_cwd: str | None,
    trusted_cwd: str,
    guard_vault: str,
    deadline: float,
    depth: int,
) -> _SegResult:
    if depth >= _MAX_UNWRAP_DEPTH:
        return _SegResult(
            False, "nested shell wrapper depth exceeded — REFUSING, fail-closed."
        )
    sub = analyze_bash(
        shell_c,
        cwd=cmd_cwd,
        trusted_cwd=trusted_cwd,
        guard_vault=guard_vault,
        deadline=deadline,
        depth=depth + 1,
    )
    return _SegResult(
        sub.outbound,
        sub.deny_reason,
        atts,
        sub.extra_text,
        shell_cwd,
        False,
        False,
        sub.resolved_paths,
    )


def _classify_argv(
    argv: list[str],
    *,
    shell_cwds: list[str | None],
    trusted_cwd: str,
    guard_vault: str,
    heredoc_text: str,
    deadline: float,
    depth: int,
) -> _SegResult:
    shell_cwd = shell_cwds[0]
    cmd_atts: list[str] = []
    unwrapped, shell_c, cmd_cwd, unwrap_err = _unwrap_command(
        argv, cmd_atts, shell_cwd, max_steps=_MAX_UNWRAP_DEPTH - depth
    )
    if unwrap_err:
        return _SegResult(False, unwrap_err)
    eff_cwds = shell_cwds if cmd_cwd == shell_cwd else [cmd_cwd]
    atts = tuple((p, c) for p in cmd_atts for c in eff_cwds)
    if shell_c is not None:
        return _classify_shell_c(
            shell_c,
            atts=atts,
            shell_cwd=shell_cwd,
            cmd_cwd=cmd_cwd,
            trusted_cwd=trusted_cwd,
            guard_vault=guard_vault,
            deadline=deadline,
            depth=depth,
        )
    if not unwrapped:
        return _SegResult(False, None, atts, "", shell_cwd)
    name = _bin_name(unwrapped[0])
    if name == "eval":
        return _SegResult(False, "`eval` is not supported — REFUSING, fail-closed.")
    if name == "find":
        if exec_cmds := _extract_find_execs(unwrapped[1:]):
            f_out, f_deny, f_atts, f_extra, f_res = _classify_find_execs(
                exec_cmds,
                cmd_cwd=cmd_cwd,
                trusted_cwd=trusted_cwd,
                guard_vault=guard_vault,
                heredoc_text=heredoc_text,
                deadline=deadline,
                depth=depth,
            )
            return _SegResult(
                f_out,
                f_deny,
                atts + tuple(f_atts),
                f_extra,
                shell_cwd,
                False,
                False,
                tuple(f_res),
            )
    cd_changed = name in ("cd", "pushd")
    cand_cwd = (
        _resolve_dir_change(shell_cwd, _extract_cd_target(unwrapped[1:]))
        if cd_changed
        else shell_cwd
    )
    resolved = (cand_cwd,) if cd_changed and cand_cwd is not None else ()
    outbound = _is_command_outbound(
        unwrapped,
        cmd_cwds=eff_cwds,
        trusted_cwd=trusted_cwd,
        guard_vault=guard_vault,
        heredoc_text=heredoc_text,
    )
    if outbound:
        atts += tuple(
            (p, c) for p in _collect_attachments(name, unwrapped[1:]) for c in eff_cwds
        )
    return _SegResult(
        outbound,
        None,
        atts,
        "",
        cand_cwd,
        cd_changed,
        name in ("exit", "return"),
        resolved,
    )


def _classify_segment(
    tokens: Sequence[str | tuple[str, bool]],
    *,
    shell_cwds: list[str | None],
    open_braces: int,
    trusted_cwd: str,
    guard_vault: str,
    heredoc_text: str,
    deadline: float,
    depth: int,
) -> tuple[_SegResult, int]:
    pre_tokens, open_braces = _unwrap_group_tokens(
        [_tok_pair(t) for t in tokens], open_braces
    )
    rem_tokens, out_targets, in_files = _extract_redirects_ops(pre_tokens)
    argv_tokens, open_braces = _unwrap_group_tokens(rem_tokens, open_braces)
    argv = [t for t, _ in argv_tokens]
    redir_atts = tuple((p, c) for p in in_files for c in shell_cwds)
    redir_outbound = any(
        c is None
        or not is_contained_write(d, trusted_cwd, os.getcwd(), guard_vault, rel_base=c)
        for d in out_targets
        for c in shell_cwds
    )
    res = _classify_argv(
        argv,
        shell_cwds=shell_cwds,
        trusted_cwd=trusted_cwd,
        guard_vault=guard_vault,
        heredoc_text=heredoc_text,
        deadline=deadline,
        depth=depth,
    )
    if res.deny:
        return (res, open_braces)
    return (
        _SegResult(
            redir_outbound or res.outbound,
            None,
            redir_atts + res.atts,
            res.extra,
            res.cand_cwd,
            res.cd_changed,
            res.exits_branch,
            res.resolved,
        ),
        open_braces,
    )


def _read_all_attachments(
    attachments: list[tuple[str, str | None]],
    deadline: float,
    extra_parts: list[str],
    extra_resolved: Sequence[str] = (),
) -> ShellAnalysis:
    resolved: list[str] = list(extra_resolved)
    for path_str, att_cwd in dict.fromkeys(attachments):
        if (
            att_cwd is None
            and not is_abs_path(path_str)
            and not path_str.startswith("~")
        ):
            return ShellAnalysis(
                False,
                f"cannot resolve relative attachment {path_str!r} after dynamic directory "
                "change — REFUSING, fail-closed.",
                "",
            )
        resolved.append(resolve_attachment_path(path_str, att_cwd or ""))
        content, err = read_attachment_bounded(path_str, att_cwd or "", deadline)
        if err:
            return ShellAnalysis(False, f"{err} — REFUSING, fail-closed.", "")
        if content:
            extra_parts.append(content)
    return ShellAnalysis(
        True, None, "\n".join(extra_parts), tuple(dict.fromkeys(resolved))
    )


def _merge_or_pending(
    cur_cwd: str | None, cand_cwd: str | None, *, exits: bool
) -> tuple[str | None, list[str | None]]:
    if exits:
        if cand_cwd is None or os.path.isdir(cand_cwd):
            return (cand_cwd, [])
        return (cur_cwd, [])
    if cand_cwd is None:
        return (None, [])
    if os.path.isdir(cand_cwd) and cand_cwd != cur_cwd:
        return (cand_cwd, [cur_cwd])
    return (cur_cwd, [])


def _advance_cwd(
    cur_cwd: str | None,
    alt_cwds: list[str | None],
    in_pipe: bool,
    or_pending: tuple[bool, str | None],
    res: _SegResult,
    sep: str,
) -> tuple[str | None, list[str | None], bool, tuple[bool, str | None]]:
    """Update effective shell cwd across Bash operators (``|``, ``||``, ``&&``, ``;``, ``&``, ``\\n``)."""
    if sep in ("|", "|&"):
        return (cur_cwd, alt_cwds, True, or_pending)
    if sep == "&":
        return (cur_cwd, [], False, (False, None))
    if sep == "||":
        if res.cd_changed and not in_pipe:
            return (cur_cwd, alt_cwds, False, (True, res.cand_cwd))
        return (cur_cwd, alt_cwds, False, or_pending)
    if sep == "&&":
        if res.cd_changed and not in_pipe:
            return (res.cand_cwd, [], False, (False, None))
        if or_pending[0]:
            nxt, alts = _merge_or_pending(
                cur_cwd, or_pending[1], exits=res.exits_branch
            )
            return (nxt, alts, False, (False, None))
        return (cur_cwd, alt_cwds, False, (False, None))
    if res.cd_changed and not in_pipe:
        nxt = (
            res.cand_cwd
            if (res.cand_cwd is None or os.path.isdir(res.cand_cwd))
            else cur_cwd
        )
        return (nxt, [], False, (False, None))
    if or_pending[0]:
        nxt, alts = _merge_or_pending(cur_cwd, or_pending[1], exits=res.exits_branch)
        return (nxt, alts, False, (False, None))
    return (cur_cwd, alt_cwds, False, (False, None))


def _prepare_bash_segments(
    command: str,
) -> tuple[list[tuple[list[tuple[str, bool]], str]], str, str | None]:
    cleaned, heredoc_items, h_err = _extract_heredoc_items(command)
    if h_err:
        return ([], "", h_err)
    if forbid := _check_forbidden_bash(cleaned):
        return ([], "", forbid)
    for body, quoted in heredoc_items:
        if not quoted:
            if forbid := _check_forbidden_heredoc(body):
                return ([], "", forbid)
    segments, tok_err = _tokenize_bash_ops_with_seps(cleaned)
    if tok_err:
        return ([], "", tok_err)
    return (segments, "\n".join(b for b, _ in heredoc_items), None)


def analyze_bash(
    command: str,
    *,
    cwd: str | None,
    guard_vault: str,
    deadline: float,
    depth: int = 0,
    trusted_cwd: str | None = None,
) -> ShellAnalysis:
    """Analyze a Bash command; return outbound flag, fail-closed deny reason, and extra text."""
    segments, heredoc_text, prep_err = _prepare_bash_segments(command)
    if prep_err:
        return ShellAnalysis(False, prep_err, "")

    any_outbound = False
    all_attachments: list[tuple[str, str | None]] = []
    extra_parts: list[str] = []
    all_resolved: list[str] = []
    cur_cwd: str | None = cwd
    alt_cwds: list[str | None] = []
    in_pipe = False
    or_pending: tuple[bool, str | None] = (False, None)
    subshell_stack: list[
        tuple[str | None, list[str | None], tuple[bool, str | None]]
    ] = []
    open_braces = 0
    root_cwd = trusted_cwd if trusted_cwd is not None else (cwd or "")

    for seg, sep in segments:
        opens = sum(1 for tok, is_op in seg if is_op and tok == "(")
        closes = sum(1 for tok, is_op in seg if is_op and tok == ")")
        for _ in range(opens):
            subshell_stack.append((cur_cwd, alt_cwds, or_pending))
            in_pipe = False
            or_pending = (False, None)
        res, open_braces = _classify_segment(
            seg,
            shell_cwds=list(dict.fromkeys([cur_cwd, *alt_cwds])),
            open_braces=open_braces,
            trusted_cwd=root_cwd,
            guard_vault=guard_vault,
            heredoc_text=heredoc_text,
            deadline=deadline,
            depth=depth,
        )
        if res.deny:
            return ShellAnalysis(False, res.deny, "")
        cur_cwd, alt_cwds, in_pipe, or_pending = _advance_cwd(
            cur_cwd, alt_cwds, in_pipe, or_pending, res, sep
        )
        for _ in range(min(closes, len(subshell_stack))):
            cur_cwd, alt_cwds, or_pending = subshell_stack.pop()
            in_pipe = sep in ("|", "|&")
        any_outbound = any_outbound or res.outbound
        all_attachments.extend(res.atts)
        all_resolved.extend(res.resolved)
        if res.extra:
            extra_parts.append(res.extra)

    if not any_outbound:
        return ShellAnalysis(False, None, "", tuple(dict.fromkeys(all_resolved)))
    if decoded_text := "\n".join(" ".join(t for t, _ in seg) for seg, _ in segments):
        extra_parts.append(decoded_text)
    if heredoc_text:
        extra_parts.append(heredoc_text)
    return _read_all_attachments(all_attachments, deadline, extra_parts, all_resolved)


def analyze_shell(
    tool: str,
    command: str,
    *,
    cwd: str,
    guard_vault: str,
    deadline: float | None = None,
) -> ShellAnalysis:
    """Dispatch shell analysis by tool name (`Bash` vs `PowerShell`), NEVER `sys.platform`."""
    budget_end = deadline if deadline is not None else (time.monotonic() + 1.8)
    if tool == "PowerShell":
        from ._egress_guard_ps import analyze_powershell

        return analyze_powershell(
            command, cwd=cwd, guard_vault=guard_vault, deadline=budget_end
        )
    return analyze_bash(command, cwd=cwd, guard_vault=guard_vault, deadline=budget_end)
