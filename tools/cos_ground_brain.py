"""The bounded `brain` caller of `cos_ground` — one place, one timeout, one retry (batch-2 drain).

Moved verbatim out of `cos_ground`; `brain_cmd`, `Brain` and `LookupFailed` are
re-imported by the parent, so the parent module's callers and the exception's
identity are unchanged.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# --- D3, the caller's own two budgets. The run-level allocations (workers,
# deadline) stay in `cos_ground`; these two belong to the caller itself. ------
# Each call is a FRESH `python -m brain.cli search` subprocess, and `search`
# always embeds the query, so every call pays a COLD bge-m3 model load — 8-12s
# measured on this host (2026-08-30), not the ~200ms-1s a warm resident embedder
# would give. The old 8.0s cutoff was below that floor, so EVERY sender lookup
# timed out and run212 judged 246 threads blind (grounding covered 0). 15s fits
# a cold subprocess with ~2x margin; the run-level DEADLINE_S still caps total
# time, so a generous per-call cutoff costs coverage-breadth, never wall-clock.
CALL_TIMEOUT_S = 60.0     # a STALL cutoff sized for a cold-subprocess embed load
#                           UNDER CONTENTION. 15 -> 30 (2026-08-30) -> 60
#                           (2026-09-03). The 2026-08-30 note already had the
#                           argument right — "a retry re-pays the full cold load
#                           from a fresh subprocess, so one 30s allowance beats
#                           two 15s tries" — and then kept the retry, so the
#                           call's 60s worst case was still spent as two 30s
#                           tries. On run 249 that lost 24 of 36 threads, every
#                           one of them `timeout`, while the run-level 720s
#                           deadline finished with 467s unused. One 60s
#                           allowance covers the same worst case and a call
#                           needing 35s now RETURNS instead of failing twice.
CALL_RETRIES = 1          # for a call that failed FAST, never one that stalled


def brain_cmd() -> list[str]:
    """How to invoke the engine. `$COS_BRAIN_CMD` overrides, which is what lets
    the offline tests drive a STUB instead of the real vault."""
    override = os.environ.get("COS_BRAIN_CMD")
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", "brain.cli"]


class Brain:
    """A bounded, counted `brain` caller. Never `--role vm` (D6)."""

    def __init__(self, vault: Path, *, timeout: float = CALL_TIMEOUT_S,
                 retries: int = CALL_RETRIES) -> None:
        self.vault = vault
        self.timeout = timeout
        self.retries = retries
        self.calls = 0
        self._lock = threading.Lock()

    def _run(self, args: list[str]) -> Any:
        argv = brain_cmd() + ["--vault", str(self.vault), *args]
        # D6, asserted rather than asserted-in-prose: the fetcher never hands a
        # role to the engine, so it can never hand it the VM one.
        assert "--role" not in argv, "the fetcher never passes --role (D6)"
        last = ""
        for _attempt in range(self.retries + 1):
            with self._lock:
                self.calls += 1
            try:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      timeout=self.timeout,
                                      env=dict(os.environ, BRAIN_ROLE="host"))
            except subprocess.TimeoutExpired:
                # A STALL IS NOT RETRIED. The budget above is the whole
                # allowance, spent on one attempt: a second subprocess re-pays
                # the same cold bge-m3 load that caused the stall, so retrying
                # a timeout doubles the wall clock and changes nothing. Every
                # OTHER failure below is fast and genuinely worth one more try.
                last = "timeout"
                break
            except OSError as exc:
                last = f"could not run the engine: {exc}"
                continue
            if proc.returncode != 0:
                last = f"exit {proc.returncode}"
                continue
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError:
                last = "unparseable JSON"
                continue
        raise LookupFailed(last or "no answer")

    def search(self, query: str, k: int) -> list[dict[str, Any]]:
        # `--` before the query, and the query LAST: a subject or sender that
        # begins with "-" would otherwise be parsed as an option and take the
        # whole lookup down, leaving that thread uncovered for a reason that
        # has nothing to do with the vault.
        doc = self._run(["search", "--json", "--max-tier", "MNPI",
                         "-k", str(k), "--no-rerank", "--", query])
        return list((doc or {}).get("results") or [])

    def get(self, note_id: str) -> dict[str, Any]:
        doc = self._run(["get", "--json", "--max-tier", "MNPI", "--", note_id])
        return doc if isinstance(doc, dict) and not doc.get("error") else {}

    def dossier(self, query: str, k: int) -> list[dict[str, Any]]:
        doc = self._run(["dossier", "--json", "--max-tier", "MNPI",
                         "-k", str(k), "--", query])
        return list((doc or {}).get("decisions") or [])


class LookupFailed(Exception):
    """One thread's lookup did not answer. That thread goes uncovered; the rest
    of the run is unaffected (D5)."""
