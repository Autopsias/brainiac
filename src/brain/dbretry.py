"""Bounded, jittered retry for SQLite write-lock contention (CC-01).

Why this exists, and why a retry loop alone is not enough:

SQLite's ``busy_handler`` (``PRAGMA busy_timeout``, set at index.py's
``conn`` property) does NOT get invoked for every contended case. In
particular, a connection that opened a DEFERRED transaction (Python's
sqlite3 default) and only discovers on its first write that it needs to
*upgrade* to a write lock hits ``SQLITE_BUSY`` immediately, with no wait at
all -- see sqlite.org/c3ref/busy_handler.html on lock-upgrade "deadlocks".
Retrying that case blind would just busy-loop past the same instant
collision every time. The real fix is ``BEGIN IMMEDIATE`` (take the write
lock up front, so the busy handler's wait is legally invoked) -- this
module is the OUTER bound for whatever *ordinary* contention still
surfaces as ``database is locked``/``database is busy`` after
``busy_timeout`` is exhausted (e.g. the hourly job and a hand-run command
both landing on ``BEGIN IMMEDIATE`` at nearly the same instant).
"""
from __future__ import annotations

import os
import random
import sqlite3
import time
from typing import Callable, TypeVar

T = TypeVar("T")

_LOCK_MESSAGES = ("database is locked", "database is busy")


def is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return any(m in msg for m in _LOCK_MESSAGES)


def with_write_retry(
    fn: Callable[[], T],
    *,
    conn: sqlite3.Connection | None = None,
    max_seconds: float | None = None,
) -> T:
    """Run ``fn`` (a write), retrying ONLY on sqlite lock contention.

    CRITICAL: rolls back ``conn`` before every retry. A failed write left
    sitting inside an open transaction still holds a read lock -- exactly
    what would block the OTHER writer's commit, turning transient
    contention into a livelock. ``fn`` must be idempotent / safe to call
    again from scratch (it should issue its own ``BEGIN IMMEDIATE`` and
    ``COMMIT`` so a retry redoes the whole unit of work cleanly).

    Bounded total wait (~30s default; override via
    ``$BRAIN_WRITE_RETRY_SECONDS`` or the ``max_seconds`` kwarg -- tests use
    a short bound so contention tests stay fast and deterministic).
    Exponential backoff with jitter between attempts. Any non-lock error
    (or a lock error past the bound) re-raises immediately -- this is not a
    general-purpose retry, only the specific transient-contention case.
    """
    bound = max_seconds if max_seconds is not None else float(
        os.environ.get("BRAIN_WRITE_RETRY_SECONDS", "30")
    )
    start = time.monotonic()
    delay = 0.05
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_lock_error(exc):
                raise
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.OperationalError:
                    pass
            elapsed = time.monotonic() - start
            if elapsed >= bound:
                raise
            sleep_for = min(delay, max(0.0, bound - elapsed)) * (0.5 + random.random())
            time.sleep(sleep_for)
            delay = min(delay * 2, 2.0)
