"""``$BRAIN_WORKSPACE_SWEEP_DIRS`` — the nightly's sweep list, and the
installed launchd plist that is its durable home (CMD-02).

Split out of ``provision_wire`` so the plist has ONE owner: the parsing rules
here must stay identical to ``maintenance_sweep.workspace_sweep_config``'s,
and the write path has a security property worth stating in one place --- the
installed plist can carry the audit signing PEM, so it is ``chmod 600`` and a
rewrite that recreates it under the ambient umask would publish the vault's
private key to every process on the machine.

Wire 6 itself (:func:`wire_sweep`) lives here for the same reason: it is the
only wire that is entirely about this file, and ``provision_wire`` was over the
500-line production ceiling with it.
"""
from __future__ import annotations

import os
import platform
import plistlib
import stat as _stat
import tempfile
from pathlib import Path
from typing import Any, Optional

from .pathkey import file_lock, same_path

# A Cowork deliverable is a FINISHED document dropped into a folder, which is
# what the `path=N` per-dir age override exists for. The global default is 14
# days and would hold every deliverable back a fortnight.
SWEEP_AGE_DAYS = 1
DELIVERABLES_DIRNAME = "deliverables"
SWEEP_ENV = "BRAIN_WORKSPACE_SWEEP_DIRS"


def sweep_entries(raw: str) -> list[str]:
    """The entries of a ``$BRAIN_WORKSPACE_SWEEP_DIRS`` value, verbatim.
    Mirrors ``maintenance_sweep.workspace_sweep_config``'s splitting rules;
    each entry is ``path`` or ``path=N``."""
    return [e.strip() for e in (raw or "").split(os.pathsep) if e.strip()]


def entry_path(entry: str) -> str:
    """The path half of a sweep entry (``/x/y=1`` -> ``/x/y``)."""
    head, sep, age = entry.rpartition("=")
    return head if sep and age.isdigit() else entry


def merge_sweep_dirs(raw: str, wanted: Path,
                     *, age: Optional[int] = SWEEP_AGE_DAYS) -> tuple[str, bool]:
    """``(value, changed)`` — ``wanted`` added unless an entry already resolves
    to it. The comparison ignores any ``=N`` suffix, so an age the operator
    tuned by hand is never overwritten by a later provision."""
    entries = sweep_entries(raw)
    if any(same_path(entry_path(e), wanted) for e in entries):
        return os.pathsep.join(entries), False
    entries.append(f"{wanted}={age}" if age is not None else str(wanted))
    return os.pathsep.join(entries), True


def _load_plist(plist: Path) -> dict:
    with plist.open("rb") as fh:
        return plistlib.load(fh)


def installed_sweep_dirs(plist: Path) -> str:
    """The sweep list the INSTALLED plist carries ("" if none). Read
    before wire 1: ``install-brief-mac.sh`` re-renders the body from the
    CALLER's environment, so a file-only merge is thrown away by the next
    ``brain init --full --apply``."""
    try:
        env = _load_plist(plist).get("EnvironmentVariables") or {}
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""
    return str(env.get(SWEEP_ENV) or "")


# The installed plist can carry the vault's private signing PEM, so no path
# through this module may leave it, its temp file or its backup readable by
# anything but the owner. 0600 is the CEILING, not the target: a plist already
# at 0400 keeps its narrower mode.
_KEY_SAFE_MODE = 0o600


def write_sweep_dirs(plist: Path, value: str) -> None:
    """Merge ``value`` into the installed plist, CLAMPING its mode to 0600.

    ``install-brief-mac.sh`` ends with ``chmod 600`` because the rendered body
    substitutes ``AUDIT_KEY_PEM_PLACEHOLDER`` with the vault's private signing
    PEM when the operator injected one (custom custody / unattended box).
    Writing a fresh temp file and ``os.replace``-ing it recreates the file
    under the ambient umask, i.e. 0644 — every sweep-dir repair would have
    widened the signing key to world-readable.

    Two things that "create the temp file 0600, then restore the original
    mode" did NOT do, and both were live (2026-08-30 review):

    * ``os.open(..., O_CREAT|O_TRUNC, 0o600)`` applies its mode only when it
      creates the inode. A ``<plist>.tmp`` already sitting there — left by a
      crashed run, or planted — kept ITS mode while key-bearing data was
      written into it. ``mkstemp`` creates an unpredictable name with
      ``O_EXCL`` at 0600, so there is no pre-existing file to inherit from.
    * restoring the ORIGINAL mode re-published a plist that was already
      0644. The mode is clamped instead, so a repair can only ever narrow it.
    """
    from . import connect as _connect

    data = _load_plist(plist)
    env = dict(data.get("EnvironmentVariables") or {})
    env[SWEEP_ENV] = value
    data["EnvironmentVariables"] = env
    mode = _stat.S_IMODE(plist.stat().st_mode) & _KEY_SAFE_MODE
    bak = _connect.backup_original(plist)
    if bak is not None:
        os.chmod(bak, mode)
    fd, name = tempfile.mkstemp(dir=str(plist.parent),
                                prefix=plist.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            plistlib.dump(data, fh)
        os.chmod(tmp, mode)
        os.replace(tmp, plist)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


DEFAULT_LAUNCH_AGENTS = "Library/LaunchAgents"


def reload_line(vault, plist: Path) -> str:
    """The launchctl incantation for a plist this command merged BY FILE.

    ``bootstrap`` alone answers ``Load failed: 5`` on a job that is registered
    but stopped, so the bootout comes first; launchd freezes
    ``EnvironmentVariables`` at bootstrap, so ``launchctl print`` (not the file)
    proves the RUNNING job carries the new value. Printed, NEVER run.

    ``gui/$UID`` is the REAL login domain whatever ``$BRAIN_LAUNCH_AGENTS_DIR``
    says, so when the plist we merged is not the one launchd loads from
    ``~/Library/LaunchAgents`` the line has to say so — pasted unread against a
    test or sandbox path it would bootstrap the relocated file into the owner's
    live launchd, or bout a job whose installed copy still holds the old list.
    """
    from . import config as _config

    label = _config.nightly_label(vault)
    line = (f"launchctl bootout gui/$UID/{label} 2>/dev/null; "
            f"launchctl bootstrap gui/$UID {plist} && "
            f"launchctl print gui/$UID/{label} | grep -c SWEEP")
    default = Path.home() / DEFAULT_LAUNCH_AGENTS
    if not same_path(plist.parent, default):
        return (f"# NOTE: {plist} is NOT in {default} "
                f"($BRAIN_LAUNCH_AGENTS_DIR is set), while gui/$UID below is "
                f"the real login domain. Point this at the plist launchd "
                f"actually loads before running it.\n{line}")
    return line


def wire_sweep(vault: Path, deliverables: Path, plist: Path, *,
               repair: bool, create: bool = True,
               registered: Optional[bool] = None) -> dict[str, Any]:
    """Wire 6 — and wire 6 OWNS the nightly, not just its sweep list.

    ``registered=False`` (the caller asked launchd and the job is not there)
    turns an otherwise-fine ``already``/``done`` into ``failed`` carrying the
    reload line. Without it the exit code lied in a way no wire reported: the
    installer writes the plist and THEN bootstraps it, so a failed load leaves
    wire 1 saying ``done`` (the vault did index — and it must, or wires 2/3/5
    are blocked over a launchd fault) and wire 6 saying ``already`` (the file
    is there and carries the dir), and ``provision-local`` exited 0 on a vault
    with no nightly job. ``None`` is "not asked", which is what the read-only
    drain report and a run whose init failed both pass.

    Otherwise four cases, none a silent no-op: not macOS -> ``skipped`` with the
    Scheduled-Task equivalent; no plist -> ``failed`` naming what installs one;
    plist already carrying the dir -> ``already``; plist without it -> the
    locked plistlib merge, the REPAIR path (``pending`` when ``repair`` is off,
    which is how the unattended drain reports the gap without writing to the
    launchd job that is running it).
    """
    res = _wire_sweep(vault, deliverables, plist, repair=repair, create=create)
    if registered is False and res["status"] in ("already", "done"):
        from . import config as _config

        return {**res, "status": "failed", "reload_required": False,
                "detail": f"{res['detail']} — but launchd does NOT hold "
                          f"{_config.nightly_label(vault)}: the plist is "
                          f"installed and the job is not loaded, so nothing "
                          f"sweeps. Load it:\n  {reload_line(vault, plist)}"}
    return res


def _wire_sweep(vault: Path, deliverables: Path, plist: Path, *,
                repair: bool, create: bool = True) -> dict[str, Any]:
    if create:
        deliverables.mkdir(parents=True, exist_ok=True)
    common = {"sweep_dir": str(deliverables), "plist": str(plist)}
    if platform.system() != "Darwin":
        return {"status": "skipped",
                "detail": f"the nightly sweep list lives in a launchd plist "
                          f"(macOS only); on {platform.system()} set "
                          f"{SWEEP_ENV} on the scheduled task instead "
                          f"(scripts/install-brief-windows.ps1)", **common}
    if not plist.is_file():
        return {"status": "failed",
                "detail": f"plist-missing: {plist} — this vault has no nightly "
                          f"task installed, so there is nothing to sweep from. "
                          f"Install it with `BRAIN_VAULT={vault} bash "
                          f"scripts/install-brief-mac.sh`, then re-run "
                          f"provision-local.", **common}
    with file_lock(vault, plist):
        merged, changed = merge_sweep_dirs(installed_sweep_dirs(plist), deliverables)
        if not changed:
            return {"status": "already",
                    "detail": f"{deliverables} already in {SWEEP_ENV}", **common}
        if not repair:
            return {"status": "pending",
                    "detail": f"{deliverables} is NOT in {SWEEP_ENV}; run "
                              f"`brain provision-local {vault} --workspace "
                              f"{deliverables.parent}` to add it", **common}
        write_sweep_dirs(plist, merged)
    return {"status": "done", "reload_required": True,
            "detail": f"added {deliverables}={SWEEP_AGE_DAYS} to {SWEEP_ENV}",
            "reload": reload_line(vault, plist), **common}
