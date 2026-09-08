#!/usr/bin/env python3
"""Why is a `read` thread still in the Inbox? — the residue, cause by cause.

THE PROMISE IS "zero threads whose latest verdict is `read` or `noise`", and
until this existed the only way to ask why it was not met was to read a night's
ledger by hand. Point it at a run and it answers with the FIRST condition that
refused each surviving thread, off the REAL belts (`cos_chips.p0_floor_refuses`,
`cos_mutate_plan_aged.aged_read_refusal`) rather than a second reading of them —
a census that re-implements the rules can be right about the rules and wrong
about the night.

    python3 tools/cos_read_census.py --vault <vault>            # newest run
    python3 tools/cos_read_census.py --vault <vault> --run 2026-09-05-run260
    python3 tools/cos_read_census.py --vault <vault> --json out.json
    python3 tools/cos_read_census.py --selfcheck   # prove it can say NON-ZERO

MEASURED 2026-09-05 on 2026-09-05-run260: 53 `read` rows, 8 archived, 45 left —
19 of them cleared every host belt and were never CLAIMED by the judge, 10 wait
on Rule 1's substance gate, 8 never opened a body, 6 carry an unsent draft and
2 were P0. So the leading hypothesis (the body-opened refusal) is real and is
18% of it; no single cause dominates, and no code change alone empties the box.

`signed_ingested` is the expensive input (~15s against a live vault: it walks
the catching-up runs). `--no-signed` skips it and OVER-reports the Rule-1
bucket, which is the safe direction for a census — never the reverse.

ponytail: no config, no registry. One run in, one table out.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brain.cos_chips import p0_floor_refuses          # noqa: E402
from cos_mutate_plan_aged import aged_read_refusal    # noqa: E402

#: The order IS the answer: each thread is attributed to the FIRST condition
#: that refused it, exactly as the belts run, so the buckets sum to the total.
CAUSES = ("p0-floor", "unsent-draft", "body-never-opened",
          "rule1-substance-gate", "never-claimed-by-the-judge")


def newest_run(ops: Path) -> str:
    led = sorted(ops.glob("_cos_ingestion_ledger_*.jsonl"))
    if not led:
        raise SystemExit(f"no ingestion ledger under {ops}")
    return led[-1].name[len("_cos_ingestion_ledger_"):-len(".jsonl")]


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def cause_of(row: dict[str, Any], signed: set[str]) -> str:
    """The FIRST belt that refuses this row an aged-read archive."""
    if p0_floor_refuses(row.get("verdict"), row.get("tier")) \
            or p0_floor_refuses(row.get("verdict"), row.get("judged_tier")):
        return "p0-floor"
    probe = dict(row, noise_signal="aged-read-no-action")
    why = aged_read_refusal(probe, signed)
    if why is None:
        return "never-claimed-by-the-judge"
    if "draft census" in why:
        return "unsent-draft"
    if "body never opened" in why:
        return "body-never-opened"
    if "no-substance" in why:
        return "rule1-substance-gate"
    return "other:" + why.split("—")[0].strip()[:48]


def census(vault: Path, run_id: str, *, signed: set[str] | None = None
           ) -> dict[str, Any]:
    ops = vault / "cos-ops"
    rows = _rows(ops / f"_cos_ingestion_ledger_{run_id}.jsonl")
    undo = _rows(ops / f"_cos_undo_ledger_{run_id}.jsonl")
    archived = {u["conversation_id"] for u in undo if u.get("verb") == "archive"}
    chipped = {u["conversation_id"] for u in undo
               if u.get("verb") == "categorize"
               and str(u.get("chip") or "").startswith("P")}
    sig = set(signed or ())
    residue = [r for r in rows if r.get("verdict") in ("read", "noise")
               and r["conversation_id"] not in archived]
    by_cause: dict[str, int] = {}
    for r in residue:
        c = cause_of(r, sig)
        by_cause[c] = by_cause.get(c, 0) + 1
    # A thread with NO chip that is also not leaving is the second half of the
    # inbox-zero promise, and it has its own cause: every lane — the chip lane
    # included — refuses a row that carries no verdict at all.
    nochip = [r for r in rows if not r.get("tier")
              and r["conversation_id"] not in archived
              and r["conversation_id"] not in chipped]
    return {
        "run_id": run_id,
        "ledger_rows": len(rows),
        "archived_this_run": len(archived),
        "read_or_noise_rows": len([r for r in rows
                                   if r.get("verdict") in ("read", "noise")]),
        "read_or_noise_remaining": len(residue),
        "remaining_by_cause": dict(sorted(by_cause.items())),
        "unchipped_remaining": len(nochip),
        "unchipped_reasons": sorted({
            "judgment_pending — the judge returned no verdict for this row, "
            "and every lane refuses a row with no verdict"
            if r.get("judgment_pending") else
            f"verdict={r.get('verdict')!r} read_state={r.get('read_state')!r}"
            for r in nochip}),
        "signed_ingested_supplied": signed is not None,
    }


def _selfcheck() -> int:
    """The known positive: a residue this census MUST report, cause by cause."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ops = Path(td) / "cos-ops"
        ops.mkdir(parents=True)
        base = {"read_state": "read", "body_opened": True,
                "received": "2026-01-01T00:00:00+00:00",
                "disposition": "no-substance", "judged_tier": "P3",
                "tier": "P3"}
        rows = [
            dict(base, conversation_id="a", verdict="read", tier="P0",
                 judged_tier="P0"),                      # p0-floor? no: `read`
            dict(base, conversation_id="b", verdict="act", tier="P0",
                 judged_tier="P0"),                      # not read/noise
            dict(base, conversation_id="c", verdict="read", isDraft=True),
            dict(base, conversation_id="d", verdict="read", body_opened=False),
            dict(base, conversation_id="e", verdict="read",
                 disposition="candidate"),
            dict(base, conversation_id="f", verdict="noise"),
            dict(base, conversation_id="g", verdict="read"),
            {"conversation_id": "h", "judgment_pending": True},
        ]
        (ops / "_cos_ingestion_ledger_R.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")
        (ops / "_cos_undo_ledger_R.jsonl").write_text(
            json.dumps({"verb": "archive", "conversation_id": "g"}) + "\n")
        got = census(Path(td), "R")
    want = {"unsent-draft": 1, "body-never-opened": 1,
            "rule1-substance-gate": 1, "never-claimed-by-the-judge": 2}
    assert got["remaining_by_cause"] == want, got["remaining_by_cause"]
    assert got["read_or_noise_remaining"] == 5, got
    assert got["archived_this_run"] == 1, got
    # `h` carries no chip AND no verdict — the second half of the promise.
    assert got["unchipped_remaining"] == 1, got
    assert "judgment_pending" in got["unchipped_reasons"][0], got
    # and the P0 floor really is lifted for `read` and really does bind on `act`
    assert not p0_floor_refuses("read", "P0")
    assert p0_floor_refuses("act", "P0")
    print("cos_read_census selfcheck OK")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", type=Path)
    ap.add_argument("--run", default=None)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--no-signed", action="store_true",
                    help="skip the signed-ingested join (~15s); over-reports "
                         "the Rule-1 bucket, never under-reports it")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)
    if a.selfcheck:
        return _selfcheck()
    if not a.vault:
        ap.error("--vault is required")
    run_id = a.run or newest_run(a.vault / "cos-ops")
    signed = None
    if not a.no_signed:
        from brain import cos                                 # noqa: PLC0415
        rows = _rows(a.vault / "cos-ops"
                     / f"_cos_ingestion_ledger_{run_id}.jsonl")
        signed = cos.signed_ingested_catching_up(a.vault, run_id, rows)
    out = census(a.vault, run_id, signed=signed)
    text = json.dumps(out, indent=2)
    if a.json:
        a.json.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
