#!/usr/bin/env python3
"""How long did the re-primed bearer actually last? (CUT-04 item 4, s09)

WHY THIS EXISTS. `cos_nightly.sh` budgets "a 40-minute apply" against a
15-18 minute OBSERVED envelope life, and that margin has never once been
measured — it is an inference from two run durations. Meanwhile the undo
ledger already carries a timestamp per mutation and the re-prime already knows
when it ran, so `last dispatch - re-prime` is the number, free.

It is printed on the HTTP-401 stop path (exit 16), where it is the whole
diagnosis: a lane whose envelope dies at minute 9 needs a different fix from
one that dies at minute 38.

A FILE RATHER THAN INLINE SHELL, deliberately. The nightly already carries
several `$PY -c` blocks, and a guard or a number computed inside one cannot be
executed by a test without slicing the script — so this one is a module a test
imports and runs directly.

    python3 tools/cos_envelope_life.py --ledger <undo.jsonl> --reprimed <iso>

It reads ONE file and prints ONE sentence. No vault, no browser, no mailbox.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def envelope_life(ledger: Path, reprimed: str) -> str:
    """The sentence the operator reads at 06:31.

    Every failure returns a SENTENCE, never an exception and never a silent
    zero: this runs on the stop path, and a diagnostic that raises would erase
    the report of the night it is diagnosing. "Nothing can be measured" is a
    legitimate answer and says which half was missing.
    """
    rows = []
    try:
        for line in Path(ledger).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, ValueError) as exc:
        return (f"the undo ledger could not be read ({str(exc)[:120]}), so the "
                "envelope's life is unmeasured here")
    # DISPATCHES, NOT TRANSITIONS (review 2026-08-13, round 5, Codex MEDIUM).
    # `UndoLedger.append()` writes one row per STATE TRANSITION, so a single
    # archive that went `intent` -> `verified` -> `reconciled` was reported as
    # "3 mutation(s) stamped" and the RECONCILIATION time was printed as "last
    # dispatch" — a number that runs past the envelope's actual life and
    # overstates the count it is beside. This diagnostic exists to tell a
    # bearer that died at minute 9 from one that died at minute 38; a
    # reconciliation stamped ten minutes after the last real request moves that
    # answer into the wrong bucket.
    #
    # `intent` is the write-ahead row: it is appended immediately BEFORE the
    # request goes out, so its timestamp is the closest thing the ledger holds
    # to a dispatch instant, and it is written exactly once per mutation.
    dispatches = [r for r in rows if str(r.get("state") or "") == "intent"]
    stamps = sorted(str(r.get("ts")) for r in dispatches if r.get("ts"))
    keys = {str(r.get("idempotency_key")) for r in dispatches
            if r.get("idempotency_key")}
    if not stamps:
        return (f"no mutation was dispatched ({len(rows)} ledger transition(s) "
                "carry no write-ahead `intent` row), so the envelope's life is "
                "unmeasured here")
    try:
        start = _dt.datetime.strptime(reprimed, _FMT)
        end = _dt.datetime.strptime(stamps[-1], _FMT)
    except (TypeError, ValueError) as exc:
        return (f"the timestamps could not be compared ({str(exc)[:120]}), so "
                "the envelope's life is unmeasured here")
    secs = int((end - start).total_seconds())
    if secs < 0:
        # A mutation stamped BEFORE the re-prime is a resumed row from an
        # earlier pass, not this envelope's. Saying so beats printing a
        # negative duration as if it meant something.
        return (f"the last mutation ({stamps[-1]}) predates the re-prime "
                f"({reprimed}) — that row belongs to an earlier pass, so this "
                "envelope's life is unmeasured here")
    return (f"the re-primed bearer lasted {secs // 60}m{secs % 60:02d}s into "
            f"the apply (re-primed {reprimed}, last dispatch {stamps[-1]}, "
            f"{len(keys) or len(stamps)} mutation(s) dispatched over "
            f"{len(rows)} ledger transition(s))")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--reprimed", required=True,
                   help="the re-prime's UTC instant, %%Y-%%m-%%dT%%H:%%M:%%SZ")
    args = p.parse_args(argv[1:])
    print(envelope_life(args.ledger, args.reprimed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
