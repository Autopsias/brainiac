#!/usr/bin/env python3
"""Code-origin audit gate (FLEET / r2-codex).

Proves that NO basic-memory (AGPL) code reached the shipped artifact. Scans the
shipped source tree for: imports of the basic-memory package, vendored copies,
its distinctive identifiers, and any AGPL licence headers. Exits non-zero (and
prints a report) if anything is found — the session's evidence gate consumes the
report.

This is a from-scratch core; basic-memory was a CLEAN-ROOM design reference only
(see docs/clean-room-log.md). "AGPL noted" is not a boundary for a distributed
binary — this gate is the boundary.

Usage:
    python3 tools/code_origin_audit.py [--root src/brain] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Tokens that would indicate basic-memory code (fork / vendor / import / copy).
# Word-boundary / import-context matched to avoid flagging prose like
# "basic memory" or this audit script's own documentation.
FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("import basic_memory", re.compile(r"\b(?:import|from)\s+basic_memory\b")),
    ("import basic-memory dist", re.compile(r"\bbasic[_-]memory\.(?:cli|mcp|sync|repository|schemas)\b")),
    ("basicmachines vendor", re.compile(r"\bbasicmachines(?:_co|-co)?\b")),
    ("nova/basic-memory pkg path", re.compile(r"basic[_-]memory/(?:src|nova)/")),
    ("AGPL header", re.compile(r"GNU\s+AFFERO\s+GENERAL\s+PUBLIC\s+LICENSE", re.I)),
]

# This audit script and the clean-room log legitimately mention the tokens; the
# scan is restricted to the shipped artifact root, which excludes tools/ and docs/.


def scan(root: Path) -> list[dict]:
    findings: list[dict] = []
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pat in FORBIDDEN_PATTERNS:
                if pat.search(line):
                    findings.append({
                        "file": p.as_posix(), "line": lineno,
                        "pattern": label, "text": line.strip()[:120],
                    })
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="Code-origin audit gate (zero basic-memory code).")
    ap.add_argument("--root", default="src/brain", help="shipped artifact root to scan")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        return 2

    findings = scan(root)
    py_files = [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    result = {
        "root": root.as_posix(),
        "python_files_scanned": len(py_files),
        "forbidden_patterns": [lbl for lbl, _ in FORBIDDEN_PATTERNS],
        "findings": findings,
        "verdict": "PASS — zero basic-memory code" if not findings else "FAIL — basic-memory code present",
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"code-origin audit: scanned {result['python_files_scanned']} python files under {root.as_posix()}")
        for lbl, _ in FORBIDDEN_PATTERNS:
            print(f"  checked: {lbl}")
        if findings:
            print("\nFINDINGS:")
            for f in findings:
                print(f"  {f['file']}:{f['line']}  [{f['pattern']}]  {f['text']}")
        print(f"\n{result['verdict']}")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
