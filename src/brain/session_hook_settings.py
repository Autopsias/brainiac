"""Reading and WRITING ``settings.json`` — the only place this engine edits a
harness configuration file.

Split out of ``session_hook.py`` at the 2026-09-04 size ratchet. The two rules
in ``session_hook``'s own docstring are enforced HERE: on a harness-managed
``~/.claude`` nothing in this module is called at all, and everywhere else it
only ever ADDS its own entry or CORRECTS one it already owns — never another
hook's entry, never ``permissions``, never a file it failed to parse.

``session_hook`` re-exports every name, so ``brain.session_hook.<name>`` stays
the call site and the monkeypatch target for every existing caller and test.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from .session_hook_spec import HOOK_EVENT, HOOKS, HookSpec


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _program_path(token: str) -> Path | None:
    """One command token as an ABSOLUTE program path, or ``None``.

    ``~`` and ``$VAR`` are expanded (Claude Code writes the ``~`` form itself,
    via :func:`hook_command`) and ``.``/``..`` segments are normalised, so the
    six spellings of one destination all reduce to the same string. A token
    carrying no ``/`` is a bare command name resolved through ``$PATH`` at run
    time, and a relative one is resolved against a working directory this code
    cannot know — both are AMBIGUOUS, both return ``None``, and an ambiguous
    command is never treated as ours.
    """
    text = os.path.expandvars(os.path.expanduser(token.strip()))
    if "/" not in text:
        return None
    path = Path(os.path.normpath(text))
    return path if path.is_absolute() else None


def managed_programs(claude_home: Path | None, script: str) -> set[Path]:
    """Where an install of ``script`` by THIS code can legitimately live.

    Two entries, not one: the destination for the ``claude_home`` in hand, and
    the stock ``~/.claude/hooks/`` one that every unqualified install writes —
    a ``--claude-home`` override does not make an entry pointing at the stock
    location somebody else's hook.
    """
    homes = {Path.home() / ".claude"}
    if claude_home is not None:
        homes.add(Path(claude_home))
    return {Path(os.path.normpath(os.path.expanduser(str(h / "hooks" / script))))
            for h in homes}


def _owns(command: Any, script: str, dests: set[Path] | None = None) -> bool:
    """True when ``command`` RUNS **our** ``script`` — not merely mentions it,
    and not somebody else's file that happens to share the name.

    This was a substring test (``script in command``) until 2026-09-03, and it
    corrupted an unrelated hook (s06 round 2, H-3). Given an entry whose command
    was ``wrapper.sh --log brainiac-egress-guard.sh``, listed FIRST, migration
    read that entry as ours, rewrote ITS group's matcher to the guard's wide
    one — so someone else's hook began firing on every Bash, Write, Edit and MCP
    call — and left the real guard on the old two-tool matcher, reporting
    ``migrated, ok: True``. Two failures and a success message.

    That fix compared the first token's BASENAME, which is the same defect one
    step to the left (s06 round 3, H-4): a DIFFERENT PROGRAM with the same
    filename —``/opt/somebody-else/brainiac-egress-guard.sh`` — was still read
    as ours, took the guard's wide matcher, and the real guard was never wired.
    Six spellings of the right path passed; a different path with the right
    basename was the discriminating input, and nobody ran it.

    Ownership is now the CANONICAL PROGRAM PATH — ``shlex`` for the first token
    (so quoting is parsed rather than stripped), ``~``/``$VAR`` expanded, ``.``
    and ``..`` normalised — compared against the destinations this installer
    actually writes. A bare or relative command is ambiguous and UNOWNED. The
    cost of being wrong this way round is an extra entry of our own, never an
    edit to someone else's.
    """
    text = str(command or "").strip()
    if not text:
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:          # unbalanced quotes: not something we wrote
        return False
    if not tokens:
        return False
    program = _program_path(tokens[0])
    if program is None:
        return False
    return program in (dests if dests is not None else managed_programs(None, script))


def _owning_groups(groups: list[Any], script: str,
                   dests: set[Path] | None = None) -> list[dict[str, Any]]:
    """EVERY group carrying an entry that runs ``script``, in file order.

    Plural on purpose: the singular version stopped at the first match, so a
    decoy entry ahead of the real one hid it completely (H-3). A settings file
    may also legitimately carry our entry under two matchers after a hand edit;
    both have to be seen for either to be corrected.
    """
    out: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        if any(_owns(e.get("command"), script, dests)
               for e in (group.get("hooks") or []) if isinstance(e, dict)):
            out.append(group)
    return out


def _owning_group(groups: list[Any], script: str,
                  dests: set[Path] | None = None) -> dict[str, Any] | None:
    """The FIRST group carrying an entry that runs ``script``, or ``None``."""
    found = _owning_groups(groups, script, dests)
    return found[0] if found else None


def _matcher_ok(found: Any, spec: HookSpec) -> bool:
    """Is a group carrying our entry under an ACCEPTABLE matcher?

    A spec with ``matcher=None`` does not own the matcher — it asks to run, and
    where the owner has scoped it (``SessionStart`` takes ``startup|resume|…``)
    that scoping is the owner's call. Comparing ``None`` for equality treated
    such a group as stale and STRIPPED the owner's matcher, silently widening
    when their banner fires. A matched spec still requires its exact matcher:
    for the egress guard the matcher IS the set of doors it watches.
    """
    return True if spec.matcher is None else found == spec.matcher


def _registration(groups: list[Any], spec: HookSpec,
                  dests: set[Path] | None = None) -> tuple[bool, Any]:
    """``(present, matcher)`` for this spec's entry. ``matcher`` is ``None``
    for a matcher-less group and meaningless when ``present`` is False.

    With SEVERAL owning groups, a correctly-matched one wins — the hook does
    run on the doors it names — and otherwise the first is reported, so the row
    names a real stale matcher rather than an invented one.
    """
    found = _owning_groups(groups, spec.script, dests)
    if not found:
        return (False, None)
    for group in found:
        if _matcher_ok(group.get("matcher") or None, spec):
            return (True, group.get("matcher") or None)
    return (True, found[0].get("matcher") or None)


def _already_registered(groups: list[Any], spec: HookSpec = HOOKS[0],
                        dests: set[Path] | None = None) -> bool:
    """True only when this spec's entry sits under THE MATCHER IT SHIPS.

    Path alone is not registration. Claude Code decides which tool calls reach
    a hook from the matcher of the group the entry sits in, so an entry with
    the right command under the previous release's narrower matcher is a hook
    that does not run on the doors this release added. Reading that as
    "already registered" is what made the 2026-09-02 matcher widening a no-op
    on every machine that already had the guard (adversarial review, s06
    round 1, H-1).
    """
    present, matcher = _registration(groups, spec, dests)
    return present and _matcher_ok(matcher, spec)


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


def _migrate_matcher(groups: list[Any], stale: dict[str, Any], spec: HookSpec,
                     dests: set[Path] | None = None) -> None:
    """Move this spec's OWN entries out from under a stale matcher.

    Two shapes, and the difference matters: a group holding nothing but our
    entries can have its matcher corrected in place, which preserves anything
    the owner tuned on the entry (a raised ``timeout``, an added
    ``statusMessage``). A group we SHARE with someone else's hook must not have
    its matcher rewritten — that would silently change when THEIR hook fires —
    so our entries are lifted out verbatim and re-homed instead.
    """
    entries = [e for e in (stale.get("hooks") or []) if isinstance(e, dict)]
    ours = [e for e in entries if _owns(e.get("command"), spec.script, dests)]
    if len(ours) == len(stale.get("hooks") or []):
        if spec.matcher is None:
            stale.pop("matcher", None)
        else:
            stale["matcher"] = spec.matcher
        return
    stale["hooks"] = [e for e in (stale.get("hooks") or []) if e not in ours]
    _group_for(groups, spec.matcher).setdefault("hooks", []).extend(ours)


def _write_settings(settings_path: Path, settings: Any) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(settings_path, settings)


def _dedupe(groups: list[Any], spec: HookSpec, dests: set[Path] | None) -> bool:
    """Collapse OUR entry to one copy under one matcher (s06 round 3, M-1).

    Migration re-homes our entries into the group carrying our matcher. If a
    group with that matcher already held one — two groups under the SAME
    matcher after a hand edit, or a decoy migration that split us across two —
    the result is the guard registered twice, so Claude Code runs it twice on
    every matching tool call. Measured at ~0.2 s each, comfortably inside the
    5 s budget, so this is cosmetic today; it is deduped rather than documented
    because a second copy is also a second thing that can drift.

    Only groups whose matcher is ALREADY ours are touched, and only OUR entries
    inside them: nothing here reads or moves anybody else's hook.
    """
    before = json.dumps(groups, sort_keys=True)
    seen: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        if not _matcher_ok(group.get("matcher") or None, spec):
            continue
        kept: list[Any] = []
        for entry in group.get("hooks") or []:
            if not (isinstance(entry, dict)
                    and _owns(entry.get("command"), spec.script, dests)):
                kept.append(entry)
                continue
            if any(e.get("command") == entry.get("command") for e in seen):
                continue                       # a duplicate of one we kept
            seen.append(entry)
            kept.append(entry)
        group["hooks"] = kept
    # A group we emptied by deduping, and that carries nothing else, goes.
    groups[:] = [g for g in groups
                 if not (isinstance(g, dict) and g.get("hooks") == []
                         and _matcher_ok(g.get("matcher") or None, spec)
                         and g.get("matcher") is not None)]
    return json.dumps(groups, sort_keys=True) != before


def _register(settings_path: Path, command: str, spec: HookSpec) -> str:
    """Add this spec's entry unless one is already there, under the right matcher.

    Returns ``"added"``, ``"already-registered"``, ``"migrated"``, or
    ``"unparseable"``."""
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

    # WHERE OUR OWN ENTRY MAY POINT: the destination we are about to write, the
    # one for THIS claude_home, and the stock `~/.claude/hooks/` one a previous
    # unqualified install wrote. Anything else with the same filename belongs to
    # somebody else and is never touched (s06 round 3, H-4).
    dests = managed_programs(settings_path.parent, spec.script)
    here = _program_path(command)
    if here is not None:
        dests.add(here)
    owning = _owning_groups(groups, spec.script, dests)
    stale = [g for g in owning if not _matcher_ok(g.get("matcher") or None, spec)]
    if owning and not stale:
        # NOTHING IS WRITTEN unless the dedupe actually removed a duplicate.
        # This module's contract is that a no-op run touches no file — a
        # rewrite would also reformat an owner's settings.json every time.
        if _dedupe(groups, spec, dests):
            _write_settings(settings_path, settings)
        return "already-registered"
    outcome = "added"
    if stale:
        # EVERY stale group, not the first (H-3). One decoy ahead of the real
        # entry used to consume the whole migration.
        for group in stale:
            _migrate_matcher(groups, group, spec, dests)
        outcome = "migrated"
    else:
        group = _group_for(groups, spec.matcher)
        group.setdefault("hooks", []).append({**spec.entry, "command": command})
    _dedupe(groups, spec, dests)

    _write_settings(settings_path, settings)
    return outcome


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
