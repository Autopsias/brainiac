"""``brain doctor`` row: **Vault wiring** — one line per registered vault
naming whether all six ``provision-local`` wires (``provision_wire.py``) are
actually in place, and if not, the FIRST one that is missing.

Style matches ``doctor_mount_leak.py`` deliberately: pure file reads, no
index, no embedder, no network, no directory walk. Every read goes through
the SAME resolver the rest of the codebase uses for that artefact, so this
row binds to the ``tests/conftest.py`` autouse guards
(``_no_live_desktop_mcp_config``, ``_no_live_brainiac_home``) exactly the way
``provision_wire``/``connect`` already do — a hand-rolled path here would
read or write the owner's real Mac in a test:

* the registry: ``$BRAINIAC_HOME`` (env, read at CALL time) / ``workspaces.json``
* the plist: ``config.nightly_plist_path``
* the Desktop config: ``connect.claude_desktop_config_path``

**Non-gating by design (grill 2026-08-30).** ``brain update`` exits 1 on any
``doctor`` row in ``_GATING_STATUSES``, and wires 5/6 live in files OTHER
programs rewrite (Claude Desktop drops stdio entries on its own; a plist edit
does nothing until ``launchctl`` reloads it) — a gating row here would hold
every unattended auto-update hostage to a Desktop restart. So this row uses
the new ``doctor.WARN`` status, which is deliberately excluded from
``doctor._GATING_STATUSES``.

**No ``reload-pending`` status.** This module reads files only — it never
calls ``launchctl``. Wire 6 (the nightly sweep dir) is ``current`` the moment
the dir is listed in the installed plist FILE; the detail says so and adds
the one caveat a file read cannot resolve: whether launchd has actually
picked the change up.
"""
from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
from typing import Any, Optional

from . import workspaces
from .pathkey import real_key, same_path
from .provision_wire import WIRE_NAMES
from .sweepdirs import (DELIVERABLES_DIRNAME, SWEEP_ENV, entry_path,
                        installed_nightly_plist, sweep_entries)

__all__ = ["check_vault_wiring", "SURFACE"]

SURFACE = "Vault wiring"

_PROVISION_HINT = "brain provision-local {vault} --workspace {workspace}"


def _registry_path() -> Path:
    """``$BRAINIAC_HOME`` resolved HERE, at call time — never
    ``workspaces.REGISTRY_PATH``, which binds at import and is inert to an
    env pin set after this module first loads (the same class of bug
    ``provision_wire.provision_local`` and ``sweepdirs`` were both written
    to avoid)."""
    home = Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
    return home / "workspaces.json"


def _group_by_vault(entries: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Registry entries -> one bucket per distinct vault, keyed by
    ``real_key`` so a symlinked or relative spelling of the same vault never
    splits into two rows."""
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        raw = entry.get("vault_path")
        if not raw:
            continue
        groups.setdefault(real_key(raw), []).append(entry)
    return groups


def _workspace_for(entries: list[dict]) -> Optional[str]:
    """The workspace to check wires 2/5/6 against: the ``host`` row's, else
    whichever entry names one. ``None`` means the registry never recorded a
    workspace for this vault at all."""
    for entry in entries:
        if entry.get("target") == "host" and entry.get("workspace_path"):
            return entry["workspace_path"]
    for entry in entries:
        if entry.get("workspace_path"):
            return entry["workspace_path"]
    return None


def _desktop_mcp_servers(config_path: Path) -> tuple[Optional[dict], bool]:
    """``(mcpServers, unreadable)``. A MISSING file is legitimately "nothing
    registered here" (``{}``, not unreadable) — Claude Desktop may simply not
    be installed on this host. Only a file that exists and fails to parse is
    ``unreadable``, which is what earns the row ``not-detectable``."""
    if not config_path.is_file():
        return {}, False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, True
    return ((data.get("mcpServers") or {}) if isinstance(data, dict) else {}), False


def _plist_sweep_env(plist: Path) -> tuple[Optional[str], bool]:
    """``(sweep-env-value, unreadable)``. Same absent-vs-corrupt split as
    the Desktop config: no plist means no nightly task installed yet (a
    legitimate missing wire 6), never "cannot tell"."""
    if not plist.is_file():
        return None, False
    try:
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None, True
    env = data.get("EnvironmentVariables") or {}
    return str(env.get(SWEEP_ENV) or ""), False


def _mcp_entry_present(servers: dict, vault: str) -> bool:
    """Bound to THIS vault, not just to a name: ``mcp_server_name`` is
    derived from the workspace folder name, so matching on the slug alone
    could find another vault's entry that happens to slug the same. The
    ``env.BRAIN_VAULT`` binding is what ``provision_mcp.register_mcp`` itself
    treats as ownership."""
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        held = (entry.get("env") or {}).get("BRAIN_VAULT")
        if held and same_path(held, vault):
            return True
    return False


def _wire_row(vault: str, entries: list[dict], *, desktop_config_path: Path,
             plist_dir: Any) -> dict:
    from . import config as _config
    from . import provision
    from .doctor import CURRENT, NOT_DETECTABLE, WARN, _row

    surface = f"{SURFACE} — {vault}"
    vpath = Path(vault)
    workspace = _workspace_for(entries)
    hint = _PROVISION_HINT.format(vault=vault, workspace=workspace or "<dir>")

    def missing(wire: str, why: str) -> dict:
        return _row(surface, WARN,
                   f"missing: {WIRE_NAMES[wire]} ({why}) — run `{hint}`")

    # wire 1 — vault initialised. Three plain existence checks (never
    # `nightly_task_registered`, which needs launchctl and is out of budget
    # for a pure-file-reads row); matches `provision_wire.vault_is_initialised`
    # minus its launchd probe.
    vault_id = vpath / ".brain" / "vault-id"
    brain_dir = vpath / "brain"
    raw_dir = vpath / "raw"
    if not (vault_id.is_file() and brain_dir.is_dir() and raw_dir.is_dir()):
        absent = [str(p) for p in (vault_id, brain_dir, raw_dir) if not p.exists()]
        return missing("1", f"absent: {', '.join(absent)}")

    # wire 3 — host registry row
    if not any(e.get("target") == "host" for e in entries):
        return missing("3", f"no host row for {vault} in {_registry_path()}")

    # wire 4 — cowork-vm registry row
    if not any(e.get("target") == "cowork-vm" for e in entries):
        return missing("4", f"no cowork-vm row for {vault}")

    # wire 2 — workspace staged: the generated CLAUDE.md marker block, and
    # the engine stamp under the relocation-aware staging root.
    if workspace is None:
        return missing("2", "no workspace_path on record for this vault")
    wpath = Path(workspace)
    claude_md = wpath / "CLAUDE.md"
    try:
        marker = claude_md.is_file() and "BEGIN BRAIN-CONTRACT" in claude_md.read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        marker = False
    stamp = provision._staging_root(vpath, wpath) / "engine" / "brain" / "_version.py"
    if not (marker and stamp.is_file()):
        return missing("2", f"{claude_md} marker "
                       f"{'present' if marker else 'absent'}, {stamp} "
                       f"{'present' if stamp.is_file() else 'absent'}")

    # wire 5 — this vault's Claude Desktop brain-mcp entry
    servers, desktop_unreadable = _desktop_mcp_servers(desktop_config_path)
    if desktop_unreadable:
        return _row(surface, NOT_DETECTABLE,
                   f"{desktop_config_path} could not be read as JSON — cannot "
                   f"verify {WIRE_NAMES['5']}")
    if not _mcp_entry_present(servers or {}, vault):
        host_row = next((e for e in entries if e.get("target") == "host"), None)
        ever_written = bool(host_row and host_row.get("mcp_registered_at"))
        state = "written, then absent" if ever_written else "never written"
        return missing("5", state)

    # wire 6 — <workspace>/deliverables as a nightly sweep source. `current`
    # needs the FOLDER to exist AND be listed in the installed plist file —
    # no launchctl probe, so a genuinely current row still carries the
    # reload-pending caveat below.
    # The plist that ACTUALLY serves this vault, not the one its CURRENT path
    # hashes to: the label is a hash of the path, so a moved vault keeps the
    # job it was registered under while the engine computes a new name. Reading
    # the computed name made this row say "plist absent" for a healthy nightly
    # that ran every branch that day (measured 2026-08-31).
    plist = installed_nightly_plist(vpath, launch_agents_dir=plist_dir)
    sweep_env, plist_unreadable = _plist_sweep_env(plist)
    if plist_unreadable:
        return _row(surface, NOT_DETECTABLE,
                   f"{plist} could not be read — cannot verify {WIRE_NAMES['6']}")
    deliverables = wpath / DELIVERABLES_DIRNAME
    listed = sweep_env is not None and any(
        same_path(entry_path(e), deliverables) for e in sweep_entries(sweep_env))
    if not (deliverables.is_dir() and listed):
        why = ("plist absent" if sweep_env is None
              else "not listed" if not listed else f"{deliverables} missing")
        return missing("6", why)

    return _row(surface, CURRENT,
               "vault init, host row, cowork-vm row, workspace staging, "
               "claude desktop mcp entry and deliverables sweep dir all "
               "present (sweep dir: in plist; reload pending unless "
               "launchctl was re-run)")


def check_vault_wiring(registry_entries: Optional[list[dict[str, Any]]] = None, *,
                       desktop_config_path: Optional[Path] = None,
                       plist_dir: Any = None) -> list[dict]:
    """One row per distinct ``vault_path`` in the workspace registry.

    ``registry_entries=None`` resolves the registry itself from
    ``$BRAINIAC_HOME`` at call time (empty/absent registry -> zero rows,
    never an error) — the shape ``brain alerts`` needs, since it has no
    already-resolved list to hand in. ``brain doctor`` passes its own
    already-resolved ``registry_entries`` instead, exactly like the sibling
    ``check_cowork_mount_leak`` call beside it.
    """
    if registry_entries is None:
        registry_entries = workspaces.list_entries(registry_path=_registry_path())
    from . import connect as _connect

    cfg = (Path(desktop_config_path) if desktop_config_path
          else _connect.claude_desktop_config_path())
    rows = []
    for entries in _group_by_vault(registry_entries).values():
        vault = entries[0]["vault_path"]
        rows.append(_wire_row(vault, entries, desktop_config_path=cfg,
                              plist_dir=plist_dir))
    return rows
