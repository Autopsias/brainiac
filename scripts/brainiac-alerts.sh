#!/bin/bash
# SessionStart hook: surface Brainiac health alerts in EVERY Claude Code
# session, from state files the engine already writes — no engine calls,
# no network, fast pure-file reads. Silent (no output) when all healthy.
# Owner ask 2026-07-19: failures must not hide in hot.md / launchd logs /
# ephemeral banners until someone remembers to run a health check.
exec python3 - <<'PY'
import datetime
import glob
import json
import os

home = os.path.expanduser("~")
alerts = []
today = datetime.date.today()

# 0) Auto-update marker (~/.brainiac/update-state.json) — written by the hourly
#    maintain auto-apply flow, read here file-only (no engine call, no network).
#    Skip anything older than 7d (a dead/stopped maintain must not nag forever).
try:
    st = json.load(open(os.path.join(home, ".brainiac", "update-state.json")))
    at = st.get("at")
    fresh = True
    if at:
        try:
            fresh = (today - datetime.date.fromisoformat(str(at)[:10])).days <= 7
        except ValueError:
            fresh = True
    if fresh:
        status = st.get("status")
        latest = st.get("latest") or "?"
        if status == "applied":
            alerts.append(f"Brainiac auto-updated to {latest} (if the Cowork Desktop skill store is stale, one click finishes it)")
        elif status == "failed":
            detail = st.get("detail") or "unknown step"
            alerts.append(f"Brainiac auto-update to {latest} FAILED at {detail} — run 'brain update'")
        elif status == "available":
            alerts.append(f"Brainiac update {latest} available — run 'brain update'")
except Exception:
    pass

# 1) Weekly synthesis task health (~/.brain/synthesis-state.json)
try:
    state = json.load(open(os.path.join(home, ".brain", "synthesis-state.json")))
    for vault, e in state.items():
        name = os.path.basename(os.path.dirname(vault)) or vault
        rc = e.get("rc")
        last_ok = e.get("last_success")
        if rc not in (0, None):
            alerts.append(f"weekly synthesis FAILING for {name} (rc={rc}, last success {last_ok or 'never'})")
        elif last_ok:
            age = (today - datetime.date.fromisoformat(last_ok)).days
            if age > 8:
                alerts.append(f"weekly synthesis STALE for {name} (last success {last_ok}, {age}d ago)")
except Exception:
    pass

# 2) Per-vault: engine-feedback backlog, owner inbox, recent degradation
#    notifications (the notify-sent markers `brain maintain` already writes)
try:
    reg = json.load(open(os.path.join(home, ".brainiac", "workspaces.json")))
    seen = set()
    for entry in reg.get("entries", []):
        vault = entry.get("vault_path", "")
        if entry.get("target") != "host" or not vault or vault in seen:
            continue
        seen.add(vault)
        name = os.path.basename(os.path.dirname(vault)) or vault
        fb = glob.glob(os.path.join(vault, ".brain", "engine-feedback", "*.md"))
        if fb:
            alerts.append(f"{len(fb)} engine-feedback bug prompt(s) waiting for {name}")
        try:
            with open(os.path.join(vault, ".brain", "memory", "inbox.jsonl")) as f:
                pending = sum(1 for line in f
                              if line.strip() and json.loads(line).get("status", "open") == "open")
            if pending:
                alerts.append(f"{pending} owner decision(s) queued for {name} (use /brain-inbox)")
        except Exception:
            pass
        keys = set()
        for m in glob.glob(os.path.join(vault, ".brain", "notify-sent", "*.marker")):
            try:
                day = datetime.date.fromisoformat(os.path.basename(m)[:10])
                if (today - day).days <= 1:
                    keys.add(open(m).read().strip() or "unknown")
            except Exception:
                continue
        # synthesis-watchdog dupes section 1; blocked/trend keys are news
        keys.discard("synthesis-watchdog")
        if keys:
            alerts.append(f"degradation finding(s) for {name} in last 48h: {', '.join(sorted(keys))}")
except Exception:
    pass

if alerts:
    text = "BRAINIAC ALERTS: " + " | ".join(alerts)
    print(json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text + "\n(Surface these to the user; fix or delegate when asked.)",
        },
    }))
PY
