#!/usr/bin/env python3
"""ATT-02 — record that the ingest bridge carried NOTHING, and why.

THE DEFECT THIS CLOSES. `tools/cos_ingest_bridge.py` is the only path from a
staged ingestion-ledger row to a `cos.propose` drop, and the nightly runs it
inside `if [ "${COS_INGEST_BRIDGE:-0}" = "1" ]`. Unset, the block is skipped
WHOLE: it invokes nothing, logs no `ingest bridge:` line, creates no
`_cos_ingest_bridge_<run>.jsonl`, and the night still reaches
`=== cos-nightly done ===` and exits 0. Measured as of run 2026-09-05-run260
(`_evidence/porter-finishes/bridge-skip-census.txt`): 15 of the 49 runs that
staged an `act` + `ingest.relevant` row have no bridge file at all, 596 of
1656 such rows sit on them, and 11 of those runs — 513 of the rows — ran with
the variable unset. The bridge's own source comment claimed those candidates
stayed "visible as the bridge's zero"; there was no zero, because there was
no file and no line. **Absence is what made 596 rows invisible.**

So this tool makes the two states DIFFERENT ON DISK, which is ATT-02's second
rule: a run that carried nothing writes a `_cos_ingest_bridge_<run>.jsonl`
holding one `outcome: no-drop` record naming a reason from the closed
vocabulary `cos.BRIDGE_SKIP_REASONS`, and stamps that same reason onto every
ingest-relevant ledger row still carrying no account of itself
(`cos.BRIDGE_SKIP_KEY`). `brain.cos_runverify_bridge.check_bridge_reach` then
COUNTS reasons rather than parsing prose, and FAILS the run — a skipped leg
that scores green is the whole defect.

WHO CALLS IT. Two callers, one rule:
  * `tools/cos_ingest_bridge.py`, on every path where the leg RAN and carried
    nothing (refused, backpressure-abort, writer-busy, receipts-root-unsafe,
    and an ordinary pass with no candidates at all).
  * `tools/cos_nightly.sh`'s bridge block, in the `else` arm — the leg the
    night did NOT run (`leg-disabled`). It is a SEPARATE script from the
    bridge deliberately: `tests/test_cos_ingest_bridge_nightly.py` slices that
    block out and runs it over a STUB bridge, and a stub cannot answer "were
    candidates staged" — the known-negative (a night with nothing relevant
    staged must stay silent) has to run this decision for real.

Exit 0 nothing to record — silence is correct, and the caller must stay quiet
· 9 a skip WAS recorded, the caller must be LOUD · 1 the recording failed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

if __name__ == "__main__":  # tools/ bootstrap, same as every cos_* tool
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brain import cos                                            # noqa: E402
from brain.cos_runverify_bridge import accounted, ingest_relevant  # noqa: E402

EXIT_SILENT = 0
EXIT_RECORDED = 9
EXIT_ERROR = 1


def _ledger_path(vault: Path, run_id: str) -> Path:
    return cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl"


def _read_rows(path: Path) -> tuple[list[dict], str]:
    """``(rows, damage)`` — a TOLERANT read, unlike the bridge's own.

    The bridge REFUSES a damaged ledger, because a row it skipped would be a
    silently lost candidate. This tool is the thing that runs when nothing was
    carried anyway, so it cannot refuse: it records what it can and NAMES the
    damage in the zero record. It never rewrites a ledger it could not read
    whole — a rewrite from a partial parse would delete the very rows the
    parse failed on.
    """
    if not path.is_file():
        return [], "no ingestion ledger on disk"
    rows: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            return rows, f"line {i} is not parseable JSON ({exc})"
        if not isinstance(row, dict):
            return rows, f"line {i} is not a JSON object"
        rows.append(row)
    return rows, ""


def record_skip(vault, run_id: str, reason: str, *,
                now: _dt.datetime | None = None) -> dict:
    """Stamp the unaccounted rows and write the zero record. Returns a report.

    ONLY UNACCOUNTED ROWS ARE STAMPED, and `accounted` is the ONE rule for
    that — shared verbatim with the check, so the recorder and the verifier
    can never disagree about what "already answered for" means. A row already
    carrying a drop stamp or a bridge settlement is left exactly as it was: a
    skip reason must never be able to sit on top of a delivered candidate and
    hide a duplicate id or a digest mismatch from E16.

    NO WRITER LOCK, deliberately. The bridge takes `cos.vault_writer_lock`
    around its DROP path, to serialize a drop and its ledger stamp against the
    hourly claim fold; there is no drop here to race. The two writers of this
    ledger are the judgment leg (which finishes before the bridge block starts)
    and the bridge itself (which is not running on any path that calls this),
    and the rewrite is `cos._write_atomic`, so a concurrent READER never sees a
    torn file. Stated residual: two of these running concurrently on the same
    run could lose one's stamps — the loud state is the same either way,
    because an unstamped row reads UNACCOUNTED and fails the run harder.
    """
    vault = Path(vault)
    if reason not in cos.BRIDGE_SKIP_REASONS:
        raise ValueError(
            f"{reason!r} is not one of {list(cos.BRIDGE_SKIP_REASONS)} — the "
            "vocabulary is closed so the run checks can count reasons instead "
            "of parsing prose")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    path = _ledger_path(vault, run_id)
    rows, damage = _read_rows(path)
    candidates = [r for r in rows if ingest_relevant(r)]
    needing = [r for r in candidates if accounted(r)[0] == "unaccounted"]

    stamped = 0
    if needing and not damage:
        for r in needing:
            r[cos.BRIDGE_SKIP_KEY] = reason
        cos._write_atomic(path, "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
            for r in rows).encode("utf-8"))
        stamped = len(needing)

    report = {
        "schema": cos.BRIDGE_ZERO_SCHEMA, "run": run_id, "reason": reason,
        "outcome": "no-drop", "dropped": 0,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidates": len(candidates), "unaccounted": len(needing),
        "stamped": stamped, "ledger_rows": len(rows),
    }
    if damage:
        report["ledger_damage"] = damage
    # THE FILE IS WRITTEN EVEN WHEN NOTHING NEEDED A STAMP. "The leg ran and
    # carried nothing" and "the leg never ran" are different facts, and the
    # only way to tell them apart later is that one of them left a file.
    cos.run_ops_dir(vault).mkdir(parents=True, exist_ok=True)
    cos.append_jsonl(
        cos.run_ops_dir(vault) / f"_cos_ingest_bridge_{run_id}.jsonl", report)
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--vault", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--reason", required=True, choices=cos.BRIDGE_SKIP_REASONS)
    p.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args(argv)
    try:
        report = record_skip(args.vault, args.run_id, args.reason)
    except Exception as exc:                                     # noqa: BLE001
        # LOUD, never a silent zero: a recorder that fails quietly is the exact
        # shape of the defect it exists to close.
        print(json.dumps({"run": args.run_id, "reason": args.reason,
                          "error": f"{type(exc).__name__}: {exc}"[:400]}))
        return EXIT_ERROR
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"ingest bridge {args.run_id}: no-drop ({args.reason}) — "
              f"{report['candidates']} staged candidate(s), "
              f"{report['stamped']} stamped")
    # SILENCE IS KEYED ON WHAT WAS LEFT BEHIND, not on what was staged, and
    # the CALLER cannot make that call: the nightly's `else` arm runs on every
    # night the leg is off, and most of those legitimately stage no candidate
    # at all. A run whose candidates all carry a drop stamp already (a replay
    # over a run an earlier bridge pass finished) left nothing behind either,
    # and shouting about it would be the false alarm that gets a guard muted.
    return EXIT_RECORDED if report["unaccounted"] else EXIT_SILENT


if __name__ == "__main__":
    raise SystemExit(main())
