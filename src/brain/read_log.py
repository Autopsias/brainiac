"""Host-private read-access log (SEC-06) — how much left the gate, and when.

The pentest asked what a sandbox session could READ. Nothing could answer what
it DID read: `verify-audit` covers writes, and `brain.querylog` covers only
`search` and `dossier` — a bulk sweep through `get`, `recent`, `grep` or
`bases-query` left no trace at all. Prompt injection cannot be prevented while
private data, untrusted content and an outbound channel share one session, so
the question after an incident is not "was it possible" but "what was taken".
This is the file that answers it.

**Why not the query log.** `querylog` is a RETRIEVAL-EVALUATION ledger: it
stores raw query text, top-k hits and rerank metadata so `brain eval replay`
can re-score real traffic, and `_validate_record_header` rejects any record
whose ``mode`` is not a search verb. Appending access records there would break
replay on the first line. Same directory conventions, same security properties,
separate file.

**What it deliberately does NOT store: query text, note ids, titles, paths.**
It records the SHAPE of an access — verb, role, tier cap, how many notes
crossed the gate, how many were withheld. That is enough to see a sweep and
never enough to become a second copy of the vault sitting outside the gate. A
log that has to be protected like the vault is a log nobody keeps.

Host-only, on by default, `BRAIN_READ_LOG=0` to disable. Best-effort
throughout: a failure to log never fails the read.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from . import config
from . import querylog as _q

VERSION = 1
LOG_DIRNAME = "read-log"

#: A read surfacing more notes than this is not a question, it is a sweep.
#: Deliberately well above a normal `-k 50` page and below a corpus scrape.
DEFAULT_BULK_THRESHOLD = 200
_FALSE_VALUES = {"0", "false", "no", "off"}


def enabled(role: str | None) -> bool:
    """Host-only and opt-out. A VM-written access log is not evidence anyway."""
    if _q._is_vm_role(role):
        return False
    return str(os.environ.get("BRAIN_READ_LOG", "1")).strip().lower() not in _FALSE_VALUES


def bulk_threshold() -> int:
    try:
        return max(1, int(os.environ.get("BRAIN_READ_LOG_BULK", DEFAULT_BULK_THRESHOLD)))
    except (TypeError, ValueError):
        return DEFAULT_BULK_THRESHOLD


def _log_dir(vault: str | os.PathLike[str] | None) -> tuple[Path, Path] | None:
    """Resolve the host-private sibling of the query-log directory.

    Reuses ``querylog._resolve_location`` so the refusals it already enforces —
    a path resolving inside the vault, an unverifiable permission mode — apply
    here unchanged rather than being re-implemented slightly differently.
    """
    try:
        vault_root, _index_root, query_dir, unsafe = _q._resolve_location(vault)
    except Exception:
        return None
    if unsafe:
        return None
    log_dir = query_dir.parent / LOG_DIRNAME
    # Same stricter treatment the query ledger gives itself: create AND
    # stat-verify owner-only, and refuse to write if that cannot be verified.
    if not _q._secure_dir(log_dir):
        return None
    resolved = log_dir.resolve()
    if _q._inside(resolved, vault_root):
        return None  # never inside the vault: it would become indexable content
    return vault_root, resolved


def record(
    *,
    vault: str | os.PathLike[str] | None,
    role: str,
    cmd: str,
    max_tier: str | None,
    surfaced: int,
    withheld: int,
    gates: int = 1,
    latency_ms: float | int | None = None,
    now: _dt.datetime | None = None,
) -> bool:
    """Append one access record. Returns True when a line was written."""
    if not enabled(role):
        return False
    location = _log_dir(vault)
    if location is None:
        return False
    _vault_root, log_dir = location
    stamp = now or _q._utc_now()
    entry: dict[str, Any] = {
        "version": VERSION,
        "ts": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cmd": cmd,
        "role": role,
        "max_tier": max_tier,
        "gates": int(gates),
        "surfaced": int(surfaced),
        "withheld": int(withheld),
        "bulk": int(surfaced) >= bulk_threshold(),
    }
    if latency_ms is not None:
        entry["latency_ms"] = round(float(latency_ms), 1)

    fd: int | None = None
    thread_locked = False
    locked = False
    try:
        thread_locked = _q._APPEND_THREAD_LOCK.acquire(timeout=_q.APPEND_LOCK_WAIT_S)
        if not thread_locked:
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(_q._month_file(log_dir, stamp)), flags, config.SECURE_FILE_MODE)
        if not _q._secure_fd(fd):
            return False
        if not _q._try_append_lock(fd):
            return False
        locked = True
        _q._write_all(
            fd,
            (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        os.fsync(fd)
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if fd is not None:
            try:
                if locked:
                    _q._release_append_lock(fd)
                os.close(fd)
            except OSError:
                pass
        if thread_locked:
            _q._APPEND_THREAD_LOCK.release()
    return True


def record_from_tally(
    *, vault: Any, role: str, cmd: str, max_tier: str | None,
    tally: dict[str, int] | None, latency_ms: float | int | None = None,
) -> bool:
    """Flush an ``egress.take_tally()`` result. No tally means nothing gated."""
    if not tally or not tally.get("gates"):
        return False
    return record(
        vault=vault, role=role, cmd=cmd, max_tier=max_tier,
        surfaced=tally.get("surfaced", 0), withheld=tally.get("withheld", 0),
        gates=tally.get("gates", 1), latency_ms=latency_ms,
    )


def status(vault: str | os.PathLike[str] | None, *, days: int = 7) -> dict[str, Any]:
    """Summarise recent access. Used by the maintenance folds, cheap file reads."""
    location = _log_dir(vault)
    if location is None:
        return {"available": False, "reason": "no readable read-log directory"}
    _vault_root, log_dir = location
    cutoff = _q._utc_now() - _dt.timedelta(days=days)
    reads = 0
    surfaced = 0
    bulk: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            try:
                when = _dt.datetime.strptime(row.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            if when.replace(tzinfo=_dt.timezone.utc) < cutoff:
                continue
            reads += 1
            surfaced += int(row.get("surfaced", 0) or 0)
            if row.get("bulk"):
                bulk.append(row)
    return {
        "available": True,
        "days": days,
        "reads": reads,
        "notes_surfaced": surfaced,
        "bulk_reads": len(bulk),
        "bulk": bulk[-10:],
        "threshold": bulk_threshold(),
        "dir": str(log_dir),
    }
