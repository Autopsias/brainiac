"""Install-channel, plugin, marketplace, and desktop-store doctor checks."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------
# Install channel detection (PYP-04c). Post-S07, a host install may land via
# any of four channels — the legacy editable dev checkout (`~/.brainiac/venv`,
# pre-PyPI / --dev / offline), or one of the three PyPI channels `install.sh`/
# `install.ps1` try in order: `uv tool install`, `pipx install`,
# `pip install --user`. Detection is a pure, offline path-substring heuristic
# (ponytail: good enough for a doctor hint, never a hard guarantee) so it
# stays fully unit-testable with a fabricated Path — no real PATH probing
# inside the pure function itself; callers resolve the live `brain` binary
# and pass it in.
# --------------------------------------------------------------------------

CHANNEL_EDITABLE = "editable-checkout"
CHANNEL_VENV_WHEEL = "venv-wheel"
CHANNEL_PYPI_UV = "pypi-uv"
CHANNEL_PIPX = "pipx"
CHANNEL_PIP_USER = "pip-user"
CHANNEL_UNKNOWN = "unknown"

# The command that moves each channel's installed version forward — the
# PACKAGE name (brainiac-cli), never the bare `uvx <pypi-name>` form (that
# fails when the console command, `brain`, differs from the distribution
# name). Editable-checkout has no single command (checkout-path-dependent);
# `/brainiac-update` resolves it.
_CHANNEL_UPGRADE_CMD = {
    CHANNEL_PYPI_UV: "uv tool upgrade brainiac-cli",
    CHANNEL_PIPX: "pipx upgrade brainiac-cli",
    CHANNEL_PIP_USER: "python3 -m pip install --user --upgrade 'brainiac-cli[mcp]'",
    CHANNEL_VENV_WHEEL: "<~/.brainiac/venv>/bin/pip install --upgrade 'brainiac-cli[mcp]' "
                        "(or the local checkout build); /brainiac-update resolves it",
    CHANNEL_EDITABLE: "git pull in the checkout, then: pip install --upgrade -e '<checkout>[mcp]'",
}


def detect_install_channel(brain_bin: Optional[Path]) -> str:
    """Best-effort, offline channel classification from a resolved `brain`
    executable path. Pure except the ONE filesystem probe the ``~/.brainiac/
    venv`` case needs (RC2): a venv there is EITHER the legacy editable dev
    checkout (``pip install -e`` leaves an ``__editable__*.pth`` under
    site-packages) OR a plain wheel install (``pip install brainiac-cli`` —
    dist-info, no ``.pth``). The old regex assumed editable unconditionally
    and would ``pip install -e`` a wrong path; disambiguate on the marker the
    editable install actually leaves behind."""
    if brain_bin is None:
        return CHANNEL_UNKNOWN
    p = str(brain_bin)
    if re.search(r"\.brainiac[/\\]+venv", p):
        # brain_bin = <venv>/bin/brain (POSIX) or <venv>\Scripts\brain.exe
        # (Windows) — the venv root is the grandparent either way.
        venv_dir = Path(brain_bin).parent.parent
        editable_markers = (
            list((venv_dir / "lib").glob("*/site-packages/__editable__*"))     # POSIX
            + list((venv_dir / "Lib" / "site-packages").glob("__editable__*"))  # Windows
        )
        return CHANNEL_EDITABLE if editable_markers else CHANNEL_VENV_WHEEL
    if re.search(r"[/\\]uv[/\\]tools[/\\]", p) or re.search(r"[/\\]uv[/\\]bin[/\\]", p):
        return CHANNEL_PYPI_UV
    if "pipx" in p:
        return CHANNEL_PIPX
    return CHANNEL_PIP_USER


# --------------------------------------------------------------------------
# Surface 3 — host engine install (channel-aware: editable dev checkout OR
# one of the three PyPI channels — PYP-04). ``resolved_brain`` is the live
# PATH-resolved `brain` binary, passed in by ``run_doctor()`` (never resolved
# inside this pure-ish function, so tests stay deterministic regardless of
# what's on the live PATH — see test_host_venv_* in tests/test_doctor.py).
# --------------------------------------------------------------------------

def _running_engine_version() -> Optional[str]:
    """The version of the engine EXECUTING this check.

    Read from the live module, never from a file on disk: the checkout's SSOT
    is the running version only when the engine runs from that checkout, and
    on a venv-wheel install it is just whatever branch the dev tree sits on."""
    try:
        from ._version import __version__

        return str(__version__) or None
    except Exception:  # noqa: BLE001 — an unreadable stamp falls back to the SSOT
        return None


def _version_tuple(v: str) -> Optional[tuple[int, int, int]]:
    """(major, minor, patch) for comparison, or None if unparseable.

    ponytail: numeric prefix only — enough to order 0.19.9 < 0.19.12 (which a
    string compare gets WRONG, and that ordering is the whole point here). No
    `packaging` dependency for three integers.
    """
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def check_host_venv(brainiac_home: Path, ssot: str, resolved_brain: Optional[Path] = None) -> dict:
    legacy_bin = brainiac_home / "venv" / "bin" / "brain"
    brain_bin = legacy_bin if legacy_bin.exists() else resolved_brain
    if brain_bin is None or not Path(brain_bin).exists():
        return _row("Host engine venv", NOT_DETECTABLE,
                    f"no `brain` found (legacy venv {legacy_bin}, or on PATH)",
                    remediation="/brainiac-install")
    channel = detect_install_channel(Path(brain_bin))
    try:
        out = subprocess.run(
            [str(brain_bin), "--version"], capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        return _row("Host engine venv", UNKNOWN, f"{type(exc).__name__}: {exc}",
                    raw={"channel": channel})
    text = (out.stdout or out.stderr or "").strip()
    m = re.search(r"(\d+\.\d+\.\d+\S*)", text)
    installed = m.group(1) if m else text
    if not installed:
        return _row("Host engine venv", UNKNOWN, "empty --version output", raw={"channel": channel})
    running = _running_engine_version() or ssot
    if installed == running:
        # The venv IS what is running. The checkout's SSOT is irrelevant here:
        # this row is about the venv versus the live engine, and a dev tree
        # parked on an older branch is not a deployment fault.
        return _row("Host engine venv", CURRENT,
                    f"{installed} == running engine (channel: {channel})",
                    raw={"installed": installed, "running": running,
                         "ssot": ssot, "channel": channel})
    if installed == ssot:
        return _row("Host engine venv", CURRENT, f"{installed} == SSOT {ssot} (channel: {channel})",
                    raw={"installed": installed, "channel": channel})
    # DIRECTION MATTERS (2026-07-25). `ssot` is the version of the engine RUNNING
    # this check, so a mismatch has two opposite causes and opposite fixes:
    #
    #   installed < ssot  -> the venv is behind      -> /brainiac-update (original case)
    #   installed > ssot  -> WE are behind: this check is running from a PINNED
    #                        engine older than what's installed -> restage/repoint
    #                        the pin. /brainiac-update here is actively wrong; it
    #                        would "upgrade" a venv that is already newer.
    #
    # Measured: the nightly's BRAIN_BIN was pinned to dist/engines/brainiac-0.19.9
    # while the venv held 0.19.12. It reported "installed 0.19.12 != SSOT 0.19.9"
    # and prescribed /brainiac-update — so the pinned engine sat 3 versions stale
    # for 4 days, its only symptom a health banner that read as the opposite
    # problem. Naming the direction is the whole fix.
    #
    # CORRECTION 2026-08-18: `ssot` is the CHECKOUT's version, which is only the
    # running engine's version when the engine runs FROM that checkout. On a
    # venv-wheel install it is just whatever branch the dev tree sits on, so a
    # checkout parked on a feature branch made this row claim "THIS ENGINE is
    # behind" and prescribe repointing $BRAIN_BIN — while $BRAIN_BIN, the venv,
    # the PATH `brain` and the running process were all on the NEW version.
    # Measured the night 0.20.19 shipped: the release was cut from master while
    # the checkout stayed on a 0.20.18 branch, and BOTH vaults went STALE on a
    # pin that was already correct. Ask the RUNNING process what it is, rather
    # than inferring it from a file on disk.
    inst_v, run_v = _version_tuple(installed), _version_tuple(running)
    if inst_v and run_v and inst_v > run_v:
        return _row(
            "Host engine venv", STALE,
            f"THIS ENGINE is behind the installed one — running {running}, installed is "
            f"{installed} (channel: {channel}). The venv is fine; whatever pinned this "
            f"engine (e.g. a scheduled job's $BRAIN_BIN) is stale.",
            remediation="repoint the pin at the current engine — check $BRAIN_BIN in "
                        "~/Library/LaunchAgents/com.brainiac.*.plist, and stage a fresh "
                        "one with tools/cos_canary_install.sh <version> if needed. Do NOT "
                        "run /brainiac-update: the installed venv is already newer.",
            raw={"installed": installed, "running": running, "channel": channel,
                 "direction": "running-engine-behind"})
    return _row("Host engine venv", STALE, f"installed {installed} != SSOT {ssot} (channel: {channel})",
                remediation="/brainiac-update",
                raw={"installed": installed, "channel": channel,
                     "direction": "venv-behind"})


# --------------------------------------------------------------------------
# Surface 4 — dist/COMPAT
# --------------------------------------------------------------------------

def check_dist_compat(repo_root: Path, ssot: str) -> dict:
    compat_path = repo_root / "dist" / "COMPAT"
    if not compat_path.exists():
        return _row("dist/COMPAT marker", NOT_DETECTABLE,
                    "dist/COMPAT not found (never packaged here)",
                    remediation="python tools/package_clients.py")
    marker = compat_path.read_text(encoding="utf-8").strip()
    if marker == ssot:
        return _row("dist/COMPAT marker", CURRENT, f"{marker} == SSOT {ssot}",
                    raw={"marker": marker})
    # dist/COMPAT is gitignored (Context, ADR-0005) — a `git pull` never
    # refreshes it, so `stale` here means "regenerate", not "investigate".
    return _row("dist/COMPAT marker", STALE, f"{marker} != SSOT {ssot} (gitignored — regenerate)",
                remediation="python tools/package_clients.py", raw={"marker": marker})


# --------------------------------------------------------------------------
# Surface 5 — CLI plugin manifests (plugins/*/.claude-plugin/plugin.json)
# --------------------------------------------------------------------------

PLUGIN_NAMES = ("brainiac-manager", "brainiac-kernel", "brainiac-extras")


def check_plugin_manifests(repo_root: Path, ssot: str) -> list[dict]:
    rows = []
    for pname in PLUGIN_NAMES:
        pjson = repo_root / "plugins" / pname / ".claude-plugin" / "plugin.json"
        surface = f"Plugin manifest ({pname})"
        data = _read_json(pjson)
        if data is None:
            rows.append(_row(surface, NOT_DETECTABLE, f"{pjson} missing/unparseable",
                             remediation="python tools/package_clients.py"))
            continue
        pv = data.get("version")
        if pv == ssot:
            rows.append(_row(surface, CURRENT, f"{pv} == SSOT {ssot}", raw={"version": pv}))
        else:
            rows.append(_row(surface, STALE, f"{pv} != SSOT {ssot}",
                             remediation="python tools/package_clients.py", raw={"version": pv}))
    return rows


# --------------------------------------------------------------------------
# Surface 7 — installed Claude Code CLI plugins (best-effort, manual-required
# only when literally not locatable; otherwise scriptable-best-effort per
# Ruling 2 row 7 — stale/current are still meaningful here since the
# marketplace + installed_plugins.json are both local files, no network).
# --------------------------------------------------------------------------

def check_installed_cli_plugins(
    claude_home: Path, ssot: str, marketplace_name: str = "brainiac",
    marketplace_dir: Optional[Path] = None,
) -> list[dict]:
    rows = []
    # RC3: a directory-source marketplace lives at known_marketplaces.json's
    # installLocation, NOT the hardcoded marketplaces/<name> dir — the caller
    # passes the resolved dir; keep the hardcoded guess only as the fallback.
    marketplace_dir = marketplace_dir or (claude_home / "plugins" / "marketplaces" / marketplace_name)
    for pname in PLUGIN_NAMES:
        surface = f"Installed CLI plugin ({pname})"
        mkt_json = marketplace_dir / "plugins" / pname / ".claude-plugin" / "plugin.json"
        mkt_data = _read_json(mkt_json)
        if mkt_data is None:
            rows.append(_row(surface, NOT_DETECTABLE,
                             f"marketplace copy not found at {mkt_json}",
                             remediation="/plugin marketplace add Autopsias/brainiac"))
            continue
        mkt_version = mkt_data.get("version")
        installed_json = claude_home / "plugins" / "installed_plugins.json"
        installed_data = _read_json(installed_json) or {}
        plugin_entries = (installed_data.get("plugins") or {}).get(f"{pname}@{marketplace_name}")
        if not plugin_entries:
            rows.append(_row(surface, NOT_DETECTABLE,
                             f"not installed (marketplace has {mkt_version})",
                             remediation=f"/plugin install {pname}@{marketplace_name}"))
            continue
        # installed_plugins.json version field is a cache-dir label, not always
        # semver (it can be a git sha for github-sourced plugins) — read the
        # REAL version from the plugin.json at the recorded installPath, the
        # same on-disk contract as the marketplace copy.
        entry = plugin_entries[0] if isinstance(plugin_entries, list) else plugin_entries
        install_path = entry.get("installPath") if isinstance(entry, dict) else None
        installed_version = None
        if install_path:
            installed_pjson = _read_json(Path(install_path) / ".claude-plugin" / "plugin.json")
            if installed_pjson:
                installed_version = installed_pjson.get("version")
        if installed_version is None:
            rows.append(_row(surface, UNKNOWN,
                             f"installed but version unreadable at {install_path}"))
            continue
        cmp_ = _compare(installed_version, mkt_version or "")
        if cmp_ == 0:
            rows.append(_row(surface, CURRENT, f"installed {installed_version} == marketplace {mkt_version}",
                             raw={"installed": installed_version, "marketplace": mkt_version}))
        elif cmp_ < 0:
            rows.append(_row(surface, STALE, f"installed {installed_version} < marketplace {mkt_version}",
                             remediation=f"/plugin update {pname}@{marketplace_name}",
                             raw={"installed": installed_version, "marketplace": mkt_version}))
        else:
            # Downgrade condition (Ruling 3 / ADR-0004 Ruling 5): installed >
            # marketplace, e.g. a stale 1.x line meeting a reconciled 0.9.x.
            # Report the RAW triple; never assert "regression" — the human/
            # update-skill interprets it (blindspot hardening).
            rows.append(_row(surface, STALE,
                             f"installed {installed_version} > marketplace {mkt_version} "
                             "(reconciliation downgrade — see ADR-0004 Ruling 5 / ADR-0005 Ruling 3)",
                             remediation=f"/plugin uninstall {pname}@{marketplace_name} "
                                         f"&& /plugin install {pname}@{marketplace_name}",
                             raw={"installed": installed_version, "marketplace": mkt_version}))
    return rows


# --------------------------------------------------------------------------
# Surface — stale-name plugin/marketplace install (NAM-03). Anyone who
# installed before the profile-a-marketplace/profile-a-kernel/profile-a-extras
# -> brainiac/brainiac-kernel/brainiac-extras rename has old names registered
# in known_marketplaces.json / installed_plugins.json. This is a
# plugin-INDEPENDENT surface (pure Python reading Claude Code's own state
# files) — it survives even if the plugin surface itself is broken, so the
# fix instructions here are the recovery-of-last-resort, not just a nicety.
# --------------------------------------------------------------------------

OLD_MARKETPLACE_NAME = "profile-a-marketplace"
OLD_TO_NEW_PLUGIN_NAMES = {
    "profile-a-kernel": "brainiac-kernel",
    "profile-a-extras": "brainiac-extras",
}
# The verbatim 2-command recovery (also in README's Updating section + the
# CHANGELOG rename entry) — add-new-before-remove-old, never the reverse.
STALE_NAME_RECOVERY = (
    "claude plugin marketplace add Autopsias/brainiac && "
    "claude plugin install brainiac-manager@brainiac  # then run /brainiac-update "
    "to finish migrating off the old names"
)


def check_stale_name_plugins(claude_home: Path) -> list[dict]:
    rows = []
    surface = "Stale-name plugin/marketplace install"
    # known_marketplaces.json is a FLAT dict keyed by marketplace name
    # (verified on-machine 2026-07-11: {"<name>": {"source": ..., "installLocation": ...}, ...}
    # — no "marketplaces" wrapper key).
    known_mkt = _read_json(claude_home / "plugins" / "known_marketplaces.json") or {}
    installed = _read_json(claude_home / "plugins" / "installed_plugins.json") or {}
    has_old_marketplace = bool(
        isinstance(known_mkt, dict) and OLD_MARKETPLACE_NAME in known_mkt
    )
    old_plugin_specs = [
        spec for spec in (installed.get("plugins") or {})
        if isinstance(spec, str) and spec.split("@", 1)[0] in OLD_TO_NEW_PLUGIN_NAMES
    ]
    if not has_old_marketplace and not old_plugin_specs:
        rows.append(_row(surface, NOT_DETECTABLE,
                         "no old-name marketplace or plugin registrations found"))
        return rows
    found = []
    if has_old_marketplace:
        found.append(f"marketplace '{OLD_MARKETPLACE_NAME}'")
    found.extend(f"plugin '{spec}'" for spec in old_plugin_specs)
    rows.append(_row(surface, STALE,
                     f"old-name registration(s) found: {', '.join(found)}",
                     remediation=STALE_NAME_RECOVERY,
                     raw={"old_marketplace": has_old_marketplace, "old_plugins": old_plugin_specs}))
    return rows


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

def check_desktop_plugin_store(
    app_support_dir: Path, ssot: str, plugin_dir_names: tuple[str, ...] = PLUGIN_NAMES,
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
    """
    sessions_root = app_support_dir / "local-agent-mode-sessions"
    rows = []
    for pname in plugin_dir_names:
        surface = f"Desktop/Cowork plugin store ({pname})"
        if not sessions_root.exists():
            rows.append(_row(surface, MANUAL_REQUIRED,
                             "no local-agent-mode-sessions dir found — best-effort, "
                             "verify manually in the Cowork/Desktop client",
                             remediation="Open Cowork/Desktop -> Plugins -> check for update"))
            continue
        candidates: list[tuple[float, Path]] = []
        try:
            for pjson in sessions_root.glob("*/*/rpm/plugin_*/.claude-plugin/plugin.json"):
                data = _read_json(pjson)
                if data and data.get("name") == pname:
                    candidates.append((pjson.stat().st_mtime, pjson))
        except Exception:
            candidates = []
        if not candidates:
            rows.append(_row(surface, MANUAL_REQUIRED,
                             "not found in any session dir — best-effort, "
                             "verify manually in the Cowork/Desktop client",
                             remediation="Open Cowork/Desktop -> Plugins -> check for update"))
            continue
        candidates.sort(key=lambda t: t[0], reverse=True)
        newest_mtime, newest_path = candidates[0]
        data = _read_json(newest_path) or {}
        version = data.get("version")
        if version is None:
            rows.append(_row(surface, UNKNOWN, f"unknown ({len(candidates)} candidate sessions, unparseable)"))
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
                              "newest_mtime": mtime_str}))
    return rows


# --------------------------------------------------------------------------
# Surface 11b — which chief-of-staff bundle EXECUTES tonight (DEP-02).
#
# "Which version is deployed" was a question only a human could answer, by

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
    _version_key as _version_key,
)
