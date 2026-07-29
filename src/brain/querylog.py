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


def _normalise_fingerprint(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    return raw[7:] if raw.startswith("sha256:") else raw


def live_index_fingerprint(index: Any) -> str | None:
    """Read the live SQLite content fingerprint, never a VM snapshot marker."""
    try:
        raw = index.get_meta("vault_fingerprint")
    except Exception:
        return None
    norm = _normalise_fingerprint(raw)
    return f"sha256:{norm}" if norm else None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _safe_top(top: Iterable[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(top, start=1):
        if len(out) >= max(0, limit):
            break
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        score = _safe_float(item.get("pre_rerank_score"))
        final_rank = item.get("final_rank")
        if not isinstance(final_rank, int) or final_rank < 1:
            final_rank = rank
        out.append({"id": ident, "pre_rerank_score": score, "final_rank": final_rank})
    return out


def _safe_digest(digest: Any) -> dict[str, Any]:
    """Keep the bounded S03 shape without trusting a frontend object blindly."""
    if not isinstance(digest, dict):
        return empty_digest([])
    per_leg_limit = digest.get("per_leg_limit", 20)
    try:
        per_leg_limit = max(1, min(20, int(per_leg_limit)))
    except (TypeError, ValueError):
        per_leg_limit = 20

    def project(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for rank, item in enumerate(items[:per_leg_limit], start=1):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            item_rank = item.get("rank")
            out.append({"id": item["id"], "rank": item_rank if isinstance(item_rank, int) else rank})
        return out

    legs = digest.get("legs") if isinstance(digest.get("legs"), dict) else {}
    return {
        "version": 1,
        "per_leg_limit": per_leg_limit,
        "truncated": bool(digest.get("truncated", False)),
        "legs": {
            "lexical": project(legs.get("lexical")),
            "dense": project(legs.get("dense")),
            "exact": project(legs.get("exact")),
        },
        "pre_rerank": project(digest.get("pre_rerank")),
        "final": project(digest.get("final")),
    }


def _rerank_mode(rerank: dict[str, Any]) -> str:
    """Return the small, stable mode label used for traffic segmentation."""
    if bool(rerank.get("applied", False)):
        return "applied"
    if bool(rerank.get("requested", False)):
        return "requested_not_applied"
    return "disabled"


def empty_digest(ids: Iterable[str], *, per_leg_limit: int = 20) -> dict[str, Any]:
    """S03-compatible bounded digest for a dossier's composed response."""
    visible = [ident for ident in ids if isinstance(ident, str) and ident]
    limit = max(1, min(20, int(per_leg_limit)))
    final = [{"id": ident, "rank": rank} for rank, ident in enumerate(visible[:limit], start=1)]
    return {
        "version": 1,
        "per_leg_limit": limit,
        "truncated": len(visible) > limit,
        "legs": {"lexical": [], "dense": [], "exact": []},
        "pre_rerank": list(final),
        "final": final,
    }


def projection_from_gated(
    surfaced: list[dict[str, Any]],
    *,
    trace: Any | None = None,
    redacted_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the sole capture projection from already-egress-approved rows.

    Frontends hand this function only their gated result list.  When an S03
    trace is available, its score is read through the same identity-redaction
    switch used for user-facing ``--explain``; otherwise the already-surfaced
    score is the only permitted fallback.  This makes the CLI and MCP adapter
    share one post-egress serialization seam.
    """
    redacted = redacted_ids or set()
    # A trace needs set membership; a composed response (dossier) needs the
    # surfaced order. Keep both representations so its final-list digest is
    # deterministic and agrees with the returned ranking instead of inheriting
    # Python set iteration order.
    visible_ids = [item["id"] for item in surfaced if isinstance(item.get("id"), str)]
    ids = set(visible_ids)
    digest = trace.compact_digest(ids) if trace is not None else empty_digest(visible_ids)
    top: list[dict[str, Any]] = []
    for rank, item in enumerate(surfaced, start=1):
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        score = item.get("score")
        if trace is not None:
            try:
                explain = trace.explain_for_id(
                    ident, rank, redact_identity=ident in redacted,
                )
            except Exception:
                explain = None
            if isinstance(explain, dict):
                score = explain.get("pre_rerank_score")
        top.append({"id": ident, "pre_rerank_score": score, "final_rank": rank})
    return _safe_top(top, limit=len(surfaced)), _safe_digest(digest)


def capture_post_egress(
    *,
    vault: str | os.PathLike[str] | None,
    role: str,
    index: Any,
    query: str,
    mode: str,
    k: int,
    rrf_k: int,
    exact_leg_enabled: bool,
    rerank: dict[str, Any],
    latency_ms: float | int,
    top: Iterable[dict[str, Any]],
    candidate_digest: dict[str, Any] | None,
    max_tier: str | None = None,
    now: _dt.datetime | None = None,
) -> bool:
    """Append one host-only, egress-safe capture record.

    This function intentionally accepts only the post-egress top projection and
    digest, not full hits.  A future caller cannot accidentally pass hidden
    candidates through this API without first converting them to the small,
    reviewed shape below.
    """
    if not capture_requested(role):
        return False
    try:
        vault_root, _index_root, log_dir, unsafe = _resolve_location(vault)
    except Exception:
        # We cannot safely name a status file without a resolved vault.
        return False
    if unsafe:
        _note_failure(vault_root, unsafe, configuration=True)
        return False
    if not _secure_dir(log_dir):
        _note_failure(vault_root, "log_permissions_unverified", configuration=True)
        return False
    # A symlink may have appeared while the directory was being created.
    resolved_log = log_dir.resolve()
    if _inside(resolved_log, vault_root):
        _note_failure(vault_root, "log_inside_vault", configuration=True)
        return False

    stamp = now or _utc_now()
    log_file = _month_file(resolved_log, stamp)
    fd: int | None = None
    thread_locked = False
    locked = False
    try:
        thread_locked = _APPEND_THREAD_LOCK.acquire(blocking=False)
        if not thread_locked:
            _note_failure(vault_root, "append_lock_unavailable")
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(log_file), flags, config.SECURE_FILE_MODE)
        if not _secure_fd(fd):
            _note_failure(vault_root, "file_permissions_unverified", configuration=True)
            return False
        if not _try_append_lock(fd):
            _note_failure(vault_root, "append_lock_unavailable")
            return False
        locked = True
        requested = bool(rerank.get("requested", False))
        applied = bool(rerank.get("applied", False))
        top_n = rerank.get("top_n", 0)
        try:
            top_n = max(0, int(top_n))
        except (TypeError, ValueError):
            top_n = 0
        fingerprint = live_index_fingerprint(index)
        if fingerprint is None:
            _note_failure(vault_root, "live_fingerprint_missing")
            return False
        record = {
            "version": VERSION,
            "at": _iso_z(stamp),
            "query": str(query),
            "mode": str(mode),
            "k": max(0, int(k)),
            "rrf_k": int(rrf_k),
            "exact_leg_enabled": bool(exact_leg_enabled),
            "rerank_mode": _rerank_mode(rerank),
            "rerank": {
                "requested": requested,
                "applied": applied,
                "model": rerank.get("model") if isinstance(rerank.get("model"), str) else None,
                "top_n": top_n,
            },
            "latency_ms": round(max(0.0, float(latency_ms)), 3),
            "vault_fingerprint": fingerprint,
            "max_tier": max_tier or cls.DEFAULT_MAX_TIER,
            "top": _safe_top(top, limit=max(0, int(k))),
            "candidate_digest": _safe_digest(candidate_digest),
        }
        _write_all(fd, (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        os.fsync(fd)
    except (OSError, TypeError, ValueError):
        _note_failure(vault_root, "append_failed")
        return False
    finally:
        if fd is not None:
            try:
                if locked:
                    _release_append_lock(fd)
                os.close(fd)
            except OSError:
                pass
        if thread_locked:
            _APPEND_THREAD_LOCK.release()
    _note_success(vault_root)
    return True


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


def status(
    vault: str | os.PathLike[str] | None,
    *,
    role: str = config.ROLE_HOST,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Read-only ledger health.  VM exits before resolving a host log path."""
    if _is_vm_role(role):
        return {"enabled": False, "state": "vm_disabled", "reason": "host_only"}
    if not _enabled_by_env():
        return {"enabled": False, "state": "disabled", "reason": "disabled_by_env"}
    try:
        vault_root, _index_root, log_dir, unsafe = _resolve_location(vault)
    except Exception as exc:
        return {"enabled": False, "state": "error", "reason": f"vault_unresolved:{type(exc).__name__}"}
    state_data = _current_state(vault_root)
    base: dict[str, Any] = {
        "enabled": not bool(unsafe),
        "path": str(log_dir),
        "retention_months": _retention_months(),
        "stale_after_days": _stale_days(),
        "failures": int(state_data.get("failures", 0) or 0),
        "consecutive_failures": int(state_data.get("consecutive_failures", 0) or 0),
        "configuration_errors": int(state_data.get("configuration_errors", 0) or 0),
        "last_failure_at": state_data.get("last_failure_at"),
        "last_failure_code": state_data.get("last_failure_code"),
        "last_capture_at": state_data.get("last_capture_at"),
        "last_prune_at": state_data.get("last_prune_at"),
    }
    if unsafe:
        return {**base, "state": "error", "reason": unsafe,
                "ledger": {"files": 0, "bytes": 0, "records": 0, "age_seconds": None}}
    files = _month_files(log_dir)
    total_bytes = 0
    records = 0
    newest_mtime: float | None = None
    readable = True
    files_owner_only = True
    ledger_valid = True
    for file_path in files:
        try:
            info = file_path.lstat()
            total_bytes += int(info.st_size)
            newest_mtime = max(newest_mtime or info.st_mtime, info.st_mtime)
            if not _is_owner_only_file(file_path):
                files_owner_only = False
                # Do not treat a weakened/symlinked raw-query file as a
                # healthy ledger just because it is still readable by this
                # process. Its metadata is enough for the doctor to name the
                # failure; `brain capture` repairs only a verified regular
                # file before appending a new raw query.
                continue
            # This reads host-only raw-query bytes solely to audit record
            # integrity; it never surfaces query content. Health must not
            # report a corrupt JSONL lane as active merely because the file
            # still has newline-delimited bytes.
            with file_path.open("rb") as handle:
                # Capture holds an exclusive lock until the complete JSON line
                # is fsynced. A health read waits for that short critical
                # section rather than falsely diagnosing a half-written final
                # line as permanent ledger corruption.
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        records += 1
                        try:
                            item = json.loads(line)
                            if not isinstance(item, dict):
                                raise ReplayDataError("non-object record")
                            _validate_record(item, line_number)
                        except (UnicodeDecodeError, json.JSONDecodeError, ReplayDataError):
                            ledger_valid = False
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            readable = False
    now_ts = (now or _utc_now()).timestamp()
    age_seconds = None if newest_mtime is None else max(0.0, now_ts - newest_mtime)
    stale = age_seconds is not None and age_seconds > _stale_days() * 86400
    if not log_dir.exists():
        state = "idle"
    elif not _is_owner_only_dir(log_dir):
        state = "error"
    elif not files_owner_only:
        state = "error"
    elif not ledger_valid:
        state = "error"
    elif not readable:
        state = "error"
    elif stale:
        state = "stale"
    elif records:
        state = "active"
    else:
        state = "idle"
    answer = {
        **base,
        "state": state,
        "ledger": {
            "files": len(files), "bytes": total_bytes, "records": records,
            "age_seconds": None if age_seconds is None else round(age_seconds, 3),
            "owner_only": _is_owner_only_dir(log_dir) if log_dir.exists() else None,
            "files_owner_only": files_owner_only,
            "valid": ledger_valid,
        },
    }
    # The counter is deliberately best-effort (a hardened host may make its
    # application-data directory temporarily unavailable). The ledger's own
    # mtime remains an auditable liveness signal, provided it contains at
    # least one complete record; do not mistake an empty failed append for a
    # successful capture.
    if not answer["last_capture_at"] and records and newest_mtime is not None:
        answer["last_capture_at"] = _iso_z(
            _dt.datetime.fromtimestamp(newest_mtime, tz=_dt.timezone.utc)
        )
        answer["last_capture_at_source"] = "ledger_mtime"
    if state == "error" and not files_owner_only:
        answer["reason"] = "file_permissions_unverified"
    elif state == "error" and not ledger_valid:
        answer["reason"] = "ledger_malformed"
    return answer


def _read_records(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            # A replay may be pointed directly at the current private month.
            # Cooperate with the capture writer so a valid in-flight append
            # cannot be misclassified as malformed JSONL.
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            except (ImportError, OSError):
                # Non-POSIX capture is disabled, so no compatible appender can
                # be active there. Keep exported-log replay usable if a host
                # copied the file to such a platform.
                fcntl = None  # type: ignore[assignment]
            try:
                raw = handle.read()
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReplayDataError(f"cannot read capture log: {type(exc).__name__}") from exc
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayDataError(f"malformed JSONL at line {number}") from exc
        if not isinstance(item, dict):
            raise ReplayDataError(f"non-object record at line {number}")
        _validate_record(item, number)
        records.append(item)
    return records


def _validate_record(record: dict[str, Any], number: int) -> None:
    if record.get("version") != VERSION:
        raise ReplayDataError(f"unsupported record version at line {number}")
    if not isinstance(record.get("query"), str):
        raise ReplayDataError(f"missing query at line {number}")
    if record.get("mode") not in {"hybrid-search", "search", "dossier"}:
        raise ReplayDataError(f"unsupported mode at line {number}")
    if not isinstance(record.get("k"), int) or record["k"] < 0:
        raise ReplayDataError(f"invalid k at line {number}")
    if not isinstance(record.get("rrf_k"), int) or record["rrf_k"] <= 0:
        raise ReplayDataError(f"invalid rrf_k at line {number}")
    if _normalise_fingerprint(record.get("vault_fingerprint")) is None:
        raise ReplayDataError(f"missing vault fingerprint at line {number}")
    if not isinstance(record.get("top"), list):
        raise ReplayDataError(f"invalid top list at line {number}")
    if _safe_float(record.get("latency_ms")) is None:
        raise ReplayDataError(f"invalid latency at line {number}")
    rerank = record.get("rerank")
    if not isinstance(rerank, dict) or not isinstance(rerank.get("requested"), bool):
        raise ReplayDataError(f"invalid rerank metadata at line {number}")
    # `rerank_mode` was added while the v1 implementation was still being
    # rolled out.  Keep the first v1 rows replayable by deriving it from the
    # mandatory detailed object when absent; new captures always write it.
    if ("rerank_mode" in record
            and record.get("rerank_mode") not in {"disabled", "requested_not_applied", "applied"}):
        raise ReplayDataError(f"invalid rerank mode at line {number}")
    if "rerank_mode" in record and record["rerank_mode"] != _rerank_mode(rerank):
        raise ReplayDataError(f"inconsistent rerank mode at line {number}")
    if "max_tier" in record and record["max_tier"] not in cls.RANK:
        raise ReplayDataError(f"invalid max tier at line {number}")
    digest = record.get("candidate_digest")
    if not isinstance(digest, dict):
        raise ReplayDataError(f"invalid candidate digest at line {number}")
    for rank, item in enumerate(record["top"], start=1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ReplayDataError(f"invalid top result at line {number}, rank {rank}")
        if item.get("pre_rerank_score") is not None and _safe_float(item.get("pre_rerank_score")) is None:
            raise ReplayDataError(f"invalid top score at line {number}, rank {rank}")
        if not isinstance(item.get("final_rank"), int) or item["final_rank"] < 1:
            raise ReplayDataError(f"invalid top rank at line {number}, rank {rank}")
    top_ids = [item["id"] for item in record["top"]]
    if len(set(top_ids)) != len(top_ids):
        raise ReplayDataError(f"duplicate top id at line {number}")


def _gated_hybrid(core: Any, record: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rerank = record["rerank"]
    top_n = rerank.get("top_n", 0)
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 15
    # Replay uses the same production trace path as host capture.  The trace
    # remains pre-egress until `projection_from_gated` receives the filtered
    # rows below, so a replay digest cannot resurrect a withheld candidate.
    trace_hits, trace = core.hybrid_search_with_trace(
        record["query"], k=record["k"], rerank=bool(rerank.get("requested")),
        rerank_top=top_n or 15, rrf_k=record["rrf_k"],
    )
    hits = [hit.to_dict() for hit in trace_hits]
    max_tier = record.get("max_tier", cls.DEFAULT_MAX_TIER)
    surfaced, _report = egress.apply_gate(hits, str(max_tier))
    redacted_ids = core.annotate_create_safety(record["query"], surfaced, str(max_tier))
    _top, digest = projection_from_gated(
        surfaced, trace=trace, redacted_ids=redacted_ids,
    )
    return surfaced, digest


def _gated_dossier(core: Any, record: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = core.dossier(record["query"], k=record["k"])
    max_tier = record.get("max_tier", cls.DEFAULT_MAX_TIER)
    decisions, _drep = egress.apply_gate(result.get("decisions", []), str(max_tier))
    sources, _srep = egress.apply_gate(result.get("sources", []), str(max_tier))
    surfaced = decisions + sources
    return surfaced, empty_digest([item.get("id", "") for item in surfaced])


def _jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _rank_movement(left: list[str], right: list[str]) -> dict[str, Any]:
    """Describe movement only among IDs that occur in both top-k lists.

    This deliberately says nothing about a newly introduced or disappeared
    item: a real-traffic ledger has no relevance labels, and a drifted vault
    cannot honestly be decomposed into content causes.  The per-result view
    remains useful alongside Jaccard for vault-same ranking/config changes.
    """
    old_rank = {ident: rank for rank, ident in enumerate(left, start=1)}
    new_rank = {ident: rank for rank, ident in enumerate(right, start=1)}
    deltas = [abs(old_rank[ident] - new_rank[ident]) for ident in old_rank.keys() & new_rank.keys()]
    return {
        "overlapping_ids": len(deltas),
        "mean_absolute_delta": _mean([float(delta) for delta in deltas]),
        "max_absolute_delta": max(deltas) if deltas else None,
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _segment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    jaccards = [float(row["jaccard_at_k"]) for row in rows]
    top1 = [1.0 if row["top1_stable"] else 0.0 for row in rows]
    deltas = [float(row["latency_delta_ms"]) for row in rows]
    baseline = [float(row["baseline_latency_ms"]) for row in rows]
    replayed = [float(row["replay_latency_ms"]) for row in rows]
    digest_baseline = [1.0 if row["baseline_digest_present"] else 0.0 for row in rows]
    digest_replay = [1.0 if row["replay_digest_present"] else 0.0 for row in rows]
    movement = [
        float(row["rank_movement"]["mean_absolute_delta"])
        for row in rows
        if row["rank_movement"]["mean_absolute_delta"] is not None
    ]
    movement_max = [
        int(row["rank_movement"]["max_absolute_delta"])
        for row in rows
        if row["rank_movement"]["max_absolute_delta"] is not None
    ]
    return {
        "count": len(rows),
        "jaccard_at_k": _mean(jaccards),
        "top1_stability": _mean(top1),
        "latency_ms": {
            "baseline_mean": _mean(baseline), "replay_mean": _mean(replayed),
            "delta_mean": _mean(deltas),
        },
        "candidate_digest_presence": {
            "baseline_rate": _mean(digest_baseline), "replay_rate": _mean(digest_replay),
        },
        "rank_movement": {
            "queries_with_overlap": len(movement),
            "mean_absolute_delta": _mean(movement),
            "max_absolute_delta": max(movement_max) if movement_max else None,
        },
    }


def replay(
    core: Any,
    against: str | os.PathLike[str],
    *,
    fail_under_top1: float | None = None,
    fail_under_jaccard: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """Replay an existing host ledger and return ``(report, thresholds_failed)``.

    Only the ``vault_same`` segment is comparable enough to enforce thresholds;
    a changed fingerprint is intentionally reported as drift/mixture without
    attempting to diagnose additions, deletions, moves, or supersessions.
    """
    if _is_vm_role(getattr(core, "role", config.ROLE_HOST)):
        # The CLI rejects this before constructing BrainCore; retain the same
        # boundary for programmatic callers so a VM can never use a mounted
        # export path to read raw host queries through this helper.
        raise ReplayDataError("replay is host-only")
    for label, threshold in (("top1", fail_under_top1), ("jaccard", fail_under_jaccard)):
        if threshold is not None and not 0.0 <= threshold <= 1.0:
            raise ValueError(f"--fail-under-{label} must be in [0, 1]")
    source = Path(against).expanduser().resolve()
    records = _read_records(source)
    current_fingerprint = live_index_fingerprint(core.index)
    if current_fingerprint is None:
        raise ReplayDataError(
            "current live-index fingerprint is missing; run host `brain sync` or `brain rebuild` first"
        )
    same: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for ordinal, record in enumerate(records, start=1):
        started = time.perf_counter()
        if record["mode"] in {"hybrid-search", "search"}:
            surfaced, replay_digest = _gated_hybrid(core, record)
        else:
            surfaced, replay_digest = _gated_dossier(core, record)
        replay_latency = round((time.perf_counter() - started) * 1000, 3)
        old_ids = [
            item["id"]
            for item in sorted(record["top"], key=lambda item: item["final_rank"])
        ]
        new_ids = [item["id"] for item in surfaced if isinstance(item.get("id"), str)]
        k = record["k"]
        old_ids, new_ids = old_ids[:k], new_ids[:k]
        category = "vault_same" if _normalise_fingerprint(record["vault_fingerprint"]) == _normalise_fingerprint(current_fingerprint) else "drift_or_mixed"
        row = {
            "record": ordinal,
            "mode": record["mode"],
            "k": k,
            "comparison": category,
            "jaccard_at_k": round(_jaccard(old_ids, new_ids), 6),
            "top1_stable": (old_ids[:1] == new_ids[:1]),
            "rank_movement": _rank_movement(old_ids, new_ids),
            "baseline_latency_ms": round(float(record["latency_ms"]), 3),
            "replay_latency_ms": replay_latency,
            "latency_delta_ms": round(replay_latency - float(record["latency_ms"]), 3),
            "baseline_digest_present": isinstance(record.get("candidate_digest"), dict),
            "replay_digest_present": isinstance(replay_digest, dict),
        }
        results.append(row)
        (same if category == "vault_same" else drift).append(row)
    same_summary = _segment(same)
    drift_summary = _segment(drift)
    breaches: list[str] = []
    # No comparable records is explicitly a successful, non-gating negative
    # control.  Drift may be interesting, but it cannot establish regression.
    if same and fail_under_top1 is not None and (same_summary["top1_stability"] or 0.0) < fail_under_top1:
        breaches.append("top1")
    if same and fail_under_jaccard is not None and (same_summary["jaccard_at_k"] or 0.0) < fail_under_jaccard:
        breaches.append("jaccard")
    report = {
        "version": VERSION,
        "against": str(source),
        "current_vault_fingerprint": current_fingerprint,
        "records": {"total": len(results), "vault_same": len(same), "drift_or_mixed": len(drift)},
        "vault_same": same_summary,
        "drift_or_mixed": drift_summary,
        "target_qrels": "not_applicable",
        "thresholds": {
            "fail_under_top1": fail_under_top1,
            "fail_under_jaccard": fail_under_jaccard,
            "evaluated_records": len(same),
            "breaches": breaches,
        },
        "results": results,
    }
    return report, bool(breaches)
