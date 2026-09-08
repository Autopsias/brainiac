#!/usr/bin/env python3
"""Arm the ego lite mail tab for a COS run — brands, hook, reload, proof.

WHY THIS EXISTS (measured 2026-08-18). Two page states must hold before any
COS pass can trust this tab, and both die with the CLI session that set them:

  1. OWA feature-gates on `navigator.userAgentData.brands`. ego lite ships
     no `Google Chrome` brand, and the degraded page writes `aria-setsize=0`
     on every list row — the scanner's declared count reads zero forever.
     `Emulation.setUserAgentOverride` with a `Google Chrome` brand fixes it.
  2. The capture hook must install at `document_start` to catch the BOOT
     `FindItem` envelope every mutation replays. That takes
     `Page.addScriptToEvaluateOnNewDocument` — also CDP-session-scoped.

Both registrations are scoped to the CDP session that made them, so override,
hook registration, reload AND the wait for proof all run inside ONE
`ego-browser nodejs` invocation. The resulting PAGE state survives the
session exiting; a second invocation can read it but could never have armed
it. This is the ego-lane analogue of `cos_cdp_capture.py --prepare`, and it
keeps that tool's exit contract so the nightly's error paths carry over:

  0  armed — brands accepted (aria-setsize > 0), hook seeded (boot FindItem)
  4  not signed in — the tab left the mailbox for a login page
  2  anything else (no tab, no ego, arming did not converge)

In ``--door-check`` mode the projection is 0=open, 4=skipped-not-signed-in,
6=closed (including unknown or insufficient remaining authorization validity).

Normal arming never opens a tab.  ``--door-check`` is the one deliberate
exception: an unattended task space can be cold even while the inherited user
profile is signed in, so it opens/reuses the Outlook URL and executes
``gotoAndWait`` before it is allowed to conclude "not signed in". Every polled
source is an IIFE — on this transport a BARE top-level source holding a
`function` expression as a call argument evaluates to `null` (a wrong value
that parses; see `EgoTab`'s docstring).
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_driver as drv                                        # noqa: E402

HOOK_JS = Path(__file__).resolve().parent / "cos_capture_hook.js"

#: One place for the brand list, so a future Chromium bump edits one line.
BRANDS = [{"brand": "Chromium", "version": "150"},
          {"brand": "Google Chrome", "version": "150"},
          {"brand": "Not;A=Brand", "version": "99"}]

#: The polled page probe. An IIFE (see module docstring), returning only
#: counts, booleans and state words — never subjects, senders or ids.
PROBE = (
    "(function(){try{"
    "var u=String(location.href);"
    "var mail=u.indexOf('outlook.cloud.microsoft/mail')!==-1;"
    "var live=(typeof window.__cosCap==='object')?window.__cosCap:null;"
    "var cap=live?live.stats():null;"
    "var remaining=null;"
    "var remaining_from=null;var finditem_remaining=null;"
    # EVERY ACTION, NOT JUST FindItem (2026-08-27). OWA attaches the SAME
    # reused access token to whatever request it makes next, so the newest
    # envelope for `FindItem` is not the newest token the page holds -- it is
    # only the newest one that happened to ride a mailbox listing. Measured
    # 2026-08-26: this gate read 2240s while the ring held 5157s, and the
    # night was refused over 49 minutes of validity it already had. Scan the
    # whole ring and take the max `exp`; keep the FindItem number beside it so
    # the gap is visible rather than inferred. Still only the integer delta
    # crosses the page boundary -- never the header, never the token.
    "if(live){try{var calls=live.calls||[];var best=null;"
    "for(var i=0;i<calls.length;i++){var c=calls[i];"
    "var auth=String(c&&c.headers&&c.headers.authorization||'');"
    "if(!auth)continue;var parts=auth.replace(/^Bearer\\s+/i,'').split('.');"
    "if(parts.length!==3)continue;"
    "var b=parts[1].replace(/-/g,'+').replace(/_/g,'/');"
    "while(b.length%4)b+='=';var claims;"
    "try{claims=JSON.parse(atob(b));}catch(e){continue;}"
    "if(typeof claims.exp!=='number')continue;"
    "if(c.action==='FindItem'&&(finditem_remaining===null"
    "||claims.exp>finditem_remaining))finditem_remaining=claims.exp;"
    "if(best===null||claims.exp>best){best=claims.exp;"
    "remaining_from=String(c.action||'');}}"
    "var now=Date.now()/1000;"
    "if(best!==null)remaining=Math.floor(best-now);"
    "if(finditem_remaining!==null)"
    "finditem_remaining=Math.floor(finditem_remaining-now);}"
    "catch(e){remaining=null;}}"
    # WHY THE LIST IS EMPTY, NOT JUST THAT IT IS (2026-08-28). An occluded
    # browser window gets no animation frames, so OWA boots -- StartupData
    # fires, a token is captured -- and its virtualized list never
    # materializes. That is indistinguishable from every other arming failure
    # unless the page says whether it considers itself visible and how many
    # rows exist. `vis` separates a starved renderer from a broken one;
    # `rows` separates "no list at all" from "a list whose aria is wrong".
    "var rows=document.querySelectorAll('[role=\"option\"][data-convid]');"
    "var row=rows[0]||null;"
    "return JSON.stringify({ready:document.readyState,mail:mail,"
    "vis:String(document.visibilityState),rows:rows.length,"
    "cap:cap?{boot:!!cap.boot_finditem,rs:String(cap.installed_at_readystate),"
    "n:cap.captured|0}:null,remaining_validity_seconds:remaining,"
    "remaining_from_action:remaining_from,"
    "finditem_remaining_seconds:finditem_remaining,"
    "setsize:row?parseInt(row.getAttribute('aria-setsize'),10):null,"
    "brands:navigator.userAgentData?navigator.userAgentData.brands"
    ".map(function(b){return b.brand}).join('|'):''});"
    "}catch(e){return JSON.stringify({err:String(e)})}})()")


def _poll_loop_js(wait_s: int) -> str:
    """The reload-and-poll loop, extracted so `build_script` stays under the
    function-length bound. Returns the JS block verbatim, spliced back into
    `build_script`'s f-string unchanged.

    SIGNED-OUT MUST PERSIST across two polls. After the page reloads, OWA's
    readyState reaches 'complete' on the intermediate shell BEFORE the mail
    app mounts, so a SINGLE `ready:complete && !mail` poll is a reload race,
    not proof of sign-out. A 2026-08-30 live backlog run false-stopped
    `skipped-not-signed-in` on exactly that transient, and a plain re-fire
    then cleared it. Require two CONSECUTIVE such polls; any other reading
    (mail present, armed, or an unreadable probe) breaks the streak. This
    costs a genuine sign-out one extra 2s poll and nothing else.
    """
    return f"""  let last = {{status: 'degraded', why: 'never-polled'}};
  const deadline = {wait_s};
  let noMailStreak = 0;
  for (let i = 0; i < deadline; i += 2) {{
    await new Promise(r => setTimeout(r, 2000));
    let raw;
    try {{ raw = await js({json.dumps(PROBE)}); }}
    catch (e) {{ noMailStreak = 0; last = {{status: 'degraded', why: String(e)}}; continue; }}
    let s;
    try {{ s = JSON.parse(String(raw)); }}
    catch (e) {{ noMailStreak = 0; last = {{status: 'degraded', why: 'probe-null'}}; continue; }}
    if (s.err) {{ noMailStreak = 0; last = {{status: 'degraded', why: s.err}}; continue; }}
    if (s.ready === 'complete' && !s.mail) {{
      noMailStreak += 1;
      last = {{status: 'not-signed-in', probe: s, streak: noMailStreak}};
      if (noMailStreak >= 2) break;
      continue;
    }}
    noMailStreak = 0;
    last = {{status: 'degraded', probe: s}};
    if (s.cap && s.cap.boot && s.setsize !== null && s.setsize > 0) {{
      last = {{status: 'armed', probe: s}}; break;
    }}
  }}"""


def build_script(space: str, match: str, wait_s: int,
                 repair_url: str | None = None) -> str:
    """The ONE invocation: take space, override brands, register hook,
    reload, poll for proof, fence the verdict. Everything session-scoped
    happens before the reload; everything after only reads."""
    hook_src = HOOK_JS.read_text(encoding="utf-8")
    fence_open, fence_close = drv.EgoTab._OPEN, drv.EgoTab._CLOSE
    repair = ""
    if repair_url:
        # Official ego semantics: openOrReuseTab establishes a current tab;
        # gotoAndWait is then the explicit navigation/settling repair this
        # unattended door check requires before it may report signed-out.
        repair = f"""
let opened = null;
if (!t) {{
  opened = await openOrReuseTab({json.dumps(repair_url)}, {{wait: true}});
  if (opened && opened.targetId) await switchTab(opened.targetId);
  t = opened;
}}
if (t) {{
  await switchTab(t.targetId);
  await gotoAndWait({json.dumps(repair_url)});
  tabs = await listTabs();
  t = tabs.find(x => String(x.url || '').includes({json.dumps(match)})) ||
      (opened && opened.targetId
       ? tabs.find(x => x.targetId === opened.targetId) : null) || t;
}}
"""
    return f"""
let sp;
try {{ sp = await takeOverTaskSpace({json.dumps(space)}); }}
catch (e) {{ sp = await useOrCreateTaskSpace({json.dumps(space)}); }}
const fence = (obj) => cliLog({json.dumps(fence_open)} +
  Buffer.from(JSON.stringify(obj), 'utf8').toString('base64') +
  {json.dumps(fence_close)});
let tabs = await listTabs();
let t = tabs.find(x => String(x.url || '').includes({json.dumps(match)}));
{repair}
if (!t) {{ fence({{status: 'no-tab'}}); }}
else {{
  await switchTab(t.targetId);
  // A WEDGED RENDERER CANNOT BE ARMED, ONLY RENAVIGATED. Run 185 (2026-08-24)
  // drove 32 attachment downloads through this tab and left `Runtime.evaluate`
  // timing out on every expression -- `navigator.userAgent` included, which is
  // the FIRST thing below. Arming could not repair that and did not claim to:
  // it returned `degraded`, and the night lost a frozen plan of 43/11/4.
  //
  // `gotoAndWait` does not need the dead document, so it is the repair, and it
  // was MEASURED as one at 13:26 that day: the same tab that had refused every
  // evaluate answered `navigator.userAgent` immediately after. It runs ONLY on
  // the failure path -- a healthy tab is never renavigated, because a
  // navigation costs an OWA reload and would be pure latency on every night.
  let ua;
  try {{ ua = await js('navigator.userAgent'); }}
  catch (e) {{
    cliLog('arm: evaluate is dead, renavigating to repair: ' + String(e).slice(0, 120));
    await gotoAndWait(String(t.url));
    ua = await js('navigator.userAgent');
  }}
  await cdp('Emulation.setUserAgentOverride', {{
    userAgent: String(ua),
    userAgentMetadata: {{
      brands: {json.dumps(BRANDS)},
      fullVersionList: {json.dumps(BRANDS)},
      platform: 'macOS', platformVersion: '15.0.0',
      architecture: 'arm', model: '', mobile: false
    }}
  }});
  await cdp('Page.enable', {{}});
  await cdp('Page.addScriptToEvaluateOnNewDocument',
            {{source: {json.dumps(hook_src)}}});
  await cdp('Page.reload', {{}});
{_poll_loop_js(wait_s)}
  fence(last);
}}
"""


def arm(space: str = "cos", match: str = "outlook", wait_s: int = 90,
        repair_url: str | None = None) -> dict[str, Any]:
    """Run the invocation and decode the fenced verdict from EITHER stream —
    `cliLog` writes to stderr and the CLI interleaves its own lines on both."""
    script = build_script(space, match, wait_s, repair_url)
    try:
        p = subprocess.run(["ego-browser", "nodejs"], input=script,
                           capture_output=True, text=True,
                           timeout=wait_s + 60)
    except FileNotFoundError:
        return {"status": "degraded", "why": "`ego-browser` is not on PATH"}
    except subprocess.TimeoutExpired:
        return {"status": "degraded",
                "why": f"ego-browser did not return within {wait_s + 60}s"}
    out = f"{p.stdout}\n{p.stderr}"
    if drv.EgoTab._OPEN not in out or drv.EgoTab._CLOSE not in out:
        tail = (p.stderr.strip() or p.stdout.strip() or f"exit {p.returncode}")
        return {"status": "degraded", "why": f"no fenced verdict: {tail[:300]}"}
    blob = out.split(drv.EgoTab._OPEN, 1)[1].split(drv.EgoTab._CLOSE, 1)[0]
    return json.loads(base64.b64decode(blob).decode("utf-8"))


EXIT = {"armed": 0, "not-signed-in": 4}

#: MEASURED on the live OWA page, 2026-08-26 (`_evidence/night-porter/
#: night-1-report.md`): four access tokens observed with `exp - iat` lifetimes
#: of 5037, 5514, 4595 and 4254 seconds. OWA mints one and REUSES it until it
#: nears expiry, so a snapshot of `remaining` is usually mid-life. Recorded
#: here as the band the `closed` reason quotes — never as a threshold, and
#: never as an input to the verdict.
OBSERVED_TOKEN_LIFETIME_BAND = (4254, 5514)


def _starved_hint(probe: dict[str, Any], browser: dict[str, Any]) -> str:
    """Name the occluded-window cause when the page's own state shows it.

    A page that booted (`mail` true) with ZERO list rows is not a broken page
    — it is a page whose renderer got no animation frames. Say so, and say
    which launch flag is missing, because the repair is a Chrome relaunch and
    nothing else. Measured 2026-08-28: the first unattended night closed here
    and the record named no cause at all.
    """
    if not probe.get("mail") or probe.get("rows"):
        return ""
    missing = browser.get("missing_flags") or []
    where = (f" The browser is running WITHOUT {' and '.join(missing)} —"
             " relaunch it with those flags." if missing else
             " The flags are present, so look at what is covering the window.")
    return (". The page booted but rendered ZERO list rows"
            f" (visibility={probe.get('vis')!r}): an occluded browser window"
            f" gets no animation frames, so OWA never loads the list.{where}")


def _closed_reason(status: Any, measured: bool, remaining: Any,
                   required: int) -> str:
    """WHY the door is closed, in words that name the right repair.

    (JUDGE-03 second finding, 2026-08-27) The verdict word was the whole
    report, so a closed door read as "sign in again" — and twice on
    2026-08-26 it was nothing of the kind: the session was signed in, armed
    and capturing, and the ONLY short thing was the phase of a token OWA had
    already been reusing for most of its life. Diagnosing that cost a hand
    probe of the page's capture ring. The verdict and the exit code are
    unchanged; this line is what the operator reads first.
    """
    if status != "armed":
        return (f"arming did not converge (arm_status={status!r}) — this is a "
                "page/tab problem, not an authorization one")
    if not measured:
        return ("armed and capturing, but NO captured request of any action "
                "carried a parseable expiry — there is no validity to "
                "measure, so the door cannot be called open")
    lo, hi = OBSERVED_TOKEN_LIFETIME_BAND
    return (
        f"TOKEN PHASE, NOT SIGN-OUT: the session is signed in, armed and "
        f"capturing; its reused access token simply has {int(remaining)}s left "
        f"against the {required}s this gate requires. OWA mints tokens with "
        f"{lo}-{hi}s lifetimes (measured 2026-08-26) and reuses each until it "
        "nears expiry, so this gate can only pass early in a token's life. The "
        "repair is to wait for the roll and RE-ARM, never a fresh sign-in.")


#: The two Chromium launch flags that stop an occluded window's renderer
#: being throttled. Measured 2026-08-08 ON GOOGLE CHROME: without them a
#: covered window renders 12 of ~290 OWA rows and a real 2000px scroll adds
#: none; with them the same tab renders 122 conversations while the browser
#: sits behind other apps. They apply ONLY at launch, so a browser that was
#: already running ignores them silently.
#:
#: NOT YET MEASURED ON `ego lite`, which is the COS browser since the ruling
#: of 2026-08-18. It is a Chromium (it forks `--type=renderer`, `gpu-process`
#: and `network.mojom` helpers), so the mechanism is the same one; that the
#: same two flags REPAIR it is inference until a run proves it. Reported here
#: as a precondition to check, never as a fix already applied.
OCCLUSION_FLAGS = ("--disable-backgrounding-occluded-windows",
                   "--disable-renderer-backgrounding")

#: The browsers that can host the OWA tab, most likely first. `ego lite` is
#: the shipped lane; Google Chrome was the lane before 2026-08-18 and is what
#: a hand probe may still be pointed at. Order matters: whichever is running
#: is the one reported.
BROWSER_PROCESSES = ("ego lite", "Google Chrome")


def browser_launch_flags() -> dict[str, Any]:
    """Which anti-backgrounding flags the browser hosting OWA was launched
    with — the precondition an unattended night depends on, never a verdict
    input.

    The night runs at 02:00 behind a screen saver, which covers the window
    completely. A browser launched without these flags then boots OWA and
    never renders its message list, and the door closes as a bare 'degraded'
    naming no cause. That is what happened on 2026-08-28, and reconstructing
    it needed a macOS power log and a process listing.
    """
    for name in BROWSER_PROCESSES:
        try:
            found = subprocess.run(["pgrep", "-x", name],
                                   capture_output=True, text=True, timeout=10)
            pids = (found.stdout or "").split()
            if not pids:
                continue
            cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", pids[0]],
                                 capture_output=True, text=True, timeout=10)
            line = (cmd.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return {"running": None, "why": f"could not read {name}"}
        return {"running": name,
                "missing_flags": [f for f in OCCLUSION_FLAGS if f not in line]}
    return {"running": False, "why": "no known browser process is running"}


def door_check(verdict: dict[str, Any], required_seconds: int) -> dict[str, Any]:
    """Project an arm result into the closed unattended door vocabulary.

    Only the JWT expiry delta crosses the page boundary. The authorization
    header and token remain in ``window.__cosCap`` and are never returned.
    """
    status = verdict.get("status")
    probe = verdict.get("probe") if isinstance(verdict.get("probe"), dict) else {}
    remaining = probe.get("remaining_validity_seconds")
    measured = (type(remaining) in (int, float)
                and math.isfinite(float(remaining)))
    if status == "not-signed-in":
        word = "skipped-not-signed-in"
    elif (status == "armed" and measured
          and remaining >= required_seconds):
        word = "open"
    else:
        word = "closed"
    # Only a CLOSED door is a puzzle. An open one needs no explaining, and a
    # signed-out one has a known answer -- reporting launch flags beside it
    # would invite a browser relaunch when the repair is a sign-in.
    browser = browser_launch_flags() if word == "closed" else {}
    return {
        "status": ("ready" if word == "open" else
                   "skipped: not signed in" if word == "skipped-not-signed-in"
                   else "closed"),
        "door_check": {
            "verdict": word,
            "lane": "rest",
            "toolset": "ego-browser",
            "remaining_validity_seconds": remaining,
            "required_validity_seconds": required_seconds,
            # Diagnostic, never an input to the verdict: which action carried
            # the winning token, and what the old FindItem-only rule would
            # have read. A large gap between these two is the bug this gate
            # had until 2026-08-27, and it is now visible in every run.
            "remaining_from_action": probe.get("remaining_from_action"),
            "finditem_remaining_seconds": probe.get(
                "finditem_remaining_seconds"),
            "arm_status": status,
            # EVERY FIELD THE PROBE ALREADY SAW. Until 2026-08-28 a closed
            # door recorded only that arming was 'degraded': the probe held
            # `ready`, `mail`, `vis`, `rows`, `setsize`, `brands` and `cap` at
            # that exact moment and the projection dropped all seven, so the
            # first failed unattended night left nothing to diagnose. None of
            # them carry mail content or a token -- they are render state.
            # Carried on the CLOSED path only; an open door is not a puzzle.
            **({} if word == "open" else {
                "arm_probe": {k: probe.get(k) for k in
                              ("ready", "mail", "vis", "rows", "setsize",
                               "brands", "cap")},
                **({"browser": browser} if browser else {}),
            }),
            "reason": (
                "lane, arm state and remaining validity all hold"
                if word == "open" else
                "the tab left the mailbox for a login page"
                if word == "skipped-not-signed-in" else
                _closed_reason(status, measured, remaining, required_seconds)
                + _starved_hint(probe, browser)),
        },
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--space", default="cos")
    p.add_argument("--match", default="outlook")
    p.add_argument("--wait", type=int, default=90,
                   help="seconds to wait for the reloaded page to prove both "
                        "arms (default 90)")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--door-check", action="store_true",
                   help="repair a cold cos space with gotoAndWait, then require "
                        "enough remaining authorization validity for a batch")
    p.add_argument("--required-seconds", type=int, default=None,
                   help="minimum remaining validity for --door-check (default "
                        "$BRAIN_COS_BATCH_REQUIRED_SECONDS or 900)")
    args = p.parse_args(argv)
    required = (args.required_seconds if args.required_seconds is not None
                else int(os.environ.get("BRAIN_COS_BATCH_REQUIRED_SECONDS", "900")))
    if required < 1:
        p.error("--required-seconds must be positive")
    verdict = dict(arm(args.space, args.match, args.wait,
                       "https://outlook.cloud.microsoft/mail/"
                       if args.door_check else None),
                   armed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    if args.door_check:
        verdict = door_check(verdict, required)
    if args.out:
        args.out.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print(json.dumps(verdict))
    if args.door_check:
        return {"open": 0, "skipped-not-signed-in": 4, "closed": 6}.get(
            (verdict.get("door_check") or {}).get("verdict"), 6)
    return EXIT.get(verdict.get("status", ""), 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
