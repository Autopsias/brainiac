#!/usr/bin/env python3
"""Score candidate move-back signals against the runs already on disk (FB-01).

WHY THIS EXISTS. The `owner_reversals` lane (PEN 2, `cos_driver_enumeration`)
ran on 59 of 60 nights, reported 405 back-in-Inbox observations and minted ZERO
rulings. "Widen what it listens for" is only answerable with a number per
candidate, and a probe that MODELS the predicate will confirm any predicate —
so this replays the REAL `feedback_cli.outlook_reversals` over each run's own
stored enumeration rows and the vault's real undo ledgers, and carries a
KNOWN-POSITIVE control so an all-clear that is really a wiring fault is caught.

Read-only. It touches no mailbox and writes nothing but its own stdout.

    python3 tools/cos_moveback_backtest.py --vault <vault> --evidence <repo>
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: A candidate that fires on hundreds of the observations is TOO LOOSE. Stated
#: here so the verdict is a threshold someone chose, not a reading of the table.
TOO_LOOSE_FRACTION = 0.25


def _ledger_rows(vault: Path) -> list[dict]:
    rows: list[dict] = []
    for p in sorted((vault / "cos-ops").glob("_cos_undo_ledger_*.jsonl")):
        for line in p.read_text("utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _enumerations(roots: list[Path]) -> dict[str, dict]:
    """`{run_id: enumeration}` — the RICHEST copy of each run, since the same
    run can be written into more than one worktree's evidence tree."""
    best: dict[str, tuple[int, dict]] = {}
    for root in roots:
        for p in root.rglob("enumeration*.json"):
            if "nightly" not in str(p):
                continue
            try:
                d = json.loads(p.read_text("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(d, dict) or "owner_reversals" not in d:
                continue
            rid = str(d.get("run_id") or "")
            n = len(d.get("rows") or [])
            if rid and (rid not in best or n > best[rid][0]):
                best[rid] = (n, d)
    return {k: v[1] for k, v in best.items()}


def _replay(vault: Path, enums: dict[str, dict]) -> dict:
    """The REAL predicate, run once per stored run with an empty `recorded`.

    THE LEDGER IS CUT BACK TO THE NIGHT, and that is not optional. Every undo
    ledger the vault has ever written is on disk today; handing all of them to a
    run from ten nights ago replays a mailbox against mutations that had not
    happened yet, and the first cut of this probe scored 8243 observations where
    the lane really reported 435. `fidelity` re-checks the replay against each
    run's OWN stored output, so a drift in this filter shows up as a number
    rather than being assumed away.
    """
    from brain.cos import feedback_cli
    from cos_driver_draw import CHIP_TIER

    led = _ledger_rows(vault)
    per_run: dict[str, dict] = {}
    fidelity: dict[str, Any] = {"matched": 0, "mismatched": []}
    for rid in sorted(enums):
        at = str(enums[rid].get("enumerated_at") or "")
        asof = [r for r in led if str(r.get("action_ts") or "") <= at]
        got = feedback_cli.outlook_reversals(
            asof, enums[rid].get("rows") or [], chip_tier=CHIP_TIER, recorded={})
        per_run[rid] = got
        stored = enums[rid]["owner_reversals"]
        pair = (len(got["new_work"]), len(got["reversals"]))
        want = (len(stored.get("new_work") or []),
                len(stored.get("reversals") or []))
        if pair == want:
            fidelity["matched"] += 1
        else:
            fidelity["mismatched"].append({"run": rid, "replayed": pair,
                                           "stored": want})
    return {"ledger_rows": len(led), "per_run": per_run, "fidelity": fidelity}


def _control(vault: Path) -> dict:
    """KNOWN POSITIVE. A synthetic landed archive whose thread is back in the
    Inbox with NO newer message must mint exactly one reversal — otherwise a
    zero above is a wiring fault, not a fact about this owner."""
    from brain.cos import feedback_cli
    from cos_driver_draw import CHIP_TIER

    led = [{"conversation_id": "CTRL", "verb": "archive", "state": "reconciled",
            "action_ts": "2026-09-01T02:00:00.000Z", "run": "control"}]
    enum = [{"conversation_id": "CTRL", "subject": "control",
             "received": "2026-08-31T09:00:00+00:00", "chip": None}]
    got = feedback_cli.outlook_reversals(led, enum, chip_tier=CHIP_TIER,
                                         recorded={})
    return {"reversals": len(got["reversals"]),
            "new_work": len(got["new_work"]),
            "passes": len(got["reversals"]) == 1 and not got["new_work"]}


def _stale_return_days(per_run: dict) -> dict:
    """Candidate B — "moved back and then left untouched for N days".

    One EVENT is a (conversation, action_ts, received) triple; its age is the
    span of run dates over which the lane re-reported that same triple with
    nothing changing.
    """
    seen: dict[tuple, list[_dt.date]] = collections.defaultdict(list)
    for rid, res in per_run.items():
        day = _dt.date.fromisoformat(rid[:10])
        for it in res["new_work"]:
            seen[(it["conversation_id"], it["action_ts"], it["received"])].append(day)
    spans = {k: (max(v) - min(v)).days for k, v in seen.items()}
    return {"events": len(spans),
            "fires_at_n_days": {n: sum(1 for d in spans.values() if d >= n)
                                for n in (1, 3, 7)},
            "span_days": sorted(spans.values(), reverse=True)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--evidence", required=True, type=Path, nargs="+",
                    help="tree(s) holding _evidence/nightly/*/enumeration*.json")
    a = ap.parse_args(argv)

    enums = _enumerations(a.evidence)
    rep = _replay(a.vault, enums)
    per_run = rep["per_run"]

    obs = sum(len(r["new_work"]) + len(r["reversals"]) for r in per_run.values())
    verbs = collections.Counter(
        it["verb"] for r in per_run.values() for it in r["new_work"] + r["reversals"])
    convs = collections.Counter(
        it["conversation_id"] for r in per_run.values()
        for it in r["new_work"] + r["reversals"])
    minted = sum(len(r["reversals"]) for r in per_run.values())

    led = _ledger_rows(a.vault)
    arch = collections.Counter(
        r.get("conversation_id") for r in led
        if r.get("verb") == "archive" and r.get("state") in ("reconciled", "confirmed"))

    out = {
        "as_of_run": max(per_run) if per_run else None,
        "replay_fidelity_vs_stored_output": rep["fidelity"],
        "runs_with_lane_output": len(per_run),
        "observations": obs,
        "distinct_conversations": len(convs),
        "observations_per_conversation": dict(
            sorted(collections.Counter(convs.values()).items())),
        "control_known_positive": _control(a.vault),
        "candidates": {
            "current-same-thread-reversal": {"would_mint": minted},
            "A-chip-change-on-a-chipped-thread": {
                "would_mint": sum(1 for r in per_run.values()
                                  for it in r["reversals"] if it["verb"] == "categorize"),
                "signals_seen": verbs.get("categorize", 0),
                "landed_categorize_mutations": sum(
                    1 for r in led if r.get("verb") == "categorize"
                    and r.get("state") in ("reconciled", "confirmed"))},
            "B-back-and-untouched-for-n-days": _stale_return_days(per_run),
            "C-draft-deleted-without-sending": {
                "measurable": False,
                "why": "no stored artifact carries the Drafts folder per "
                       "conversation: the page's drafts census stamps `isDraft` "
                       "on inbox items and ENUMERATION_FIELDS drops it, and "
                       "OBSERVABLE_VERBS excludes `draft` for the same reason"},
            "D-repeat-archive-churn": {
                "conversations_archived_twice_or_more":
                    sum(1 for v in arch.values() if v >= 2),
                "landed_archives": sum(arch.values()),
                "distinct_conversations": len(arch)},
        },
        "too_loose_fraction": TOO_LOOSE_FRACTION,
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
