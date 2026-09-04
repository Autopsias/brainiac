"""The repo-local permission surface nothing else can enforce (M-2).

Split out of ``session_hook`` on 2026-09-03: placing a hook in the owner's
``~/.claude`` and READING the owner's repo-local ``.claude/settings.local.json``
are different jobs against different files, and keeping them in one module put
it over the file-size ratchet. Nothing re-exports from here — a re-export shim
is how one file ends up with two module names.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .session_hook import (
    GUARD_EVENT,
    GUARD_MATCHER,
    GUARD_SCRIPT,
    HOOKS,
    _event_groups,
    _read_settings,
    _registration,
    managed_programs,
    registry_path,
)

LOCAL_SETTINGS = ".claude/settings.local.json"

#: A `Bash(...)` allow entry whose WHOLE payload is an interpreter or a code
#: runner, left OPEN by a wildcard (or by `-`, "read the program from stdin").
#: Those hand the model arbitrary code execution with no prompt, which routes
#: around every PreToolUse guard the same file registers. A SCOPED entry —
#: `Bash(python3 tools/validate.py:*)` — does not match and is not the finding.
_BROAD_INTERPRETER = re.compile(
    r"^(?:[\w./~-]*/)?"
    r"(?:(?:python3?|node|deno|bun|bash|sh|zsh|ruby|perl)(?:\s+-[ce])?"
    r"|uv\s+run|uvx|npx|bunx|pnpm\s+dlx|yarn\s+dlx|pipx\s+run)$"
)

#: `env FOO=1 python3` is `python3`. Claude Code matches a Bash rule against the
#: command as written, so an env prefix is a DIFFERENT STRING and a separate
#: allow entry — and it opens exactly the same door. Stripped before classifying.
_ENV_PREFIX = re.compile(r"^(?:env\s+|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")

#: The wildcard suffixes Claude Code documents for a `Bash` rule. `cmd:*` is
#: THE canonical prefix-match form and the first cut of this detector could not
#: see it (s06 round 2, H-6): it required a SPACE before the wildcard, so
#: `Bash(python3:*)` — a wide-open host, written the documented way — scored
#: `current`. A detector that only recognises the uncommon spelling of the
#: thing it looks for reports every host clean.
_OPEN_SUFFIXES = (":*", " *", " -", "*")


def _is_broad(entry: str) -> bool:
    """True when this allow rule leaves an interpreter or runner wide open."""
    m = re.match(r"^Bash\(\s*(.*?)\s*\)$", entry)
    if not m:
        return False
    body = m.group(1)
    for suffix in _OPEN_SUFFIXES:
        if body.endswith(suffix):
            body = body[: -len(suffix)]
            break
    else:
        return False      # no wildcard at all: an exact, scoped command
    return bool(_BROAD_INTERPRETER.match(_ENV_PREFIX.sub("", body.strip()).strip()))


def broad_interpreter_allows(settings: Any) -> list[str]:
    """Every `permissions.allow` entry that is a bare interpreter wildcard."""
    if not isinstance(settings, dict):
        return []
    perms = settings.get("permissions")
    allows = perms.get("allow") if isinstance(perms, dict) else None
    if not isinstance(allows, list):
        return []
    return [e for e in allows if isinstance(e, str) and _is_broad(e)]


#: The variable that USED to switch the SEC-07 guard off. Claude Code lets
#: committed project settings define a session's environment, so it was never
#: the owner-only escape it was documented as (s06 round 2, H-5). The shipped
#: hook no longer reads it — but a host still carrying a pre-2026-09-03 copy of
#: the script DOES, and on that host the variable really does disable the guard.
#: Either way its presence is a finding, so `brain doctor` names it.
ESCAPE_VAR = "BRAINIAC_EGRESS_GUARD"

#: EVERY environment variable that changes what the SEC-07 guard decides —
#: the CLASS, not the two instances (s06 round 3, C-1).
#:
#: Round 2 closed `BRAINIAC_EGRESS_GUARD` and this row was written to watch
#: exactly that one name. Round 3 found `BRAIN_BIN`, read from the same
#: inherited environment, doing strictly more (it selects the ENGINE, so a
#: no-op binary is the whole guard off) — and this row still reported the host
#: healthy, because it was looking for the retired name. Fixing the second
#: variable and leaving the row single-purpose would have set the same trap for
#: whoever finds the third, so the row now enumerates the class and the shipped
#: hook SCRUBS all of it from the engine's environment.
#:
#: `BRAIN_VAULT` was left OUT until round 8, and is reported from a settings
#: file only — see :data:`_SETTINGS_ONLY_VARS`. It was left out on round 4's
#: reasoning that "the
#: worst a hostile value produces is the empty-ring state". That reasoning is
#: measured wrong twice over, and the guard's own header records it: the empty
#: ring ALLOWS on every non-strict tool (measured exit 0), and the same variable
#: was a write-containment base. On a host still running a pre-2026-09-03 script
#: it selects both the ring and that base — the widest of the five — so it is
#: the last one a row may leave unnamed.
#:
#: ONE VARIABLE IS LISTED BUT CONDITIONAL. `BRAIN_EGRESS_TERM_MIN_TIER` faults
#: only when its value LOOSENS the guard — see :func:`_loosens`. Tightening is
#: not an escape, and flagging an owner who asked for a stricter bar teaches
#: them to ignore the row, which is how a row that cannot go green gets deleted.
GUARD_ENV_VARS: dict[str, str] = {
    ESCAPE_VAR: "the RETIRED in-band escape. The shipped hook ignores it, but a "
                "host still carrying a pre-2026-09-03 copy of "
                "brainiac-egress-guard.sh honours it and the guard is DISABLED "
                "there",
    "BRAIN_BIN": "selects the ENGINE the guard consults. A no-op binary, or a "
                 "path to nothing, is the ENTIRE guard off. The shipped hook "
                 "ignores it and uses the pin the installer wrote beside the "
                 "script, but a pre-2026-09-03 copy reads it",
    "BRAIN_OVERLAY_DIR": "selects WHICH decoder ring is consulted. Point it at "
                         "an empty directory and the ring reads empty, which "
                         "allows on every tool but WebSearch/WebFetch",
    "BRAIN_VAULT": "selects the vault whose decoder ring is consulted, and on "
                   "a pre-2026-09-03 script the write-containment base as well. "
                   "The shipped hook resolves both from the host registry and "
                   "scrubs this from the engine's environment; an older copy "
                   "honours it, and an empty ring ALLOWS on every tool but "
                   "WebSearch/WebFetch",
    "BRAIN_EGRESS_TERM_MIN_TIER": "the refusal threshold. Raising it lets every "
                                  "term below the new bar leave; `min_tier` now "
                                  "CLAMPS it, but a pre-2026-09-03 engine does "
                                  "not",
}


#: Variables this row reports from a SETTINGS FILE only, never from the ambient
#: environment (s06 round 8). ``BRAIN_VAULT`` is how an owner is TOLD to pin
#: their vault, and the shipped hook scrubs it before the engine call, so a row
#: that faults every host where it is exported is a row nobody reads by the
#: second week. The threat this module models is a COMMITTED,
#: collaborator-editable file reaching into a security control — a repository
#: can write ``.claude/settings.local.json``; it cannot write the owner's shell.
#: On a host still running a pre-2026-09-03 script the variable selects both the
#: decoder ring and the write-containment base, which is why it is listed at all.
_SETTINGS_ONLY_VARS = frozenset({"BRAIN_VAULT"})


def _loosens(var: str, value: str) -> bool:
    """Does this value WIDEN what may leave? Tightening is never a finding."""
    if var != "BRAIN_EGRESS_TERM_MIN_TIER":
        return True
    from . import classification as cls
    from .egress_terms import DEFAULT_MIN_TIER

    raw = str(value).strip()
    return raw in cls.RANK and cls.RANK[raw] > cls.RANK[DEFAULT_MIN_TIER]


def _escape_faults(settings: Any) -> list[str]:
    """Faults for every guard-affecting variable that is set, live or declared.

    Two sources, and both matter: the EFFECTIVE environment (what this process
    actually inherited — the state a session runs in) and the `env` block of a
    settings file (what every FUTURE session will inherit, from a file a
    collaborator can edit and commit).
    """
    faults: list[str] = []
    env = settings.get("env") if isinstance(settings, dict) else None
    for var, why in GUARD_ENV_VARS.items():
        live = None if var in _SETTINGS_ONLY_VARS else os.environ.get(var)
        if live is not None and _loosens(var, live):
            faults.append(
                f"the SEC-07 guard is reported DISABLED: {var}={live!r} is set in "
                f"the effective environment — {why}. Unset it, and re-run "
                "`brain install-hook` to replace the script")
        if isinstance(env, dict) and var in env and _loosens(var, env[var]):
            faults.append(
                f"this settings file DECLARES {var}={env[var]!r} in its `env` "
                "block — a committed, collaborator-editable file reaching into a "
                f"security control for every later tool call ({why}). Remove the "
                "key")
    return faults


def _set_vars(settings: Any) -> list[str]:
    """Which guard-affecting variables are set, live or declared. Sorted."""
    env = settings.get("env") if isinstance(settings, dict) else None
    return sorted(
        v for v in GUARD_ENV_VARS
        if (v not in _SETTINGS_ONLY_VARS and v in os.environ
            and _loosens(v, os.environ[v]))
        or (isinstance(env, dict) and v in env and _loosens(v, env[v])))


def guard_settings_row(repo_root: Path) -> dict[str, Any]:
    """`brain doctor`'s row for the repo-local M-2 permission edit.

    **Why a doctor row and not a test.** `.claude/settings.local.json` is
    gitignored by the owner's global ignore, so no test, CI leg or fresh clone
    can see it — the edit silently reverts on a new machine or worktree and
    nothing says so. A pull surface the owner reads is the only mechanism
    available, and without it an acceptance review scores M-2 closed while the
    live hook still matches `WebSearch|WebFetch` and `Bash(python3 *)` is still
    allowed.

    **WARN, never gating.** The file is the OWNER's to edit — this engine may
    not write a permission file — so a red row would hold `brain update` and
    every CI leg hostage to a manual step, which is how a check gets deleted.
    """
    path = repo_root / LOCAL_SETTINGS
    fix = (f"apply the edit in _evidence/security-followup/m2-settings-edit.md "
           f"to {path} by hand — the engine never writes a permission file")
    # The retired escape is checked FIRST, and against the live environment, so
    # it is reported on a checkout that carries no local settings file at all —
    # the variable is set per session, not per repo.
    escape = _escape_faults(None)
    if not path.is_file():
        if escape:
            return {"surface": "SEC-07 guard wiring (repo-local settings)",
                    "status": "warn", "detail": "; ".join(escape),
                    "remediation": f"unset {', '.join(_set_vars(None))}",
                    "raw": {"escape_var_set": ESCAPE_VAR in os.environ,
                            "guard_env_vars_set": _set_vars(None)}}
        return {"surface": "SEC-07 guard wiring (repo-local settings)",
                "status": "unmanaged",
                "detail": f"no {path} — this checkout carries no local "
                          "permission overrides, so there is nothing to widen",
                "remediation": None, "raw": {}}
    settings = _read_settings(path)
    if settings is None:
        return {"surface": "SEC-07 guard wiring (repo-local settings)",
                "status": "warn",
                "detail": f"{path} could not be parsed — cannot tell whether the "
                          "broad interpreter allows are gone or the PreToolUse "
                          "guard is registered",
                "remediation": fix, "raw": {}}
    broad = broad_interpreter_allows(settings)
    present, found = _registration(
        _event_groups(settings, GUARD_EVENT), HOOKS[1],
        managed_programs(None, GUARD_SCRIPT))
    registered = present and found == GUARD_MATCHER
    faults = _escape_faults(settings)
    if broad:
        faults.append(f"{len(broad)} broad interpreter allow(s) still present "
                      f"({', '.join(sorted(broad))}) — each one runs arbitrary "
                      "code with no prompt, around every PreToolUse guard")
    if not present:
        faults.append(f"no {GUARD_EVENT} entry for {GUARD_SCRIPT} — the outbound "
                      "term guard does not run in this checkout")
    elif not registered:
        faults.append(f"the {GUARD_EVENT} entry for {GUARD_SCRIPT} is registered "
                      f"under matcher {found!r}, not {GUARD_MATCHER!r} — the tools "
                      "it does not name never reach the guard")
    if faults:
        return {"surface": "SEC-07 guard wiring (repo-local settings)",
                "status": "warn", "detail": "; ".join(faults),
                "remediation": fix,
                "raw": {"broad_allows": broad, "guard_registered": registered,
                        "escape_var_set": os.environ.get(ESCAPE_VAR) is not None,
                        "guard_env_vars_set": _set_vars(settings)}}
    return {"surface": "SEC-07 guard wiring (repo-local settings)",
            "status": "current",
            "detail": f"{path}: no broad interpreter allow, {GUARD_SCRIPT} "
                      f"registered on {GUARD_EVENT}",
            "remediation": None,
            "raw": {"broad_allows": [], "guard_registered": True,
                    "escape_var_set": False, "guard_env_vars_set": []}}


# How long a session's accumulated vault set is kept. A Claude Code session id
# outlives the session itself only as this one file, so the window is about
# resumed sessions, not about security: an entry older than this cannot belong
# to a conversation anyone is still holding.
SESSION_STATE_TTL_DAYS = 30


def pinned_registry() -> Path | None:
    """The registry the INSTALLED guard actually reads, from its own pin.

    Read the FILE, never recompute the path (s06 round 8). Both this module and
    :func:`registry_path` derive from ``Path.home()``, so comparing them to each
    other compares one expression with itself: a pin written under a different
    home, or edited by hand, passes every check while the guard resolves vaults
    from a registry nobody writes any more. The pin's CONTENT is the only
    statement of what the shell will do.
    """
    for program in managed_programs(None, GUARD_SCRIPT):
        try:
            named = program.with_suffix(".registry").read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if named.startswith("/"):
            return Path(named)
    return None


def session_state_dir() -> Path:
    """Where the guard accumulates one file per session id.

    Derived from the registry pin's CONTENT when there is one, because that is
    what the shell derives it from; :func:`registry_path` is the fallback for a
    host with no guard installed yet.
    """
    pinned = pinned_registry()
    return (pinned or Path(registry_path())).parent / "egress-sessions"


def prune_session_state(ttl_days: int = SESSION_STATE_TTL_DAYS) -> int:
    """Delete session files older than ``ttl_days``. Returns how many went.

    Called from `install()`, which is also what `brain update` re-runs, so the
    directory is bounded by the ordinary update cadence and needs no scheduled
    task of its own. Failure is never fatal: this is housekeeping for a control
    that already works, and an unwritable state directory is reported by the
    doctor row below rather than raised here.
    """
    directory = session_state_dir()
    cutoff = time.time() - ttl_days * 86400
    gone = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                gone += 1
        except OSError:                              # pragma: no cover - rare
            continue
    return gone


def session_state_row() -> dict[str, Any]:
    """`brain doctor`'s row for the guard's session state (s06 round 7, Claude).

    **Why it exists.** The guard writes one file per session id so that leaving
    a vault does not release the terms that vault protects. Writing it is
    deliberately best-effort — a hook that dies because it could not write a
    cache would be worse than the leak — so the accumulation degrades to
    "current vault only" with NO surface at all when the directory is
    unwritable. The guard's own header argues that every state, including the
    ones that allow, must be nameable by `brain doctor`; this state was not.

    **Never gating.** A missing directory is the normal state on a host where
    the guard has never resolved a vault, and a degraded union still checks the
    session's current vault.
    """
    directory = session_state_dir()
    surface = "SEC-07 guard session state"
    if not directory.exists():
        return {"surface": surface, "status": "unmanaged",
                "detail": f"no {directory} — the guard has not accumulated a "
                          "vault set on this host yet",
                "remediation": None, "raw": {"path": str(directory)}}
    if not os.access(directory, os.W_OK):
        return {"surface": surface, "status": "warn",
                "detail": f"{directory} is NOT WRITABLE — the guard checks only "
                          "the vault a call is made from, so a term from a vault "
                          "the session entered earlier no longer refuses",
                "remediation": f"restore write access to {directory}",
                "raw": {"path": str(directory), "writable": False}}
    try:
        count = sum(1 for e in directory.iterdir() if e.is_file())
    except OSError as exc:                           # pragma: no cover - rare
        return {"surface": surface, "status": "warn",
                "detail": f"{directory} could not be listed: {type(exc).__name__}",
                "remediation": None, "raw": {"path": str(directory)}}
    return {"surface": surface, "status": "ok",
            "detail": f"{count} session(s) recorded in {directory}, pruned after "
                      f"{SESSION_STATE_TTL_DAYS} days",
            "remediation": None,
            "raw": {"path": str(directory), "sessions": count, "writable": True}}


def registry_pin_row() -> dict[str, Any]:
    """`brain doctor`'s row for a registry the pin no longer names.

    **The state this catches** (s06 round 7, Codex). The guard resolves its
    vault from the registry PINNED beside the script, and that pin is written
    from ``$HOME`` on purpose — honouring ``$BRAINIAC_HOME`` there would let a
    committed settings block relocate the registry and have `brain update`
    re-assert the poisoned pin on every run, which is round 5's finding one
    variable over. The cost is that an engine relocated with
    ``$BRAINIAC_HOME`` keeps a guard reading the OLD registry: it resolves no
    current vault, so it protects nothing, silently.

    **Warn, never gating.** Setting the variable is a legitimate thing to do,
    and the row must not hold `brain update` or CI hostage to it. The fault is
    the DIVERGENCE, not which file exists — the ordinary post-relocation state
    has both.
    """
    surface = "SEC-07 guard registry pin"
    pinned = pinned_registry() or Path(registry_path())
    live = Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac")
                ) / "workspaces.json"
    would_write = Path(registry_path())
    diverged = [str(other) for other in (live, would_write) if other != pinned]
    if not diverged:
        return {"surface": surface, "status": "ok",
                "detail": f"the guard resolves vaults from {pinned}",
                "remediation": None, "raw": {"pinned": str(pinned)}}
    return {"surface": surface, "status": "warn",
            "detail": f"the guard is pinned to {pinned}, but this engine's "
                      f"registry is at {', '.join(diverged)} — the guard "
                      "resolves vaults from a registry this engine no longer "
                      "writes, so it may protect nothing",
            "remediation": f"re-run `brain install-hook` to re-pin, or register "
                           f"this host's workspaces in {pinned}",
            "raw": {"pinned": str(pinned), "effective": str(live),
                    "engine_writes": str(would_write)}}
