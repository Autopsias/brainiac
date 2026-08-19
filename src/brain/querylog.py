"""Host-contained real-query capture, replay, and retention (ADR-0008 §4).

The ledger deliberately lives beside the *live* host index, never beneath a
vault or its ``.brain`` runtime tree.  It contains raw queries, so role checks
alone are not a sufficient control: every write resolves symlinks and refuses
an unsafe location before a byte of query text is written.

This module has three small public seams:

``capture_post_egress``
    Best-effort append of an already-gated response.  It is safe to call on
    every post-egress frontend path: failures are recorded locally and never
    fail the search itself.
``replay``
    Host-side report over an existing JSONL month file.  It never calls the
    capture seam, so evaluation cannot contaminate production traffic.
``status`` / ``prune_expired_months``
    Host health and maintenance folds.  VM callers return before resolving or
    touching any host ledger path.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import classification as cls
from . import config, egress
from . import querylog_status as _querylog_status


VERSION = 1
QUERY_LOG_DIRNAME = "query-log"
STATUS_DIRNAME = "query-capture-status"
STATUS_FILENAME = "status.json"
DEFAULT_RETENTION_MONTHS = 3
DEFAULT_STALE_DAYS = 7
_FALSE_VALUES = {"0", "false", "no", "off"}
_MONTH_NAME_LEN = len("2000-01.jsonl")
# A permission/configuration failure can itself make the durable status counter
# unavailable. Keep a process-local fallback so the *current* status/doctor
# call still surfaces the failure instead of reporting a false green. Durable
# app-data state remains the cross-process source of truth when available.
_VOLATILE_STATUS: dict[str, dict[str, Any]] = {}
_APPEND_THREAD_LOCK = threading.Lock()


class ReplayDataError(ValueError):
    """A capture file is not a valid S04 JSONL ledger."""


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso_z(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _is_owner_only_dir(path: Path) -> bool:
    if os.name != "posix":
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid()


def _is_owner_only_file(path_or_fd: Path | int) -> bool:
    if os.name != "posix":
        return False
    try:
        # `lstat` makes a symlink an explicit failure rather than following it
        # to a seemingly-secure target. Capture opens with O_NOFOLLOW; health
        # must enforce the same no-indirection posture when it audits a ledger.
        info = os.fstat(path_or_fd) if isinstance(path_or_fd, int) else path_or_fd.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == config.SECURE_FILE_MODE
            and info.st_uid == os.getuid())


def _secure_dir(path: Path) -> bool:
    """Create then stat-verify a POSIX owner-only directory.

    ``config.secure_file_permissions`` is deliberately best-effort for ordinary
    derived files.  This ledger is stricter: an unverifiable permission result
    means no raw query may be persisted.
    """
    if os.name != "posix":
        return False
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)  # nosemgrep: insecure-file-permissions -- intentionally owner-only
    except OSError:
        return False
    return _is_owner_only_dir(path)


def _secure_fd(fd: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.fchmod(fd, config.SECURE_FILE_MODE)
    except OSError:
        return False
    return _is_owner_only_file(fd)


def _enabled_by_env() -> bool:
    raw = os.environ.get("BRAIN_QUERY_CAPTURE_ENABLED", "1").strip().lower()
    return raw not in _FALSE_VALUES


def _is_vm_role(role: str | None) -> bool:
    return str(role or "").strip().lower() == config.ROLE_VM


def capture_requested(role: str | None) -> bool:
    """Cheap gate used before deciding whether a frontend needs an S03 trace."""
    return not _is_vm_role(role) and _enabled_by_env()


def _resolve_location(vault: str | os.PathLike[str] | None) -> tuple[Path, Path, Path, str | None]:
    """Return resolved ``(vault, index_dir, log_dir, unsafe_reason)``.

    ``Path.resolve`` intentionally follows existing symlinks and normalises
    non-existing tails.  Re-check after directory creation in the write path to
    close the ordinary symlink-override configuration mistake as well.
    """
    vault_root = config.vault_root(vault).resolve()
    index_root = config.index_dir(vault_root).expanduser().resolve()
    log_dir = (index_root / QUERY_LOG_DIRNAME).resolve()
    if _inside(log_dir, vault_root):
        return vault_root, index_root, log_dir, "log_inside_vault"
    return vault_root, index_root, log_dir, None


def _state_dir(vault_root: Path) -> Path:
    """A query-free counter survives an unsafe index-dir override.

    The ledger itself must be exactly ``index_dir/query-log``.  Its status
    counter cannot live there when that configured location resolves into the
    vault, otherwise containment failure would silently lose observability.
    App-data is intentionally independent of ``BRAIN_INDEX_DIR`` for this tiny
    non-content state record.
    """
    # This is intentionally based on the resolved vault path instead of
    # config.vault_id(create=True): a capture health check must never write a
    # vault marker merely to name its host-only state.
    digest = hashlib.sha256(str(vault_root).encode("utf-8")).hexdigest()[:16]
    return (config._app_data_base() / STATUS_DIRNAME / digest).resolve()  # type: ignore[attr-defined]


def _state_path(vault_root: Path) -> Path:
    return _state_dir(vault_root) / STATUS_FILENAME


def _state_key(vault_root: Path) -> str:
    return str(vault_root)


def _load_state(vault_root: Path) -> dict[str, Any]:
    path = _state_path(vault_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _current_state(vault_root: Path) -> dict[str, Any]:
    """Durable state, overlaid by an in-process fallback if persistence failed."""
    state_data = _load_state(vault_root)
    volatile = _VOLATILE_STATUS.get(_state_key(vault_root))
    if volatile and int(volatile.get("failures", 0) or 0) >= int(state_data.get("failures", 0) or 0):
        return {**state_data, **volatile}
    return state_data


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - normal files never return zero
            raise OSError("short write")
        view = view[written:]


def _try_append_lock(fd: int) -> bool:
    """Acquire a non-blocking writer lock for one JSONL record.

    O_APPEND protects the file offset, but it cannot turn a partial write of
    an unusually long raw query into one indivisible JSONL record.  A
    non-blocking advisory lock keeps concurrent current-version appenders from
    interleaving records without ever making a search wait for observability.
    A busy/unavailable lock is an ordinary best-effort capture failure.
    """
    if os.name != "posix":
        return False
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ImportError, OSError):
        return False


def _release_append_lock(fd: int) -> None:
    if os.name != "posix":
        return
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def _write_state(vault_root: Path, state_data: dict[str, Any]) -> bool:
    """Atomically persist only non-query health counters, owner-only."""
    state_dir = _state_dir(vault_root)
    if _inside(state_dir, vault_root) or not _secure_dir(state_dir):
        return False
    path = state_dir / STATUS_FILENAME
    # Status writes can be triggered by concurrent MCP requests in one host
    # process. A pid-only temp name lets those writers truncate/replace each
    # other's staging file; make the short-lived, query-free name unique.
    tmp = state_dir / (
        f".{STATUS_FILENAME}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(tmp), flags, config.SECURE_FILE_MODE)
        if not _secure_fd(fd):
            return False
        payload = (json.dumps(state_data, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        try:
            os.chmod(path, config.SECURE_FILE_MODE)
        except OSError:
            return False
        return _is_owner_only_file(path)
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _note_failure(vault_root: Path, code: str, *, configuration: bool = False) -> None:
    state_data = _current_state(vault_root)
    state_data["version"] = VERSION
    state_data["failures"] = int(state_data.get("failures", 0) or 0) + 1
    state_data["consecutive_failures"] = int(state_data.get("consecutive_failures", 0) or 0) + 1
    if configuration:
        state_data["configuration_errors"] = int(state_data.get("configuration_errors", 0) or 0) + 1
    state_data["last_failure_at"] = _iso_z(_utc_now())
    # Codes, never exception strings or raw query text.
    state_data["last_failure_code"] = code
    if _write_state(vault_root, state_data):
        _VOLATILE_STATUS.pop(_state_key(vault_root), None)
    else:
        _VOLATILE_STATUS[_state_key(vault_root)] = state_data


def _note_success(vault_root: Path) -> None:
    state_data = _current_state(vault_root)
    state_data["version"] = VERSION
    state_data["last_capture_at"] = _iso_z(_utc_now())
    state_data["consecutive_failures"] = 0
    if _write_state(vault_root, state_data):
        _VOLATILE_STATUS.pop(_state_key(vault_root), None)
    else:
        _VOLATILE_STATUS[_state_key(vault_root)] = state_data


def _month_file(log_dir: Path, now: _dt.datetime | None = None) -> Path:
    stamp = now or _utc_now()
    return log_dir / f"{stamp.astimezone(_dt.timezone.utc):%Y-%m}.jsonl"


def _month_files(log_dir: Path) -> list[Path]:
    if not log_dir.is_dir():
        return []
    return [p for p in sorted(log_dir.glob("????-??.jsonl")) if len(p.name) == _MONTH_NAME_LEN]


def _parse_month(name: str) -> _dt.date | None:
    try:
        return _dt.datetime.strptime(name[:7], "%Y-%m").date().replace(day=1)
    except ValueError:
        return None


def _retention_months() -> int:
    try:
        return max(1, int(os.environ.get("BRAIN_QUERY_LOG_RETENTION_MONTHS", DEFAULT_RETENTION_MONTHS)))
    except ValueError:
        return DEFAULT_RETENTION_MONTHS


def _stale_days() -> int:
    try:
        return max(0, int(os.environ.get("BRAIN_QUERY_CAPTURE_STALE_DAYS", DEFAULT_STALE_DAYS)))
    except ValueError:
        return DEFAULT_STALE_DAYS


def _month_cutoff(today: _dt.date, retention_months: int) -> _dt.date:
    ordinal = today.year * 12 + today.month - 1 - (retention_months - 1)
    return _dt.date(ordinal // 12, ordinal % 12 + 1, 1)


def prune_expired_months(
    vault: str | os.PathLike[str] | None,
    *,
    role: str = config.ROLE_HOST,
    today: _dt.date | None = None,
) -> dict[str, Any]:
    """Unlink whole expired month files only; never rewrite a live JSONL file."""
    if _is_vm_role(role):
        return {"pruned": [], "skipped": "role_vm", "retention_months": _retention_months()}
    try:
        vault_root, _index_root, log_dir, unsafe = _resolve_location(vault)
    except Exception:
        return {"pruned": [], "skipped": "vault_unresolved", "retention_months": _retention_months()}
    months = _retention_months()
    if unsafe:
        _note_failure(vault_root, unsafe, configuration=True)
        return {"pruned": [], "skipped": unsafe, "retention_months": months}
    if not log_dir.is_dir():
        return {"pruned": [], "retention_months": months}
    if not _is_owner_only_dir(log_dir):
        _note_failure(vault_root, "log_permissions_unverified", configuration=True)
        return {"pruned": [], "skipped": "log_permissions_unverified", "retention_months": months}
    cutoff = _month_cutoff(today or _dt.date.today(), months)
    pruned: list[str] = []
    errors: list[str] = []
    for candidate in _month_files(log_dir):
        month = _parse_month(candidate.name)
        if month is None or month >= cutoff:
            continue
        try:
            # Whole-file unlink only.  A current or retained month is never
            # compacted/truncated while an appender may hold it open.
            candidate.unlink()
            pruned.append(candidate.name)
        except OSError:
            errors.append(candidate.name)
    state_data = _current_state(vault_root)
    state_data["version"] = VERSION
    state_data["last_prune_at"] = _iso_z(_utc_now())
    state_data["last_pruned_files"] = len(pruned)
    _write_state(vault_root, state_data)
    return {"pruned": pruned, "errors": errors, "retention_months": months,
            "cutoff_month": cutoff.isoformat()}


_querylog_status.configure(globals())
_validate_record = _querylog_status._validate_record
capture_post_egress = _querylog_status.capture_post_egress
status = _querylog_status.status

# The digest helpers live in querylog_digest.py and the private replay engine
# in querylog_replay.py since the 2026-08-16 size ratchet; re-exported so
# every `brain.querylog.<name>` caller (and the querylog_status source-proxy
# seam, which resolves these names from this module's globals) is unchanged.
from .querylog_digest import (  # noqa: E402,F401  (facade re-export)
    _normalise_fingerprint as _normalise_fingerprint,
    _rerank_mode as _rerank_mode,
    _safe_digest as _safe_digest,
    _safe_float as _safe_float,
    _safe_top as _safe_top,
    empty_digest as empty_digest,
    live_index_fingerprint as live_index_fingerprint,
    projection_from_gated as projection_from_gated,
)
from .querylog_replay import (  # noqa: E402,F401  (facade re-export)
    _gated_dossier as _gated_dossier,
    _gated_hybrid as _gated_hybrid,
    _jaccard as _jaccard,
    _mean as _mean,
    _rank_movement as _rank_movement,
    _read_records as _read_records,
    _segment as _segment,
    replay as replay,
)

