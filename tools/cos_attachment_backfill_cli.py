#!/usr/bin/env python3
"""The ATT-04 backfill's command line. Split from `cos_attachment_backfill.py`
only to keep that file under the 500-LOC bound — the seam is the repo's own
`cos_driver` / `cos_driver_cli` pattern, and every verb below is one call into
the library beside it."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cos_attachment_backfill import (  # noqa: E402
    apply_batch, owed_files, plan_batch, verify_recovered)


def _report(plan: dict[str, Any]) -> str:
    lines = [f"as-of run: {plan['as_of_run']}",
             f"owed (shortfall census): {plan['owed']}",
             f"fetchable now (batch):  {plan['counts']['batch']} file(s) "
             f"across {plan['counts']['conversations']} conversation(s), "
             f"~{plan['counts']['approx_bytes'] / 1e6:.1f} MB",
             f"not fetchable (residual): {plan['counts']['residual']}"]
    for reason, n in plan["counts"]["residual_by_reason"].items():
        lines.append(f"    {reason}: {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", default=os.environ.get("BRAIN_VAULT"), required=False)
    sub = ap.add_subparsers(dest="cmd", required=True)

    census = sub.add_parser("census", help="the shortfall, with no enumeration join")
    census.add_argument("--json", default=None)

    plan = sub.add_parser("plan", help="build the fetch batch against one run")
    plan.add_argument("--against", required=True, help="enumeration run id")
    plan.add_argument("--json", default=None, help="write the plan here")

    ap_apply = sub.add_parser("apply", help="LIVE: fetch the planned batch and sweep")
    ap_apply.add_argument("--plan", required=True)
    ap_apply.add_argument("--run-id", required=True, help="this backfill's own run id")
    ap_apply.add_argument("--verify-run", default=None,
                          help="a fresh ingestion-ledger run id to re-read from")
    ap_apply.add_argument("--verify-enumeration", default=None,
                          help="a `cos_driver.py --enumerate-only --out` JSON "
                               "to re-read from (cheaper; preferred)")
    ap_apply.add_argument("--json", default=None)

    ver = sub.add_parser("verify", help="resolve signed note ids for a result")
    ver.add_argument("--result", required=True, help="an `apply` result JSON")
    ver.add_argument("--json", default=None)

    args = ap.parse_args(argv)
    if not args.vault:
        print("--vault (or $BRAIN_VAULT) is required", file=sys.stderr)
        return 2
    vault = Path(args.vault).expanduser()

    if args.cmd == "census":
        out: dict[str, Any] = {"owed": owed_files(vault)}
        out["count"] = len(out["owed"])
        print(json.dumps(out, indent=2) if not args.json else f"owed: {out['count']}")
    elif args.cmd == "plan":
        out = plan_batch(vault, args.against)
        print(_report(out))
    elif args.cmd == "apply":
        loaded = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        out = apply_batch(vault, args.run_id, loaded,
                          verify_run=args.verify_run,
                          verify_enumeration=args.verify_enumeration)
        print(f"recovered {len(out['recovered'])}, "
              f"residual {len(out['residual'])}")
    else:
        loaded = json.loads(Path(args.result).read_text(encoding="utf-8"))
        rows = verify_recovered(vault, loaded.get("recovered") or [])
        out = {"recovered": rows,
               "signed": sum(1 for r in rows if r.get("note_id")),
               "awaiting_acceptance": sum(1 for r in rows if not r.get("note_id"))}
        print(f"signed {out['signed']}, awaiting acceptance "
              f"{out['awaiting_acceptance']}")

    if getattr(args, "json", None):
        Path(args.json).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
