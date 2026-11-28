#!/usr/bin/env python3
"""REP-02 — ground-truth regression gate for `eval/cos_replay.py` runs.

Reuses `eval/gate.py`'s shape and vocabulary (PASS/FAIL/ERROR, a flat
non-inferiority band printed as a checklist). Gates on GROUND-TRUTH
accuracy, precision, recall, F1, and empty-body refusal compliance — never on
raw candidate rate. `candidate_rate` is reported as informational telemetry
only (gating on candidate rate previously rewarded false-positive explosions
and failed false-positive fixes).

PRECEDENCE (strict, fail-closed):
  1. Empty-body refusal floor (`verdict == "refuse"` and `not judged` on every
     expected-`refuse` row): ANY violation is a hard FAIL (`VERDICT: WORSE`),
     even if accuracy/precision improved.
  2. Degenerate all-candidate / zero-specificity judge (`tn == 0` when
     non-candidates exist): hard FAIL (`VERDICT: WORSE`).
  3. Total recall collapse (`cur_recall > 0` and `new_recall == 0.0`): hard
     FAIL (`VERDICT: WORSE`) regardless of `--band`.
  4. Recall floor (`cur_recall - new_recall > min(band, 0.05)`): ANY recall
     drop exceeding `min(band, 0.05)` (at most 5pp ever allowed) is a hard FAIL
     (`VERDICT: WORSE`), even if precision or accuracy improved (a mixed
     regression that drops recall while removing false positives MUST fail).
  5. Precision / F1 / Accuracy non-inferiority: ANY drop beyond `-band` on
     precision, F1, or exact accuracy is a FAIL (`VERDICT: WORSE`) — so a
     degenerate all-candidate judge ALWAYS fails on precision/accuracy drop.
  6. Improvement: if no floor/non-inferiority check fails and any of accuracy,
     precision, recall, or F1 rises beyond `+band`, `VERDICT: BETTER` (PASS).
  7. Otherwise: `VERDICT: NEITHER` (PASS).

ERROR (exit 2): runs cannot be compared — corpus mismatch, no overlapping
judged rows, invalid `--band` (must be in `[0.0, 0.10]`), malformed JSON,
contradictory row `verdict`/`disposition`, or missing/incomplete/conflicting
ground-truth labels.

Usage:
    python3 eval/replay_gate.py --current eval/runs/A.json --new eval/runs/B.json \\
        [--expected eval/fixtures/cos-corpus-synthetic-verdicts.json]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

# s03 (eval/runs/cos-replay-measurements.json, 120 judgments = 6 bodies x 20
# repeats): 4.17% of judgments disagreed with their own body's modal verdict.
# This is the measured judge-noise floor a two-run comparison must clear.
DEFAULT_NOISE_BAND = 0.0417

# Strict upper bound on `--band`: a band > 0.10 would mask substantial
# regressions across precision, F1, or accuracy.
MAX_NOISE_BAND = 0.10

# Strict recall-drop ceiling independent of `--band`: even at the maximum
# allowed band (0.10), recall may NEVER drop by more than 5 percentage points.
MAX_RECALL_DROP = 0.05

# Below this many paired judged rows, print a low-confidence caveat.
LOW_CONFIDENCE_N = 20

_VALID_LABELS = frozenset({"candidate", "not_candidate", "refuse"})


def _load(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _row_class(value: str) -> str:
    if value == "candidate":
        return "candidate"
    if value == "refuse":
        return "refuse"
    if value in ("not_candidate", "held", "no-substance"):
        return "not_candidate"
    return value


def _validate_run(run: object, label: str) -> str | None:
    if not isinstance(run, dict):
        return f"invalid {label} run: expected JSON object, got {type(run).__name__}"
    corpus = run.get("corpus")
    if corpus is not None and not isinstance(corpus, dict):
        return f"invalid {label} run corpus: expected JSON object, got {type(corpus).__name__}"
    verdicts = run.get("verdicts")
    if not isinstance(verdicts, dict):
        return f"invalid {label} run verdicts: expected JSON object, got {type(verdicts).__name__}"
    for cid, row in verdicts.items():
        if not isinstance(cid, str) or not isinstance(row, dict):
            return f"invalid {label} verdict entry for {cid!r}: expected JSON object"
        v = row.get("verdict")
        if v is not None and not isinstance(v, str):
            return (f"invalid {label} verdict value for {cid!r}: "
                    f"expected string, got {type(v).__name__}")
        disp = row.get("disposition")
        if disp is not None and not isinstance(disp, str):
            return (f"invalid {label} disposition value for {cid!r}: "
                    f"expected string, got {type(disp).__name__}")
        dec = row.get("decision")
        if dec is not None and not isinstance(dec, str):
            return (f"invalid {label} decision value for {cid!r}: "
                    f"expected string, got {type(dec).__name__}")
        if v is not None and disp is not None:
            if _row_class(v) != _row_class(disp):
                return (
                    f"invalid {label} row for {cid!r}: contradictory "
                    f"verdict/disposition (verdict={v!r}, disposition={disp!r})"
                )
        if v is not None and dec is not None:
            if _row_class(v) != _row_class(dec):
                return (
                    f"invalid {label} row for {cid!r}: contradictory "
                    f"verdict/decision (verdict={v!r}, decision={dec!r})"
                )
        if disp is not None and dec is not None:
            if _row_class(disp) != _row_class(dec):
                return (
                    f"invalid {label} row for {cid!r}: contradictory "
                    f"disposition/decision (disposition={disp!r}, decision={dec!r})"
                )
    acc = run.get("accuracy")
    if acc is not None:
        if not isinstance(acc, dict):
            return f"invalid {label} run accuracy: expected JSON object, got {type(acc).__name__}"
        rows = acc.get("rows")
        if rows is not None and not isinstance(rows, dict):
            return (f"invalid {label} run accuracy.rows: "
                    f"expected JSON object, got {type(rows).__name__}")
    return None


def paired_rows(current: dict, new: dict) -> tuple[list[str], str | None]:
    """conversation_ids judged (disposition present) in BOTH runs.

    Returns (ids, error) — ids is empty and error is set when the two runs
    are not comparable at all: different corpus, or nothing overlaps.
    """
    for run, label in ((current, "current"), (new, "new")):
        err = _validate_run(run, label)
        if err:
            return [], err
    ccorp = (current.get("corpus") or {}).get("path")
    ncorp = (new.get("corpus") or {}).get("path")
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
    the row-level detail underneath the metrics. Never `kind` (s03: noisier
    still, flips even where `disposition` holds)."""
    out = []
    for cid in ids:
        cd = current["verdicts"][cid].get("disposition")
        nd = new["verdicts"][cid].get("disposition")
        if cd != nd:
            out.append({"conversation_id": cid, "current_disposition": cd,
                        "new_disposition": nd})
    return out


def _normalize_expected(raw: object) -> tuple[dict[str, str], str | None]:
    if not isinstance(raw, dict):
        return {}, (f"invalid ground-truth labels: expected JSON object, "
                    f"got {type(raw).__name__}")
    gt: dict[str, str] = {}
    for cid, entry in raw.items():
        if not isinstance(cid, str):
            return {}, f"invalid ground-truth conversation_id {cid!r}: expected string"
        if isinstance(entry, str):
            label: object = entry
        elif isinstance(entry, dict):
            label = entry.get("expected_verdict")
            if label is None:
                label = entry.get("expected")
        else:
            label = None
        if not isinstance(label, str) or label not in _VALID_LABELS:
            return {}, (f"invalid ground-truth label for {cid!r}: {label!r} "
                        f"(expected one of {sorted(_VALID_LABELS)})")
        gt[cid] = label
    return gt, None


def extract_ground_truth(
    current: dict, new: dict, expected: object = None
) -> tuple[dict[str, str], str | None]:
    """Resolve ground-truth labels from `--expected` and/or embedded `accuracy.rows`.

    Whenever multiple ground-truth sources are present (e.g. `--expected` plus
    embedded `current`/`new` `accuracy.rows`, or both runs' `accuracy.rows`),
    every overlapping `conversation_id` MUST agree across all sources.
    """
    sources: list[tuple[str, dict[str, str]]] = []
    if expected is not None:
        exp_gt, err = _normalize_expected(expected)
        if err:
            return {}, err
        if not exp_gt:
            return {}, "empty ground-truth labels mapping provided"
        sources.append(("expected", exp_gt))

    cur_rows = (current.get("accuracy") or {}).get("rows") or {}
    new_rows = (new.get("accuracy") or {}).get("rows") or {}
    if cur_rows:
        cur_gt, err = _normalize_expected(cur_rows)
        if err:
            return {}, f"current run {err}"
        sources.append(("current", cur_gt))
    if new_rows:
        new_gt, err = _normalize_expected(new_rows)
        if err:
            return {}, f"new run {err}"
        sources.append(("new", new_gt))

    if not sources:
        return {}, (
            "missing ground-truth labels: pass --expected <verdicts.json> "
            "or run cos_replay.py with --expected so accuracy.rows is embedded"
        )

    merged: dict[str, str] = {}
    seen_from: dict[str, str] = {}
    conflicts: list[str] = []
    for src_name, gt_map in sources:
        for cid, label in sorted(gt_map.items()):
            if cid in merged and merged[cid] != label:
                conflicts.append(
                    f"{cid!r} ({seen_from[cid]}={merged[cid]!r} vs {src_name}={label!r})"
                )
            elif cid not in merged:
                merged[cid] = label
                seen_from[cid] = src_name

    if conflicts:
        return {}, f"conflicting ground-truth labels: {'; '.join(conflicts)}"

    if expected is not None:
        return sources[0][1], None
    return merged, None


def _verdict_of(row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    v = row.get("verdict")
    if isinstance(v, str) and v in _VALID_LABELS:
        return v
    if row.get("judged"):
        return "candidate" if row.get("disposition") == "candidate" else "not_candidate"
    return v if isinstance(v, str) else None


def score_metrics(
    verdicts: dict,
    judged_ids: list[str],
    refuse_ids: list[str],
    gt: dict[str, str],
) -> dict:
    """Compute precision, recall, F1, exact accuracy, and refusal compliance."""
    tp = fp = tn = fn = 0
    for cid in judged_ids:
        want = gt[cid]
        got = _verdict_of(verdicts.get(cid) or {})
        if want == "candidate":
            if got == "candidate":
                tp += 1
            else:
                fn += 1
        elif want == "not_candidate":
            if got == "candidate":
                fp += 1
            elif got == "not_candidate":
                tn += 1

    refusal_violations: list[str] = []
    refuse_ok = 0
    for cid in refuse_ids:
        row = verdicts.get(cid) or {}
        if row.get("verdict") == "refuse" and not row.get("judged"):
            refuse_ok += 1
        else:
            refusal_violations.append(cid)

    if tp + fp > 0:
        precision = tp / (tp + fp)
    else:
        precision = 1.0 if (tp + fn) == 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)
    total = len(judged_ids) + len(refuse_ids)
    agree = tp + tn + refuse_ok
    accuracy = agree / total if total > 0 else 0.0
    refusal_rate = refuse_ok / len(refuse_ids) if refuse_ids else 1.0

    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
        "agree": agree,
        "total": total,
        "refuse_ok": refuse_ok,
        "refuse_total": len(refuse_ids),
        "refusal_rate": refusal_rate,
        "refusal_violations": refusal_violations,
    }


def _validate_band(band: float) -> str | None:
    if (isinstance(band, bool)
            or not isinstance(band, (int, float))
            or not math.isfinite(band)
            or band < 0.0
            or band > MAX_NOISE_BAND):
        return (f"invalid noise band {band!r}: must be a finite number "
                f"in [0.0, {MAX_NOISE_BAND}]")
    return None


def evaluate(
    current: dict,
    new: dict,
    band: float = DEFAULT_NOISE_BAND,
    expected: object = None,
) -> dict:
    """The gate's decision, as data — `main()` is a thin print/exit wrapper
    over this so tests can assert on the decision without parsing stdout."""
    band_err = _validate_band(band)
    if band_err:
        return {"error": band_err}

    ids, err = paired_rows(current, new)
    if err:
        return {"error": err}

    gt, gt_err = extract_ground_truth(current, new, expected=expected)
    if gt_err:
        return {"error": gt_err}

    cv, nv = current.get("verdicts", {}), new.get("verdicts", {})

    # Fail closed if ground-truth labels omit ANY conversation_id present in
    # current, new, or embedded baseline/new accuracy.rows (prevents a partial
    # `--expected` sidecar from hiding missing true candidates).
    cur_acc_rows = (current.get("accuracy") or {}).get("rows") or {}
    new_acc_rows = (new.get("accuracy") or {}).get("rows") or {}
    all_known_ids = set(cv) | set(nv) | set(cur_acc_rows) | set(new_acc_rows)
    missing_gt = sorted(cid for cid in all_known_ids if cid not in gt)
    if missing_gt:
        return {
            "error": (
                f"incomplete ground-truth labels: missing label(s) for "
                f"conversation_id(s): {missing_gt}"
            )
        }

    # Fail closed if any expected item is missing from either run, or if any
    # expected non-refuse item was not judged (e.g. dropped or errored).
    missing_cur = sorted(cid for cid in gt if cid not in cv)
    missing_new = sorted(cid for cid in gt if cid not in nv)
    if missing_cur or missing_new:
        return {
            "error": (
                f"missing expected judgment(s): "
                f"current={missing_cur} new={missing_new}"
            )
        }
    unjudged_cur = sorted(
        cid for cid, label in gt.items()
        if label != "refuse" and not (cv.get(cid) or {}).get("judged")
    )
    unjudged_new = sorted(
        cid for cid, label in gt.items()
        if label != "refuse" and not (nv.get(cid) or {}).get("judged")
    )
    if unjudged_cur or unjudged_new:
        return {
            "error": (
                f"unjudged expected non-refuse row(s): "
                f"current={unjudged_cur} new={unjudged_new}"
            )
        }

    refuse_ids = sorted(cid for cid, label in gt.items() if label == "refuse")
    judged_ids = sorted(
        cid for cid, label in gt.items()
        if label in ("candidate", "not_candidate")
    )

    cur_m = score_metrics(cv, judged_ids, refuse_ids, gt)
    new_m = score_metrics(nv, judged_ids, refuse_ids, gt)

    deltas = {
        k: new_m[k] - cur_m[k]
        for k in ("accuracy", "precision", "recall", "f1")
    }

    # Precedence:
    # 1. Empty-body refusal floor (hard fail — overrides any accuracy/precision gain)
    # 2. Degenerate all-candidate / zero-specificity judge (hard fail — overrides
    #    any improvement over a broken baseline)
    # 3. Total recall collapse (hard fail — overrides --band)
    # 4. Recall floor (hard fail — overrides any accuracy/precision gain)
    # 5. Precision / F1 / Accuracy non-inferiority
    failure_reasons: list[str] = []
    if new_m["refusal_violations"]:
        failure_reasons.append(
            f"refusal_floor: expected-refuse row(s) not refused in new run: "
            f"{new_m['refusal_violations']}"
        )
    neg_total = new_m["fp"] + new_m["tn"]
    if neg_total > 0 and new_m["tn"] == 0:
        failure_reasons.append(
            f"degenerate_judge: 0/{neg_total} true negatives "
            f"(100% false-positive rate on non-candidate rows)"
        )
    if cur_m["recall"] > 0.0 and new_m["recall"] == 0.0:
        failure_reasons.append(
            f"recall_collapse: recall dropped to 0.0000 from "
            f"{cur_m['recall']:.4f} (total recall loss is always a hard fail)"
        )
    max_recall_drop = min(band, MAX_RECALL_DROP)
    if deltas["recall"] < -max_recall_drop:
        failure_reasons.append(
            f"recall_floor: recall dropped {cur_m['recall']:.4f} -> "
            f"{new_m['recall']:.4f} (delta {deltas['recall']:+.4f} < -{max_recall_drop:.4f})"
        )
    if deltas["precision"] < -band:
        failure_reasons.append(
            f"precision_drop: precision dropped {cur_m['precision']:.4f} -> "
            f"{new_m['precision']:.4f} (delta {deltas['precision']:+.4f} < -{band:.4f})"
        )
    if deltas["f1"] < -band:
        failure_reasons.append(
            f"f1_drop: F1 dropped {cur_m['f1']:.4f} -> "
            f"{new_m['f1']:.4f} (delta {deltas['f1']:+.4f} < -{band:.4f})"
        )
    if deltas["accuracy"] < -band:
        failure_reasons.append(
            f"accuracy_drop: accuracy dropped {cur_m['accuracy']:.4f} -> "
            f"{new_m['accuracy']:.4f} (delta {deltas['accuracy']:+.4f} < -{band:.4f})"
        )

    if failure_reasons:
        verdict = "WORSE"
    elif any(deltas[k] > band for k in ("accuracy", "precision", "recall", "f1")):
        verdict = "BETTER"
    else:
        verdict = "NEITHER"

    # Candidate rate is informational telemetry ONLY — never gates.
    n = len(ids)
    cur_n = candidate_count(cv, ids)
    new_n = candidate_count(nv, ids)
    cur_rate, new_rate = cur_n / n, new_n / n
    churn = disposition_churn(current, new, ids)

    return {
        "error": None,
        "n": n,
        "low_confidence": n < LOW_CONFIDENCE_N,
        "band": band,
        "current_metrics": cur_m,
        "new_metrics": new_m,
        "deltas": deltas,
        "refusal_violations": new_m["refusal_violations"],
        "failure_reasons": failure_reasons,
        "verdict": verdict,
        "gate_pass": verdict != "WORSE",
        # Informational telemetry only:
        "current_rate": cur_rate,
        "current_candidates": cur_n,
        "new_rate": new_rate,
        "new_candidates": new_n,
        "delta": new_rate - cur_rate,
        "churn": churn,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--current", required=True, help="cos_replay.py run file — the baseline")
    ap.add_argument("--new", required=True, help="cos_replay.py run file — the change under test")
    ap.add_argument("--expected", default=None,
                    help="ground-truth verdicts JSON (optional if embedded in accuracy.rows)")
    ap.add_argument("--band", type=float, default=DEFAULT_NOISE_BAND,
                    help=f"non-inferiority band on ground-truth metrics "
                         f"(default {DEFAULT_NOISE_BAND} — s03's measured "
                         f"verdict-disagreement rate)")
    args = ap.parse_args(argv)

    band_err = _validate_band(args.band)
    if band_err:
        print(f"ERROR: {band_err}")
        print("This is NOT a pass.")
        return 2

    try:
        current, new = _load(args.current), _load(args.new)
        expected = _load(args.expected) if args.expected else None
    except (OSError, ValueError) as exc:
        print(f"ERROR: failed to load JSON input: {exc}")
        print("This is NOT a pass.")
        return 2

    print("=" * 72)
    print(f"REPLAY REGRESSION GATE — ground-truth accuracy/precision/recall "
          f"(band = ±{args.band:.2%})")
    print("=" * 72)
    cur_sys = current.get("system") if isinstance(current, dict) else "<invalid>"
    new_sys = new.get("system") if isinstance(new, dict) else "<invalid>"
    print(f"  current: {cur_sys}  ({args.current})")
    print(f"  new:     {new_sys}  ({args.new})")

    if not isinstance(current, dict) or not isinstance(new, dict):
        err = _validate_run(current, "current") or _validate_run(new, "new")
        print(f"\nERROR: {err}")
        print("This is NOT a pass.")
        return 2

    r = evaluate(current, new, band=args.band, expected=expected)
    if r["error"]:
        print(f"\nERROR: {r['error']}")
        print("This is NOT a pass.")
        return 2

    cm, nm, d = r["current_metrics"], r["new_metrics"], r["deltas"]
    print(f"\n  paired judged rows: n={r['n']}"
          + (f"  [LOW CONFIDENCE — band was measured at n={LOW_CONFIDENCE_N} per body]"
             if r["low_confidence"] else ""))
    print(f"  accuracy:       current={cm['accuracy']:.4f} ({cm['agree']}/{cm['total']})  "
          f"new={nm['accuracy']:.4f} ({nm['agree']}/{nm['total']})  "
          f"delta={d['accuracy']:+.4f}")
    print(f"  precision:      current={cm['precision']:.4f} (tp={cm['tp']}, fp={cm['fp']})  "
          f"new={nm['precision']:.4f} (tp={nm['tp']}, fp={nm['fp']})  "
          f"delta={d['precision']:+.4f}")
    print(f"  recall:         current={cm['recall']:.4f} (tp={cm['tp']}, fn={cm['fn']})  "
          f"new={nm['recall']:.4f} (tp={nm['tp']}, fn={nm['fn']})  "
          f"delta={d['recall']:+.4f}")
    print(f"  f1:             current={cm['f1']:.4f}  new={nm['f1']:.4f}  "
          f"delta={d['f1']:+.4f}")
    if nm["refuse_total"] > 0:
        print(f"  refusal floor:  current={cm['refuse_ok']}/{cm['refuse_total']}  "
              f"new={nm['refuse_ok']}/{nm['refuse_total']}")
    print(f"  candidate rate (telemetry only): "
          f"current={r['current_rate']:.4f} ({r['current_candidates']}/{r['n']})  "
          f"new={r['new_rate']:.4f} ({r['new_candidates']}/{r['n']})  "
          f"delta={r['delta']:+.4f}")

    if r["churn"]:
        print(f"\n  disposition changed on {len(r['churn'])} of {r['n']} row(s):")
        for c in r["churn"]:
            print(f"    {c['conversation_id']}: "
                  f"{c['current_disposition']} -> {c['new_disposition']}")
    else:
        print("\n  disposition unchanged on every paired row.")

    if r["failure_reasons"]:
        print("\n  gate failure(s):")
        for reason in r["failure_reasons"]:
            print(f"    - {reason}")

    print()
    print("-" * 72)
    print(f"VERDICT: {r['verdict']}")
    if r["gate_pass"]:
        print("GATE: PASS — not a regression.")
        return 0
    print("GATE: FAIL — regression detected on ground-truth metrics.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
