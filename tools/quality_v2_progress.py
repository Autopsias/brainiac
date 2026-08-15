#!/usr/bin/env python3
"""Report which baselined quality keys this tree has RETIRED, and fail if none.

The ratchet's three checkers are NO-REGRESSION gates: an untouched tree passes
them maximally. That makes them useless as a completion gate — a refactor
session that does nothing satisfies every one. This script is the positive
half: it re-measures the tree against the three baselines and reports the keys
that no longer violate. Nothing here reads a session-written artifact, so there
is nothing for a session to forge.

Exit codes:
  0 - at least `--min` keys retired (or --report, which never fails)
  2 - fewer than `--min` retired (default 1, i.e. "you did nothing")
  3 - a baseline file is missing or unreadable

Usage:
  python3 tools/quality_v2_progress.py                  # gate: >=1 retired
  python3 tools/quality_v2_progress.py --min 5
  python3 tools/quality_v2_progress.py --report         # never fails
  python3 tools/quality_v2_progress.py --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_complexity as cx  # noqa: E402
import check_file_sizes as fs  # noqa: E402
import check_function_lengths as fl  # noqa: E402


def _baseline_at(project: Path, name: str, ref: str) -> dict | None:
    """The baseline file as of `ref`, or None if git cannot produce it.

    ponytail: read the COMMITTED baseline, not the working copy. A session that
    does the work then runs `--generate-baseline` deletes the very keys that
    prove it did the work, so comparing against the working file would fail
    exactly the sessions that succeeded. `ref` defaults to HEAD, which at gate
    time is the state before this session's own commit.
    """
    if not ref:
        return None
    try:
        out = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=project,
                             capture_output=True, text=True)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def retired(project: Path, ref: str = "HEAD") -> dict[str, list[str]]:
    """Baselined keys that no longer violate, per baseline.

    A key counts as retired when it is gone from the tree entirely (the file or
    function was moved/removed) or when it now measures at or under the limit.
    Both are real progress: this plan's whole method is moving code OUT.
    """
    out: dict[str, list[str]] = {}

    cfg = fs.load_config(project)
    sizes = {
        str(p.relative_to(project)): fs.count_lines(p)
        for p in fs.iter_python_files(project, cfg.get("exclude"))
    }
    fs_base = _baseline_at(project, ".file-size-exceptions", ref)
    fs_keys = ({e["file"]: e["loc"] for e in fs_base.get("exceptions", [])}
               if fs_base is not None else fs.load_exceptions(project))
    out["file-size"] = sorted(
        f for f in fs_keys
        if f not in sizes
        or sizes[f] <= (cfg["test_limit"] if fs.is_test_file(f) else cfg["limit"])
    )

    fl_base = _baseline_at(project, ".function-length-exceptions", ref)
    fl_keys = ({fl.exception_key(e["file"], e["name"]) for e in fl_base.get("exceptions", {}).values()}
               if fl_base is not None else set(fl.load_exceptions(project)))
    cfg2 = fl.load_config(project)
    lengths: dict[str, int] = {}
    for p in fl.iter_python_files(project, cfg2.get("exclude")):
        rel = str(p.relative_to(project))
        for _lineno, name, length in fl.get_functions(p):
            lengths[fl.exception_key(rel, name)] = length
    out["function-length"] = sorted(
        k for k in fl_keys
        if k not in lengths or lengths[k] <= cfg2["limit"]
    )

    cx_base = _baseline_at(project, ".complexity-exceptions", ref)
    cx_keys = ({f"{e['file']}::{e['function']}" for e in cx_base.get("exceptions", [])}
               if cx_base is not None else set(cx.load_baseline(project)))
    found = cx.scan(project)
    out["complexity"] = sorted(cx_keys - set(found))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Report retired quality-baseline keys")
    ap.add_argument("--project", default=".")
    ap.add_argument("--min", type=int, default=1,
                    help="fail below this many retired keys (default 1)")
    ap.add_argument("--report", action="store_true", help="never fail; just report")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline-ref", default="HEAD",
                    help="git ref whose baselines are the comparison basis "
                         "(default HEAD: the state before this session commits). "
                         "Pass '' to compare against the working copy.")
    args = ap.parse_args()

    project = Path(args.project).resolve()
    try:
        groups = retired(project, args.baseline_ref)
    except FileNotFoundError as exc:
        print(f"ERROR: baseline missing: {exc}", file=sys.stderr)
        return 3

    total = sum(len(v) for v in groups.values())
    if args.json:
        print(json.dumps({"retired": groups, "total": total}, indent=2))
    else:
        for name, keys in groups.items():
            print(f"\n=== {name}: {len(keys)} retired ===")
            for k in keys:
                print(f"  {k} RETIRED")
        print(f"\nTOTAL {total}")

    if args.report:
        return 0
    if total < args.min:
        print(f"\nREFUSED: {total} baselined key(s) retired, need at least "
              f"{args.min}. A no-regression gate passes on an untouched tree; "
              f"this is the gate that does not.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
