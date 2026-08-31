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

Host-only, on by default, `BRAIN_READ_LOG=0` to disable. **Not best-effort any
more:** since VULN-3385/A-05 a gated read whose record cannot be written is
WITHHELD (`cli_read_record`). That makes every reason this module can return
`False` a reason a read fails, which is why the directory gate below is its own
platform-aware function and not `querylog`'s.
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


def _posix_permissions() -> bool:
    """Can this OS express and stat-verify a POSIX owner-only mode at all?"""
    return os.name == "posix"


def _secure_log_dir(path: Path) -> bool:
    """Create the read-log directory, owner-only where the OS can say so.

    On POSIX this is ``querylog._secure_dir`` unchanged: create, chmod 0700,
    stat-verify, refuse if that cannot be confirmed.

    On Windows it is NOT. ``_secure_dir`` returns a flat ``False`` off
    ``os.name``, which is right for the QUERY ledger it belongs to — that one
    stores raw query text, so an unverifiable mode means the text must not be
    persisted at all. This log stores no query text, no ids, no titles and no
    paths, only the SHAPE of an access, and it sits under the per-user app-data
    base the OS already ACLs to that user.

    Borrowing the query ledger's refusal cost the whole product on Windows.
    Once VULN-3385 made the record fail-CLOSED, ``False`` here stopped meaning
    "no log line" and started meaning "withhold the read": measured on
    distribution-matrix run 33386239277 (v0.20.33, 2026-08-31), `search`, `get`,
    `recent` and `grep` each exited 5 on Windows with every result withheld,
    while macOS stayed green. 0.20.32 and every release before it were green
    because the CLI leg still swallowed the failure.

    Stated limit, because it is weaker and must not read as equal: a Windows
    read-log directory is protected by the inherited ACL of the app-data base,
    NOT by a mode this code set and re-read. ``_posix_permissions`` is the one
    switch that says which of the two you got.
    """
    if _posix_permissions():
        return _q._secure_dir(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return path.is_dir()


def _log_dir(vault: str | os.PathLike[str] | None) -> tuple[Path, Path] | None:
    """Resolve the host-private sibling of the query-log directory.

    Reuses ``querylog._resolve_location`` so the refusals it already enforces —
    a path resolving inside the vault, an unverifiable location — apply here
    unchanged rather than being re-implemented slightly differently. The
    directory PERMISSION gate is :func:`_secure_log_dir`, which is not shared.
    """
    try:
        vault_root, _index_root, query_dir, unsafe = _q._resolve_location(vault)
    except Exception:
        return None
    if unsafe:
        return None
    log_dir = query_dir.parent / LOG_DIRNAME
    if not _secure_log_dir(log_dir):
        return None
    resolved = log_dir.resolve()
    if _q._inside(resolved, vault_root):
        return None  # never inside the vault: it would become indexable content
    return vault_root, resolved


def _secure_and_lock(fd: int) -> tuple[bool, bool]:
    """Verify and lock the record file, returning ``(usable, locked)``.

    Both gates this wraps answer off ``os.name`` before they do anything, for
    the same reason and with the same consequence as :func:`_secure_log_dir`:
    ``_secure_fd`` re-reads a POSIX mode Windows does not have, and
    ``_try_append_lock`` needs ``fcntl``. Calling them off POSIX returns False,
    which since VULN-3385 withholds the read.

    So off POSIX the handle is usable and UNLOCKED: the record is serialised by
    ``_APPEND_THREAD_LOCK`` (the caller holds it) plus ``O_APPEND``, which is
    one process rather than all of them. Weaker, and bounded by the payload
    being one short line written in a single ``os.write``.
    """
    if not _posix_permissions():
        return True, False
    if not _q._secure_fd(fd):
        return False, False
    if not _q._try_append_lock(fd):
        return False, False
    return True, True


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
        ok, locked = _secure_and_lock(fd)
        if not ok:
            return False
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


#: The four outcomes of a flush, because ``bool`` cannot tell them apart and
#: the difference decides whether a read may return its data (closed-stacks
#: S02, acceptance criterion 3).
#:
#: ``record_from_tally`` returns ``False`` for THREE unrelated situations, and
#: a caller that treats them alike either fails every status verb or keeps the
#: hole open:
#:   * nothing was gated at all (``not tally`` / ``gates == 0``) — a verb that
#:     surfaced no note bodies has nothing to log, and logging a zero would
#:     bury the real reads (see :func:`egress.take_tally`). NOT a failure.
#:     This is also what a caller sees when a CONCURRENT task consumed the
#:     shared tally, which is why S02 moved the tally to a ContextVar first.
#:   * the log is switched off for this caller — ``BRAIN_READ_LOG=0``, or a vm
#:     role, for which a self-written access log is not evidence anyway. An
#:     operator decision, NOT a failure.
#:   * the record could not be written — no securable log dir, an OSError, a
#:     lock timeout. THAT is the failure a read must not survive.
RECORD_WRITTEN = "written"
RECORD_NOTHING_TO_RECORD = "nothing_to_record"
RECORD_DISABLED = "disabled"
RECORD_FAILED = "failed"


def outcome_from_tally(
    *, vault: Any, role: str, cmd: str, max_tier: str | None,
    tally: dict[str, int] | None, latency_ms: float | int | None = None,
) -> str:
    """Flush an ``egress.take_tally()`` result, saying WHICH of the four happened."""
    if not tally or not tally.get("gates"):
        return RECORD_NOTHING_TO_RECORD
    if not enabled(role):
        return RECORD_DISABLED
    written = record(
        vault=vault, role=role, cmd=cmd, max_tier=max_tier,
        surfaced=tally.get("surfaced", 0), withheld=tally.get("withheld", 0),
        gates=tally.get("gates", 1), latency_ms=latency_ms,
    )
    return RECORD_WRITTEN if written else RECORD_FAILED


def record_from_tally(
    *, vault: Any, role: str, cmd: str, max_tier: str | None,
    tally: dict[str, int] | None, latency_ms: float | int | None = None,
) -> bool:
    """Flush an ``egress.take_tally()`` result. No tally means nothing gated."""
    return outcome_from_tally(
        vault=vault, role=role, cmd=cmd, max_tier=max_tier, tally=tally,
        latency_ms=latency_ms,
    ) == RECORD_WRITTEN


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
