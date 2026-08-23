#!/usr/bin/env python3
"""Capture the app's own requests at the BROWSER level, not in the page.

WHY THIS EXISTS. `tools/cos_capture_hook.js` wraps `fetch`/`XHR` in the page's
MAIN world, and for the READ lane that is enough — the page really does issue
`FindItem`/`GetItem` itself. It is NOT enough for the MUTATION lane: measured
2026-08-11, six owner archives produced ZERO capturable `MoveItem` while every
`service.svc` POST the page made was classified and none was a write.
Independently corroborated by `outlook-tool` issue #3 (2026-04): modern Outlook
Web dispatches those calls from the SERVICE WORKER, a separate JS realm where a
page-level override simply does not apply.

CDP's `Network` domain sees every request whatever realm issued it, which is the
same fix that tool landed (it used Playwright's `context.on('request')`).

WHAT IT DOES NOT CHANGE. "Replay, never synthesize" (doctrine v4.7) is untouched
— this captures the shape of a request the SERVER ALREADY ACCEPTED from a real
owner action. Only the capture POINT moves.

    python3 tools/cos_cdp_capture.py --watch 120        # what fires, by action
    python3 tools/cos_cdp_capture.py --shapes --out s.json

REQUIRES a Chrome started with `--remote-debugging-port` on a NON-DEFAULT
`--user-data-dir` (Chrome 151 refuses the port on the default profile), signed in
to the mailbox.
"""
from __future__ import annotations

import urllib.parse

import argparse
import asyncio
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
from typing import Any

try:
    import websockets
except ImportError:                                          # pragma: no cover
    sys.exit("this needs `websockets` (pip install websockets)")

#: Header names that never leave this process. The bearer is the whole reason
#: the page-world design kept the envelope in the page; at the browser level the
#: same rule is enforced here, at the only boundary that exists.
SECRET = re.compile(r"^(authorization|cookie|x-owa-canary|.*token.*)$", re.I)

#: An EWS request names itself in `__type`: "MoveItemJsonRequest:#Exchange".
TYPE = re.compile(r"^([A-Za-z]+?)(?:Json)?(?:Request)?(?::|$)")


def action_of(url: str, headers: dict[str, str], body: str | None) -> str | None:
    m = re.search(r"[?&]action=([A-Za-z]+)", url or "")
    if m:
        return m.group(1)
    for k, v in (headers or {}).items():
        if k.lower() == "x-owa-action":
            return str(v)
    for candidate in (body, _postdata(headers)):
        if not candidate:
            continue
        try:
            doc = json.loads(candidate)
        except ValueError:
            continue
        # A JSON body is not always an object: telemetry posts arrays, and
        # `doc.get` on a list raised AttributeError INSIDE the recorder loop —
        # killing the capture mid-run on 2026-08-11. A classifier that crashes
        # on an unexpected shape looks exactly like a page that sent nothing.
        if not isinstance(doc, dict):
            continue
        t = doc.get("__type") or (doc.get("Body") or {}).get("__type")
        if t:
            hit = TYPE.match(str(t))
            if hit:
                return hit.group(1)
    return None


def _postdata(headers: dict[str, str]) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == "x-owa-urlpostdata":
            return urllib.parse.unquote(str(v))
    return None


def scrub(headers: dict[str, str]) -> dict[str, str]:
    """Header NAMES survive; a secret's VALUE never does."""
    return {k: ("<withheld>" if SECRET.match(k) else v)
            for k, v in (headers or {}).items()}


class CDP:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.seq = 0
        #: Events that arrived while waiting for a reply. They are EVENTS, not
        #: noise — a request can fire between the attach and the first recv.
        self.pending: list[dict] = []

    async def call(self, method: str, params: dict | None = None,
                   sid: str | None = None, timeout: float = 10.0) -> dict:
        """Every call is BOUNDED. An unbounded one hangs before the first line
        of output, which is indistinguishable from a tool that captured nothing
        (measured 2026-08-11: three silent recorders, zero rows, no traceback)."""
        self.seq += 1
        msg: dict[str, Any] = {"id": self.seq, "method": method,
                               "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        want = self.seq
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                got = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=2.0))
            except asyncio.TimeoutError:
                continue
            if got.get("id") == want:
                if "error" in got:
                    raise RuntimeError(f"{method}: {got['error']}")
                return got.get("result", {})
            self.pending.append(got)
        raise TimeoutError(f"{method} did not answer within {timeout:.0f}s")


async def watch(endpoint_url: str, seconds: float, match: str,
                stream: str | None = None) -> list[dict]:
    """Record every matching request, from ANY realm in that browser.

    Auto-attach only. Walking the target list and attaching by hand is what
    hung — and it also misses the service worker, which is spawned on demand
    and is exactly the realm the page-level hook could not see.
    """
    async with websockets.connect(endpoint_url, max_size=40_000_000) as ws:
        cdp = CDP(ws)
        await cdp.call("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": False,
            "flatten": True})
        print("auto-attach armed", file=sys.stderr, flush=True)

        seen: list[dict] = []
        realms: dict[str, str] = {}
        deadline = time.monotonic() + seconds

        async def handle(evt: dict) -> None:
            method = evt.get("method")
            if method == "Target.attachedToTarget":
                sid = evt["params"]["sessionId"]
                info = evt["params"]["targetInfo"]
                realms[sid] = info["type"]
                try:
                    await cdp.call("Network.enable", {}, sid, timeout=5.0)
                    # NESTED AUTO-ATTACH. Browser-level auto-attach reaches
                    # pages and service workers; a DEDICATED worker is a child
                    # of its page and is only reachable by arming auto-attach on
                    # the page's own session. This build runs two of them with
                    # empty urls, and they were the last realm never watched.
                    if info["type"] in ("page", "service_worker", "worker"):
                        await cdp.call("Target.setAutoAttach", {
                            "autoAttach": True, "waitForDebuggerOnStart": False,
                            "flatten": True}, sid, timeout=5.0)
                    print(f"attached: {info['type']} {info.get('url','')[:45]}",
                          file=sys.stderr, flush=True)
                except (RuntimeError, TimeoutError) as exc:
                    print(f"attach failed ({info['type']}): {exc}",
                          file=sys.stderr, flush=True)
                return
            if method != "Network.requestWillBeSent":
                return
            req = evt["params"]["request"]
            if match not in req["url"]:
                return
            body = req.get("postData")
            row = {
                "realm": realms.get(evt.get("sessionId"), "?"),
                "method": req.get("method"),
                "action": action_of(req["url"], req.get("headers") or {}, body),
                "url_path": req["url"].split("?")[0],
                "headers": scrub(req.get("headers") or {}),
                "body": body,
                "has_post_data": bool(req.get("hasPostData")),
            }
            seen.append(row)
            if stream:
                with open(stream, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            tag = "WRITE" if row["action"] in ("MoveItem", "UpdateItem",
                                               "CreateItem") else "read"
            print(f"{tag}: {row['action']} <- {row['realm']} "
                  f"({len(body or '')} body bytes)", file=sys.stderr, flush=True)

        for evt in cdp.pending:
            await handle(evt)
        cdp.pending.clear()

        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await handle(json.loads(raw))
            except Exception as exc:                       # noqa: BLE001
                # NEVER let one malformed event end the recording. The rows
                # already streamed to disk are evidence; a dead recorder is not.
                print(f"skipped one event: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
        return seen


def endpoint(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                timeout=5) as fh:
        return json.load(fh)["webSocketDebuggerUrl"]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=9222)
    p.add_argument("--watch", type=float, default=60.0,
                   help="seconds to record")
    p.add_argument("--match", default="service.svc")
    p.add_argument("--out", default=None)
    p.add_argument("--prepare", action="store_true",
                   help="launch/attach the automation browser, install the hook "
                        "at document_start, and report whether it got its seed")
    args = p.parse_args(argv[1:])

    if args.prepare:
        out = prepare(args.port)
        print(json.dumps(out, indent=2))
        return 0 if out["ready"] else 4

    rows = asyncio.run(watch(endpoint(args.port), args.watch, args.match,
                             (args.out + ".jsonl") if args.out else None))
    by_action: dict[str, int] = {}
    by_target: dict[str, int] = {}
    for r in rows:
        by_action[r["action"] or "(unclassified)"] = \
            by_action.get(r["action"] or "(unclassified)", 0) + 1
        by_target[r["realm"] or "?"] = by_target.get(r["realm"] or "?", 0) + 1
    summary = {"captured": len(rows), "by_action": by_action,
               "by_realm": by_target}
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "rows": rows}, fh, indent=2)
        print(f"wrote {args.out}", file=sys.stderr)
    return 0




async def _evaluate(endpoint_url: str, url_substr: str, expression: str) -> Any:
    """Evaluate in the page's MAIN world over CDP.

    CDP evaluates in the main world by DEFAULT, which is the whole reason this
    lane exists: `osascript` lands in an isolated world and the Chrome extension
    is the only other main-world surface, and neither can reach the browser
    started on the automation profile.
    """
    async with websockets.connect(endpoint_url, max_size=80_000_000) as ws:
        cdp = CDP(ws)
        targets = (await cdp.call("Target.getTargets"))["targetInfos"]
        page = next((t for t in targets
                     if t["type"] == "page" and url_substr in t.get("url", "")), None)
        if page is None:
            raise SystemExit(f"no page whose url contains {url_substr!r}")
        sid = (await cdp.call("Target.attachToTarget",
                              {"targetId": page["targetId"], "flatten": True})
               )["sessionId"]
        res = await cdp.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True}, sid, timeout=120.0)
        if res.get("exceptionDetails"):
            raise RuntimeError(res["exceptionDetails"].get("text", "eval failed"))
        return res.get("result", {}).get("value")


def evaluate(expression: str, *, port: int = 9222,
             url_substr: str = "outlook.cloud.microsoft/mail") -> Any:
    return asyncio.run(_evaluate(endpoint(port), url_substr, expression))


# ---------------------------------------------------------------------------
# the session: one command, so a nightly needs no human with a script
# ---------------------------------------------------------------------------
PROFILE = pathlib.Path.home() / "Library/Application Support/Google/Chrome-COS"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAIL = "https://outlook.cloud.microsoft/mail/inbox"


def launch(port: int = 9222) -> str:
    """Start the automation browser if it is not already answering.

    It MUST be the NON-DEFAULT profile: Chrome 151 refuses
    `--remote-debugging-port` on the default one and ignores `--load-extension`
    there too, so this copied profile is the only lane that can be driven.
    """
    try:
        return endpoint(port)
    except Exception:                                            # noqa: BLE001
        pass
    if not PROFILE.is_dir():
        raise SystemExit(
            f"no automation profile at {PROFILE}. Copy the signed-in Chrome "
            "profile there once; a fresh profile needs an interactive sign-in "
            "that a nightly cannot perform.")
    for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (PROFILE / stale).unlink(missing_ok=True)
    subprocess.Popen(
        [CHROME, f"--user-data-dir={PROFILE}", f"--remote-debugging-port={port}",
         "--disable-backgrounding-occluded-windows",
         "--disable-renderer-backgrounding", "--no-first-run", MAIL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(1.0)
        try:
            return endpoint(port)
        except Exception:                                        # noqa: BLE001
            continue
    raise SystemExit("the automation browser never opened its debug port")


async def _prepare(endpoint_url: str, timeout_s: float) -> dict:
    hook = (pathlib.Path(__file__).parent / "cos_capture_hook.js").read_text(
        encoding="utf-8")
    async with websockets.connect(endpoint_url, max_size=40_000_000) as ws:
        cdp = CDP(ws)
        targets = (await cdp.call("Target.getTargets"))["targetInfos"]
        page = next((t for t in targets if t["type"] == "page"
                     and "outlook" in t.get("url", "")), None)
        if page is None:
            page = await cdp.call("Target.createTarget",
                                  {"url": "about:blank", "newWindow": True})
        sid = (await cdp.call("Target.attachToTarget",
                              {"targetId": page["targetId"], "flatten": True})
               )["sessionId"]
        await cdp.call("Page.enable", {}, sid)
        # DOCUMENT_START, then navigate: the read lane replays the app's own boot
        # `FindItem`, and a hook installed after that call has nothing to replay.
        await cdp.call("Page.addScriptToEvaluateOnNewDocument",
                       {"source": hook}, sid)
        await cdp.call("Page.navigate", {"url": MAIL}, sid)
        # ACTIVE TAB, not merely open: this build renders (and re-queries) only
        # for the active tab of its window.
        await cdp.call("Page.bringToFront", {}, sid)
        deadline = time.monotonic() + timeout_s
        stats: dict = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            try:
                res = await cdp.call("Runtime.evaluate", {
                    "expression": "JSON.stringify(window.__cosCap ? "
                                  "window.__cosCap.stats() : {})",
                    "returnByValue": True}, sid, timeout=10.0)
                stats = json.loads(res["result"]["value"] or "{}")
            except Exception:                                    # noqa: BLE001
                continue
            if stats.get("boot_finditem"):
                return {"ready": True,
                        "document_start": stats.get("document_start"),
                        "captured": stats.get("captured"),
                        "why": "seed envelope captured"}
        return {"ready": False, "document_start": stats.get("document_start"),
                "captured": stats.get("captured", 0),
                "why": ("the hook installed but no FindItem was captured — the "
                        "mailbox likely needs an interactive sign-in")
                       if stats else
                       "no page state at all; the browser may be on a sign-in page"}


def prepare(port: int = 9222, timeout_s: float = 90.0) -> dict:
    """Launch if needed, arm the hook at document_start, wait for the seed."""
    return asyncio.run(_prepare(launch(port), timeout_s))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
