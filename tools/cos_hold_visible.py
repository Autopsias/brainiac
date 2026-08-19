#!/usr/bin/env python3
"""Raise the Chrome window holding OWA and HOLD it visible, then put it back."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = "outlook.cloud.microsoft"
_PROBE = (
    "JSON.stringify({"
    "vis:document.visibilityState,"
    "focus:document.hasFocus(),"
    "rows:document.querySelectorAll('[role=\"option\"][data-convid]').length"
    "})"
)


class JsUnavailable(RuntimeError):
    """Chrome disallows JavaScript from Apple Events."""


class OsaUnavailable(RuntimeError):
    """The host cannot reach AppleScript at all."""


class _HostProxy:
    """Expose live facade globals so action helpers honor monkeypatches."""

    def __getattr__(self, name: str) -> object:
        return globals()[name]


_REFUSAL_DETAIL = {
    "page-stayed-hidden":
        "the tab was found and read, and stayed `hidden` through every "
        "assert. On Chrome 151 `set index of window N to 1` is a no-op, so "
        "this tool can only make visible a tab in the window Chrome is "
        "ALREADY showing. Read `observed`: if `target_is_front_window` is "
        "false, open the run-owned tab in the front window (or focus that "
        "window) — retrying will not help.",
    "tab-disappeared":
        "the tab named by --tab-id/--exact-url stopped resolving mid-acquire. "
        "It was closed, or an --exact-url binding was broken by a navigation "
        "(use --tab-id for any pass that navigates).",
    "js-from-apple-events-off":
        "Chrome > View > Developer > Allow JavaScript from Apple Events is "
        "OFF and no tab selector was given, so visibility cannot be verified "
        "and the right tab cannot be identified. Name the tab with --tab-id "
        "and this degrades to `verified: false` instead of refusing.",
    "osascript-failed":
        "an AppleScript call failed repeatedly; `last_error` carries it "
        "verbatim.",
}
_OSA_DENIED = (
    "com.apple.hiservices-xpcservice", "Connection invalid",
    "Not authorized to send Apple events", "-1743", "-10827", "-10810", "(-600)",
)


def _osa(script: str, *args: str, timeout: float = 30) -> str:
    try:
        process = subprocess.run(
            ["osascript", "-e", script, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise OsaUnavailable("osascript is not on this host") from exc
    if process.returncode != 0:
        error = process.stderr.strip() or f"osascript exit {process.returncode}"
        if "Executing JavaScript through AppleScript is turned off" in error:
            raise JsUnavailable(error)
        if any(marker in error for marker in _OSA_DENIED):
            raise OsaUnavailable(error)
        raise RuntimeError(error)
    return process.stdout.strip()


def _locate(match: str) -> list[tuple[int, int, str, str]]:
    """Return every Chrome tab whose URL contains the requested text."""
    output = _osa(
        'on run argv\n'
        '  set h to item 1 of argv\n'
        '  set r to ""\n'
        '  tell application "Google Chrome"\n'
        '    repeat with w from 1 to (count of windows)\n'
        '      repeat with t from 1 to (count of tabs of window w)\n'
        '        set u to URL of tab t of window w\n'
        '        if u contains h then set r to r & w & "\t" & t & "\t" & '
        '(id of tab t of window w) & "\t" & u & linefeed\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end tell\n'
        '  return r\n'
        'end run', match,
    )
    hits: list[tuple[int, int, str, str]] = []
    for line in output.splitlines():
        if line.strip():
            window, tab, tab_id, url = line.split("\t", 3)
            hits.append((int(window), int(tab), tab_id, url))
    return hits


def tab_id(win: int, tab: int) -> str:
    """Return Chrome's stable id for one tab."""
    return _osa(
        'on run argv\n'
        '  tell application "Google Chrome" to id of tab '
        '((item 2 of argv) as integer) of window ((item 1 of argv) as integer)\n'
        'end run', str(win), str(tab),
    )


def eval_js(win: int, tab: int, js: str, timeout: float = 30) -> str:
    """Evaluate JavaScript in a Chrome tab."""
    return _osa(
        'on run argv\n'
        '  set w to (item 1 of argv) as integer\n'
        '  set t to (item 2 of argv) as integer\n'
        '  set js to item 3 of argv\n'
        '  tell application "Google Chrome" to execute tab t of window w javascript js\n'
        'end run', str(win), str(tab), js, timeout=timeout,
    )


def _read(win: int, tab: int) -> dict:
    return json.loads(eval_js(win, tab, _PROBE))


def chrome_profiles() -> list[str]:
    """Return profile paths for top-level Chrome processes."""
    try:
        output = subprocess.run(
            ["ps", "-axo", "command"], capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001 — diagnostics must never fail the hold
        return []
    return parse_chrome_profiles(output)


def parse_chrome_profiles(ps_output: str) -> list[str]:
    """Parse top-level Chrome profile flags from a process listing."""
    profiles: list[str] = []
    for line in ps_output.splitlines():
        if "MacOS/Google Chrome" not in line or " --type=" in line:
            continue
        flag = "--user-data-dir="
        profiles.append(line.split(flag, 1)[1].split(" ")[0]
                        if flag in line else "<default>")
    return profiles


def _rival_chrome_hint() -> str:
    profiles = chrome_profiles()
    if len(profiles) < 2:
        return ""
    return (f". NOTE: {len(profiles)} Chrome instances are running "
            f"({', '.join(profiles)}) — AppleScript addresses only ONE of them, "
            "so this refusal may be about the WRONG browser. Quit the extra "
            "instance (the chrome-devtools MCP starts one under "
            "`chrome-devtools-mcp/chrome-profile`) and re-run")


def _pick(
    match: str, exact_url: str | None = None, want_id: str | None = None
) -> tuple[int, int, str, dict]:
    """Choose the matching OWA tab with the most useful rendered state."""
    hits = _locate(match)
    if want_id:
        hits = [hit for hit in hits if hit[2] == want_id]
        if not hits:
            raise LookupError(f"no Chrome tab whose id is {want_id!r}" + _rival_chrome_hint())
    if exact_url:
        hits = [hit for hit in hits if hit[3] == exact_url]
        if not hits:
            raise LookupError(f"no Chrome tab whose URL is exactly {exact_url!r}"
                              + _rival_chrome_hint())
    if not hits:
        raise LookupError(f"no Chrome tab whose URL contains {match!r}"
                          + _rival_chrome_hint())
    scored: list[tuple[tuple[int, int], int, int, str, dict]] = []
    for window, tab, _tab_id, url in hits:
        try:
            state = _read(window, tab)
        except JsUnavailable:
            state = {"vis": "unknown", "focus": None, "rows": -1,
                     "js_unavailable": True}
        except OsaUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — a tab may be mid-navigation
            state = {"vis": "unknown", "focus": None, "rows": -1,
                     "error": str(exc)[:120]}
        scored.append(((1 if "/mail" in url else 0, state.get("rows") or 0),
                       window, tab, url, state))
    scored.sort(key=lambda item: item[0], reverse=True)
    _, window, tab, url, state = scored[0]
    return window, tab, url, state


def _frontmost_app() -> str:
    return _osa('tell application "System Events" to name of first '
                'application process whose frontmost is true')


def _front_window_id() -> int | None:
    try:
        return int(_osa('tell application "Google Chrome" to id of window 1'))
    except Exception:
        return None


def _assert_visible(win: int, tab: int, want_id: str | None = None) -> None:
    """Activate the target tab, its window, and Chrome itself."""
    if want_id:
        assert_visible_by_id(want_id)
        _osa('tell application "System Events" to set frontmost of process '
             '"Google Chrome" to true')
        return
    _osa('on run argv\n'
         '  set w to (item 1 of argv) as integer\n'
         '  set t to (item 2 of argv) as integer\n'
         '  tell application "Google Chrome"\n'
         '    set active tab index of window w to t\n'
         '    set index of window w to 1\n'
         '  end tell\n'
         'end run', str(win), str(tab))
    _osa('tell application "System Events" to set frontmost of process '
         '"Google Chrome" to true')


from cos_hold_actions import (  # noqa: E402
    _BY_ID,
    _BEAT,
    _observe,
    _restore,
    _window_order,
    _write,
    assert_visible_by_id,
    cmd_hold,
    configure_host,
    eval_js_by_id,
)

configure_host(_HostProxy())


class _HelpFormatter(argparse.HelpFormatter):
    """Keep the root help alignment stable while preserving subcommand layout."""

    def _format_action(self, action: argparse.Action) -> str:
        if not action.option_strings:
            return super()._format_action(action)
        original_length = self._action_max_length
        self._action_max_length = max(original_length, 22)
        try:
            return super()._format_action(action)
        finally:
            self._action_max_length = original_length


def cmd_check(args: argparse.Namespace) -> int:
    try:
        window, tab, url, state = _pick(args.match, args.exact_url, args.tab_id)
    except LookupError as exc:
        print(json.dumps({"ok": False, "reason": "no-owa-tab", "detail": str(exc)}))
        return 3
    if state.get("js_unavailable"):
        print(json.dumps({
            "ok": False, "reason": "js-from-apple-events-off",
            "detail": "Chrome > View > Developer > Allow JavaScript from Apple "
                       "Events is OFF, so this tab cannot be read from the host. "
                       "The RAISE still works; verify visibility through your own "
                       "browser handle.",
            "window": window, "tab": tab, "url": url[:80],
        }))
        return 4
    output = {"ok": state.get("vis") == "visible",
              "visibilityState": state.get("vis"), "hasFocus": state.get("focus"),
              "rows": state.get("rows"), "window": window, "tab": tab,
              "url": url[:80]}
    print(json.dumps(output))
    return 0 if output["ok"] else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=_HelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="is the OWA tab visible right now?")
    check.add_argument("--match", default=HOST)
    check.add_argument("--exact-url", default=None,
                       help="target the tab whose URL is EXACTLY this "
                            "(a caller driving its own tab passes its location.href)")
    check.add_argument("--tab-id", default=None,
                       help="target the tab with this stable Chrome tab id — the "
                            "ONLY selector that survives the tab NAVIGATING, so a "
                            "deep-link body pass must use it, never --exact-url")
    check.set_defaults(fn=cmd_check)

    hold = sub.add_parser("hold", help="raise and hold the OWA tab visible")
    hold.add_argument("--match", default=HOST)
    hold.add_argument("--exact-url", default=None,
                      help="target the tab whose URL is EXACTLY this "
                           "(a caller driving its own tab passes its location.href)")
    hold.add_argument("--tab-id", default=None,
                      help="target the tab with this stable Chrome tab id — the "
                           "ONLY selector that survives the tab NAVIGATING, so a "
                           "deep-link body pass must use it, never --exact-url")
    hold.add_argument("--seconds", type=float, default=180.0)
    hold.add_argument("--interval", type=float, default=3.0)
    hold.add_argument("--acquire-timeout", type=float, default=25.0)
    hold.add_argument("--stop-file", default=None, help="release early once this file exists")
    hold.add_argument("--heartbeat-file", default=None,
                      help="the caller touches this while it is working the page; "
                           "no touch for --max-idle seconds releases the screen")
    hold.add_argument("--max-idle", type=float, default=90.0,
                      help="only applies with --heartbeat-file")
    hold.add_argument("--max-assert-failures", type=int, default=3,
                      help="consecutive failed re-asserts that end the hold with "
                           "stopped_by: \"lost-tab\" instead of going on "
                           "reporting `holding` while asserting nothing")
    hold.add_argument("--status-file", default=None)
    hold.set_defaults(fn=cmd_hold)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except OsaUnavailable as exc:
        print(json.dumps({
            "ok": False, "reason": "apple-events-denied",
            "detail": "this process cannot reach AppleScript. Grant it "
                       "Automation / Apple Events for \"Google Chrome\" and "
                       "\"System Events\" (System Settings > Privacy & Security "
                       "> Automation), or run it outside the sandbox that is "
                       "denying the XPC lookup.",
            "error": str(exc)[:300],
        }))
        return 5
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "reason": "osascript-failed",
                          "error": str(exc)[:300]}))
        return 6


if __name__ == "__main__":
    sys.exit(main())
