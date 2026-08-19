"""Run the lane report's executable self-check cases."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import unquote

import cos_lane_rehearsal as _lane


def _stats(attempts: list[dict], key: str) -> dict | None:
    values = sorted(attempt[key] for attempt in attempts if attempt.get(key) is not None)
    if not values:
        return None
    return {"median": round(values[len(values) // 2], 2),
            "max": round(values[-1], 2), "total": round(sum(values), 2)}


def summarize(attempts: list[dict]) -> dict:
    """Summarize targeting outcomes and the measured recovery costs."""
    opened = [attempt for attempt in attempts
              if attempt["outcome"] in ("landed", "landed-on-retarget")]
    return {
        "rows_attempted": len({attempt["intended"] for attempt in attempts}),
        "opens_landed": len(opened),
        "first_attempt_ok": sum(1 for attempt in attempts
                                 if attempt["attempt"] == 1
                                 and attempt["outcome"] == "landed"),
        "retargets": sum(1 for attempt in attempts if attempt["attempt"] == 2),
        "mismatches": sum(1 for attempt in attempts if attempt["outcome"] == "mismatch"),
        "never_moved": sum(1 for attempt in attempts
                            if attempt["outcome"] == "mismatch"
                            and attempt["target_produced"] == attempt["target_produced_pre"]),
        "unreadable": sum(1 for attempt in attempts if attempt["outcome"] == "no-id"),
        "navigation_refused": sum(1 for attempt in attempts
                                   if attempt["outcome"] == "navigation-refused"),
        "refused_recovered": sum(1 for attempt in attempts
                                  if attempt["attempt"] == 2
                                  and attempt["outcome"] == "landed-on-retarget"
                                  and attempt.get("retarget_scrolls") is not None),
        "row_unreachable": sum(1 for attempt in attempts
                                if attempt["outcome"] == "row-unreachable"),
        "reach_scrolls": _stats(attempts, "retarget_scrolls"),
        "unconfirmed": sum(1 for attempt in attempts if attempt["outcome"] == "unconfirmed"),
        "corroborated_direct": sum(1 for attempt in attempts
                                    if attempt.get("corroborated_via") == "direct"),
        "corroborated_after_recovery": sum(1 for attempt in attempts
                                            if attempt.get("corroborated_via") == "recovery"),
        "recovery_attempted": sum(1 for attempt in attempts
                                   if attempt.get("recovery_steps") is not None),
        "recovery_scrolls": _stats(attempts, "recovery_steps"),
        "full_reloads": sum(1 for attempt in attempts if attempt.get("reloaded") is True),
        "open_wait_s": _stats(attempts, "waited_s"),
        "identity_wait_s": _stats(attempts, "ready_s"),
        "ready_timeouts": sum(1 for attempt in attempts if attempt.get("ready_timed_out")),
        "body_settle_timeouts": sum(1 for attempt in attempts
                                     if attempt.get("body_settle_timed_out")),
        "bodies_rendered": sum(1 for attempt in attempts
                                if attempt["outcome"] in ("landed", "landed-on-retarget")
                                and (attempt.get("body_chars") or 0) > 0),
    }


def verdict(
    summary: dict,
    eligible: int,
    problems: list[str] | None = None,
    requested: int | None = None,
) -> str:
    """Render the stable lane verdict vocabulary."""
    if problems:
        return ("INVALID — the record cannot be scored: " + "; ".join(problems[:3])
                + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""))
    if not eligible:
        return "NO-EVIDENCE — no eligible already-read row to rehearse on"
    if summary["mismatches"]:
        shape = ("the open never moved the pane"
                 if summary["never_moved"] == summary["mismatches"]
                 else "the pane moved to the wrong conversation")
        return (f"LANE REGRESSION — {summary['mismatches']} mismatch(es); {shape}. "
                "This is run 103's shape; do not spend a night on it")
    if summary.get("unconfirmed"):
        return (f"UNCORROBORATED — {summary['unconfirmed']} open(s) had the intended URL "
                "and NO app-produced confirmation (the list named no single selected "
                "row, and the conversation did not render even after the list was "
                "scrolled back to it). Under navigation the URL is the input, not "
                "the evidence: this does not promote")
    if summary.get("row_unreachable"):
        return (f"REFUSED, UNREACHABLE — {summary['row_unreachable']} navigation(s) "
                "OWA refused whose row never rendered inside the "
                f"{_lane._REACH_STEPS}-step scroll bound, so the click fallback had "
                "nothing to click. Nothing was opened and nothing moved; these are "
                "held by name, not scored as identity mismatches")
    if summary["opens_landed"] < summary["rows_attempted"]:
        return "DEGRADED — some rows never opened; see the per-attempt records"
    if requested and summary["rows_attempted"] < requested:
        return (f"SHORT SAMPLE — only {summary['rows_attempted']} of the {requested} rows "
                "asked for could be sampled (the list exposed no more rows PROVEN "
                "already read); "
                f"{summary['first_attempt_ok']}/{summary['rows_attempted']} first attempt"
                + (f", {summary['retargets']} re-target(s)" if summary["retargets"] else "")
                + ". This is NOT a clean run: a pass measured over fewer rows than "
                "requested is a false all-clear")
    recovery_count = summary.get("corroborated_after_recovery") or 0
    tail = (f"; {recovery_count} corroborated only after the list was scrolled back to it"
            if recovery_count else "")
    refused_count = summary.get("refused_recovered") or 0
    if refused_count:
        tail += (f"; {refused_count} navigation(s) OWA refused, recovered by the click "
                 "fallback after scrolling the row into the list")
    if summary["retargets"]:
        return (f"LANDS, WITH RETRIES — {summary['first_attempt_ok']}/"
                f"{summary['rows_attempted']} first attempt{tail}")
    return (f"CLEAN — {summary['first_attempt_ok']}/{summary['rows_attempted']} "
            f"first attempt{tail}")


def _check_basic_verdicts() -> None:
    clean = [{"seq": 1, "attempt": 1, "intended": "a",
              "target_produced_pre": "z", "target_produced": "a",
              "outcome": "landed"}]
    assert _lane.summarize(clean)["first_attempt_ok"] == 1
    assert _lane.verdict(_lane.summarize(clean), 1).startswith("CLEAN")
    stuck = [{"seq": 1, "attempt": n, "intended": "a",
              "target_produced_pre": "z", "target_produced": "z",
              "outcome": "mismatch"} for n in (1, 2)]
    summary = _lane.summarize(stuck)
    assert summary["mismatches"] == 2 and summary["never_moved"] == 2
    assert summary["opens_landed"] == 0
    assert "never moved" in _lane.verdict(summary, 1)
    assert "REGRESSION" in _lane.verdict(summary, 1)
    wrong = [{"seq": 1, "attempt": 1, "intended": "a",
              "target_produced_pre": "z", "target_produced": "b",
              "outcome": "mismatch"}]
    assert "wrong conversation" in _lane.verdict(_lane.summarize(wrong), 1)
    retried = clean[:1] + [
        {"seq": 2, "attempt": 1, "intended": "b", "point": {"x": 60, "y": 40},
         "target_produced_pre": "a", "target_produced": "a", "outcome": "mismatch"},
        {"seq": 2, "attempt": 2, "intended": "b", "point": {"x": 140, "y": 40},
         "retarget_changed": "re-scrolled, re-read rect+id, clicked a different point",
         "target_produced_pre": "a", "target_produced": "b",
         "outcome": "landed-on-retarget"},
    ]
    summary = _lane.summarize(retried)
    assert summary["opens_landed"] == 2 and summary["retargets"] == 1
    assert summary["first_attempt_ok"] == 1
    assert "REGRESSION" in _lane.verdict(summary, 2)
    assert _lane.verdict(_lane.summarize([]), 0).startswith("NO-EVIDENCE")


def _check_read_state_and_contract() -> None:
    mixed = [{"convid": "a", "unread": False}, {"convid": "b", "unread": True},
             {"convid": "c", "unread": False}]
    assert _lane.read_state(mixed) == (True, ["a", "c"])
    assert _lane.read_state([{"convid": "a", "unread": False}]) == (False, [])
    assert _lane.read_state([{"convid": "b", "unread": True}]) == (True, [])
    assert _lane.read_state([]) == (False, [])
    clean = [{"seq": 1, "attempt": 1, "intended": "a",
              "target_produced_pre": "z", "target_produced": "a",
              "outcome": "landed"}]
    retried = [clean[0], {"seq": 2, "attempt": 1, "intended": "b",
                          "point": {"x": 60, "y": 40},
                          "target_produced_pre": "a", "target_produced": "a",
                          "outcome": "mismatch"},
               {"seq": 2, "attempt": 2, "intended": "b",
                "point": {"x": 140, "y": 40},
                "retarget_changed": "re-scrolled, re-read rect+id, clicked a different point",
                "target_produced_pre": "a", "target_produced": "b",
                "outcome": "landed-on-retarget"}]
    assert _lane.contract_problems(clean) == []
    assert _lane.contract_problems(retried) == []
    same_point = [
        {"seq": 1, "attempt": 1, "intended": "a", "point": {"x": 60, "y": 9},
         "target_produced_pre": "z", "target_produced": "z", "outcome": "mismatch"},
        {"seq": 1, "attempt": 2, "intended": "a", "point": {"x": 60, "y": 9},
         "retarget_changed": "re-queried and clicked again",
         "target_produced_pre": "z", "target_produced": "a",
         "outcome": "landed-on-retarget"},
    ]
    assert any("SAME point" in problem
               for problem in _lane.contract_problems(same_point))
    assert _lane.verdict(_lane.summarize(same_point), 1,
                         _lane.contract_problems(same_point)).startswith("INVALID")
    unnamed = [dict(same_point[0]), {**same_point[1], "point": {"x": 140, "y": 9},
                                    "retarget_changed": ""}]
    assert any("names no change" in problem
               for problem in _lane.contract_problems(unnamed))
    assert _lane.contract_problems([{**same_point[1], "point": {"x": 140, "y": 9}}]) == [
        "seq 1: attempt 2 with no attempt-1 row (E30(a))"]
    assert any("target_produced_pre" in problem for problem in _lane.contract_problems(
        [{"seq": 1, "attempt": 1, "intended": "a", "outcome": "landed"}]))
    unrendered = [{"seq": 1, "attempt": n, "intended": "a", "point": None,
                   "target_produced_pre": None, "target_produced": None,
                   "outcome": "no-click", "detail": "row-not-rendered",
                   **({"retarget_changed": "re-scrolled, re-read rect+id"}
                      if n == 2 else {})} for n in (1, 2)]
    assert _lane.contract_problems(unrendered) == []


def _check_navigation_contract() -> None:
    assert _lane.classify({"produced": "", "body_chars": _lane._SHELL_CHARS}, "a") == "navigation-refused"
    assert _lane.classify({"produced": "", "body_chars": 4000}, "a") == "no-id"
    assert _lane.classify({"produced": ""}, "a") == "no-id"
    assert _lane.classify({"produced": "b", "selected": "b", "body_chars": 12}, "a") == "mismatch"
    assert _lane.classify({"produced": "b", "selected": None,
                           "target_rendered": True, "selected_attr_seen": True,
                           "selected_count": 0, "body_chars": _lane._SHELL_CHARS}, "a") == "mismatch"
    refused = [{"seq": 1, "attempt": 1, "intended": "a", "nav_url": "/id/a",
                "target_produced_pre": None, "target_produced": None,
                "body_chars": _lane._SHELL_CHARS, "outcome": "navigation-refused"},
               {"seq": 1, "attempt": 2, "intended": "a", "point": {"x": 60, "y": 40},
                "retarget_changed": "fell back to the CLICK primitive (after 17 scroll step(s) to bring the row into the rendered list)",
                "retarget_scrolls": 17, "target_produced_pre": None,
                "target_produced": "a", "outcome": "landed-on-retarget"}]
    summary = _lane.summarize(refused)
    assert summary["navigation_refused"] == 1 and summary["mismatches"] == 0
    assert summary["refused_recovered"] == 1 and summary["opens_landed"] == 1
    assert _lane.contract_problems(refused) == []
    verdict = _lane.verdict(summary, 1, _lane.contract_problems(refused))
    assert "REGRESSION" not in verdict and "recovered by the click fallback" in verdict
    unreachable = [refused[0], {"seq": 1, "attempt": 2, "intended": "a",
                   "point": None, "retarget_changed": "fell back to the CLICK primitive",
                   "retarget_scrolls": _lane._REACH_STEPS,
                   "target_produced_pre": None, "target_produced": None,
                   "outcome": "row-unreachable"}]
    summary = _lane.summarize(unreachable)
    assert summary["row_unreachable"] == 1 and summary["mismatches"] == 0
    assert _lane.contract_problems(unreachable) == []
    assert _lane.verdict(summary, 1).startswith("REFUSED, UNREACHABLE")
    assert _lane.contract_problems([{"seq": 1, "attempt": 1, "intended": "a",
                                    "outcome": "already-open-skipped"}]) == []
    locate = _lane._LOCATE_JS % {"convid": '"a"', "dx": 60}
    assert "dispatchEvent" not in locate and "elementFromPoint" in locate
    assert "dispatchEvent" in (_lane._OPEN_JS % {"convid": '"a"', "dx": 60})
    navigation = _lane._NAV_JS % {"convid": '"a"'}
    assert "dispatchEvent" not in navigation and "data-convid" not in navigation


def _check_deep_link_and_wait() -> None:
    conversation_id = ("AAQkADMyNTM0MDJjLWUyNjktNGNhMC1hNWU0LTczNDU4OTZhZDkyMgAQ"
                       "ANUmJH4QS2RNt99AlrSvTuo=")
    base = "https://outlook.cloud.microsoft/mail/inbox"
    assert _lane.deep_link(conversation_id, base) == base + "/id/" + (
        "AAQkADMyNTM0MDJjLWUyNjktNGNhMC1hNWU0LTczNDU4OTZhZDkyMgAQ"
        "ANUmJH4QS2RNt99AlrSvTuo%3D")
    assert unquote(_lane.deep_link(conversation_id, base).split("/id/")[1]) == conversation_id
    assert _lane.deep_link(conversation_id, base + "/") == _lane.deep_link(conversation_id, base)
    assert "/mail/archive/id/" in _lane.deep_link(conversation_id, base.replace("inbox", "archive"))
    navigation_ok = [{"seq": 1, "attempt": 1, "intended": "a", "method": "navigate",
                      "nav_url": "…/id/a", "target_produced_pre": "z",
                      "target_produced": "a", "selected": "a", "outcome": "landed"}]
    assert _lane.contract_problems(navigation_ok) == []
    assert _lane.verdict(_lane.summarize(navigation_ok), 1).startswith("CLEAN")
    unconfirmed = [dict(navigation_ok[0], selected=None, outcome="unconfirmed")]
    summary = _lane.summarize(unconfirmed)
    assert summary["unconfirmed"] == 1 and summary["opens_landed"] == 0
    assert _lane.verdict(summary, 1).startswith("UNCORROBORATED")
    assert _lane.verdict(_lane.summarize([dict(navigation_ok[0], outcome="mismatch",
                                                target_produced="b")]), 1).startswith("LANE REGRESSION")
    renavigated = [dict(navigation_ok[0], target_produced="z", outcome="mismatch"),
                   {"seq": 1, "attempt": 2, "intended": "a", "method": "navigate",
                    "nav_url": "…/id/a", "retarget_changed": "navigated again",
                    "target_produced_pre": "z", "target_produced": "a",
                    "outcome": "landed-on-retarget"}]
    assert any("SAME URL" in problem for problem in _lane.contract_problems(renavigated))
    assert _lane.verdict(_lane.summarize(renavigated), 1,
                         _lane.contract_problems(renavigated)).startswith("INVALID")
    to_click = [renavigated[0], dict(renavigated[1], method="click", nav_url=None,
                                    point={"x": 60, "y": 40},
                                    retarget_changed="fell back to the CLICK primitive")]
    assert _lane.contract_problems(to_click) == []
    assert _lane.summarize([dict(navigation_ok[0], reloaded=True)])[
        "full_reloads"] == 1
    assert _lane.summarize(navigation_ok)["full_reloads"] == 0
    sequence = [{"produced": "z", "selected": "z", "ready": False},
                {"produced": "a", "selected": "z", "ready": False},
                {"produced": "a", "selected": "a", "ready": True, "body_chars": 28},
                {"produced": "a", "selected": "a", "ready": True, "body_chars": 3953},
                {"produced": "a", "selected": "a", "ready": True, "body_chars": 4020},
                {"produced": "a", "selected": "a", "ready": True, "body_chars": 4020}]
    calls: list[int] = []

    def fake_read(_js: str, _sequence: list[dict] = sequence) -> str:
        calls.append(1)
        return json.dumps(_sequence.pop(0))

    got = _lane.await_ready(fake_read, "a", timeout=5.0)
    assert got["selected"] == "a" and len(calls) == 6
    assert got["body_chars"] == 4020 and got["ready_s"] < got["waited_s"] < 5.0
    assert "ready_timed_out" not in got and "body_settle_timed_out" not in got
    stuck_read = _lane.await_ready(lambda _js: json.dumps(
        {"produced": "a", "selected": None, "ready": False}), "a", timeout=0.2)
    assert stuck_read["ready_timed_out"] is True and stuck_read["ready_s"] is None
    empty = _lane.await_ready(lambda _js: json.dumps(
        {"produced": "a", "selected": "a", "ready": True, "body_chars": 0}),
        "a", timeout=0.2)
    assert empty["body_settle_timed_out"] is True and "ready_timed_out" not in empty

    def raises(_js: str) -> str:
        raise RuntimeError("Invalid index")

    blind = _lane.await_ready(raises, "a", timeout=0.2)
    assert blind["ready_timed_out"] is True and "Invalid index" in blind["read_error"]


def _check_verdict_metrics() -> None:
    """Check recovery, verdict, and timing summaries from rehearsal records."""
    goto = _lane._GOTO_JS % {"url": json.dumps(_lane.deep_link("a/b=", "https://outlook.cloud.microsoft/mail/inbox")),
                             "convid": json.dumps("a/b=")}
    assert "/mail/inbox/id/a%2Fb%3D" in goto and "data-convid" not in goto
    assert "dispatchEvent" not in goto and "not-on-a-mail-folder-url" in _lane._NAV_JS
    assert "not-on-a-mail-folder-url" not in goto
    assert "[^/?#]+" in _lane._BASE_JS and _lane._BASE_JS.count("mail") == 1
    paths = [{"seq": 1, "attempt": 1, "intended": "a", "target_produced_pre": "z",
              "target_produced": "a", "selected": "a", "outcome": "landed",
              "corroborated_via": "direct"},
             {"seq": 2, "attempt": 1, "intended": "b", "target_produced_pre": "z",
              "target_produced": "b", "selected": "b", "outcome": "landed",
              "corroborated_via": "recovery", "recovery_steps": 2},
             {"seq": 3, "attempt": 1, "intended": "c", "target_produced_pre": "z",
              "target_produced": "c", "selected": None, "outcome": "unconfirmed",
              "recovery_steps": 6}]
    summary = _lane.summarize(paths)
    assert summary["corroborated_direct"] == 1 and summary["corroborated_after_recovery"] == 1
    assert summary["recovery_attempted"] == 2
    assert summary["recovery_scrolls"] == {"median": 6, "max": 6, "total": 8}
    assert _lane.verdict(_lane.summarize(paths[:2]), 2, None, 2).startswith("CLEAN")
    assert "scrolled back" in _lane.verdict(_lane.summarize(paths[:2]), 2, None, 2)
    assert _lane.verdict(summary, 3).startswith("UNCORROBORATED")
    assert any("names no recovery_steps" in problem for problem in _lane.contract_problems(
        [dict(paths[1], recovery_steps=None)]))
    assert any("unknown corroborated_via" in problem for problem in _lane.contract_problems(
        [dict(paths[0], corroborated_via="scrolled")]))
    five = [{"seq": n, "attempt": 1, "intended": str(n), "target_produced_pre": "z",
             "target_produced": str(n), "outcome": "landed"} for n in range(5)]
    assert _lane.verdict(_lane.summarize(five), 5, None, 5).startswith("CLEAN")
    short = _lane.verdict(_lane.summarize(five), 5, None, 20)
    assert short.startswith("SHORT SAMPLE") and "only 5 of the 20" in short
    assert _lane.verdict(_lane.summarize([{"seq": 1, "attempt": 1, "intended": "a",
                                           "target_produced_pre": "z", "target_produced": "z",
                                           "outcome": "mismatch"}]), 1, None, 20).startswith("LANE REGRESSION")
    timed = [dict(five[0], waited_s=1.5, ready_s=1.0, body_chars=4020),
             dict(five[1], waited_s=7.25, ready_s=2.0, body_chars=900),
             dict(five[2], waited_s=3.0, ready_s=1.5, body_chars=0,
                  body_settle_timed_out=True)]
    summary = _lane.summarize(timed)
    assert summary["open_wait_s"] == {"median": 3.0, "max": 7.25, "total": 11.75}
    assert summary["identity_wait_s"]["median"] == 1.5
    assert summary["body_settle_timeouts"] == 1 and summary["ready_timeouts"] == 0
    assert summary["bodies_rendered"] == 2 and _lane.summarize(five)["open_wait_s"] is None


def _check_collection_recovery_and_verdicts() -> None:
    views = [[{"convid": "a", "marks_unread": True}, {"convid": "b", "marks_unread": True}],
             [{"convid": "c", "marks_unread": True}, {"convid": "d", "marks_unread": True}],
             [{"convid": "e", "marks_unread": True}, {"convid": "f", "marks_unread": True}]]

    def pages(js: str, _views: list[list[dict]] = list(views)) -> str:
        if "el.scrollTop = 0" in js:
            return json.dumps({"ok": True, "before": 7656, "after": 0})
        if "scrollIntoView" in js:
            return json.dumps({"ok": True, "method": "scrollTop"})
        return json.dumps(_views.pop(0) if len(_views) > 1 else _views[0])

    got = _lane.collect_eligible(pages, 5, max_scrolls=10)
    assert got["from_top"] is True and got["eligible"] == ["a", "b", "c", "d", "e", "f"]
    assert got["scrolls"] == 2 and got["rows_seen"] == 6 and got["reached_requested"] is True
    same = _lane.collect_eligible(
        lambda js: json.dumps({"ok": True, "method": "scrollTop"})
        if "scrollIntoView" in js else json.dumps(views[0]), 20, max_scrolls=99)
    assert same["eligible"] == ["a", "b"] and same["scrolls"] <= 3
    assert same["reached_requested"] is False
    blind_list = _lane.collect_eligible(
        lambda js: json.dumps({"ok": True, "method": "scrollTop"})
        if "scrollIntoView" in js else json.dumps([{"convid": "a", "unread": False}]),
        5, max_scrolls=3)
    assert blind_list["eligible"] == [] and blind_list["observable"] is False
    selected = {"produced": "a", "selected_attr_seen": True}
    assert _lane.classify({**selected, "selected": "a", "selected_count": 1}, "a") == "landed"
    assert _lane.classify({**selected, "selected": "b", "selected_count": 1}, "a") == "mismatch"
    assert _lane.classify({**selected, "produced": "b", "selected": None}, "a") == "mismatch"
    assert _lane.classify({**selected, "selected": None, "selected_count": 0,
                           "target_rendered": True, "target_selected": False}, "a") == "mismatch"
    assert _lane.classify({**selected, "selected": None, "selected_count": 0,
                           "target_rendered": False}, "a") == "unconfirmed"
    absent = {"produced": "a", "selected": None, "selected_count": 0,
              "selected_attr_seen": True, "target_rendered": False,
              "ready_s": 1.5, "waited_s": 20.0}

    def found_after(n: int, selected_id: str | None = "a") -> object:
        state = {"scrolls": 0}

        def evaluate(js: str) -> str:
            if "el.scrollTop = 0" in js:
                return json.dumps({"ok": True, "before": 7656, "after": 0})
            if "scrollIntoView" in js:
                state["scrolls"] += 1
                return json.dumps({"ok": True, "method": "scrollTop"})
            if state["scrolls"] < n:
                return json.dumps(dict(absent, body_chars=536))
            return json.dumps({"produced": "a", "selected": selected_id,
                               "selected_count": 1 if selected_id else 0,
                               "selected_attr_seen": True, "target_rendered": True,
                               "target_selected": selected_id == "a", "body_chars": 536})
        return evaluate

    recovered = _lane.recover_selection(found_after(2), "a", absent)
    assert recovered["recovery_steps"] == 2 and _lane.classify(recovered, "a") == "landed"
    negative = _lane.recover_selection(found_after(1, None), "a", absent)
    assert negative["target_rendered"] is True and _lane.classify(negative, "a") == "mismatch"
    never = _lane.recover_selection(
        lambda js: json.dumps({"ok": True, "method": "scrollTop"})
        if "scrollIntoView" in js else json.dumps(absent), "a", absent, steps=2)
    assert never["recovery_steps"] == 2 and _lane.classify(never, "a") == "unconfirmed"
    stuck = _lane.recover_selection(
        lambda js: json.dumps({"ok": False, "reason": "no-rows"})
        if "scrollIntoView" in js else json.dumps(absent), "a", absent)
    assert stuck["recovery_steps"] == 0 and _lane.classify(stuck, "a") == "unconfirmed"
    _check_verdict_metrics()


def _check_off_lane_entrypoints() -> None:
    clean = [{"seq": 1, "attempt": 1, "intended": "a",
              "target_produced_pre": "z", "target_produced": "a",
              "outcome": "landed"}]
    stuck = [{"seq": 1, "attempt": n, "intended": "a",
              "target_produced_pre": "z", "target_produced": "z",
              "outcome": "mismatch"} for n in (1, 2)]
    same_point = [
        {"seq": 1, "attempt": 1, "intended": "a", "point": {"x": 60, "y": 9},
         "target_produced_pre": "z", "target_produced": "z", "outcome": "mismatch"},
        {"seq": 1, "attempt": 2, "intended": "a", "point": {"x": 60, "y": 9},
         "retarget_changed": "re-queried and clicked again",
         "target_produced_pre": "z", "target_produced": "a",
         "outcome": "landed-on-retarget"},
    ]
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "l.json").write_text(json.dumps(
            {"list_rows": [{"convid": "a", "marks_unread": True, "marks_read": False},
                           {"convid": "b", "marks_read": True}]}
        ))
        assert _lane._select(directory / "l.json", 5) == 0
        (directory / "none.json").write_text(json.dumps([{"convid": "a", "unread": False}]))
        assert _lane._select(directory / "none.json", 5) == 3
        (directory / "ok.json").write_text(json.dumps({"lane": "iab", "attempts": clean}))
        assert _lane._score(directory / "ok.json", directory / "sub" / "r.json") == 0
        assert json.loads((directory / "sub" / "r.json").read_text())["verdict"].startswith("CLEAN")
        (directory / "bad.json").write_text(json.dumps({"lane": "iab", "attempts": stuck}))
        assert _lane._score(directory / "bad.json", None) == 2
        (directory / "fake.json").write_text(json.dumps({"lane": "iab", "attempts": same_point}))
        assert _lane._score(directory / "fake.json", None) == 2
        (directory / "short.json").write_text(json.dumps(
            {"lane": "iab", "rows_requested": 20, "attempts": clean}))
        assert _lane._score(directory / "short.json", None) == 2
        (directory / "full.json").write_text(json.dumps(
            {"lane": "iab", "rows_requested": 1, "attempts": clean}))
        assert _lane._score(directory / "full.json", None) == 0


def _self_check() -> int:
    """Run each report, navigation, recovery, and off-lane check case."""
    _check_basic_verdicts()
    _check_read_state_and_contract()
    _check_navigation_contract()
    _check_deep_link_and_wait()
    _check_collection_recovery_and_verdicts()
    _check_off_lane_entrypoints()
    print("self-check OK")
    return 0


__all__ = [
    "_stats",
    "_self_check",
    "_check_basic_verdicts",
    "_check_collection_recovery_and_verdicts",
    "_check_deep_link_and_wait",
    "_check_navigation_contract",
    "_check_off_lane_entrypoints",
    "_check_read_state_and_contract",
    "summarize",
    "verdict",
]
