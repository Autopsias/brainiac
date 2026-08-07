#!/usr/bin/env python3
"""Render an HTML file to PNG with headless Chrome — and clean up after it.

WHY THIS EXISTS (measured, run 62, 2026-08-01). The nightly rendered the
morning brief and the decision card to PNG by improvising a shell command. An
improvised command has no timeout, no cleanup and no owner, so **four headless
Chrome instances survived their completed screenshots** — started 19:43-19:44,
PNGs written at 19:44, still alive at 20:30 — each holding its own throwaway
``--user-data-dir``.

The leak itself was cheap. What it cost was TRUTH: **AppleScript answered from
one of the orphans.** A throwaway profile has no signed-in session and default
preferences, so two separate sessions concluded — confidently, and wrongly —
that the mailbox was signed out and that Chrome's *Allow JavaScript from Apple
Events* was off. Both were false. Killing the four orphans made the owner's
real Chrome (2 windows, 13 tabs, signed-in Outlook) answer instantly.

So this module is deliberately two things, and the second one is the point:

``render``
    HTML -> PNG under a **bounded timeout**, in its **own temp profile**,
    killing the whole **process GROUP** on timeout (Chrome's helpers do not die
    just because the browser process was signalled), and removing the temp dir
    on **every** exit path — success, failure, timeout, exception, SIGTERM.

``reap``
    Clear orphan headless Chromes matching **our own signature** before a run's
    browser work, and **REPORT THE COUNT**. A silent reaper would have hidden
    the run-62 bug forever, which is the exact failure class this instrument
    exists to remove. ``render`` runs it as preflight and puts the count in its
    own JSON output, so a night that quietly leaks is visible the next night.

THE SIGNATURE IS NARROW ON PURPOSE — same bar as the automation-profile-lock
recovery in the chief-of-staff skill, which is the only other host-process
action this repo takes. A process is a target only when ALL of these hold:

  * its EXECUTABLE (``ps -o comm=``, not the command line) is a Chrome/Chromium
    binary — a wrapper shell that merely QUOTES the flags reports ``/bin/zsh``
    and is excluded structurally, which is what keeps "never an editor,
    terminal or agent session" a guarantee rather than a hope; AND
  * its command line carries ``--headless``; AND
  * it carries ``--user-data-dir=<path>`` and that path is **inside a system
    temp root** (``/tmp``, ``/private/tmp``, ``/var/folders``, ``$TMPDIR``); AND
  * it is a browser process, not a helper (no ``--type=``); AND
  * it has been alive longer than ``--min-age`` (default 300 s), which is far
    longer than any legitimate render, so a concurrently running render is
    never the thing we kill.

A process with **no** ``--user-data-dir`` can never match — that is the owner's
real Chrome (pid 1289 on this machine, running since 20 July), and it is
excluded by construction rather than by care. Neither is a profile outside the
temp roots, which is what the ``chrome-devtools`` MCP's fixed automation
profile uses. Helpers are killed only as members of a matched browser's own
profile set, never on their own account.

    cos_render_png.py render brief.html --out brief.png [--timeout 60]
    cos_render_png.py reap [--min-age 300] [--dry-run]

Exits: 0 ok · 2 render timed out · 3 no Chrome binary found · 4 Chrome ran but
produced no PNG. ``reap`` exits 0 whether or not it found anything — finding
nothing is a result, not a failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# Our own temp profiles carry this prefix so a leak of OURS is attributable.
PROFILE_PREFIX = "cos-render-"

# Alive longer than this and a headless temp-profile Chrome is an orphan, not a
# render in flight. A render is bounded at --timeout (default 60 s).
DEFAULT_MIN_AGE = 300.0

_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

_UDD_RE = re.compile(r"--user-data-dir=(\S+)")
# ps etime: [[dd-]hh:]mm:ss
_ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")


def temp_roots() -> list[str]:
    """Every directory we are willing to call "a throwaway profile lives here".

    Resolved, because macOS hands out ``/var/folders/...`` through the
    ``/private`` symlink and a string compare against the unresolved form
    silently matches nothing.
    """
    roots = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders",
             tempfile.gettempdir()]
    out = []
    for r in roots:
        for form in (r, os.path.realpath(r)):
            form = form.rstrip("/")
            if form and form not in out:
                out.append(form)
    return out


def _under_temp_root(path: str) -> bool:
    real = os.path.realpath(path).rstrip("/")
    return any(real == root or real.startswith(root + "/") for root in temp_roots())


def _parse_etime(etime: str) -> float:
    m = _ETIME_RE.match(etime.strip())
    if not m:
        return 0.0
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def classify(pid: int, etime: str, comm: str, cmd: str,
             min_age: float) -> dict | None:
    """Is this ps row an orphan headless render of ours? The whole signature.

    ``comm`` is the EXECUTABLE (``ps -o comm=``), which is not the same thing as
    the command line and is why it is here. Measured 2026-08-01: a wrapper shell
    launching an improvised render carries the whole `--headless
    --user-data-dir=/tmp/…` string in its own command line, so a command-line
    match alone reaches `/bin/zsh` — an editor, terminal or agent session is a
    HARD DENY in this repo's host-process doctrine, and "it has not happened
    yet" is not a control. ``comm`` resolves to the real binary, so a shell is
    excluded structurally rather than by luck.

    Returns the match record, or None. Kept pure and separate from the ps calls
    so every rule can be tested against synthetic rows AND against real ones.
    """
    if "chrome" not in comm.lower() and "chromium" not in comm.lower():
        return None                       # not a browser at all
    if "--type=" in cmd:
        return None                       # a helper, not a browser
    if "--headless" not in cmd:
        return None                       # not a headless render
    m = _UDD_RE.search(cmd)
    if not m:
        return None                       # NO PROFILE => the owner's Chrome. Never.
    profile = m.group(1).rstrip("/")
    if not _under_temp_root(profile):
        return None                       # a fixed profile is somebody's resource
    age = _parse_etime(etime)
    if age < min_age:
        return None                       # young enough to be a render in flight
    return {"pid": pid, "age_seconds": age, "profile": profile,
            "executable": comm, "command": cmd[:200]}


def _ps_rows() -> list[tuple[int, str, str, str]]:
    """(pid, etime, comm, command) for every process.

    Two ps calls joined on pid rather than one combined format: both ``comm``
    and ``command`` can contain spaces ("Google Chrome"), so a single row
    carrying both cannot be split unambiguously.
    """
    comms: dict[int, str] = {}
    p = subprocess.run(["ps", "-Ao", "pid=,comm="],
                       capture_output=True, text=True, timeout=30)
    for line in p.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            comms[int(parts[0])] = parts[1]

    p = subprocess.run(["ps", "-Ao", "pid=,etime=,command="],
                       capture_output=True, text=True, timeout=30)
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        rows.append((pid, parts[1], comms.get(pid, ""), parts[2]))
    return rows


def _pids_using(profile: str, rows: list[tuple[int, str, str]]) -> list[int]:
    """Every process sharing an already-matched browser's exact profile path.

    Chrome's helpers carry the same ``--user-data-dir`` but not always
    ``--headless``, and they are reparented to launchd when their browser is
    killed. They are killed as members of a matched profile's set — never
    matched on their own.
    """
    hits = []
    for pid, _etime, comm, cmd in rows:
        if "chrome" not in comm.lower() and "chromium" not in comm.lower():
            continue                     # same executable gate as the browser
        m = _UDD_RE.search(cmd)
        if m and m.group(1).rstrip("/") == profile:
            hits.append(pid)
    return hits


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _survivors(pids: list[int]) -> list[int]:
    """Which of these are still RUNNING — one ps call, zombies excluded.

    ``os.kill(pid, 0)`` succeeds against a zombie, so using it alone reports a
    process we successfully killed as a survivor whenever its parent has not
    reaped it yet. That would understate ``reaped`` — the one number this whole
    module exists to publish honestly.
    """
    if not pids:
        return []
    want = set(pids)
    out = []
    p = subprocess.run(["ps", "-Ao", "pid=,stat="],
                       capture_output=True, text=True, timeout=30)
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid in want and not parts[1].startswith("Z"):
            out.append(pid)
    return out


def reap(min_age: float = DEFAULT_MIN_AGE, dry_run: bool = False) -> dict:
    """Clear orphan headless renders. THE COUNT IS THE PRODUCT, not the kill."""
    rows = _ps_rows()
    mine = os.getpid()
    matches = [r for r in (classify(pid, etime, comm, cmd, min_age)
                           for pid, etime, comm, cmd in rows)
               if r and r["pid"] != mine]

    profiles = sorted({m["profile"] for m in matches})
    killed: list[int] = []
    if not dry_run:
        for m in matches:                       # browsers first
            _kill(m["pid"])
            killed.append(m["pid"])
        after = _ps_rows()                      # one rescan, not one per profile
        for profile in profiles:                # then anything left on that profile
            for pid in _pids_using(profile, after):
                if pid != mine and pid not in killed:
                    _kill(pid)
                    killed.append(pid)
        time.sleep(0.2)

    survivors = _survivors(killed)
    return {"orphans": len(matches), "reaped": len(killed) - len(survivors),
            "pids": killed, "survivors": survivors, "profiles": profiles,
            "min_age_seconds": min_age, "dry_run": dry_run,
            "matched": matches}


def find_chrome() -> str | None:
    env = os.environ.get("BRAIN_COS_CHROME")
    if env:
        return env if os.path.exists(env) else None
    for path in _CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    for name in ("google-chrome-stable", "google-chrome", "chromium",
                 "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def render(html_path: str, out_png: str, timeout: float = 60.0,
           width: int = 1100, height: int = 1600,
           min_age: float = DEFAULT_MIN_AGE) -> dict:
    preflight = reap(min_age=min_age)

    chrome = find_chrome()
    if not chrome:
        return {"ok": False, "reason": "no-chrome-binary", "exit": 3,
                "detail": "no Chrome/Chromium found; set $BRAIN_COS_CHROME",
                "preflight_reap": preflight}

    src = html_path if "://" in html_path else "file://" + os.path.abspath(html_path)
    out_png = os.path.abspath(out_png)
    # A stale PNG from an earlier render would make "the file appeared" a lie.
    try:
        os.unlink(out_png)
    except FileNotFoundError:
        pass
    profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
    proc = None
    timed_out = False
    produced = False
    started = time.time()
    try:
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             f"--user-data-dir={profile}",
             f"--window-size={width},{height}",
             f"--screenshot={out_png}", src],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # its own process group, so the timeout can kill Chrome AND every
            # helper it forked in one signal
            start_new_session=True)

        # WAIT ON THE ARTEFACT, NOT ON CHROME. Measured 2026-08-01 against
        # Chrome 151.0.7922.71: `--headless --screenshot` writes the PNG and
        # then KEEPS RUNNING — three headless variants, all still alive 30 s
        # after a complete 13 kB file. That is precisely how run 62 came to
        # leak four browsers "surviving their completed screenshots". A
        # renderer that waits for Chrome to exit is therefore a renderer that
        # leaks on every successful render. So: poll for the file, accept it
        # once its size stops changing, and kill the group ourselves.
        deadline = started + timeout
        stable_at = None
        while time.time() < deadline:
            if proc.poll() is not None:      # older builds do exit on their own
                produced = os.path.exists(out_png) and os.path.getsize(out_png) > 0
                break
            if os.path.exists(out_png):
                size = os.path.getsize(out_png)
                if size > 0 and size == stable_at:
                    produced = True
                    break
                stable_at = size
            time.sleep(0.2)
        else:
            timed_out = True
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(profile, ignore_errors=True)

    elapsed = round(time.time() - started, 2)
    if timed_out:
        return {"ok": False, "reason": "render-timeout", "exit": 2,
                "timeout_seconds": timeout, "elapsed_seconds": elapsed,
                "png": out_png, "profile_removed": not os.path.exists(profile),
                "preflight_reap": preflight}
    if not produced:
        return {"ok": False, "reason": "no-png-produced", "exit": 4,
                "returncode": proc.returncode if proc else None,
                "elapsed_seconds": elapsed, "png": out_png,
                "profile_removed": not os.path.exists(profile),
                "preflight_reap": preflight}
    return {"ok": True, "png": out_png, "bytes": os.path.getsize(out_png),
            "elapsed_seconds": elapsed,
            "profile_removed": not os.path.exists(profile),
            "preflight_reap": preflight}


def cmd_render(args) -> int:
    out = render(args.html, args.out, timeout=args.timeout, width=args.width,
                 height=args.height, min_age=args.min_age)
    print(json.dumps(out))
    return int(out.get("exit", 0))


def cmd_reap(args) -> int:
    print(json.dumps(reap(min_age=args.min_age, dry_run=args.dry_run)))
    return 0


def main(argv=None) -> int:
    # A SIGTERM would otherwise skip every `finally` and leak exactly the temp
    # profile this module exists to not leak.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="HTML -> PNG, bounded and cleaned up")
    r.add_argument("html")
    r.add_argument("--out", required=True)
    r.add_argument("--timeout", type=float, default=60.0)
    r.add_argument("--width", type=int, default=1100)
    r.add_argument("--height", type=int, default=1600)
    r.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE,
                   help="preflight reap floor (seconds)")
    r.set_defaults(fn=cmd_render)

    p = sub.add_parser("reap", help="clear orphan headless renders, report count")
    p.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_reap)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    # signature + timeout cleanup are covered by tests/test_cos_render_png.py
    sys.exit(main())
