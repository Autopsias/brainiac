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
CALL_TIMEOUT_S = 8.0      # a STALL cutoff, not a working budget (~200ms-1s median)
CALL_RETRIES = 1          # so one call's worst case is 16s, not 8


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
                last = "timeout"
                continue
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
        doc = self._run(["search", query, "--json", "--max-tier", "MNPI",
                         "-k", str(k), "--no-rerank"])
        return list((doc or {}).get("results") or [])

    def get(self, note_id: str) -> dict[str, Any]:
        doc = self._run(["get", note_id, "--json", "--max-tier", "MNPI"])
        return doc if isinstance(doc, dict) and not doc.get("error") else {}

    def dossier(self, query: str, k: int) -> list[dict[str, Any]]:
        doc = self._run(["dossier", query, "--json", "--max-tier", "MNPI",
                         "-k", str(k)])
        return list((doc or {}).get("decisions") or [])


class LookupFailed(Exception):
    """One thread's lookup did not answer. That thread goes uncovered; the rest
    of the run is unaffected (D5)."""
