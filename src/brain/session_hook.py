"""Place and register the SessionStart alert hook in a Claude Code install.

``docs/harness-wiring.md`` calls the Claude Code channel **hard** — the harness
injects the banner, so a session cannot forget to look. That was true on
exactly one machine until 2026-08-20: the hook script rode the wheel and NO
install path ever placed it, so a new owner's only surface was the AGENTS.md
line asking the model to run ``brain alerts`` itself. A "hard" wiring nothing
installs is a soft one.

Two rules this module does not break:

* **On a harness-managed `~/.claude` it does not write settings.json AT ALL**
  (owner ruling 2026-08-20, `harness_managed` below). A deploy target has one
  writer. It places its own artifact and REPORTS the registration state.
* **Everywhere else, it only ever ADDS its own entry.** It reads ``settings.json``, appends one
  SessionStart command if no ``brainiac-alerts.sh`` entry is already there, and
  writes the file back whole. It never removes an entry, never reorders one,
  and never touches ``permissions`` or any other key — widening what an agent
  may do is not this function's business, and an installer that edits
  permissions is indistinguishable from one that escalates.
* **An unreadable ``settings.json`` is refused, never replaced.** The file is
  the owner's harness configuration; overwriting a version this code failed to
  parse would destroy work to fix a banner. It reports ``settings`` as
  ``"unparseable"`` and leaves the file exactly as found.

The SCRIPT is overwritten unconditionally, and that is deliberate: it is a thin
caller into ``brain alerts``, this engine owns its contents, and overwriting is
how a host still carrying the pre-0.20.7 inline copy gets fixed.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

HOOK_SCRIPT = "brainiac-alerts.sh"
HOOK_EVENT = "SessionStart"
HOOK_ENTRY = {
    "type": "command",
    "command": "",   # filled in per install — see `hook_command`
    "timeout": 10,
    "statusMessage": "Checking Brainiac health",
}


# A `~/.claude` that some harness repo DEPLOYS carries its own tooling. Two
# marker files are enough to recognise one, and both are cheap file reads.
HARNESS_MARKERS = ("scripts/gearbox", "scripts/deploy.pathspec")


def harness_managed(claude_home: Path) -> bool:
    """True when this ``~/.claude`` is a deploy target some harness repo owns.

    Owner ruling 2026-08-20 — "brainiac auto update should not update shit in
    gearbox": a deploy target has exactly ONE writer. The engine ships and
    updates its own artifact (the hook script); the harness declares the
    wiring (the settings.json entry); neither writes the other's file.

    Today the substring guard in `_register` already makes that write a no-op
    on such a host — but a CONDITIONAL no-op is not a guarantee. Rename the
    script, reformat the file, or drop the harness's own line and the next
    `brain update` writes a tracked, harness-class config and aborts every
    deploy until someone harvests it, months later, with no obvious cause.

    A standalone host has no such markers and keeps registering itself, or
    `install-hook` would place a script nothing ever runs."""
    return all((claude_home / marker).exists() for marker in HARNESS_MARKERS)


def hook_command(claude_home: Path) -> str:
    """The command string to register, `~`-relative when it is under $HOME.

    Derived from the ACTUAL destination rather than hardcoded: a test home or
    a `--claude-home` override would otherwise register a path pointing at
    `~/.claude`, where the script was never placed."""
    path = (claude_home / "hooks" / HOOK_SCRIPT).expanduser()
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _already_registered(groups: list[Any]) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks") or []:
            if isinstance(entry, dict) and HOOK_SCRIPT in str(entry.get("command", "")):
                return True
    return False


def _register(settings_path: Path, command: str) -> str:
    """Add the SessionStart entry unless one is already there.

    Returns ``"added"``, ``"already-registered"``, or ``"unparseable"``."""
    settings: Any = {}
    if settings_path.exists():
        settings = _read_settings(settings_path)
        if not isinstance(settings, dict):
            return "unparseable"

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return "unparseable"
    groups = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(groups, list):
        return "unparseable"
    if _already_registered(groups):
        return "already-registered"

    # Join the first group that applies to every session (no matcher) rather
    # than adding a second one — a matcher-less group is what SessionStart
    # already uses, and two of them run the same set twice.
    for group in groups:
        if isinstance(group, dict) and not group.get("matcher"):
            group.setdefault("hooks", []).append({**HOOK_ENTRY, "command": command})
            break
    else:
        groups.append({"hooks": [{**HOOK_ENTRY, "command": command}]})

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(settings_path, settings)
    return "added"


def install(claude_home: Path, script_src: Path | None) -> dict[str, Any]:
    """Place ``brainiac-alerts.sh`` and register it. Idempotent.

    ``script_src`` is the packaged thin caller; ``None`` (nothing resolved it)
    is reported rather than treated as success — a registered hook pointing at
    a file that is not there fires an error banner every session."""
    result: dict[str, Any] = {
        "hook_path": str(claude_home / "hooks" / HOOK_SCRIPT),
        "settings_path": str(claude_home / "settings.json"),
    }
    if script_src is None or not Path(script_src).is_file():
        result["script"] = "missing"
        result["settings"] = "skipped"
        result["ok"] = False
        return result

    destination = claude_home / "hooks" / HOOK_SCRIPT
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(script_src, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result["script"] = "installed"

    if harness_managed(claude_home):
        # Placed, not wired. `check()` below reports whether the harness has
        # actually registered it, so this is visible rather than assumed.
        registered = _is_registered(claude_home)
        result["settings"] = (
            "harness-managed" if registered else "harness-managed-UNREGISTERED")
        result["ok"] = registered
        return result

    result["settings"] = _register(
        claude_home / "settings.json", hook_command(claude_home))
    result["ok"] = result["settings"] != "unparseable"
    return result


def render_human(result: dict[str, Any]) -> str:
    lines = [f"session-start hook: {result['script']} -> {result['hook_path']}"]
    lines.append(f"registration: {result['settings']} ({result['settings_path']})")
    if result["script"] == "missing":
        lines.append("  ! the packaged hook script could not be resolved — nothing "
                     "was registered, so no session would have found it")
    if result["settings"] == "harness-managed":
        lines.append("  the harness repo that deploys this ~/.claude owns the "
                     "settings.json entry — placed the script, wrote nothing else")
    if result["settings"] == "harness-managed-UNREGISTERED":
        lines.append("  ! this ~/.claude is harness-managed, so nothing here writes "
                     "settings.json — and NO SessionStart entry is registered. "
                     "Add it in the harness repo, or sessions get no banner")
    if result["settings"] == "unparseable":
        lines.append("  ! settings.json could not be parsed and was left UNTOUCHED "
                     "— add the SessionStart entry by hand, or fix the JSON and "
                     "re-run")
    return "\n".join(lines)


def doctor_row(claude_home: Path) -> dict[str, Any]:
    """`brain doctor`'s row for this hook. GATING when stale, by design."""
    status, detail, remediation = check(claude_home)
    return {"surface": "SessionStart alert hook (~/.claude)", "status": status,
            "detail": detail, "remediation": remediation, "raw": {}}


def check(claude_home: Path) -> tuple[str, str, str | None]:
    """``(status, detail, remediation)`` for the ``brain doctor`` row.

    A hook that is deleted or unregistered is SILENT — the session simply
    opens with no banner, which is indistinguishable from a healthy vault.
    That is the failure mode this whole surface exists to prevent, so it needs
    a check of its own: neither `install-hook` nor `brain update` NOTICES the
    file going missing, they only re-place it when something runs them.

    A host with no `~/.claude` at all is not a broken install — it is a
    machine that does not run Claude Code — so it reports unmanaged rather
    than dragging `doctor` to DEGRADED over a harness nobody uses here."""
    if not claude_home.is_dir():
        return ("unmanaged", f"no {claude_home} on this host — Claude Code is not "
                             "installed here, so there is no hook surface", None)
    script = claude_home / "hooks" / HOOK_SCRIPT
    registered = _is_registered(claude_home)
    if script.is_file() and registered:
        return ("current", f"{HOOK_SCRIPT} placed and registered in settings.json", None)
    missing = []
    if not script.is_file():
        missing.append(f"{script} is MISSING")
    if not registered:
        missing.append("no SessionStart entry in settings.json")
    fix = ("brain install-hook places the script; the harness repo that "
           "deploys this ~/.claude owns the settings.json entry"
           if harness_managed(claude_home) else "brain install-hook")
    return ("stale", "; ".join(missing) + " — sessions open with NO degradation "
                     "banner, which reads exactly like a healthy vault", fix)


def _is_registered(claude_home: Path) -> bool:
    settings = _read_settings(claude_home / "settings.json")
    return isinstance(settings, dict) and _already_registered(
        _session_start_groups(settings))


def _read_settings(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _session_start_groups(settings: dict[str, Any]) -> list[Any]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(HOOK_EVENT)
    return groups if isinstance(groups, list) else []
