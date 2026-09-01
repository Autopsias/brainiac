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
from typing import Any, NamedTuple

HOOK_SCRIPT = "brainiac-alerts.sh"
HOOK_EVENT = "SessionStart"
HOOK_ENTRY = {
    "type": "command",
    "command": "",   # filled in per install — see `hook_command`
    "timeout": 10,
    "statusMessage": "Checking Brainiac health",
}

GUARD_SCRIPT = "brainiac-egress-guard.sh"
GUARD_EVENT = "PreToolUse"
#: Which tool calls the SEC-07 guard inspects. Claude Code matches a tool by
#: name, and an MCP tool's name is `mcp__<server>__<tool>` — an owner with an
#: MCP search tool adds it here, in their own settings, rather than us guessing
#: server names we cannot know.
GUARD_MATCHER = "WebSearch|WebFetch"
GUARD_ENTRY = {
    "type": "command",
    "command": "",
    "timeout": 5,
}


class HookSpec(NamedTuple):
    """One placeable hook. Two ship today; the shape is the point.

    ``brainiac-egress-guard.sh`` was added on 2026-09-01 and reproduced this
    module's own founding defect for a day: it rode the wheel and NO install
    path placed it, which is exactly the "a hard wiring nothing installs is a
    soft one" failure the docstring above describes. A second hook could not be
    added without generalising, so it was generalised.
    """

    script: str
    event: str
    entry: dict[str, Any]
    matcher: str | None
    label: str
    #: What the OWNER loses when this hook is silently absent. The row exists
    #: to name that, not to report a missing file: "script not found" is not a
    #: consequence anyone can weigh.
    silent_symptom: str


HOOKS: tuple[HookSpec, ...] = (
    HookSpec(HOOK_SCRIPT, HOOK_EVENT, HOOK_ENTRY, None,
             "SessionStart alert hook",
             "sessions open with NO degradation banner, which reads exactly "
             "like a healthy vault"),
    HookSpec(GUARD_SCRIPT, GUARD_EVENT, GUARD_ENTRY, GUARD_MATCHER,
             "PreToolUse egress guard (SEC-07)",
             "a web search carrying a term this vault classifies Confidential "
             "or above leaves unchecked, and nothing records that it did"),
)


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


def hook_command(claude_home: Path, script: str = HOOK_SCRIPT) -> str:
    """The command string to register, `~`-relative when it is under $HOME.

    Derived from the ACTUAL destination rather than hardcoded: a test home or
    a `--claude-home` override would otherwise register a path pointing at
    `~/.claude`, where the script was never placed."""
    path = (claude_home / "hooks" / script).expanduser()
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _already_registered(groups: list[Any], script: str = HOOK_SCRIPT) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks") or []:
            if isinstance(entry, dict) and script in str(entry.get("command", "")):
                return True
    return False


def _group_for(groups: list[Any], matcher: str | None) -> dict[str, Any]:
    """The group this spec's entry belongs in, created if absent.

    A matcher-less spec joins the first matcher-less group rather than adding a
    second one — that is what SessionStart already uses, and two of them run
    the same set twice. A MATCHED spec joins the group carrying its own exact
    matcher, and never a matcher-less one: appending a `PreToolUse` entry to a
    group with no matcher would run it before EVERY tool call, which is a
    different and much larger promise than the one this guard makes.
    """
    for group in groups:
        if isinstance(group, dict) and (group.get("matcher") or None) == matcher:
            return group
    group: dict[str, Any] = {"hooks": []} if matcher is None else {
        "matcher": matcher, "hooks": []}
    groups.append(group)
    return group


def _register(settings_path: Path, command: str, spec: HookSpec) -> str:
    """Add this spec's entry unless one is already there.

    Returns ``"added"``, ``"already-registered"``, or ``"unparseable"``."""
    settings: Any = {}
    if settings_path.exists():
        settings = _read_settings(settings_path)
        if not isinstance(settings, dict):
            return "unparseable"

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return "unparseable"
    groups = hooks.setdefault(spec.event, [])
    if not isinstance(groups, list):
        return "unparseable"
    if _already_registered(groups, spec.script):
        return "already-registered"

    group = _group_for(groups, spec.matcher)
    group.setdefault("hooks", []).append({**spec.entry, "command": command})

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(settings_path, settings)
    return "added"


def install_all(
    claude_home: Path, resolve: Any, specs: tuple[HookSpec, ...] = HOOKS,
) -> list[dict[str, Any]]:
    """Place and register EVERY shipped hook. Idempotent, and independent.

    ``resolve(script_name)`` returns the packaged source path, or ``None``.
    One spec failing never stops another: a missing egress-guard script must
    not cost the owner their degradation banner, and the reverse is just as
    true.
    """
    return [install(claude_home, resolve(spec.script), spec) for spec in specs]


def install(
    claude_home: Path, script_src: Path | None, spec: HookSpec = HOOKS[0],
) -> dict[str, Any]:
    """Place one hook script and register it. Idempotent.

    ``script_src`` is the packaged thin caller; ``None`` (nothing resolved it)
    is reported rather than treated as success — a registered hook pointing at
    a file that is not there fires an error banner every session."""
    result: dict[str, Any] = {
        "hook": spec.label,
        "event": spec.event,
        "symptom": spec.silent_symptom,
        "hook_path": str(claude_home / "hooks" / spec.script),
        "settings_path": str(claude_home / "settings.json"),
    }
    if script_src is None or not Path(script_src).is_file():
        result["script"] = "missing"
        result["settings"] = "skipped"
        result["ok"] = False
        return result

    destination = claude_home / "hooks" / spec.script
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(script_src, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result["script"] = "installed"

    if harness_managed(claude_home):
        # Placed, not wired. `check()` below reports whether the harness has
        # actually registered it, so this is visible rather than assumed.
        registered = _is_registered(claude_home, spec)
        result["settings"] = (
            "harness-managed" if registered else "harness-managed-UNREGISTERED")
        result["ok"] = registered
        return result

    result["settings"] = _register(
        claude_home / "settings.json", hook_command(claude_home, spec.script), spec)
    result["ok"] = result["settings"] != "unparseable"
    return result


def render_all(results: list[dict[str, Any]]) -> str:
    return "\n".join(render_human(r) for r in results)


def render_human(result: dict[str, Any]) -> str:
    lines = [f"{result.get('hook', 'session-start hook')}: "
             f"{result['script']} -> {result['hook_path']}"]
    lines.append(f"registration: {result['settings']} ({result['settings_path']})")
    if result["script"] == "missing":
        lines.append("  ! the packaged hook script could not be resolved — nothing "
                     "was registered, so no session would have found it")
    if result["settings"] == "harness-managed":
        lines.append("  the harness repo that deploys this ~/.claude owns the "
                     "settings.json entry — placed the script, wrote nothing else")
    if result["settings"] == "harness-managed-UNREGISTERED":
        lines.append("  ! this ~/.claude is harness-managed, so nothing here writes "
                     f"settings.json — and NO {result.get('event', HOOK_EVENT)} entry "
                     "is registered. Add it in the harness repo, or: "
                     f"{result.get('symptom', 'the hook never runs')}")
    if result["settings"] == "unparseable":
        lines.append("  ! settings.json could not be parsed and was left UNTOUCHED "
                     f"— add the {result.get('event', HOOK_EVENT)} entry by hand, "
                     "or fix the JSON and re-run")
    return "\n".join(lines)


def doctor_row(claude_home: Path, spec: HookSpec = HOOKS[0]) -> dict[str, Any]:
    """`brain doctor`'s row for one hook. GATING when stale, by design."""
    status, detail, remediation = check(claude_home, spec)
    return {"surface": f"{spec.label} (~/.claude)", "status": status,
            "detail": detail, "remediation": remediation, "raw": {}}


def doctor_rows(claude_home: Path) -> list[dict[str, Any]]:
    """One row per shipped hook. Both are GATING when stale for the same
    reason: each fails SILENTLY, and a silent control reads exactly like a
    working one."""
    return [doctor_row(claude_home, spec) for spec in HOOKS]


def check(claude_home: Path, spec: HookSpec = HOOKS[0]) -> tuple[str, str, str | None]:
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
    script = claude_home / "hooks" / spec.script
    registered = _is_registered(claude_home, spec)
    if script.is_file() and registered:
        return ("current", f"{spec.script} placed and registered in settings.json", None)
    missing = []
    if not script.is_file():
        missing.append(f"{script} is MISSING")
    if not registered:
        missing.append(f"no {spec.event} entry in settings.json")
    fix = ("brain install-hook places the script; the harness repo that "
           "deploys this ~/.claude owns the settings.json entry"
           if harness_managed(claude_home) else "brain install-hook")
    return ("stale", "; ".join(missing) + " — " + spec.silent_symptom, fix)


def _is_registered(claude_home: Path, spec: HookSpec = HOOKS[0]) -> bool:
    settings = _read_settings(claude_home / "settings.json")
    return isinstance(settings, dict) and _already_registered(
        _event_groups(settings, spec.event), spec.script)


def _read_settings(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _event_groups(settings: dict[str, Any], event: str = HOOK_EVENT) -> list[Any]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    return groups if isinstance(groups, list) else []
