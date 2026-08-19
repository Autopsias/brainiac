"""Own the lane's eligible-attempt mechanics."""

from __future__ import annotations

import json
import time
from pathlib import Path

import cos_lane_rehearsal as _lane


def collect_eligible(
    ev: object, want: int, max_scrolls: int = 20
) -> dict:
    """Collect already-read rows from successive virtualized-list views."""
    eligible: list[str] = []
    pool: set[str] = set()
    seen: set[str] = set()
    unread: set[str] = set()
    observable, scrolls, stagnant = False, 0, 0
    scroll_method: str | None = None
    from_top = _lane._to_top(ev)
    while True:
        rows = json.loads(ev(_lane._LIST_JS))
        observed, candidates = _lane.read_state(rows)
        observable = observable or observed
        new = 0
        for row in rows:
            conversation_id = row.get("convid")
            if not conversation_id:
                continue
            if conversation_id not in seen:
                seen.add(conversation_id)
                new += 1
            if row.get("unread"):
                unread.add(conversation_id)
        for conversation_id in candidates:
            if conversation_id not in pool:
                pool.add(conversation_id)
                eligible.append(conversation_id)
        if len(eligible) >= want:
            break
        stagnant = stagnant + 1 if (scrolls and not new) else 0
        if stagnant >= 2 or scrolls >= max_scrolls:
            break
        scroll = json.loads(ev(_lane._SCROLL_JS))
        if not scroll.get("ok"):
            break
        scroll_method = scroll.get("method")
        scrolls += 1
        time.sleep(0.6)
    return {
        "eligible": eligible,
        "observable": observable,
        "seen": seen,
        "rows_seen": len(seen),
        "unread": len(unread),
        "scrolls": scrolls,
        "scroll_method": scroll_method,
        "from_top": from_top,
        "rows_requested": want,
        "reached_requested": len(eligible) >= want,
    }


def _parse_convids(spec: str) -> list[str]:
    """Parse comma, whitespace, and @file conversation-id specifications."""
    if not spec:
        return []
    if spec.startswith("@"):
        spec = Path(spec[1:]).expanduser().read_text()
    result: list[str] = []
    for raw in spec.replace(",", "\n").split():
        conversation_id = raw.strip().strip('"\'')
        if conversation_id and conversation_id not in result:
            result.append(conversation_id)
    return result


def _fingerprint(attempt: dict) -> tuple | None:
    """Describe the concrete action an attempt performed."""
    point, url = attempt.get("point"), attempt.get("nav_url")
    if not point and not url:
        return None
    return (attempt.get("method"), json.dumps(point, sort_keys=True), url)


def _attempt_contract_problems(index: int, attempt: dict) -> list[str]:
    problems: list[str] = []
    where = f"attempt[{index}]"
    for key in ("seq", "attempt", "intended", "outcome"):
        if attempt.get(key) in (None, ""):
            problems.append(f"{where}: missing {key}")
    via = attempt.get("corroborated_via")
    if via not in (None, "direct", "recovery"):
        problems.append(f"{where}: unknown corroborated_via {via!r} (v5.57)")
    elif via == "recovery" and not attempt.get("recovery_steps"):
        problems.append(f"{where}: corroborated_via 'recovery' names no "
                        "recovery_steps (v5.57)")
    if attempt.get("outcome") != "already-open-skipped":
        for key in ("target_produced_pre", "target_produced"):
            if key not in attempt:
                problems.append(f"{where}: missing {key} (E30(a)/(d))")
    return problems


def _retarget_contract_problems(attempts: list[dict]) -> list[str]:
    by_sequence: dict[object, list[dict]] = {}
    for attempt in attempts:
        by_sequence.setdefault(attempt.get("seq"), []).append(attempt)
    problems: list[str] = []
    for sequence, rows in by_sequence.items():
        second = [row for row in rows if row.get("attempt") == 2]
        if not second:
            continue
        first = [row for row in rows if row.get("attempt") == 1]
        if not first:
            problems.append(f"seq {sequence}: attempt 2 with no attempt-1 row (E30(a))")
            continue
        if not second[0].get("retarget_changed"):
            problems.append(f"seq {sequence}: re-target names no change (E30(e))")
            continue
        second_fingerprint = _fingerprint(second[0])
        if second_fingerprint and second_fingerprint == _fingerprint(first[-1]):
            action = (f"clicked the SAME point {second[0]['point']}"
                      if second[0].get("point") else
                      f"navigated to the SAME URL {second[0].get('nav_url')}")
            problems.append(f"seq {sequence}: re-target {action} — not a re-target (E30(e))")
    return problems


def contract_problems(attempts: list[dict]) -> list[str]:
    """Return structural scoring problems in a model-provided attempt record."""
    problems: list[str] = []
    for index, attempt in enumerate(attempts):
        problems.extend(_attempt_contract_problems(index, attempt))
    problems.extend(_retarget_contract_problems(attempts))
    return problems


def _open_once(
    win: int, tab: int, convid: str, dx: int, settle: float
) -> dict:
    raw = _lane._ev(win, tab, _lane._OPEN_JS % {
        "convid": json.dumps(convid), "dx": dx,
    })
    click = json.loads(raw)
    if not click.get("ok"):
        return {"outcome": "no-click", "detail": click.get("reason"),
                "method": "click", **click}
    time.sleep(settle)
    produced = json.loads(_lane._ev(win, tab, _lane._PANE_JS))["produced"]
    click["produced"], click["method"] = produced, "click"
    if not produced:
        click["outcome"] = "no-id"
    elif produced == convid:
        click["outcome"] = "landed"
    else:
        click["outcome"] = "mismatch"
    return click


def await_ready(ev: object, convid: str, timeout: float) -> dict:
    """Poll identity and body readiness until the bounded timeout."""
    js = _lane._AFTER_JS % {"convid": json.dumps(convid)}
    started, error = time.time(), None
    last: dict = {"produced": "", "selected": None}
    ready_at, previous_body = None, None
    while True:
        try:
            last = json.loads(ev(js))
            if last.get("ready"):
                if ready_at is None:
                    ready_at = time.time() - started
                body = last.get("body_chars")
                if body and body == previous_body:
                    break
                previous_body = body
        except Exception as exc:  # noqa: BLE001 — a page read can fail mid-load
            error = str(exc)[:160]
        if time.time() - started >= timeout:
            last["body_settle_timed_out" if ready_at is not None
                 else "ready_timed_out"] = True
            break
        time.sleep(0.25)
    last["ready_s"] = round(ready_at, 2) if ready_at is not None else None
    last["waited_s"] = round(time.time() - started, 2)
    if error and not last.get("produced"):
        last["read_error"] = error
    return last


def classify(after: dict, convid: str) -> str:
    """Classify a deep-link reading without treating an absent row as negative."""
    produced, selected = after.get("produced"), after.get("selected")
    body = after.get("body_chars")
    if not produced:
        if isinstance(body, int) and not isinstance(body, bool) and body <= _lane._SHELL_CHARS:
            return "navigation-refused"
        return "no-id"
    if produced != convid:
        return "mismatch"
    if selected == convid:
        return "landed"
    if selected is not None:
        return "mismatch"
    if (after.get("target_rendered") and after.get("selected_attr_seen")
            and after.get("selected_count") == 0):
        return "mismatch"
    return "unconfirmed"


def recover_selection(
    ev: object, convid: str, after: dict, steps: int = _lane._RECOVERY_STEPS
) -> dict:
    """Scroll a target back into view and re-read the same assertion."""
    reading, taken = _lane.bring_into_list(ev, convid, steps)
    return {**after, **reading, "recovery_steps": taken}


def bring_into_list(ev: object, convid: str, steps: int) -> tuple[dict, int]:
    """Search from the top for a target row without opening it."""
    reading: dict = {}
    js = _lane._AFTER_JS % {"convid": json.dumps(convid)}
    taken, stalled = 0, 0
    _lane._to_top(ev)
    while True:
        try:
            reading = json.loads(ev(js))
        except Exception as exc:  # noqa: BLE001 — preserve the last readable state
            reading = {**reading, "recovery_error": str(exc)[:160]}
        if reading.get("target_rendered") or taken >= steps:
            break
        try:
            scroll = json.loads(ev(_lane._SCROLL_JS))
        except Exception as exc:  # noqa: BLE001 — preserve the last readable state
            reading = {**reading, "recovery_error": str(exc)[:160]}
            break
        if not scroll.get("ok"):
            stalled += 1
            if stalled >= 3:
                reading = {**reading, "scroll_stalled": scroll.get("reason")}
                break
            time.sleep(0.8)
            continue
        stalled = 0
        taken += 1
        time.sleep(0.6)
    return reading, taken


def _navigate_once(
    win: int,
    tab: int,
    convid: str,
    settle: float,
    base: str | None = None,
) -> dict:
    """Attempt a deep-link open, recovering the app-produced selection signal."""
    try:
        navigation = json.loads(_lane._ev(win, tab, _lane._NAV_JS % {
            "convid": json.dumps(convid),
        }))
    except Exception as exc:  # noqa: BLE001 — continue to the page assertion
        navigation = {"ok": True, "id": convid, "pre": None, "url": None,
                      "nav_eval_error": str(exc)[:160]}
    if (not navigation.get("ok") and base
            and navigation.get("reason") == "not-on-a-mail-folder-url"):
        navigation = json.loads(_lane._ev(win, tab, _lane._GOTO_JS % {
            "url": json.dumps(_lane.deep_link(convid, base)),
            "convid": json.dumps(convid),
        }))
    if not navigation.get("ok"):
        return {"outcome": "no-click", "detail": navigation.get("reason"),
                "method": "navigate", **navigation}

    def evaluate(js: str) -> str:
        return _lane._ev(win, tab, js)

    after = _lane.await_ready(evaluate, convid, settle)
    outcome = _lane.classify(after, convid)
    recovered = False
    if (outcome == "unconfirmed" and not after.get("target_rendered")
            and after.get("selected_attr_seen")):
        after = _lane.recover_selection(evaluate, convid, after)
        outcome = _lane.classify(after, convid)
        recovered = True
    navigation.update(after, method="navigate", produced=after.get("produced"),
                       nav_url=navigation.get("url"), outcome=outcome)
    if outcome == "landed":
        navigation["corroborated_via"] = "recovery" if recovered else "direct"
    return navigation


def _reach_and_click(
    win: int, tab: int, convid: str, dx: int
) -> tuple[dict, int]:
    """Reach a refused row before taking the bounded click fallback."""
    evaluate = lambda js: _lane._ev(win, tab, js)  # noqa: E731
    reading, steps = _lane.bring_into_list(evaluate, convid, _lane._REACH_STEPS)
    if not reading.get("target_rendered"):
        return ({"outcome": "row-unreachable", "method": "click",
                 "detail": f"row-not-rendered-after-{steps}-scroll-step(s)",
                 "pre": reading.get("produced") or None, "produced": None,
                 "rows_rendered": reading.get("rows_rendered")}, steps)
    click = _lane._open_once(win, tab, convid, dx, _lane._CLICK_SETTLE)
    if click.get("outcome") == "no-click":
        click["outcome"] = "row-unreachable"
        click["detail"] = f"click-refused-after-{steps}-scroll-step(s): " \
                          f"{click.get('detail') or 'unknown'}"
    if click.get("outcome") == "landed":
        after = _lane.await_ready(evaluate, convid, _lane._NAV_TIMEOUT)
        for key in ("body_chars", "rows_rendered", "selected", "selected_count",
                    "selected_attr_seen", "target_rendered", "ready_s",
                    "waited_s", "body_settle_timed_out"):
            if after.get(key) is not None:
                click[key] = after[key]
    return click, steps


__all__ = [
    "_fingerprint",
    "_navigate_once",
    "_open_once",
    "_parse_convids",
    "_reach_and_click",
    "await_ready",
    "bring_into_list",
    "classify",
    "collect_eligible",
    "contract_problems",
    "recover_selection",
]
