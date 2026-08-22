"""The Chrome bridge layer of `cos_mutate` — the REST-speaking tab, staging, capture verification

Moved verbatim out of `cos_mutate` (batch-2 drain); every name is re-imported
by the parent so its `cos_mutate` module path is unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

IN_ID = "__cos_min"
OUT_ID = "__cos_mout"
SRC_ID = "__cos_msrc"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import time                                                   # noqa: E402
import cos_driver as drv                                     # noqa: E402
from cos_mutate_gates import MutationStop, _ts, short  # noqa: E402
from cos_mutate_policy import _OPS_MODE  # noqa: E402

HERE = Path(__file__).resolve().parent
PAGE_JS = HERE / "cos_mutate_page.js"
HOOK_JS = HERE / "cos_capture_hook.js"


class Bridge:
    """Drive `cos_mutate_page.js` through the two hidden DOM nodes.

    Same transport discipline as the read driver — chunked writes, base64 reads,
    length-checked — because the same three silent transport bugs apply (a
    mangled non-ASCII eval, Trusted Types voiding a `<script>` write, and a
    surrogate pair split by a fixed-width slice).
    """

    def __init__(self, tab: drv.ChromeTab, *, poll_seconds: float = 1.5,
                 max_wait: float = 180.0) -> None:
        self.tab = tab
        self.seq = 0
        self.poll_seconds = poll_seconds
        self.max_wait = max_wait

    def stage(self) -> str:
        return drv.stage(self.tab, PAGE_JS, SRC_ID)

    def call(self, op: str, args: dict[str, Any] | None = None,
             max_wait: float | None = None) -> dict[str, Any]:
        self.seq += 1
        payload = json.dumps({"seq": self.seq, "op": op, "args": args or {}},
                             ensure_ascii=False)
        self.tab.js(drv._fresh_node(IN_ID))
        for off in range(0, len(payload), drv.CHUNK):
            self.tab.js(
                f"(function(){{document.getElementById({json.dumps(IN_ID)})"
                f".textContent+={json.dumps(payload[off:off + drv.CHUNK])};"
                f"return 'chunk';}})()")
        deadline = time.time() + (max_wait or self.max_wait)
        while time.time() < deadline:
            time.sleep(self.poll_seconds)
            st = self.tab.json(
                f"(function(){{var e=document.getElementById({json.dumps(OUT_ID)});"
                "var s=e?JSON.parse(e.textContent):{};"
                "return JSON.stringify({done:!!s.done,seq:s.seq||0,"
                "phase:s.phase||null,error:s.error||null,"
                "canary449:!!s.canary449,auth401:!!s.auth401});})()")
            if st.get("seq") == self.seq and st.get("done"):
                out = drv._read_out(self.tab, OUT_ID)
                if out.get("error"):
                    err = MutationStop(f"the in-page mutation driver failed in "
                                       f"{out.get('phase')!r}: {out['error']}")
                    err.canary449 = bool(out.get("canary449"))       # type: ignore[attr-defined]
                    err.auth401 = bool(out.get("auth401"))           # type: ignore[attr-defined]
                    err.mutation_in_flight = bool(out.get("mutation_in_flight"))  # type: ignore[attr-defined]
                    err.runtime = out.get("runtime")                 # type: ignore[attr-defined]
                    raise err
                return out
        raise MutationStop(f"the in-page mutation driver did not finish {op!r} "
                           f"within {max_wait or self.max_wait:.0f}s")


#: Where the staged hook source and its mirrored `stats()` live. Two more inert
#: divs on the same two-world bridge the driver already uses.
HOOK_SRC_ID = "__cos_hook_src"
HOOK_STAT_ID = "__cos_hookstat"

#: What a hook installed in the HOST's own world looks like from the host: it
#: reports `installed`, it answers `stats()`, and it captures nothing but its own
#: traffic. Measured 2026-08-10 on the live tab — `has_cosCap: true`,
#: `page_globals_seen: []`. So the host asserts its own BLINDNESS: if this
#: process can see `window.__cosCap`, the hook is in the isolated world.
WRONG_WORLD = (
    "the capture hook is in the host's ISOLATED world, not the page's MAIN "
    "world — `ChromeTab.js` (osascript) evaluates there, and a hook installed "
    "that way reports itself installed, answers stats(), and captures NOTHING "
    "of the app's traffic. Evaluate the staged bootstrap line through a "
    "MAIN-world surface (the browser extension) instead.")


def stage_hook(tab_id: int) -> dict[str, Any]:
    """Stage `cos_capture_hook.js` and return the ONE line to evaluate in MAIN.

    It deliberately does NOT evaluate it: this process cannot reach the page's
    world, and an install it performs itself is the silent failure above. The
    returned line also mirrors `stats()` into `#__cos_hookstat`, which is how
    the host reads a buffer it can never see directly.
    """
    tab = drv.ChromeTab(tab_id)
    drv.stage(tab, HOOK_JS, HOOK_SRC_ID)
    tab.js(drv._fresh_node(HOOK_STAT_ID))
    line = (f"(function(){{var r={drv.bootstrap_for(HOOK_SRC_ID)};"
            f"document.getElementById({json.dumps(HOOK_STAT_ID)}).textContent="
            f"JSON.stringify(window.__cosCap.stats());return r;}})()")
    return {"bootstrap": line, "source_node": HOOK_SRC_ID,
            "stats_node": HOOK_STAT_ID,
            "evaluate_this_in": "the page's MAIN world (browser extension)"}


def verify_capture_world(tab_id: int, *, require_boot: bool = True) -> dict[str, Any]:
    """Refuse to trust a capture buffer until both halves check out.

    Two-sided, because either side alone lies. The host asserting it CANNOT see
    the hook proves the world; the mirrored `stats()` proves the install caught
    BOOT rather than arriving after it.
    """
    tab = drv.ChromeTab(tab_id)
    if tab.js("String(typeof window.__cosCap)") != "undefined":
        raise MutationStop(WRONG_WORLD)
    raw = tab.js(f"(function(){{var e=document.getElementById("
                 f"{json.dumps(HOOK_STAT_ID)});return e?e.textContent:'';}})()")
    if not raw.strip():
        raise MutationStop(
            f"no hook stats at #{HOOK_STAT_ID} — the staged bootstrap line was "
            "never evaluated in the page's MAIN world. `stage_hook` prints it.")
    stats = json.loads(raw)
    # THE GATE IS THE SEED, NOT THE CLOCK. `document_start` was the gate until
    # 2026-08-11, when a hook installed at readyState `complete` captured a
    # `FindItem` with its `authorization` intact on the live tab. s03's "boot
    # only" reading held for a SETTLED tab; a tab still settling, or a list
    # gesture, can fire one later. So the timing is reported and the ENVELOPE
    # is what decides — a gate that refuses a usable seed is as wrong as one
    # that accepts a missing one.
    if require_boot and not stats.get("boot_finditem"):
        raise MutationStop(
            "the hook caught no `FindItem` envelope, so there is nothing to "
            "replay — every mutation resolves its item through that seed. "
            f"(installed at readyState {stats.get('installed_at_readystate')!r}; "
            "install at document_start, or re-load the tab with the hook "
            "staged, and let the list settle.)")
    return {"world": "main", "host_is_blind_to_the_buffer": True, **stats}


class CdpBridge:
    """The same page driver, driven over CDP instead of AppleScript.

    WHY A SECOND TRANSPORT. `Bridge` talks to the tab through `osascript`, which
    (a) evaluates in an ISOLATED world, so the page driver has to be booted by
    some other main-world surface, and (b) addresses "Google Chrome" by name —
    ambiguous the moment a second Chrome is running, which is exactly the setup
    the capture needs. CDP evaluates in the MAIN world by default and addresses
    one browser by port, so both problems disappear and the nightly can run this
    unattended. The PAGE PROTOCOL is unchanged: same two nodes, same ops, same
    validator — only the wire is different.
    """

    def __init__(self, port: int = 9222, max_wait: float = 180.0) -> None:
        self.port = port
        self.max_wait = max_wait
        self.seq = 0

    def _eval(self, expression: str) -> Any:
        import cos_cdp_capture as cdp                          # noqa: PLC0415
        return cdp.evaluate(expression, port=self.port)

    def stage(self) -> str:
        booted = self._eval(PAGE_JS.read_text(encoding="utf-8"))
        if "cos-mutate-page-loaded" not in str(booted):
            raise MutationStop(f"the page driver did not boot over CDP: {booted!r}")
        return str(booted)

    def call(self, op: str, args: dict[str, Any] | None = None,
             max_wait: float | None = None) -> dict[str, Any]:
        """One round trip, awaited INSIDE the page.

        The poll runs in the page rather than here, so a slow mutation is one
        awaited evaluate instead of a stream of transport calls — the pattern
        that wedged Chrome's evaluation bridge on run 112.
        """
        self.seq += 1
        payload = json.dumps({"seq": self.seq, "op": op, "args": args or {}},
                             ensure_ascii=False)
        wait_ms = int((max_wait or self.max_wait) * 1000)
        expr = f"""(async function(){{
          var IN={json.dumps(IN_ID)}, OUT={json.dumps(OUT_ID)};
          var e=document.getElementById(IN);
          if(!e){{e=document.createElement('div');e.hidden=true;e.id=IN;
                  document.documentElement.appendChild(e);}}
          e.textContent={json.dumps(payload)};
          var deadline=Date.now()+{wait_ms};
          while(Date.now()<deadline){{
            await new Promise(function(r){{setTimeout(r,300);}});
            var o=document.getElementById(OUT);
            if(!o) continue;
            var st;
            try {{ st=JSON.parse(o.textContent); }} catch(err) {{ continue; }}
            if(st.seq==={self.seq} && st.done) return JSON.stringify(st);
          }}
          return JSON.stringify({{timeout:true,seq:{self.seq}}});
        }})()"""
        out = json.loads(self._eval(expr))
        if out.get("timeout"):
            raise MutationStop(f"the in-page mutation driver did not finish {op!r} "
                               f"within {wait_ms / 1000:.0f}s")
        if out.get("error"):
            err = MutationStop(f"the in-page mutation driver failed in "
                               f"{out.get('phase')!r}: {out['error']}")
            err.canary449 = bool(out.get("canary449"))           # type: ignore[attr-defined]
            err.auth401 = bool(out.get("auth401"))               # type: ignore[attr-defined]
            err.mutation_in_flight = bool(out.get("mutation_in_flight"))  # type: ignore[attr-defined]
            raise err
        return out


class EgoBridge(Bridge):
    """The same page driver over the ego lite transport.

    IT IS THE OTHER TWO, HALF EACH, and neither half is a new idea. Like
    `CdpBridge` it reaches the page's MAIN world, so it stages AND boots the
    driver itself — no browser extension, no isolated-world dance, none of the
    two-surface choreography `_init_page` exists to police. Like `Bridge` it
    polls from the HOST, because ego lite's per-call evaluation window is a hard
    15 s that is not configurable, and `CdpBridge` waits for a mutation to finish
    INSIDE one awaited evaluate — up to 180 s. That call cannot exist on this
    transport, so the wait goes back on this side of the wire.

    Nothing else changes, and deliberately so: `call` is inherited unmodified, so
    the ops, the sequence discipline, the 449/401 flags and the validator are the
    ones the other two lanes already run. A second copy of that body is how two
    lanes drift apart.
    """

    def __init__(self, *, poll_seconds: float = 1.5, max_wait: float = 180.0) -> None:
        super().__init__(drv.EgoTab(), poll_seconds=poll_seconds, max_wait=max_wait)

    def stage(self) -> str:
        """Stage the source AND evaluate the bootstrap — this world can."""
        booted = self.tab.js(drv.stage(self.tab, PAGE_JS, SRC_ID))
        if "cos-mutate-page-loaded" not in str(booted):
            raise MutationStop(
                f"the page driver did not boot over ego lite: {booted!r}")
        return str(booted)


def _init_page(bridge: Bridge, shapes: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Stage the page driver, then REQUIRE that something else booted it in MAIN.

    It used to boot the driver itself with `tab.js`, which is the isolated world
    — the same silent failure as `WRONG_WORLD`, one layer up: the driver would
    load, answer the bridge, and be refused 401 on every request because the
    captured envelope lives in the other world.
    """
    bridge.stage()
    if bridge.tab.js("String(typeof window.__cosMut)") != "undefined":
        raise MutationStop(
            "the page driver is in the host's ISOLATED world. " + WRONG_WORLD)
    if bridge.tab.js(f"String(!!document.getElementById({json.dumps(OUT_ID)}))") != "true":
        raise MutationStop(
            "the page driver has not booted in the tab's MAIN world. Evaluate "
            f"this line there (browser extension), then retry:\n"
            f"  {drv.bootstrap_for(SRC_ID)}")
    return bridge.call("init", {"shapes": shapes, "signature": f"[cos:{run_id}:"})


def _bridge_for(tab_id: int | None, shapes: dict[str, Any], run_id: str,
                *, use_cdp: bool, use_ego: bool = False) -> Any:
    """ego, CDP or AppleScript, one place, so no pass can pick a different one."""
    if use_cdp or use_ego:
        bridge: Any = EgoBridge() if use_ego else CdpBridge()
        bridge.stage()
        bridge.call("init", {"shapes": shapes, "signature": f"[cos:{run_id}:"})
        return bridge
    bridge = Bridge(drv.ChromeTab(tab_id))
    _init_page(bridge, shapes, run_id)
    return bridge


def _transport_name(*, use_cdp: bool, use_ego: bool) -> str:
    """One name for the evidence row, decided where the bridge is."""
    return "ego" if use_ego else ("cdp" if use_cdp else "applescript")
