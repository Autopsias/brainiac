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
  * `set index of window N to 1` IS A SILENT NO-OP on Chrome 151.0.7922.108
    (measured 2026-08-10, five probes: background, foreground, both
    directions, and after `activate`; `set minimized` is a no-op too, and
    System Events reports `count of windows` = 0 for Chrome unless the calling
    process holds Accessibility, so `AXRaise` is not reachable either). THERE
    IS NO WORKING WINDOW-RAISE PRIMITIVE. What remains is `set active tab
    index` (works) plus `System Events -> set frontmost` (works) — and both of
    those act on THE WINDOW CHROME IS ALREADY SHOWING. So this tool can make
    visible exactly one thing: a tab in Chrome's front window. That is the
    whole of run 109's failure (2026-08-10): the run's fresh run-owned tab was
    not in that window, the acquire loop re-issued the dead ordering call for
    its full 30 s budget, and wrote `could-not-acquire` with no reason, no
    attempt count and no observation attached — so the cause could not be
    recovered from the artifact the next day. 0 bodies opened, 111 rows
    `browser-not-visible`, empty corpus, dead ingestion funnel.

WHAT THAT MEANS FOR A CALLER, and it is the whole contract now: OPEN YOUR TAB
IN THE WINDOW CHROME IS SHOWING, name it with `--tab-id`, and read the refusal.
A refusal now NAMES its cause (`page-stayed-hidden` carries the observation
that decides it — which window is front, whether your tab is its window's
active tab, whether the ordering call moved anything) instead of being one
anonymous verdict for a denied Apple Events channel, a Chrome that refuses host
JS, a tab that closed, and a window nothing can raise.

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


#: The CLOSED set of reasons a `hold` may refuse with, each with the operator
#: action it implies. A limit ships with its outcome word: run 109 refused with
#: no word at all, and a day later nobody could say which of four causes fired.
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


def _locate(match: str) -> list[tuple[int, int, str, str]]:
    """(window, tab, tab id, url) for every Chrome tab whose URL contains `match`.

    Re-located on every call on purpose: raising a window renumbers AppleScript
    window indices, so a cached index silently addresses the wrong window.

    The TAB ID is Chrome's own `uniqueID` — stable for the life of the tab, and
    unlike the URL it does not change when the page navigates. See `_pick`.
    """
    out = _osa(
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
        'end run', match)
    hits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        w, t, i, u = line.split("\t", 3)
        hits.append((int(w), int(t), i, u))
    return hits


def tab_id(win: int, tab: int) -> str:
    """Chrome's stable id for one tab, for a caller that will navigate it."""
    return _osa('on run argv\n'
                '  tell application "Google Chrome" to id of tab '
                '((item 2 of argv) as integer) of window ((item 1 of argv) as integer)\n'
                'end run', str(win), str(tab))


def eval_js(win: int, tab: int, js: str, timeout: float = 30) -> str:
    """Run `js` in a Chrome tab and return its raw string result.

    The one channel every host-side page read goes through, so callers that
    need their own probe (tools/cos_lane_rehearsal.py) inherit this module's
    JsUnavailable / OsaUnavailable diagnosis instead of re-deriving it.
    """
    return _osa(
        'on run argv\n'
        '  set w to (item 1 of argv) as integer\n'
        '  set t to (item 2 of argv) as integer\n'
        '  set js to item 3 of argv\n'
        '  tell application "Google Chrome" to execute tab t of window w javascript js\n'
        'end run', str(win), str(tab), js, timeout=timeout)


def _read(win: int, tab: int) -> dict:
    return json.loads(eval_js(win, tab, _PROBE))


def chrome_profiles() -> list[str]:
    """The `--user-data-dir` of every top-level Chrome process running now.

    WHY THIS IS A DIAGNOSIS AND NOT TRIVIA (measured 2026-08-10). Starting the
    `chrome-devtools` MCP launches a SECOND Chrome, under its own profile
    (`~/.cache/chrome-devtools-mcp/chrome-profile`) and with no OWA session.
    From that moment `tell application "Google Chrome"` addresses THAT instance:
    every AppleScript read answered from its single `about:blank` page —
    `windows: 1`, no Outlook tab — while the owner's signed-in Chrome sat
    untouched beside it. Fronting the owner's Chrome by pid did not move the
    routing back; only ending the rival process did.

    So the tools then refuse `no-owa-tab`, which reads as "the owner closed
    Outlook" and sends the reader to the wrong place entirely. One extra process
    listing turns that into the one sentence that resolves it.
    """
    try:
        out = subprocess.run(["ps", "-axo", "command"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:                                              # noqa: BLE001
        return []                    # a diagnosis aid may never become a failure
    return parse_chrome_profiles(out)


def parse_chrome_profiles(ps_output: str) -> list[str]:
    """The parsing half of `chrome_profiles`, split out so it is testable
    against a real `ps` capture without running one."""
    profiles = []
    for line in ps_output.splitlines():
        # Top-level browser processes only. Every renderer/GPU/utility helper
        # carries `--type=`, and the crashpad handler is a different binary —
        # counting those would report a dozen "instances" on a normal machine.
        if "MacOS/Google Chrome" not in line or " --type=" in line:
            continue
        flag = "--user-data-dir="
        profiles.append(line.split(flag, 1)[1].split(" ")[0]
                        if flag in line else "<default>")
    return profiles


def _rival_chrome_hint() -> str:
    """The trailing sentence for a "no such tab" refusal, or "" when there is
    only one Chrome and the refusal means what it says."""
    profiles = chrome_profiles()
    if len(profiles) < 2:
        return ""
    return (f". NOTE: {len(profiles)} Chrome instances are running "
            f"({', '.join(profiles)}) — AppleScript addresses only ONE of them, "
            "so this refusal may be about the WRONG browser. Quit the extra "
            "instance (the chrome-devtools MCP starts one under "
            "`chrome-devtools-mcp/chrome-profile`) and re-run")


def _pick(match: str, exact_url: str | None = None,
          want_id: str | None = None) -> tuple[int, int, str, dict]:
    """The OWA tab actually showing a message list.

    Several OWA tabs can be open (the owner's, and the tab a run opened for
    itself), and a substring match cannot tell them apart — `/mail/inbox` is a
    prefix of the owner's `/mail/inbox/id/AAQk…`. A caller driving its own tab
    names it, one of two ways:

    * ``--tab-id`` — Chrome's own stable tab id. **The only one that survives a
      NAVIGATION**, and therefore the one a v5.55 deep-link body pass must use:
      that pass changes `location.href` on every open, so an `--exact-url` hold
      stops matching after the first one. The hold loop swallows the resulting
      LookupError, which means it silently stops re-asserting visibility while
      still reporting `status: holding` — a page that goes hidden mid-pass
      renders zero rows (measured 2026-08-01), so this failure is invisible and
      total.
    * ``--exact-url`` — that tab's `location.href`. Exact and unambiguous for a
      caller whose tab does NOT navigate.

    With neither: prefer a mail URL, then the one with the most rendered rows,
    because a tab rendering rows is the one whose rendering we care about.
    Whichever is picked is REPORTED, so a wrong pick is visible rather than
    silent.
    """
    hits = _locate(match)
    if want_id:
        hits = [h for h in hits if h[2] == want_id]
        if not hits:
            raise LookupError(f"no Chrome tab whose id is {want_id!r}"
                              + _rival_chrome_hint())
    if exact_url:
        hits = [h for h in hits if h[3] == exact_url]
        if not hits:
            raise LookupError(f"no Chrome tab whose URL is exactly {exact_url!r}"
                              + _rival_chrome_hint())
    if not hits:
        raise LookupError(f"no Chrome tab whose URL contains {match!r}"
                          + _rival_chrome_hint())
    scored = []
    for w, t, _i, u in hits:
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


def _window_order() -> list[str]:
    """Chrome's window ids, in AppleScript window order (index 1 first).

    Read BEFORE and AFTER the raise so `window_order_changed` is a measured
    fact in the artifact rather than an assumption. On Chrome 151 it is always
    `false` — see the module docstring — and a caller that can see that is a
    caller who knows to move its tab instead of retrying a dead call.
    """
    out = _osa('tell application "Google Chrome"\n'
               '  set r to ""\n'
               '  repeat with w from 1 to (count of windows)\n'
               '    set r to r & (id of window w) & linefeed\n'
               '  end repeat\n'
               '  return r\n'
               'end tell')
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _observe(win: int, tab: int) -> dict:
    """The facts that tell one `hidden` page apart from another.

    Best-effort by construction: this runs on the failure path, where half the
    probes may themselves be what failed. Every one is individually guarded so
    a second failure cannot swallow the first one's evidence.
    """
    obs: dict = {}
    for key, fn in (
        ("frontmost_app", _frontmost_app),
        ("window_order", _window_order),
        ("target_window_id", lambda: _osa(
            'on run argv\n  tell application "Google Chrome" to id of window '
            '((item 1 of argv) as integer)\nend run', str(win))),
        ("target_window_active_tab", lambda: _osa(
            'on run argv\n  tell application "Google Chrome" to active tab index '
            'of window ((item 1 of argv) as integer)\nend run', str(win))),
    ):
        try:
            obs[key] = fn()
        except Exception as exc:                              # noqa: BLE001
            obs[key] = None
            obs[f"{key}_error"] = str(exc)[:160]
    order, wid = obs.get("window_order"), obs.get("target_window_id")
    obs["target_is_front_window"] = bool(order) and bool(wid) and order[0] == wid
    try:
        obs["target_is_active_tab"] = int(obs["target_window_active_tab"]) == tab
    except (TypeError, ValueError):
        obs["target_is_active_tab"] = None
    return obs


def _assert_visible(win: int, tab: int, want_id: str | None = None) -> None:
    """Make `tab` the active tab of `win`, put `win` in front, front Chrome.

    All three steps, every time: the tab can be active in a window that is
    covered, and the window can be at index 1 inside an application that is
    behind a full-screen app. Only the combination renders.

    With `want_id` the first two steps address the tab by its STABLE id
    (`assert_visible_by_id`, index-form) instead of by the (window, tab) pair,
    so a caller that named its tab cannot be defeated by a renumbering between
    the resolve and the assert. The System Events step is unconditional either
    way — it is the only one of the three that has been measured to work.
    """
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


_BEAT = 0


def _write(path: str | None, payload: dict) -> None:
    """Write the status file, stamping this process's own beat on every write.

    The status file is the hold's ONLY liveness signal — the `--heartbeat-file`
    runs the other way (the caller touches it). Run 109 wrote exactly one
    status payload, at the end of a 30 s acquire, so nothing on disk could tell
    "still trying" from "died on the first call". Every write now carries a
    monotonic `beat` and an `updated` timestamp, including during acquire.
    """
    global _BEAT
    _BEAT += 1
    payload = {**payload, "beat": _BEAT,
               "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not path:
        return
    try:
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=1)
    except OSError:
        pass


def cmd_check(args) -> int:
    try:
        w, t, u, st = _pick(args.match, args.exact_url, args.tab_id)
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
        w, t, u, _ = _pick(args.match, args.exact_url, args.tab_id)
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
    # EVERY iteration re-resolves BEFORE it asserts. Before 2026-08-10 the
    # refresh sat AFTER the assert inside the same `try`, so one raise failure
    # froze (w, t) for the rest of the budget and a transient error became
    # terminal. And every exception went into one bare `except`, which is why
    # `main`'s exit-5 handler for a denied Apple Events channel was unreachable
    # from in here and run 109's refusal named nothing at all.
    acquired_at, verified = None, True
    attempts, last_error, last_state = 0, None, None
    reason, order_changed = "page-stayed-hidden", None
    while time.time() - started < args.acquire_timeout:
        attempts += 1
        try:
            w, t, u, st = _pick(args.match, args.exact_url, args.tab_id)
            last_state = st
        except OsaUnavailable:
            raise                    # a permission problem, reported as one
        except LookupError as exc:
            reason, last_error = "tab-disappeared", str(exc)[:200]
            _write(args.status_file, {
                "status": "acquiring", "attempts": attempts,
                "elapsed": round(time.time() - started, 1),
                "reason_so_far": reason, "last_error": last_error})
            time.sleep(0.5)
            continue
        # The page cannot be READ from the host, but it CAN be raised, and a
        # caller that NAMED ITS TAB — by id or by url — has its own way to
        # verify. Hold it and say plainly that this leg is unverified; refusing
        # would report an un-raisable window, which is not what happened.
        # Until 2026-08-10 this tested `--exact-url` only, so the `--tab-id`
        # selector doctrine v5.55 MANDATES for a navigating pass could never
        # take the escape hatch and burned the whole budget instead.
        if st.get("js_unavailable") and (args.exact_url or args.tab_id):
            acquired_at, verified = time.time(), False
            break
        if st.get("js_unavailable"):
            reason, last_error = "js-from-apple-events-off", "no tab selector given"
        # Already visible ⇒ already acquired. Asserting first and only THEN
        # looking cost run 109's successor a whole 30 s budget on a tab that
        # read `visible` throughout, because a raise that raised an exception
        # skipped the check that would have said so.
        if st.get("vis") == "visible":
            acquired_at = time.time()
            break
        try:
            before = _window_order()
            _assert_visible(w, t, args.tab_id)
            order_changed = _window_order()[:1] != before[:1]
        except OsaUnavailable:
            raise
        except Exception as exc:                              # noqa: BLE001
            reason, last_error = "osascript-failed", str(exc)[:200]
            _write(args.status_file, {
                "status": "acquiring", "attempts": attempts,
                "elapsed": round(time.time() - started, 1),
                "reason_so_far": reason, "last_error": last_error})
            time.sleep(0.5)
            continue
        time.sleep(0.6)              # the flip is not instantaneous
        try:
            w, t, u, st = _pick(args.match, args.exact_url, args.tab_id)
            last_state = st
        except OsaUnavailable:
            raise
        except LookupError as exc:
            reason, last_error = "tab-disappeared", str(exc)[:200]
            time.sleep(0.5)
            continue
        except Exception as exc:                              # noqa: BLE001
            reason, last_error = "osascript-failed", str(exc)[:200]
            time.sleep(0.5)
            continue
        if st.get("vis") == "visible":
            acquired_at = time.time()
            break
        reason = "page-stayed-hidden"
        _write(args.status_file, {
            "status": "acquiring", "attempts": attempts,
            "elapsed": round(time.time() - started, 1),
            "reason_so_far": reason, "visibilityState": st.get("vis"),
            "window_order_changed": order_changed,
            "window": w, "tab": t, "url": u[:80]})
    if acquired_at is None:
        _restore(prior_app, prior_win_id, prior_tab)
        payload = {"status": "could-not-acquire", "reason": reason,
                   "held_seconds": 0, "attempts": attempts,
                   "acquire_timeout": args.acquire_timeout,
                   "last_error": last_error,
                   "visibilityState": (last_state or {}).get("vis"),
                   "rows": (last_state or {}).get("rows"),
                   "window_order_changed": order_changed,
                   "observed": _observe(w, t),
                   "detail": _REFUSAL_DETAIL.get(reason, ""),
                   "window": w, "tab": t, "url": u[:80]}
        _write(args.status_file, payload)
        print(json.dumps(payload))
        return 2

    # --- hold ---------------------------------------------------------------
    samples, visible_samples, reasserts = 0, 0, 0
    assert_failures, consecutive_failures, last_error = 0, 0, None
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
        # A LOST TAB MUST NOT KEEP REPORTING `holding`. This block used to be a
        # bare `except Exception: pass` beneath an unconditional
        # `status: "holding"` write, so a hold whose tab had navigated out from
        # under an `--exact-url` binding went on asserting NOTHING while every
        # reader — including v5.60's `hold_status` ledger stamp — saw a healthy
        # hold. On a page whose hidden state renders zero rows that failure is
        # invisible and total, which is the whole reason `--tab-id` exists.
        try:
            w, t, u, st = _pick(args.match, args.exact_url, args.tab_id)
            samples += 1
            consecutive_failures, last_error = 0, None
            if st.get("vis") == "visible":
                visible_samples += 1
            else:
                _assert_visible(w, t, args.tab_id)
                reasserts += 1
        except OsaUnavailable as exc:
            # The channel died mid-hold. Nothing this loop does can recover it,
            # and every further sample would be a lie.
            stopped, last_error = "apple-events-denied", str(exc)[:200]
            assert_failures += 1
            break
        except Exception as exc:                                  # noqa: BLE001
            assert_failures += 1
            consecutive_failures += 1
            last_error = str(exc)[:200]
            if consecutive_failures >= args.max_assert_failures:
                stopped = "lost-tab"
                _write(args.status_file, {
                    "status": "holding-degraded", "acquired": True,
                    "verified": verified,
                    "held_seconds": round(time.time() - acquired_at, 1),
                    "samples": samples, "visible_samples": visible_samples,
                    "reasserts": reasserts, "assert_failures": assert_failures,
                    "consecutive_failures": consecutive_failures,
                    "last_error": last_error,
                    "window": w, "tab": t, "url": u[:80]})
                break
        _write(args.status_file, {
            "status": "holding" if consecutive_failures == 0 else "holding-degraded",
            "acquired": True, "verified": verified,
            "held_seconds": round(time.time() - acquired_at, 1),
            "samples": samples, "visible_samples": visible_samples,
            "reasserts": reasserts, "assert_failures": assert_failures,
            "consecutive_failures": consecutive_failures,
            "last_error": last_error,
            "window": w, "tab": t, "url": u[:80]})
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
               "reasserts": reasserts, "assert_failures": assert_failures,
               "last_error": last_error,
               "window": w, "tab": t, "url": u[:80],
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
    c.add_argument("--tab-id", default=None,
                   help="target the tab with this stable Chrome tab id — the "
                        "ONLY selector that survives the tab NAVIGATING, so a "
                        "deep-link body pass must use it, never --exact-url")
    c.set_defaults(fn=cmd_check)

    h = sub.add_parser("hold", help="raise and hold the OWA tab visible")
    h.add_argument("--match", default=HOST)
    h.add_argument("--exact-url", default=None,
                   help="target the tab whose URL is EXACTLY this "
                        "(a caller driving its own tab passes its location.href)")
    h.add_argument("--tab-id", default=None,
                   help="target the tab with this stable Chrome tab id — the "
                        "ONLY selector that survives the tab NAVIGATING, so a "
                        "deep-link body pass must use it, never --exact-url")
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
    h.add_argument("--max-assert-failures", type=int, default=3,
                   help="consecutive failed re-asserts that end the hold with "
                        "stopped_by: \"lost-tab\" instead of going on "
                        "reporting `holding` while asserting nothing")
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


# --- stable-id addressing -------------------------------------------------
# Chrome's AppleScript `window N` is Z-ORDER: the front window is window 1, so
# every focus change renumbers them and a (window, tab) pair resolved moments
# ago can address a different tab — or none ("Invalid index"). It broke the
# lane rehearsal three times on 2026-08-09, twice mid-run. A tab's `id` is
# stable for the life of the tab and survives navigation, so these two address
# by it. ADDITIVE ON PURPOSE: `eval_js`/`_assert_visible` above are unchanged,
# because the live run's visibility holder depends on them.
# INDEX-based, not `repeat with w in windows` / `repeat with t in tabs of w`.
# The reference form binds `t` to the LAZY chain `item j of every tab of item i
# of every window`, which AppleScript re-resolves at each dereference against
# the CURRENT window/tab sets — so with two windows of unequal tab counts it
# asks window 1 for a tab index only window 2 has and dies with
# "Can't get item 8 of every tab of item 1 of every window. Invalid index."
# Measured 2026-08-10 on a 7-tab + 10-tab Chrome: the rehearsal could not read
# its own list even though the tab id was correct and the tab was open.
# `assert_visible_by_id` below has always used the index form and has never hit
# this; use the same shape here.
_BY_ID = ('  repeat with wi from 1 to (count of windows)\n'
          '    set w to window wi\n'
          '    repeat with ti from 1 to (count of tabs of w)\n'
          '      set t to tab ti of w\n'
          '      if ((id of t) as text) is tid then\n')


def eval_js_by_id(tab_id: int, js: str, timeout: float = 30) -> str:
    """Run `js` in the tab with this stable id, whatever window it now sits in."""
    return _osa(
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
    """Make that tab active in its window, front the window, front Chrome."""
    _osa(
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


if __name__ == "__main__":
    # tab-selection logic is covered by tests/test_cos_hold_visible.py
    sys.exit(main())
