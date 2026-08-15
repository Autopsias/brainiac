#!/usr/bin/env python3
"""Check cyclomatic complexity against a ratcheting baseline.

Runs ruff's C901 rule and compares each function against
``.complexity-exceptions``. A function fails when it is absent from the
baseline, or when it scored WORSE than the baseline records. Improving a
function never fails; the baseline only ever shrinks.

Companion to check_file_sizes.py / check_function_lengths.py, which own the
same ratchet for file size and function length. Shares their exclude list
(pyproject.toml ``[tool.claude-quality] exclude``) and their exit codes.

Exit codes:
  0 - no blocking violations
  1 - blocking violations found
  2 - ruff missing or unusable (the check could not run)

Usage:
  python3 tools/check_complexity.py [--project DIR] [--generate-baseline]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - py<3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

MAX_COMPLEXITY = 12
BASELINE_NAME = ".complexity-exceptions"

# Dirs ruff should never walk, mirroring check_file_sizes.SKIP_DIRS plus the
# worktree tree (a sibling session's checkout, not this commit's source).
SKIP_DIRS = [
    ".venv", "venv", "node_modules", "dist", "build", ".tox", "opensrc",
    ".claude/worktrees",
]

# `parse_ingest_rules` is too complex (13 > 12)
_MESSAGE = re.compile(r"^`(?P<func>[^`]+)` is too complex \((?P<score>\d+) > \d+\)$")


def load_excludes(project: Path) -> list[str]:
    """The [tool.claude-quality] exclude list, so all three gates share a scope."""
    pyproject = project / "pyproject.toml"
    if not pyproject.exists() or tomllib is None:
        return list(SKIP_DIRS)
    try:
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return list(SKIP_DIRS)
    configured = data.get("tool", {}).get("claude-quality", {}).get("exclude", [])
    return list(SKIP_DIRS) + [str(item) for item in configured]


def scan(project: Path) -> dict[str, int]:
    """Map ``relative/path.py::function`` -> complexity, for every C901 hit."""
    if shutil.which("ruff") is None:
        print("ERROR: ruff not found; cannot check complexity.", file=sys.stderr)
        raise SystemExit(2)

    result = subprocess.run(
        [
            "ruff", "check", "--select", "C901",
            "--config", f"lint.mccabe.max-complexity={MAX_COMPLEXITY}",
            "--exclude", ",".join(load_excludes(project)),
            "--output-format", "json", ".",
        ],
        cwd=project, capture_output=True, text=True,
    )
    # ruff exits 1 when it finds violations, which is the normal path here.
    if result.returncode not in (0, 1) or not result.stdout.strip():
        print(f"ERROR: ruff failed ({result.returncode}): {result.stderr.strip()}",
              file=sys.stderr)
        raise SystemExit(2)

    found: dict[str, int] = {}
    for item in json.loads(result.stdout):
        match = _MESSAGE.match(item["message"])
        if match is None:
            # ponytail: an unparseable message means ruff changed its wording.
            # Fail loud rather than silently under-reporting.
            print(f"ERROR: unrecognised ruff message: {item['message']}", file=sys.stderr)
            raise SystemExit(2)
        rel = Path(item["filename"]).resolve().relative_to(project.resolve())
        found[f"{rel}::{match['func']}"] = int(match["score"])
    return found


def load_baseline(project: Path) -> dict[str, int]:
    path = project / BASELINE_NAME
    if not path.exists():
        return {}
    with open(path) as fh:
        data = json.load(fh)
    return {
        f"{e['file']}::{e['function']}": e["complexity"]
        for e in data.get("exceptions", [])
    }


def write_baseline(project: Path, found: dict[str, int]) -> None:
    exceptions = [
        {"file": key.split("::", 1)[0], "function": key.split("::", 1)[1],
         "complexity": score}
        for key, score in sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    path = project / BASELINE_NAME
    with open(path, "w") as fh:
        json.dump({"exceptions": exceptions}, fh, indent=2)
        fh.write("\n")
    print(f"Wrote {len(exceptions)} exception(s) to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check cyclomatic complexity")
    parser.add_argument("--project", default=".", help="Project root directory")
    parser.add_argument("--generate-baseline", action="store_true",
                        help="Write current violations as the exceptions baseline")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    found = scan(project)

    if args.generate_baseline:
        write_baseline(project, found)
        return 0

    baseline = load_baseline(project)
    blocking = sorted(
        (key, score) for key, score in found.items()
        if score > baseline.get(key, MAX_COMPLEXITY)
    )

    if blocking:
        print(f"\n=== Complexity Violations ({len(blocking)} blocking) ===")
        for key, score in blocking:
            path, func = key.split("::", 1)
            was = baseline.get(key)
            limit = f"baseline={was}" if was is not None else f"LIMIT={MAX_COMPLEXITY}"
            print(f"  {path}: {func}() complexity {score} [{limit}] BLOCKING")

    stale = sorted(set(baseline) - set(found))
    if stale:
        print(f"\n{len(stale)} baselined function(s) no longer violate "
              f"(run --generate-baseline to shrink the baseline).")

    print(f"\nSummary: {len(blocking)} BLOCKING violation(s), "
          f"{len(baseline)} baselined, limit={MAX_COMPLEXITY}")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
