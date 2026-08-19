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

THIS TOOL NEVER NAVIGATES. It attaches to a tab already holding the mailbox
and reloads THAT tab; opening one would race the owner's sign-in. Every
polled source is an IIFE — on this transport a BARE top-level source holding
a `function` expression as a call argument evaluates to `null` (a wrong
value that parses; see `EgoTab`'s docstring).
"""
from __future__ import annotations

import argparse
import base64
import json
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
    "var cap=(typeof window.__cosCap==='object')?window.__cosCap.stats():null;"
    "var row=document.querySelector('[role=\"option\"][data-convid]');"
    "return JSON.stringify({ready:document.readyState,mail:mail,"
    "cap:cap?{boot:!!cap.boot_finditem,rs:String(cap.installed_at_readystate),"
    "n:cap.captured|0}:null,"
    "setsize:row?parseInt(row.getAttribute('aria-setsize'),10):null,"
    "brands:navigator.userAgentData?navigator.userAgentData.brands"
    ".map(function(b){return b.brand}).join('|'):''});"
    "}catch(e){return JSON.stringify({err:String(e)})}})()")


def build_script(space: str, match: str, wait_s: int) -> str:
    """The ONE invocation: take space, override brands, register hook,
    reload, poll for proof, fence the verdict. Everything session-scoped
    happens before the reload; everything after only reads."""
    hook_src = HOOK_JS.read_text(encoding="utf-8")
    fence_open, fence_close = drv.EgoTab._OPEN, drv.EgoTab._CLOSE
    return f"""
let sp;
try {{ sp = await takeOverTaskSpace({json.dumps(space)}); }}
catch (e) {{ sp = await useOrCreateTaskSpace({json.dumps(space)}); }}
const fence = (obj) => cliLog({json.dumps(fence_open)} +
  Buffer.from(JSON.stringify(obj), 'utf8').toString('base64') +
  {json.dumps(fence_close)});
const tabs = await listTabs();
const t = tabs.find(x => String(x.url || '').includes({json.dumps(match)}));
if (!t) {{ fence({{status: 'no-tab'}}); }}
else {{
  await switchTab(t.targetId);
  const ua = await js('navigator.userAgent');
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
  let last = {{status: 'degraded', why: 'never-polled'}};
  const deadline = {wait_s};
  for (let i = 0; i < deadline; i += 2) {{
    await new Promise(r => setTimeout(r, 2000));
    let raw;
    try {{ raw = await js({json.dumps(PROBE)}); }}
    catch (e) {{ last = {{status: 'degraded', why: String(e)}}; continue; }}
    let s;
    try {{ s = JSON.parse(String(raw)); }}
    catch (e) {{ last = {{status: 'degraded', why: 'probe-null'}}; continue; }}
    if (s.err) {{ last = {{status: 'degraded', why: s.err}}; continue; }}
    if (s.ready === 'complete' && !s.mail) {{
      last = {{status: 'not-signed-in', probe: s}}; break;
    }}
    last = {{status: 'degraded', probe: s}};
    if (s.cap && s.cap.boot && s.setsize !== null && s.setsize > 0) {{
      last = {{status: 'armed', probe: s}}; break;
    }}
  }}
  fence(last);
}}
"""


def arm(space: str = "cos", match: str = "outlook",
        wait_s: int = 90) -> dict[str, Any]:
    """Run the invocation and decode the fenced verdict from EITHER stream —
    `cliLog` writes to stderr and the CLI interleaves its own lines on both."""
    script = build_script(space, match, wait_s)
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


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--space", default="cos")
    p.add_argument("--match", default="outlook")
    p.add_argument("--wait", type=int, default=90,
                   help="seconds to wait for the reloaded page to prove both "
                        "arms (default 90)")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    verdict = dict(arm(args.space, args.match, args.wait),
                   armed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    if args.out:
        args.out.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print(json.dumps(verdict))
    return EXIT.get(verdict.get("status", ""), 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
