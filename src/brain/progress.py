"""Progress narration for long-running verbs (OB-01/OB-02).

Stdlib-only, stderr-only -- no logging framework, no tqdm (matches the rest
of src/brain/). ``cli.py``'s ``_emit`` owns stdout for the final JSON/text
payload; every progress byte, in BOTH text mode and ``--json`` mode, goes to
stderr instead. Several consumers parse a subprocess's captured stdout+stderr
for unrelated reasons (doctor.py's version regex, update.py/connect.py's
install-state detection) -- a subprocess-captured stderr is never a TTY, so
gating on ``sys.stderr.isatty()`` means those consumers are unaffected by
construction; nothing needs updating on their side.
"""

from __future__ import annotations

import json
import os
import sys
import time


def progress_enabled() -> bool:
    """Whether progress lines should be emitted right now.

    Gated on ``sys.stderr.isatty()``. ``BRAIN_PROGRESS=1`` (or the CLI's
    ``--progress`` flag, which just sets that env var) forces it on even when
    stderr isn't a TTY -- e.g. a human tailing a redirected log file.
    """
    if os.environ.get("BRAIN_PROGRESS") == "1":
        return True
    return sys.stderr.isatty()


class ProgressReporter:
    """Emits one line per batch to stderr: done/total, percent, rate, ETA.

    Rate-limited to ~1/line/sec so a tight per-note loop doesn't spam. A
    no-op when progress isn't enabled (see :func:`progress_enabled`) --
    callers can call ``update()`` unconditionally without checking first.
    """

    def __init__(self, verb: str, total: int, *, json_mode: bool = False):
        self.verb = verb
        self.total = total
        self.json_mode = json_mode
        self.enabled = progress_enabled()
        self._start = time.monotonic()
        self._last_emit = 0.0

    def update(self, done: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        is_final = done >= self.total > 0
        if not is_final and (now - self._last_emit) < 1.0:
            return
        self._last_emit = now
        elapsed = max(now - self._start, 1e-9)
        rate = done / elapsed
        eta_s = max(self.total - done, 0) / rate if rate > 0 else 0.0
        if self.json_mode:
            event = {
                "event": "progress",
                "verb": self.verb,
                "done": done,
                "total": self.total,
                "rate": round(rate, 2),
                "eta_s": round(eta_s, 1),
            }
            print(json.dumps(event), file=sys.stderr, flush=True)
        else:
            pct = int(done / self.total * 100) if self.total else 100
            print(
                f"{self.verb} {done}/{self.total} ({pct}%) ~{rate:.0f}/s "
                f"eta {int(eta_s // 60)}m",
                file=sys.stderr, flush=True,
            )


def progress_note(msg: str, *, json_mode: bool = False, verb: str = "") -> None:
    """One-off begin/end line (e.g. warmup's model download), same gating."""
    if not progress_enabled():
        return
    if json_mode:
        print(json.dumps({"event": "progress", "verb": verb, "note": msg}),
              file=sys.stderr, flush=True)
    else:
        print(msg, file=sys.stderr, flush=True)
