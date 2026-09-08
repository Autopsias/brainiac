#!/usr/bin/env python3
"""Does every `act` thread carry a draft, or a stated reason for waiting?

THE PROMISE IS "the inbox holds only act threads with a chip and a draft or
hold", and the only way to ask it so far was to read a night's ledger by hand.
Point this at a run and it answers off the PORTER'S OWN ENUMERATION — the
conversations that run's ingestion ledger records, which is what the driver
actually saw in the Inbox — never off a sheet.

    python3 tools/cos_act_census.py --vault <vault>            # newest run
    python3 tools/cos_act_census.py --vault <vault> --run 2026-09-05-run263
    python3 tools/cos_act_census.py --vault <vault> --json out.json
    python3 tools/cos_act_census.py --selfcheck   # prove it can say NON-ZERO

TWO READINGS, AND THE DIFFERENCE IS THE POINT (measured 2026-09-05).

* `hold_category` is the HOLD vocabulary — the closed screen list of DOCTRINE
  §3.3, derived host-side as the first screen that fired, one per conversation.
  It answers "why does this thread wait". On `2026-09-05-run263`: 65 of 65 act
  threads carry one, so **0** are undisposed.
* `disposition` / `needs_owner` are INGESTION fields — did the body become a
  vault candidate, and does the row need a human before it can be ingested.
  Reading those instead reports **33** undisposed act threads on the same run,
  and **38** on `2026-09-04-run258`, which is where the acceptance review's
  "40 act threads with neither a draft nor a hold" came from.

Both are printed. The second is not wrong about its own field; it is a
different question, and only the first one is the hold.

ponytail: no config, no registry. One run in, one table out.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_LEDGER = "_cos_ingestion_ledger_"
_UNDO = "_cos_undo_ledger_"


def run_key(run_id: str) -> tuple[str, int]:
    """Date first, then the run NUMBER — `run68` sorts after `run106` as text."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})-run(\d+)", run_id)
    return (m.group(1), int(m.group(2))) if m else (run_id, 0)


def rows(path: Path) -> list[dict[str, Any]]:
    """Every parsable row. A truncated line is skipped, never fatal: one
    half-written row in a 154-file history must not blind the census."""
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def ledgers(ops: Path, prefix: str) -> list[tuple[str, Path]]:
    return sorted(((p.name[len(prefix):-len(".jsonl")], p)
                   for p in ops.glob(f"{prefix}*.jsonl")),
                  key=lambda t: run_key(t[0]))


def drafted(ops: Path) -> set[str]:
    """Conversations carrying a draft this lane SAVED and re-read in Drafts."""
    out = set()
    for _run, path in ledgers(ops, _UNDO):
        for row in rows(path):
            if (row.get("verb") == "draft" and row.get("state") == "reconciled"
                    and row.get("verification") == "verified-draft-saved"):
                out.add(row.get("conversation_id"))
    return out


def held_history(ops: Path, population: set[str], upto: str
                 ) -> dict[str, dict[str, Any]]:
    """Per conversation: its latest judged facts, and how long it has waited.

    `held_since` is the start of the CURRENT unbroken streak of held runs — a
    run that judged the thread and gave it no hold clears it, so a hold that
    was lifted and re-taken reports the new age, not the old one.
    """
    state: dict[str, dict[str, Any]] = {}
    for run_id, path in ledgers(ops, _LEDGER):
        if run_key(run_id) > run_key(upto):
            break
        for row in rows(path):
            cid = row.get("conversation_id")
            if cid not in population:
                continue
            cur = state.setdefault(cid, {"held_since": None})
            cur["last_seen_run"] = run_id
            hold = row.get("hold_category")
            if hold:
                cur["hold_category"] = hold
                cur["hold_run"] = run_id
                if cur["held_since"] is None:
                    cur["held_since"] = row.get("ts") or run_id[:10]
            elif row.get("verdict"):
                cur.pop("hold_category", None)
                cur["held_since"] = None
            for key in ("verdict", "disposition", "needs_owner", "tier",
                        "judged_tier", "received"):
                if row.get(key) not in (None, ""):
                    cur[key] = row.get(key)
    return state


def _days(stamp: str | None, now: dt.datetime) -> int | None:
    if not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0, (now - when).days)


def census(vault: Path, run_id: str | None = None,
           now: dt.datetime | None = None) -> dict[str, Any]:
    from brain import cos                                       # noqa: PLC0415
    from cos_driver_transport import short                       # noqa: PLC0415
    ops = cos.run_ops_dir(vault)
    runs = ledgers(ops, _LEDGER)
    if not runs:
        raise SystemExit(f"no ingestion ledger under {ops}")
    run_id = run_id or runs[-1][0]
    path = dict(runs).get(run_id)
    if path is None:
        raise SystemExit(f"{run_id} has no ingestion ledger under {ops}")
    population = {r["conversation_id"] for r in rows(path)
                  if r.get("conversation_id")}
    state = held_history(ops, population, run_id)
    saved = drafted(ops)
    now = now or dt.datetime.now(dt.timezone.utc)

    threads = []
    for cid in sorted(population):
        f = state.get(cid, {})
        threads.append({
            # A DIGEST, never the id: a conversation id names a real mailbox
            # and this repo is a public-export source (s01 precedent).
            "conversation": short(cid),
            "verdict": f.get("verdict"),
            "tier": f.get("judged_tier") or f.get("tier"),
            "draft": cid in saved,
            "hold": f.get("hold_category"),
            "hold_age_days": _days(f.get("held_since"), now),
            "hold_run": f.get("hold_run"),
            "ingestion_disposition": f.get("disposition"),
            "needs_owner": f.get("needs_owner"),
        })
    act = [t for t in threads if t["verdict"] == "act"]
    return {
        "run_id": run_id, "measured_at": now.isoformat(),
        "threads_enumerated": len(threads),
        "verdicts": {v: sum(1 for t in threads if t["verdict"] == v)
                     for v in ("act", "read", "noise", None)},
        "act_threads": len(act),
        "act_with_draft": sum(1 for t in act if t["draft"]),
        "act_with_hold": sum(1 for t in act if t["hold"]),
        "act_with_neither": [t for t in act if not t["draft"] and not t["hold"]],
        # The acceptance review's reading, kept so the two are comparable.
        "act_with_neither_ingestion_reading": [
            t for t in act if not t["draft"]
            and t["ingestion_disposition"] != "held" and not t["needs_owner"]],
        "holds": sorted((t for t in act if t["hold"]),
                        key=lambda t: -(t["hold_age_days"] or 0)),
        "threads": threads,
    }


def _selfcheck() -> int:
    """The all-clear must be earned. Feed it a run where one act thread has
    neither, and require it to SAY SO."""
    import tempfile                                             # noqa: PLC0415
    with tempfile.TemporaryDirectory() as tmp:
        ops = Path(tmp) / "cos-ops"
        ops.mkdir(parents=True)
        led = ops / f"{_LEDGER}2026-01-01-run1.jsonl"
        led.write_text("".join(json.dumps(r) + "\n" for r in (
            {"conversation_id": "held", "verdict": "act",
             "hold_category": "Held · ask", "ts": "2025-12-02T00:00:00Z"},
            {"conversation_id": "bare", "verdict": "act"},
        )), encoding="utf-8")
        import brain.cos as cos_mod                             # noqa: PLC0415
        original = cos_mod.run_ops_dir
        cos_mod.run_ops_dir = lambda _v: ops                    # noqa: ARG005
        try:
            out = census(Path(tmp), now=dt.datetime(
                2026, 1, 1, tzinfo=dt.timezone.utc))
        finally:
            cos_mod.run_ops_dir = original
    bare = [t["conversation"] for t in out["act_with_neither"]]
    ages = [t["hold_age_days"] for t in out["holds"]]
    ok = out["act_threads"] == 2 and len(bare) == 1 and ages == [30]
    print(json.dumps({"selfcheck": "pass" if ok else "FAIL",
                      "act_with_neither": bare, "hold_ages": ages}, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default=os.environ.get(
        "BRAIN_VAULT", str(Path.home() / "DeveloperFolder/Brainiac/vault")))
    ap.add_argument("--run")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args(argv)
    if args.selfcheck:
        return _selfcheck()

    out = census(Path(args.vault).expanduser(), args.run)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2) + "\n",
                                       encoding="utf-8")
    print(f"run {out['run_id']} — {out['threads_enumerated']} threads "
          f"enumerated, {out['act_threads']} act")
    print(f"  with a saved draft        {out['act_with_draft']}")
    print(f"  with a stated hold        {out['act_with_hold']}")
    print(f"  with NEITHER              {len(out['act_with_neither'])}")
    print(f"  (ingestion-field reading  "
          f"{len(out['act_with_neither_ingestion_reading'])})")
    by_reason: dict[str, list[int]] = {}
    for t in out["holds"]:
        by_reason.setdefault(t["hold"], []).append(t["hold_age_days"] or 0)
    for reason, ages in sorted(by_reason.items()):
        print(f"    {reason:<20} {len(ages):>3}  oldest {max(ages)}d "
              f"median {sorted(ages)[len(ages) // 2]}d")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    raise SystemExit(main())
