"""PowerShell-aware fail-closed analyzer for the SEC-07 PreToolUse egress guard.

Split out of :mod:`brain._egress_guard_shell` to keep each module well below
the repo file-size limit and avoid compiling PowerShell tables on Bash calls.
"""

from __future__ import annotations

import os
import re

from ._egress_guard_paths import is_contained_write
from ._egress_guard_shell import (
    _INLINE_NET_RE,
    _OUTBOUND_BINS,
    _URL_RE,
    ShellAnalysis,
    _bin_name,
    _collect_attachments,
    _extract_redirects,
    _is_git_outbound,
    _read_all_attachments,
    _resolve_dir_change,
)

_PS_OUTBOUND_CMDLETS = frozenset(
    {
        "invoke-webrequest",
        "invoke-restmethod",
        "iwr",
        "irm",
        "start-bitstransfer",
        "send-mailmessage",
    }
)
_PS_EXTERNAL_CMDLETS = frozenset({"out-file", "set-content", "add-content"})
_PS_UPLOAD_OPTS = frozenset(
    {
        "-infile",
        "-attachments",
        "-source",
        "-certificate",
        "-bodyfile",
        "-postfile",
        "-uploadfile",
    }
)
_PS_BODY_OPTS = frozenset({"-form", "-body"})
_PS_READ_CMDLETS = frozenset({"get-content", "gc", "get-item", "gi"})
_PS_CD_CMDLETS = frozenset(
    {
        "set-location",
        "cd",
        "sl",
        "chdir",
        "push-location",
        "pushd",
    }
)
_PS_FAIL_CLOSED_RE = re.compile(
    r"(?:\$\(|\b(?:invoke-expression|iex|invoke-command|icm)\b|"
    r"(?:^|\s)-(?:encodedcommand|enc|ec)(?:\s|$)|[&.]\s*\{)",
    re.IGNORECASE,
)
_PS_BACKTICK_MAP = {
    '"': '"',
    "'": "'",
    "`": "`",
    "$": "$",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "0": "\0",
}
_PS_REDIR_OPS = (">&", "<&", ">>", ">", "<")


def _split_ps_segments(command: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if quote == '"' and ch == "`" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and command[i + 1] == quote:
                    buf.append(command[i + 1])
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in (";", "|", "\n") or command[i : i + 2] in ("&&", "||"):
            if seg := "".join(buf).strip():
                segments.append(seg)
            buf = []
            i += 2 if command[i : i + 2] in ("&&", "||") else 1
            continue
        buf.append(ch)
        i += 1
    if tail := "".join(buf).strip():
        segments.append(tail)
    return segments


def _read_ps_single_quote(seg: str, i: int, cur: list[str]) -> tuple[int, str | None]:
    j, n = i + 1, len(seg)
    while j < n:
        if seg[j] == "'":
            if j + 1 < n and seg[j + 1] == "'":
                cur.append("'")
                j += 2
                continue
            return (j + 1, None)
        cur.append(seg[j])
        j += 1
    return (n, "unterminated quote in PowerShell command — REFUSING, fail-closed.")


def _read_ps_double_quote(seg: str, i: int, cur: list[str]) -> tuple[int, str | None]:
    j, n = i + 1, len(seg)
    while j < n:
        ch = seg[j]
        if ch == '"':
            if j + 1 < n and seg[j + 1] == '"':
                cur.append('"')
                j += 2
                continue
            return (j + 1, None)
        if ch == "`":
            if j + 1 < n and seg[j + 1] in _PS_BACKTICK_MAP:
                cur.append(_PS_BACKTICK_MAP[seg[j + 1]])
                j += 2
                continue
            return (
                n,
                "obfuscated PowerShell backtick escape — REFUSING, fail-closed.",
            )
        if ch == "$" and j + 1 < n and seg[j + 1] == "(":
            return (
                n,
                "obfuscated or dynamic PowerShell construct ($(...)) — REFUSING, fail-closed.",
            )
        cur.append(ch)
        j += 1
    return (n, "unterminated quote in PowerShell command — REFUSING, fail-closed.")


def _match_ps_redir_op(seg: str, i: int) -> str | None:
    for op in _PS_REDIR_OPS:
        if seg.startswith(op, i):
            return op
    return None


def _tokenize_ps_segment(seg: str) -> tuple[list[tuple[str, bool]], str | None]:
    """Tokenize one PowerShell segment with native quote/backtick decoding."""
    tokens: list[tuple[str, bool]] = []
    cur: list[str] = []
    has_tok = False
    i, n = 0, len(seg)
    while i < n:
        ch = seg[i]
        if ch in " \t\r\n":
            if has_tok:
                tokens.append(("".join(cur), False))
                cur, has_tok = [], False
            i += 1
            continue
        if ch == "'":
            has_tok = True
            i, err = _read_ps_single_quote(seg, i, cur)
            if err:
                return ([], err)
            continue
        if ch == '"':
            has_tok = True
            i, err = _read_ps_double_quote(seg, i, cur)
            if err:
                return ([], err)
            continue
        if ch == "`":
            return (
                [],
                "obfuscated or dynamic PowerShell construct (backtick) — REFUSING, fail-closed.",
            )
        if op := _match_ps_redir_op(seg, i):
            if has_tok:
                tokens.append(("".join(cur), False))
                cur, has_tok = [], False
            tokens.append((op, True))
            i += len(op)
            continue
        cur.append(ch)
        has_tok = True
        i += 1
    if has_tok:
        tokens.append(("".join(cur), False))
    return (tokens, None)


def _ps_external_cmdlet_target(args: list[str]) -> str | None:
    for idx, tok in enumerate(args):
        if tok.lower() in ("-filepath", "-path", "-literalpath") and idx + 1 < len(
            args
        ):
            return args[idx + 1]
    for tok in args:
        if not tok.startswith("-"):
            return tok
    return None


def _ps_at_file(val: str) -> str | None:
    if val.startswith("@") and not val.startswith("@{") and val != "@-":
        return val[1:]
    return None


def _ps_colon_attachment(tok: str) -> str | None:
    opt, val = tok.split(":", 1)
    low, clean = opt.lower(), val.strip("\"'")
    if low in _PS_UPLOAD_OPTS and clean:
        return clean
    if low in _PS_BODY_OPTS:
        return _ps_at_file(clean)
    return None


def _collect_ps_attachments(name: str, args: list[str]) -> list[str]:
    files: list[str] = []
    if name in _PS_READ_CMDLETS:
        if target := _ps_external_cmdlet_target(args):
            files.append(target)
    i, n = 0, len(args)
    while i < n:
        tok, low = args[i], args[i].lower()
        if ":" in low and low.startswith("-"):
            if p := _ps_colon_attachment(tok):
                files.append(p)
            i += 1
        elif low in _PS_UPLOAD_OPTS and i + 1 < n:
            files.append(args[i + 1])
            i += 2
        elif low in _PS_BODY_OPTS and i + 1 < n:
            if p := _ps_at_file(args[i + 1]):
                files.append(p)
            i += 2
        else:
            if low.strip("({") in _PS_READ_CMDLETS and i + 1 < n:
                cand = args[i + 1].strip("()\"'")
                if cand and not cand.startswith("-"):
                    files.append(cand)
            elif p := _ps_at_file(tok):
                files.append(p)
            i += 1
    return files


def _classify_ps_segment(
    seg: str,
    *,
    cwd: str | None,
    trusted_cwd: str,
    guard_vault: str,
) -> tuple[bool, str | None, list[tuple[str, str | None]], str, str | None]:
    tokens, tok_err = _tokenize_ps_segment(seg)
    if tok_err:
        return (False, tok_err, [], "", cwd)
    if not tokens:
        return (False, None, [], "", cwd)
    argv, out_targets, in_files = _extract_redirects(tokens)
    raw_atts = list(in_files)
    outbound = any(
        cwd is None
        or not is_contained_write(
            d, trusted_cwd, os.getcwd(), guard_vault, rel_base=cwd
        )
        for d in out_targets
    )
    decoded = " ".join(t for t, _ in tokens)
    if not argv:
        return (outbound, None, [(p, cwd) for p in raw_atts], decoded, cwd)

    name, args = _bin_name(argv[0]), argv[1:]
    next_cwd = cwd
    if name in _PS_CD_CMDLETS:
        target = _ps_external_cmdlet_target(args) or ""
        next_cwd = _resolve_dir_change(cwd, target)
    if name in _PS_OUTBOUND_CMDLETS or name in _OUTBOUND_BINS:
        outbound = True
    elif name == "git" and _is_git_outbound(args):
        outbound = True
    elif name in _PS_EXTERNAL_CMDLETS:
        dest = _ps_external_cmdlet_target(args)
        if dest and (
            cwd is None
            or not is_contained_write(
                dest, trusted_cwd, os.getcwd(), guard_vault, rel_base=cwd
            )
        ):
            outbound = True
    raw_atts.extend(_collect_ps_attachments(name, args))
    if name in _OUTBOUND_BINS or outbound:
        raw_atts.extend(_collect_attachments(name, args))
    return (outbound, None, [(p, cwd) for p in raw_atts], decoded, next_cwd)


def analyze_powershell(
    command: str,
    *,
    cwd: str,
    guard_vault: str,
    deadline: float,
) -> ShellAnalysis:
    """Explicit PowerShell-aware fail-closed analyzer."""
    if _PS_FAIL_CLOSED_RE.search(command):
        return ShellAnalysis(
            False,
            "obfuscated or dynamic PowerShell construct (backtick, $(...), IEX, "
            "-EncodedCommand, or script block) — REFUSING, fail-closed.",
            "",
        )

    any_outbound = bool(_URL_RE.search(command) or _INLINE_NET_RE.search(command))
    attachments: list[tuple[str, str | None]] = []
    decoded_lines: list[str] = []
    cur_cwd: str | None = cwd
    for seg in _split_ps_segments(command):
        out, err, att, decoded, cur_cwd = _classify_ps_segment(
            seg,
            cwd=cur_cwd,
            trusted_cwd=cwd,
            guard_vault=guard_vault,
        )
        if err:
            return ShellAnalysis(False, err, "")
        any_outbound = any_outbound or out
        attachments.extend(att)
        if decoded:
            decoded_lines.append(decoded)

    if not any_outbound:
        return ShellAnalysis(False, None, "")
    extra_parts = ["\n".join(decoded_lines)] if decoded_lines else []
    return _read_all_attachments(attachments, deadline, extra_parts)
