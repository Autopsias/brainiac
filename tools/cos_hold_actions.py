"""Coordinate the visible-tab hold lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, TypeAlias

_host: Any = None

HoldTarget: TypeAlias = tuple[int, int, str, dict]
PriorTab: TypeAlias = tuple[int | None, int | None]


def configure_host(host: Any) -> None:
    """Bind the compatibility facade used for monkeypatchable host calls."""
    global _host
    _host = host


def _window_order() -> list[str]:
    """Read Chrome window ids in AppleScript order."""
    out = _host._osa('tell application "Google Chrome"\n'
                     '  set r to ""\n'
                     '  repeat with w from 1 to (count of windows)\n'
                     '    set r to r & (id of window w) & linefeed\n'
                     '  end repeat\n'
                     '  return r\n'
                     'end tell')
    return [line.strip() for line in out.splitlines() if line.strip()]


def _observe(win: int, tab: int) -> dict:
    """Collect best-effort evidence for a hidden target tab."""
    observation: dict = {}
    probes = (
        ("frontmost_app", _host._frontmost_app),
        ("window_order", _window_order),
        ("target_window_id", lambda: _host._osa(
            'on run argv\n  tell application "Google Chrome" to id of window '
            '((item 1 of argv) as integer)\nend run', str(win))),
        ("target_window_active_tab", lambda: _host._osa(
            'on run argv\n  tell application "Google Chrome" to active tab index '
            'of window ((item 1 of argv) as integer)\nend run', str(win))),
    )
    for key, probe in probes:
        try:
            observation[key] = probe()
        except Exception as exc:  # noqa: BLE001 — failure evidence is best effort
            observation[key] = None
            observation[f"{key}_error"] = str(exc)[:160]
    order = observation.get("window_order")
    window_id = observation.get("target_window_id")
    observation["target_is_front_window"] = (
        bool(order) and bool(window_id) and order[0] == window_id
    )
    try:
        observation["target_is_active_tab"] = (
            int(observation["target_window_active_tab"]) == tab
        )
    except (TypeError, ValueError):
        observation["target_is_active_tab"] = None
    return observation


def _restore(app: str | None, win_id: int | None, tab_index: PriorTab) -> dict:
    """Restore the prior window order, tab, and frontmost application."""
    done = {"window_order": False, "active_tab": False, "frontmost_app": False}
    try:
        if win_id is not None:
            _host._osa('on run argv\n'
                       '  set wid to (item 1 of argv) as integer\n'
                       '  tell application "Google Chrome"\n'
                       '    repeat with w from 1 to (count of windows)\n'
                       '      if (id of window w) is wid then set index of window w to 1\n'
                       '    end repeat\n'
                       '  end tell\n'
                       'end run', str(win_id))
            done["window_order"] = True
    except Exception:
        pass
    try:
        if tab_index[0] is not None:
            wid, tab = tab_index
            _host._osa('on run argv\n'
                       '  set wid to (item 1 of argv) as integer\n'
                       '  set ti to (item 2 of argv) as integer\n'
                       '  tell application "Google Chrome"\n'
                       '    repeat with w from 1 to (count of windows)\n'
                       '      if (id of window w) is wid then set active tab index of window w to ti\n'
                       '    end repeat\n'
                       '  end tell\n'
                       'end run', str(wid), str(tab))
            done["active_tab"] = True
    except Exception:
        pass
    try:
        if app and app != "Google Chrome":
            _host._osa('on run argv\n'
                       '  tell application "System Events" to set frontmost of '
                       'process (item 1 of argv) to true\n'
                       'end run', app)
            done["frontmost_app"] = True
    except Exception:
        pass
    return done


_BEAT = 0


def _write(path: str | None, payload: dict) -> None:
    """Write a status payload with a monotonic beat and timestamp."""
    global _BEAT
    _BEAT += 1
    payload = {
        **payload,
        "beat": _BEAT,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not path:
        return
    try:
        with open(path, "w") as handle:
            json.dump(payload, handle, indent=1)
    except OSError:
        pass


def _capture_prior_context(
    args: argparse.Namespace,
) -> tuple[float, str | None, int | None, PriorTab]:
    started = time.time()
    prior_app = None
    try:
        prior_app = _host._frontmost_app()
    except Exception:
        pass
    return started, prior_app, _host._front_window_id(), (None, None)


def _resolve_initial_target(
    args: argparse.Namespace,
) -> tuple[HoldTarget | None, int]:
    try:
        target = _host._pick(args.match, args.exact_url, args.tab_id)
    except LookupError as exc:
        _write(args.status_file, {"status": "no-owa-tab", "detail": str(exc)})
        print(json.dumps({"status": "no-owa-tab", "detail": str(exc)}))
        return None, 3
    return target, 0


def _remember_prior_tab(args: argparse.Namespace, target: HoldTarget) -> PriorTab:
    window, _tab, _url, _state = target
    try:
        window_id = int(_host._osa(
            'on run argv\n  tell application "Google Chrome" to id of window '
            '((item 1 of argv) as integer)\nend run', str(window)))
        tab_index = int(_host._osa(
            'on run argv\n  tell application "Google Chrome" to active tab index of window '
            '((item 1 of argv) as integer)\nend run', str(window)))
        return window_id, tab_index
    except Exception:
        return None, None


def _resolve_acquire_target(
    args: argparse.Namespace, state: dict
) -> HoldTarget | None:
    try:
        target = _host._pick(args.match, args.exact_url, args.tab_id)
        state.update({"w": target[0], "t": target[1], "u": target[2], "last_state": target[3]})
        return target
    except _host.OsaUnavailable:
        raise
    except LookupError as exc:
        state.update({"reason": "tab-disappeared", "last_error": str(exc)[:200]})
        _write(args.status_file, {
            "status": "acquiring",
            "attempts": state["attempts"],
            "elapsed": round(time.time() - state["started"], 1),
            "reason_so_far": state["reason"],
            "last_error": state["last_error"],
        })
        time.sleep(0.5)
        return None


def _assert_acquire_target(
    args: argparse.Namespace, state: dict, target: HoldTarget
) -> bool:
    window, tab, url, observation = target
    if observation.get("js_unavailable") and (args.exact_url or args.tab_id):
        state.update({"acquired_at": time.time(), "verified": False})
        return True
    if observation.get("js_unavailable"):
        state.update({"reason": "js-from-apple-events-off", "last_error": "no tab selector given"})
    if observation.get("vis") == "visible":
        state["acquired_at"] = time.time()
        return True
    try:
        before = _window_order()
        _host._assert_visible(window, tab, args.tab_id)
        state["order_changed"] = _window_order()[:1] != before[:1]
    except _host.OsaUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — retry transient AppleScript failures
        state.update({"reason": "osascript-failed", "last_error": str(exc)[:200]})
        _write(args.status_file, {
            "status": "acquiring",
            "attempts": state["attempts"],
            "elapsed": round(time.time() - state["started"], 1),
            "reason_so_far": state["reason"],
            "last_error": state["last_error"],
        })
        time.sleep(0.5)
        return False
    time.sleep(0.6)
    try:
        refreshed = _host._pick(args.match, args.exact_url, args.tab_id)
        state.update({"w": refreshed[0], "t": refreshed[1], "u": refreshed[2],
                      "last_state": refreshed[3]})
    except _host.OsaUnavailable:
        raise
    except LookupError as exc:
        state.update({"reason": "tab-disappeared", "last_error": str(exc)[:200]})
        time.sleep(0.5)
        return False
    except Exception as exc:  # noqa: BLE001 — retry transient AppleScript failures
        state.update({"reason": "osascript-failed", "last_error": str(exc)[:200]})
        time.sleep(0.5)
        return False
    if state["last_state"].get("vis") == "visible":
        state["acquired_at"] = time.time()
        return True
    state["reason"] = "page-stayed-hidden"
    _write(args.status_file, {
        "status": "acquiring",
        "attempts": state["attempts"],
        "elapsed": round(time.time() - state["started"], 1),
        "reason_so_far": state["reason"],
        "visibilityState": state["last_state"].get("vis"),
        "window_order_changed": state["order_changed"],
        "window": state["w"], "tab": state["t"], "url": state["u"][:80],
    })
    return False


def _acquire_visible(
    args: argparse.Namespace, started: float, target: HoldTarget
) -> dict:
    state: dict = {
        "started": started,
        "w": target[0], "t": target[1], "u": target[2],
        "last_state": target[3],
        "acquired_at": None, "verified": True, "attempts": 0,
        "last_error": None, "reason": "page-stayed-hidden",
        "order_changed": None,
    }
    while time.time() - started < args.acquire_timeout:
        state["attempts"] += 1
        current = _resolve_acquire_target(args, state)
        if current is not None and _assert_acquire_target(args, state, current):
            break
    return state


def _refuse_acquisition(
    args: argparse.Namespace,
    state: dict,
    prior_app: str | None,
    prior_win_id: int | None,
    prior_tab: PriorTab,
) -> int:
    _host._restore(prior_app, prior_win_id, prior_tab)
    last_state = state.get("last_state") or {}
    payload = {
        "status": "could-not-acquire",
        "reason": state["reason"],
        "held_seconds": 0,
        "attempts": state["attempts"],
        "acquire_timeout": args.acquire_timeout,
        "last_error": state["last_error"],
        "visibilityState": last_state.get("vis"),
        "rows": last_state.get("rows"),
        "window_order_changed": state["order_changed"],
        "observed": _observe(state["w"], state["t"]),
        "detail": _host._REFUSAL_DETAIL.get(state["reason"], ""),
        "window": state["w"], "tab": state["t"], "url": state["u"][:80],
    }
    _write(args.status_file, payload)
    print(json.dumps(payload))
    return 2


def _stop_reason(args: argparse.Namespace, acquired_at: float) -> str | None:
    if args.stop_file:
        try:
            open(args.stop_file).close()
            return "stop-file"
        except OSError:
            pass
    if args.heartbeat_file:
        try:
            last = os.path.getmtime(args.heartbeat_file)
        except OSError:
            last = acquired_at
        if time.time() - max(last, acquired_at) > args.max_idle:
            return "idle"
    return None


def _sample_hold(args: argparse.Namespace, state: dict) -> None:
    try:
        target = _host._pick(args.match, args.exact_url, args.tab_id)
        state.update({"w": target[0], "t": target[1], "u": target[2]})
        state["samples"] += 1
        state["consecutive_failures"], state["last_error"] = 0, None
        if target[3].get("vis") == "visible":
            state["visible_samples"] += 1
        else:
            _host._assert_visible(target[0], target[1], args.tab_id)
            state["reasserts"] += 1
    except _host.OsaUnavailable as exc:
        state.update({"stopped": "apple-events-denied", "last_error": str(exc)[:200]})
        state["assert_failures"] += 1
    except Exception as exc:  # noqa: BLE001 — a lost tab is a measured degradation
        state["assert_failures"] += 1
        state["consecutive_failures"] += 1
        state["last_error"] = str(exc)[:200]
        if state["consecutive_failures"] >= args.max_assert_failures:
            state["stopped"] = "lost-tab"
            _write(args.status_file, {
                "status": "holding-degraded", "acquired": True,
                "verified": state["verified"],
                "held_seconds": round(time.time() - state["acquired_at"], 1),
                "samples": state["samples"],
                "visible_samples": state["visible_samples"],
                "reasserts": state["reasserts"],
                "assert_failures": state["assert_failures"],
                "consecutive_failures": state["consecutive_failures"],
                "last_error": state["last_error"],
                "window": state["w"], "tab": state["t"], "url": state["u"][:80],
            })


def _write_holding_status(args: argparse.Namespace, state: dict) -> None:
    _write(args.status_file, {
        "status": "holding" if state["consecutive_failures"] == 0
                  else "holding-degraded",
        "acquired": True, "verified": state["verified"],
        "held_seconds": round(time.time() - state["acquired_at"], 1),
        "samples": state["samples"], "visible_samples": state["visible_samples"],
        "reasserts": state["reasserts"],
        "assert_failures": state["assert_failures"],
        "consecutive_failures": state["consecutive_failures"],
        "last_error": state["last_error"],
        "window": state["w"], "tab": state["t"], "url": state["u"][:80],
    })


def _hold_until_release(
    args: argparse.Namespace, state: dict, acquired_at: float
) -> None:
    state.update({"samples": 0, "visible_samples": 0, "reasserts": 0,
                  "assert_failures": 0, "consecutive_failures": 0,
                  "last_error": None, "stopped": "deadline"})
    deadline = acquired_at + args.seconds
    while time.time() < deadline:
        reason = _stop_reason(args, acquired_at)
        if reason is not None:
            state["stopped"] = reason
            break
        _sample_hold(args, state)
        _write_holding_status(args, state)
        if state["stopped"] in {"apple-events-denied", "lost-tab"}:
            break
        time.sleep(args.interval)


def _release_payload(
    args: argparse.Namespace,
    state: dict,
    acquired_at: float,
    prior_app: str | None,
    prior_win_id: int | None,
    prior_tab: PriorTab,
) -> int:
    restored = _host._restore(prior_app, prior_win_id, prior_tab)
    held = round(time.time() - acquired_at, 1)
    samples = state["samples"]
    payload = {
        "status": "released", "stopped_by": state["stopped"],
        "acquired": True, "verified": state["verified"],
        "budget_seconds": args.seconds,
        "released_early": state["stopped"] != "deadline",
        "held_seconds": held,
        "samples": samples, "visible_samples": state["visible_samples"],
        "visible_fraction": round(state["visible_samples"] / samples, 3)
        if samples else None,
        "reasserts": state["reasserts"],
        "assert_failures": state["assert_failures"],
        "last_error": state["last_error"],
        "window": state["w"], "tab": state["t"], "url": state["u"][:80],
        "restored": restored, "prior_frontmost_app": prior_app,
    }
    _write(args.status_file, payload)
    print(json.dumps(payload))
    return 0


def cmd_hold(args: argparse.Namespace) -> int:
    """Acquire visibility, hold it, and restore the owner's display state."""
    started, prior_app, prior_win_id, prior_tab = _capture_prior_context(args)
    target, error = _resolve_initial_target(args)
    if target is None:
        return error
    prior_tab = _remember_prior_tab(args, target)
    state = _acquire_visible(args, started, target)
    if state["acquired_at"] is None:
        return _refuse_acquisition(args, state, prior_app, prior_win_id, prior_tab)
    acquired_at = state["acquired_at"]
    _hold_until_release(args, state, acquired_at)
    return _release_payload(args, state, acquired_at, prior_app, prior_win_id, prior_tab)


_BY_ID = ('  repeat with wi from 1 to (count of windows)\n'
          '    set w to window wi\n'
          '    repeat with ti from 1 to (count of tabs of w)\n'
          '      set t to tab ti of w\n'
          '      if ((id of t) as text) is tid then\n')


def eval_js_by_id(tab_id: int, js: str, timeout: float = 30) -> str:
    """Run JavaScript in the tab with a stable Chrome id."""
    return _host._osa(
        'on run argv\n'
        '  set tid to (item 1 of argv) as text\n'
        '  set js to item 2 of argv\n'
        '  tell application "Google Chrome"\n'
        + _BY_ID +
        '        return (execute t javascript js)\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  end tell\n'
        '  error "no Chrome tab with id " & tid\n'
        'end run', str(tab_id), js, timeout=timeout)


def assert_visible_by_id(tab_id: int, timeout: float = 30) -> None:
    """Make a stable-id tab active, front its window, and front Chrome."""
    _host._osa(
        'on run argv\n'
        '  set tid to (item 1 of argv) as text\n'
        '  tell application "Google Chrome"\n'
        '  repeat with wi from 1 to (count of windows)\n'
        '    set w to window wi\n'
        '    repeat with ti from 1 to (count of tabs of w)\n'
        '      if ((id of tab ti of w) as text) is tid then\n'
        '        set active tab index of w to ti\n'
        '        set index of w to 1\n'
        '        activate\n'
        '        return "ok"\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  end tell\n'
        '  error "no Chrome tab with id " & tid\n'
        'end run', str(tab_id), timeout=timeout)


__all__ = [
    "_BY_ID",
    "_BEAT",
    "_observe",
    "_restore",
    "_window_order",
    "_write",
    "assert_visible_by_id",
    "cmd_hold",
    "configure_host",
    "eval_js_by_id",
]
