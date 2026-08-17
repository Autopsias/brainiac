#!/usr/bin/env python3
"""The COS driver — a night's books, written by code (REST-02, 2026-08-10).

WHAT THIS IS. Everything a COS run must ACCOUNT FOR — the census, the ledgers,
the capture corpus, the metrics row, the outcome contract — produced by
deterministic code with no model anywhere in the path. The model's remaining job
is JUDGMENT (what matters, what to hold, what to stage, what to draft), and this
driver leaves exactly those slots null for it (s03).

WHY IT EXISTS. Runs 100-112 were a model executing browser mechanics from a
6,000-line constitution, and what failed was never judgment: it was miscounted
funnels, invented ledger vocabulary, a fabricated run ledger (run 64), a
metrics row that disagreed with its own ledger (runs 64/105/108/111), and a body
pass that wedged Chrome's evaluation bridge (run 112). Code cannot invent a
`held_reason`, cannot miscount a set it just built, and cannot claim a read it
did not make — so the mechanics move here.

    brain cos-run-begin --lane codex-automation      # the HOST stamps the sheet
    python3 tools/cos_driver.py --stage --tab-id <id>   # prints ONE line…
    #   …evaluate that line in the tab's MAIN world (a browser extension), which
    #   boots the page driver and seals the captured envelope where it was found
    python3 tools/cos_driver.py --vault <vault> --tab-id <chrome tab id>
    python3 tools/cos_driver.py --vault <vault> --replay <run-id>
    python3 tools/cos_driver.py --selfcheck

THE SEEDING STEP IS NOT CEREMONY. `osascript` — this file's transport — evaluates
in an ISOLATED world, a separate JS heap on the same document, so a capture hook
installed from here sees none of the app's traffic and a request issued from here
carries no `authorization`. The auth-bearing half therefore lives in the page's
MAIN world and never leaves it; `--stage` puts the page driver where that world
can reach it, and `#__cos_in`/`#__cos_out` (two hidden divs) are the only channel
between the two. Full measurement: `_evidence/s02/read-lane-seed-blocked.md`.

THE TWO HALVES, and the seam between them is the determinism claim:

  CAPTURE   drives the signed-in tab (DOM scan + service.svc FindItem/GetItem)
            and persists every raw response into the HOST-ONLY capture corpus.
  ACCOUNTING is a PURE function of that capture. `--replay <run-id>` rebuilds
            the ledgers and the metrics row from the corpus alone, in a separate
            process, and the two must be byte-identical.

Read-only by construction: the only verbs it can issue are `FindItem` and
`GetItem`, it never dispatches a click, and it refuses to fetch a message that
is not already read. `tests/test_cos_driver.py` asserts each of those against
the source of this file and of `tools/cos_driver_page.js`.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PAGE_JS = Path(__file__).resolve().parent / "cos_driver_page.js"
CONTRACT = Path(__file__).resolve().parent / "cos_contract.py"

#: The read lane this driver elects, recorded on every corpus row. It is NOT a
#: manifest lane and never claims to be: `brain cos-run-begin` pins the
#: DEPLOYMENT surface (`codex-automation` / `cowork-desktop`), a different axis,
#: and inventing a third value there would be a doctrine patch this plan put out
#: of scope. The browser TOOLSET the outcome contract knows is unchanged too
#: (`chrome-plugin`, the owner's pin) — what is new is the METHOD inside it.
READ_LANE = "rest"

#: `cos_contract.py` closed vocabulary. Nothing here is a judgment: a read-only
#: night archived nothing and drafted nothing, so every enumerated conversation
#: is still resident and undrafted.
BUCKET_RESIDENT = "held_non_drafted"

BODY_BUDGET_CHARS = 4000
BODY_BUDGET = "4000 extracted characters"
BODY_OPEN_CAP = 20

#: Fields excluded from the determinism diff, and why. Everything else in the
#: ledger and the metrics row is a function of the capture alone.
DIFF_EXCLUDED = {
    "run_ts": "the wall clock of the append itself; the CAPTURE's own stamps "
              "(`enumerated_at`, per-row `ts`) are inside the diff",
    "bundle_version": "stamped by the host from the run manifest at append time",
    "extraction_rules_version": "stamped by the host from the run manifest",
    "skill_sha256": "stamped by the host from the run manifest",
}


class DriverStop(Exception):
    """A condition the driver refuses to run past. Never a warning line."""


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ts(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def short(value: str) -> str:
    """A mailbox id as its 16-hex SHA-256 prefix.

    Conversation and item ids identify a real mailbox and this repo is a
    public-export source, so evidence written here carries digests. Set equality
    and symmetric differences are fully checkable from them (s01 precedent).
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# transport: one Chrome tab, addressed by ID
# ---------------------------------------------------------------------------
class ChromeTab:
    """`osascript` -> Chrome, addressed by TAB ID and retried.

    TAB INDICES ARE Z-ORDER AND THEY MOVE. `window 2` is the second window from
    the front, so activating any window renumbers every tab reference — measured
    2026-08-10, when a probe addressed `tab 18 of window 2` and evaluated against
    a YouTube tab for 200 polls without erroring once. The tab ID is stable for
    the tab's life; the window is re-resolved on every call.

    The bridge itself is flaky (`AppleEvent timed out (-1712)`, `Application
    isn't running (-600)` — both measured today on a healthy Chrome), which is
    why every call retries and why nothing long is ever run as one evaluation.
    """

    def __init__(self, tab_id: int, timeout: int = 45, tries: int = 4) -> None:
        self.tab_id = int(tab_id)
        self.timeout = timeout
        self.tries = tries

    @staticmethod
    def _osa(script: str, timeout: int) -> tuple[int, str, str]:
        p = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout.rstrip("\n"), p.stderr.strip()

    def _window(self) -> int:
        rc, out, err = self._osa('tell application "Google Chrome" to count windows', 20)
        if rc or not out.strip().isdigit():
            raise DriverStop(f"Chrome did not answer `count windows`: {err or out}")
        for w in range(1, int(out.strip()) + 1):
            rc, ids, _ = self._osa(
                f'tell application "Google Chrome" to get id of tabs of window {w}', 20)
            if rc == 0 and str(self.tab_id) in [x.strip() for x in ids.split(",")]:
                return w
        raise DriverStop(f"Chrome tab {self.tab_id} is not open in any window")

    def js(self, source: str) -> str:
        """Evaluate `source` in the tab and return its value as text.

        The payload is base64-encoded and `eval`-ed in page. That is a QUOTING
        decision, not a code-loading feature: AppleScript's `execute javascript`
        takes one double-quoted string, and hand-escaping a multi-kilobyte JS
        file through it is where the bugs live. The payload is always a file in
        this repository — never anything read from the page or the network.

        DECODED AS UTF-8, NOT VIA `atob` ALONE. `atob` yields a latin-1 string,
        so every multi-byte character (an em dash in a comment, the `·` in a
        priority chip name) arrives mangled and the eval dies of a syntax error
        — which `osascript` reports as the bare string `missing value`, with no
        exception anywhere. Measured 2026-08-10: a 20 KB staging write stored
        zero characters and raised nothing.
        """
        b64 = base64.b64encode(source.encode("utf-8")).decode("ascii")
        payload = ("eval(new TextDecoder().decode(Uint8Array.from("
                   f"atob('{b64}'), function(c){{return c.charCodeAt(0);}})))")
        last = "no attempt"
        for i in range(self.tries):
            try:
                w = self._window()
                rc, out, err = self._osa(
                    f'tell application "Google Chrome" to execute '
                    f'(first tab of window {w} whose id is {self.tab_id}) '
                    f'javascript "{payload}"', self.timeout)
                if rc == 0:
                    if out == "missing value":
                        # AppleScript's word for "your JS threw or returned
                        # undefined". Silent on both sides otherwise.
                        raise DriverStop(
                            "the tab returned `missing value` — the evaluated "
                            "source threw or produced no value")
                    return out
                last = err
            except (DriverStop, subprocess.TimeoutExpired) as exc:
                last = str(exc)
            time.sleep(0.8 * (i + 1))
        raise DriverStop(f"execute javascript failed after {self.tries} tries: {last}")

    def json(self, source: str) -> Any:
        raw = self.js(source)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise DriverStop(f"tab returned non-JSON ({exc}): {raw[:160]!r}") from None


class CdpTab:
    """The same tab, addressed over CDP instead of AppleScript.

    WHY A SECOND TRANSPORT (measured 2026-08-11). AppleScript addresses "Google
    Chrome" BY NAME, which is ambiguous the moment a second Chrome runs — and
    the capture lane requires a second Chrome, because Chrome 151 refuses
    `--remote-debugging-port` on a default profile. CDP addresses ONE browser by
    port and evaluates in the MAIN world, so the driver can also stage itself
    instead of asking another surface to boot it.

    The PAGE PROTOCOL is unchanged: `js()` returns the evaluated value as text,
    exactly as `ChromeTab.js` does, so every caller above is transport-blind.
    """

    def __init__(self, port: int = 9222) -> None:
        self.port = port
        self.tab_id = f"cdp:{port}"

    def js(self, source: str) -> str:
        # No `eval` on this transport: every caller above passes ONE expression,
        # so CDP evaluates it directly and `String(...)` only fixes the type.
        import cos_cdp_capture as cdp                            # noqa: PLC0415
        value = cdp.evaluate(f"String({source})", port=self.port)
        if value is None:
            raise DriverStop("the tab returned undefined — the evaluated source "
                             "threw or produced no value")
        return str(value)

    def json(self, source: str) -> Any:
        raw = self.js(source)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise DriverStop(f"tab returned non-JSON ({exc}): {raw[:160]!r}") from None


# ---------------------------------------------------------------------------
# the instruction sheet (MAN-01) — the gate, before anything else
# ---------------------------------------------------------------------------
def load_sheet(vault: Path) -> dict[str, Any]:
    """The host's frozen instruction sheet, read LITERALLY. Never composed.

    `brain cos-run-begin` is HOST-ONLY and writes this at run launch. A run with
    no sheet scores INCONCLUSIVE — "the host never recorded what was supposed to
    run" — which is how run 102 lost a whole night, and stamping one afterwards
    is not a gate at all. So the driver refuses to start.
    """
    from brain import cos                                        # noqa: PLC0415

    path = cos.current_run_path(vault)
    try:
        sheet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DriverStop(
            f"no readable host instruction sheet at {path} ({exc}). "
            "`brain cos-run-begin` writes it at run LAUNCH and it is HOST-ONLY: "
            "a run cannot stamp its own manifest, and one stamped afterwards is "
            "a gate that can be satisfied retroactively. Begin the run through "
            "the host, then start the driver.") from None

    for key in ("run_id", "expected_artifacts", "skill_path", "skill_sha256", "lane"):
        if not sheet.get(key):
            raise DriverStop(f"the instruction sheet at {path} carries no {key!r} "
                             "— it is not a MAN-01 sheet")

    manifest = cos.run_manifest(vault, sheet["run_id"])
    if manifest is None:
        raise DriverStop(
            f"the sheet names {sheet['run_id']} but the host holds no run "
            "manifest for it — the pointer and the frozen record disagree")

    got = hashlib.sha256(
        Path(sheet["skill_path"]).read_text(encoding="utf-8", errors="replace")
        .encode("utf-8")).hexdigest()
    if got != sheet["skill_sha256"]:
        raise DriverStop(
            f"the bundle the sheet pins ({sheet['skill_path']}) now hashes to "
            f"{got[:12]}…, not the frozen {sheet['skill_sha256'][:12]}… — the "
            "driver would be running under a stale manifest, and on any such "
            "disagreement NOT ONE host check runs")

    if sheet["lane"] not in ("codex-automation", "cowork-desktop"):
        raise DriverStop(
            f"the sheet pins lane {sheet['lane']!r}, which is not a manifest "
            "lane this build accepts. The driver's READ lane "
            f"({READ_LANE!r}) is a different axis and is recorded on the corpus "
            "rows, never invented into the manifest.")

    sheet["manifest"] = manifest
    return sheet


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
    state = tab.json(
        "JSON.stringify({p:location.pathname,"
        "rows:document.querySelectorAll('[role=\"option\"][data-convid]').length,"
        "out:!!document.getElementById('__cos_out')})")
    if not str(state.get("p", "")).rstrip("/").endswith("/mail"):
        raise DriverStop(
            f"the run-owned tab is on {state.get('p')!r}, not {MAIL_ROOT}. The "
            "driver refuses to navigate there itself: a navigation wipes the "
            "main-world capture hook and the captured envelope with it.")
    if not state.get("out"):
        raise DriverStop(
            "the run-owned tab carries no `#__cos_out` node, so "
            "`tools/cos_driver_page.js` was never injected into its MAIN world. "
            "The host's own AppleScript world is ISOLATED — injecting from here "
            "produces a driver that cannot see the captured envelope and is "
            "refused 401. Seed the tab first, then start the driver.")
    if not state.get("rows"):
        raise DriverStop(
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
        raise DriverStop(f"staged {got} of {len(src)} source characters")
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
    total = int(tab.js(
        f"(function(){{var e=document.getElementById({json.dumps(out_id)});"
        "if(!e)return '-1';"
        "var b=new TextEncoder().encode(e.textContent);var s='';"
        "for(var i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);"
        "window.__cosB64=btoa(s);return String(window.__cosB64.length);})()"))
    if total < 0:
        raise DriverStop(f"the `#{out_id}` bridge node vanished mid-run")
    parts = [tab.js(f"window.__cosB64.substr({off},{CHUNK})")
             for off in range(0, total, CHUNK)]
    b64 = "".join(parts)
    if len(b64) != total:
        raise DriverStop(f"the bridge read back {len(b64)} of {total} base64 "
                         "characters — the transport truncated the night")
    try:
        return json.loads(base64.b64decode(b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DriverStop(f"the bridge payload is not JSON ({exc})") from None


def capture_night(tab: ChromeTab, *, cap: int, poll_seconds: float,
                  max_wait: float, now: _dt.datetime) -> dict[str, Any]:
    """Run the in-page driver and return its raw output. No accounting here."""
    assert_ready(tab)
    window_start = _ts(now - _dt.timedelta(hours=24))

    # Pass 1: scan + enumerate + sent, with an EMPTY draw. The draw cannot be
    # computed until the enumeration exists, and the enumeration is what says
    # which rows are already read.
    opts = {"cap": 0, "budget": BODY_BUDGET_CHARS, "sent_window_start": window_start}
    first = _await_run(tab, 1, opts, poll_seconds, max_wait)

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
    opts = {"cap": len(draw), "budget": BODY_BUDGET_CHARS,
            "sent_window_start": window_start, "draw": draw, "max_scrolls": 0}
    res = _await_run(tab, 2, opts, poll_seconds, max_wait)
    return res["out"]["bodies"] or []


def _await_run(tab: ChromeTab, seq: int, opts: dict[str, Any],
               poll_seconds: float, max_wait: float) -> dict[str, Any]:
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
                raise DriverStop(f"the in-page driver failed in phase "
                                 f"{st.get('phase')!r} using a "
                                 f"{st.get('seed_kind')!r} envelope: {st['error']}")
            return _read_out(tab)
    raise DriverStop(f"the in-page driver did not finish within {max_wait:.0f}s")


# ---------------------------------------------------------------------------
# completeness: SETS, not counts (adv-10)
# ---------------------------------------------------------------------------
#: Until a trustworthy baseline has been MEASURED, the tolerance is zero. A
#: non-zero one would have to cite the measurement that justifies it.
SET_DIFFERENCE_TOLERANCE = 0


def completeness(capture: dict[str, Any]) -> dict[str, Any]:
    """The per-run enumeration invariant, compared as ID SETS.

    Counts are not sets. A truncated enumeration that drops five threads while
    five arrivals appear has an equal COUNT and is exactly the failure this
    exists to catch, so the symmetric difference is itemised and every element
    must be attributable. Any unexplained element is a HARD STOP before a single
    body is fetched: if enumeration silently under-returns, the ledgers, the
    metrics and the contract all agree with each other and the night scores PASS
    over a mailbox it barely read.
    """
    items = capture["enumeration"].get("items", [])
    enumerated = {it["convId"] for it in items if it.get("convId")}
    scanner = set(capture["scan"].get("ids") or [])
    # THE DOM SCANNER SEES ONE VIEW. Focused/Other is a UI filter and switching
    # it means clicking a tab, which this driver may not do — so the cross-check
    # is run over the REST census PARTITIONED by the same `InferenceClassification`
    # the displayed view represents. Comparing the whole REST set against one
    # view's DOM ids would raise a hard stop on every run for a reason that is
    # not a truncation. The uncovered partition is REPORTED, never assumed away:
    # `TotalItemsInView` reconciliation and pagination termination are the two
    # completeness signals that do cover the whole folder, and both are hard.
    view = capture["scan"].get("view")
    if view in ("Focused", "Other"):
        covered = {it["convId"] for it in items
                   if it.get("convId") and str(it.get("cls") or "") == view}
    else:
        covered = enumerated
    only_rest = sorted(covered - scanner)
    only_scan = sorted(scanner - covered)
    folder_total = capture["enumeration"].get("folder_total")
    messages = len(capture["enumeration"].get("items", []))
    return {
        "enumerated_count": len(enumerated),
        "scanner_count": len(scanner),
        "dom_cross_check_view": view,
        "dom_cross_check_covered": len(covered),
        "dom_cross_check_uncovered": len(enumerated - covered),
        "enumerated_ids": sorted(short(c) for c in enumerated),
        "scanner_ids": sorted(short(c) for c in scanner),
        "cross_checked_ids": sorted(short(c) for c in covered),
        "symmetric_difference": {
            "only_in_rest_enumeration": [short(c) for c in only_rest],
            "only_in_dom_scanner": [short(c) for c in only_scan],
        },
        # Nothing arrives or moves inside a read-only night by our hand, so an
        # element is attributable only to mail that arrived while we read. Any
        # element we cannot attribute is unexplained, and unexplained is fatal.
        "attributed": [],
        "unexplained_set_difference": len(only_rest) + len(only_scan),
        "tolerance": SET_DIFFERENCE_TOLERANCE,
        "tolerance_basis": "zero until a trustworthy baseline has been measured",
        "pagination_terminated": bool(capture["enumeration"].get("terminated")),
        "page_count": capture["enumeration"].get("page_count"),
        "terminating_condition": ("FindItem RootFolder.IncludesLastItemInRange"
                                  if capture["enumeration"].get("terminated")
                                  else "page cap reached without a last-item flag"),
        "folder_total_reported": folder_total,
        "messages_enumerated": messages,
        "folder_total_reconciled": folder_total == messages,
        "dom_declared_size": capture["scan"].get("declared"),
        "dom_scan_complete": bool(capture["scan"].get("complete")),
    }


def assert_complete(report: dict[str, Any]) -> None:
    if report["unexplained_set_difference"] > report["tolerance"]:
        raise DriverStop(
            f"enumeration completeness FAILED: "
            f"{report['unexplained_set_difference']} conversation id(s) appear "
            f"in one enumeration and not the other and none is attributable to "
            f"a recorded arrival — REST {report['enumerated_count']}, DOM "
            f"scanner {report['scanner_count']}. Refusing to fetch any body: a "
            "truncated night whose ledgers, metrics and contract all agree with "
            "each other scores PASS over a mailbox it barely read.")
    if not report["pagination_terminated"]:
        raise DriverStop(
            "enumeration paging never reached the end of the folder "
            f"({report['page_count']} page(s), no IncludesLastItemInRange) — a "
            "short read that never paged is the same failure as a dropped id.")
    if not report["folder_total_reconciled"]:
        raise DriverStop(
            f"the server reports {report['folder_total_reported']} item(s) in "
            f"the folder and the enumeration returned "
            f"{report['messages_enumerated']} — the census does not reconcile.")


# ---------------------------------------------------------------------------
# accounting: a PURE function of the capture
# ---------------------------------------------------------------------------
#: The three managed priority chips, read off the SERVER's own category list.
#: An observed chip is a fact about the mailbox, not a judgment about the mail —
#: which is exactly why the driver may use it to order the draw and may not use
#: it to decide what any thread MEANS.
CHIP_TIER = {"P0 · Now": "P0", "P1 · Today": "P1", "P2 · This week": "P2",
             "P3 · Read": "P3"}

#: `P3 · Read` reads back as tier P3 for the two things that need a tier — the
#: ADD-ONLY screen ("this thread already carries one of ours") and the draw
#: order — but it ASSERTS NO TIER, and that difference is load-bearing. The
#: v7 matrix writes it for `read`/P2, `read`/P3 AND `act`/P3 (DOCTRINE §4.1),
#: so reading it back as an assertion that the thread IS P3 would make
#: `triage.tier_vocabulary` reject tonight's honest `read`/P2 verdict as
#: "contradicts the row's own managed chip" — the ratchet, running backwards.
#: The ledger therefore SOURCES the tier differently for it, and
#: `cos_judge.load_night` only feeds `chip_tier` from the unambiguous source.
TIER_SOURCE_PRIORITY_CHIP = "outlook-priority-chip"
TIER_SOURCE_READ_CHIP = "outlook-read-chip"
AMBIGUOUS_TIER_CHIPS = ("P3 · Read",)


def _observed_chip(categories: list[str]) -> str | None:
    """The ONE managed chip on this thread, strongest first."""
    for name in ("P0 · Now", "P1 · Today", "P2 · This week", "P3 · Read"):
        if name in (categories or []):
            return name
    return None


def _tier(categories: list[str]) -> str | None:
    name = _observed_chip(categories)
    return CHIP_TIER[name] if name else None


def _tier_source(categories: list[str]) -> str:
    """Which KIND of managed chip the tier above was read off — derived from
    the SAME pick `_tier` made, never from a second scan that could disagree."""
    name = _observed_chip(categories)
    return (TIER_SOURCE_READ_CHIP if name in AMBIGUOUS_TIER_CHIPS
            else TIER_SOURCE_PRIORITY_CHIP)


def _draw_rank(tier: str | None) -> int:
    return {"P0": 0, "P1": 1}.get(tier or "", 2)


def conversations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per CONVERSATION, newest message winning, in a stable order.

    Deterministic ordering matters more than which order: the ledger is diffed
    byte-for-byte against a replay, so the sort has to be total. Received time
    then conversation id is total (two messages can share a timestamp; two
    conversations cannot share an id).
    """
    newest: dict[str, dict[str, Any]] = {}
    for it in items:
        cid = it.get("convId")
        if not cid:
            continue
        prev = newest.get(cid)
        if prev is None or str(it.get("received") or "") > str(prev.get("received") or ""):
            newest[cid] = it
    rows = list(newest.values())
    rows.sort(key=lambda r: (str(r.get("received") or ""), str(r.get("convId"))),
              reverse=True)
    return rows


def body_draw(convs: list[dict[str, Any]], cap: int,
              exclude: set[str] | frozenset[str] = frozenset()
              ) -> list[dict[str, str]]:
    """P0 before P1 before the rest, newest-first inside a group, unread EXCLUDED.

    The unread filter is applied HERE, before a single id reaches the fetcher:
    the page-side `fetchBody` refuses an unread message as a second gate, but a
    gate that is the only gate is one edit away from being none.

    `exclude` IS THE CATEGORY GATE, and it is the reason this parameter exists
    (JDG-01, 2026-08-10). SKILL.md rule 1¾ excludes a `never`-category thread on
    the DRAW, before its body is opened — "a `never` thread that was OPENED is a
    FAIL even when it is ledgered correctly afterwards", because it spent one of
    the twenty opens the cap owed to actionable material. The category is a
    JUDGMENT over typed fields, so the driver cannot compute it: the caller runs
    the category batch after enumeration and hands the excluded ids back here.
    Measured on run 115, whose draw had no such gate: 5 of 20 opens went to
    `never` threads (runs 103 and 108 lost 11 of 19 and 3 of 19 the same way).
    """
    eligible = [c for c in convs
                if c.get("isRead") is True and c.get("convId") not in exclude]
    eligible.sort(key=lambda c: (_draw_rank(_tier(c.get("categories"))),
                                 [-ord(ch) for ch in str(c.get("received") or "")],
                                 str(c.get("convId"))))
    return [{"convId": c["convId"], "itemId": c["itemId"]} for c in eligible[:cap]]


def starvation_stop(convs: list[dict[str, Any]], cap: int,
                    excluded: set[str] | frozenset[str]) -> str | None:
    """Did the gate take a drawable mailbox down to ZERO opens? (item 2, s09)

    THE DEGENERATE CASE, AND IT NEEDS NO CALIBRATION. A category pass that
    stamped everything `never` would blind the night, and the cited backstop —
    `_CATEGORY_DOMINANCE_MAX_SHARE`, 0.75, in `cos_runverify.check_category_stamp`
    — is evadable by construction: split the stamps across two `never` ids at
    50/50 and 100 % of the inbox is excluded while no single category reaches
    the bar. Two ids blind the night and pass.

    What share is TOO MUCH is an owner decision wanting data nobody has yet
    (`_evidence/s09/excluded-share.json`). What is unambiguous today is zero,
    and this is the one number a wrong pre-draw `never` cannot hide behind: no
    body was read, so an incorrectly excluded row is byte-identical to a
    correct one on this run's own artifacts — the only observable left is that
    nothing was drawn at all.

    Compared against the SAME `body_draw` with no exclusions, so the two calls
    differ in exactly ONE input. A mailbox that draws zero for its own reasons
    — every thread unread, an empty inbox — draws zero either way and is left
    alone; only a zero the gate CAUSED stops the night.

    A pure function on purpose: the caller runs inside a browser session, and a
    guard only reachable through a live mailbox is a guard no test can prove
    fires (`hardening-prose-is-not-a-mechanism`).
    """
    if not convs:
        return None
    ungated = body_draw(convs, cap)
    if not ungated or body_draw(convs, cap, exclude=excluded):
        return None
    held = sum(1 for c in convs if c["convId"] in excluded)
    return (f"the category gate excluded EVERY drawable thread: {len(ungated)} "
            f"conversation(s) would have had their bodies opened and {held} of "
            f"{len(convs)} enumerated row(s) were held out, leaving nothing. A "
            "night that reads no body judges nothing, and a pre-draw `never` "
            "is unfalsifiable from this run's own artifacts — no body was "
            "read, so the row is byte-identical to a correct exclusion. "
            "Stopping for review instead of reporting a quiet mailbox. Re-run "
            "without --categories to draw ungated, or fix the taxonomy at "
            "<vault>/overlay/cos/ingest.md")


def resolve_never(vault: Path, categories: dict[str, str]
                  ) -> dict[str, Any]:
    """Which of the model's stamped categories the OWNER's taxonomy calls `never`.

    THE DRIVER DOES NOT DECIDE A CATEGORY, and this is not it deciding one. The
    category arrives already judged, one per conversation, from the pre-draw
    category batch; all that happens here is a lookup in
    `<vault>/overlay/cos/ingest.md` — the owner's own config file — to ask which
    ids that file dispositions `never`. A stamp naming an id the taxonomy does
    not define is NOT excluded and is reported: an unknown id is a guess, and
    acting on a guess by skipping a body is the same defect as inventing one.
    """
    from brain import cos                                         # noqa: PLC0415

    taxonomy = cos.ingest_taxonomy(vault) or {}
    rules = taxonomy.get("rules") or {}
    never_ids = {cid for cid, r in rules.items()
                 if str((r or {}).get("disposition") or "").strip().lower() == "never"}
    excluded, undefined = set(), {}
    for conv, cat in categories.items():
        name = str(cat or "").strip()
        if not name:
            continue
        if name not in rules:
            undefined[name] = undefined.get(name, 0) + 1
            continue
        if name in never_ids:
            excluded.add(conv)
    return {"mode": taxonomy.get("mode"), "never_ids": sorted(never_ids),
            # WHAT THE OWNER ACTUALLY DEFINED, carried out so the gate's state
            # can be checked against it instead of against "non-empty string".
            "defined_ids": sorted(rules),
            "excluded": excluded, "undefined_categories": undefined,
            "categorised": len(categories)}


def load_categories(path: Path, *,
                    in_scope_ids: Iterable[str] | None = None) -> dict[str, str]:
    """`[{"conversation_id": ..., "category": ...}]` -> `{conv: category}`.

    ONE SHAPE, AND IT IS CHECKED (review 2026-08-13, round 2). This used to
    accept a mapping root too — "one shape reaching the disk in the other's
    clothing should not cost a night" — and that branch is exactly how a FAILED
    model run got read as an answer. The output a truncated or abandoned run
    leaves is a SINGLE OBJECT rather than an array, and
    `{"conversation_id": "c1", "category": "x"}` came out of the mapping branch
    as two stamps named `conversation_id` and `category`: neither is a
    conversation, both are silently wrong, and nothing downstream could tell.
    The batch asks for an array of rows; anything else is REFUSED.

    A refusal is survivable and documented: the nightly leaves `--categories`
    off, the draw runs ungated and `category_gate.state` reads `not-run` — the
    shape every run before this one had. What is not survivable is a wrong
    stamp, because a `never` stamp on the wrong thread silently withholds a
    body from the draw.

    `in_scope_ids`, when given, is the enumeration this answer was asked about.
    A stamp naming a conversation that enumeration does not carry means the
    file is not an answer to THIS run's batch, so the file is refused rather
    than partly believed. A category of `null` is kept as the empty string and
    simply never matches a `never` id.

    AN ABSENCE IS NOT A `null` (review 2026-08-13, round 4, Codex HIGH). This
    read the value as `str(row.get("category") or "").strip()`, which collapsed
    a MISSING key, an explicit `null` and a whitespace-only string into one
    empty string — and the coverage predicate then armed the gate on all three.
    Round 3's own requirement is that every value be "either `null` or a
    currently defined taxonomy id"; a row that never names the key is neither,
    it is a row the model did not finish. So the key must be PRESENT, and its
    value must be an explicit `null` (kept as the empty string, the honest
    "nothing to exclude here") or a nonblank string (checked against the
    taxonomy afterwards, as before). Anything else is a load REFUSAL, which
    lands on the same survivable path as every other one: exit 2, no
    `--categories`, an UNGATED draw, `category_gate.state` reading `not-run`.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(
            f"{path.name} is a JSON {type(raw).__name__}, not the array of "
            "`{conversation_id, category}` rows the batch asks for. A single "
            "object is what a failed or truncated model run leaves behind, and "
            "reading its KEYS as conversation ids is how a run stamps threads "
            "that do not exist")
    out: dict[str, str] = {}
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} row {i} is a "
                             f"{type(row).__name__}, not an object")
        cid = str(row.get("conversation_id") or "").strip()
        if not cid:
            raise ValueError(f"{path.name} row {i} carries no conversation_id — "
                             "a stamp that names no thread stamps nothing")
        if "category" not in row:
            raise ValueError(
                f"{path.name} row {i} (conversation {short(cid)}) carries no "
                "`category` key at all. An absence is not the `null` the batch "
                "calls honest — it is a row the model never finished, and "
                "reading the two as the same empty string is how an INCOMPLETE "
                "answer armed the gate as a complete one")
        cat = row["category"]
        if cat is None:
            value = ""
        elif not isinstance(cat, str) or not cat.strip():
            raise ValueError(
                f"{path.name} row {i} (conversation {short(cid)}) carries "
                f"category {cat!r}. The batch asks for `null` or one of the "
                "owner's category ids; a blank string, a whitespace-only one "
                "and a non-string are none of those, and each used to collapse "
                "into the same empty string an explicit `null` produces")
        else:
            value = cat.strip()
        # CONFLICT-AWARE DUPLICATE (run 132, live capture — the category-leg
        # analog of judge_night's H3). The STREAM-01 multi-turn reassembly
        # RECOVERS the full answer, but the model re-emits a boundary object
        # across a turn split, so an identical duplicate arrives — measured on
        # run 132, one re-emission in a 258-row answer. That is a benign
        # re-emission of the SAME stamp, not "two answers to one question", so it
        # is collapsed. Only a CONFLICT — two DIFFERENT categories for one thread
        # — is refused, the genuine ambiguity the old blanket refusal meant to
        # catch. Refusing the benign case failed the whole 258-row gate on a
        # single re-emission and read `not-run` on an answer that was complete.
        if cid in out:
            if out[cid] == value:
                continue
            raise ValueError(
                f"{path.name} stamps conversation {short(cid)} with two "
                f"DIFFERENT categories ({out[cid]!r} and {value!r}) — two answers "
                "to one question, and whichever wins is decided by row order")
        out[cid] = value
    if in_scope_ids is not None:
        stray = sorted(set(out) - {str(c) for c in in_scope_ids})
        if stray:
            raise ValueError(
                f"{path.name} stamps {len(stray)} conversation(s) this run did "
                f"not enumerate (e.g. {short(stray[0])}) — it is not an answer "
                "to this run's batch")
    return out


def category_gate_state(categories: dict[str, str] | None,
                        in_scope_ids: Iterable[str],
                        defined_categories: Iterable[str] | None
                        ) -> dict[str, Any]:
    """Did the pre-draw category gate actually run? ONE definition, two callers.

    THE TWO SPELLINGS WERE THE FIRST DEFECT (review 2026-08-13, round 2). The
    driver reported `armed` when `categories is not None` and the judge when
    `categories` was truthy, so an empty answer — `[]`, two bytes, which
    `[ -s ]` happily passes — made ONE run report `armed` from one leg and
    `not-run` from the other while excluding nothing at all.

    ONE NON-EMPTY STAMP WAS THE SECOND (round 3, Codex HIGH). Deriving state
    from "any in-scope stamp with a non-empty value" armed the gate on a
    PARTIAL answer and on a taxonomy-UNDEFINED one:
    `{"c0": "no-such-category"}` over three enumerated rows reported `armed`
    while `resolve_never` ignores unknown ids, so nothing could be excluded and
    nothing was.

    So `armed` now means what the batch prompt already CLAIMS is machine-checked
    ("EXACTLY ONE category id per conversation … an id the owner never wrote is
    REFUSED"): every enumerated conversation carries a row, and every value is
    either `null` (honest, and cheap — the row stays in the draw) or an id the
    owner's taxonomy defines. Exclusion is reported SEPARATELY and is never
    part of this predicate: a complete all-`null` answer is a gate that RAN and
    held nothing out, which is a different fact from a gate that never ran.

    `defined_categories` HAS NO DEFAULT, deliberately (round 4, Claude LOW). It
    used to default to `None`, which is this function's "the taxonomy could not
    be read on this leg" sentinel — so a caller that simply FORGOT the argument
    got a permanently disarmed gate explained by a cause that never happened. A
    missing argument is now a `TypeError` at the call site, which is loud.
    """
    ids = {str(c) for c in in_scope_ids}
    supplied = dict(categories or {})
    in_scope = {c: v for c, v in supplied.items() if c in ids}
    missing = sorted(ids - set(in_scope))
    stamped = sorted(c for c, v in in_scope.items() if str(v or "").strip())
    known = None if defined_categories is None else {
        str(c) for c in defined_categories}
    undefined = sorted({str(in_scope[c]).strip() for c in stamped
                        if known is not None and str(in_scope[c]).strip() not in known})

    if categories is None:
        why = "no category answer reached this leg"
    elif not known:
        # None (not read) and EMPTY (read, defines nothing) both land here on
        # purpose: with no defined id, NO STAMP CAN BE CHECKED, so whether this
        # answer is a valid one is unverifiable — and an unverifiable answer is
        # not an armed gate. (The reason is deliberately NOT "nothing could be
        # excluded": the sibling `null` case excludes nothing either and DOES
        # arm, because a taxonomy with no `never` id is a gate that ran and
        # held nothing out. Round 4, Claude LOW — the old wording gave a reason
        # its own neighbour contradicts.)
        why = ("the owner's taxonomy "
               + ("could not be read on this leg" if known is None
                  else "defines no category")
               + ", so no stamp in this answer could be CHECKED against it and "
                 "whether the gate really ran is unverifiable")
    elif not ids:
        why = "this run enumerated no conversation, so there was nothing to stamp"
    elif missing:
        why = (f"{len(missing)} of {len(ids)} enumerated conversation(s) carry "
               f"no stamp (e.g. {short(missing[0])}) — a partial answer gates "
               "only the part it covers, and used to report `armed` anyway")
    elif undefined:
        why = (f"{len(undefined)} stamp(s) name a category the owner's taxonomy "
               f"does not define (e.g. {undefined[0]!r}); an undefined id "
               "excludes nothing, so this would be `armed` over a gate that "
               "cannot hold anything out")
    else:
        why = (f"all {len(ids)} enumerated conversation(s) carry a stamp the "
               f"taxonomy defines — {len(stamped)} named a category and "
               f"{len(ids) - len(stamped)} answered null")
    armed = bool(categories is not None and known and ids
                 and not missing and not undefined)
    return {"state": "armed" if armed else "not-run",
            # COVERAGE, not exclusion. `categorised_in_scope` is how many rows
            # named a category at all; how many of those the taxonomy
            # dispositions `never` — the actual exclusion — is the caller's
            # `excluded_before_draw`, and the two are never the same number.
            "stamps_in_scope": len(in_scope),
            "categorised_in_scope": len(stamped),
            "unstamped_in_scope": len(missing),
            "undefined_ids": undefined[:8],
            "stamps_supplied": len(supplied),
            "in_scope": len(ids),
            "why": why}


def build_accounting(capture: dict[str, Any], *, run_id: str,
                     bundle_version: str, rules_version: str,
                     enumerated_at: str,
                     gate_excluded: set[str] | frozenset[str] = frozenset()
                     ) -> dict[str, Any]:
    """Ledger rows + counters, computed from the capture and nothing else.

    JUDGMENT SLOTS ARE `None`, DELIBERATELY AND VISIBLY. `disposition`,
    `held_reason`, `category`, `verdict` and `dedup_check` are the judge's
    (s03). The driver writing a plausible value into any of them is the exact
    defect this rebuild exists to remove: run 106 coined `no-new-substance` and
    15 rows fell out of every total; run 108 coined
    `no-substance-or-already-represented` and the one check written to score
    substance verdicts passed reporting there were none.
    """
    convs = conversations(capture["enumeration"].get("items", []))
    bodies = {b["conv_id"]: b for b in capture.get("bodies", [])}
    opened_seq: dict[str, int] = {}
    seq = 0
    for d in capture.get("draw", []):
        b = bodies.get(d["convId"])
        if b and b.get("ok") and int(b.get("body_chars") or 0) > 0:
            seq += 1
            opened_seq[d["convId"]] = seq

    rows: list[dict[str, Any]] = []
    for c in convs:
        cid = c["convId"]
        b = bodies.get(cid)
        opened = cid in opened_seq
        row: dict[str, Any] = {
            "run": run_id,
            "run_profile": "full",
            "conversation_id": cid,
            "message_id": c.get("itemId"),
            "received": c.get("received"),
            "read_state": "read" if c.get("isRead") else "unread",
            "read_lane": READ_LANE,
            "tier": _tier(c.get("categories")),
            "tier_source": _tier_source(c.get("categories")),
            "body_opened": opened,
            # A FACT ABOUT THE PASS, not a judgment about the mail: this row was
            # held out of the rule-1½ draw because the category batch stamped it
            # with an id the owner's taxonomy dispositions `never`. The judgment
            # is the CATEGORY, and it is the model's; what the driver records is
            # that the body was consequently never opened. `cos_judge`'s
            # `mechanical_disposition` reads this to write rule 1¾'s pairing
            # (`no-substance` / `never-category`) without asking the model to
            # re-decide something already on disk.
            "category_gate_excluded": cid in gate_excluded,
            "body_chars": int(b.get("body_chars") or 0) if b else 0,
            "body_open_seq": opened_seq.get(cid),
            "body_budget": BODY_BUDGET,
            "staging_cap": BODY_OPEN_CAP,
            "attachment_lane": "not-exercised",
            "send_attempted": False,
            "extraction_rules_version": rules_version,
            "bundle_version": bundle_version,
            "ts": enumerated_at,
            # --- judgment slots, owned by s03 and left EMPTY on purpose -------
            "verdict": None,
            "category": None,
            "disposition": None,
            "held_reason": None,
            "dedup_check": None,
            "candidate_count": 0,
            "proposal_id": None,
            "content_sha256": None,
            "judgment_pending": True,
        }
        rows.append(row)

    in_scope = len(rows)
    return {
        "rows": rows,
        "counters": {
            "ingestion_in_scope": in_scope,
            "ingestion_candidates": 0,
            "ingestion_held": in_scope,
        },
        "body_open_actual": len(opened_seq),
    }


def build_contract_inputs(capture: dict[str, Any], accounting: dict[str, Any], *,
                          run_id: str, enumerated_at: str, reported_at: str
                          ) -> tuple[dict[str, Any], dict[str, Any]]:
    """PRE and POST for `tools/cos_contract.py`.

    Nothing here is a judgment either. A read-only night archived nothing and
    drafted nothing, so every enumerated conversation is still resident and
    undrafted — `held_non_drafted` — and every archive candidate is INELIGIBLE
    for one mechanical reason: this driver has no mutation lane at all.
    """
    convs = conversations(capture["enumeration"].get("items", []))
    ids = [c["convId"] for c in convs]
    scan = capture["scan"]
    sent = capture["sent"]
    evidence = {
        "unique_ids": len(ids),
        "list_declared_size": len(ids),
        "stagnant_scans": int(scan.get("stagnant_scans") or 0),
        "scroll_at_end": bool(scan.get("at_end")),
        "dom_scanner_ids": len(scan.get("ids") or []),
        "dom_declared_size": scan.get("declared"),
        "rest_pages": capture["enumeration"].get("page_count"),
        "rest_terminated": bool(capture["enumeration"].get("terminated")),
    }
    provenance = {
        "run_id": run_id,
        "toolset": "chrome-plugin",
        "folder": "Inbox",
        "identity_field": "conversation_id",
        "read_lane": READ_LANE,
    }
    sent_block = {
        "identity_field": "item_id",
        "identity_source": "service.svc FindItem ItemId",
        "window_start": capture["window_start"],
        "captured_at": sent.get("captured_at") or enumerated_at,
        "sort": "newest-first",
        "complete": True,
        "boundary": "list-end",
        "boundary_timestamp": None,
        "items": sent.get("items") or [],
    }
    pre = {
        "run_profile": "full",
        "run_id": run_id,
        "enumerated_at": enumerated_at,
        "enumerated": ids,
        "pre_run_holds": {c["convId"]: "Held · chip"
                          for c in convs if _tier(c.get("categories"))},
        "inbox_conversation_count_before": len(ids),
        "owa_folder_item_count_before": len(capture["enumeration"].get("items", [])),
        "enumeration_complete": True,
        "enumeration_evidence": evidence,
        "scan_provenance": provenance,
        "browser_election": {
            "attempted": ["chrome-plugin"],
            "elected": "chrome-plugin",
            "chrome_plugin_result": ("owner-pinned lane; run-owned tab; read-only "
                                     "service.svc FindItem/GetItem, no click dispatch"),
        },
        "sent_zero_send": sent_block,
    }
    post = {
        "run_profile": "full",
        "run_id": run_id,
        "enumerated_at": reported_at,
        "post_run": {cid: BUCKET_RESIDENT for cid in ids},
        "inbox_conversation_count_after": len(ids),
        "owa_folder_item_count_after": len(capture["enumeration"].get("items", [])),
        "enumeration_complete": True,
        "enumeration_evidence": evidence,
        "scan_provenance": provenance,
        "sent_zero_send": dict(sent_block, captured_at=reported_at),
        "arrived_during_run": [],
        "candidates": [
            {"convid": cid, "capability": "archives", "eligible": False,
             "exclusion_reason": "read-only night: the driver has no mutation lane"}
            for cid in ids
        ],
        "capabilities": {
            "archives": {"in_scope": True, "exercised": False},
            "drafts": {"in_scope": True, "exercised": False},
            "chip_clears": {"in_scope": True, "exercised": False},
        },
    }
    return pre, post


# ---------------------------------------------------------------------------
# the corpus is the FIXTURE
# ---------------------------------------------------------------------------
def corpus_extraction(row: dict[str, Any]) -> dict[str, Any]:
    """The mechanical facts a replay needs to rebuild this row from the corpus.

    The corpus already holds the TEXT; these are the census facts around it.
    Together they make the corpus a complete fixture for the accounting path,
    which is what makes "same captured inputs => byte-identical ledgers" a
    statement anyone can check rather than a claim.
    """
    out = {k: row[k] for k in ("received", "read_state", "tier", "tier_source",
                               "body_opened", "body_chars", "body_open_seq",
                               "message_id")}
    # `.get`, and only for this one: every row THIS builder emits carries it,
    # but a row from a night that predates the category gate does not, and a
    # replay of one must rebuild rather than crash.
    out["category_gate_excluded"] = bool(row.get("category_gate_excluded"))
    return out


def write_corpus(vault: Path, run_id: str, accounting: dict[str, Any],
                 capture: dict[str, Any]) -> dict[str, Any]:
    """One corpus row per in-scope thread, carrying the TYPED FIELDS.

    THE SUBJECT AND THE SENDER ARE PERSISTED FOR EVERY ROW, not only for opened
    ones (JDG-01, 2026-08-10; the sender 2026-08-11 for the same reason, measured
    on run 117: 283 of 303 rows judged with `sender: null`, which disarms the
    priority map and every recurring-sender count).
    Phase 1.5 triages from typed fields and nothing else (INJ-03),
    so a row whose subject this run captured and then discarded cannot be
    triaged at all — measured on run 115, where 290 of 310 enumerated rows
    reached the judgment layer with a timestamp and a read-state and no way to
    tell what they were about. `FindItem` already returns the subject on every
    enumerated item; the only defect was throwing it away.
    """
    from brain import cos_corpus                                 # noqa: PLC0415

    bodies = {b["conv_id"]: b for b in capture.get("bodies", [])}
    enumerated = {i.get("convId"): i
                  for i in capture.get("enumeration", {}).get("items", [])}
    appended = 0
    for row in accounting["rows"]:
        cid = row["conversation_id"]
        b = bodies.get(cid) if row["body_opened"] else None
        cos_corpus.append_thread(
            vault, run_id,
            conversation_id=cid,
            text=(b or {}).get("text", "") if b else "",
            sender=((b or {}).get("sender")
                    or (enumerated.get(cid) or {}).get("sender") or None),
            sent=(b or {}).get("sent"),
            subject=((b or {}).get("subject")
                     or (enumerated.get(cid) or {}).get("subject") or None),
            read_lane=READ_LANE,
            body_opened=bool(row["body_opened"]),
            extraction=corpus_extraction(row))
        appended += 1
    cos_corpus.close_run(vault, run_id)
    return {"appended": appended, "run": run_id}


def accounting_from_corpus(vault: Path, run_id: str, *, bundle_version: str,
                           rules_version: str, enumerated_at: str) -> dict[str, Any]:
    """Rebuild the ledger rows from the CORPUS alone — the replay path.

    Deliberately a different entry point over the same builder: re-hashing an
    output file proves the file did not change, which is not what "byte-identical
    from the same captured inputs" means.
    """
    from brain import cos_corpus                                 # noqa: PLC0415

    items = []
    bodies = []
    draw: list[dict[str, str]] = []
    gate_excluded: set[str] = set()
    for r in cos_corpus.read_corpus(vault, run_id):
        ext = r.get("extraction") or {}
        cid = r["conversation_id"]
        items.append({
            "convId": cid,
            "itemId": ext.get("message_id"),
            "isRead": ext.get("read_state") == "read",
            "categories": [k for k, v in CHIP_TIER.items() if v == ext.get("tier")],
            "received": ext.get("received"),
            "subject": (r.get("provenance") or {}).get("subject") or "",
        })
        if ext.get("category_gate_excluded"):
            # PERSISTED, NOT RE-DERIVED. The replay has no taxonomy lookup and
            # no category batch; re-deciding the exclusion here would make the
            # determinism check a test of two lookups agreeing rather than of
            # the accounting being a pure function of the capture.
            gate_excluded.add(cid)
        if ext.get("body_opened"):
            bodies.append({"conv_id": cid, "ok": True,
                           "body_chars": int(ext.get("body_chars") or 0),
                           "text": r.get("text", ""),
                           "seq": ext.get("body_open_seq")})
    bodies.sort(key=lambda b: int(b.get("seq") or 0))
    draw = [{"convId": b["conv_id"], "itemId": None} for b in bodies]
    capture = {"enumeration": {"items": items}, "bodies": bodies, "draw": draw,
               "scan": {}, "sent": {}}
    return build_accounting(capture, run_id=run_id, bundle_version=bundle_version,
                            rules_version=rules_version, enumerated_at=enumerated_at,
                            gate_excluded=gate_excluded)


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------
def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                      for r in rows)
    path.write_text(payload, encoding="utf-8")


def write_report(path: Path, run_id: str, accounting: dict[str, Any],
                 completeness_report: dict[str, Any]) -> None:
    """The run report. It states 0 repair rounds because the driver repairs
    NOTHING in flight: a counter is computed from the ledger once, and there is
    no second pass that could disagree with the first."""
    c = accounting["counters"]
    path.write_text(
        f"# COS run {run_id} — driver night (read-only)\n\n"
        f"Produced by `tools/cos_driver.py`. Mechanics only: this run made no "
        f"judgment and staged no candidate.\n\n"
        f"## Census\n\n"
        f"- conversations enumerated: {completeness_report['enumerated_count']} "
        f"(DOM scanner {completeness_report['scanner_count']}, unexplained set "
        f"difference {completeness_report['unexplained_set_difference']})\n"
        f"- messages enumerated: {completeness_report['messages_enumerated']} "
        f"against a server folder total of "
        f"{completeness_report['folder_total_reported']}\n"
        f"- bodies opened: {accounting['body_open_actual']} of a cap of "
        f"{BODY_OPEN_CAP}, budget {BODY_BUDGET}\n"
        f"- ingestion in scope {c['ingestion_in_scope']}, candidates "
        f"{c['ingestion_candidates']}, held {c['ingestion_held']}\n\n"
        f"## Judgment\n\n"
        f"Every judgment slot in `_cos_ingestion_ledger_{run_id}.jsonl` is "
        f"`null` (`judgment_pending: true`): `verdict`, `category`, "
        f"`disposition`, `held_reason`, `dedup_check`. The driver does not own "
        f"them and does not guess them.\n\n"
        f"## 🧪 Run-integrity — E-checks (0 repair rounds)\n\n"
        f"The bundle's self-eval is a JUDGMENT pass over this night's artifacts "
        f"and is not the driver's to report. It is left unexecuted rather than "
        f"asserted.\n\n"
        f"## 🔧 Repairs\n\n"
        f"None.\n",
        encoding="utf-8")


def run_host_checks(vault: Path, run_id: str) -> dict[str, Any]:
    """Every host check, EXECUTED — never "would have passed".

    The verdict is reported as it comes back. A read-only night leaves the
    judgment slots empty by design, and the checks that score judgment are
    therefore expected to FAIL; naming which ones is the honest form of that,
    and suppressing them would be the dishonest one.

    `--quiesce-seconds 0` is safe HERE and only here: the quiesce window exists
    so a validator does not score a run that is still writing, and this call is
    made by the writer itself, after its last write. It does NOT pass
    `--record` — scoring for the evidence file is not claiming the run.
    """
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "cos_run_verify.py"),
         str(vault), "--run-id", run_id, "--quiesce-seconds", "0", "--json"],
        capture_output=True, text=True, timeout=1800)
    try:
        report = json.loads(proc.stdout)[0]
    except (ValueError, IndexError, KeyError):
        return {"verdict": "not-scored", "returncode": proc.returncode,
                "stderr": proc.stderr[-800:]}
    checks = report.get("checks") or []
    return {
        "verdict": report.get("verdict"),
        "executed": [c["check"] for c in checks],
        "executed_count": len(checks),
        "passed": [c["check"] for c in checks if c.get("status") == "pass"],
        "failed": [{"check": c["check"], "detail": c.get("detail", "")[:400]}
                   for c in checks if c.get("status") != "pass"],
        "inputs_digest": report.get("inputs_digest"),
    }


def run_contract(ops: Path, run_id: str, pre: Path, post: Path,
                 out: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(CONTRACT), "--pre", str(pre), "--post", str(post),
         "--ledgers", str(ops), "--run-id", run_id, "--profile", "full",
         "--out", str(out)],
        capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ---------------------------------------------------------------------------
# the night
# ---------------------------------------------------------------------------
def _persist(evidence_path: Path | None, evidence: dict[str, Any]) -> None:
    if not evidence_path:
        return
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def run_night(vault: Path, tab_id: int | None, *, cap: int,
              evidence_path: Path | None,
              poll_seconds: float = 3.0, max_wait: float = 900.0,
              exclude_convids: set[str] | None = None,
              categories: dict[str, str] | None = None,
              prior_enumeration: list[dict[str, Any]] | None = None,
              use_cdp: bool = False) -> dict[str, Any]:
    """A read-only night. A STOP is written to the evidence file, not swallowed:
    a night that stopped and a night that found nothing must never look alike."""
    try:
        return _run_night(vault, tab_id, cap=cap, evidence_path=evidence_path,
                          poll_seconds=poll_seconds, max_wait=max_wait,
                          exclude_convids=exclude_convids,
                          categories=categories,
                          prior_enumeration=prior_enumeration, use_cdp=use_cdp)
    except DriverStop as exc:
        _persist(evidence_path, dict(_PARTIAL, stopped=str(exc),
                                     stopped_at=_ts(_utcnow())))
        raise


#: The typed fields the CATEGORY batch is allowed to see. Phase 1.5 judges from
#: typed fields and nothing else (INJ-03) and no body exists yet at this point in
#: the night, so this list is the whole surface — it is stated here rather than
#: assembled ad hoc so that adding a field is a decision someone makes on purpose.
ENUMERATION_FIELDS = ("conversation_id", "subject", "sender", "received",
                      "read_state", "chip")


def enumeration_row(conv: dict[str, Any]) -> dict[str, Any]:
    """One capture item projected onto the typed fields the categoriser sees.

    ONE PROJECTION, TWO CALLERS. `enumerate_only` writes these rows out and
    `_run_night` re-derives them from its OWN enumeration to compare — and a
    comparison between two spellings of "the typed fields" would report drift
    that is really a formatting difference, or miss drift that is real.
    """
    return {"conversation_id": conv["convId"],
            "subject": conv.get("subject") or "",
            "sender": conv.get("sender") or None,
            "received": conv.get("received"),
            "read_state": "read" if conv.get("isRead") else "unread",
            "chip": _tier(conv.get("categories"))}


def row_digest(row: dict[str, Any]) -> str:
    """The identity of ONE enumerated conversation, over every typed field.

    Every field, because every field is an INPUT to the category judgment: a
    thread whose subject changed, whose sender changed, or which was read since
    the stamp was made is a thread the model judged from data that no longer
    describes it.
    """
    return hashlib.sha256(json.dumps(
        {k: row.get(k) for k in ENUMERATION_FIELDS},
        sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def bind_categories(categories: dict[str, str],
                    prior_rows: list[dict[str, Any]],
                    live_convs: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind stamps judged on enumeration A to the enumeration B about to draw.

    TWO ENUMERATIONS, ONE SET OF STAMPS (review 2026-08-13, round 1, HIGH).
    `cos_nightly.sh` enumerates, runs the model, then starts the driver AGAIN
    and it re-enumerates. Stamps were applied by conversation id to whatever
    the second read returned — so a thread that arrived during the model call
    entered the draw with no stamp at all, and a thread whose subject, sender
    or read state CHANGED was excluded on data the model never saw. Nothing
    compared the two, so the gate's own metrics still read valid.

    Two passes are structural here and carrying A whole into B is not
    available: the body fetch needs a fresh `itemId` (OWA re-issues one when an
    item moves) and the completeness cross-check has to be a live read. So the
    delta is RESOLVED rather than wished away, and every part of it is counted:

    * `honored` — the thread is still here and its typed fields are unchanged.
      Only these can exclude anything.
    * `stale` — still here, fields CHANGED. The stamp is dropped: it was judged
      from data that no longer describes the thread, and the conservative
      reading of a stale `never` is to leave the row in the draw. Withholding a
      body on an obsolete judgment is silent blindness; opening one that did
      not need opening costs a slot and is visible.
    * `arrivals` — in B, never in A. Nothing judged them, so they draw UNGATED,
      and the count says so on the report rather than being inferred.
    * `departed` — in A, gone from B. Excluding nothing is already correct.
    """
    prior = {str(r.get("conversation_id")): row_digest(r) for r in prior_rows}
    live = {c["convId"]: row_digest(enumeration_row(c)) for c in live_convs}
    both = set(prior) & set(live)
    stale = sorted(c for c in both if prior[c] != live[c])
    honored = {c: v for c, v in categories.items()
               if c in both and c not in stale}
    return {"honored": honored,
            "scope": sorted(both),
            "stale": stale,
            "arrivals": sorted(set(live) - set(prior)),
            "departed": sorted(set(prior) - set(live)),
            "stamps_dropped_as_stale": sorted(c for c in stale
                                              if c in categories)}


def enumerate_only(vault: Path, tab_id: int | None, *,
                   evidence_path: Path | None,
                   poll_seconds: float = 3.0, max_wait: float = 900.0,
                   use_cdp: bool = False) -> dict[str, Any]:
    """Pass 1 alone: enumerate, cross-check, and STOP before the draw.

    THIS IS THE SEAM THE CATEGORY GATE NEEDED (GAP 9). `body_draw`'s `exclude`
    parameter — the gate itself — has existed since JDG-01 and has never once
    been fed, because the category is a MODEL judgment and the model ran after
    every body was already open. Measured on runs 126, 129 and 130 alike: 8 of
    ~228 rows carried a `never` category and 8 of the night's 20 body opens went
    to them, while `category_gate.state` reported `not-run` on every run ever
    scored.

    `capture_night` already runs pass 1 with an EMPTY draw, so nothing new is
    read here — this only stops afterwards and writes the typed fields out, so
    a caller can put a category batch between the two passes. It writes NO
    ledger, NO contract and NO corpus: those belong to the night that opens
    bodies, and a half-night that wrote them would be a second run.
    """
    now = _utcnow()
    sheet = load_sheet(vault)
    try:
        if use_cdp:
            tab: Any = CdpTab()
            tab.js(stage(tab))
        else:
            tab = ChromeTab(tab_id)                              # type: ignore[arg-type]
        capture = capture_night(tab, cap=0, poll_seconds=poll_seconds,
                                max_wait=max_wait, now=now)
        report = completeness(capture)
        assert_complete(report)
    except DriverStop as exc:
        # SAME CONTRACT AS `run_night`: a stop is written to the evidence file,
        # not swallowed. The nightly's failure message names this path, and a
        # path it names has to exist when it points there.
        _persist(evidence_path, {"run_id": sheet["run_id"], "rows": [],
                                 "stopped": str(exc),
                                 "stopped_at": _ts(_utcnow())})
        raise
    convs = conversations(capture["enumeration"].get("items", []))
    out = {
        "run_id": sheet["run_id"],
        "enumerated_at": capture["enumeration"].get("at") or _ts(now),
        "window_start": capture["window_start"],
        "driver_transport": "cdp" if use_cdp else "applescript",
        "fields": list(ENUMERATION_FIELDS),
        "completeness": report,
        "rows": [enumeration_row(c) for c in convs],
    }
    _persist(evidence_path, out)
    return out


#: Whatever the run had established when it stopped. Module-level so the stop
#: path can report a partial night instead of an empty file.
_PARTIAL: dict[str, Any] = {}


def _run_night(vault: Path, tab_id: int | None, *, cap: int,
               evidence_path: Path | None,
               poll_seconds: float = 3.0, max_wait: float = 900.0,
               exclude_convids: set[str] | None = None,
               categories: dict[str, str] | None = None,
               prior_enumeration: list[dict[str, Any]] | None = None,
               use_cdp: bool = False) -> dict[str, Any]:
    from brain import cos                                        # noqa: PLC0415
    import cos_reconcile_metrics as recon                        # noqa: PLC0415

    now = _utcnow()
    sheet = load_sheet(vault)
    run_id = sheet["run_id"]
    manifest = sheet["manifest"]
    ops = cos.run_ops_dir(vault)
    raw_sources = len(list((vault / "raw").rglob("*.md")))
    evidence: dict[str, Any] = {
        "session": "s02", "item": "REST-02",
        "run_id": run_id,
        "run_id_source": "host-stamped MAN-01 sheet (brain cos-run-begin)",
        "manifest_lane": sheet["lane"],
        "manifest_lane_accepted": True,
        "driver_read_lane": READ_LANE,
        "vault_root_asserted": {
            "BRAIN_VAULT": str(vault),
            "raw_source_count_at_preflight": raw_sources,
            "cos_ops_exists": ops.is_dir(),
            "asserted_before_any_browser_action": True,
        },
        "started_at": _ts(now),
        # Declared UP FRONT and overwritten as the night earns them. A stop then
        # leaves a complete-shaped record whose zeros are visible, instead of an
        # absence a reader has to interpret (WAT-01: ship the failure mode with
        # the number that reveals it).
        "bodies_attempted": 0,
        "bodies_succeeded": 0,
        "bodies_error": 0,
        "seed_kind": None,
        "contract": {"exit_code": None, "render": "not reached"},
        "host_checks_executed": [],
        "second_process_diff": None,
        "excluded_fields": DIFF_EXCLUDED,
        "fixture_ref": None,
    }
    _PARTIAL.clear()
    _PARTIAL.update(evidence)

    if use_cdp:
        # This transport reaches the MAIN world itself, so the driver stages AND
        # boots its own page half — no second surface, nothing to do by hand.
        tab: Any = CdpTab()
        tab.js(stage(tab))
    else:
        tab = ChromeTab(tab_id)                                  # type: ignore[arg-type]
    evidence["driver_transport"] = "cdp" if use_cdp else "applescript"
    capture = capture_night(tab, cap=cap, poll_seconds=poll_seconds,
                            max_wait=max_wait, now=now)
    report = completeness(capture)
    evidence["completeness"] = report
    assert_complete(report)

    convs = conversations(capture["enumeration"].get("items", []))
    # THE GATE, FED. `categories` is the pre-draw category batch's answer, one
    # stamp per conversation, judged by the model from typed fields alone. The
    # driver looks each id up in the OWNER's taxonomy and excludes the ones that
    # file dispositions `never` — it never decides a category itself.
    gate: dict[str, Any] = {}
    # THE STAMPS WERE JUDGED ON ANOTHER ENUMERATION, AND ARE BOUND TO IT
    # (review 2026-08-13, round 1, HIGH). `prior_enumeration` is the
    # `--enumerate-only` output the category batch was asked about; anything
    # the mailbox did since then — an arrival, a thread whose subject or read
    # state changed — is resolved by `bind_categories` before a stamp can
    # exclude a body. Without it, this pass applied stamps by id to a snapshot
    # the model never saw.
    binding: dict[str, Any] = {}
    gate_scope: list[str] = [c["convId"] for c in convs]
    if categories is not None:
        if prior_enumeration is None:
            raise DriverStop(
                "a category answer was supplied with no enumeration to bind it "
                "to. The stamps were judged on the `--enumerate-only` snapshot "
                "and this pass re-enumerates, so without that file a stamp "
                "would be applied by id to a mailbox the model never saw — an "
                "arrival would draw ungated and a changed thread would be "
                "excluded on obsolete data, with nothing to notice either")
        binding = bind_categories(categories, prior_enumeration, convs)
        gate_scope = binding["scope"]
        gate = resolve_never(vault, binding["honored"])
        exclude_convids = set(exclude_convids or ()) | gate["excluded"]
    excluded = frozenset(exclude_convids or ())
    # An id we were handed for a conversation this enumeration does not carry
    # excludes nothing; counting it would inflate the metric that proves the
    # gate works, which is the one number that must stay honest.
    in_scope_excluded = {c["convId"] for c in convs if c["convId"] in excluded}
    # THE DEGENERATE CASE, AND IT NEEDS NO THRESHOLD (review 2026-08-13, round
    # 1, HIGH). A category pass that stamped everything `never` would blind the
    # night, and the cited backstop — `_CATEGORY_DOMINANCE_MAX_SHARE`, 0.75 —
    # is evadable by construction: split the stamps across two `never` ids at
    # 50/50 and 100% of the inbox is excluded while no single category reaches
    # the bar. What SHARE is too much is an owner decision that wants data
    # nobody has yet (`_evidence/s09/excluded-share.json`); what is unambiguous
    # today is ZERO. A mailbox that would have drawn bodies and draws none
    # BECAUSE of the gate is a blinded night, and a blinded night must stop for
    # a human rather than proceed reporting a quiet one.
    #
    # Compared against the SAME `body_draw` with no exclusions, so the two
    # differ in exactly one input: any zero it reports is the gate's doing and
    # not an all-unread mailbox, which draws zero either way and is left alone.
    ungated_draw = body_draw(convs, cap)
    starved = starvation_stop(convs, cap, excluded)
    # AND WHAT THE INTERLOCK WOULD HAVE SAID ABOUT THE SCOPE IT WAS ASKED ABOUT
    # (review 2026-08-13, round 5 — the one place the two review lanes
    # disagreed, settled by probe in `_evidence/s09/arrivals-probe.txt`).
    #
    # Codex said an arrival flood fills the cap ungated and pushes
    # `starvation_stop` back to None; Claude said `body_draw` filters
    # `isRead is True` so a new arrival cannot enter the draw at all. The probe
    # says BOTH, on different arrivals: an UNREAD arrival is structurally
    # excluded and the interlock still fires; a READ one — the owner opening
    # mail on a phone during a 15-40 minute model call, or a thread re-entering
    # the window — enters the draw ungated AND silences the interlock.
    #
    # WHAT TO DO ABOUT THE ARRIVAL ITSELF IS THE OWNER'S CALL (defer it, judge
    # the delta, or stop) and is carded, not invented here. What is NOT a
    # policy question is whether the night can tell: a blinded mailbox that
    # drew one arrival looked exactly like a healthy one. So the interlock is
    # ALSO evaluated over `gate_scope` — the threads the model was actually
    # asked about — and the difference is reported. Reported, not policed:
    # nothing changes what the night does.
    scope_ids = set(gate_scope)
    starved_in_scope = starvation_stop(
        [c for c in convs if c["convId"] in scope_ids], cap, excluded)
    draw = body_draw(convs, cap, exclude=excluded)
    capture["draw"] = draw
    # WAT-01: the gate ships with the number that reveals it was never armed.
    # `state` comes from the ONE shared predicate the judge also calls, so the
    # two legs cannot disagree about the same run — and an empty answer reads
    # `not-run` on both, because it excluded nothing.
    # `defined_ids` is the owner's taxonomy as `resolve_never` read it — the
    # same load, not a second one — so `armed` is checked against what the
    # owner actually wrote rather than against "the string is non-empty".
    # SCOPED TO WHAT THE MODEL WAS ASKED ABOUT AND WHAT IS STILL HERE. Judging
    # coverage against THIS enumeration would read `not-run` on every real
    # night — one mail arriving during a 15-40 minute model call is enough —
    # so the gate would have reported "never ran" while still excluding on its
    # stamps. Arrivals are reported on their own line instead, which is the
    # honest shape: the gate ran, over the threads it was asked about.
    gate_state = category_gate_state(binding["honored"] if binding else categories,
                                     gate_scope, gate.get("defined_ids"))
    evidence["category_gate"] = {
        "excluded_before_draw": len(in_scope_excluded),
        "state": gate_state["state"],
        "state_why": gate_state["why"],
        "stamps_in_scope": gate_state["stamps_in_scope"],
        "categorised_in_scope": gate_state["categorised_in_scope"],
        "unstamped_in_scope": gate_state["unstamped_in_scope"],
        "undefined_ids": gate_state["undefined_ids"],
        "stamps_supplied": gate_state["stamps_supplied"],
        "categorised": gate.get("categorised", 0),
        "in_scope": len(convs),
        # REPORTED, NOT POLICED. A category pass that stamped everything
        # `never` would blind the night, and the temptation is a share
        # threshold here — but nothing has ever measured what share of this
        # mailbox a FULL pre-draw pass calls `never` (runs 126-130 stamped only
        # the ~20 rows their bodies reached), so any number would be invented.
        # The host already fails a blanket-default night on its own calibrated
        # bar (`_CATEGORY_DOMINANCE_MAX_SHARE` in `check_category_stamp`); this
        # is the number that lets the first live runs calibrate one honestly.
        "excluded_share": (round(len(in_scope_excluded) / len(convs), 4)
                           if convs else 0.0),
        # THE SNAPSHOT DELTA, ON THE REPORT. Absent these, a gate whose stamps
        # were judged on a different mailbox looks exactly like one that was
        # not — which is how this went unnoticed. `arrivals_ungated` is the
        # number that matters: threads no stamp covers, drawing freely.
        "arrivals_ungated": len(binding.get("arrivals") or ()),
        # THE INTERLOCK'S OWN BLIND SPOT, ON THE REPORT. True means the gate
        # excluded every thread it was ASKED about and the night proceeded only
        # because an arrival nothing judged filled the draw.
        "starvation_suppressed_by_arrivals": bool(starved_in_scope and not starved),
        "starvation_in_scope_why": starved_in_scope or None,
        # THE HONORED MAP ITSELF, ON THE REPORT (review 2026-08-13, round 5,
        # H4). The counts above said stamps had been dropped; the STAMPS were
        # never published, so the nightly had nothing to hand the judge except
        # the raw model answer — and the judge then re-applied the very stamps
        # this pass dropped as stale, reporting `armed` on a gate the driver
        # reported `not-run`, one run, two contradictory gate states. This is
        # the artifact the judge must read: the stamps that actually bound,
        # after the snapshot delta was resolved. `{}` when the gate did not run,
        # which is the same thing the counts say.
        "honored_stamps": dict(binding.get("honored") or {}),
        "stamps_dropped_as_stale": len(binding.get("stamps_dropped_as_stale") or ()),
        "rows_changed_since_the_stamp": len(binding.get("stale") or ()),
        "departed_since_the_stamp": len(binding.get("departed") or ()),
        "bound_to_prior_enumeration": bool(binding),
        # THE SHADOW NUMBER (item 2). What the draw would have been with the
        # gate off, beside what it was — so the first armed nights measure the
        # cost of the gate instead of an owner inventing a share threshold.
        "draw_ungated": len(ungated_draw),
        "draw_gated": len(draw),
        "opens_withheld_by_the_gate": len(ungated_draw) - len(draw),
        "never_ids": gate.get("never_ids", []),
        "taxonomy_mode": gate.get("mode"),
        "undefined_categories": gate.get("undefined_categories", {}),
        "excluded_ids_not_enumerated": len(excluded) - len(in_scope_excluded),
        "why": ("rule 1¾ excludes a `never`-category thread BEFORE its body is "
                "opened; the category is a judgment, so the caller supplies the "
                "stamps and the owner's taxonomy says which are `never`. "
                "`not-run` means every opened body was drawn without that gate "
                "and the run may have spent opens on excluded material — "
                "measured at 8 of 20 opens on runs 126, 129 and 130"),
    }
    # THE BLINDED NIGHT KEEPS ITS NUMBERS (review 2026-08-13, round 5). The
    # starvation raise used to fire BEFORE this block was built, so the one
    # night the gate blinded the mailbox — the night an operator most needs
    # `excluded_share`, `arrivals_ungated` and the taxonomy's `never` ids to
    # tell a broken taxonomy from a quiet inbox — reported none of them. The
    # stop report is `dict(_PARTIAL, stopped=…)` and `_PARTIAL` was snapshotted
    # from `evidence` before any of this existed, so the block is pushed across
    # explicitly, the same way `seed_kind` is.
    _PARTIAL["category_gate"] = evidence["category_gate"]
    if starved:
        raise DriverStop(starved)
    capture["bodies"] = capture_bodies(tab, draw, poll_seconds=poll_seconds,
                                       max_wait=max_wait,
                                       window_start=capture["window_start"])
    succeeded = sum(1 for b in capture["bodies"]
                    if b.get("ok") and int(b.get("body_chars") or 0) > 0)
    evidence["bodies_attempted"] = len(draw)
    evidence["bodies_succeeded"] = succeeded
    evidence["bodies_error"] = len(capture["bodies"]) - succeeded
    evidence["seed_kind"] = capture["enumeration"].get("seed_kind")

    enumerated_at = capture["enumeration"].get("at") or _ts(now)
    reported_at = _ts(_utcnow())
    accounting = build_accounting(
        capture, run_id=run_id,
        bundle_version=str(manifest.get("bundle_version") or ""),
        rules_version=str(manifest.get("extraction_rules_version") or ""),
        enumerated_at=enumerated_at, gate_excluded=in_scope_excluded)

    ledger = ops / f"_cos_ingestion_ledger_{run_id}.jsonl"
    write_jsonl(ledger, accounting["rows"])
    corpus = write_corpus(vault, run_id, accounting, capture)
    evidence["corpus"] = corpus

    pre, post = build_contract_inputs(capture, accounting, run_id=run_id,
                                      enumerated_at=enumerated_at,
                                      reported_at=reported_at)
    pre_path = ops / f"cos_contract_pre_{run_id}.json"
    post_path = ops / f"cos_contract_post_{run_id}.json"
    pre_path.write_text(json.dumps(pre, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    post_path.write_text(json.dumps(post, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    (ops / f"_cos_sent_baseline_{run_id}.json").write_text(
        json.dumps(pre["sent_zero_send"], indent=2) + "\n", encoding="utf-8")

    write_report(ops / f"_cos_nightly_{run_id}.md", run_id, accounting, report)

    metrics_row = {
        "date": run_id[:10], "run": run_id.rsplit("run", 1)[-1], "run_id": run_id,
        "run_ts": reported_at, "run_profile": "full",
        "mail_triaged": report["enumerated_count"],
        "inbox_count": report["enumerated_count"],
        "marked": 0, "archived": 0, "captured": 0, "drafts_created": 0,
        "held_drafted": 0, "held_non_drafted": report["enumerated_count"],
        "stopped_by_guard": 0,
        "attachment_lane": "not-exercised",
        "body_open_cap": BODY_OPEN_CAP,
        "body_open_actual": accounting["body_open_actual"],
        "body_budget": BODY_BUDGET,
        "mutation_lane": "none-read-only",
        "mutation_toolset": "chrome-plugin",
        "read_lane": READ_LANE,
        **accounting["counters"],
    }
    prior = recon._rows(ops / "_cos_metrics.jsonl")
    siblings = [r for r in prior if (r.get("date"), str(r.get("run")))
                == (metrics_row["date"], metrics_row["run"])]
    if siblings:
        metrics_row[recon.SUPERSEDES] = str(siblings[-1].get("run_ts"))
    evidence["metrics_append"] = recon.append_metric(ops, metrics_row)
    (ops / f"_cos_metrics_row_{run_id}.json").write_text(
        json.dumps(metrics_row, indent=2) + "\n", encoding="utf-8")

    code, out = run_contract(ops, run_id, pre_path, post_path,
                             ops / f"cos_contract_block_{run_id}.json")
    evidence["contract"] = {"exit_code": code, "render": out[:2000]}

    host = run_host_checks(vault, run_id)
    evidence["host_checks"] = host
    evidence["host_checks_executed"] = host.get("executed", [])

    replay = subprocess.run(
        [sys.executable,
         str(Path(__file__).resolve().parent / "cos_driver_replay_check.py"),
         "--vault", str(vault), "--run-id", run_id],
        capture_output=True, text=True, timeout=900)
    try:
        rep = json.loads(replay.stdout)
        evidence["second_process_diff"] = rep["second_process_diff"]
        evidence["determinism"] = {k: rep[k] for k in
                                   ("method", "rows_live", "rows_replayed",
                                    "excluded_fields",
                                    "enumerated_at_compared_as_one_value")}
    except (ValueError, KeyError):
        evidence["second_process_diff"] = None
        evidence["determinism"] = {"error": replay.stderr[-800:]}

    evidence["fixture_ref"] = {
        "kind": "host-only COS capture corpus (never in this repository)",
        "run_id": run_id,
        "rows": corpus["appended"],
        "schema": "brain.cos_corpus CORPUS_SCHEMA + `extraction` census facts",
        "response_digests": sorted(
            b["body_sha256"] for b in capture["bodies"] if b.get("body_sha256")),
        "why_not_here": ("the raw responses are real message bodies, classified "
                         "MNPI; `_evidence/` is inside the repository and no "
                         "retention or deletion guarantee reaches git history"),
    }
    evidence["finished_at"] = _ts(_utcnow())
    if evidence_path:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n",
                                 encoding="utf-8")
    return evidence


# ---------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------
def selfcheck() -> int:
    """The structural properties, provable with no mailbox anywhere."""
    # The SOURCE-level properties (no click dispatch, no mutation verb) live in
    # `tests/test_cos_driver.py`, not here: a scanner that reads the file it is
    # written in matches its own pattern list, and the fix for that is to put the
    # patterns in a different file rather than to weaken the scan.

    # Unread is excluded BEFORE the fetch.
    convs = [{"convId": "a", "itemId": "1", "isRead": True, "received": "2026-08-10",
              "categories": ["P1 · Today"]},
             {"convId": "b", "itemId": "2", "isRead": False, "received": "2026-08-11",
              "categories": ["P0 · Now"]},
             {"convId": "c", "itemId": "3", "isRead": True, "received": "2026-08-09",
              "categories": ["P0 · Now"]}]
    draw = body_draw(convs, 10)
    assert [d["convId"] for d in draw] == ["c", "a"], draw
    assert all(d["convId"] != "b" for d in draw), "an unread row reached the draw"

    # Completeness compares SETS, and equal counts over different ids FAIL.
    capture = {"enumeration": {"items": [{"convId": x, "received": "", "isRead": True}
                                         for x in ("a", "b", "c")],
                               "folder_total": 3, "terminated": True, "page_count": 1},
               "scan": {"ids": ["a", "b", "d"], "declared": 3, "complete": True,
                        "stagnant_scans": 3, "at_end": True}}
    rep = completeness(capture)
    assert rep["enumerated_count"] == rep["scanner_count"] == 3
    assert rep["unexplained_set_difference"] == 2, rep
    try:
        assert_complete(rep)
        raise AssertionError("equal counts over different ids must be a HARD STOP")
    except DriverStop:
        pass

    # The accounting is a pure function: same capture in, byte-identical rows out.
    cap2 = {"enumeration": {"items": convs}, "bodies": [], "draw": []}
    a1 = build_accounting(cap2, run_id="2026-01-01-run1", bundle_version="v5.61",
                          rules_version="ext-4", enumerated_at="2026-01-01T00:00:00Z")
    a2 = build_accounting(cap2, run_id="2026-01-01-run1", bundle_version="v5.61",
                          rules_version="ext-4", enumerated_at="2026-01-01T00:00:00Z")
    assert json.dumps(a1, sort_keys=True) == json.dumps(a2, sort_keys=True)
    for row in a1["rows"]:
        for slot in ("verdict", "category", "disposition", "held_reason", "dedup_check"):
            assert row[slot] is None, f"{slot} was filled by the driver"
    assert a1["counters"] == {"ingestion_in_scope": 3, "ingestion_candidates": 0,
                              "ingestion_held": 3}

    # The sheet gate refuses a vault with no stamped sheet.
    import tempfile                                              # noqa: PLC0415
    with tempfile.TemporaryDirectory() as d:
        try:
            load_sheet(Path(d))
            raise AssertionError("the driver started without a stamped sheet")
        except DriverStop as exc:
            assert "cos-run-begin" in str(exc)

    print("cos_driver selfcheck: OK")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--tab-id", type=int, default=None,
                   help="Chrome tab id of the signed-in mail tab (never an index)")
    p.add_argument("--cap", type=int, default=BODY_OPEN_CAP)
    p.add_argument("--out", type=Path, default=None, help="evidence json path")
    p.add_argument("--replay", default=None, metavar="RUN_ID",
                   help="rebuild the accounting from the corpus and print it")
    p.add_argument("--enumerated-at", default=None,
                   help="with --replay: the run's own `enumerated_at`, which is "
                        "a CAPTURED input (it is in the PRE snapshot and on "
                        "every ledger row), never a fresh clock read")
    p.add_argument("--stage", action="store_true",
                   help="stage the page-side driver into the tab's DOM and print "
                        "the one line to evaluate in its MAIN world")
    p.add_argument("--cdp", action="store_true",
                   help="drive the tab over CDP (port 9222) instead of "
                        "AppleScript — MAIN world, one browser by port")
    p.add_argument("--enumerate-only", action="store_true",
                   help="run pass 1 and STOP before the draw, writing the typed "
                        "fields to --out. This is the seam the category batch "
                        "goes in: enumerate, categorise, then re-run with "
                        "--categories to draw bodies with the `never` rows out")
    p.add_argument("--categories", type=Path, default=None,
                   help="the category batch's answer "
                        "(`[{conversation_id, category}]`). Rows whose category "
                        "the OWNER's taxonomy dispositions `never` are excluded "
                        "from the draw BEFORE any body is opened (rule 1¾)")
    p.add_argument("--enumeration", type=Path, default=None,
                   help="the `--enumerate-only` output the category answer was "
                        "asked about. REQUIRED with --categories (the body "
                        "pass re-enumerates, so the stamps are bound to the "
                        "snapshot they were judged on) and with "
                        "--validate-categories")
    p.add_argument("--validate-categories", action="store_true",
                   help="check --categories against --enumeration and exit 0 "
                        "only if it is a usable answer to THIS run's batch. "
                        "Non-zero means the nightly must run the draw ungated "
                        "rather than gate it on a file it cannot trust")
    p.add_argument("--selfcheck", action="store_true")
    args = p.parse_args(argv[1:])

    if args.selfcheck:
        return selfcheck()

    # THE ONE COMMAND WHOSE EXIT STATUS THE NIGHTLY GATES ON. Kept out of the
    # TAB preflight below on purpose: it needs no browser, and a gate that
    # cannot run without one is a gate that fails open on the paths that
    # matter. It DOES need the vault, because an answer is only valid against
    # the owner's taxonomy — and an unreadable taxonomy exits non-zero, which
    # is the ungated-draw path, never a gate armed over rules it never read.
    if args.validate_categories:
        if args.categories is None or args.enumeration is None:
            print("--validate-categories needs --categories and --enumeration",
                  file=sys.stderr)
            return 2
        cat_vault = args.vault or Path(
            os.environ.get("BRAIN_VAULT", "")).expanduser()
        try:
            from brain import cos as _cos                        # noqa: PLC0415
            defined = sorted((_cos.ingest_taxonomy(cat_vault) or {}
                              ).get("rules") or {})
        except Exception as exc:                                 # noqa: BLE001
            print(f"the owner's ingest taxonomy could not be read from "
                  f"{cat_vault}: {exc} — an answer cannot be checked against "
                  "rules this leg never saw", file=sys.stderr)
            return 2
        if not defined:
            print(f"the owner's ingest taxonomy at {cat_vault} defines no "
                  "category, so no stamp could be a valid one", file=sys.stderr)
            return 2
        try:
            rows = json.loads(args.enumeration.read_text(encoding="utf-8"))["rows"]
            ids = [r["conversation_id"] for r in rows]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"the enumeration at {args.enumeration} is unreadable: {exc}",
                  file=sys.stderr)
            return 2
        try:
            cats = load_categories(args.categories, in_scope_ids=ids)
        except (OSError, ValueError) as exc:
            print(f"the category answer is not usable: {exc}", file=sys.stderr)
            return 2
        state = category_gate_state(cats, ids, defined)
        print(json.dumps(state, indent=2))
        # AN EMPTY, PARTIAL OR UNDEFINED-ID ANSWER IS NOT AN ANSWER. `[]`
        # parses, passes `[ -s ]` at two bytes and stamps nothing; a partial
        # one gates only the part it covers; an id the owner never wrote
        # excludes nothing at all. Each used to arm the gate. The REASON goes
        # to stderr so the nightly's log names what actually happened rather
        # than reciting a fixed list of causes.
        if state["state"] != "armed":
            print(state["why"], file=sys.stderr)
            return 1
        return 0

    if args.stage:
        if args.cdp:
            tab: Any = CdpTab()
            print(tab.js(stage(tab)))
            return 0
        if args.tab_id is None:
            print("--stage needs --tab-id", file=sys.stderr)
            return 2
        print(stage(ChromeTab(args.tab_id)))
        return 0

    vault = args.vault or Path(os.environ.get("BRAIN_VAULT", "")).expanduser()
    if not vault or not vault.is_dir():
        print("--vault (or $BRAIN_VAULT) must name an existing vault", file=sys.stderr)
        return 2

    if args.replay:
        from brain import cos                                    # noqa: PLC0415
        manifest = cos.run_manifest(vault, args.replay) or {}
        rows = accounting_from_corpus(
            vault, args.replay,
            bundle_version=str(manifest.get("bundle_version") or ""),
            rules_version=str(manifest.get("extraction_rules_version") or ""),
            enumerated_at=str(args.enumerated_at or ""))
        print(json.dumps(rows, sort_keys=True, ensure_ascii=False))
        return 0

    if args.tab_id is None and not args.cdp:
        print("--tab-id is required for a live night (or --cdp)", file=sys.stderr)
        return 2

    if args.enumerate_only:
        try:
            out = enumerate_only(vault, args.tab_id, evidence_path=args.out,
                                 use_cdp=args.cdp)
        except DriverStop as exc:
            print(f"DRIVER STOP: {exc}", file=sys.stderr)
            return 3
        print(json.dumps({k: v for k, v in out.items()
                          if k not in ("rows", "completeness")}
                         | {"rows": len(out["rows"])}, indent=2))
        return 0

    # `--categories` WITHOUT `--enumeration` IS REFUSED. The stamps were judged
    # on the `--enumerate-only` snapshot and this pass re-enumerates; with no
    # file to bind them to, a stamp lands by id on a mailbox the model never
    # saw. Refused here rather than defaulted, for the same reason
    # `category_gate_state`'s `defined_categories` has no default: a caller
    # that forgets an argument must get a loud error, never a quiet degradation.
    prior_rows = None
    if args.categories is not None:
        if args.enumeration is None:
            print("--categories needs --enumeration: the stamps are bound to "
                  "the enumeration they were judged on", file=sys.stderr)
            return 2
        try:
            prior_rows = json.loads(
                args.enumeration.read_text(encoding="utf-8"))["rows"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"the enumeration at {args.enumeration} is unreadable: {exc}",
                  file=sys.stderr)
            return 2
    try:
        ev = run_night(vault, args.tab_id, cap=args.cap, evidence_path=args.out,
                       categories=(load_categories(args.categories)
                                   if args.categories else None),
                       prior_enumeration=prior_rows,
                       use_cdp=args.cdp)
    except DriverStop as exc:
        print(f"DRIVER STOP: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({k: v for k, v in ev.items()
                      if k not in ("completeness", "fixture_ref")}, indent=2))
    return 0 if ev["contract"]["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
