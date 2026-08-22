#!/usr/bin/env python3
"""Severity-scoped wrapper around plan-execute's `llm_review_gate.py`.

WHY THIS EXISTS (2026-08-19, s03 of the COS inbox-zero backfill plan).

The bundled gate blocks on ANY finding: its pass condition is a parseable and
EMPTY findings array. On a small diff that is the right bar. On a large one it
is not reachable, and we measured that: eight consecutive review rounds over
the ingestion-bridge diff returned 6, 4, 4, 3, 3, 3 findings and stopped
falling. Each round fixed every finding it was given. The bar never arrived.

Two documented properties of LLM review explain it, and both are why this
wrapper scopes rather than loosens:

  * An LLM reviewer grades severity ON A CURVE relative to whatever fills its
    context, so "critical" is locally accurate within one run and NOT portable
    between runs. Wiring a hard gate to the raw label stalls good work behind a
    prominent-looking nit.
  * An LLM reviewer remembers nothing between runs, so a finding it raised last
    round comes back this round with equal confidence. Cost per finding stays
    flat while value falls. The fix is not memory — it is promoting a recurring
    finding into a DETERMINISTIC check that can catch it without a reviewer.

So: the reviewer still flags anything it likes, and every finding is printed.
Only a HIGH or CRITICAL finding blocks. Medium and low ride out as advisory
text for a human to read. The deterministic tests remain the real gate; this
one is the second opinion, and a second opinion should not have a veto over
nits.

WHAT IS DELIBERATELY UNCHANGED — the fail-closed half:

  * INDETERMINATE (exit 2) stays a FAILURE. A headless reviewer can exit 0
    having emitted nothing, and a gate that cannot tell "no findings" from "no
    output" is the silent-green bug the bundled gate was written to refuse.
    Every unparseable, empty or errored run still fails here.
  * A findings block this wrapper cannot parse is INDETERMINATE, never a pass.
    Downgrading an unreadable verdict to green would reintroduce exactly the
    failure this file's docstring is about.

Exit codes match the bundled gate: 0 pass, 1 blocking finding(s), 2
indeterminate.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

PASS, FINDINGS, INDETERMINATE = 0, 1, 2

#: Severities that BLOCK. Everything else is printed and allowed through.
BLOCKING = {"high", "critical"}

#: The bundled gate, resolved the same way the registry entry resolves it.
_BUNDLED = Path.home() / ".claude" / "skills" / "plan-execute" / "scripts" / "llm_review_gate.py"

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.S)


def _bundled_path() -> Path | None:
    if _BUNDLED.is_file():
        return _BUNDLED
    local = Path("skills/plan-execute/scripts/llm_review_gate.py")
    return local if local.is_file() else None


def _findings(text: str) -> list[dict] | None:
    """LAST fenced JSON array in the output, or None when none parses."""
    out = None
    for m in _JSON_FENCE.finditer(text):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            out = parsed
    return out


def _run_in_own_process_group(argv: list[str]) -> subprocess.CompletedProcess:
    """Run the bundled gate so that killing THIS process kills it too.

    MEASURED CAUSE (2026-08-21). Every layer here enforces its timeout by
    killing its DIRECT child only. plan-execute's argv runner kills the
    `/bin/sh -c` wrapper; this module was that wrapper's child, so the gate
    below it was reparented to init and kept running — with a live model
    connection and nobody reading its output. One such orphan was observed at
    34 minutes (`ppid=1`) after a gate reported `timeout after 1800s`, and
    three of this session's five gate runs timed out while an orphan from the
    previous one was still alive. Raising the timeout makes it WORSE: a longer
    window breeds a longer-lived orphan.

    So the child gets its own process group (`start_new_session=True`) and the
    signals that kill this process kill that whole group first. The gate's own
    `subprocess.run(..., timeout=...)` at `llm_review_gate.py:305` has the same
    single-child defect one layer further down; that file is a DEPLOY TARGET
    (`~/.claude`) and must be fixed at its source repo, not here.
    """
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)

    def _reap(signum, _frame):
        _kill_group(proc)
        # Re-raise as the default action so the caller sees a normal signal death.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, _reap)
        except (ValueError, OSError):      # not the main thread, or unsupported
            pass
    try:
        stdout, stderr = proc.communicate()
    except BaseException:                  # noqa: BLE001 — including KeyboardInterrupt
        _kill_group(proc)
        raise
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group; never raise."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    gate = _bundled_path()
    if gate is None:
        print("llm-review-scoped: INDETERMINATE — cannot locate llm_review_gate.py",
              file=sys.stderr)
        return INDETERMINATE

    proc = _run_in_own_process_group([sys.executable, str(gate), *argv])
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    print(combined.rstrip())

    # The bundled gate could not say what it reviewed, or reviewed nothing
    # readable. That is a failure here too — never scoped away.
    if proc.returncode == INDETERMINATE:
        print("\n[scoped] INDETERMINATE from the underlying gate — failing closed.",
              file=sys.stderr)
        return INDETERMINATE

    if proc.returncode == PASS:
        print("\n[scoped] underlying gate returned an empty findings block — PASS.")
        return PASS

    found = _findings(combined)
    if found is None:
        print("\n[scoped] INDETERMINATE — the underlying gate reported findings but "
              "no findings array parsed, so the severities cannot be read. An "
              "unreadable verdict is never a pass.", file=sys.stderr)
        return INDETERMINATE

    blocking = [f for f in found
                if str(f.get("severity", "")).strip().lower() in BLOCKING]
    advisory = [f for f in found if f not in blocking]

    for f in advisory:
        print(f"[scoped] ADVISORY ({f.get('severity')}): "
              f"{f.get('file')}:{f.get('line')} — {f.get('summary')}")

    if not blocking:
        print(f"\n[scoped] PASS — {len(advisory)} advisory finding(s), "
              f"0 blocking (blocking severities: {sorted(BLOCKING)}). "
              f"Advisory findings are real feedback, not noise to discard: read "
              f"them, and promote any that recurs into a deterministic test.")
        return PASS

    print(f"\n[scoped] FAILED — {len(blocking)} blocking finding(s) "
          f"({sorted(BLOCKING)}), {len(advisory)} advisory.", file=sys.stderr)
    for f in blocking:
        print(f"  BLOCKING ({f.get('severity')}): {f.get('file')}:{f.get('line')} "
              f"— {f.get('summary')}", file=sys.stderr)
    return FINDINGS


if __name__ == "__main__":
    sys.exit(main())
