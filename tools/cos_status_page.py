#!/usr/bin/env python3
"""The COS nightly status page — what ran, what it did, and how to reverse it.

One static HTML file, rebuilt from the artifacts the runs already write (undo
ledgers, run logs, launchd state, the kill switch). It renders FACTS RECORDED
ELSEWHERE and holds no state of its own: deleting it loses nothing.

    python3 tools/cos_status_page.py            # writes the page, prints its path
    python3 tools/cos_status_page.py --text     # the same facts, to the terminal

The page lands in `<vault>/cos-ops/` — machine output, never indexed (INT-03).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import plistlib
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

VAULT = Path(os.environ.get("BRAIN_VAULT",
                            Path.home() / "DeveloperFolder/Brainiac/vault"))
LOG_DIR = Path(os.environ.get("BRAIN_LOG_DIR", Path.home() / ".brain/logs"))
PLIST = Path.home() / "Library/LaunchAgents/com.brainiac.cos-nightly.plist"
LABEL = "com.brainiac.cos-nightly"
SHOW_RUNS = 14


def _ops() -> Path:
    from brain import cos
    return cos.run_ops_dir(VAULT)


def scheduled_target() -> dict:
    """What the INSTALLED plist actually runs — and whether it is still there.

    "Loaded" is not "will run" (review 2026-08-12). The plist renders a repo
    path into `ProgramArguments`; merge this branch and delete the worktree and
    launchd still lists the job, this page still said `ON — fires daily at
    06:30`, and nothing runs, every morning, silently. The nightly's own header
    NAMES that scenario in a comment and nothing guarded it.

    So the target is resolved from the plist and checked on disk. A plist that
    a strict parser cannot read is itself the finding — `plutil` is lenient and
    accepted a malformed one once already, which is why `plistlib` is what asks.
    """
    # EVERYTHING is inside the guard (review 2026-08-13, round 3): a plist
    # whose root is a valid non-dict (list, string) parsed fine and then
    # crashed `doc.get` — so a schema-drifted file killed the status page
    # instead of being reported by it. The interpreter (argv[0]) is checked
    # too: a job whose script exists but whose python/bash is gone is exactly
    # as dead as a missing script, and the page said ON.
    try:
        with PLIST.open("rb") as fh:
            doc = plistlib.load(fh)
        if not isinstance(doc, dict):
            return {"ok": False, "script": None,
                    "why": f"the installed plist's root is "
                           f"{type(doc).__name__}, not a dict"}
        argv = doc.get("ProgramArguments") or []
        if not (isinstance(argv, list)
                and argv and all(isinstance(a, str) for a in argv)):
            return {"ok": False, "script": None,
                    "why": f"ProgramArguments is not a list of strings: "
                           f"{argv!r}"}
        # The script is the first argument that looks like a path to a file we
        # own; `/bin/bash <script>` is the shape this plist ships, but reading
        # it positionally would break on the first flag anyone adds.
        script = next((a for a in argv if a.endswith(".sh")), None)
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        return {"ok": False, "script": None,
                "why": f"the installed plist does not parse: {exc}"}
    if not script:
        return {"ok": False, "script": None,
                "why": f"the installed plist names no .sh to run: {argv!r}"}
    # REGULAR FILES, and an EXECUTABLE interpreter (verify round, 2026-08-13):
    # `exists()` passed a directory named x.sh, and an interpreter that exists
    # but cannot execute is exactly as dead as a missing one.
    if not Path(script).is_file():
        return {"ok": False, "script": script,
                "why": f"the scheduled script is GONE or not a regular file: "
                       f"{script} — the job is loaded and nothing will run. "
                       f"Re-render it with tools/cos_ctl.sh install"}
    interp = argv[0]
    if "/" not in interp:
        # The shipped shape is ALWAYS an absolute interpreter — `cos_ctl.sh
        # install` resolves it with `command -v` and refuses otherwise,
        # precisely because launchd's PATH is not the shell's (the measured
        # 2026-08-12 failure). A bare name here is drift from that shape, and
        # this page cannot check it against launchd's PATH — so it fails
        # closed instead of certifying what it cannot see (verify round 2).
        return {"ok": False, "script": script,
                "why": f"the scheduled interpreter is a bare name ({interp!r}),"
                       f" not an absolute path — launchd resolves its own PATH,"
                       f" so this page cannot verify it. Re-render with "
                       f"tools/cos_ctl.sh install"}
    if not (Path(interp).is_file() and os.access(interp, os.X_OK)):
        return {"ok": False, "script": script,
                "why": f"the scheduled interpreter is GONE or not executable: "
                       f"{interp} — the job is loaded and nothing will run"}
    return {"ok": True, "script": script, "why": None}


def schedule_state() -> dict:
    if not PLIST.exists():
        return {"installed": False, "loaded": False, "target_ok": None,
                "line": "NOT INSTALLED — tools/cos_ctl.sh install prints the two commands"}
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    loaded = any(LABEL in ln for ln in out.splitlines())
    target = scheduled_target()
    if loaded and not target["ok"]:
        # LOUD, and it says ON nowhere: a job pointing at nothing is worse than
        # an uninstalled one, because the page used to call it healthy.
        return {"installed": True, "loaded": True, "target_ok": False,
                "target": target["script"],
                "line": f"BROKEN — loaded, but it will NOT run: {target['why']}"}
    return {"installed": True, "loaded": loaded, "target_ok": target["ok"],
            "target": target["script"],
            "line": ("ON — fires daily at 06:30" if loaded
                     else "installed but PAUSED (unloaded) — tools/cos_ctl.sh resume")}


def kill_switch_state() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cos_mutate as cm
    ks = cm.kill_switch(VAULT)
    return "ARMED (mutations allowed)" if ks["enabled"] else \
        f"OFF — mutations refused ({ks['state']})"


def _known_runs() -> list[str]:
    """Every run this host STARTED, newest first — manifests, not ledgers.

    THE PAGE USED TO BE KEYED ON THE UNDO LEDGER (review 2026-08-13, round 1,
    HIGH). A ledger only exists once a mutation was dispatched, so a run that
    enumerated, spent two model calls, opened twenty bodies and then died at
    the re-prime gate did not appear on this page AT ALL — indistinguishable
    from "the schedule never fired" and from a quiet night. Measured on the
    reference vault the morning this was found: 34 manifested runs, 6 undo
    ledgers. Runs 127-130 — including the run-130 morning this whole plan
    exists to end — were invisible.

    The MANIFEST is written by `cos-run-begin` before any leg runs, so every
    run that started has one. `cos_runverify.known_run_ids` is the existing
    enumerator; ledgers still supply what a run DID, and any ledger without a
    manifest is folded back in so nothing is ever dropped by the swap.
    """
    ops = _ops()
    ledgered = [re.match(r"_cos_undo_ledger_(.+)\.jsonl", p.name).group(1)
                for p in ops.glob("_cos_undo_ledger_*.jsonl")]
    known: list[str] = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from brain import cos_runverify as rv

        known = list(rv.known_run_ids(VAULT))
    except Exception:                                             # noqa: BLE001
        # A page that cannot read the manifests still shows what mutated.
        pass
    seen: dict[str, None] = {}
    for rid in sorted(set(known) | set(ledgered), reverse=True):
        seen[rid] = None
    return list(seen)


def _outcome(run_id: str) -> str:
    """What the host later scored this run, or the honest absence of a score."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from brain import cos

        v = cos.run_validity(VAULT, run_id) or {}
        verdict = str(v.get("verdict") or "").strip()
        return verdict or "not yet scored"
    except Exception:                                             # noqa: BLE001
        return "unknown"


def runs() -> list[dict]:
    """One row per run this host STARTED, newest first — mutating or not."""
    rows = []
    for run_id in _known_runs():
        path = _ops() / f"_cos_undo_ledger_{run_id}.jsonl"
        latest: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                latest[r.get("idempotency_key")
                       or f"{r['conversation_id']}|{r['verb']}"] = r
        counts: dict[str, int] = {}
        unfinished = 0
        manual = 0
        when = None
        # The reversal-eligibility rule is IMPORTED, never re-implemented: the
        # engine holds one notion of "may a reversal touch this row", and this
        # page only reports it (review 2026-08-13, round 3 — the manual list
        # used to exist only in command stdout, so a row needing a human had
        # no persistent home on the surface the owner actually reads).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cos_mutate as cm
        for r in latest.values():
            key = f"{r['verb']}:{r['state']}"
            counts[key] = counts.get(key, 0) + 1
            if r["state"] not in ("reconciled", "aborted-not-applied", "unknown"):
                unfinished += 1
            if (r.get("verb") in ("archive", "categorize")
                    and r.get("state") in cm.REVERSIBLE_STATES
                    and cm._reversal_eligibility(r) == cm.REVERSAL_MANUAL):
                manual += 1
            when = max(when or "", r.get("ts") or "")
        rows.append({"run_id": run_id, "when": when, "counts": counts,
                     "unfinished": unfinished, "needs_manual": manual,
                     "dispatched": bool(latest), "outcome": _outcome(run_id)})
        if len(rows) >= SHOW_RUNS:
            break
    return rows


def last_log_tail(lines: int = 14) -> list[str]:
    """Only the NARRATED lines. The log also holds full JSON dumps from the
    sub-commands, which drown the twelve lines a human actually reads."""
    logs = sorted(LOG_DIR.glob("cos-nightly-*.log"), reverse=True)
    if not logs:
        return ["(no nightly log yet — the scheduled task has not fired)"]
    told = [ln for ln in logs[0].read_text(encoding="utf-8").splitlines()
            if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", ln)]
    return told[-lines:]


def _fmt_counts(counts: dict[str, int]) -> str:
    nice = []
    for key in sorted(counts):
        verb, state = key.split(":", 1)
        nice.append(f"{counts[key]} {verb} {state}")
    return ", ".join(nice) or "nothing recorded"


def _what_it_did(r: dict) -> str:
    """A run that dispatched nothing SAYS so, rather than being absent.

    "nothing recorded" against a run with no undo ledger used to be
    unreachable — the run simply was not on the page — and that absence read
    as "the schedule never fired"."""
    if r.get("dispatched"):
        return _fmt_counts(r["counts"])
    return f"no mutation dispatched — {r.get('outcome', 'unknown')}"


def as_text() -> str:
    sched = schedule_state()
    out = [f"schedule:    {sched['line']}",
           f"kill switch: {kill_switch_state()}",
           "", "runs (newest first):"]
    for r in runs():
        flag = f"  !! {r['unfinished']} UNFINISHED" if r["unfinished"] else ""
        if r.get("needs_manual"):
            flag += (f"  · {r['needs_manual']} row(s) need MANUAL resolution "
                     f"before a reversal can touch them")
        out.append(f"  {r['run_id']}  {_what_it_did(r)}{flag}")
        # BOTH reversals, on the surface the owner actually reads at 06:30
        # (review 2026-08-12). `unchip` shipped into `cos_ctl.sh` and its own
        # `--help` and nowhere else, so after a bad night this page told the
        # owner how to put the archives back and gave no hint that the chips
        # come off at all.
        out.append(f"      undo:   tools/cos_ctl.sh undo {r['run_id']}")
        out.append(f"      unchip: tools/cos_ctl.sh unchip {r['run_id']}")
    out += ["", "last log:"] + [f"  {ln}" for ln in last_log_tail()]
    return "\n".join(out)


def as_html() -> str:
    sched = schedule_state()
    ks = kill_switch_state()
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    body = [f"""<title>COS nightly — status</title>
<style>
 body{{font:15px/1.5 -apple-system,sans-serif;max-width:900px;margin:2rem auto;
      padding:0 1rem;color:#1a1a1a;background:#fafafa}}
 h1{{font-size:1.3rem}} h2{{font-size:1.05rem;margin-top:1.6rem}}
 .card{{background:#fff;border:1px solid #ddd;border-radius:8px;
       padding:.8rem 1rem;margin:.5rem 0}}
 .on{{color:#0a7d32;font-weight:600}} .off{{color:#b00020;font-weight:600}}
 .warn{{color:#b00020;font-weight:600}}
 table{{border-collapse:collapse;width:100%}}
 td,th{{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #eee;
       vertical-align:top}}
 code{{background:#f0f0f0;padding:.1rem .35rem;border-radius:4px;font-size:.85em}}
 pre{{background:#1e1e1e;color:#d8d8d8;padding:.8rem;border-radius:8px;
     overflow-x:auto;font-size:.8em}}
 .muted{{color:#777;font-size:.85em}}
</style>
<h1>COS nightly</h1>
<p class="muted">rebuilt {escape(now)} · <code>tools/cos_ctl.sh page</code> refreshes it</p>

<div class="card"><b>Schedule:</b>
 <span class="{'on' if sched.get('loaded') else 'off'}">{escape(sched['line'])}</span><br>
<b>Kill switch:</b>
 <span class="{'on' if ks.startswith('ARMED') else 'off'}">{escape(ks)}</span></div>

<div class="card"><b>Controls</b> (from the repo root, any terminal):
<table>
<tr><td><code>tools/cos_ctl.sh status</code></td><td>these facts, in the terminal</td></tr>
<tr><td><code>tools/cos_ctl.sh dry</code></td><td>a full night that stops before applying</td></tr>
<tr><td><code>tools/cos_ctl.sh run</code></td><td>a full night, now</td></tr>
<tr><td><code>tools/cos_ctl.sh stop</code></td><td>halt the run in flight + pause the schedule</td></tr>
<tr><td><code>tools/cos_ctl.sh resume</code></td><td>re-arm after a stop</td></tr>
<tr><td><code>tools/cos_ctl.sh undo &lt;run&gt;</code></td><td>put that run's archives back</td></tr>
<tr><td><code>tools/cos_ctl.sh unchip &lt;run&gt;</code></td><td>take that run's priority chips back off</td></tr>
</table></div>

<h2>Runs</h2>
<table><tr><th>run</th><th>last activity</th><th>what it did</th><th>reverse it</th></tr>"""]
    for r in runs():
        warn = (f' <span class="warn">{r["unfinished"]} UNFINISHED</span>'
                if r["unfinished"] else "")
        body.append(
            f"<tr><td>{escape(r['run_id'])}</td>"
            f"<td>{escape((r['when'] or '')[:16].replace('T', ' '))}</td>"
            f"<td>{escape(_what_it_did(r))}{warn}</td>"
            f"<td><code>cos_ctl.sh undo {escape(r['run_id'])}</code><br>"
            f"<code>cos_ctl.sh unchip {escape(r['run_id'])}</code></td></tr>")
    body.append("</table>")
    body.append("<h2>Last log</h2><pre>"
                + escape("\n".join(last_log_tail())) + "</pre>")
    return "\n".join(body)


def main() -> int:
    if "--text" in sys.argv:
        print(as_text())
        return 0
    out = _ops() / "_cos_nightly_status.html"
    out.write_text(as_html(), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
