"""Maintenance state methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    Path,
    WriterLockBusy,
    config,
)


class _MaintenanceStateMixin:
    """Maintenance state methods for BrainCore."""

    def _acquire_maintain_lock(
        self, lock_path: Path, *, stale_after_seconds: float = 2 * 3600,
    ) -> dict[str, Any] | None:
        """Best-effort single-runner lock. Returns the lock-info dict on
        success, or ``None`` if another live-looking ``maintain`` run holds it
        (caller should skip the run, never block/wait). A lock older than
        ``stale_after_seconds`` — far beyond the ADR's ~60s/5min graphify
        budget — is treated as an abandoned crash and broken automatically.

        A lock whose holder is DEAD is broken immediately (2026-08-18). The
        two-hour timer alone left a crashed run blocking every hourly firing
        for two hours: measured on a live reference vault, where a dead pid held
        this lock for 92 minutes and the corpus-invariant metrics went on
        reporting a regression that was already repaired. Liveness is checked
        only for a lock written by THIS host — the file sits on the Cowork
        mount, and a pid number means nothing across machines — and only ever
        SHORTENS the wait: a reused pid looks alive, so it falls back to the
        timer rather than breaking a lock someone may still hold."""
        import json as _json
        import os as _os
        import socket as _socket
        import time as _time

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        info = {"pid": _os.getpid(), "started": _time.time(),
                "host": _socket.gethostname()}
        try:
            fd = _os.open(str(lock_path), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
            with _os.fdopen(fd, "w") as fh:
                fh.write(_json.dumps(info))
            return info
        except FileExistsError:
            pass
        existing = self._read_maintain_lock(lock_path)
        started = existing.get("started")
        expired = (isinstance(started, (int, float))
                   and (_time.time() - started) > stale_after_seconds)
        if expired or self._maintain_lock_holder_is_dead(existing):
            lock_path.unlink(missing_ok=True)
            return self._acquire_maintain_lock(lock_path, stale_after_seconds=stale_after_seconds)
        return None

    @staticmethod
    def _maintain_lock_holder_is_dead(existing: dict[str, Any]) -> bool:
        """True only when THIS host wrote the lock and that pid is gone."""
        import os as _os
        import socket as _socket

        pid = existing.get("pid")
        host = existing.get("host")
        if not isinstance(pid, int) or pid <= 0:
            return False  # pre-2026-08-18 lock, or malformed — let the timer rule
        if host != _socket.gethostname():
            return False  # another machine's pid number is not ours to interpret
        if pid == _os.getpid():
            return False
        try:
            _os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False  # alive, owned by another user
        except OSError:
            return False
        return False

    def _read_maintain_lock(self, lock_path: Path) -> dict[str, Any]:
        import json as _json

        try:
            return _json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    def _release_maintain_lock(self, lock_path: Path) -> None:
        lock_path.unlink(missing_ok=True)
    def _mark_writer_busy(
        self, state: dict[str, Any], branch: str, d: Any, exc: "WriterLockBusy"
    ) -> None:
        """CC-02 skip contract (s02 owns this definition; s05 consumes it).

        A clean writer-busy skip is NOT a failure and NOT work completed:
        - status literal ``skipped-writer-busy``
        - refresh ``last_attempt``; NEVER increment ``consecutive_failures``
        - record ``writer_busy_since`` (first consecutive skip) + holder
          (pid/verb) metadata
        - increment a SEPARATE ``consecutive_skips`` counter
        - NEVER touch ``last_run`` / ``last_successful_index_run`` -- those
          mean work completed, and are what liveness detection keys on.
        """
        prev = state.get(branch) if isinstance(state.get(branch), dict) else {}
        entry = dict(prev)
        entry["last_attempt"] = d.isoformat()
        entry["status"] = "skipped-writer-busy"
        entry["consecutive_skips"] = int(prev.get("consecutive_skips", 0)) + 1
        entry["writer_busy_since"] = prev.get("writer_busy_since") or d.isoformat()
        entry["writer_busy_holder"] = {
            "pid": exc.holder.get("pid"), "verb": exc.holder.get("verb"),
        }
        # Left exactly as they were -- a skip is not a failure.
        entry["consecutive_failures"] = int(prev.get("consecutive_failures", 0))
        state[branch] = entry
    def _load_maintain_state(self) -> dict[str, Any]:
        import json as _json

        path = config.maintain_state_path(self.vault)
        try:
            state = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}
    def _save_maintain_state(self, state: dict[str, Any]) -> None:
        """Atomic write (tmp + replace) so a crash mid-write never corrupts
        the file the session-start hook and ``brain status`` both read."""
        import json as _json
        import os as _os

        path = config.maintain_state_path(self.vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(_json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        _os.replace(tmp, path)
    def _note_graphify_build_outcome(
        self, *, status: str, attempted_at: str, detail: str = "",
    ) -> None:
        """Best-effort, ALWAYS-updated record of the graphify build's last
        outcome, persisted into ``.brain/maintain-state.json`` under the
        ``graphify`` entry's ``last_build_*`` keys (finding 4, 2026-07-20
        dedup batch) — deliberately SEPARATE from ``maintain()``'s own
        ``_mark("graphify", ...)`` due/streak bookkeeping (`last_run`,
        `consecutive_failures`, ...), so a raw/manual `brain graphify`
        invocation (never wrapped by `maintain`) still leaves a trace
        `brain status`/the retro fold can see, without ever perturbing
        `maintain`'s own monthly-floor streak logic. Never raises — this is
        observability, not a load-bearing write."""
        try:
            state = self._load_maintain_state()
            prev = state.get("graphify") if isinstance(state.get("graphify"), dict) else {}
            entry = dict(prev)
            entry["last_build_attempt"] = attempted_at
            entry["last_build_status"] = status
            if detail:
                entry["last_build_detail"] = detail
            else:
                entry.pop("last_build_detail", None)
            state["graphify"] = entry
            self._save_maintain_state(state)
        except Exception:  # noqa: BLE001 — observability only, never blocking
            pass
    def _maintain_heartbeat_summary(self, today: Any = None) -> dict[str, Any]:
        """``brain status`` surfacing (HARDENED:premortem) — a heartbeat older
        than 48h on the ``daily`` branch, or any branch with >=2 consecutive
        failures, is flagged. Reads the SAME file `maintain` writes and the
        session-start hook already reads (one file, two consumers)."""
        import datetime as _dt

        state = self._load_maintain_state()
        if not state:
            return {"status": "no-record", "note": "no maintain-state.json yet — brain maintain has not run"}
        today = today or _dt.date.today()
        branches: dict[str, Any] = {}
        stale, repeated_failures = [], []
        for branch, entry in state.items():
            if str(branch).startswith("_"):
                continue  # [S04 fix 7] marker, not a branch (mirrors maintain()'s own filter)
            if not isinstance(entry, dict):
                continue
            last_run = entry.get("last_run")
            age_hours: float | None = None
            if last_run:
                try:
                    age_hours = (today - _dt.date.fromisoformat(last_run)).days * 24
                except ValueError:
                    age_hours = None
            branches[branch] = {
                "last_run": last_run,
                "last_attempt": entry.get("last_attempt"),
                "status": entry.get("status"),
                "consecutive_failures": entry.get("consecutive_failures", 0),
                "age_hours": age_hours,
            }
            if branch == "daily" and (entry.get("failed") or (age_hours is not None and age_hours > 48)):
                stale.append(branch)
            if int(entry.get("consecutive_failures", 0)) >= 2:
                repeated_failures.append(branch)
        overall = "stale" if stale else ("repeated_failures" if repeated_failures else "ok")
        return {
            "status": overall, "stale_branches": stale,
            "repeated_failure_branches": repeated_failures, "branches": branches,
        }
    def _hot_md_path(self) -> Path:
        return config.memory_dir(self.vault) / "hot.md"
    def _append_hot_once(self, key: str, entry_md: str) -> bool:
        """Append ``entry_md`` to ``hot.md`` guarded by an idempotency-key
        HTML comment; a no-op (returns ``False``) if the key is already
        present — the per-branch/per-run-date idempotency guard for every
        scheduled fold that queues a hot-queue entry (HARDENED:codex).

        Two shared gates live HERE so every fold renderer inherits them:
        (1) vault-absolute paths are rewritten vault-relative (retro
        signature ``absolute-paths`` — an absolute path in fold output goes
        stale on a vault move); (2) the idempotency check also scans rotated
        ``archive/hot-*.md`` segments, so an entry rotated out by
        ``_rotate_hot_md`` is never re-appended under the same key."""
        import os as _os
        path = self._hot_md_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for root in {str(self.vault), str(Path(self.vault).resolve())}:
            entry_md = entry_md.replace(root.rstrip(_os.sep) + _os.sep, "")
        marker = f"<!-- idempotency-key: {key} -->"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if marker in existing:
            return False
        # ponytail: linear scan of rotated segments; index them if they ever
        # number in the hundreds.
        for seg in sorted((path.parent / "archive").glob("hot-*.md")):
            try:
                if marker in seg.read_text(encoding="utf-8"):
                    return False
            except OSError:
                continue
        with path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(marker + "\n" + entry_md.rstrip("\n") + "\n\n")
        return True
    def _rotate_hot_md(self, today: Any) -> bool:
        """Rotate aged, resolved hot.md entries into ``archive/hot-<date>.md``
        once the live file exceeds the soft cap (retro signature
        ``hot-md-bloat``) — same auto-rotate posture handoff.md already has.
        Pure judgment lives in ``maintenance.rotate_hot_md``; this method is
        the file I/O. Returns True when a rotation happened."""
        from .. import maintenance as maint
        path = self._hot_md_path()
        if not path.exists():
            return False
        kept, rotated = maint.rotate_hot_md(
            path.read_text(encoding="utf-8"), today)
        if not rotated:
            return False
        archive_dir = path.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        seg = archive_dir / f"hot-{today.isoformat()}.md"
        with seg.open("a", encoding="utf-8") as fh:
            fh.write(rotated)
        path.write_text(kept, encoding="utf-8")
        return True
    def _inbox_path(self) -> Path:
        return config.memory_dir(self.vault) / "inbox.jsonl"
    def _read_inbox(self) -> list[dict[str, Any]]:
        from .. import inbox as ibx
        p = self._inbox_path()
        return ibx.parse_inbox(p.read_text(encoding="utf-8")) if p.exists() else []
    def _write_inbox(self, entries: list[dict[str, Any]]) -> None:
        from .. import inbox as ibx
        p = self._inbox_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ibx.render_inbox(entries), encoding="utf-8")
    def enqueue_question(self, question: dict[str, Any], *,
                         source: str = "", today: Any = None) -> bool:
        """Queue ONE owner-only decision (validated options+default). Idempotent
        on (source, question). Host-only: the queue is host session state under
        ``.brain/`` — the headless synthesis session enqueues, an interactive
        ``/brain-inbox`` answers. Returns True if newly appended."""
        from .. import inbox as ibx
        import datetime as _dt
        self._require_host("enqueue an owner question")
        d = today or _dt.date.today()
        entries, appended = ibx.enqueue(
            self._read_inbox(), question, created=d.isoformat(), source=source)
        if appended:
            self._write_inbox(entries)
        return appended
    def answer_question(self, key: str, answer: str, *, today: Any = None,
                        answered_at: str | None = None) -> bool:
        """Record the owner's answer to queued question ``key``. Host-only. The
        answer is CONSUMED (executed through the audited write path) by the next
        fold — recording it here is plain host queue state, not an index write."""
        from .. import inbox as ibx
        import datetime as _dt
        self._require_host("answer an owner question")
        d = today or _dt.date.today()
        entries, matched = ibx.record_answer(
            self._read_inbox(), key, answer, answered=d.isoformat(),
            answered_at=answered_at)
        if matched:
            self._write_inbox(entries)
        return matched
    def open_questions(self) -> list[dict[str, Any]]:
        """The open owner-decision queue (host-only read)."""
        from .. import inbox as ibx
        self._require_host("read the owner question queue")
        return ibx.open_questions(self._read_inbox())
