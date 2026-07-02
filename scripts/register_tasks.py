#!/usr/bin/env python3
"""register_tasks.py — idempotent cross-client scheduled-task registrar (CUT-04 / s07).

Reads routines/manifest.json and emits per-client registration steps for the
two task classes the persistence budget (docs/cutover/persistence-budget.md)
allows:

  HOST leg  — the ONE locked OS-scheduled task (`brain-nightly`, manifest id
              "brain-nightly", command `brain maintain --json`). Registers via
              macOS launchd or Windows Task Scheduler, reusing the existing
              installer scripts. list-then-create-or-update: never blindly
              re-registers without first checking what's there.

  COWORK leg — the manifest's vm_eligible ON-INVOKE tasks (promotion-scan,
              autoresearch-cascade, ingestion-digest-weekly's on-demand form).
              These are NEVER auto-firing: persistence-budget.md locks the
              Cowork/VM OS-scheduled-task count at exactly 0, so this leg
              never emits a cron_expression. It emits ONE paste-ready prompt
              a human pastes into a Cowork chat session; the prompt itself
              instructs the agent inside Cowork to register POKE-ONLY
              triggers (list_scheduled_tasks -> create_scheduled_task only if
              absent, else update_scheduled_task to adopt -- never
              delete_scheduled_task, retire via enabled:false).

Default mode is --dry-run: read-only on the host (a `launchctl list` /
`schtasks` probe is harmless) and the Cowork leg is ALWAYS just printed text
-- this script has no way to reach Cowork's MCP tools from the Mac host, so
"apply" for that leg is "paste the printed prompt into a Cowork chat
yourself". --apply only changes behaviour for the HOST leg (it invokes the
existing idempotent installer script).

Usage:
    python3 scripts/register_tasks.py --dry-run                  # default-safe report
    python3 scripts/register_tasks.py --apply --client host      # actually install
    python3 scripts/register_tasks.py --dry-run --client cowork  # just print the prompt
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "routines" / "manifest.json"

MAC_LABEL = "com.profile-a-brain.daily-brief"
WIN_TASK_NAME = "brain-daily-brief"


def load_manifest(path: Path) -> dict:
    with path.open() as f:
        d = json.load(f)
    locked = d.get("locked_counts", {})
    if locked.get("host_os_scheduled") != 1 or locked.get("vm_os_scheduled") != 0:
        raise SystemExit(
            "manifest locked_counts do not match persistence-budget.md THE LOCK "
            f"(host=1, vm=0) -- got {locked}. Refusing to register against an "
            "unratified budget. Amend docs/cutover/persistence-budget.md first."
        )
    return d


def find_task(manifest: dict, task_id: str) -> dict:
    for t in manifest["tasks"]:
        if t["id"] == task_id:
            return t
    raise SystemExit(f"manifest has no task id={task_id!r}")


# --------------------------------------------------------------------------
# HOST leg
# --------------------------------------------------------------------------

def host_probe_mac() -> tuple[bool, str]:
    """Return (already_registered, detail) by listing launchd, read-only."""
    if not shutil.which("launchctl"):
        return False, "launchctl not found (not on macOS, or PATH issue)"
    try:
        out = subprocess.run(
            ["launchctl", "list", MAC_LABEL],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"launchctl probe failed: {exc}"
    if out.returncode == 0:
        return True, out.stdout.strip() or "(label found, no detail)"
    return False, f"label {MAC_LABEL!r} not currently loaded"


def host_probe_windows() -> tuple[bool, str]:
    """Return (already_registered, detail) by listing Task Scheduler, read-only."""
    if not shutil.which("schtasks") and not shutil.which("powershell"):
        return False, "schtasks/powershell not found (not on Windows, or PATH issue)"
    cmd = ["schtasks", "/Query", "/TN", WIN_TASK_NAME] if shutil.which("schtasks") else [
        "powershell", "-NonInteractive", "-Command",
        f"Get-ScheduledTask -TaskName '{WIN_TASK_NAME}' -ErrorAction SilentlyContinue",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"Task Scheduler probe failed: {exc}"
    if out.returncode == 0 and out.stdout.strip():
        return True, out.stdout.strip().splitlines()[0]
    return False, f"task {WIN_TASK_NAME!r} not currently registered"


def register_host_leg(manifest: dict, *, apply: bool) -> dict:
    nightly = find_task(manifest, "brain-nightly")
    system = platform.system()  # "Darwin" | "Windows" | "Linux"
    report: dict = {
        "task_id": nightly["id"],
        "command": nightly["command"],
        "detected_os": system,
        "apply": apply,
    }

    if system == "Darwin":
        already, detail = host_probe_mac()
        report["already_registered"] = already
        report["probe_detail"] = detail
        report["action"] = "update (re-install, picks up the new `brain maintain` body)" if already else "create"
        report["install_script"] = str(REPO_ROOT / "scripts" / "install-brief-mac.sh")
        if apply:
            vault = subprocess_env_brain_vault()
            if not vault:
                report["apply_result"] = "SKIPPED — BRAIN_VAULT not set; export BRAIN_VAULT=<path> and re-run --apply"
            else:
                proc = subprocess.run(
                    ["bash", report["install_script"]],
                    cwd=REPO_ROOT, env={**_os_environ(), "BRAIN_VAULT": vault},
                    capture_output=True, text=True, timeout=60,
                )
                report["apply_result"] = {
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout.strip(),
                    "stderr": proc.stderr.strip(),
                }
        else:
            report["apply_result"] = "DRY-RUN — would run: bash " + report["install_script"]

    elif system == "Windows":
        already, detail = host_probe_windows()
        report["already_registered"] = already
        report["probe_detail"] = detail
        report["action"] = "update (Register-ScheduledTask -Force is idempotent — re-running re-points the task at the new `brain maintain` body)" if already else "create"
        report["install_script"] = str(REPO_ROOT / "scripts" / "install-brief-windows.ps1")
        report["apply_result"] = (
            "Run from an elevated-NOT-required PowerShell prompt (Windows leg cannot be "
            "driven from this Mac host): "
            f"powershell -File {report['install_script']} -VaultPath <path-to-vault>"
        )

    else:
        report["already_registered"] = None
        report["probe_detail"] = f"unsupported/undetected host OS for this leg: {system}"
        report["action"] = "n/a"
        report["apply_result"] = "SKIPPED — host leg targets macOS (launchd) or Windows (Task Scheduler) only"

    return report


def subprocess_env_brain_vault() -> str | None:
    import os
    return os.environ.get("BRAIN_VAULT")


def _os_environ() -> dict:
    import os
    return dict(os.environ)


# --------------------------------------------------------------------------
# COWORK leg — paste-ready, idempotent, POKE-ONLY (never cron — budget=0)
# --------------------------------------------------------------------------

def cowork_task_block(task: dict) -> str:
    trigger_name = f"brain-{task['id']}"
    cmd = task["command"].strip()
    return f"""### `{trigger_name}` — {task['id']}

**Manifest source:** routines/manifest.json id `{task['id']}` (disposition: {task['disposition']}, cadence: {task['cadence']})

Idempotent registration steps for this trigger:
1. Call `list_scheduled_tasks` and search for a task/trigger named `{trigger_name}`.
2. If ABSENT: call `create_scheduled_task` with name=`{trigger_name}`. Do **NOT**
   set a cron/schedule expression — leave it poke-only / fire-on-demand. The
   locked persistence budget (docs/cutover/persistence-budget.md THE LOCK) caps
   Cowork/VM OS-scheduled-task count at exactly **0**; this trigger exists only
   so the prompt below can be re-fired by name instead of retyped.
3. If PRESENT: call `update_scheduled_task` on the existing entry to adopt /
   refresh its prompt body to the block below (do not create a duplicate).
4. **Never call `delete_scheduled_task`.** To retire this trigger later, call
   `update_scheduled_task(enabled=false)` instead.
5. **#29022 caveat:** `create_scheduled_task` is sometimes not injected by the
   MCP layer (silently no-ops). After step 2, re-run `list_scheduled_tasks`
   and confirm `{trigger_name}` now appears. If it does not, fall back to the
   Cowork Schedule UI and register the same prompt body manually.

**Trigger prompt body (what `{trigger_name}` runs when manually fired):**

```
{cmd}
```

PF-02 export-egress gate (docs/cutover/export-egress-gate.md) — already satisfied
by this block: every `brain` call above carries `--max-tier Internal`; no
personal names appear (role-title only); this is a read/draft operation, not a
`brain project`-style export, so no `brain snapshot` step is required before it
— but if you extend this trigger to ship results outside the Cowork session
(an email, a doc, a paste to another tool), run `brain snapshot` first and
record the gate evidence per export-egress-gate.md Step D before doing so.
"""


def build_cowork_prompt(manifest: dict) -> str:
    vm_tasks = [t for t in manifest["tasks"] if t.get("vm_eligible")]
    blocks = "\n".join(cowork_task_block(t) for t in vm_tasks)
    return f"""# Paste-ready Cowork registrar prompt — brain on-invoke VM tasks (s07 / TSK-04)

Paste this entire block into a Cowork chat session that has the
scheduled-tasks MCP tools available (`list_scheduled_tasks`,
`create_scheduled_task`, `update_scheduled_task`). It registers
{len(vm_tasks)} POKE-ONLY triggers — none of them auto-fire on a cron, so
this paste never increases the Cowork/VM OS-scheduled-task count above the
locked **0** (docs/cutover/persistence-budget.md). They exist purely so the
analyst can re-fire a named, idempotently-registered prompt instead of
retyping it.

Before registering anything, confirm `BRAIN_VAULT` (or the projected
`brain --vault` path the Cowork sandbox uses) is set in the trigger's own
command lines, not assumed from the session environment — Cowork sessions are
ephemeral and do not inherit a persistent shell profile.

For EACH of the {len(vm_tasks)} tasks below, run the 5-step idempotent
sequence (list -> create-if-absent / update-if-present -> never delete):

{blocks}
---
**Summary you should report back after running this:** which of the
{len(vm_tasks)} triggers were CREATED vs UPDATED (adopted), and whether the
#29022 verify-after-create check passed for each, or required the Schedule UI
fallback.
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--client", choices=["host", "cowork", "all"], default="all")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: report-only, no mutation (host leg probe is read-only; Cowork leg is always just printed text)")
    mode.add_argument("--apply", action="store_true", help="HOST leg only: actually invoke the installer script. Cowork leg is unaffected (this script cannot reach Cowork's MCP tools)")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of human text")
    p.add_argument("--save-cowork-prompt", default=None, help="also write the Cowork paste-prompt to this file path")
    args = p.parse_args(argv)

    apply = bool(args.apply)  # dry-run is the default whenever --apply is absent

    manifest = load_manifest(Path(args.manifest))

    out: dict = {"manifest": args.manifest, "apply": apply, "client": args.client}

    if args.client in ("host", "all"):
        out["host"] = register_host_leg(manifest, apply=apply)

    if args.client in ("cowork", "all"):
        prompt = build_cowork_prompt(manifest)
        out["cowork"] = {
            "vm_eligible_tasks": [t["id"] for t in manifest["tasks"] if t.get("vm_eligible")],
            "prompt": prompt,
        }
        if args.save_cowork_prompt:
            dest = Path(args.save_cowork_prompt)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(prompt)
            out["cowork"]["saved_to"] = str(dest)

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"=== register_tasks.py — mode={'APPLY' if apply else 'DRY-RUN'} client={args.client} ===\n")
        if "host" in out:
            h = out["host"]
            print(f"-- HOST leg ({h['detected_os']}) --")
            print(f"  task: {h['task_id']}  command: {h['command']}")
            print(f"  already_registered: {h.get('already_registered')}  ({h.get('probe_detail')})")
            print(f"  action: {h.get('action')}")
            print(f"  apply_result: {h.get('apply_result')}")
            print()
        if "cowork" in out:
            c = out["cowork"]
            print(f"-- COWORK leg — {len(c['vm_eligible_tasks'])} on-invoke task(s): {', '.join(c['vm_eligible_tasks'])} --")
            print(c["prompt"])
            if c.get("saved_to"):
                print(f"(prompt also saved to {c['saved_to']})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
