#!/usr/bin/env python3
"""The COS driver — a night's books, written by code (REST-02, 2026-08-10).

WHAT THIS IS. Everything a COS run must ACCOUNT FOR — the census, the ledgers,
the capture corpus, the metrics row, the outcome contract — produced by
deterministic code with no model anywhere in the path. The model's remaining job
is JUDGMENT (what matters, what to hold, what to stage, what to draft), and this
driver leaves exactly those slots null for it (s03).

WHY IT EXISTS. Runs 100-112 were a model executing browser mechanics from a
6,000-line constitution, and what failed was never judgment: it was miscounted
funnels, invented ledger vocabulary, a fabricated run ledger (run 64), a
metrics row that disagreed with its own ledger (runs 64/105/108/111), and a body
pass that wedged Chrome's evaluation bridge (run 112). Code cannot invent a
`held_reason`, cannot miscount a set it just built, and cannot claim a read it
did not make — so the mechanics move here.

    brain cos-run-begin --lane codex-automation      # the HOST stamps the sheet
    python3 tools/cos_driver.py --stage --tab-id <id>   # prints ONE line…
    #   …evaluate that line in the tab's MAIN world (a browser extension), which
    #   boots the page driver and seals the captured envelope where it was found
    python3 tools/cos_driver.py --vault <vault> --tab-id <chrome tab id>
    python3 tools/cos_driver.py --vault <vault> --replay <run-id>
    python3 tools/cos_driver.py --selfcheck

THE SEEDING STEP IS NOT CEREMONY. `osascript` — this file's transport — evaluates
in an ISOLATED world, a separate JS heap on the same document, so a capture hook
installed from here sees none of the app's traffic and a request issued from here
carries no `authorization`. The auth-bearing half therefore lives in the page's
MAIN world and never leaves it; `--stage` puts the page driver where that world
can reach it, and `#__cos_in`/`#__cos_out` (two hidden divs) are the only channel
between the two. Full measurement: `_evidence/s02/read-lane-seed-blocked.md`.

THE TWO HALVES, and the seam between them is the determinism claim:

  CAPTURE   drives the signed-in tab (DOM scan + service.svc FindItem/GetItem)
            and persists every raw response into the HOST-ONLY capture corpus.
  ACCOUNTING is a PURE function of that capture. `--replay <run-id>` rebuilds
            the ledgers and the metrics row from the corpus alone, in a separate
            process, and the two must be byte-identical.

Read-only by construction: the only verbs it can issue are `FindItem` and
`GetItem`, it never dispatches a click, and it refuses to fetch a message that
is not already read. `tests/test_cos_driver.py` asserts each of those against
the source of this file and of `tools/cos_driver_page.js`.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cos_driver_accounting import (  # noqa: E402,F401  batch-2 drain
    BUCKET_RESIDENT, CONTRACT, READ_LANE, _persist, accounting_from_corpus,
    body_open_succeeded,
    build_accounting, build_contract_inputs, corpus_extraction, run_contract,
    run_host_checks, write_corpus, write_jsonl, write_report)
from cos_driver_categories import (  # noqa: E402,F401
    category_gate_state, category_row_stamps, load_categories, resolve_never)
from cos_driver_cli import night_argparser, validate_categories_mode  # noqa: E402
from cos_driver_completeness import (  # noqa: E402,F401
    SET_DIFFERENCE_TOLERANCE, assert_complete, completeness)
from cos_driver_enumeration import (  # noqa: E402,F401
    ENUMERATION_FIELDS, bind_categories, enumerate_only, enumeration_row,
    row_digest)
from cos_driver_draw import (  # noqa: E402,F401
    AMBIGUOUS_TIER_CHIPS, CHIP_TIER, TIER_SOURCE_PRIORITY_CHIP,
    TIER_SOURCE_READ_CHIP, _draw_rank, _observed_chip, _tier, _tier_source,
    body_draw, conversations, starvation_stop)
from cos_driver_gate import gate_evidence_block, gate_scope_and_exclusions  # noqa: E402
from cos_driver_night_records import (  # noqa: E402
    fixture_ref, night_evidence_skeleton, replay_determinism,
    write_night_artifacts)
from cos_driver_selfcheck import selfcheck  # noqa: E402,F401
from cos_driver_transport import (  # noqa: E402,F401
    BODY_BUDGET, BODY_BUDGET_CHARS, BODY_OPEN_CAP, BOOTSTRAP, CHUNK, CdpTab,
    EgoTab, IN_ID, OUT_ID, PAGE_JS, SRC_ID, DriverStop, ChromeTab, _PARTIAL,
    _await_run, _fresh_node, _read_out, _start, _ts, _utcnow,
    assert_ready, bootstrap_for, capture_bodies, capture_night, load_sheet,
    open_tab, short, stage)

#: Fields excluded from the determinism diff, and why. Everything else in the
#: ledger and the metrics row is a function of the capture alone.
DIFF_EXCLUDED = {
    "run_ts": "the wall clock of the append itself; the CAPTURE's own stamps "
              "(`enumerated_at`, per-row `ts`) are inside the diff",
    "bundle_version": "stamped by the host from the run manifest at append time",
    "extraction_rules_version": "stamped by the host from the run manifest",
    "skill_sha256": "stamped by the host from the run manifest",
}


def run_night(vault: Path, tab_id: int | None, *, cap: int,
              evidence_path: Path | None,
              poll_seconds: float = 3.0, max_wait: float = 900.0,
              exclude_convids: set[str] | None = None,
              categories: dict[str, str] | None = None,
              prior_enumeration: list[dict[str, Any]] | None = None,
              use_cdp: bool = False,
              use_ego: bool = False) -> dict[str, Any]:
    """A read-only night. A STOP is written to the evidence file, not swallowed:
    a night that stopped and a night that found nothing must never look alike."""
    try:
        return _run_night(vault, tab_id, cap=cap, evidence_path=evidence_path,
                          poll_seconds=poll_seconds, max_wait=max_wait,
                          exclude_convids=exclude_convids,
                          categories=categories,
                          prior_enumeration=prior_enumeration, use_cdp=use_cdp,
                          use_ego=use_ego)
    except DriverStop as exc:
        _persist(evidence_path, dict(_PARTIAL, stopped=str(exc),
                                     stopped_at=_ts(_utcnow())))
        raise


def _bodies_and_accounting(tab: Any, capture: dict[str, Any],
                           draw: list[dict[str, Any]],
                           evidence: dict[str, Any], cap: int,
                           now: _dt.datetime, poll_seconds: float,
                           max_wait: float, vault: Path, run_id: str,
                           manifest: dict[str, Any],
                           gate: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """Open the drawn bodies through the (patchable) parent `capture_bodies`,
    then build the accounting. Returns (accounting, enumerated_at,
    reported_at)."""
    capture["bodies"] = capture_bodies(tab, draw, poll_seconds=poll_seconds,
                                       max_wait=max_wait,
                                       window_start=capture["window_start"])
    succeeded = sum(1 for b in capture["bodies"] if body_open_succeeded(b))
    evidence["bodies_attempted"] = len(draw)
    evidence["bodies_succeeded"] = succeeded
    evidence["bodies_error"] = len(capture["bodies"]) - succeeded
    evidence["seed_kind"] = capture["enumeration"].get("seed_kind")

    enumerated_at = capture["enumeration"].get("at") or _ts(now)
    reported_at = _ts(_utcnow())
    accounting = build_accounting(
        capture, run_id=run_id,
        bundle_version=str(manifest.get("bundle_version") or ""),
        rules_version=str(manifest.get("extraction_rules_version") or ""),
        enumerated_at=enumerated_at, gate_excluded=gate["in_scope_excluded"])
    return accounting, enumerated_at, reported_at


def _metrics_contract_and_host_checks(ops: Path, vault: Path, run_id: str,
                                      report: dict[str, Any],
                                      accounting: dict[str, Any],
                                      reported_at: str,
                                      evidence: dict[str, Any],
                                      pre_path: Path, post_path: Path,
                                      recon: Any) -> None:
    """Append the metrics row (kept IN THIS FILE: a test pins the literal
    `"read_lane": READ_LANE` to the driver's own source), run the outcome
    contract, the host checks."""
    metrics_row = {
        "date": run_id[:10], "run": run_id.rsplit("run", 1)[-1], "run_id": run_id,
        "run_ts": reported_at, "run_profile": "full",
        "mail_triaged": report["enumerated_count"],
        "inbox_count": report["enumerated_count"],
        "marked": 0, "archived": 0, "captured": 0, "drafts_created": 0,
        "held_drafted": 0, "held_non_drafted": report["enumerated_count"],
        "stopped_by_guard": 0,
        "attachment_lane": "not-exercised",
        "body_open_cap": BODY_OPEN_CAP,
        "body_open_actual": accounting["body_open_actual"],
        "body_budget": BODY_BUDGET,
        "mutation_lane": "none-read-only",
        "mutation_toolset": "chrome-plugin",
        "read_lane": READ_LANE,
        **accounting["counters"],
    }
    prior = recon._rows(ops / "_cos_metrics.jsonl")
    siblings = [r for r in prior if (r.get("date"), str(r.get("run")))
                == (metrics_row["date"], metrics_row["run"])]
    if siblings:
        metrics_row[recon.SUPERSEDES] = str(siblings[-1].get("run_ts"))
    evidence["metrics_append"] = recon.append_metric(ops, metrics_row)
    (ops / f"_cos_metrics_row_{run_id}.json").write_text(
        json.dumps(metrics_row, indent=2) + "\n", encoding="utf-8")

    code, out = run_contract(ops, run_id, pre_path, post_path,
                             ops / f"cos_contract_block_{run_id}.json")
    evidence["contract"] = {"exit_code": code, "render": out[:2000]}

    host = run_host_checks(vault, run_id)
    evidence["host_checks"] = host
    evidence["host_checks_executed"] = host.get("executed", [])


def _run_night(vault: Path, tab_id: int | None, *, cap: int,
               evidence_path: Path | None,
               poll_seconds: float = 3.0, max_wait: float = 900.0,
               exclude_convids: set[str] | None = None,
               categories: dict[str, str] | None = None,
               prior_enumeration: list[dict[str, Any]] | None = None,
               use_cdp: bool = False,
               use_ego: bool = False) -> dict[str, Any]:
    from brain import cos                                        # noqa: PLC0415
    import cos_reconcile_metrics as recon                        # noqa: PLC0415

    now = _utcnow()
    sheet = load_sheet(vault)
    run_id = sheet["run_id"]
    manifest = sheet["manifest"]
    ops = cos.run_ops_dir(vault)
    raw_sources = len(list((vault / "raw").rglob("*.md")))
    evidence = night_evidence_skeleton(
        run_id, sheet["lane"], vault, ops, raw_sources, _ts(now), READ_LANE,
        DIFF_EXCLUDED)
    _PARTIAL.clear()
    _PARTIAL.update(evidence)

    tab, transport = open_tab(tab_id, use_cdp=use_cdp, use_ego=use_ego)
    evidence["driver_transport"] = transport
    capture = capture_night(tab, cap=cap, poll_seconds=poll_seconds,
                            max_wait=max_wait, now=now)
    report = completeness(capture)
    evidence["completeness"] = report
    assert_complete(report)

    convs = conversations(capture["enumeration"].get("items", []))
    # THE GATE, FED. `categories` is the pre-draw category batch's answer, one
    # stamp per conversation, judged by the model from typed fields alone. The
    # driver looks each id up in the OWNER's taxonomy and excludes the ones that
    # file dispositions `never` — it never decides a category itself. The
    # binding, the exclusions, the interlock, the shadow draw, and the whole
    # `category_gate` evidence block live in cos_driver_gate, called through
    # this module's own functions so a patched parent attribute still holds.
    gate = gate_scope_and_exclusions(
        vault, convs, cap, categories, prior_enumeration, exclude_convids,
        bind_categories=bind_categories, resolve_never=resolve_never,
        body_draw=body_draw, starvation_stop=starvation_stop,
        category_gate_state=category_gate_state, driver_stop=DriverStop)
    draw = gate["draw"]
    capture["draw"] = draw
    evidence["category_gate"] = gate_evidence_block(
        convs, gate["gate"], gate["gate_state"], gate["binding"],
        gate["in_scope_excluded"], gate["excluded"], gate["starved"],
        gate["starved_in_scope"], gate["ungated_draw"], gate["draw"])
    # THE BLINDED NIGHT KEEPS ITS NUMBERS (review 2026-08-13, round 5). The
    # starvation raise used to fire BEFORE this block was built, so the one
    # night the gate blinded the mailbox — the night an operator most needs
    # `excluded_share`, `arrivals_ungated` and the taxonomy's `never` ids to
    # tell a broken taxonomy from a quiet inbox — reported none of them. The
    # stop report is `dict(_PARTIAL, stopped=…)` and `_PARTIAL` was snapshotted
    # from `evidence` before any of this existed, so the block is pushed across
    # explicitly, the same way `seed_kind` is.
    _PARTIAL["category_gate"] = evidence["category_gate"]
    if gate["starved"]:
        raise DriverStop(gate["starved"])
    accounting, enumerated_at, reported_at = _bodies_and_accounting(
        tab, capture, draw, evidence, cap, now, poll_seconds, max_wait,
        vault, run_id, manifest, gate)

    artifacts = write_night_artifacts(
        vault, ops, run_id, capture, accounting, report, enumerated_at,
        reported_at, write_jsonl=write_jsonl, write_corpus=write_corpus,
        build_contract_inputs=build_contract_inputs,
        write_report=write_report)
    corpus = artifacts["corpus"]
    evidence["corpus"] = corpus
    pre_path, post_path = artifacts["pre_path"], artifacts["post_path"]

    _metrics_contract_and_host_checks(ops, vault, run_id, report, accounting,
                                      reported_at, evidence, pre_path,
                                      post_path, recon)

    (evidence["second_process_diff"],
     evidence["determinism"]) = replay_determinism(
        vault, run_id, Path(__file__).resolve().parent / "cos_driver_replay_check.py")

    evidence["fixture_ref"] = fixture_ref(corpus["appended"], run_id,
                                          capture["bodies"])
    evidence["finished_at"] = _ts(_utcnow())
    if evidence_path:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n",
                                 encoding="utf-8")
    return evidence


def _live_night_mode(args: argparse.Namespace, vault: Path) -> int:
    """The live-night tail of `main`: bind `--categories` to its enumeration,
    run the night, print the evidence, exit on the contract's code."""
    # `--categories` WITHOUT `--enumeration` IS REFUSED. The stamps were judged
    # on the `--enumerate-only` snapshot and this pass re-enumerates; with no
    # file to bind them to, a stamp lands by id on a mailbox the model never
    # saw. Refused here rather than defaulted, for the same reason
    # `category_gate_state`'s `defined_categories` has no default: a caller
    # that forgets an argument must get a loud error, never a quiet degradation.
    prior_rows = None
    if args.categories is not None:
        if args.enumeration is None:
            print("--categories needs --enumeration: the stamps are bound to "
                  "the enumeration they were judged on", file=sys.stderr)
            return 2
        try:
            prior_rows = json.loads(
                args.enumeration.read_text(encoding="utf-8"))["rows"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"the enumeration at {args.enumeration} is unreadable: {exc}",
                  file=sys.stderr)
            return 2
    try:
        ev = run_night(vault, args.tab_id, cap=args.cap, evidence_path=args.out,
                       categories=(load_categories(args.categories)
                                   if args.categories else None),
                       prior_enumeration=prior_rows,
                       use_cdp=args.cdp, use_ego=args.ego)
    except DriverStop as exc:
        print(f"DRIVER STOP: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({k: v for k, v in ev.items()
                      if k not in ("completeness", "fixture_ref")}, indent=2))
    return 0 if ev["contract"]["exit_code"] == 0 else 1


def stage_command(args: argparse.Namespace) -> int:
    """`--stage`: put the page-side driver where the MAIN world can reach it.

    On CDP and ego lite the host IS in the main world, so staging and booting
    are one step and there is no line left for another surface to evaluate by
    hand. AppleScript cannot get there, so it prints the line for a browser
    extension to run.
    """
    if args.cdp or args.ego:
        tab: Any = EgoTab() if args.ego else CdpTab()
        print(tab.js(stage(tab)))
        return 0
    if args.tab_id is None:
        print("--stage needs --tab-id", file=sys.stderr)
        return 2
    print(stage(ChromeTab(args.tab_id)))
    return 0


def main(argv: list[str]) -> int:
    args = night_argparser(__doc__.splitlines()[0], BODY_OPEN_CAP).parse_args(
        argv[1:])

    if args.selfcheck:
        return selfcheck()

    # THE ONE COMMAND WHOSE EXIT STATUS THE NIGHTLY GATES ON — the mode body
    # lives in cos_driver_cli, threaded through this module's own loaders so a
    # patched parent attribute still holds.
    if args.validate_categories:
        return validate_categories_mode(
            args, load_categories=load_categories,
            category_gate_state=category_gate_state)

    if args.stage:
        return stage_command(args)

    vault = args.vault or Path(os.environ.get("BRAIN_VAULT", "")).expanduser()
    if not vault or not vault.is_dir():
        print("--vault (or $BRAIN_VAULT) must name an existing vault", file=sys.stderr)
        return 2

    if args.replay:
        from brain import cos                                    # noqa: PLC0415
        manifest = cos.run_manifest(vault, args.replay) or {}
        rows = accounting_from_corpus(
            vault, args.replay,
            bundle_version=str(manifest.get("bundle_version") or ""),
            rules_version=str(manifest.get("extraction_rules_version") or ""),
            enumerated_at=str(args.enumerated_at or ""))
        print(json.dumps(rows, sort_keys=True, ensure_ascii=False))
        return 0

    if args.tab_id is None and not (args.cdp or args.ego):
        print("--tab-id is required for a live night (or --cdp / --ego)",
              file=sys.stderr)
        return 2

    if args.enumerate_only:
        try:
            out = enumerate_only(vault, args.tab_id, evidence_path=args.out,
                                 use_cdp=args.cdp, use_ego=args.ego)
        except DriverStop as exc:
            print(f"DRIVER STOP: {exc}", file=sys.stderr)
            return 3
        print(json.dumps({k: v for k, v in out.items()
                          if k not in ("rows", "completeness")}
                         | {"rows": len(out["rows"])}, indent=2))
        return 0

    return _live_night_mode(args, vault)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
