#!/usr/bin/env python3
"""What share of this mailbox does the category layer actually cover? (s09)

WHY THIS EXISTS. `cos_driver.py` states an honest deviation: no threshold on
the gate's excluded share, "because nothing has ever measured what share of
this mailbox a FULL pre-draw pass calls `never`". The cited backstop is
`cos_runverify._CATEGORY_DOMINANCE_MAX_SHARE` (0.75), and TWO facts about it
were asserted rather than measured:

  1. its `share` is `max(count per category among STAMPED rows) / ALL in-scope
     rows` — an asymmetric fraction. Until s08 only the ~20 body-opened rows
     of ~228 carried a category, so the numerator could not exceed ~9 % of the
     denominator and the 0.75 bar was UNREACHABLE BY CONSTRUCTION. Stamping
     every enumerated row makes a dormant FAIL path live on the first armed
     night, and an INVALID run quarantines its candidates.
  2. its calibration ("every honest night 0.20-0.33, every blanket-default
     night 0.81-0.90") was recorded on nights whose stamp coverage nobody
     stated. A bar calibrated on a 9 %-coverage denominator does not
     automatically discriminate on a 100 %-coverage one.

So this reads the runs that already happened and reports both numbers with
their denominators. It reads ledgers only — no mailbox, no browser, no model.

    python3 tools/cos_category_share_report.py <vault> [--out r.json]

COUNTS, NEVER CONTENT. Category ids are the owner's own taxonomy words and are
printed; subjects, senders and bodies are never read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain import cos, cos_runverify as rv                        # noqa: E402

#: `check_category_stamp`'s own exclusion: marker rows are not in scope.
_MARKER = getattr(rv, "_MARKER_DISPOSITION", None)


def _never_ids(vault: Path) -> set[str]:
    rules = (cos.ingest_taxonomy(vault) or {}).get("rules") or {}
    return {cid for cid, r in rules.items()
            if str((r or {}).get("disposition") or "").strip().lower() == "never"}


def one_run(vault: Path, run_id: str, never: set[str]) -> dict:
    path = cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl"
    rows = [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines() if x.strip()]
    scored = [r for r in rows if r.get("disposition") != _MARKER]
    stamped = [r for r in scored if r.get("category") is not None]
    # THE NUMBER THE DEVIATION SAYS NOBODY HAS. On a FULL-COVERAGE night every
    # in-scope row carries a stamp, so the share the pre-draw gate would have
    # excluded is computable exactly — no live pass required. On a partial
    # night it is not, and the coverage figure beside it says so.
    never_rows = sum(1 for r in stamped
                     if str(r.get("category")).strip() in never)
    counts: dict[str, int] = {}
    for r in stamped:
        c = str(r.get("category")).strip()
        counts[c] = counts.get(c, 0) + 1
    top, top_n = (max(counts.items(), key=lambda kv: kv[1]) if counts
                  else ("<none>", 0))
    return {
        "run_id": run_id,
        "rows_total": len(rows),
        "in_scope_rows": len(scored),
        "stamped_rows": len(stamped),
        "stamp_coverage": round(len(stamped) / len(scored), 4) if scored else 0.0,
        "dominant_category": top,
        "dominant_count": top_n,
        # THE SHIPPED FRACTION, exactly as `check_category_stamp` computes it.
        "share_as_the_check_computes_it":
            round(top_n / len(scored), 4) if scored else 0.0,
        # THE SAME DOMINANCE, over the rows that actually carry a stamp. This
        # is what the shipped fraction CONVERGES TO once coverage reaches 1.0,
        # which is exactly what s08 changed.
        "share_over_stamped_rows":
            round(top_n / len(stamped), 4) if stamped else 0.0,
        "scored_at_all": len(scored) >= rv._CATEGORY_DOMINANCE_MIN_ROWS,
        "would_fail_today":
            len(scored) >= rv._CATEGORY_DOMINANCE_MIN_ROWS
            and (top_n / len(scored) if scored else 0)
            > rv._CATEGORY_DOMINANCE_MAX_SHARE,
        "would_fail_at_full_coverage":
            len(scored) >= rv._CATEGORY_DOMINANCE_MIN_ROWS
            and (top_n / len(stamped) if stamped else 0)
            > rv._CATEGORY_DOMINANCE_MAX_SHARE,
        "category_histogram": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        # --- the gate's own excluded share, where it can be computed ---
        "never_rows": never_rows,
        "excluded_share_over_in_scope":
            round(never_rows / len(scored), 4) if scored else 0.0,
        "excluded_share_is_exact": len(stamped) == len(scored) and bool(scored),
    }


def report(vault: Path) -> dict:
    ops = cos.run_ops_dir(vault)
    never = _never_ids(vault)
    runs = sorted(p.name[len("_cos_ingestion_ledger_"):-len(".jsonl")]
                  for p in ops.glob("_cos_ingestion_ledger_*.jsonl"))
    per = []
    for rid in runs:
        try:
            per.append(one_run(vault, rid, never))
        except (OSError, ValueError) as exc:
            per.append({"run_id": rid, "unreadable": str(exc)[:160]})
    usable = [r for r in per if r.get("scored_at_all")]
    exact = [r for r in per if r.get("excluded_share_is_exact")]
    shares = sorted(r["excluded_share_over_in_scope"] for r in exact)
    return {
        "never_category_ids": sorted(never),
        "runs_with_exact_excluded_share": len(exact),
        "excluded_share_min": shares[0] if shares else None,
        "excluded_share_median": shares[len(shares) // 2] if shares else None,
        "excluded_share_max": shares[-1] if shares else None,
        "excluded_share_by_run": {r["run_id"]: r["excluded_share_over_in_scope"]
                                  for r in exact},
        "bar": rv._CATEGORY_DOMINANCE_MAX_SHARE,
        "min_rows_before_the_bar_is_scored": rv._CATEGORY_DOMINANCE_MIN_ROWS,
        "runs_examined": len(per),
        "runs_the_bar_actually_scores": len(usable),
        "max_stamp_coverage_ever_recorded":
            max((r["stamp_coverage"] for r in usable), default=0.0),
        "max_share_as_the_check_computes_it":
            max((r["share_as_the_check_computes_it"] for r in usable), default=0.0),
        "max_share_over_stamped_rows":
            max((r["share_over_stamped_rows"] for r in usable), default=0.0),
        "runs_that_would_fail_today":
            [r["run_id"] for r in usable if r["would_fail_today"]],
        "runs_that_would_fail_at_full_coverage":
            [r["run_id"] for r in usable if r["would_fail_at_full_coverage"]],
        "per_run": per,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("vault", type=Path)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv[1:])
    res = report(args.vault.expanduser().resolve())
    text = json.dumps(res, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
