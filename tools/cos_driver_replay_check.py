#!/usr/bin/env python3
"""Prove the accounting is reproducible from the CORPUS, in a SEPARATE process.

Re-hashing a file proves the file did not change. The determinism claim is
different and stronger: given the same captured inputs, the ledger is rebuilt
byte-for-byte by code that never saw the mailbox. So this runs
``cos_driver.py --replay`` as its own process — a fresh interpreter, reading only
the host-only capture corpus — and diffs its rows against the ledger the live
night wrote.

    python3 tools/cos_driver_replay_check.py --vault <vault> --run-id <id>

Exit 0 and an empty diff is the proof. Anything else names the first row and key
that differ.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "cos_driver.py"

#: Excluded from the comparison, each for a stated reason. Everything else in
#: every row is inside the diff.
EXCLUDED = {
    "bundle_version": "stamped by the HOST from the run manifest at write time, "
                      "not derived from the capture",
    "extraction_rules_version": "same — a host manifest field",
    "ts": "the run's `enumerated_at`, a capture-time stamp the replay is GIVEN "
          "rather than deriving; it is compared separately, as one value",
}


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def strip(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in EXCLUDED}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv[1:])

    sys.path.insert(0, str(HERE.parent / "src"))
    from brain import cos  # noqa: PLC0415

    ledger = cos.run_ops_dir(args.vault) / f"_cos_ingestion_ledger_{args.run_id}.jsonl"
    live = rows(ledger)
    if not live:
        print(f"no ledger rows at {ledger}", file=sys.stderr)
        return 2
    enumerated_at = live[0]["ts"]

    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--vault", str(args.vault),
         "--replay", args.run_id, "--enumerated-at", enumerated_at],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
        return 3
    replayed = json.loads(proc.stdout)["rows"]

    diff: list[dict] = []
    if len(live) != len(replayed):
        diff.append({"row_count": {"live": len(live), "replayed": len(replayed)}})
    for i, (a, b) in enumerate(zip(live, replayed)):
        sa, sb = strip(a), strip(b)
        if sa != sb:
            keys = sorted(k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k))
            diff.append({"row": i, "conversation_id": a.get("conversation_id"),
                         "keys": keys})
        if b["ts"] != enumerated_at:
            diff.append({"row": i, "keys": ["ts"]})

    report = {
        "run_id": args.run_id,
        "method": ("`cos_driver.py --replay` in a SEPARATE process, reading the "
                   "host-only capture corpus; never a re-hash of an output file"),
        "replay_process": {"argv": ["cos_driver.py", "--replay", args.run_id],
                           "returncode": proc.returncode},
        "rows_live": len(live),
        "rows_replayed": len(replayed),
        "excluded_fields": EXCLUDED,
        "enumerated_at_compared_as_one_value": enumerated_at,
        "second_process_diff": diff,
    }
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0 if not diff else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
