"""Bash/POSIX heredoc, forbidden-construct, and quote-aware segment lexer.

Split out of :mod:`brain._egress_guard_shell` to keep both modules well below
the repo file-size limit with zero extra third-party imports.
"""

from __future__ import annotations

from collections.abc import Sequence

_COMMENT_BOUNDARY_CHARS = frozenset(" \t;|&<>()\n")
_HEREDOC_END_CHARS = frozenset(" \t\r\n;|&<>()")
_BASH_REDIR_OPS = ("<<<", "&>>", "&>", ">>", ">|", ">&", "<&", ">", "<")
_DQ_ESCAPABLE = frozenset({'"', "\\", "$", "`"})
_IGNORED_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stderr", "/dev/stdout", "-"})


def _read_heredoc_delim_quote(
    line: str, idx: int, quote: str, out: list[str]
) -> int | None:
    n = len(line)
    j = idx + 1
    while j < n:
        ch = line[j]
        if quote == '"' and ch == "\\" and j + 1 < n and line[j + 1] in _DQ_ESCAPABLE:
            out.append(line[j + 1])
            j += 2
            continue
        if ch == quote:
            return j + 1
        out.append(ch)
        j += 1
    return None


def _match_heredoc_at(line: str, pos: int) -> tuple[int, int, str, bool, bool] | None:
    n = len(line)
    if pos + 2 < n and line[pos + 2] == "<":
        return None
    idx = pos + 2
    strip_tabs = False
    if idx < n and line[idx] == "-":
        strip_tabs = True
        idx += 1
    while idx < n and line[idx] in " \t":
        idx += 1
    start_word = idx
    quoted = False
    delim: list[str] = []
    while idx < n and line[idx] not in _HEREDOC_END_CHARS:
        ch = line[idx]
        if ch in ("'", '"'):
            quoted = True
            nxt = _read_heredoc_delim_quote(line, idx, ch, delim)
            if nxt is None:
                return None
            idx = nxt
        elif ch == "\\" and idx + 1 < n:
            quoted = True
            delim.append(line[idx + 1])
            idx += 2
        else:
            delim.append(ch)
            idx += 1
    if idx == start_word:
        return None
    return (pos, idx, "".join(delim), strip_tabs, quoted)


def _find_heredoc_ops(
    line: str, state: str
) -> tuple[list[tuple[int, int, str, bool, bool]], str]:
    """Find ALL unquoted, non-comment `<<` heredoc ops on ``line`` carrying quote ``state``."""
    hits: list[tuple[int, int, str, bool, bool]] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if state in ("normal", "double") and ch == "\\":
            i += 2
            continue
        if state != "normal":
            if (state == "single" and ch == "'") or (state == "double" and ch == '"'):
                state = "normal"
        elif ch in ("'", '"'):
            state = "single" if ch == "'" else "double"
        elif ch == "#" and (i == 0 or line[i - 1] in _COMMENT_BOUNDARY_CHARS):
            break
        elif line[i : i + 2] == "<<":
            if hit := _match_heredoc_at(line, i):
                hits.append(hit)
                i = hit[1]
                continue
            while i < n and line[i] == "<":
                i += 1
            continue
        i += 1
    return (hits, state)


def _strip_spans(line: str, hits: list[tuple[int, int, str, bool, bool]]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end, _delim, _strip_tabs, _quoted in hits:
        parts.append(line[cursor:start])
        cursor = end
    parts.append(line[cursor:])
    return "".join(parts)


def _consume_heredoc_body(
    lines: list[str],
    start: int,
    delim: str,
    strip_tabs: bool,
) -> tuple[str, int, bool]:
    body_lines: list[str] = []
    i = start
    while i < len(lines):
        candidate = lines[i].lstrip("\t") if strip_tabs else lines[i]
        if candidate == delim:
            return ("\n".join(body_lines), i + 1, True)
        body_lines.append(lines[i])
        i += 1
    return ("", i, False)


def _extract_heredoc_items(
    command: str,
) -> tuple[str, list[tuple[str, bool]], str | None]:
    """Extract heredocs as ``(cleaned_command, [(body, quoted_delim), ...], err)``."""
    if "<<" not in command:
        return (command, [], None)
    lines = command.splitlines()
    out_lines: list[str] = []
    items: list[tuple[str, bool]] = []
    state = "normal"
    i = 0
    while i < len(lines):
        hits, state = _find_heredoc_ops(lines[i], state)
        if not hits:
            out_lines.append(lines[i])
            i += 1
            continue
        out_lines.append(_strip_spans(lines[i], hits))
        i += 1
        for _start, _end, delim, strip_tabs, quoted in hits:
            body, i, closed = _consume_heredoc_body(lines, i, delim, strip_tabs)
            if not closed:
                return (
                    "",
                    [],
                    f"unterminated heredoc (expected {delim!r}) — REFUSING, fail-closed.",
                )
            items.append((body, quoted))
    return ("\n".join(out_lines), items, None)


def _extract_heredocs(command: str) -> tuple[str, list[str], str | None]:
    cleaned, items, err = _extract_heredoc_items(command)
    return (cleaned, [b for b, _ in items], err)


def _strip_heredoc_line_continuations(body: str) -> str:
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        if body[i] == "\\" and i + 1 < n:
            nxt = body[i + 1]
            if nxt == "\n":
                i += 2
                continue
            out.append("\\" + nxt)
            i += 2
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def _check_forbidden_heredoc(body: str) -> str | None:
    """Reject active `$(...)` and backticks inside an unquoted heredoc body."""
    norm = _strip_heredoc_line_continuations(body)
    i, n = 0, len(norm)
    while i < n:
        ch = norm[i]
        if ch == "\\" and i + 1 < n and norm[i + 1] in ("\\", "$", "`"):
            i += 2
            continue
        if ch == "$" and i + 1 < n and norm[i + 1] == "(":
            return "command substitution ($(...)) is not supported — REFUSING, fail-closed."
        if ch == "`":
            return "backtick command substitution is not supported — REFUSING, fail-closed."
        i += 1
    return None


def _forbidden_pair_msg(ch: str, nxt: str, *, in_double: bool) -> str | None:
    if ch == "$" and nxt == "(":
        return "command substitution ($(...)) is not supported — REFUSING, fail-closed."
    if ch == "`":
        return "backtick command substitution is not supported — REFUSING, fail-closed."
    if not in_double and ch == "$" and nxt == "'":
        return "ANSI-C quoting ($'...') is not supported — REFUSING, fail-closed."
    if not in_double and ch in ("<", ">") and nxt == "(":
        return "process substitution (<(...)/ >(...)) is not supported — REFUSING, fail-closed."
    return None


def _check_forbidden_bash(command: str) -> str | None:
    """Reject `$'...'`, `$(...)`, backticks, `<(...)`, `>(...)`, and open quotes."""
    state = "normal"
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if state in ("normal", "double") and ch == "\\":
            i += 2
            continue
        if state == "single":
            if ch == "'":
                state = "normal"
        elif state == "double":
            if ch == '"':
                state = "normal"
            elif msg := _forbidden_pair_msg(ch, nxt, in_double=True):
                return msg
        else:
            if ch == "#" and (i == 0 or command[i - 1] in _COMMENT_BOUNDARY_CHARS):
                nl = command.find("\n", i)
                i = n if nl < 0 else nl + 1
                continue
            if ch in ("'", '"'):
                state = "single" if ch == "'" else "double"
            elif msg := _forbidden_pair_msg(ch, nxt, in_double=False):
                return msg
        i += 1
    if state != "normal":
        return "unterminated quote in shell command — REFUSING, fail-closed."
    return None


def _consume_escape(command: str, i: int, buf: list[str]) -> int:
    if i + 1 < len(command):
        if command[i + 1] == "\n":
            return i + 2
        buf.extend((command[i], command[i + 1]))
        return i + 2
    buf.append(command[i])
    return i + 1


def _match_bash_sep(command: str, i: int) -> tuple[str | None, int]:
    """Return ``(kept_op, step)`` for redirects containing `|`/`&`, ``(None, step)`` for separators."""
    if command[i : i + 3] == "&>>":
        return ("&>>", 3)
    two = command[i : i + 2]
    if two in ("&>", ">|", ">&", "<&"):
        return (two, 2)
    if two in ("|&", "||", "&&"):
        return (None, 2)
    if command[i] in ("|", ";", "&", "\n"):
        return (None, 1)
    return ("", 0)


def _split_bash_segments_with_seps(command: str) -> list[tuple[str, str]]:
    """Split on unquoted `|&`, `||`, `&&`, `|`, `;`, `&`, and newlines; keep trailing sep."""
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    state = "normal"
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if state in ("normal", "double") and ch == "\\":
            i = _consume_escape(command, i, buf)
            continue
        if state != "normal":
            buf.append(ch)
            if (state == "single" and ch == "'") or (state == "double" and ch == '"'):
                state = "normal"
            i += 1
            continue
        if ch == "#" and (not buf or buf[-1] in _COMMENT_BOUNDARY_CHARS):
            nl = command.find("\n", i)
            i = n if nl < 0 else nl
            continue
        kept, step = _match_bash_sep(command, i)
        if step:
            if kept is None:
                if seg := "".join(buf).strip():
                    segments.append((seg, command[i : i + step]))
                buf = []
            else:
                buf.append(kept)
            i += step
            continue
        if ch in ("'", '"'):
            state = "single" if ch == "'" else "double"
        buf.append(ch)
        i += 1
    if tail := "".join(buf).strip():
        segments.append((tail, ""))
    return segments


def _split_bash_segments(command: str) -> list[str]:
    """Split on unquoted `|&`, `||`, `&&`, `|`, `;`, `&`, and newlines; strip comments."""
    return [seg for seg, _ in _split_bash_segments_with_seps(command)]


def _read_bash_quote(
    seg: str, i: int, quote: str, cur: list[str]
) -> tuple[int, str | None]:
    n = len(seg)
    if quote == "'":
        end = seg.find("'", i + 1)
        if end < 0:
            return (n, "unterminated quote in shell command — REFUSING, fail-closed.")
        cur.append(seg[i + 1 : end])
        return (end + 1, None)
    j = i + 1
    while j < n:
        ch = seg[j]
        if ch == "\\" and j + 1 < n:
            nxt = seg[j + 1]
            if nxt == "\n":
                j += 2
                continue
            cur.append(nxt if nxt in _DQ_ESCAPABLE else "\\" + nxt)
            j += 2
            continue
        if ch == '"':
            return (j + 1, None)
        cur.append(ch)
        j += 1
    return (n, "unterminated quote in shell command — REFUSING, fail-closed.")


def _read_bash_escape(seg: str, i: int, cur: list[str]) -> tuple[int, bool]:
    if i + 1 < len(seg):
        if seg[i + 1] != "\n":
            cur.append(seg[i + 1])
            return (i + 2, True)
        return (i + 2, False)
    return (i + 1, False)


def _match_redir_op(seg: str, i: int) -> str | None:
    for op in _BASH_REDIR_OPS:
        if seg.startswith(op, i):
            return op
    if seg[i] in ("(", ")"):
        return seg[i]
    return None


def _flush_bash_tok(
    tokens: list[tuple[str, bool]],
    cur: list[str],
    has_tok: bool,
    quoted_or_esc: bool,
) -> tuple[list[str], bool, bool]:
    if has_tok:
        word = "".join(cur)
        tokens.append((word, not quoted_or_esc and word in ("{", "}")))
    return ([], False, False)


def _tokenize_bash_segment(seg: str) -> tuple[list[tuple[str, bool]], str | None]:
    """Tokenize one Bash segment into ``(word, is_unquoted_operator)`` pairs."""
    tokens: list[tuple[str, bool]] = []
    cur: list[str] = []
    has_tok = False
    quoted_or_esc = False
    i, n = 0, len(seg)
    while i < n:
        ch = seg[i]
        if ch in " \t\r\n":
            cur, has_tok, quoted_or_esc = _flush_bash_tok(
                tokens, cur, has_tok, quoted_or_esc
            )
            i += 1
            continue
        if ch in ("'", '"'):
            has_tok = True
            quoted_or_esc = True
            i, err = _read_bash_quote(seg, i, ch, cur)
            if err:
                return ([], err)
            continue
        if ch == "\\":
            i, added = _read_bash_escape(seg, i, cur)
            if added:
                has_tok = True
                quoted_or_esc = True
            continue
        if op := _match_redir_op(seg, i):
            cur, has_tok, quoted_or_esc = _flush_bash_tok(
                tokens, cur, has_tok, quoted_or_esc
            )
            tokens.append((op, True))
            i += len(op)
            continue
        cur.append(ch)
        has_tok = True
        i += 1
    _flush_bash_tok(tokens, cur, has_tok, quoted_or_esc)
    return (tokens, None)


def _tokenize_bash_ops_with_seps(
    command: str,
) -> tuple[list[tuple[list[tuple[str, bool]], str]], str | None]:
    segments: list[tuple[list[tuple[str, bool]], str]] = []
    for seg_str, sep in _split_bash_segments_with_seps(command):
        toks, err = _tokenize_bash_segment(seg_str)
        if err:
            return ([], err)
        if toks:
            segments.append((toks, sep))
    return (segments, None)


def _tokenize_bash_ops(command: str) -> tuple[list[list[tuple[str, bool]]], str | None]:
    segs, err = _tokenize_bash_ops_with_seps(command)
    if err:
        return ([], err)
    return ([toks for toks, _ in segs], None)


def _tokenize_bash(command: str) -> tuple[list[list[str]], str | None]:
    ops_segs, err = _tokenize_bash_ops(command)
    if err:
        return ([], err)
    return ([[t for t, _ in seg] for seg in ops_segs], None)


def _tok_pair(item: str | tuple[str, bool]) -> tuple[str, bool]:
    return (item, True) if isinstance(item, str) else item


def _unwrap_group_tokens(
    tokens: list[tuple[str, bool]],
    open_braces: int,
) -> tuple[list[tuple[str, bool]], int]:
    """Strip leading/trailing Bash grouping operators (`(`, `)`, `{`, `}`) and `fn() {`."""
    if (
        len(tokens) >= 4
        and not tokens[0][1]
        and tokens[1:4] == [("(", True), (")", True), ("{", True)]
    ):
        tokens = tokens[4:]
        open_braces += 1
    start = 0
    while start < len(tokens) and tokens[start][1] and tokens[start][0] in ("(", "{"):
        if tokens[start][0] == "{":
            open_braces += 1
        start += 1
    end = len(tokens)
    while end > start and tokens[end - 1][1]:
        tok = tokens[end - 1][0]
        if tok == ")":
            end -= 1
        elif tok == "}" and open_braces > 0:
            open_braces -= 1
            end -= 1
        else:
            break
    return (tokens[start:end], open_braces)


def _extract_redirects_ops(
    tokens: Sequence[str | tuple[str, bool]],
) -> tuple[list[tuple[str, bool]], list[str], list[str]]:
    rem: list[tuple[str, bool]] = []
    out_targets: list[str] = []
    in_files: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        tok, is_op = _tok_pair(tokens[i])
        if is_op and tok in (">", ">>", ">|", "&>", "&>>", ">&") and i + 1 < n:
            if rem and rem[-1][0].isdigit():
                rem.pop()
            nxt, _ = _tok_pair(tokens[i + 1])
            if tok == ">&" and (nxt.isdigit() or nxt == "-"):
                i += 2
                continue
            if nxt == "&" and i + 2 < n:
                i += 3
                continue
            if nxt not in _IGNORED_REDIRECT_TARGETS and not nxt.startswith("&"):
                out_targets.append(nxt)
            i += 2
            continue
        if is_op and tok in ("<&", "<<<") and i + 1 < n:
            if rem and rem[-1][0].isdigit():
                rem.pop()
            i += 2
            continue
        if is_op and tok == "<" and i + 1 < n:
            nxt, _ = _tok_pair(tokens[i + 1])
            in_files.append(nxt)
            i += 2
            continue
        rem.append((tok, is_op))
        i += 1
    return (rem, out_targets, in_files)


def _extract_redirects(
    tokens: Sequence[str | tuple[str, bool]],
) -> tuple[list[str], list[str], list[str]]:
    rem, out_targets, in_files = _extract_redirects_ops(tokens)
    return ([t for t, _ in rem], out_targets, in_files)
