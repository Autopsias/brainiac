"""CLI surface sub-steps of the mutation module (s18 drain of main).

The argument parser, the command dispatch and the MutationStop report moved
verbatim out of ``cos_mutate.main``. Dispatch reaches every command through
``lane`` — the loaded ``cos_mutate`` module's own namespace — so a
monkeypatch on the parent keeps governing what the CLI dispatches to.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable


def build_parser(description: str, *, since_days_default: int,
                 draft_resume_policy: str) -> argparse.ArgumentParser:
    """The mutation module's argument parser, unchanged in shape and order."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument("command", choices=("plan", "dry-run", "rehearsal-gate",
                                       "apply", "undo",
                                       "unchip", "canary",
                                       "hook-stage", "hook-verify",
                                       "shapes-from-capture",
                                       "capture-shapes", "evidence", "selfcheck"))
    p.add_argument("--plan", type=Path, default=None,
                   help="the `plan` command's output. REQUIRED by `apply` and "
                        "read by `dry-run` and `rehearsal-gate`: one plan is "
                        "built once, frozen, and consumed by all three, so "
                        "what is dispatched is what was rehearsed")
    p.add_argument("--rehearsal", type=Path, default=None,
                   help="apply: the `dry-run` output that validated --plan. "
                        "Its `plan_digest` must equal the plan's or nothing "
                        "is dispatched")
    p.add_argument("--dry-run-json", type=Path, default=None,
                   help="rehearsal-gate: the `dry-run` command's output")
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--tab-id", type=int, default=None)
    p.add_argument("--canary-convid", default=None)
    p.add_argument("--undo-limit", type=int, default=None,
                   help="undo/unchip: reverse at most N of this run's archives "
                        "or chips")
    # Caps default to UNLIMITED (owner ruling: content, not a number, bounds a
    # night). A value <= 0 is read as unlimited too, so `--cap-archive 0` and an
    # absent flag mean the same thing. The scope is the recency window below.
    p.add_argument("--cap-archive", type=int, default=None)
    # THE ATTENDED CAP IS AN ABORT, NOT A TRUNCATION, and that difference is
    # the whole point (adversarial review 2026-08-14). `--cap-archive` above
    # EXCLUDES each row past the cap and lets the run proceed with the rest —
    # which is the right shape for a bounded hand-run and exactly the WRONG
    # shape for "stop and come back to the owner". An attended run that plans
    # more archives than the owner authorised must dispatch NOTHING and say how
    # many it wanted, never a truncated prefix that looks like consent.
    p.add_argument("--archive-abort-cap", type=int, default=None,
                   help="plan: REFUSE the whole plan (exit 4, nothing written) "
                        "when it would archive more than N threads. Attended "
                        "runs only; absent ⇒ no abort, and the scheduled "
                        "lane's behaviour is unchanged.")
    p.add_argument("--cap-categorize", type=int, default=None)
    p.add_argument("--cap-draft", type=int, default=None)
    p.add_argument("--since-days", type=int, default=since_days_default,
                   help=f"only act on threads received within N days "
                        f"(default {since_days_default}; $BRAIN_COS_SINCE_DAYS)")
    p.add_argument("--all", action="store_true",
                   help="historic: lift the recency window, act on the whole "
                        "mailbox regardless of age")
    p.add_argument("--no-require-boot", action="store_true",
                   help="hook-verify: accept a hook installed after load. Legal "
                        "for shape capture only — the mutation lane needs the "
                        "BOOT envelope and refuses without it.")
    p.add_argument("--allow-draft-resume", action="store_true",
                   help=draft_resume_policy)
    p.add_argument("--cdp", action="store_true",
                   help="drive the page over CDP (main world, addresses one "
                        "browser by port) instead of AppleScript")
    p.add_argument("--ego", action="store_true",
                   help="drive the page through ego lite (main world, no "
                        "extension, no CDP port) instead of AppleScript")
    p.add_argument("--capture", default=None,
                   help="a cos_cdp_capture jsonl to build the shapes from")
    p.add_argument("--out", type=Path, default=None)
    return p


class AttendedCapRefused(Exception):
    """The plan exceeded the attended abort cap: dispatch NOTHING, exit 4."""


def plan_command(args: argparse.Namespace, vault, caps: dict[str, Any],
                 since_days: int | None, *, lane) -> dict[str, Any]:
    """Build the plan artifact, refusing it whole under the attended abort cap."""
    out = lane.build_plan(
        vault, args.run_id, caps=caps, since_days=since_days,
        applied=lane.UndoLedger(vault, args.run_id).applied_counts())
    # STAMPED INTO THE PLAN whether it fires or not, so the artifact the
    # owner approves records the bound he set — a cap recorded nowhere
    # is an abort rule nobody can prove was enforced.
    out["archive_abort_cap"] = args.archive_abort_cap
    want = out["planned_by_verb"]["archive"]
    if args.archive_abort_cap is not None \
            and want > args.archive_abort_cap:
        print(f"REFUSING the whole mutation lane: this plan would "
              f"archive {want} thread(s) and the attended cap is "
              f"{args.archive_abort_cap}. Nothing was written and "
              "nothing will be dispatched — an attended cap STOPS the "
              "run and comes back to the owner; it never archives a "
              "truncated prefix of what he did not approve.",
              file=lane.sys.stderr)
        raise AttendedCapRefused()
    out["vault_root_asserted"] = lane.assert_vault(vault)
    out["e17"] = lane.canary_status(vault)
    out["kill_switch"] = lane.kill_switch(vault)
    return out


def dispatch_command(args: argparse.Namespace, vault, caps: dict[str, Any],
                     since_days: int | None, *, lane) -> dict[str, Any]:
    """Run the parsed command through the lane module's own functions."""
    if args.command == "plan":
        out = plan_command(args, vault, caps, since_days, lane=lane)
    elif args.command == "dry-run":
        out = lane.dry_run(vault, args.run_id, args.tab_id, caps=caps,
                           since_days=since_days, plan_path=args.plan,
                           use_cdp=args.cdp, use_ego=args.ego)
    elif args.command == "apply":
        # THE CLI HAS NO UNREHEARSED APPLY (K1). Every production caller —
        # the nightly, and any hand-run resuming it — already has a
        # `plan.json` and a `dry-run.json` in the run's evidence directory,
        # so requiring them costs nothing legitimate and closes the one
        # door through which an unrehearsed payload could reach the
        # mailbox. There is deliberately NO override flag: a knob that
        # turns this off is the hole with a longer name.
        if args.plan is None or args.rehearsal is None:
            raise lane.MutationStop(
                "apply needs --plan and --rehearsal: the frozen plan and "
                "the rehearsal that validated it. An apply that plans for "
                "itself dispatches a payload nothing rehearsed — a P1/add "
                "plan behind a P3/remove rehearsal used to return ok. Run "
                "`plan` then `dry-run --plan …`, then apply against both")
        out = lane.apply_pass(vault, args.run_id, args.tab_id, caps=caps,
                              since_days=since_days,
                              allow_draft_resume=args.allow_draft_resume,
                              plan_path=args.plan, rehearsal_path=args.rehearsal,
                              use_cdp=args.cdp, use_ego=args.ego)
    elif args.command == "undo":
        out = lane.undo_pass(vault, args.run_id, args.tab_id, use_cdp=args.cdp,
                             use_ego=args.ego, limit=args.undo_limit)
    elif args.command == "unchip":
        out = lane.unchip_pass(vault, args.run_id, args.tab_id, use_cdp=args.cdp,
                               use_ego=args.ego, limit=args.undo_limit)
    elif args.command == "canary":
        out = lane.canary_drill(vault, args.run_id, args.tab_id,
                                args.canary_convid, use_cdp=args.cdp,
                                use_ego=args.ego)
    elif args.command == "shapes-from-capture":
        out = lane.shapes_from_capture(vault, args.capture)
    elif args.command == "hook-stage":
        out = lane.stage_hook(args.tab_id)
    elif args.command == "hook-verify":
        out = lane.verify_capture_world(args.tab_id,
                                        require_boot=not args.no_require_boot)
    elif args.command == "capture-shapes":
        out = lane.capture_shapes(vault, args.tab_id)
    else:
        out = lane.build_evidence(vault if vault and vault.is_dir() else None,
                                  args.run_id)
    return out


def write_stop_report(args: argparse.Namespace, exc: Exception, *,
                      ts: Callable, write_atomic: Callable,
                      ops_mode: int) -> None:
    """A STOP WRITES ITS REPORT TOO (review 2026-08-12).

    Every `MutationStop` returned 3 having written no `--out` file at all, and
    `cos_nightly.sh` reads that file to decide whether the night finished — so
    a kill switch off, an invalid E17 canary or missing approved shapes left no
    report, no `stopped` field, and a log line reading `done` at exit 0: the
    exact run-125 symptom the stop detection was added to end. The report is
    the operator-facing record of a refusal, not only of a completed pass.

    `results: []` IS TRUE HERE, AND ONLY HERE (review 2026-08-12). It used
    to be a claim this handler could not support: three bridge calls in
    `apply_pass` sat outside its `try`, so a timeout in any of them threw
    past the pass and a night that had applied fifty mutations wrote an
    artifact asserting zero. Those three now stop the pass from INSIDE and
    return its real report, so anything reaching here is a refusal that
    happened before the pass could dispatch anything — kill switch off,
    invalid E17 canary, missing shapes.
    """
    import json                                                  # noqa: PLC0415

    if not args.out:
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(args.out, json.dumps(
        {"command": args.command, "run_id": args.run_id,
         "stopped": str(exc), "stop_class": "mutation-stop",
         "results": [], "skipped_absent": [], "at": ts()},
        indent=2, ensure_ascii=False) + "\n", mode=ops_mode)
