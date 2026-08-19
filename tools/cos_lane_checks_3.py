"""Own lane rehearsal command-line modes."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cos_lane_rehearsal as _lane
from cos_lane_checks_2 import (
    _navigate_once,
    _open_once,
    _reach_and_click,
    collect_eligible,
    contract_problems,
)


def _run_attempt(
    win: int,
    tab: int,
    convid: str,
    settle: float,
    base: str | None,
    deep_link_mode: bool,
    dx: int | None,
) -> tuple[dict, int | None, str | None]:
    """Execute one click or navigation attempt and refresh the folder base."""
    reach_steps = None
    if dx is None:
        result = _navigate_once(win, tab, convid, settle, base)
    elif deep_link_mode:
        result, reach_steps = _reach_and_click(win, tab, convid, dx)
    else:
        result = _open_once(win, tab, convid, dx, settle)
    if result.get("url"):
        base = result["url"].split("/id/")[0] or base
    return result, reach_steps, base


def _attempt_record(
    sequence: int,
    attempt_number: int,
    convid: str,
    result: dict,
    reach_steps: int | None,
    changed: str | None,
) -> tuple[dict, bool]:
    """Turn one browser result into the stable attempt record."""
    if result.get("pre") == convid:
        return ({"seq": sequence, "attempt": attempt_number,
                 "intended": convid, "method": result.get("method"),
                 "target_produced_pre": convid, "target_produced": convid,
                 "outcome": "already-open-skipped"}, True)
    row = {
        "seq": sequence, "attempt": attempt_number, "intended": convid,
        "target_intended": convid, "method": result.get("method"),
        "detail": result.get("detail"), "target_produced_pre": result.get("pre"),
        "target_produced": result.get("produced"), "in_view": result.get("in_view"),
        "point": result.get("point"), "nav_url": result.get("nav_url"),
        "rect": result.get("rect"), "selected": result.get("selected"),
        "selected_count": result.get("selected_count"),
        "selected_attr_seen": result.get("selected_attr_seen"),
        "target_rendered": result.get("target_rendered"),
        "target_selected": result.get("target_selected"),
        "corroborated_via": result.get("corroborated_via"),
        "recovery_steps": result.get("recovery_steps"),
        "body_chars": result.get("body_chars"), "rows_rendered": result.get("rows_rendered"),
        "reloaded": result.get("reloaded"), "ready_s": result.get("ready_s"),
        "waited_s": result.get("waited_s"),
        "ready_timed_out": result.get("ready_timed_out"),
        "body_settle_timed_out": result.get("body_settle_timed_out"),
        "retarget_scrolls": reach_steps, "outcome": result["outcome"],
    }
    if attempt_number == 2:
        row["retarget_changed"] = changed
        if reach_steps:
            row["retarget_changed"] += (
                f" (after {reach_steps} scroll step(s) to bring the row "
                "into the rendered list)"
            )
        if row["outcome"] == "landed":
            row["outcome"] = "landed-on-retarget"
    done = row["outcome"] in ("landed", "landed-on-retarget", "unconfirmed")
    return ({key: value for key, value in row.items()
             if value is not None or key in ("target_produced_pre", "target_produced")}, done)


def rehearse(
    win: int,
    tab: int,
    convids: list[str],
    settle: float,
    deep_link_mode: bool = False,
) -> list[dict]:
    """Open each requested row at most twice with a meaningfully different retry."""
    attempts: list[dict] = []
    try:
        base = json.loads(_lane._ev(win, tab, _lane._BASE_JS)).get("base") or None
    except Exception:  # noqa: BLE001 — no base is a valid first-pass state
        base = None
    steps = ([(None, None), (60, "fell back to the CLICK primitive: scrolled the "
                               "virtualized list until the row rendered, re-read "
                               "rect and id in one evaluation, clicked the sender line")]
             if deep_link_mode else
             [(60, None), (140, "re-scrolled into view, re-read rect+id, clicked a "
                                "different point on the sender line")])
    for sequence, convid in enumerate(convids, 1):
        for attempt_number, (dx, changed) in enumerate(steps, 1):
            result, reach_steps, base = _run_attempt(
                win, tab, convid, settle, base, deep_link_mode, dx
            )
            record, done = _attempt_record(
                sequence, attempt_number, convid, result, reach_steps, changed
            )
            attempts.append(record)
            if done:
                break
    return attempts


def _emit(report: dict, out: Path | None) -> int:
    payload = json.dumps(report, indent=1)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    verdict = report.get("verdict", "")
    return 2 if ("REGRESSION" in verdict or verdict.startswith("INVALID")
                 or verdict.startswith("UNCORROBORATED")
                 or verdict.startswith("REFUSED")
                 or verdict.startswith("SHORT SAMPLE")) else 0


def _select(path: Path, rows: int) -> int:
    """Apply the fail-closed read-state screen to an off-lane list."""
    listing = json.loads(path.read_text(encoding="utf-8"))
    listing = listing.get("list_rows", listing) if isinstance(listing, dict) else listing
    observable, eligible = _lane.read_state(listing)
    selected = eligible[:max(0, rows)]
    print(json.dumps({"rows_seen": len(listing),
                      "read_state_signal": "found" if observable else "not-found",
                      "proven_read": len(eligible), "selected": selected,
                      "open_nothing": not selected}, indent=1))
    return 0 if selected else 3


def _score(path: Path, out: Path | None) -> int:
    """Score another lane's record with the same report logic and exits."""
    record = json.loads(path.read_text(encoding="utf-8"))
    attempts = record.get("attempts", [])
    listing = record.get("list_rows", [])
    observable, eligible = _lane.read_state(listing) if listing else (None, [])
    summary, problems = _lane.summarize(attempts), contract_problems(attempts)
    report = {
        "tool": "cos_lane_rehearsal", "lane": record.get("lane", "unknown"),
        "primitive": record.get("primitive", "click"), "mutations": "none",
        "run_id": None, "ledgers_written": [], "scored_from": str(path),
        "tab": record.get("tab"), "qualification": record.get("qualification"),
        "list": {"rows_seen": len(listing), "proven_read": len(eligible),
                 "rows_requested": record.get("rows_requested"),
                 "read_state_signal": {True: "found", False: "not-found",
                                        None: "not-supplied"}[observable]},
        "ok": True, "attempts": attempts, "summary": summary,
        "contract_problems": problems, "opened": summary["opens_landed"],
        "verdict": _lane.verdict(summary, summary["rows_attempted"], problems,
                                  record.get("rows_requested")),
    }
    return _emit(report, out)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=_lane.__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=5,
                        help="how many already-read conversations to open (default 5)")
    parser.add_argument("--probe", action="store_true",
                        help="report what the list exposes and open NOTHING")
    parser.add_argument("--match", default=_lane.HOST, help="URL substring naming the OWA tab")
    parser.add_argument("--tab-id", default=None,
                        help="rehearse THIS Chrome tab, by its stable id, instead of "
                             "whichever OWA tab scores highest. NAME IT WHENEVER THE "
                             "RUN OPENED ITS OWN: the owner has an OWA tab too, a "
                             "substring match cannot tell them apart, and the tab "
                             "that loses the pick is the one the visibility hold is "
                             "holding — so the pass then drives a tab nothing is "
                             "keeping visible while the hold re-activates the other, "
                             "and the two fight. Measured 2026-08-10: 19 of 20 opens "
                             "timed out waiting for identity, 0 landed, on a lane "
                             "that was healthy. Bind this and the hold's --tab-id to "
                             "the same id")
    parser.add_argument("--deep-link", action="store_true",
                        help="open by NAVIGATING to each conversation's own URL "
                             "instead of clicking its row; the click path becomes "
                             "the one bounded re-target")
    parser.add_argument("--settle", type=float, default=None,
                        help="clicking: seconds to let the reading pane settle after "
                             f"an open (default {_lane._CLICK_SETTLE}). NAVIGATING: the "
                             "readiness-wait TIMEOUT, not a sleep — the open is "
                             "waited for until the page has loaded, the URL carries "
                             "the intended conversation, OWA marks it selected "
                             "and the reading pane has stopped growing (default "
                             f"{_lane._NAV_TIMEOUT:.0f}s ≈ 4x the ~4.5s measured "
                             "cost of one open)")
    parser.add_argument("--convids", default="",
                        help="rehearse EXACTLY these conversation ids (comma- or "
                             "whitespace-separated, or @<file> one per line) instead "
                             "of the first --rows of the pool. The read-state screen "
                             "is NOT bypassed: a requested id that the list does not "
                             "render, or does not PROVE already-read, is reported as "
                             "skipped and never opened. Use it to ask whether the "
                             "conversations that failed on a given night fail in "
                             "daylight too")
    parser.add_argument("--max-scrolls", type=int, default=20,
                        help="how many times the list may be scrolled to reach "
                             "--rows eligible conversations (default 20)")
    parser.add_argument("--out", type=Path, help="also write the report as JSON here")
    parser.add_argument("--self-check", action="store_true",
                        help="verify the report logic, touch no browser")
    parser.add_argument("--emit-js", choices=("list", "locate", "pane", "nav", "after",
                                                "scroll", "top", "base"),
                        help="print the JS to evaluate and exit (touches no browser)")
    parser.add_argument("--convid", default="",
                        help="--emit-js locate/nav/after: the target id. On `after` "
                             "it is what `ready` is computed against — omit it and "
                             "`ready` is always false")
    parser.add_argument("--dx", type=int, default=60,
                        help="--emit-js locate: click-point x offset (60, re-target 140)")
    parser.add_argument("--select", type=Path, metavar="LIST.json",
                        help="screen a list JSON for rows PROVEN read; open nothing")
    parser.add_argument("--score", type=Path, metavar="ATTEMPTS.json",
                        help="score an off-lane rehearsal record; touches no browser")
    return parser


def _emit_js(args: argparse.Namespace) -> int:
    fixed = {"list": _lane._LIST_JS, "pane": _lane._PANE_JS,
             "scroll": _lane._SCROLL_JS, "top": _lane._TOP_JS,
             "base": _lane._BASE_JS}
    templates = {"nav": _lane._NAV_JS, "after": _lane._AFTER_JS,
                 "locate": _lane._LOCATE_JS}
    print((fixed.get(args.emit_js) or templates[args.emit_js])
          % {"convid": json.dumps(args.convid), "dx": args.dx})
    return 0


def _prepare_browser(args: argparse.Namespace) -> tuple[dict, int, int, str | None]:
    report: dict = {"tool": "cos_lane_rehearsal", "mutations": "none",
                    "primitive": "deep-link" if args.deep_link else "click",
                    "run_id": None, "ledgers_written": []}
    prior_win = _lane._front_window_id()
    try:
        prior_app = _lane._frontmost_app()
    except Exception:
        prior_app = None
    for attempt in range(3):
        win, tab, url, state = _lane._pick_for_lane(args)
        report["tab"] = {"window": win, "tab": tab, "url": url[:80],
                         "visibilityState": state.get("vis"),
                         "rows_rendered": state.get("rows")}
        try:
            report["tab"]["tab_id"] = _lane.tab_id(win, tab)
            _lane._TAB_ID = int(report["tab"]["tab_id"])
        except Exception:
            pass
        try:
            if _lane._TAB_ID is not None:
                _lane.assert_visible_by_id(_lane._TAB_ID)
            else:
                _lane._assert_visible(win, tab)
            rows = []
            for _ in range(12):
                rows = json.loads(_lane._ev(win, tab, _lane._LIST_JS))
                if rows:
                    break
                time.sleep(1.0)
            report["tab"]["visibilityState"] = json.loads(
                _lane._ev(win, tab, 'JSON.stringify(document.visibilityState)'))
            if not rows:
                print(json.dumps({**report, "ok": False,
                                  "reason": "list-never-rendered",
                                  "detail": "the OWA message list rendered no rows "
                                            "12s after this tab was made active. It "
                                            "is not a read-state problem: make the "
                                            "Outlook tab the ACTIVE tab of its window, "
                                            "showing the message list, and leave it there."}, indent=1))
                return report, prior_win, 6, prior_app
            report["_seed_needed"] = bool(args.deep_link and not json.loads(
                _lane._ev(win, tab, _lane._BASE_JS)).get("base"))
            return report, prior_win, 0, prior_app
        except RuntimeError as exc:
            if "Invalid index" not in str(exc) or attempt == 2:
                raise
            report["reresolved"] = attempt + 1
            time.sleep(1.0)
    raise RuntimeError("unable to resolve the OWA tab")


def _seed_deep_link_route(
    report: dict,
    win: int,
    tab: int,
    targets: list[str],
    found: dict,
) -> tuple[list[str], int]:
    """Acquire an app-produced folder route without consuming the sample silently."""
    spare = [conversation_id for conversation_id in found["eligible"]
             if conversation_id not in targets]
    seed_cid = spare[0] if spare else targets[0]
    if not spare:
        targets = targets[1:]
    route = _lane.acquire_base(lambda js: _lane._ev(win, tab, js), seed_cid)
    report["folder_route"] = route
    if route.get("base"):
        return targets, 0
    print(json.dumps({**report, "ok": False,
                      "reason": "could-not-acquire-a-folder-route",
                      "detail": "this tab is on the DEFAULT folder, whose list URL carries no "
                                "`/mail/<folder>` segment, and the one click that would "
                                "have made OWA produce the item route did not. The folder "
                                "is never guessed. Check the tab is the ACTIVE tab of the "
                                "window Chrome is showing (a hidden tab renders no rows "
                                "and its rows cannot be clicked), then re-run; opening any "
                                "already-read conversation by hand also leaves the tab on "
                                "a route this tool can derive from."}, indent=1))
    return targets, 6


def _run_browser(args: argparse.Namespace) -> int:
    report, prior_win, early_exit, prior_app = _prepare_browser(args)
    if early_exit:
        return early_exit
    seed_needed = bool(report.pop("_seed_needed", False))
    win, tab = report["tab"]["window"], report["tab"]["tab"]
    restored = False
    try:
        requested = _lane._parse_convids(args.convids)
        want = 10 ** 6 if requested else max(0, args.rows)
        found = collect_eligible(lambda js: _lane._ev(win, tab, js),
                                 want + (1 if seed_needed else 0), args.max_scrolls)
        eligible = found["eligible"]
        if requested:
            want = len(requested)
            eligible_set, seen_set = set(eligible), found["seen"]
            eligible = [conversation_id for conversation_id in requested
                        if conversation_id in eligible_set]
            report["targeted"] = {
                "requested": len(requested), "opened": len(eligible),
                "skipped_not_rendered": [conversation_id for conversation_id in requested
                                          if conversation_id not in seen_set],
                "skipped_not_proven_read": [conversation_id for conversation_id in requested
                                             if conversation_id in seen_set
                                             and conversation_id not in eligible_set],
            }
        report["list"] = {
            "rows_seen": found["rows_seen"], "unread": found["unread"],
            "proven_read": len(eligible), "rows_requested": want,
            "scrolls": found["scrolls"], "scroll_method": found["scroll_method"],
            "from_top": found["from_top"],
            "read_state_signal": "found" if found["observable"] else "not-found",
        }
        if args.probe or not eligible:
            report.update(ok=True, opened=0,
                          verdict=("PROBE ONLY — nothing opened" if args.probe else
                                   "NO-EVIDENCE — no row could be PROVEN already-read "
                                   "(the unread affordance was "
                                   + ("absent from every row" if found["observable"]
                                      else "not observable in this list")
                                   + "), so none was opened"))
        else:
            targets = eligible[:want]
            if seed_needed:
                targets, seed_exit = _seed_deep_link_route(
                    report, win, tab, targets, found
                )
                if seed_exit:
                    return seed_exit
            elif args.deep_link:
                report["folder_route"] = {
                    "base": json.loads(_lane._ev(win, tab, _lane._BASE_JS)).get("base"),
                    "acquired_via": "tab-url",
                }
            settle = args.settle if args.settle is not None else (
                _lane._NAV_TIMEOUT if args.deep_link else _lane._CLICK_SETTLE)
            attempts = rehearse(win, tab, targets, settle, args.deep_link)
            summary = _lane.summarize(attempts)
            problems = contract_problems(attempts)
            report.update(ok=True, attempts=attempts, summary=summary,
                          contract_problems=problems, opened=summary["opens_landed"],
                          verdict=_lane.verdict(summary, len(targets), problems, want))
    except (_lane.JsUnavailable, _lane.OsaUnavailable):
        raise
    finally:
        _lane._restore(prior_app, prior_win, (None, None))
        restored = True
    return _emit(report, args.out) if restored else 6


def main(argv: list[str] | None = None) -> int:
    """Dispatch self-check, off-lane, and browser rehearsal phases."""
    args = _build_parser().parse_args(argv)
    if args.self_check:
        return _lane._self_check()
    if args.emit_js:
        return _emit_js(args)
    if args.select:
        return _select(args.select, args.rows)
    if args.score:
        return _score(args.score, args.out)
    try:
        return _run_browser(args)
    except LookupError as exc:
        print(json.dumps({"ok": False, "reason": "no-owa-tab", "detail": str(exc)}, indent=1))
        return 3
    except _lane.JsUnavailable as exc:
        print(json.dumps({"ok": False, "reason": "js-from-apple-events-off",
                          "detail": str(exc)[:200]}, indent=1))
        return 4
    except _lane.OsaUnavailable as exc:
        print(json.dumps({"ok": False, "reason": "osascript-unavailable",
                          "detail": str(exc)[:200]}, indent=1))
        return 5


__all__ = [
    "_emit",
    "_score",
    "_select",
    "main",
    "rehearse",
]
