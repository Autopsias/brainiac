#!/usr/bin/env python3
"""REP-02 — the regression gate for `eval/cos_replay.py` runs.

Reuses `eval/gate.py`'s shape and vocabulary (PASS/FAIL/ERROR, a flat
non-inferiority bound printed as a checklist) rather than inventing a second
gate idiom in this repo. What differs is what it gates: not per-query recall
against a golden set, but the CANDIDATE RATE of two `cos_replay.py` runs that
judged the SAME saved corpus — "did this change to the extractor make things
better, worse, or neither."

WHY A BAND, NOT A POINT COMPARISON (s03, `eval/runs/cos-replay-measurements.json`):
the judge (codex exec, gpt-5.6-sol) is NOT deterministic. Repeating the same
config 20 times each over 6 fixed bodies, 4.17% of judgments disagreed with
their own body's modal verdict — two of the six bodies came back 19/20 and
16/20, not 20/20. A gate that fails on ANY nonzero candidate-rate delta reads
that noise as a regression on the very first re-run and trains everyone to
ignore it. So PASS requires the delta to clear a NOISE BAND, not merely be
>= 0 — the same shape as `eval/gate.py`'s -2pp bound, except the bound here
is a MEASURED judge-noise floor, not a chosen quality margin. Only
`disposition` is compared, never `kind` — s03 found `kind` noisier still,
flipping even on rows where the verdict itself held.

PASS (exit 0): new_rate - current_rate >= -NOISE_BAND. Also true when new is
  BETTER (rate rose beyond the band) — a non-inferiority gate never fails an
  improvement.
FAIL (exit 1): delta < -NOISE_BAND — the candidate rate dropped by more than
  measured judge noise can explain. Reported WORSE.
ERROR (exit 2): the two runs cannot be compared — different corpus, or no
  conversation_id was judged in both.

Usage:
    python3 eval/replay_gate.py --current eval/runs/A.json --new eval/runs/B.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# s03 (eval/runs/cos-replay-measurements.json, 120 judgments = 6 bodies x 20
# repeats): 4.17% of judgments disagreed with their own body's modal verdict.
# This is the noise floor a two-run comparison must clear before a delta
# counts as regression — MEASURED, not a chosen quality margin the way
# eval/gate.py's -2pp is.
DEFAULT_NOISE_BAND = 0.0417

# Below this many paired judged rows, the band above is being applied with
# less evidence than it was measured with (s03 sampled n=20 per body). Not a
# SKIP — a small corpus is exactly where a regression gate gets asked to run
# — just a printed caveat so a PASS/FAIL at low n is not mistaken for a
# tight one.
LOW_CONFIDENCE_N = 20


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def paired_rows(current: dict, new: dict) -> tuple[list[str], str | None]:
    """conversation_ids judged (disposition present) in BOTH runs.

    Returns (ids, error) — ids is empty and error is set when the two runs
    are not comparable at all: different corpus, or nothing overlaps.
    """
    ccorp = current.get("corpus", {}).get("path")
    ncorp = new.get("corpus", {}).get("path")
    if ccorp and ncorp and ccorp != ncorp:
        return [], (f"corpus mismatch: current={ccorp!r} new={ncorp!r} — a "
                    f"regression gate compares two versions over ONE corpus")
    cv, nv = current.get("verdicts", {}), new.get("verdicts", {})
    ids = sorted(cid for cid in (set(cv) & set(nv))
                 if cv[cid].get("judged") and nv[cid].get("judged"))
    if not ids:
        return [], "no conversation_id was judged in BOTH runs — nothing to compare"
    return ids, None


def candidate_count(verdicts: dict, ids: list[str]) -> int:
    return sum(1 for cid in ids if verdicts[cid].get("disposition") == "candidate")


def disposition_churn(current: dict, new: dict, ids: list[str]) -> list[dict]:
    """Rows whose `disposition` differs between the two runs — informational,
    the row-level detail underneath the one rate number. Never `kind` (s03:
    noisier still, flips even where `disposition` holds)."""
    out = []
    for cid in ids:
        cd = current["verdicts"][cid].get("disposition")
        nd = new["verdicts"][cid].get("disposition")
        if cd != nd:
            out.append({"conversation_id": cid, "current_disposition": cd,
                        "new_disposition": nd})
    return out


def evaluate(current: dict, new: dict, band: float = DEFAULT_NOISE_BAND) -> dict:
    """The gate's decision, as data — `main()` is a thin print/exit wrapper
    over this so tests can assert on the decision without parsing stdout."""
    ids, err = paired_rows(current, new)
    if err:
        return {"error": err}

    n = len(ids)
    cur_n = candidate_count(current["verdicts"], ids)
    new_n = candidate_count(new["verdicts"], ids)
    cur_rate, new_rate = cur_n / n, new_n / n
    delta = new_rate - cur_rate
    churn = disposition_churn(current, new, ids)

    verdict = "NEITHER"
    if delta > band:
        verdict = "BETTER"
    elif delta < -band:
        verdict = "WORSE"

    return {
        "error": None,
        "n": n,
        "low_confidence": n < LOW_CONFIDENCE_N,
        "current_rate": cur_rate, "current_candidates": cur_n,
        "new_rate": new_rate, "new_candidates": new_n,
        "delta": delta, "band": band,
        "verdict": verdict,
        "gate_pass": delta >= -band,
        "churn": churn,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--current", required=True, help="cos_replay.py run file — the baseline")
    ap.add_argument("--new", required=True, help="cos_replay.py run file — the change under test")
    ap.add_argument("--band", type=float, default=DEFAULT_NOISE_BAND,
                    help=f"non-inferiority band on the candidate-rate delta "
                         f"(default {DEFAULT_NOISE_BAND} — s03's measured "
                         f"verdict-disagreement rate)")
    args = ap.parse_args(argv)

    current, new = _load(args.current), _load(args.new)

    print("=" * 72)
    print(f"REPLAY REGRESSION GATE — candidate-rate non-inferiority "
          f"(band = ±{args.band:.2%})")
    print("=" * 72)
    print(f"  current: {current.get('system')}  ({args.current})")
    print(f"  new:     {new.get('system')}  ({args.new})")

    r = evaluate(current, new, args.band)
    if r["error"]:
        print(f"\nERROR: {r['error']}")
        print("This is NOT a pass.")
        return 2

    print(f"\n  paired judged rows: n={r['n']}"
          + (f"  [LOW CONFIDENCE — the band above was measured at n="
             f"{LOW_CONFIDENCE_N} per body; deciding on fewer rows than that "
             f"is not a tight read of the band]" if r["low_confidence"] else ""))
    print(f"  candidate rate: current={r['current_rate']:.4f} "
          f"({r['current_candidates']}/{r['n']})  "
          f"new={r['new_rate']:.4f} ({r['new_candidates']}/{r['n']})")
    print(f"  delta: {r['delta']:+.4f}  (need >= {-r['band']:+.4f} to clear the noise band)")

    if r["churn"]:
        print(f"\n  disposition changed on {len(r['churn'])} of {r['n']} row(s):")
        for c in r["churn"]:
            print(f"    {c['conversation_id']}: "
                  f"{c['current_disposition']} -> {c['new_disposition']}")
    else:
        print("\n  disposition unchanged on every paired row.")

    print()
    print("-" * 72)
    print(f"VERDICT: {r['verdict']}")
    if r["gate_pass"]:
        print("GATE: PASS — not a regression.")
        return 0
    print(f"GATE: FAIL — regression. The candidate rate dropped more than "
          f"measured judge noise (±{args.band:.2%}) can explain.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
