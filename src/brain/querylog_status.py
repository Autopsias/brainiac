"""Maintain the query-log status seam."""
from __future__ import annotations

import datetime as _dt
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


_SOURCE: Mapping[str, Any] | None = None


def configure(source: Mapping[str, Any]) -> None:
    """Bind the source module's stable filesystem and policy helpers."""
    global _SOURCE
    _SOURCE = source


class _SourceProxy:
    """Resolve helpers from the facade so monkeypatches remain observable."""

    def __getattr__(self, name: str) -> Any:
        if _SOURCE is None:
            raise RuntimeError("query-log facade has not configured its status seam")
        return _SOURCE[name]


source = _SourceProxy()


def _build_capture_record(
    index: Any,
    stamp: _dt.datetime,
    query: str,
    mode: str,
    k: int,
    rrf_k: int,
    exact_leg_enabled: bool,
    rerank: dict[str, Any],
    latency_ms: float | int,
    top: Iterable[dict[str, Any]],
    candidate_digest: dict[str, Any] | None,
    max_tier: str | None,
) -> dict[str, Any] | None:
    """Build the bounded persisted representation of one gated capture."""
    requested = bool(rerank.get("requested", False))
    applied = bool(rerank.get("applied", False))
    top_n = rerank.get("top_n", 0)
    try:
        top_n = max(0, int(top_n))
    except (TypeError, ValueError):
        top_n = 0
    fingerprint = source.live_index_fingerprint(index)
    if fingerprint is None:
        return None
    return {
        "version": source.VERSION,
        "at": source._iso_z(stamp),
        "query": str(query),
        "mode": str(mode),
        "k": max(0, int(k)),
        "rrf_k": int(rrf_k),
        "exact_leg_enabled": bool(exact_leg_enabled),
        "rerank_mode": source._rerank_mode(rerank),
        "rerank": {
            "requested": requested,
            "applied": applied,
            "model": rerank.get("model") if isinstance(rerank.get("model"), str) else None,
            "top_n": top_n,
        },
        "latency_ms": round(max(0.0, float(latency_ms)), 3),
        "vault_fingerprint": fingerprint,
        "max_tier": max_tier or source.cls.DEFAULT_MAX_TIER,
        "top": source._safe_top(top, limit=max(0, int(k))),
        "candidate_digest": source._safe_digest(candidate_digest),
    }


def _prepare_capture_location(
    vault: str | os.PathLike[str] | None,
) -> tuple[Path, Path] | None:
    """Resolve and verify the private query-log directory."""
    try:
        vault_root, _index_root, log_dir, unsafe = source._resolve_location(vault)
    except Exception:
        return None
    if unsafe:
        source._note_failure(vault_root, unsafe, configuration=True)
        return None
    if not source._secure_dir(log_dir):
        source._note_failure(vault_root, "log_permissions_unverified", configuration=True)
        return None
    resolved_log = log_dir.resolve()
    if source._inside(resolved_log, vault_root):
        source._note_failure(vault_root, "log_inside_vault", configuration=True)
        return None
    return vault_root, resolved_log


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
    """Append one host-only, egress-safe capture record."""
    if not source.capture_requested(role):
        return False
    location = _prepare_capture_location(vault)
    if location is None:
        return False
    vault_root, resolved_log = location

    stamp = now or source._utc_now()
    log_file = source._month_file(resolved_log, stamp)
    fd: int | None = None
    thread_locked = False
    locked = False
    try:
        thread_locked = source._APPEND_THREAD_LOCK.acquire(blocking=False)
        if not thread_locked:
            source._note_failure(vault_root, "append_lock_unavailable")
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(log_file), flags, source.config.SECURE_FILE_MODE)
        if not source._secure_fd(fd):
            source._note_failure(vault_root, "file_permissions_unverified", configuration=True)
            return False
        if not source._try_append_lock(fd):
            source._note_failure(vault_root, "append_lock_unavailable")
            return False
        locked = True
        record = _build_capture_record(
            index, stamp, query, mode, k, rrf_k, exact_leg_enabled,
            rerank, latency_ms, top, candidate_digest, max_tier,
        )
        if record is None:
            source._note_failure(vault_root, "live_fingerprint_missing")
            return False
        source._write_all(
            fd,
            (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        os.fsync(fd)
    except (OSError, TypeError, ValueError):
        source._note_failure(vault_root, "append_failed")
        return False
    finally:
        if fd is not None:
            try:
                if locked:
                    source._release_append_lock(fd)
                os.close(fd)
            except OSError:
                pass
        if thread_locked:
            source._APPEND_THREAD_LOCK.release()
    source._note_success(vault_root)
    return True


def _validate_record_header(record: dict[str, Any], number: int) -> None:
    """Validate the required scalar fields of one query-log record."""
    if record.get("version") != source.VERSION:
        raise source.ReplayDataError(f"unsupported record version at line {number}")
    if not isinstance(record.get("query"), str):
        raise source.ReplayDataError(f"missing query at line {number}")
    if record.get("mode") not in {"hybrid-search", "search", "dossier"}:
        raise source.ReplayDataError(f"unsupported mode at line {number}")
    if not isinstance(record.get("k"), int) or record["k"] < 0:
        raise source.ReplayDataError(f"invalid k at line {number}")
    if not isinstance(record.get("rrf_k"), int) or record["rrf_k"] <= 0:
        raise source.ReplayDataError(f"invalid rrf_k at line {number}")
    if source._normalise_fingerprint(record.get("vault_fingerprint")) is None:
        raise source.ReplayDataError(f"missing vault fingerprint at line {number}")
    if not isinstance(record.get("top"), list):
        raise source.ReplayDataError(f"invalid top list at line {number}")
    if source._safe_float(record.get("latency_ms")) is None:
        raise source.ReplayDataError(f"invalid latency at line {number}")


def _validate_record_metadata(record: dict[str, Any], number: int) -> None:
    """Validate rerank, egress-tier, and digest metadata."""
    rerank = record.get("rerank")
    if not isinstance(rerank, dict) or not isinstance(rerank.get("requested"), bool):
        raise source.ReplayDataError(f"invalid rerank metadata at line {number}")
    if (
        "rerank_mode" in record
        and record.get("rerank_mode") not in {"disabled", "requested_not_applied", "applied"}
    ):
        raise source.ReplayDataError(f"invalid rerank mode at line {number}")
    if "rerank_mode" in record and record["rerank_mode"] != source._rerank_mode(rerank):
        raise source.ReplayDataError(f"inconsistent rerank mode at line {number}")
    if "max_tier" in record and record["max_tier"] not in source.cls.RANK:
        raise source.ReplayDataError(f"invalid max tier at line {number}")
    digest = record.get("candidate_digest")
    if not isinstance(digest, dict):
        raise source.ReplayDataError(f"invalid candidate digest at line {number}")


def _validate_record_top(record: dict[str, Any], number: int) -> None:
    """Validate top-result identity, score, rank, and uniqueness."""
    for rank, item in enumerate(record["top"], start=1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise source.ReplayDataError(f"invalid top result at line {number}, rank {rank}")
        if (
            item.get("pre_rerank_score") is not None
            and source._safe_float(item.get("pre_rerank_score")) is None
        ):
            raise source.ReplayDataError(f"invalid top score at line {number}, rank {rank}")
        if not isinstance(item.get("final_rank"), int) or item["final_rank"] < 1:
            raise source.ReplayDataError(f"invalid top rank at line {number}, rank {rank}")
    top_ids = [item["id"] for item in record["top"]]
    if len(set(top_ids)) != len(top_ids):
        raise source.ReplayDataError(f"duplicate top id at line {number}")


def _validate_record(record: dict[str, Any], number: int) -> None:
    """Validate one persisted query-log record."""
    _validate_record_header(record, number)
    _validate_record_metadata(record, number)
    _validate_record_top(record, number)


def _scan_ledger_files(files: list[Path]) -> dict[str, Any]:
    """Scan owner-only ledger files and validate every JSONL record."""
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
            if not source._is_owner_only_file(file_path):
                files_owner_only = False
                continue
            with file_path.open("rb") as handle:
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
                                raise source.ReplayDataError("non-object record")
                            _validate_record(item, line_number)
                        except (UnicodeDecodeError, json.JSONDecodeError, source.ReplayDataError):
                            ledger_valid = False
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            readable = False
    return {
        "total_bytes": total_bytes,
        "records": records,
        "newest_mtime": newest_mtime,
        "readable": readable,
        "files_owner_only": files_owner_only,
        "ledger_valid": ledger_valid,
    }


def _ledger_state(
    log_dir: Path,
    files_owner_only: bool,
    ledger_valid: bool,
    readable: bool,
    stale: bool,
    records: int,
) -> str:
    """Classify the private ledger from its scan and freshness signals."""
    if not log_dir.exists():
        return "idle"
    if not source._is_owner_only_dir(log_dir):
        return "error"
    if not files_owner_only or not ledger_valid or not readable:
        return "error"
    if stale:
        return "stale"
    return "active" if records else "idle"


def status(
    vault: str | os.PathLike[str] | None,
    *,
    role: str = "host",
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Read-only query-ledger health projection."""
    if source._is_vm_role(role):
        return {"enabled": False, "state": "vm_disabled", "reason": "host_only"}
    if not source._enabled_by_env():
        return {"enabled": False, "state": "disabled", "reason": "disabled_by_env"}
    try:
        vault_root, _index_root, log_dir, unsafe = source._resolve_location(vault)
    except Exception as exc:
        return {
            "enabled": False,
            "state": "error",
            "reason": f"vault_unresolved:{type(exc).__name__}",
        }
    state_data = source._current_state(vault_root)
    base: dict[str, Any] = {
        "enabled": not bool(unsafe),
        "path": str(log_dir),
        "retention_months": source._retention_months(),
        "stale_after_days": source._stale_days(),
        "failures": int(state_data.get("failures", 0) or 0),
        "consecutive_failures": int(state_data.get("consecutive_failures", 0) or 0),
        "configuration_errors": int(state_data.get("configuration_errors", 0) or 0),
        "last_failure_at": state_data.get("last_failure_at"),
        "last_failure_code": state_data.get("last_failure_code"),
        "last_capture_at": state_data.get("last_capture_at"),
        "last_prune_at": state_data.get("last_prune_at"),
    }
    if unsafe:
        return {
            **base,
            "state": "error",
            "reason": unsafe,
            "ledger": {"files": 0, "bytes": 0, "records": 0, "age_seconds": None},
        }
    files = source._month_files(log_dir)
    ledger = _scan_ledger_files(files)
    total_bytes = ledger["total_bytes"]
    records = ledger["records"]
    newest_mtime = ledger["newest_mtime"]
    readable = ledger["readable"]
    files_owner_only = ledger["files_owner_only"]
    ledger_valid = ledger["ledger_valid"]
    now_ts = (now or source._utc_now()).timestamp()
    age_seconds = None if newest_mtime is None else max(0.0, now_ts - newest_mtime)
    stale = age_seconds is not None and age_seconds > source._stale_days() * 86400
    state = _ledger_state(
        log_dir, files_owner_only, ledger_valid, readable, stale, records,
    )
    answer = {
        **base,
        "state": state,
        "ledger": {
            "files": len(files),
            "bytes": total_bytes,
            "records": records,
            "age_seconds": None if age_seconds is None else round(age_seconds, 3),
            "owner_only": source._is_owner_only_dir(log_dir) if log_dir.exists() else None,
            "files_owner_only": files_owner_only,
            "valid": ledger_valid,
        },
    }
    if not answer["last_capture_at"] and records and newest_mtime is not None:
        answer["last_capture_at"] = source._iso_z(
            _dt.datetime.fromtimestamp(newest_mtime, tz=_dt.timezone.utc)
        )
        answer["last_capture_at_source"] = "ledger_mtime"
    if state == "error" and not files_owner_only:
        answer["reason"] = "file_permissions_unverified"
    elif state == "error" and not ledger_valid:
        answer["reason"] = "ledger_malformed"
    return answer
