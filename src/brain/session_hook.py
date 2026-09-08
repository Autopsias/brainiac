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
* **Everywhere else, it only ever ADDS its own entry — or CORRECTS one it
  already owns.** It reads ``settings.json``, appends its command if no entry
  for its own script is there, and writes the file back whole. It never
  touches another hook's entry, never reorders one, and never touches
  ``permissions`` or any other key — widening what an agent may do is not this
  function's business, and an installer that edits permissions is
  indistinguishable from one that escalates.

  The one write it makes to an EXISTING entry is a matcher migration on its
  own script, added 2026-09-03. Matching on the script path alone made every
  upgrade a silent no-op: an install from the previous release carried the same
  path under ``matcher: "WebSearch|WebFetch"``, so ``install`` reported
  ``already-registered`` / ``ok: True`` and the widened matcher never landed —
  the feature reached no machine that already had it, and said it had. See
  :func:`_already_registered`.
* **An unreadable ``settings.json`` is refused, never replaced.** The file is
  the owner's harness configuration; overwriting a version this code failed to
  parse would destroy work to fix a banner. It reports ``settings`` as
  ``"unparseable"`` and leaves the file exactly as found.

The SCRIPT is overwritten unconditionally, and that is deliberate: it is a thin
caller into ``brain alerts``, this engine owns its contents, and overwriting is
how a host still carrying the pre-0.20.7 inline copy gets fixed.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any



# Split out at the 2026-09-04 size ratchet; re-exported so every
# `brain.session_hook.<name>` caller and monkeypatch target is unchanged.
from .session_hook_spec import (  # noqa: E402,F401  (facade re-export)
    GUARD_ENTRY as GUARD_ENTRY,
    GUARD_EVENT as GUARD_EVENT,
    GUARD_MATCHER as GUARD_MATCHER,
    GUARD_SCRIPT as GUARD_SCRIPT,
    HARNESS_MARKERS as HARNESS_MARKERS,
    HOOK_ENTRY as HOOK_ENTRY,
    HOOK_EVENT as HOOK_EVENT,
    HOOK_SCRIPT as HOOK_SCRIPT,
    HOOKS as HOOKS,
    HookSpec as HookSpec,
)
from .session_hook_settings import (  # noqa: E402,F401  (facade re-export)
    _already_registered as _already_registered,
    _dedupe as _dedupe,
    _event_groups as _event_groups,
    _group_for as _group_for,
    _matcher_ok as _matcher_ok,
    _migrate_matcher as _migrate_matcher,
    _owning_group as _owning_group,
    _owning_groups as _owning_groups,
    _owns as _owns,
    _program_path as _program_path,
    _read_settings as _read_settings,
    _register as _register,
    _registration as _registration,
    _write_json_atomic as _write_json_atomic,
    _write_settings as _write_settings,
    managed_programs as managed_programs,
)


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


def engine_path() -> str:
    """The absolute ``brain`` this process was launched as, or ``""``.

    The SEC-07 guard has to call an engine, and until 2026-09-03 it picked one
    from ``$BRAIN_BIN`` (s06 round 3, C-1). Claude Code lets COMMITTED PROJECT
    SETTINGS define a session's environment, so that made the whole guard
    switchable from a file in the repo: a no-op engine, or a path to nothing,
    and every outbound call exited 0 with nothing reporting it. An engine may
    not be authenticated through state the project controls.

    The installer does not have to guess — it IS the engine — so it writes this
    path down once, beside the script. A console script lives next to the
    interpreter that runs it, which is the first candidate; ``$PATH`` is the
    fallback for an install shape that does not (an editable checkout run
    through ``python -m``). ``""`` means "could not tell", and the guard then
    keeps its own pre-2026-09-03 fallbacks rather than being pinned to a lie.
    """
    beside = Path(sys.executable).resolve().parent / "brain"
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    return shutil.which("brain") or ""


def _write_engine_pin(destination: Path) -> str:
    """Write ``<script>.engine`` beside a freshly placed hook script.

    Returns what happened, for the install report. A pin that cannot be written
    is not fatal: the guard falls back exactly as it did before, and
    ``brain doctor`` reports the environment overrides either way.
    """
    pin = destination.with_suffix(".engine")
    engine = engine_path()
    if not engine:
        return "unresolved"
    try:
        pin.write_text(engine + "\n", encoding="utf-8")
    except OSError as exc:                       # pragma: no cover - rare
        return f"failed: {type(exc).__name__}"
    return "pinned"


def registry_path() -> str:
    """The absolute host workspace registry, or ``""``.

    **A LOCATION, NOT AN ANSWER — and that distinction is the whole fix**
    (s06 round 5). Round 4 pinned the resolved VAULT here, which failed twice:

    * the resolver it used, ``config.vault_root()``, reads ``$BRAIN_VAULT``
      first, and committed project settings define a session's environment.
      Measured: ``install()`` under ``BRAIN_VAULT=<attacker dir>`` wrote that
      directory into the pin and reported success. ``brain update`` re-runs the
      installer, so the poisoned pin returned on every update.
    * it pinned ONE vault per machine while the design registers many (four on
      the reference host), so the last install answered for all of them.

    ``~/.brainiac/workspaces.json`` is host-owned, already shipped, and already
    read by :mod:`brain.workspaces` and :mod:`brain.doctor_wiring`. Pinning its
    PATH lets the guard resolve the right vault per invocation, from state no
    repository can write, and asks the environment nothing.

    ``Path.home()`` deliberately, NOT ``$BRAINIAC_HOME``: an env var naming the
    registry would reintroduce exactly the bug this closes. ``$HOME`` remains
    the stated ceiling, shared with :func:`engine_path`.
    """
    return str(Path.home() / ".brainiac" / "workspaces.json")


def _write_registry_pin(destination: Path) -> str:
    """Write ``<script>.registry`` beside a freshly placed hook script.

    The pin records WHERE the registry is, so it is written whether or not that
    file exists yet — a host that registers its first vault tomorrow must not
    need a re-install. The guard treats an absent or unreadable registry, and a
    session matching no entry, as "no vault": empty ring, containment against
    the session's own tree, fail-safe.

    Any ``<script>.vault`` from the round-4 shape is REMOVED here. Leaving it
    would be a second, staler answer to a question that now has one source.
    """
    stale = destination.with_suffix(".vault")
    try:
        stale.unlink(missing_ok=True)
    except OSError:                              # pragma: no cover - rare
        pass
    pin = destination.with_suffix(".registry")
    try:
        pin.write_text(registry_path() + "\n", encoding="utf-8")
    except OSError as exc:                       # pragma: no cover - rare
        return f"failed: {type(exc).__name__}"
    return "pinned"




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


def _is_windows() -> bool:
    """One place, so a test can say "pretend this is Windows" without patching
    `os.name` globally — which perturbs `pathlib` and made the first version of
    that test report every source file as missing."""
    return os.name == "nt"


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

    # THE EGRESS GUARD DOES NOT RUN ON WINDOWS, AND INSTALLING IT THERE BREAKS
    # THE HOST (s08 round 3, Codex HIGH). The pins this installer writes are
    # NATIVE paths, so on Windows they read `C:\Users\...`; the guard accepts a
    # pin only if it starts with `/` and REFUSES otherwise, fail-closed. Its
    # matcher covers almost every useful tool, so the result is not a degraded
    # guard — it is a host where every Write, Read, Bash and web call exits 2.
    # Measured on this box by writing each pin in the Windows shape: control
    # rc=0, Windows-style engine pin rc=2, Windows-style registry pin rc=2.
    #
    # Declining is REPORTED, never silent, and `ok` is False: the security
    # control really is absent on that host, and a row that says so every
    # session is the honest state. Normalising the pins into whichever shell
    # form the host's bash wants (Git Bash `/c/...`, MSYS, WSL) is not done
    # here because it cannot be verified from a POSIX box, and an unverified
    # workaround in a fail-closed security path is worse than a stated gap.
    if spec.script == GUARD_SCRIPT and _is_windows():
        result["script"] = "skipped-unsupported-os"
        result["settings"] = "skipped-unsupported-os"
        result["symptom"] = (
            "the SEC-07 egress guard is NOT installed on Windows: its pins are "
            "native paths that the guard refuses, which would block every "
            "matched tool call. No egress classification on this host.")
        result["ok"] = False
        return result

    destination = claude_home / "hooks" / spec.script
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(script_src, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result["script"] = "installed"
    if spec.script == GUARD_SCRIPT:
        result["engine_pin"] = _write_engine_pin(destination)
        result["registry_pin"] = _write_registry_pin(destination)
    # Bound the guard's per-session state on the ordinary update cadence rather
    # than with a scheduled task of its own (s06 round 7, Claude). Imported here
    # because `guard_settings` imports this module.
    from .guard_settings import prune_session_state
    result["sessions_pruned"] = prune_session_state()

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
    if result["settings"] == "migrated":
        lines.append("  this host carried an entry from an EARLIER release, under a "
                     "narrower matcher — the tool calls added since never reached it. "
                     "The matcher was corrected; nothing else in settings.json was "
                     "touched")
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
    present, found = _registration(
        _event_groups(_read_settings(claude_home / "settings.json") or {}, spec.event),
        spec, managed_programs(claude_home, spec.script))
    if script.is_file() and present and _matcher_ok(found, spec):
        broken = _pin_faults(script, spec)
        if broken:
            # A PIN FAULT IS A TOTAL OUTBOUND LOCKOUT UNDER A GREEN ROW (s06
            # round 5). The guard REFUSES every watched tool when the engine it
            # pins is gone, and it resolves no vault at all when the registry
            # pin is missing — while every path-shaped check above reads this
            # host as healthy. The owner then meets a hook that blocks each
            # call with no diagnostic naming the cause.
            return ("stale", "; ".join(broken), "brain install-hook")
        return ("current", f"{spec.script} placed and registered in settings.json", None)
    missing = []
    if not script.is_file():
        missing.append(f"{script} is MISSING")
    if not present:
        missing.append(f"no {spec.event} entry in settings.json")
    elif not _matcher_ok(found, spec):
        # A STALE MATCHER, named. The entry is there and the file is there, so
        # every path-shaped check reads this host as healthy — while the tool
        # calls this release added never reach the hook at all.
        missing.append(
            f"the {spec.event} entry is registered under matcher {found!r}, not "
            f"{spec.matcher!r} — it is an install from an earlier release and the "
            "tool calls added since are NOT sent to it")
    # A GATING ROW THAT CANNOT GO GREEN IS A ROW THAT GETS DELETED (s06 round 2).
    # On a harness-managed `~/.claude` this engine may not write settings.json —
    # one deploy target, one writer — so a registration fault there is not
    # something `brain install-hook` or `brain update` can ever clear, and
    # `stale` gates the process exit code. `manual-required` is the status that
    # already means exactly this ("unscriptable REQUIRED surface", ADR-0005
    # Ruling 2) and does not gate. The SCRIPT going missing still gates
    # everywhere: placing it is this engine's own job.
    harness = harness_managed(claude_home)
    fix = ("brain install-hook places the script; the harness repo that "
           "deploys this ~/.claude owns the settings.json entry"
           if harness else "brain install-hook")
    status = "manual-required" if harness and script.is_file() else "stale"
    return (status, "; ".join(missing) + " — " + spec.silent_symptom, fix)


def _pin_faults(script: Path, spec: HookSpec) -> list[str]:
    """What is wrong with the guard's sidecar pins, in owner-readable words.

    Empty for every hook that carries no pins, and for a guard whose pins are
    both usable. See :func:`registry_path` for why the vault is resolved from
    a pinned LOCATION rather than the environment.
    """
    if spec.script != GUARD_SCRIPT:
        return []
    faults = []
    # An ABSENT `.engine` is not a fault: the guard then falls back to the
    # standard install path and finally to PATH, and a host with no `brain` at
    # all correctly has no opinion. A pin that is PRESENT and unusable is the
    # lockout — it refuses every watched tool and names only itself.
    engine = script.with_suffix(".engine")
    if engine.is_file():
        try:
            named = engine.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            named = ""
        if not named or not os.access(named, os.X_OK):
            faults.append(f"{engine} names {named!r}, which is not executable "
                          "— the guard REFUSES every watched tool until this "
                          "is repaired")
    # THE PIN'S CONTENT, NOT JUST ITS EXISTENCE (s06 round 6). This tested
    # `is_file()` and stopped, so a pin holding a relative path — the one shape
    # the guard REFUSES on — read `current` here. That is a total outbound
    # lockout under a green row: the precise failure this function was added to
    # prevent, reintroduced by the function itself.
    registry = script.with_suffix(".registry")
    if not registry.is_file():
        faults.append(f"{registry} is MISSING — the guard resolves NO vault, "
                      "so its decoder ring is empty and it can only report "
                      "that no term could have been caught")
        return faults
    try:
        named = registry.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        named = ""
    if not named.startswith("/"):
        faults.append(f"{registry} holds {named!r}, which is not an absolute "
                      "path — the guard REFUSES every watched tool on that")
        return faults
    # The pin naming a registry that is not there YET is deliberately NOT a
    # fault: the guard then runs against an empty ring, which is the documented
    # allow state, and a host that has never registered a workspace is ordinary.
    #
    # `$BRAINIAC_HOME` divergence is reported by `guard_settings.registry_pin_row`
    # and NOT here (s06 round 7, Codex): it is a real state — the guard reads the
    # pinned registry, so a relocated one leaves it resolving vaults nobody
    # registers any more — but it is not a broken install, and the pin may not
    # follow that variable without reopening round 5's poisoning class.
    return faults


def _is_registered(claude_home: Path, spec: HookSpec = HOOKS[0]) -> bool:
    settings = _read_settings(claude_home / "settings.json")
    return isinstance(settings, dict) and _already_registered(
        _event_groups(settings, spec.event), spec,
        managed_programs(claude_home, spec.script))

