#!/usr/bin/env python3
"""Replay a RECORDED night's draw through the category gate, offline.

WHY THIS EXISTS. `body_draw`'s `exclude` parameter is rule 1¾'s gate and it had
never once been fed: the category is a MODEL judgment and the model ran after
every body was already open, so `category_gate.state` read `not-run` on every
run ever scored. The fix moves the category batch BEFORE the draw — and the
claim that this recovers real body opens has to be MEASURED on real nights, not
asserted from the code that makes it.

So this takes a night that already happened, the categories that night's own
model actually stamped (off its ingestion ledger), and the enumeration it
actually saw (off its capture corpus), and runs the REAL `resolve_never` and
`body_draw` over them. It answers one question: with the gate armed, how many of
that night's twenty opens would have gone to material the owner's taxonomy says
never to keep — and how many opens does closing it hand back?

It reads only. It touches no mailbox, no browser and no ledger.

    python3 tools/cos_category_gate_replay.py <vault> --run-id 126
    python3 tools/cos_category_gate_replay.py <vault> --run-id 126 --out r.json

THE OUTPUT CARRIES COUNTS, NEVER CONTENT. The corpus is real mail bodies at
MNPI and the ids name live threads; a file written where git can reach it holds
neither.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_driver as drv                                          # noqa: E402
from brain import cos, cos_corpus                                 # noqa: E402


def _resolve(vault: Path, run_id: str) -> str:
    """`--run-id 126` and the full host id are the same vocabulary here."""
    want = run_id.strip()
    if not want.isdigit():
        return want
    ops = cos.run_ops_dir(vault)
    for path in sorted(ops.glob("_cos_ingestion_ledger_*.jsonl")):
        rid = path.name[len("_cos_ingestion_ledger_"):-len(".jsonl")]
        if rid.rsplit("run", 1)[-1] == want:
            return rid
    return want


def replay(vault: Path, run_id: str, *, cap: int = drv.BODY_OPEN_CAP) -> dict:
    ledger = (cos.run_ops_dir(vault)
              / f"_cos_ingestion_ledger_{run_id}.jsonl")
    rows = [json.loads(x) for x in
            ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
    corpus = {r["conversation_id"]: r for r in cos_corpus.read_corpus(vault, run_id)}

    # The categories THAT NIGHT'S OWN MODEL stamped. Not a fixture, not a
    # re-judgment: the run's ledger is the only place its verdicts survive.
    categories = {r["conversation_id"]: str(r.get("category") or "")
                  for r in rows if r.get("category")}
    gate = drv.resolve_never(vault, categories)

    # The enumeration that night saw, rebuilt from its capture corpus — the
    # same reconstruction `cos_driver.accounting_from_corpus` uses, so the draw
    # replayed here is the draw the driver would have computed.
    items = []
    for r in rows:
        ext = (corpus.get(r["conversation_id"]) or {}).get("extraction") or {}
        items.append({
            "convId": r["conversation_id"],
            "itemId": ext.get("message_id") or r.get("message_id"),
            "isRead": (ext.get("read_state") or r.get("read_state")) == "read",
            "categories": [k for k, v in drv.CHIP_TIER.items()
                           if v == (ext.get("tier") or r.get("tier"))],
            "received": ext.get("received") or r.get("received"),
        })
    convs = drv.conversations(items)

    ungated = {d["convId"] for d in drv.body_draw(convs, cap)}
    gated = {d["convId"] for d in drv.body_draw(convs, cap,
                                                exclude=frozenset(gate["excluded"]))}
    # GROUND TRUTH, independent of the replay: what the night's own ledger says
    # it opened. If the ungated replay and the ledger disagree, the replay is
    # not reproducing that night's draw and its recovery number means nothing.
    ledger_opened = {r["conversation_id"] for r in rows if r.get("body_opened")}
    ledger_never_opens = sum(
        1 for r in rows
        if r.get("body_opened") and str(r.get("category") or "") in gate["never_ids"])

    return {
        "run_id": run_id,
        "vault_rows": len(rows),
        "taxonomy_mode": gate["mode"],
        "never_categories": gate["never_ids"],
        "categories_stamped": gate["categorised"],
        "undefined_categories": gate["undefined_categories"],
        "rows_the_gate_excludes": len(gate["excluded"]),
        "draw_cap": cap,
        "ungated_draw": len(ungated),
        "ungated_draw_matches_the_ledger": sorted(ungated) == sorted(ledger_opened),
        "never_opens_ungated": len(ungated & gate["excluded"]),
        "never_opens_ledgered": ledger_never_opens,
        "never_opens_gated": len(gated & gate["excluded"]),
        "opens_recovered_for_actionable_material": len(gated - ungated),
        "gate_state_ungated": "not-run",
        "gate_state_gated": "armed",
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("vault", type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--cap", type=int, default=drv.BODY_OPEN_CAP)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv[1:])

    vault = args.vault.expanduser().resolve()
    res = replay(vault, _resolve(vault, args.run_id), cap=args.cap)
    text = json.dumps(res, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    # A replay whose ungated draw does not reproduce the night's own opens is
    # not evidence about that night, and says so with an exit code.
    return 0 if res["ungated_draw_matches_the_ledger"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
