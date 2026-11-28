"""Path containment, registry lookup, session union, and bounded attachment I/O.

Pure-stdlib helper for :mod:`brain.egress_guard` so cold-start stays well under
the 200 ms budget and every module stays within the repo size/complexity gates.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from typing import Any

#: Strict byte cap for dereferenced `@file` / `< file` payload inspection.
#: Larger attachments fail closed rather than risking Claude Code's 5s timeout.
MAX_ATTACHMENT_BYTES = 256 * 1024

_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_POSIX_PATH_SPLIT_RE = re.compile(r"[^A-Za-z0-9_./~-]+")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'`;|&<>]+")


def is_abs_path(path_str: str) -> bool:
    """True for a POSIX (`/…`) or Windows (`C:\\…`, `C:/…`, `\\\\…`) absolute path."""
    if not path_str:
        return False
    return path_str.startswith("/") or bool(_WIN_ABS_RE.match(path_str))


def _is_win_path(path_str: str) -> bool:
    return os.name == "nt" or bool(_WIN_ABS_RE.match(path_str))


def _norm_cmp(path_str: str) -> str:
    """Normalize separators and case (on Windows paths) for prefix comparisons."""
    is_win = _is_win_path(path_str)
    norm = path_str.replace("\\", "/") if is_win else path_str
    if len(norm) > 1:
        norm = norm.rstrip("/")
    return norm.lower() if is_win else norm


def _is_fs_root(path_str: str) -> bool:
    norm = _norm_cmp(path_str)
    if norm in ("", "/"):
        return True
    if re.match(r"^[a-z]:/?$", norm):
        return True
    return False


def _has_symlink_component(path_str: str) -> bool:
    """True if ANY literal component (leaf, ancestor, or dangling) is a symlink."""
    probe = path_str
    while probe and not _is_fs_root(probe):
        if os.path.islink(probe):
            return True
        parent = os.path.dirname(probe)
        if not parent or parent == probe:
            break
        probe = parent
    return False


def canon_no_symlinks(path_str: str, rel_base: str = "") -> str | None:
    """Canonical path with symlinks REJECTED, even when the leaf does not exist.

    Returns ``None`` on any symlink (leaf, ancestor, or dangling) or unresolvable
    path so callers CHECK the write rather than exempting it.
    """
    if not path_str:
        return None
    p = path_str
    if p == "~" or p.startswith("~/") or (os.name == "nt" and p.startswith("~\\")):
        p = os.path.expanduser("~") + p[1:]
    elif not is_abs_path(p):
        base = rel_base or os.getcwd()
        p = os.path.join(base, p)

    if len(p) > 1:
        p = p.rstrip("/\\") if _is_win_path(p) else p.rstrip("/")
    if _has_symlink_component(p):
        return None

    tail: list[str] = []
    cur = p
    while cur and not os.path.isdir(cur):
        parent = os.path.dirname(cur)
        name = os.path.basename(cur)
        if not parent or parent == cur:
            return None
        tail.append(name)
        cur = parent

    if not cur:
        return None
    try:
        real = os.path.realpath(cur)
    except OSError:
        return None
    if tail:
        return os.path.normpath(os.path.join(real, *reversed(tail)))
    return os.path.normpath(real)


def _scratchpad_bases() -> tuple[str, ...]:
    """Claude Code session scratchpad roots on POSIX (macOS `/private/tmp` + `/tmp`)."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return ()
    uid = getuid()
    return (f"/private/tmp/claude-{uid}", f"/tmp/claude-{uid}")


def _is_owned_dir(path_str: str) -> bool:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return False
    try:
        st = os.lstat(path_str)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == getuid()

def is_contained_write(
    target: str,
    hook_cwd: str,
    proc_cwd: str,
    guard_vault: str,
    *,
    rel_base: str | None = None,
) -> bool:
    """True when ``target`` resolves inside working tree, vault, or scratchpad."""
    resolve_dir = rel_base if rel_base is not None else (hook_cwd or proc_cwd)
    ctarget = canon_no_symlinks(target, resolve_dir)
    if not ctarget:
        return False
    chome = canon_no_symlinks(os.path.expanduser("~"), proc_cwd)
    ctarget_cmp = _norm_cmp(ctarget)
    chome_cmp = _norm_cmp(chome) if chome else ""

    bases: list[tuple[str, bool]] = [
        (b, False) for b in (hook_cwd, proc_cwd, guard_vault)
    ]
    bases.extend((b, True) for b in _scratchpad_bases())
    for base, is_scratch in bases:
        if not base:
            continue
        cbase = canon_no_symlinks(base, proc_cwd)
        if not cbase or _is_fs_root(cbase):
            continue
        if is_scratch and not _is_owned_dir(cbase):
            continue
        cbase_cmp = _norm_cmp(cbase)
        if chome_cmp and cbase_cmp == chome_cmp:
            continue
        if ctarget_cmp.startswith(cbase_cmp + "/"):
            return True
    return False


def load_registry_entries(registry_path: str) -> list[dict[str, Any]]:
    """Read ``workspaces.json`` entries; return ``[]`` if absent or unreadable."""
    if not registry_path or not os.path.isfile(registry_path):
        return []
    try:
        with open(registry_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return (
        [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    )


def _entry_bases(entries: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Return ``(raw_base, cmp_base, vault_path)`` tuples for every entry."""
    out: list[tuple[str, str, str]] = []
    for entry in entries:
        vault_path = str(entry.get("vault_path") or "")
        if not vault_path:
            continue
        ws_path = str(entry.get("workspace_path") or "")
        stripped = (
            vault_path.rstrip("/\\")
            if _is_win_path(vault_path)
            else vault_path.rstrip("/")
        )
        vault_parent = os.path.dirname(stripped)
        for base in (ws_path, vault_parent):
            if base:
                out.append((base, _norm_cmp(base), vault_path))
    return out


def path_pair(candidate: str) -> tuple[str, str]:
    """Return ``(raw, realpath)`` for a candidate path (dir or parent dir)."""
    if not candidate:
        return ("", "")
    real = ""
    if os.path.isdir(candidate):
        try:
            real = os.path.realpath(candidate)
        except OSError:
            real = ""
    else:
        parent = os.path.dirname(candidate)
        if parent and parent != candidate and os.path.isdir(parent):
            try:
                real = os.path.realpath(parent)
            except OSError:
                real = ""
    return (candidate, real)


def _match_one(
    pair: tuple[str, str],
    bases: list[tuple[str, str, str]],
) -> str:
    """Longest matching base wins; tie-broken lexicographically by vault_path."""
    candidates = [_norm_cmp(p) for p in pair if p]
    if not candidates:
        return ""
    matches: list[tuple[int, str]] = []
    for raw_base, cmp_base, vault_path in bases:
        if any(c == cmp_base or c.startswith(cmp_base + "/") for c in candidates):
            matches.append((len(raw_base), vault_path))
    if not matches:
        return ""
    matches.sort()
    chosen = matches[-1][1]
    return chosen if is_abs_path(chosen) and os.path.isdir(chosen) else ""


def resolve_vault_for(path_str: str, entries: list[dict[str, Any]]) -> str:
    """Resolve the single best vault for ``path_str`` from registry entries."""
    if not path_str or not entries:
        return ""
    return _match_one(path_pair(path_str), _entry_bases(entries))


def extract_path_candidates(
    named_paths: list[str],
    tool: str,
    text: str,
) -> list[tuple[str, str]]:
    """Collect absolute path candidates (capped at 40 shell tokens) as ``(raw, real)``."""
    cands: list[str] = [p for p in named_paths if p]
    if tool in ("Bash", "PowerShell") and text:
        tokens: set[str] = set()
        for frag in _POSIX_PATH_SPLIT_RE.split(text):
            if frag.startswith("/"):
                tokens.add(frag)
        for m in _WIN_PATH_RE.finditer(text):
            tokens.add(m.group(0))
        cands.extend(sorted(tokens)[:40])
    return [path_pair(c) for c in cands]


def resolve_vaults_for_many(
    pairs: list[tuple[str, str]],
    raw_text: str,
    entries: list[dict[str, Any]],
) -> list[str]:
    """Resolve every vault touched by ``pairs`` or spelled literally in ``raw_text``."""
    if not entries or (not pairs and not raw_text):
        return []
    bases = _entry_bases(entries)
    found: set[str] = set()
    for pair in pairs:
        v = _match_one(pair, bases)
        if v:
            found.add(v)
    if raw_text:
        for raw_base, _, vault_path in bases:
            if (
                raw_base in raw_text
                and is_abs_path(vault_path)
                and os.path.isdir(vault_path)
            ):
                found.add(vault_path)
    return sorted(found)


def update_session_union(
    registry_path: str,
    session_id: str,
    current_vault: str,
    named_vaults: list[str],
) -> list[str]:
    """Accumulate vaults entered by ``session_id``; return deduplicated list."""
    record = sorted({v for v in [current_vault, *named_vaults] if v})
    if not registry_path or not session_id or not _SESSION_ID_RE.match(session_id):
        return record
    sdir = os.path.join(os.path.dirname(registry_path), "egress-sessions")
    sfile = os.path.join(sdir, session_id)
    if record:
        try:
            os.makedirs(sdir, mode=0o700, exist_ok=True)
            with open(sfile, "a", encoding="utf-8") as fh:
                for v in record:
                    fh.write(v + "\n")
        except OSError:
            pass
    try:
        with open(sfile, encoding="utf-8", errors="replace") as fh:
            seen = sorted({v for raw in fh if (v := raw.rstrip("\r\n"))})
        if seen:
            return seen
    except OSError:
        pass
    return record


def check_control_files(
    hooks_dir: str,
    registry_path: str,
    text: str,
    target: str,
    pairs: list[tuple[str, str]],
) -> str | None:
    """Return the protected path if this call names the guard's own control files."""
    protected: list[str] = []
    if hooks_dir and is_abs_path(hooks_dir):
        protected.append(hooks_dir)
        try:
            real_hooks = os.path.realpath(hooks_dir)
            if real_hooks and real_hooks != hooks_dir:
                protected.append(real_hooks)
        except OSError:
            pass
    if registry_path:
        protected.append(
            os.path.join(os.path.dirname(registry_path), "egress-sessions")
        )
        protected.append(registry_path)

    pair_text = " ".join(f"{r} {p}" for r, p in pairs)
    haystack = f"{text} {target} {pair_text}"
    for own in protected:
        if own and own in haystack:
            return own
    return None


def _check_regular_stat(
    st: os.stat_result, path_str: str, max_bytes: int
) -> str | None:
    if stat.S_ISLNK(st.st_mode):
        return f"attachment {path_str!r} is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return f"attachment {path_str!r} is not a regular file"
    if st.st_size > max_bytes:
        return (
            f"attachment {path_str!r} exceeds the {max_bytes}-byte inspection cap "
            f"({st.st_size} bytes)"
        )
    return None


def _read_regular_file_bytes(
    resolved_path: str,
    path_str: str,
    max_bytes: int,
) -> tuple[bytes | None, str | None]:
    """Read up to ``max_bytes + 1`` bytes from a verified regular file (no symlinks/FIFOs)."""
    try:
        lst = os.lstat(resolved_path)
    except FileNotFoundError:
        return (None, None)
    except OSError as exc:
        return (
            None,
            f"attachment {path_str!r} could not be stated ({type(exc).__name__})",
        )
    if err := _check_regular_stat(lst, path_str, max_bytes):
        return (None, err)

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(resolved_path, flags)
    except FileNotFoundError:
        return (None, None)
    except OSError as exc:
        return (
            None,
            f"attachment {path_str!r} could not be opened ({type(exc).__name__})",
        )

    try:
        if err := _check_regular_stat(os.fstat(fd), path_str, max_bytes):
            return (None, err)
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return (fh.read(max_bytes + 1), None)
    except OSError as exc:
        return (
            None,
            f"attachment {path_str!r} could not be read ({type(exc).__name__})",
        )
    finally:
        if fd >= 0:
            os.close(fd)


def resolve_attachment_path(path_str: str, cwd: str) -> str:
    """Resolve an attachment path (``~`` or relative) to a normalized path."""
    p = path_str
    if p == "~" or p.startswith("~/") or (os.name == "nt" and p.startswith("~\\")):
        p = os.path.expanduser("~") + p[1:]
    elif not is_abs_path(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.normpath(p)


def _decode_attachment_bytes(raw: bytes) -> str:
    """Decode attachment bytes with UTF-32/UTF-16 (BOM & BOM-less) and UTF-8 support."""
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw.decode("utf-32", errors="replace")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    pairs = len(raw) // 2
    if pairs >= 1:
        even_nuls = raw[0 : pairs * 2 : 2].count(0)
        odd_nuls = raw[1 : pairs * 2 : 2].count(0)
        min_nuls = max(1, pairs // 4)
        if odd_nuls >= min_nuls and odd_nuls > even_nuls * 2:
            return raw.decode("utf-16-le", errors="replace")
        if even_nuls >= min_nuls and even_nuls > odd_nuls * 2:
            return raw.decode("utf-16-be", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_attachment_bounded(
    path_str: str,
    cwd: str,
    deadline: float,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> tuple[str | None, str | None]:
    """Read bounded text from an attached file; return ``(content, deny_reason)``.

    Nonexistent regular paths return ``(None, None)`` (cannot exfiltrate bytes).
    Symlinks, oversized files, non-regular files (FIFOs/devices), unreadable
    files, or deadline expiry return ``(None, reason)`` so the guard FAILS
    CLOSED without outlasting ``deadline``.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return (
            None,
            f"attachment inspection deadline exceeded before reading {path_str!r}",
        )
    p = resolve_attachment_path(path_str, cwd)

    import threading

    box: list[tuple[bytes | None, str | None]] = []

    def _worker() -> None:
        box.append(_read_regular_file_bytes(p, path_str, max_bytes))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=max(0.0, deadline - time.monotonic()))
    if t.is_alive() or not box:
        return (
            None,
            f"attachment inspection deadline exceeded while reading {path_str!r}",
        )

    raw, err = box[0]
    if err is not None:
        return (None, err)
    if raw is None:
        return (None, None)
    if len(raw) > max_bytes:
        return (
            None,
            f"attachment {path_str!r} grew past the {max_bytes}-byte inspection cap",
        )
    if time.monotonic() >= deadline:
        return (
            None,
            f"attachment inspection deadline exceeded while reading {path_str!r}",
        )
    return (_decode_attachment_bytes(raw), None)
