"""Reconcile COS metric steps."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tools import cos_reconcile_metrics as _source


def _run_observation_guard(argv: list[str]) -> int | None:
    if "--observation-guard" not in argv:
        return None
    index = argv.index("--observation-guard")
    rest = [arg for arg in argv[index + 2:] if not arg.startswith("--")]
    if index + 1 >= len(argv) or not rest:
        print("usage: cos_reconcile_metrics.py --observation-guard "
              "<date>-run<N> <cos-ops dir>", file=sys.stderr)
        return 2
    ops = Path(rest[0]).expanduser().resolve()
    if not ops.is_dir():
        print(f"FAIL: no cos-ops dir at {ops}", file=sys.stderr)
        return 2
    result = _source.observation_guard(ops, argv[index + 1])
    if "--json" in argv:
        print(json.dumps(result, indent=2))
    else:
        for key, value in result.items():
            print(f"  {key}: {value}")
        print(f"\n{result['verdict']}: {result['reason']}")
    return 1 if result["verdict"] == "FAIL" else 0


def _append_metric(argv: list[str], append_path: Path) -> int:
    args = [
        arg for index, arg in enumerate(argv[1:], 1)
        if not arg.startswith("--")
        and index != argv.index("--append") + 1
    ]
    if not args:
        print("usage: cos_reconcile_metrics.py [--json] [--append <row.json>] "
              "<cos-ops dir>", file=sys.stderr)
        return 2
    ops = Path(args[0]).expanduser().resolve()
    if not ops.is_dir():
        print(f"FAIL: no cos-ops dir at {ops}", file=sys.stderr)
        return 2
    try:
        row = json.loads(append_path.read_text(encoding="utf-8"))
        result = _source.append_metric(ops, row)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"{result}: metrics row {(row['date'], str(row['run']))!r}")
    return 0


def _render_reconciliation(report: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        for date, per in report.items():
            bits = " ".join(
                f"{category}={values['ledgered']}/{values['reported']}"
                + ("!" if values["shortfall"] else "")
                for category, values in per.items()
            )
            print(f"{date}  (ledgered/reported)  {bits}")
    shortfalls = _source.shortfalls(report)
    if shortfalls:
        print("\nUNDER-REPORTED — the ledgers record work no metrics row accounts for:")
        for date, category, ledgered, reported in shortfalls:
            print(f"  {date}: {category} ledgered {ledgered}, reported {reported}")
        return 1
    print("\nOK: every date's counters cover its ledgers")
    return 0


def main(argv: list[str]) -> int:
    observation_result = _run_observation_guard(argv)
    if observation_result is not None:
        return observation_result

    append_path = None
    if "--append" in argv:
        index = argv.index("--append")
        if index + 1 >= len(argv):
            print("usage: cos_reconcile_metrics.py --append <row.json> <cos-ops dir>",
                  file=sys.stderr)
            return 2
        append_path = Path(argv[index + 1]).expanduser().resolve()
    args = [arg for arg in argv[1:] if not arg.startswith("--")]
    if append_path is not None:
        return _append_metric(argv, append_path)
    if not args:
        print("usage: cos_reconcile_metrics.py [--json] [--append <row.json>] "
              "<cos-ops dir>", file=sys.stderr)
        return 2
    ops = Path(args[0]).expanduser().resolve()
    if not ops.is_dir():
        print(f"FAIL: no cos-ops dir at {ops}", file=sys.stderr)
        return 2
    report = _source.reconcile(ops)
    return _render_reconciliation(report, "--json" in argv)
