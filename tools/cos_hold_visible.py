#!/usr/bin/env python3
"""Raise the Chrome window holding OWA and HOLD it visible, then put it back.

WHY THIS EXISTS (measured, session s12, 2026-08-01). An OWA tab whose Chrome
window is covered reports `document.visibilityState: hidden`. Chrome then runs
ZERO `requestAnimationFrame` callbacks for that page, so OWA's virtualized
message list stops rendering rows: 11-12 of 178 conversations reachable while
hidden, 178 of 178 while visible; 0 sequential body opens while hidden, 17
consecutive at ~191 ms each while visible. A body pass on a hidden tab is not
slow, it is starved — and it reports as a doctrine failure.

WHAT DOES AND DOES NOT WORK, each one measured rather than assumed:

  * `document.hasFocus()` is NOT the signal — `hasFocus: true` was observed
    alongside `visibilityState: hidden, rows: 0`.
  * Chrome's own `activate`, AppleScript `set active tab index`, and raising a
    window by id all FAIL to flip the state on their own.
  * Overriding `document.visibilityState` from JS does NOT work: the frame
    suppression is in the renderer, not in the JS value.
  * Only `System Events -> set frontmost` flips it, AND it does not HOLD —
    another application reclaims the display within a minute or two. Hence the
    loop: assert, sample, re-assert.
  * Two maximized Chrome windows with identical bounds occlude each other, so
    the OWA tab must be the ACTIVE tab of a window that is itself at index 1.

BE A GOOD GUEST. This takes the owner's screen. It takes it for a bounded time,
it reports how much of that time it actually held, and on exit it restores the
frontmost application, the window that was at index 1, and the active tab of
the window it touched.

GIVE IT BACK THE MOMENT IT IS NOT BEING USED (2026-08-01, run 63). The budget
is a CEILING, never a plan: run 63 budgeted 3000 s, released on its stop-file at
891.5 s, and still took the owner's screen for **14.9 minutes to do ~2 minutes
of reading**. The gap is not the pass being slow, it is the pass THINKING
between opens while still holding the display. So `hold` now also watches a
caller-touched `--heartbeat-file`: no touch for `--max-idle` seconds and it
releases (`stopped_by: "idle"`) with the full restore. The heartbeat is
OPT-IN — without `--heartbeat-file` the budget/stop-file behaviour is exactly
what it was, so an older caller cannot be starved by a newer tool.

    cos_hold_visible.py check
    cos_hold_visible.py hold --seconds 180 --status-file /tmp/hold.json
    cos_hold_visible.py hold --seconds 900 --heartbeat-file /tmp/hb \
        --max-idle 90 --stop-file /tmp/stop --status-file /tmp/hold.json

`check` exits 0 when the OWA tab is visible, 2 when it is not, 3 when no OWA tab
exists, 4 when Chrome refuses host-side page reads, and 5 when the host cannot
reach AppleScript at all. `hold` exits 2 without holding if it cannot reach
`visible` inside --acquire-timeout: that is the honest refusal the caller should
ledger, not a reason to grind out five opens on a starved lane.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HOST = "outlook.cloud.microsoft"

# One round trip per sample, so keep the payload small.
_PROBE = (
    "JSON.stringify({"
    "vis:document.visibilityState,"
    "focus:document.hasFocus(),"
    "rows:document.querySelectorAll('[role=\"option\"][data-convid]').length"
    "})"
)


class JsUnavailable(RuntimeError):
    """Chrome's View > Developer > "Allow JavaScript from Apple Events" is off.

    Measured 2026-08-01: this flipped off mid-session under a running COS run,
    and every host-side probe that reads the page went blind at once. It does NOT
    stop the RAISE (window ordering and System Events need no JS) and it does not
    affect a caller driving the tab through its own browser handle — so it must
    degrade to "raised, not verified", never to a refusal that reads like the
    window could not be raised.
    """


class OsaUnavailable(RuntimeError):
    """The host cannot reach AppleScript AT ALL — a permission, not a browser.

    Measured 2026-08-01 (s13): run under a sandbox that denies mach-lookup,
    `check` died with a raw Python traceback, so a PERMISSIONS problem read as a
    crash and sent the reader looking at Chrome. The denial announces itself on
    osascript's stderr as `Connection Invalid error for service
    com.apple.hiservices-xpcservice` (and the script then fails to compile, so
    the tail of the message is a misleading syntax error); a TCC refusal is
    `Not authorized to send Apple events` / -1743; a launch refusal is -10827.
    All of them mean the same operator action: grant this process Automation /
    Apple Events, or run it outside the sandbox.
    """


# Substrings that mean "the AppleScript channel itself is unavailable to this
# process". Deliberately specific — a Chrome error must stay a Chrome error.
_OSA_DENIED = (
    "com.apple.hiservices-xpcservice",
    "Connection invalid",
    "Not authorized to send Apple events",
    "-1743",
    "-10827",
    "-10810",
    "(-600)",
)


def _osa(script: str, *args: str, timeout: float = 30) -> str:
    try:
        p = subprocess.run(["osascript", "-e", script, *args],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise OsaUnavailable("osascript is not on this host") from exc
    if p.returncode != 0:
        err = p.stderr.strip() or f"osascript exit {p.returncode}"
        if "Executing JavaScript through AppleScript is turned off" in err:
            raise JsUnavailable(err)
        if any(marker in err for marker in _OSA_DENIED):
            raise OsaUnavailable(err)
        raise RuntimeError(err)
    return p.stdout.strip()


def _locate(match: str) -> list[tuple[int, int, str]]:
    """(window, tab, url) for every Chrome tab whose URL contains `match`.

    Re-located on every call on purpose: raising a window renumbers AppleScript
    window indices, so a cached index silently addresses the wrong window.
    """
    out = _osa(
        'on run argv\n'
        '  set h to item 1 of argv\n'
        '  set r to ""\n'
        '  tell application "Google Chrome"\n'
        '    repeat with w from 1 to (count of windows)\n'
        '      repeat with t from 1 to (count of tabs of window w)\n'
        '        set u to URL of tab t of window w\n'
        '        if u contains h then set r to r & w & "\t" & t & "\t" & u & linefeed\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end tell\n'
        '  return r\n'
        'end run', match)
    hits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        w, t, u = line.split("\t", 2)
        hits.append((int(w), int(t), u))
    return hits


def _read(win: int, tab: int) -> dict:
    raw = _osa(
        'on run argv\n'
        '  set w to (item 1 of argv) as integer\n'
        '  set t to (item 2 of argv) as integer\n'
        '  set js to item 3 of argv\n'
        '  tell application "Google Chrome" to execute tab t of window w javascript js\n'
        'end run', str(win), str(tab), _PROBE)
    return json.loads(raw)


def _pick(match: str, exact_url: str | None = None) -> tuple[int, int, str, dict]:
    """The OWA tab actually showing a message list.

    Several OWA tabs can be open (the owner's, and the tab a run opened for
    itself), and a substring match cannot tell them apart — `/mail/inbox` is a
    prefix of the owner's `/mail/inbox/id/AAQk…`. A caller driving its own tab
    passes `--exact-url` with that tab's own `location.href`, which is exact and
    therefore unambiguous. Without it: prefer a mail URL, then the one with the
    most rendered rows, because a tab rendering rows is the one whose rendering
    we care about. Whichever is picked is REPORTED, so a wrong pick is visible
    rather than silent.
    """
    hits = _locate(match)
    if exact_url:
        hits = [h for h in hits if h[2] == exact_url]
        if not hits:
            raise LookupError(f"no Chrome tab whose URL is exactly {exact_url!r}")
    if not hits:
        raise LookupError(f"no Chrome tab whose URL contains {match!r}")
    scored = []
    for w, t, u in hits:
        try:
            st = _read(w, t)
        except JsUnavailable:
            st = {"vis": "unknown", "focus": None, "rows": -1, "js_unavailable": True}
        except OsaUnavailable:
            raise            # the channel died, not this tab — never score it
        except Exception as exc:  # a tab mid-navigation is not a fatal error
            st = {"vis": "unknown", "focus": None, "rows": -1, "error": str(exc)[:120]}
        scored.append(((1 if "/mail" in u else 0, st.get("rows") or 0), w, t, u, st))
    scored.sort(key=lambda s: s[0], reverse=True)
    _, w, t, u, st = scored[0]
    return w, t, u, st


def _frontmost_app() -> str:
    return _osa('tell application "System Events" to name of first '
                'application process whose frontmost is true')


def _front_window_id() -> int | None:
    try:
        return int(_osa('tell application "Google Chrome" to id of window 1'))
    except Exception:
        return None


def _assert_visible(win: int, tab: int) -> None:
    """Make `tab` the active tab of `win`, put `win` in front, front Chrome.

    All three steps, every time: the tab can be active in a window that is
    covered, and the window can be at index 1 inside an application that is
    behind a full-screen app. Only the combination renders.
    """
    _osa('on run argv\n'
         '  set w to (item 1 of argv) as integer\n'
         '  set t to (item 2 of argv) as integer\n'
         '  tell application "Google Chrome"\n'
         '    set active tab index of window w to t\n'
         '    set index of window w to 1\n'
         '  end tell\n'
         'end run', str(win), str(tab))
    # Chrome's own activate does not flip visibilityState; System Events does.
    _osa('tell application "System Events" to set frontmost of process '
         '"Google Chrome" to true')


def _restore(app: str | None, win_id: int | None, tab_index: int | None) -> dict:
    """Put back what we took: window order, active tab, frontmost app."""
    done = {"window_order": False, "active_tab": False, "frontmost_app": False}
    try:
        if win_id is not None:
            _osa('on run argv\n'
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
        if tab_index is not None and tab_index[0] is not None:
            wid, ti = tab_index
            _osa('on run argv\n'
                 '  set wid to (item 1 of argv) as integer\n'
                 '  set ti to (item 2 of argv) as integer\n'
                 '  tell application "Google Chrome"\n'
                 '    repeat with w from 1 to (count of windows)\n'
                 '      if (id of window w) is wid then set active tab index of window w to ti\n'
                 '    end repeat\n'
                 '  end tell\n'
                 'end run', str(wid), str(ti))
            done["active_tab"] = True
    except Exception:
        pass
    try:
        if app and app != "Google Chrome":
            _osa('on run argv\n'
                 '  tell application "System Events" to set frontmost of '
                 'process (item 1 of argv) to true\n'
                 'end run', app)
            done["frontmost_app"] = True
    except Exception:
        pass
    return done


def _write(path: str | None, payload: dict) -> None:
    if not path:
        return
    try:
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=1)
    except OSError:
        pass


def cmd_check(args) -> int:
    try:
        w, t, u, st = _pick(args.match, args.exact_url)
    except LookupError as exc:
        print(json.dumps({"ok": False, "reason": "no-owa-tab", "detail": str(exc)}))
        return 3
    if st.get("js_unavailable"):
        print(json.dumps({
            "ok": False, "reason": "js-from-apple-events-off",
            "detail": "Chrome > View > Developer > Allow JavaScript from Apple "
                      "Events is OFF, so this tab cannot be read from the host. "
                      "The RAISE still works; verify visibility through your own "
                      "browser handle.",
            "window": w, "tab": t, "url": u[:80]}))
        return 4
    out = {"ok": st.get("vis") == "visible", "visibilityState": st.get("vis"),
           "hasFocus": st.get("focus"), "rows": st.get("rows"),
           "window": w, "tab": t, "url": u[:80]}
    print(json.dumps(out))
    return 0 if out["ok"] else 2


def cmd_hold(args) -> int:
    started = time.time()
    prior_app = None
    prior_win_id = _front_window_id()
    prior_tab = (None, None)
    try:
        prior_app = _frontmost_app()
    except Exception:
        pass

    try:
        w, t, u, _ = _pick(args.match, args.exact_url)
    except LookupError as exc:
        _write(args.status_file, {"status": "no-owa-tab", "detail": str(exc)})
        print(json.dumps({"status": "no-owa-tab", "detail": str(exc)}))
        return 3

    try:
        wid = int(_osa('on run argv\n'
                       '  tell application "Google Chrome" to id of window '
                       '((item 1 of argv) as integer)\n'
                       'end run', str(w)))
        ti = int(_osa('on run argv\n'
                      '  tell application "Google Chrome" to active tab index of window '
                      '((item 1 of argv) as integer)\n'
                      'end run', str(w)))
        prior_tab = (wid, ti)
    except Exception:
        pass

    # --- acquire ------------------------------------------------------------
    acquired_at, verified = None, True
    while time.time() - started < args.acquire_timeout:
        try:
            _assert_visible(w, t)
            time.sleep(0.6)          # the flip is not instantaneous
            w, t, u, st = _pick(args.match, args.exact_url)
            if st.get("vis") == "visible":
                acquired_at = time.time()
                break
            # The page cannot be READ from the host, but it CAN be raised, and a
            # caller that named its tab exactly has its own way to verify. Hold
            # it and say plainly that this leg is unverified — refusing here
            # would report an un-raisable window, which is not what happened.
            if st.get("js_unavailable") and args.exact_url:
                acquired_at, verified = time.time(), False
                break
        except Exception:
            time.sleep(0.5)
    if acquired_at is None:
        _restore(prior_app, prior_win_id, prior_tab)
        payload = {"status": "could-not-acquire", "held_seconds": 0,
                   "acquire_timeout": args.acquire_timeout,
                   "window": w, "tab": t, "url": u[:80]}
        _write(args.status_file, payload)
        print(json.dumps(payload))
        return 2

    # --- hold ---------------------------------------------------------------
    samples, visible_samples, reasserts = 0, 0, 0
    deadline = acquired_at + args.seconds
    stopped = "deadline"
    while time.time() < deadline:
        if args.stop_file:
            try:
                open(args.stop_file).close()
                stopped = "stop-file"
                break
            except OSError:
                pass
        # The caller touches the heartbeat as it works the page. Silence for
        # --max-idle means the pass is thinking, not reading, and the owner's
        # screen is being held for nothing. Baseline is the acquire time, so a
        # caller that names a heartbeat file and never touches it still gets its
        # screen back after one idle window rather than at the budget.
        if args.heartbeat_file:
            try:
                last = os.path.getmtime(args.heartbeat_file)
            except OSError:
                last = acquired_at
            if time.time() - max(last, acquired_at) > args.max_idle:
                stopped = "idle"
                break
        try:
            w, t, u, st = _pick(args.match, args.exact_url)
            samples += 1
            if st.get("vis") == "visible":
                visible_samples += 1
            else:
                _assert_visible(w, t)
                reasserts += 1
        except Exception:
            pass
        _write(args.status_file, {
            "status": "holding", "acquired": True, "verified": verified,
            "held_seconds": round(time.time() - acquired_at, 1),
            "samples": samples, "visible_samples": visible_samples,
            "reasserts": reasserts, "window": w, "tab": t, "url": u[:80]})
        time.sleep(args.interval)

    restored = _restore(prior_app, prior_win_id, prior_tab)
    held = round(time.time() - acquired_at, 1)
    payload = {"status": "released", "stopped_by": stopped, "acquired": True,
               "verified": verified,
               # budget beside held_seconds so "it released early" is a fact
               # anyone can recount from the artifact, never a claim
               "budget_seconds": args.seconds,
               "released_early": stopped != "deadline",
               "held_seconds": held,
               "samples": samples, "visible_samples": visible_samples,
               "visible_fraction": round(visible_samples / samples, 3) if samples else None,
               "reasserts": reasserts, "window": w, "tab": t, "url": u[:80],
               "restored": restored, "prior_frontmost_app": prior_app}
    _write(args.status_file, payload)
    print(json.dumps(payload))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="is the OWA tab visible right now?")
    c.add_argument("--match", default=HOST)
    c.add_argument("--exact-url", default=None,
                   help="target the tab whose URL is EXACTLY this "
                        "(a caller driving its own tab passes its location.href)")
    c.set_defaults(fn=cmd_check)

    h = sub.add_parser("hold", help="raise and hold the OWA tab visible")
    h.add_argument("--match", default=HOST)
    h.add_argument("--exact-url", default=None,
                   help="target the tab whose URL is EXACTLY this "
                        "(a caller driving its own tab passes its location.href)")
    h.add_argument("--seconds", type=float, default=180.0)
    h.add_argument("--interval", type=float, default=3.0)
    h.add_argument("--acquire-timeout", type=float, default=25.0)
    h.add_argument("--stop-file", default=None,
                   help="release early once this file exists")
    h.add_argument("--heartbeat-file", default=None,
                   help="the caller touches this while it is working the page; "
                        "no touch for --max-idle seconds releases the screen")
    h.add_argument("--max-idle", type=float, default=90.0,
                   help="only applies with --heartbeat-file")
    h.add_argument("--status-file", default=None)
    h.set_defaults(fn=cmd_hold)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except OsaUnavailable as exc:
        # A PERMISSIONS problem, reported as one. Before this, any osascript
        # failure other than the JS-off case escaped `check` as a raw traceback
        # and read as a crash in the browser lane (s13, 2026-08-01).
        print(json.dumps({
            "ok": False, "reason": "apple-events-denied",
            "detail": "this process cannot reach AppleScript. Grant it "
                      "Automation / Apple Events for \"Google Chrome\" and "
                      "\"System Events\" (System Settings > Privacy & Security "
                      "> Automation), or run it outside the sandbox that is "
                      "denying the XPC lookup.",
            "error": str(exc)[:300]}))
        return 5
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "reason": "osascript-failed",
                          "error": str(exc)[:300]}))
        return 6


if __name__ == "__main__":
    # tab-selection logic is covered by tests/test_cos_hold_visible.py
    sys.exit(main())
