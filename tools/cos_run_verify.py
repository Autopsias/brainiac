#!/usr/bin/env python3
"""Score a COS run against its OWN artifacts — the host validator, standalone.

WHY THIS EXISTS. Run 59 (2026-07-31) skipped its entire self-eval — zero
E-check output across 16 artifacts — and nothing noticed, because the only
thing checking a run's homework was the run itself. E16, written to catch a
candidate with no stamps, never executed. Doctrine cannot police itself.

The judgment lives in ``brain.cos_runverify`` (so the hourly broker fold runs
exactly this, with no second copy of the rules); this file is the operator
probe over it.

    python3 tools/cos_run_verify.py <vault>                  # every recent run
    python3 tools/cos_run_verify.py <vault> --run-id 2026-07-31-run59
    python3 tools/cos_run_verify.py <vault> --json
    python3 tools/cos_run_verify.py <vault> --record         # write the verdicts
    python3 tools/cos_run_verify.py --selfcheck              # prove it can FAIL

Read-only unless ``--record`` is passed: recording the verdict is what GATES
claiming (an INVALID/INCONCLUSIVE run's candidates are quarantined, never
bound), and that belongs to the hourly fold, not to an ad-hoc probe.

Exit 0 = every scored run is claimable (VALID / VALID_DEGRADED) or still
pending. Exit 1 = at least one run is INVALID or INCONCLUSIVE. Exit 2 = usage.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brain import cos, cos_runverify  # noqa: E402


def _render(res: dict) -> str:
    head = (f"{res['run_id']}: {res['verdict'] or 'PENDING'}"
            + (f" — {res['reason']}" if res.get("reason") else ""))
    lines = [head]
    for c in res.get("checks", []):
        mark = {"pass": "ok  ", "degraded": "deg ", "fail": "FAIL",
                "inconclusive": "??  "}.get(c["status"], "?   ")
        reexec = "re-executed" if c["reexecuted"] else "read-only"
        lines.append(f"  [{mark}] {c['check']} ({reexec}): {c['detail']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("vault", type=Path, nargs="?")
    p.add_argument("--run-id", help="score exactly this run (default: recent runs)")
    p.add_argument("--window", type=int, default=cos_runverify.DEFAULT_RUN_WINDOW)
    p.add_argument("--quiesce-seconds", type=int, default=None,
                   help="override the 'run has stopped writing' window")
    p.add_argument("--record", action="store_true",
                   help="write the verdicts (this is what gates claiming)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selfcheck", action="store_true")
    args = p.parse_args(argv[1:])

    if args.selfcheck:
        return _selfcheck()
    if args.vault is None:
        p.error("a vault path is required (or --selfcheck)")
    vault = args.vault.expanduser().resolve()
    if not vault.is_dir():
        print(f"INCONCLUSIVE: no vault at {vault}", file=sys.stderr)
        return 1

    if args.record and not args.run_id:
        report = cos_runverify.verify_pending_runs(
            vault, window=args.window, quiesce_seconds=args.quiesce_seconds)
        print(json.dumps(report, indent=2) if args.json else
              "\n".join(f"{s['run_id']}: {s['verdict']} — {s['reason']}"
                        for s in report["scored"]) or "nothing newly scored")
        return 1 if (report["invalid"] or report["inconclusive"]) else 0

    run_ids = ([args.run_id] if args.run_id
               else cos_runverify.known_run_ids(vault)[:args.window])
    if not run_ids:
        print(f"INCONCLUSIVE: no host run manifests under {cos.runs_dir(vault)} "
              "— nothing to validate, which is not the same as everything "
              "passing", file=sys.stderr)
        return 1
    results = [cos_runverify.verify_run(vault, r,
                                        quiesce_seconds=args.quiesce_seconds)
               for r in run_ids]
    if args.record:
        for res in results:
            if res["verdict"] is not None:
                cos.record_run_validity(
                    vault, res["run_id"], res["verdict"], reason=res["reason"],
                    detail={"inputs_digest": res["inputs_digest"],
                            "checks": res["checks"]})
    print(json.dumps(results, indent=2) if args.json else
          "\n\n".join(_render(r) for r in results))
    return 1 if any(r["verdict"] in (cos.RUN_INVALID, cos.RUN_INCONCLUSIVE)
                    for r in results) else 0


def _selfcheck() -> int:
    """Prove the validator can actually FAIL before anyone trusts it.

    A gate nobody has watched fail is not a gate — that is the whole reason
    this file exists. The full known-positive matrix (runs 57/58/59's REAL
    artifacts, and each sub-check fired independently) lives in
    ``tests/test_cos_runverify.py``; this is the 20-second version."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        ops = cos.run_ops_dir(vault)
        ops.mkdir(parents=True)
        skill = vault / "SKILL.md"
        skill.write_text('kernel_version: "v9.9"\nextraction_rules_version: "ext-9"\n'
                         "- **E1** · first\n- **E2** · second\n", encoding="utf-8")
        rid = "2026-07-31-run99"
        cos.write_run_manifest(vault, run_id=rid, skill_path=skill)
        for name in (f"_cos_nightly_{rid}.md", f"_cos_ingestion_ledger_{rid}.jsonl",
                     f"cos_contract_pre_{rid}.json", "_cos_metrics.jsonl"):
            (ops / name).write_text("", encoding="utf-8")

        res = cos_runverify.verify_run(vault, rid, quiesce_seconds=0)
        assert res["verdict"] == cos.RUN_INVALID, res
        checks = {c["check"]: c["status"] for c in res["checks"]}
        assert checks["self_eval"] == "fail", checks
        assert checks["metrics_row"] == "fail", checks
        assert checks["contract"] == "fail", checks

        # ... and that it stops failing once the run does its homework is what
        # the pytest matrix proves; here, only that an EMPTY run cannot pass.
        (ops / f"_cos_nightly_{rid}.md").write_text(
            "- E1: PASS — a\n- E2: PASS — b\n", encoding="utf-8")
        after = cos_runverify.verify_run(vault, rid, quiesce_seconds=0)
        assert {c["check"]: c["status"] for c in after["checks"]}["self_eval"] == "pass"
    print("selfcheck OK — the run validator fires on a known-positive, and the "
          "self-eval check clears once the block is there")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
