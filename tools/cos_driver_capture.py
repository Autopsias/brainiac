"""The page-capture protocol of `cos_driver` — the DOM bridge, staging, the two capture passes

Moved verbatim out of `cos_driver_transport` (quality drain, batch 3): the tab
WIRE (`ChromeTab`/`CdpTab`/`EgoTab`, `open_tab`, `DriverStop`) stays there,
this module owns everything that drives `tools/cos_driver_page.js` once a tab
is open — the two hidden-DOM-node bridge, chunked staging, and the two
capture passes (`capture_night` scans + enumerates with an empty draw,
`capture_bodies` fetches the drawn bodies). Every name is re-imported by
`cos_driver_transport` so its module path is unchanged for every existing
caller (`cos_driver.py`, `cos_driver_enumeration.py`, `cos_driver_selfcheck.py`
all import these names `from cos_driver_transport import (...)` today) —
which makes `cos_driver_transport` re-import THIS module, so the three names
this module needs FROM it (`DriverStop`, `_ts`, `BODY_BUDGET_CHARS`) are read
back through a LOCAL import inside each function that needs them, never a
module-level one: a module-level `from cos_driver_transport import …` here
deadlocks the cycle the moment something imports `cos_driver_capture` before
`cos_driver_transport` has (measured — a bare `import cos_driver_capture`
raised `ImportError: cannot import name 'BOOTSTRAP' from partially
initialized module`). `ChromeTab` is a type hint only and needs no import at
all under `from __future__ import annotations`.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # cos_driver_transport re-exports from THIS module, so a
    # module-level import would cycle. Annotations are strings under
    # `from __future__ import annotations`, so this costs nothing at runtime.
    from cos_driver_transport import ChromeTab

sys.path.insert(0, str(Path(__file__).resolve().parent))

PAGE_JS = Path(__file__).resolve().parent / "cos_driver_page.js"

# ---------------------------------------------------------------------------
# capture: drive the tab
# ---------------------------------------------------------------------------
MAIL_ROOT = "https://outlook.cloud.microsoft/mail/"


def assert_ready(tab: ChromeTab) -> dict[str, Any]:
    """The run-owned tab must ALREADY be seeded. The driver never navigates.

    It used to navigate to the Inbox when the tab was on the wrong view, and
    that is now a foot-gun rather than a convenience: a navigation destroys the
    main-world capture hook and the captured envelope with it, so the driver
    would tidy the tab into a state where it can no longer authenticate and then
    discover that one HTTP 401 later.

    Both halves of readiness are checked from the HOST's world, which can see
    neither `window.__cosCap` nor `window.__cosRun` — only the shared DOM. So
    the page half announces itself through `#__cos_out`, and its `seed_kind` is
    the seed proof.
    """
    import cos_driver_transport as _t                             # noqa: PLC0415
    state = tab.json(
        "JSON.stringify({p:location.pathname,"
        "rows:document.querySelectorAll('[role=\"option\"][data-convid]').length,"
        "out:!!document.getElementById('__cos_out')})")
    if not str(state.get("p", "")).rstrip("/").endswith("/mail"):
        raise _t.DriverStop(
            f"the run-owned tab is on {state.get('p')!r}, not {MAIL_ROOT}. The "
            "driver refuses to navigate there itself: a navigation wipes the "
            "main-world capture hook and the captured envelope with it.")
    if not state.get("out"):
        raise _t.DriverStop(
            "the run-owned tab carries no `#__cos_out` node, so "
            "`tools/cos_driver_page.js` was never injected into its MAIN world. "
            "The host's own AppleScript world is ISOLATED — injecting from here "
            "produces a driver that cannot see the captured envelope and is "
            "refused 401. Seed the tab first, then start the driver.")
    if not state.get("rows"):
        raise _t.DriverStop(
            "the run-owned tab renders no message rows, so the DOM leg of the "
            "completeness cross-check would compare the REST census against an "
            "empty set and pass. A background tab renders no list on this build "
            "— make the run-owned tab the ACTIVE tab of its window and retry.")
    return state


#: The DOM bridge. `#__cos_in` carries options into the page's main world,
#: `#__cos_out` mirrors the run state back. Two inert `<script
#: type="application/json">` nodes — the only thing the host's isolated world
#: and the page's main world share.
IN_ID = "__cos_in"
OUT_ID = "__cos_out"
SRC_ID = "__cos_src"

#: The one line that has to be run in the page's MAIN world, by whatever surface
#: can reach it (a browser extension; not this process — see `assert_ready`).
#: It carries no logic: `--stage` puts `tools/cos_driver_page.js` verbatim into a
#: DOM node from the host's own repo, and this evaluates THAT. The alternative is
#: pasting 17 KB of driver through the extension on every run, where the source
#: of truth stops being the file in git.
def bootstrap_for(node_id: str) -> str:
    return (f"(function(){{var e=document.getElementById('{node_id}');"
            f"return e?eval(JSON.parse(e.textContent)):'no-source';}})()")


BOOTSTRAP = bootstrap_for(SRC_ID)


def stage(tab: ChromeTab, source: Path | None = None,
          node_id: str | None = None) -> str:
    """Put the page-side driver source where the main world can reach it.

    Written in CHUNKS and length-verified. A single 20 KB write through
    `osascript`'s `execute javascript` silently stored nothing (measured
    2026-08-10: 0 of 20,534 characters, no error raised anywhere) — which is the
    same class of failure as the truncating read, and gets the same treatment.
    """
    import cos_driver_transport as _t                             # noqa: PLC0415
    node = node_id or SRC_ID
    src = json.dumps((source or PAGE_JS).read_text(encoding="utf-8"))
    tab.js(_fresh_node(node))
    for off in range(0, len(src), CHUNK):
        tab.js(f"(function(){{document.getElementById({json.dumps(node)})"
               f".textContent+={json.dumps(src[off:off + CHUNK])};"
               f"return 'chunk';}})()")
    got = tab.js(f"String((document.getElementById({json.dumps(node)})"
                 f"||{{textContent:''}}).textContent.length)")
    if int(got) != len(src):
        raise _t.DriverStop(f"staged {got} of {len(src)} source characters")
    return bootstrap_for(node)

#: AppleScript returns one string, and a 20-body payload is ~100 KB. Read it in
#: slices and reassemble on length, so a transport that truncates FAILS instead
#: of handing back a shorter night that parses.
CHUNK = 16000


def _fresh_node(node_id: str) -> str:
    """Replace `#<node_id>` with an empty hidden div, whatever it was before.

    Not `if (!el) create`: an earlier attempt may have left a `<script>` node at
    that id, and Trusted Types then refuses every `textContent` write to it —
    silently, from the host's side. Recreating the node is one line and removes
    the whole class.
    """
    return (f"(function(){{var old=document.getElementById({json.dumps(node_id)});"
            f"if(old)old.remove();"
            f"var e=document.createElement('div');e.hidden=true;"
            f"e.id={json.dumps(node_id)};document.documentElement.appendChild(e);"
            f"return 'fresh';}})()")


def _start(tab: ChromeTab, seq: int, opts: dict[str, Any]) -> None:
    payload = json.dumps({"seq": seq, "opts": opts}, ensure_ascii=False)
    tab.js(_fresh_node(IN_ID))
    for off in range(0, len(payload), CHUNK):
        tab.js(f"(function(){{document.getElementById({json.dumps(IN_ID)})"
               f".textContent+={json.dumps(payload[off:off + CHUNK])};"
               f"return 'chunk';}})()")


def _read_out(tab: ChromeTab, out_id: str = OUT_ID) -> dict[str, Any]:
    """Read the bridge node back, as BASE64.

    Not as text. Slicing the JSON at a fixed code-unit width splits surrogate
    pairs, and `osascript` drops the lone halves: measured on run 114, a 204,750
    character night came back 204,748 characters long — two dropped halves of one
    emoji in one subject line, and the only reason it was visible at all is that
    the length is checked. Base64 is pure ASCII, so no boundary can be unsafe.
    """
    import cos_driver_transport as _t                             # noqa: PLC0415
    total = int(tab.js(
        f"(function(){{var e=document.getElementById({json.dumps(out_id)});"
        "if(!e)return '-1';"
        "var b=new TextEncoder().encode(e.textContent);var s='';"
        "for(var i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);"
        "window.__cosB64=btoa(s);return String(window.__cosB64.length);})()"))
    if total < 0:
        raise _t.DriverStop(f"the `#{out_id}` bridge node vanished mid-run")
    parts = [tab.js(f"window.__cosB64.substr({off},{CHUNK})")
             for off in range(0, total, CHUNK)]
    b64 = "".join(parts)
    if len(b64) != total:
        raise _t.DriverStop(f"the bridge read back {len(b64)} of {total} base64 "
                            "characters — the transport truncated the night")
    try:
        return json.loads(base64.b64decode(b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _t.DriverStop(f"the bridge payload is not JSON ({exc})") from None


#: How many times pass 1 may run before a short scan is allowed to reach
#: `assert_complete` and stop the night. Three, not "until it works": a scanner
#: that never finishes is a defect to surface, not to spin on.
_SCAN_ATTEMPTS = 3


def _scan_finished(scan: dict[str, Any]) -> bool:
    """Did the scanner reach the end of the virtualized list?

    `complete` is the scanner's own verdict (end of list AND every declared id
    collected). The id/declared comparison is repeated here rather than trusted
    blindly, so a build whose in-page half forgets to emit `complete` degrades
    to counting instead of silently reading as finished."""
    ids = scan.get("ids") or []
    declared = scan.get("declared")
    if not scan.get("complete"):
        return False
    return not isinstance(declared, int) or len(ids) >= declared


def capture_night(tab: ChromeTab, *, cap: int, poll_seconds: float,
                  max_wait: float, now: _dt.datetime) -> dict[str, Any]:
    """Run the in-page driver and return its raw output. No accounting here."""
    import cos_driver_transport as _t                             # noqa: PLC0415
    assert_ready(tab)
    window_start = _t._ts(now - _dt.timedelta(hours=24))

    # Pass 1: scan + enumerate + sent, with an EMPTY draw. The draw cannot be
    # computed until the enumeration exists, and the enumeration is what says
    # which rows are already read.
    opts = {"cap": 0, "budget": _t.BODY_BUDGET_CHARS, "sent_window_start": window_start}
    first = _await_run(tab, 1, opts, poll_seconds, max_wait)

    # RE-SCAN WHILE THE SCANNER SAYS IT DID NOT FINISH (2026-08-22). The mail
    # list is VIRTUALIZED — ~60 of a few hundred rows exist in the page at once
    # — so the scanner has to scroll to see the rest, and one scroll pass can
    # come back short. It knows when it did: `scan.complete` is false unless it
    # reached the end AND collected `declared` ids.
    #
    # Nothing read that field, and a short scan does not fail quietly — it flows
    # into `completeness()`, where the ids it never reached appear as a set
    # difference against the REST census and stop the night as
    # "N conversation id(s) appear in one enumeration and not the other and none
    # is attributable to a recorded arrival". That accuses the MAILBOX of
    # changing under the read when the instrument simply had not finished
    # looking. Measured twice on the same evening, minutes apart, on a mailbox
    # that reconciled at 505 = 505 both times: run155 scanned 258 of 263 (5
    # "unexplained"), run156 scanned 259 of 263 (4). Both aborted before a
    # single body was fetched, and both cost a full category-stamping pass.
    #
    # A retry is the honest fix because the scan is READ-ONLY and idempotent:
    # pass 1 scans, enumerates, and reads the sent window. Bounded, because a
    # scanner that cannot finish in three passes is a real defect that must
    # reach `assert_complete` and stop the night, not be looped over.
    for _ in range(_SCAN_ATTEMPTS - 1):
        scan = first["out"]["scan"] or {}
        if _scan_finished(scan):
            break
        again = _await_run(tab, 1, opts, poll_seconds, max_wait)
        # Keep whichever pass saw MORE of the list: a later short pass must
        # never discard an earlier complete one.
        if len((again["out"]["scan"] or {}).get("ids") or []) >= len(scan.get("ids") or []):
            first = again

    enumeration = first["out"]["enumeration"] or {}
    scan = first["out"]["scan"] or {}
    sent = first["out"]["sent"] or {}
    return {"scan": scan, "enumeration": enumeration, "sent": sent,
            "bodies": [], "cap": cap, "window_start": window_start}


def capture_bodies(tab: ChromeTab, draw: list[dict[str, str]], *,
                   poll_seconds: float, max_wait: float,
                   window_start: str) -> list[dict[str, Any]]:
    """Pass 2: fetch the drawn bodies. Every element of `draw` is already read."""
    if not draw:
        return []
    import cos_driver_transport as _t                             # noqa: PLC0415
    opts = {"cap": len(draw), "budget": _t.BODY_BUDGET_CHARS,
            "sent_window_start": window_start, "draw": draw, "max_scrolls": 0}
    res = _await_run(tab, 2, opts, poll_seconds, max_wait)
    return res["out"]["bodies"] or []


def _await_run(tab: ChromeTab, seq: int, opts: dict[str, Any],
               poll_seconds: float, max_wait: float) -> dict[str, Any]:
    import cos_driver_transport as _t                             # noqa: PLC0415
    _start(tab, seq, opts)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(poll_seconds)
        st = tab.json(
            f"(function(){{var e=document.getElementById({json.dumps(OUT_ID)});"
            "var s=e?JSON.parse(e.textContent):{};"
            "return JSON.stringify({done:!!s.done,phase:s.phase,seq:s.seq||0,"
            "error:s.error||null,seed_kind:s.seed_kind||null});})()")
        # `seq` is what makes this a read of THIS pass. Without it the first poll
        # can see the previous pass's terminal state and return its payload.
        if st.get("seq") == seq and st.get("done"):
            if st.get("error"):
                _PARTIAL["seed_kind"] = st.get("seed_kind")
                raise _t.DriverStop(f"the in-page driver failed in phase "
                                    f"{st.get('phase')!r} using a "
                                    f"{st.get('seed_kind')!r} envelope: {st['error']}")
            return _read_out(tab)
    raise _t.DriverStop(f"the in-page driver did not finish within {max_wait:.0f}s")


#: Whatever the run had established when it stopped. Module-level so the stop
#: path can report a partial night instead of an empty file.
_PARTIAL: dict[str, Any] = {}
