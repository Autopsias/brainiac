#!/usr/bin/env python3
"""Read back a LOADED launchd job and assert it actually has a schedule.

NEVER read the plist FILE — it is what a tool most recently wrote and proves
nothing about what launchd loaded. This reads `launchctl print
gui/<uid>/<label>`, the running truth, and asserts two properties any
scheduled job needs:

  1. A StartCalendarInterval trigger is loaded and fires at --hour:--minute.
  2. The job's ProgramArguments does not point into a plan worktree
     (`.plan-worktrees/…`) — a worktree that disappears the moment its plan
     merges, silencing the job with no error anywhere.

PARAMETERISED on --label/--hour/--minute, not hard-wired to cos-nightly: a
second scheduled job (a different label, a different hour, a different
program) needs the SAME two assertions, and a checker that only knows one
label could never verify it.

    python3 tools/cos_nightly_schedule_check.py --label com.brainiac.cos-nightly --hour 2
    python3 tools/cos_nightly_schedule_check.py --label com.brainiac.cos-nightly --hour 2 --input print.txt

Exit 0 = both properties hold. Exit 1 = loaded but one or both fail.
Exit 2 = the job is not loaded at all, or `launchctl print` could not run.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

CALENDAR_INTERVAL_RE = re.compile(
    r"stream = com\.apple\.launchd\.calendarinterval.*?descriptor = \{(?P<body>.*?)\}",
    re.DOTALL,
)
KV_RE = re.compile(r'"(\w+)"\s*=>\s*(\d+)')
ARGUMENTS_RE = re.compile(r"\n\s*arguments = \{(?P<body>.*?)\n\s*\}", re.DOTALL)
PROGRAM_RE = re.compile(r"\n\s*program = (?P<prog>\S+)")


@dataclass
class CheckResult:
    ok: bool
    trigger_ok: bool
    path_ok: bool
    intervals: list = field(default_factory=list)
    argv: list = field(default_factory=list)
    plan_worktree_hits: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def parse_calendar_intervals(text: str) -> list[dict]:
    """Every StartCalendarInterval trigger loaded, as {"Hour": n, ...} dicts."""
    out = []
    for m in CALENDAR_INTERVAL_RE.finditer(text):
        out.append({k: int(v) for k, v in KV_RE.findall(m.group("body"))})
    return out


def parse_argv(text: str) -> list[str]:
    m = ARGUMENTS_RE.search(text)
    if m:
        return [line.strip() for line in m.group("body").splitlines() if line.strip()]
    m = PROGRAM_RE.search(text)
    return [m.group("prog")] if m else []


def check(text: str, hour: int, minute: int = 0) -> CheckResult:
    intervals = parse_calendar_intervals(text)
    # An OMITTED field in StartCalendarInterval is a wildcard (fires every
    # value of that unit), not 0 — a descriptor carrying only "Hour" fires 60
    # times during that hour. So a match requires "Minute" to be PRESENT and
    # equal, never defaulted from an absent key (2026-09-04 review finding).
    trigger_ok = any(
        iv.get("Hour") == hour and "Minute" in iv and iv["Minute"] == minute
        for iv in intervals
    )
    argv = parse_argv(text)
    plan_worktree_hits = [a for a in argv if ".plan-worktrees" in a]
    path_ok = bool(argv) and not plan_worktree_hits

    reasons = []
    if not intervals:
        reasons.append("no StartCalendarInterval trigger is loaded — this job has no schedule")
    elif not trigger_ok:
        reasons.append(
            f"loaded trigger(s) {intervals} do not fire at Hour={hour} Minute={minute}")
    if not argv:
        reasons.append("could not find a ProgramArguments/program line in the loaded job")
    elif plan_worktree_hits:
        reasons.append(f"ProgramArguments points into a plan worktree: {plan_worktree_hits}")

    return CheckResult(
        ok=trigger_ok and path_ok, trigger_ok=trigger_ok, path_ok=path_ok,
        intervals=intervals, argv=argv, plan_worktree_hits=plan_worktree_hits,
        reasons=reasons,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", required=True, help="launchd job label, e.g. com.brainiac.cos-nightly")
    p.add_argument("--hour", type=int, required=True, help="expected StartCalendarInterval Hour")
    p.add_argument("--minute", type=int, default=0, help="expected StartCalendarInterval Minute (default 0)")
    p.add_argument("--uid", default=None, help="defaults to the current uid")
    p.add_argument("--input", help="read `launchctl print` output from this file instead of running it (tests, offline review)")
    args = p.parse_args(argv)

    if args.input:
        text = open(args.input, encoding="utf-8").read()
    else:
        uid = args.uid or str(os.getuid())
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{args.label}"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(
                f"cos_nightly_schedule_check: {args.label} is not loaded "
                f"(launchctl print exited {proc.returncode}): {proc.stderr.strip()}",
                file=sys.stderr,
            )
            return 2
        text = proc.stdout

    result = check(text, args.hour, args.minute)
    print(f"{args.label}: trigger={'OK' if result.trigger_ok else 'MISSING/WRONG'} "
          f"path={'OK' if result.path_ok else 'PLAN-WORKTREE'}")
    if result.intervals:
        print(f"  loaded trigger(s): {result.intervals}")
    if result.argv:
        print(f"  ProgramArguments: {result.argv}")
    for reason in result.reasons:
        print(f"  FAIL: {reason}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
