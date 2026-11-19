#!/usr/bin/env python3
"""Measure the size cliff: files and functions per size band, numbered _2 modules, hottest files.

Counts exactly as the quality gate does (imports its own helpers), so a
counting difference never shows up as a fake delta. Prints JSON; --compare
prints the delta against an earlier run.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_file_sizes import count_lines, is_test_file, iter_python_files, load_config  # noqa: E402
from check_function_lengths import functions_in_source  # noqa: E402

FILE_BANDS = [(lo, lo + 19) for lo in range(380, 600, 20)] + [(600, 799)]
FUNC_BANDS = [(lo, lo + 4) for lo in range(80, 120, 5)]
NUMBERED = re.compile(r"_[2-9]$")


def _band(n, bands, top):
    for lo, hi in bands:
        if lo <= n <= hi:
            return f"{lo}-{hi}"
    return top if n >= bands[-1][1] + 1 else None


def hottest(repo, since, files, n=15):
    out = subprocess.run(
        ["git", "-C", str(repo), "log", f"--since={since}", "--name-only", "--pretty=format:"],
        capture_output=True, text=True, check=True).stdout
    counts = Counter(l for l in out.splitlines() if l in files)
    return [{"file": f, "commits": c, "lines": files[f]} for f, c in counts.most_common(n)]


def measure(repo, since):
    repo = Path(repo).resolve()
    exclude = load_config(repo)["exclude"]
    files, fbands, funcs, numbered = {}, Counter(), Counter(), []
    for p in iter_python_files(repo, exclude):
        rel = p.relative_to(repo).as_posix()
        if is_test_file(rel):
            continue
        n = count_lines(p)
        files[rel] = n
        if n == 500:
            fbands["exactly-500"] += 1
        if (b := _band(n, FILE_BANDS, "800+" if n >= 800 else None)) or n >= 800:
            fbands[b or "800+"] += 1
        if NUMBERED.search(p.stem):
            numbered.append(rel)
        for _, _, length in functions_in_source(p.read_text(encoding="utf-8", errors="replace"), rel):
            if (fb := _band(length, FUNC_BANDS, "120+")):
                funcs[fb] += 1
    return {
        "since": since,
        "file_bands": dict(sorted(fbands.items())),
        "function_bands": dict(sorted(funcs.items())),
        "numbered_modules": sorted(numbered),
        "numbered_module_count": len(numbered),
        "hottest_files": hottest(repo, since, files),
    }


def delta(new, old):
    d = {}
    for k in ("file_bands", "function_bands"):
        d[k] = {b: new[k].get(b, 0) - old[k].get(b, 0) for b in sorted(set(new[k]) | set(old[k]))}
    d["numbered_module_count"] = new["numbered_module_count"] - old["numbered_module_count"]
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--since", required=True, help="git date, e.g. 2026-08-15")
    ap.add_argument("--compare", help="baseline JSON; print the delta instead")
    a = ap.parse_args()
    res = measure(a.repo, a.since)
    if a.compare:
        res = delta(res, json.loads(Path(a.compare).read_text()))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
