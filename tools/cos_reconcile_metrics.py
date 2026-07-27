#!/usr/bin/env python3
"""Join the COS ledgers to `_cos_metrics.jsonl` and fail on an under-report.

WHY THIS EXISTS (measured 2026-07-25 / 2026-07-21). The metrics row is the
instrument every "COS delivered nothing" conclusion rests on, and it was
lying. `_cos_drafts_ledger_2026-07-25-run34.jsonl` records one
`draft-saved-verified` reply draft; every 2026-07-25 metrics row reads
`drafts_created: 0` — because run 34 mutated and appended no row of its own,
and E10 only ever demanded that SOME row exist for the date. Same shape, far
larger, on 2026-07-21: 181 verified archives and 26 verified marks in the
ledgers against `archived: 0, marked: 0` in the one row for that date.

This implements the SKILL.md v5.27 Disposition step 4¾(c) target-day join as a
runnable check, so "a ledgered verified draft and a zero counter must never
coexist silently" is enforced by something that can actually fail, not only by
prose a model is asked to honour.

    python3 tools/cos_reconcile_metrics.py <vault>/cos-ops
    python3 tools/cos_reconcile_metrics.py --json <vault>/cos-ops

Exit 0 = every date's reported counters cover its ledgers. Exit 1 = a
shortfall (ledgered > reported) on at least one date. Over-reporting is
listed too but is not, on its own, the defect this gate exists to catch.

ponytail: deliberately CONSERVATIVE — a row is only counted as executed when
its verification says so, so the ledger side under-counts before it over-counts
and the gate never cries wolf. Unknown row shapes are ignored, not guessed.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
COUNTERS = ("drafts_created", "marked", "archived")

# A re-verification of an EARLIER run's draft is not a creation (4¾(a)).
NOT_A_CREATION = {"existing-draft-visible", "draft-expired", "draft-discarded"}


def _rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _date_of(path: Path) -> str | None:
    m = DATE_RE.search(path.name)
    return m.group(1) if m else None


def counts_draft(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        status = r.get("status")
        if status in NOT_A_CREATION or r.get("operation") == "same-night-draft-verification":
            continue
        if status == "draft-saved-verified":
            n += 1
        elif r.get("action") == "draft-created" and r.get("verification"):
            n += 1
    return n


def counts_mark(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        status, ver = r.get("status"), r.get("verification")
        if status == "verified-marked":
            n += 1
        elif status == "verified" and r.get("operation") == "category-set-verification":
            n += 1
        elif isinstance(ver, str) and ver.startswith(("server-reread-confirmed", "PASS", "response-confirmed")):
            n += 1
    return n


def counts_archive(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        if r.get("operation") == "archive-summary":
            n += int(r.get("verified_archived") or 0)
            continue
        ver = r.get("verification")
        if ver in ("verified-archived", "response-confirmed") or isinstance(ver, dict):
            n += 1
    return n


LEDGERS = (
    ("_cos_drafts_ledger_*.jsonl", "drafts_created", counts_draft),
    ("_cos_chip_ledger_*.jsonl", "marked", counts_mark),
    ("_cos_archive_ledger_*.jsonl", "archived", counts_archive),
)


def reconcile(ops_dir: Path) -> dict:
    """{date: {counter: {"reported": int, "ledgered": int, "shortfall": int}}}."""
    reported: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNTERS, 0))
    metrics = ops_dir / "_cos_metrics.jsonl"
    if metrics.exists():
        for row in _rows(metrics):
            date = row.get("date")
            if not date:
                continue
            for c in COUNTERS:
                reported[date][c] += int(row.get(c) or 0)

    ledgered: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNTERS, 0))
    for pattern, counter, fn in LEDGERS:
        for path in sorted(ops_dir.glob(pattern)):
            date = _date_of(path)
            if date:
                ledgered[date][counter] += fn(_rows(path))

    out: dict[str, dict[str, dict[str, int]]] = {}
    for date in sorted(set(reported) | set(ledgered)):
        out[date] = {
            c: {
                "reported": reported[date][c],
                "ledgered": ledgered[date][c],
                "shortfall": max(0, ledgered[date][c] - reported[date][c]),
            }
            for c in COUNTERS
        }
    return out


def shortfalls(report: dict) -> list[tuple[str, str, int, int]]:
    return [
        (date, c, v["ledgered"], v["reported"])
        for date, per in report.items()
        for c, v in per.items()
        if v["shortfall"]
    ]


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: cos_reconcile_metrics.py [--json] <cos-ops dir>", file=sys.stderr)
        return 2
    ops = Path(args[0]).expanduser().resolve()
    if not ops.is_dir():
        print(f"FAIL: no cos-ops dir at {ops}", file=sys.stderr)
        return 2
    report = reconcile(ops)
    if "--json" in argv:
        print(json.dumps(report, indent=2))
    else:
        for date, per in report.items():
            bits = " ".join(
                f"{c}={v['ledgered']}/{v['reported']}" + ("!" if v["shortfall"] else "")
                for c, v in per.items()
            )
            print(f"{date}  (ledgered/reported)  {bits}")
    bad = shortfalls(report)
    if bad:
        print("\nUNDER-REPORTED — the ledgers record work no metrics row accounts for:")
        for date, c, led, rep in bad:
            print(f"  {date}: {c} ledgered {led}, reported {rep}")
        return 1
    print("\nOK: every date's counters cover its ledgers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
