"""Machine-wide cap on concurrent model work (load + inference).

WHY. One `brain search` peaks at ~4 GB for a few seconds: loading the fp32
reranker alone measures 3.0 GB, a 15-pair rerank adds ~1 GB (measured
2026-10-18, M4 Pro). On 2026-10-18 three subagents ran ten searches at once on
a 24 GB Mac; macOS recorded ten Python processes at 4.1-6.8 GB each and the
machine froze until a forced reset. Jetsam stopped only small system daemons,
never the searches. Nothing in brain bounded how many ran together.

WHAT. `model_slot()` waits for one of `$BRAIN_MODEL_SLOTS` (default 2) slot
files under `$BRAIN_MODEL_SLOT_DIR` (default ~/.cache/brain-model-slots) and
holds an exclusive `flock` on it for the duration of the block. The kernel drops
the lock when the process dies, so a crash never leaks a slot.

RE-ENTRANT PER PROCESS. A search holds a slot and its reranker worker thread
runs inside it; that work must not wait for a second slot. So the first
acquire in a process takes a slot, and nested or concurrent acquires in the
same process ride on it until the depth returns to zero. The cap is therefore
"processes doing model work", which is what costs memory.

FAIL OPEN. If the slot directory cannot be created or opened (read-only HOME,
Windows without fcntl), the work runs unguarded: a guard must never stop a
search from working. ponytail: no Windows lock — add msvcrt when a Windows
host runs parallel searches.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

_POLL_SECONDS = 0.2
_gate = threading.Lock()  # a second thread waits here while the first acquires
_state = threading.Lock()
_depth = 0
_fd: int | None = None


def slot_count() -> int:
    raw = os.environ.get("BRAIN_MODEL_SLOTS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 2


def slot_dir() -> Path:
    raw = os.environ.get("BRAIN_MODEL_SLOT_DIR")
    return Path(raw) if raw else Path.home() / ".cache" / "brain-model-slots"


def _try_take(paths: list[Path]) -> int | None:
    for path in paths:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        return fd
    return None


def _acquire() -> int | None:
    if fcntl is None:
        return None
    try:
        root = slot_dir()
        root.mkdir(parents=True, exist_ok=True)
        paths = [root / f"slot-{i}.lock" for i in range(slot_count())]
        fd = _try_take(paths)
        if fd is not None:
            return fd
        print(f"brain: waiting for a model slot ({len(paths)} in use on this "
              "machine; $BRAIN_MODEL_SLOTS sets the cap)", file=sys.stderr, flush=True)
        while fd is None:
            time.sleep(_POLL_SECONDS)
            fd = _try_take(paths)
        return fd
    except OSError as exc:
        print(f"brain: model slot unavailable ({exc}); running unguarded",
              file=sys.stderr, flush=True)
        return None


@contextlib.contextmanager
def model_slot() -> Iterator[None]:
    global _depth, _fd
    with _gate:
        with _state:
            first = _depth == 0
        fd = _acquire() if first else None  # a KeyboardInterrupt here holds nothing
        with _state:
            _depth += 1
            if first:
                _fd = fd
    try:
        yield
    finally:
        with _state:
            _depth -= 1
            if _depth == 0 and _fd is not None:
                os.close(_fd)  # closing the only fd on the file drops the flock
                _fd = None
