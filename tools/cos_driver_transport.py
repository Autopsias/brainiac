"""The transport layer of `cos_driver` — Chrome/CDP tab, staging, the two capture passes

Moved verbatim out of `cos_driver` (batch-2 drain) and re-imported by it, so
every name keeps its `cos_driver` module path; the parent's night orchestration
calls these through its own globals exactly as before, so a test that
monkeypatches one on `cos_driver` still steers the callers.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

PAGE_JS = Path(__file__).resolve().parent / "cos_driver_page.js"

#: The read lane this driver elects, recorded on every corpus row. It is NOT a
#: manifest lane and never claims to be: `brain cos-run-begin` pins the
#: DEPLOYMENT surface (`codex-automation` / `cowork-desktop`), a different axis.
READ_LANE = "rest"

BODY_BUDGET_CHARS = 4000
BODY_BUDGET = "4000 extracted characters"
BODY_OPEN_CAP = 20


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


class EgoTab:
    """The same tab, addressed through the ego lite CLI instead of Chrome.

    WHY A THIRD TRANSPORT (measured 2026-08-18, live mailbox). ego lite's `js()`
    evaluates in the page's MAIN world, so a capture hook installed from HERE
    sees the app's own authorized traffic — 14 `service.svc` calls, `FindItem`
    among them, every one carrying `authorization`. That is the whole reason the
    AppleScript lane needs a browser extension plus the `#__cos_in`/`#__cos_out`
    bridge, and on this transport none of it is required. The tab is addressed by
    a stable target id inside a named task space, so a second browser cannot be
    driven by accident the way `tell application "Google Chrome"` can.

    WHAT IT DOES NOT FIX, and the measurement is in this file's history: a
    BACKGROUNDED mail tab still throttles to ~0.5 fps and reports
    `document.visibilityState === "hidden"`, exactly as Chrome does. A trivial
    page stays at full frame rate there and the mailbox does not, so the run-owned
    tab still has to be the foreground tab. `assert_ready`'s row check is the gate
    that catches it, unchanged.

    ego lite exposes no CDP port, so this shells out to `ego-browser nodejs`. Its
    per-call evaluation window is a HARD 15 s and is not configurable (the third
    `cdp()` argument is a sessionId, not a timeout) — shorter than AppleScript's
    45 s, which costs nothing here because nothing long is ever run as one
    evaluation on any transport.

    THIS TRANSPORT NEVER NAVIGATES. It attaches to a tab that already holds the
    signed-in mailbox and refuses when there is none; opening one would wipe the
    main-world capture hook and the captured envelope with it.

    The PAGE PROTOCOL is unchanged: `js()` returns the evaluated value as text,
    exactly as `ChromeTab.js` does, so every caller above is transport-blind.

    ALWAYS WRAP THE SOURCE IN AN IIFE ON THIS TRANSPORT. Measured 2026-08-18: a
    BARE top-level source containing a `function` expression as a call argument
    evaluates to `null` — no exception, no syntax error, a wrong value that
    PARSES. `JSON.stringify([1,2].map(function(x){return x*2;}))` returns
    `null`; wrapped as `(function(){...})()` it returns `[2,4]`, and an arrow
    works bare. It is the CLI wrapper mangling one shape of top-level source,
    not the JS engine — a staged page FILE is full of callbacks and runs fine,
    because it is eval'd inside the bootstrap's IIFE. Every source shipped today
    already has the safe shape; the hazard is the next one written.
    """

    #: Sentinels around a base64 payload. An evaluated value may contain
    #: newlines, and the CLI interleaves its own lines on stdout, so the value is
    #: encoded and fenced rather than read off the tail of the output.
    _OPEN, _CLOSE = "<<<COS:", ":COS>>>"

    def __init__(self, space: str = "cos", match: str = "outlook",
                 timeout: int = 60, tries: int = 3) -> None:
        self.space = space
        self.match = match
        self.timeout = timeout
        self.tries = tries
        self.tab_id = f"ego:{space}"

    def _script(self, source: str) -> str:
        """One heredoc: take the space, select the mail tab, evaluate, fence."""
        space, match = json.dumps(self.space), json.dumps(self.match)
        return (
            f"let sp;\n"
            f"try {{ sp = await takeOverTaskSpace({space}); }}\n"
            f"catch (e) {{ sp = await useOrCreateTaskSpace({space}); }}\n"
            f"const tabs = await listTabs();\n"
            f"const t = tabs.find(x => String(x.url || '').includes({match}));\n"
            f"if (!t) throw new Error('no tab matching ' + {match} + ' in task "
            f"space ' + {space} + ' — this transport attaches to a signed-in "
            f"mail tab and never opens one');\n"
            f"await switchTab(t.targetId);\n"
            f"const v = await js({json.dumps(source)});\n"
            f"cliLog({json.dumps(self._OPEN)} + "
            f"Buffer.from(String(v), 'utf8').toString('base64') + "
            f"{json.dumps(self._CLOSE)});\n"
        )

    def js(self, source: str) -> str:
        last = ""
        for i in range(self.tries):
            try:
                p = subprocess.run(["ego-browser", "nodejs"],
                                   input=self._script(source), capture_output=True,
                                   text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                last = f"ego-browser did not return within {self.timeout}s"
            except FileNotFoundError:
                raise DriverStop(
                    "`ego-browser` is not on PATH. It ships with the ego lite "
                    "app and is registered by its onboarding, usually into "
                    "~/.local/bin.") from None
            else:
                # `cliLog` writes to STDERR, and the CLI puts its own diagnostics
                # on both streams — measured 2026-08-18, when scanning stdout
                # alone reported a successful evaluation as a failure and handed
                # back the fenced value as the error text. The fence is what
                # distinguishes a value from noise, so both streams are scanned.
                out = f"{p.stdout}\n{p.stderr}"
                if self._OPEN in out and self._CLOSE in out:
                    blob = out.split(self._OPEN, 1)[1].split(self._CLOSE, 1)[0]
                    return base64.b64decode(blob).decode("utf-8")
                last = (p.stderr.strip() or p.stdout.strip()
                        or f"exit {p.returncode}, no output")[:300]
            time.sleep(0.8 * (i + 1))
        raise DriverStop(f"ego-browser evaluate failed after {self.tries} "
                         f"tries: {last}")

    def json(self, source: str) -> Any:
        raw = self.js(source)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise DriverStop(f"tab returned non-JSON ({exc}): {raw[:160]!r}") from None


def open_tab(tab_id: int | None, *, use_cdp: bool = False,
             use_ego: bool = False) -> tuple[Any, str]:
    """Resolve the run's transport, and stage the page half where it can.

    CDP and ego lite both reach the MAIN world themselves, so on those the driver
    stages AND boots its own page half — no second surface, nothing to do by
    hand. AppleScript cannot, which is the whole reason `--stage` exists.

    Returns the tab and the name that goes on the evidence row, so the record of
    which transport ran a night is produced at the one place that decides it.
    """
    if use_ego:
        tab: Any = EgoTab()
        tab.js(stage(tab))
        return tab, "ego"
    if use_cdp:
        tab = CdpTab()
        tab.js(stage(tab))
        return tab, "cdp"
    return ChromeTab(tab_id), "applescript"                      # type: ignore[arg-type]


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



#: Whatever the run had established when it stopped. Module-level so the stop
#: path can report a partial night instead of an empty file.
_PARTIAL: dict[str, Any] = {}


#: Whatever the run had established when it stopped. Module-level so the stop
#: path can report a partial night instead of an empty file.
_PARTIAL: dict[str, Any] = {}
