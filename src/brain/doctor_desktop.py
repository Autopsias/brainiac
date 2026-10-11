"""Claude Desktop / Cowork registration doctor checks.

Split out of ``doctor_plugins.py`` (2026-08-21 size ratchet) — a distinct
responsibility from that module's install-channel/CLI-plugin surfaces: these
three checks all read Claude Desktop's OWN on-disk registration state
(``claude_desktop_config.json`` mcpServers, ``extensions-installations.json``,
and the per-session plugin-store cache), not the engine's install channel.
``doctor.py`` re-exports every name here so every existing
``brain.doctor.<name>`` caller (tests, ``doctor_context.build_context_and_checks``)
keeps working unchanged. Covered by ``tests/test_doctor_workspace.py``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .doctor_plugins import PLUGIN_NAMES

# --------------------------------------------------------------------------
# Surface — Desktop MCP registration collision (SUI-03 hardening addendum).
# A user can end up with BOTH the .mcpb-installed extension (Settings ->
# Extensions -> Advanced -> Install Extension, or double-click) AND a
# claude_desktop_config.json `mcpServers` stanza (written by `brain connect
# --client claude-desktop`) registering brainiac's MCP server at once —
# Desktop does not reconcile the two, so this is a "pick ONE" hygiene
# warning, not a version-staleness problem: UNMANAGED, never gates the exit
# code (ADR-0005 Ruling 2 — only STALE/UNKNOWN gate).
#
# Ground-truthed on-machine 2026-07-11 (S10 recon): Desktop's
# ``extensions-installations.json`` is ``{"extensions": {<id>: {"manifest":
# {"name": ..., ...}, ...}}}`` — matched on ``manifest.name`` rather than the
# ``id`` key, since built-in directory extensions use an ``ant.dir.*`` id
# scheme that a locally side-loaded .mcpb won't share.
# --------------------------------------------------------------------------

def check_mcp_vault_paths(config_path: Optional[Path] = None) -> list[dict]:
    """Every brainiac MCP server points at a vault that EXISTS (2026-08-17).

    A stale entry does not fail loudly — it keeps answering, from whatever
    index still sits at the old path's hash. Measured here: the registered
    server pointed at a vault moved five weeks earlier, and Claude Desktop had
    been served from a frozen index with 286 fewer notes the whole time.
    Results came back, so nothing looked wrong.

    Only `brain-mcp` servers are judged; other MCP servers are none of this
    engine's business.
    """
    from . import connect as _connect

    rows: list[dict] = []
    cfg = config_path or _connect.claude_desktop_config_path()
    data = _read_json(cfg) or {}
    servers = data.get("mcpServers") or {}
    for name, entry in sorted(servers.items()):
        if "brain-mcp" not in str((entry or {}).get("command", "")):
            continue
        vault = str(((entry or {}).get("env") or {}).get("BRAIN_VAULT") or "")
        surface = f"MCP server vault ({name})"
        if not vault:
            rows.append(_row(surface, UNKNOWN, "entry sets no BRAIN_VAULT",
                             remediation="brain connect --client claude-desktop"))
        elif Path(vault).is_dir():
            rows.append(_row(surface, CURRENT, f"{vault} exists"))
        else:
            rows.append(_row(
                surface, STALE,
                f"{vault} DOES NOT EXIST — this server still answers, from "
                "whatever stale index sits at that path's hash",
                remediation=("brain connect --client claude-desktop --name "
                             f"{name} (from the vault's real location), or "
                             "remove the entry")))
    return rows


def check_mcpb_desktop_collision(
    app_support_dir: Path, config_path: Optional[Path] = None, name: str = "brainiac",
) -> dict:
    surface = "Desktop MCP registration (mcpb vs claude_desktop_config.json)"
    if config_path is None:
        # Reuse the SAME platform-aware resolver connect.py's own
        # `brain connect --client claude-desktop` writer uses (win32/darwin/
        # linux) instead of hardcoding the macOS path here too — a second,
        # divergent macOS-only guess is exactly how this check went blind on
        # Windows (%APPDATA%\Claude) despite the .mcpb itself supporting
        # win32 (manifest.json compatibility.platforms).
        from . import connect

        config_path = connect.claude_desktop_config_path()
    config_data = _read_json(config_path) or {}
    config_present = bool(
        isinstance(config_data, dict) and (config_data.get("mcpServers") or {}).get(name)
    )

    installations = _read_json(app_support_dir / "extensions-installations.json") or {}
    exts = installations.get("extensions") if isinstance(installations, dict) else None
    mcpb_present = False
    if isinstance(exts, dict):
        for entry in exts.values():
            manifest = (entry or {}).get("manifest") or {}
            if manifest.get("name") == name:
                mcpb_present = True
                break

    if config_present and mcpb_present:
        return _row(
            surface, UNMANAGED,
            f"BOTH registered: claude_desktop_config.json mcpServers.{name} AND "
            "a .mcpb extension — Claude Desktop does not reconcile them, pick ONE",
            remediation=(
                "remove ONE: `brain connect --client claude-desktop --remove` "
                f"(drops the config.json stanza) OR Claude Desktop -> Settings -> "
                f"Extensions -> {name} -> Remove (drops the .mcpb)"),
            raw={"config_present": config_present, "mcpb_present": mcpb_present})
    if not app_support_dir.exists():
        return _row(surface, NOT_DETECTABLE,
                    f"{app_support_dir} not found — Claude Desktop not installed here")
    return _row(surface, CURRENT,
                f"no collision (config.json entry: {config_present}, .mcpb extension: {mcpb_present})",
                raw={"config_present": config_present, "mcpb_present": mcpb_present})


# --------------------------------------------------------------------------
# Surface 11 — Desktop / Cowork plugin-skill store (best-effort, ALWAYS
# manual-required, NEVER gates the exit code — ADR-0005 Ruling 2/4).
# --------------------------------------------------------------------------

_STAMP = re.compile(rb"<!-- SKILL_VERSION:[^\n]*-->\n?")

# brainiac-manager's skills install/update the HOST and refuse to run in
# Cowork (brainiac-update is host-only by design), so its absence from the
# Cowork store is the expected state, not a step the owner owes.
_OPTIONAL_IN_COWORK = {"brainiac-manager"}


def _skill_reference(registry_entries: Optional[list[dict]], ssot: str) -> dict[str, bytes]:
    """name -> current SKILL.md bytes (version stamp stripped), read from the
    workspaces' staged ``.skill`` bundles at the SSOT version.

    Cowork's marketplace is a cloud copy that can lag weeks behind GitHub and
    re-label identical skill text with an old version (measured 2026-10-05:
    0.20.39 installed, text identical to 0.20.41). Comparing TEXT against the
    bundles `brain update` just staged is what tells the owner whether to act.
    """
    import zipfile

    from .vmstaging import _cowork_staging_root

    ref: dict[str, bytes] = {}
    for entry in registry_entries or []:
        if entry.get("target") == "host":
            continue
        skills_dir = _cowork_staging_root(entry) / "skills"
        for bundle in sorted(skills_dir.glob("*.skill")) if skills_dir.is_dir() else []:
            if bundle.stem in ref:
                continue
            try:
                with zipfile.ZipFile(bundle) as zf:
                    if zf.read(f"{bundle.stem}/VERSION").decode("utf-8").strip() != ssot:
                        continue
                    ref[bundle.stem] = _STAMP.sub(b"", zf.read(f"{bundle.stem}/SKILL.md"))
            except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
                continue
    return ref


def _content_drift(plugin_dir: Path, reference: dict[str, bytes]) -> Optional[tuple[int, list[str]]]:
    """(skills compared, names whose text differs), or None when nothing in
    the installed plugin has a reference to compare against."""
    skills_root = plugin_dir / "skills"
    if not skills_root.is_dir():
        return None
    names = sorted(p.name for p in skills_root.iterdir()
                   if p.name in reference and (p / "SKILL.md").is_file())
    if not names:
        return None
    differs = [n for n in names
               if _STAMP.sub(b"", (skills_root / n / "SKILL.md").read_bytes()) != reference[n]]
    return len(names), differs


def _store_refresh_steps(pname: str, registry_entries: Optional[list[dict]]) -> str:
    from .vmstaging import _cowork_staging_root

    staged = next((str(_cowork_staging_root(e) / "skills") for e in registry_entries or []
                   if e.get("target") != "host"), "<workspace>/vault/.brain/skills")
    return (f"in Cowork: Customize -> Plugins -> refresh the brainiac marketplace, then "
            f"update {pname}; if it still offers older text, uninstall it and upload "
            f"the .skill bundles from {staged}; re-run brain doctor on the host to confirm")


def _newest_store_copy(sessions_root: Path, pname: str) -> list[tuple[float, Path]]:
    candidates: list[tuple[float, Path]] = []
    try:
        for pjson in sessions_root.glob("*/*/rpm/plugin_*/.claude-plugin/plugin.json"):
            data = _read_json(pjson)
            if data and data.get("name") == pname:
                candidates.append((pjson.stat().st_mtime, pjson))
    except Exception:
        return []
    return sorted(candidates, key=lambda t: t[0], reverse=True)


def check_desktop_plugin_store(
    app_support_dir: Path, ssot: str, plugin_dir_names: tuple[str, ...] = PLUGIN_NAMES,
    registry_entries: Optional[list[dict]] = None,
) -> list[dict]:
    """Best-effort read of
    ``.../local-agent-mode-sessions/<uuid>/<uuid>/rpm/plugin_*/.claude-plugin/plugin.json``.

    The path carries a per-session UUID with no stable pointer to "the live
    one" from outside that session, so this picks the most-recently-modified
    candidate plugin.json per plugin name and labels the row accordingly
    (HARDEN:consensus). If nothing is found at all it's `manual-required`
    (not scriptable from here); if multiple candidates tie or none can be
    confidently chosen it reports 'unknown (N candidate sessions)' rather than
    inventing a version.

    When the registered workspaces carry staged bundles at the SSOT version,
    the verdict is the installed skill TEXT against them, not the label: a
    match reads `current` (never gating either way, ADR-0005 Ruling 2/4).
    Every row that needs the owner sets ``raw["action_needed"]``.
    """
    sessions_root = app_support_dir / "local-agent-mode-sessions"
    reference = _skill_reference(registry_entries, ssot)
    rows = []
    for pname in plugin_dir_names:
        surface = f"Desktop/Cowork plugin store ({pname})"
        if not sessions_root.exists():
            rows.append(_row(surface, MANUAL_REQUIRED,
                             "no local-agent-mode-sessions dir found — best-effort, "
                             "verify manually in the Cowork/Desktop client",
                             remediation="Open Cowork/Desktop -> Plugins -> check for update"))
            continue
        candidates = _newest_store_copy(sessions_root, pname)
        if not candidates and pname in _OPTIONAL_IN_COWORK:
            rows.append(_row(surface, UNMANAGED,
                             "not installed in Cowork — optional: its skills manage the "
                             "host install and refuse to run in Cowork; nothing to do",
                             raw={"action_needed": False}))
            continue
        if not candidates:
            rows.append(_row(surface, MANUAL_REQUIRED,
                             "not found in any session dir — best-effort, "
                             "verify manually in the Cowork/Desktop client",
                             remediation=_store_refresh_steps(pname, registry_entries),
                             raw={"action_needed": True}))
            continue
        newest_mtime, newest_path = candidates[0]
        data = _read_json(newest_path) or {}
        version = data.get("version")
        if version is None:
            rows.append(_row(surface, UNKNOWN, f"unknown ({len(candidates)} candidate sessions, unparseable)"))
            continue
        drift = _content_drift(newest_path.parent.parent, reference)
        if drift is not None:
            rows.append(_content_row(surface, pname, str(version), ssot, drift, registry_entries))
            continue
        import datetime

        mtime_str = datetime.datetime.fromtimestamp(newest_mtime).isoformat(timespec="seconds")
        detail = (f"best-effort, last-seen (mtime {mtime_str}): version {version} "
                  f"(SSOT {ssot}); {len(candidates)} candidate session(s) found")
        # Always manual-required (Ruling 2/4): never gates the exit code, no
        # matter what the version comparison says. The remediation text still
        # differentiates stale-vs-current so it points at the real fix: the
        # CLI only DETECTS this surface (it structurally cannot invoke a Claude
        # slash-command skill); in a Cowork session /skill-creator is what
        # repackages + presents the skill for Save-and-Replace. /brainiac-update
        # is host-only (refuses --role vm) so it is NOT the Cowork fix.
        #
        # ANY mismatch needs action, not just `installed < ssot` (Codex cloud
        # review, 2026-08-07). `installed > ssot` was reported as "looks current
        # — no action needed", but ADR-0004 Ruling 5 / the CLI-plugin path
        # explicitly handle RECONCILIATION DOWNGRADES, where an old installed
        # plugin legitimately carries a numerically higher version than the
        # current SSOT. The surface left stale by a false green here is the
        # LLM-facing Cowork/Desktop skill instructions, so "newer" is not a
        # reason to leave it alone after a security-relevant release.
        skew = _compare(str(version), ssot)
        if skew < 0:
            remediation = ("in a Cowork session use /skill-creator to repackage + "
                           "Save-and-Replace the stale skill(s); re-run brain doctor on "
                           "the host to confirm it took")
        elif skew > 0:
            remediation = (f"installed {version} is AHEAD of SSOT {ssot} — a "
                           "reconciliation downgrade, not a current install; in a "
                           "Cowork session use /skill-creator to repackage + "
                           "Save-and-Replace so the shipped skill matches SSOT")
        else:
            remediation = "looks current — no action needed"
        rows.append(_row(surface, MANUAL_REQUIRED, detail, remediation=remediation,
                         raw={"version": version, "candidates": len(candidates),
                              "newest_mtime": mtime_str, "action_needed": skew != 0}))
    return rows


def _content_row(surface: str, pname: str, version: str, ssot: str,
                 drift: tuple[int, list[str]], registry_entries: Optional[list[dict]]) -> dict:
    checked, differs = drift
    raw = {"version": version, "checked": checked, "differs": differs,
           "action_needed": bool(differs)}
    if not differs:
        return _row(surface, CURRENT,
                    f"{checked} skill(s) match the {ssot} text (Cowork labels it {version}; "
                    "the label can lag, the text is what runs)", raw=raw)
    return _row(surface, MANUAL_REQUIRED,
                f"{len(differs)} of {checked} skill(s) differ from the {ssot} text "
                f"(Cowork labels it {version}): {', '.join(differs)}",
                remediation=_store_refresh_steps(pname, registry_entries), raw=raw)


# --------------------------------------------------------------------------
# Claude Desktop's running brain-mcp servers. A Python process keeps the code
# it imported at start, so after `brain update` reinstalls the engine every
# server Desktop already started still serves the OLD engine until Desktop is
# quit and reopened. Measured 2026-10-04: 8 servers started 22:30, engine
# reinstalled 23:15, nothing reported it.
# --------------------------------------------------------------------------

def _ps_rows() -> Optional[list[tuple[int, float, str]]]:
    """(pid, start epoch, command) for every process, or None where `ps` with
    `lstart` is unavailable (Windows)."""
    import datetime
    import os
    import subprocess
    import sys

    if sys.platform == "win32":
        return None
    try:
        out = subprocess.run(["ps", "-axo", "pid=,lstart=,command="], capture_output=True,
                             text=True, timeout=15, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        try:
            start = datetime.datetime.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y")
            rows.append((int(parts[0]), start.timestamp(), parts[6]))
        except ValueError:
            continue
    return rows


def _engine_install(command: str) -> Optional[tuple[float, str]]:
    """(install time, version) of the engine a `<venv>/bin/brain-mcp` runs."""
    for stamp in sorted(Path(command).parent.parent.glob("lib/python*/site-packages/brain/_version.py")):
        match = re.search(r'__version__\s*=\s*"([^"]+)"', stamp.read_text(encoding="utf-8"))
        return stamp.stat().st_mtime, (match.group(1) if match else "?")
    return None


def check_mcp_server_code(config_path: Optional[Path] = None,
                          ps_rows: Optional[list[tuple[int, float, str]]] = None) -> list[dict]:
    """One row per brain-mcp command Desktop launches: are its running
    servers newer than the engine they would load today? Never gating — a
    Desktop restart must not hold `brain update` or CI hostage."""
    import datetime

    from . import connect as _connect

    data = _read_json(config_path or _connect.claude_desktop_config_path()) or {}
    commands = sorted({str((e or {}).get("command")) for e in (data.get("mcpServers") or {}).values()
                       if "brain-mcp" in str((e or {}).get("command", ""))})
    procs = _ps_rows() if ps_rows is None else ps_rows
    rows = []
    for command in commands:
        surface = f"Claude Desktop Brainiac servers ({command})"
        installed = _engine_install(command)
        if procs is None or installed is None:
            rows.append(_row(surface, NOT_DETECTABLE, "cannot read the running servers or the "
                             "engine they load on this platform", raw={"action_needed": False}))
            continue
        at, version = installed
        when = datetime.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M")
        starts = [start for _pid, start, cmd in procs if command in cmd]
        old = [s for s in starts if s < at]
        if not starts:
            rows.append(_row(surface, NOT_DETECTABLE,
                             f"none running — Claude Desktop will load {version} when it starts them",
                             raw={"action_needed": False}))
        elif old:
            rows.append(_row(surface, MANUAL_REQUIRED,
                             f"{len(old)} of {len(starts)} running process(es) started before the "
                             f"engine was installed ({version}, {when}) — they still serve the old code",
                             remediation="quit Claude Desktop completely (Cmd+Q) and reopen it",
                             raw={"action_needed": True, "version": version}))
        else:
            rows.append(_row(surface, CURRENT,
                             f"{len(starts)} running process(es), all started after the engine "
                             f"install ({version}, {when})", raw={"action_needed": False}))
    return rows


# Parent-namespace binds, deferred past this module's own defs.
from .doctor import (  # noqa: E402
    CURRENT as CURRENT,
    MANUAL_REQUIRED as MANUAL_REQUIRED,
    NOT_DETECTABLE as NOT_DETECTABLE,
    STALE as STALE,
    UNMANAGED as UNMANAGED,
    UNKNOWN as UNKNOWN,
    _compare as _compare,
    _read_json as _read_json,
    _row as _row,
)
