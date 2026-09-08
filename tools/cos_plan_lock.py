#!/usr/bin/env python3
"""Acquire/release the COS plan-lock: the ACQUIRER side of the guard
`cos_nightly.sh` already carries (see its own "BEGIN plan-lock guard" block).

A plan session that is about to mutate the LIVE mailbox takes this lock first
and releases it when done, whatever the outcome — that is what makes the
scheduled night's guard fire on something real instead of a file nobody ever
writes (2026-09-04 review finding: no acquirer existed anywhere in the repo).

SAME file, SAME format, SAME env overrides the guard reads — this is not a
second lock, it is the other end of the one lock:
    PLAN_LOCK_FILE = $COS_PLAN_LOCK_FILE or ~/.brain/locks/cos-plan-lock
    PLAN_LOCK_PID=<pid>
    PLAN_LOCK_ACQUIRED_EPOCH=<unix seconds>

Usage — a plan session wraps its live-mailbox mutation with this:
    python3 tools/cos_plan_lock.py acquire      # refuses if already held+fresh
    ... do the mutation ...
    python3 tools/cos_plan_lock.py release      # ALWAYS run, whatever the outcome

Exit 0 = ok. `acquire` exits 1 if the lock is already held and not past the
same stale bar the guard uses (`COS_PLAN_LOCK_STALE_SECONDS`, default 6h) —
pass --force to steal it after confirming the pid it names is not real work.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_STALE_SECONDS = 21600  # 6h — matches cos_nightly.sh's default


def lock_path() -> Path:
    override = os.environ.get("COS_PLAN_LOCK_FILE")
    return Path(override) if override else Path.home() / ".brain" / "locks" / "cos-plan-lock"


def _stale_seconds() -> int:
    return int(os.environ.get("COS_PLAN_LOCK_STALE_SECONDS", DEFAULT_STALE_SECONDS))


def _read_lock(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value
    return fields


def acquire(path: Path, pid: int, force: bool) -> int:
    if path.is_file() and not force:
        fields = _read_lock(path)
        try:
            age = int(time.time()) - int(fields["PLAN_LOCK_ACQUIRED_EPOCH"])
        except (KeyError, ValueError):
            age = None
        if age is None or age < _stale_seconds():
            held_pid = fields.get("PLAN_LOCK_PID", "unknown")
            age_str = f"{age}s" if age is not None else "unreadable"
            print(
                f"cos_plan_lock: REFUSING acquire — {path} already held "
                f"(pid={held_pid}, age={age_str}). Use --force to steal it.",
                file=sys.stderr,
            )
            return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".cos-plan-lock.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(f"PLAN_LOCK_PID={pid}\nPLAN_LOCK_ACQUIRED_EPOCH={int(time.time())}\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    print(f"cos_plan_lock: acquired {path} (pid={pid})")
    return 0


def release(path: Path) -> int:
    # Unconditional: "release after the last [mutation], whatever the
    # outcome" (the plan's own instruction to every mailbox-mutating
    # session) — a release is not a permission check, it is cleanup.
    path.unlink(missing_ok=True)
    print(f"cos_plan_lock: released {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    acq = sub.add_parser("acquire")
    acq.add_argument(
        "--pid", type=int, default=os.getppid(),
        help="pid to record as the holder (default: this process's parent, "
             "i.e. the calling shell)",
    )
    acq.add_argument("--force", action="store_true",
                      help="steal the lock even if held and not stale")
    sub.add_parser("release")
    args = p.parse_args(argv)

    path = lock_path()
    if args.cmd == "acquire":
        return acquire(path, args.pid, args.force)
    return release(path)


if __name__ == "__main__":
    sys.exit(main())
