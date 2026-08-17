"""``brain doctor`` (ADR-0005 Ruling 2, DV-02) — READ-ONLY health + version
table across every Brainiac surface.

Pure inspection: this module never writes a file, never calls a subprocess
that mutates state, and never reaches the network beyond a local
``git rev-list`` against the already-cloned marketplace checkout (no fetch) —
EXCEPT the OPT-IN "PyPI registry drift" row (``run_doctor(registry_fetch=...)``
/ ``brain doctor --check-registry``), which is skipped by default (``None``)
and even then only ever calls an injected fetcher, never touches the network
directly in a fixture test. Safe to run anywhere, any number of times.

Status classes (ADR-0005 Ruling 2): every row gets exactly one of
``current | stale | unmanaged | manual-required | not-detectable | unknown``.
Only **scriptable REQUIRED** surfaces gate the process exit code — the
Desktop/Cowork plugin-skill store (surface 11) is always ``manual-required``
and never fails the run, otherwise `brain update`/CI could never go green
while an unscriptable surface stays stale.

Role-aware VM leg (2026-07-07 addendum, see docs/adr/0005-update-versioning-ux.md):
``run_doctor()`` above assumes a full host checkout (pyproject SSOT, ~/.brainiac
venv, ~/.claude plugins, tools/workspace_registry.py). None of that exists on
the Cowork VM's staged zero-install copy — ``run_doctor_vm()`` covers the
surfaces the VM CAN see (engine stamp, skill bundles, snapshot, model cache,
maintain heartbeat) and lists the rest as not-detectable host-only surfaces,
never a crash and never a fake-green.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

CURRENT = "current"
STALE = "stale"
UNMANAGED = "unmanaged"
MANUAL_REQUIRED = "manual-required"
NOT_DETECTABLE = "not-detectable"
UNKNOWN = "unknown"

# Surfaces whose `stale`/`unknown` verdict gates the process exit code
# (ADR-0005 Ruling 2: "Only scriptable REQUIRED surfaces may hard-fail").
_GATING_STATUSES = {STALE, UNKNOWN}


def _version_key(v: str):
    """``packaging.version.Version`` when available, else an integer-tuple
    fallback over the leading ``X.Y.Z`` digits (same semantics on the
    constrained semver shape this codebase uses — never a naive string
    compare, which fails at 0.9.1 -> 0.10.0). ponytail: no hard dependency —
    packaging is a transitive install today, not a declared one."""
    try:
        from packaging.version import Version

        return Version(v)
    except Exception:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v)
        if m:
            return tuple(int(x) for x in m.groups())
        return (0, 0, 0)


def _compare(a: str, b: str) -> int:
    """-1 / 0 / 1 for a<b / a==b / a>b, tolerant of non-semver strings."""
    ka, kb = _version_key(a), _version_key(b)
    try:
        if ka < kb:
            return -1
        if ka > kb:
            return 1
        return 0
    except TypeError:  # mixed Version/tuple types after a parse failure
        sa, sb = str(a), str(b)
        return -1 if sa < sb else (1 if sa > sb else 0)


def _row(
    surface: str,
    status: str,
    detail: str,
    *,
    remediation: Optional[str] = None,
    raw: Optional[dict] = None,
) -> dict:
    return {
        "surface": surface,
        "status": status,
        "detail": detail,
        "remediation": remediation,
        "raw": raw or {},
    }


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def marketplace_install_location(claude_home: Path, name: str = "brainiac") -> Optional[Path]:
    """The one already-persisted authoritative pointer to the marketplace's
    on-disk checkout (RC1/RC3/RC4). ``~/.claude/plugins/known_marketplaces.json``
    is a FLAT dict keyed by marketplace name (verified on-machine: no
    "marketplaces" wrapper — same read as ``check_stale_name_plugins``), each
    value carrying an ``installLocation``. A directory-source marketplace (this
    machine: ``installLocation`` == the engine checkout) records the REAL path
    there, so reading it fixes three bugs at once: the hardcoded
    ``marketplaces/<name>`` guess that read a directory-source install as "not
    installed" (RC3), the ``__file__``-inference that mislocated ``repo_root``
    on a wheel install (RC1/RC4), and the ``~/brainiac`` engine-src fallback
    (RC1). Returns ``None`` when the file, the key, or the dir is absent —
    every caller keeps its own fallback."""
    known = _read_json(claude_home / "plugins" / "known_marketplaces.json")
    if not isinstance(known, dict):
        return None
    entry = known.get(name)
    if not isinstance(entry, dict):
        return None
    loc = entry.get("installLocation")
    if not loc:
        return None
    p = Path(str(loc)).expanduser()
    return p if p.is_dir() else None


# --------------------------------------------------------------------------
# Surface 1 — Version SSOT (pyproject.toml)
# --------------------------------------------------------------------------

def _ssot_version(repo_root: Path) -> Optional[str]:
    pyproject = repo_root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Surface 2 — committed src/brain/_version.py stamp
# --------------------------------------------------------------------------

def check_committed_stamp(repo_root: Path, ssot: str) -> dict:
    stamp_path = repo_root / "src" / "brain" / "_version.py"
    if not stamp_path.exists():
        return _row("Committed stamp (src/brain/_version.py)", UNKNOWN,
                    "stamp file missing",
                    remediation="python tools/package_clients.py")
    text = stamp_path.read_text(encoding="utf-8")
    m = re.search(r'(?m)^__version__ = "([^"]+)"$', text)
    if not m:
        return _row("Committed stamp (src/brain/_version.py)", UNKNOWN,
                    "no __version__ line found",
                    remediation="python tools/package_clients.py")
    stamped = m.group(1)
    if stamped == ssot:
        return _row("Committed stamp (src/brain/_version.py)", CURRENT,
                    f"{stamped} == SSOT {ssot}", raw={"stamped": stamped})
    return _row("Committed stamp (src/brain/_version.py)", STALE,
                f"{stamped} != SSOT {ssot}",
                remediation="python tools/package_clients.py",
                raw={"stamped": stamped})


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
    inst_v, ssot_v = _version_tuple(installed), _version_tuple(ssot)
    if inst_v and ssot_v and inst_v > ssot_v:
        return _row(
            "Host engine venv", STALE,
            f"THIS ENGINE is behind the installed one — running {ssot}, installed is "
            f"{installed} (channel: {channel}). The venv is fine; whatever pinned this "
            f"engine (e.g. a scheduled job's $BRAIN_BIN) is stale.",
            remediation="repoint the pin at the current engine — check $BRAIN_BIN in "
                        "~/Library/LaunchAgents/com.brainiac.*.plist, and stage a fresh "
                        "one with tools/cos_canary_install.sh <version> if needed. Do NOT "
                        "run /brainiac-update: the installed venv is already newer.",
            raw={"installed": installed, "running": ssot, "channel": channel,
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
# knowing which of two surfaces to look at. Getting that wrong produced two
# false freeze alarms (runs 37 and 55): a pin ahead of the deployment silently
# freezes every gated phase, and a readback pointed at the non-executing
# surface manufactures the opposite remediation with confidence. This makes the
# answer one command, from the SAME lane-resolution the run manifest stamps
# with (`brain.cos_deploy`) — never a second copy of the rules.
# --------------------------------------------------------------------------

def check_cos_deployed_skill() -> dict:
    """Lane-aware: what the next COS nightly will load, or why we can't tell.

    NEVER gates the exit code. Most installs have no chief-of-staff deployment
    at all, and an unresolved lane there is the correct, healthy answer — a
    gating status would turn every such host falsely DEGRADED (the 2026-07-20
    field failure this project has already paid for once).
    """
    surface = "COS deployed skill (executing lane)"
    try:
        from . import cos_deploy
    except Exception as exc:  # pragma: no cover - import guard only
        return _row(surface, NOT_DETECTABLE, f"cos_deploy unavailable ({exc})")
    try:
        info = cos_deploy.deployed_skill()
    except cos_deploy.LaneUnresolved as exc:
        return _row(surface, NOT_DETECTABLE, str(exc))
    except Exception as exc:
        return _row(surface, NOT_DETECTABLE, f"readback failed ({exc})")

    version = info.get("bundle_version") or "(unversioned)"
    ext = info.get("extraction_rules_version") or "-"
    detail = (f"{info['lane']} → {version} (extraction rules {ext}), "
              f"sha {info['sha256'][:12]} — {info['path']}")
    # The other surface is REPORTED, never counted. Naming it here is the whole
    # point: it is what someone reads by mistake.
    try:
        store = cos_deploy.from_skill_store()
        codex = cos_deploy.from_codex_automations()
        support = cos_deploy.cowork_support(store, codex)
    except Exception:
        support = {"supported": True, "store_versions": []}
    remediation = None
    if not support["supported"]:
        held = ", ".join(support.get("store_versions") or []) or "(no bundle)"
        detail += (f"; the Claude Desktop skill store holds {held} and is "
                   "RETIRED as a version source — it does not execute")
        remediation = ("if you want that surface usable again, upload the "
                       "current bundle in Claude Desktop (owner-only click); "
                       "until then readbacks of it return UNSUPPORTED")
    return _row(surface, CURRENT, detail, remediation=remediation,
                raw={"lane": info["lane"], "version": info.get("bundle_version"),
                     "extraction_rules_version": info.get("extraction_rules_version"),
                     "sha256": info["sha256"], "path": info["path"],
                     "cowork_surface_supported": support["supported"]})


def _staged_payload_rows(registry_entries: list[dict], ssot: str) -> list[dict]:
    """Every versioned payload staged INTO a workspace, in report order: the
    skill bundles, then the frozen VM binaries. Both answer the same question
    — does what we staged match SSOT — so they are read together."""
    return (check_staged_skill_bundles(registry_entries, ssot)
            + check_staged_vm_binaries(registry_entries, ssot))


def check_workspace_schema(registry_entries: list[dict], binary_schema_version: int) -> list[dict]:
    rows = []
    for entry in registry_entries:
        if entry.get("target") == "host":
            continue
        vault_dir = _cowork_vault_dir(entry)
        snap_meta = Path(vault_dir) / ".brain" / "snapshot" / "snapshot.manifest.json"
        surface = f"Snapshot schema ({vault_dir})"
        data = _read_json(snap_meta)
        if data is None:
            rows.append(_row(surface, NOT_DETECTABLE, f"{snap_meta} not found"))
            continue
        stored = data.get("schema_version")
        try:
            stored_int = int(stored)
        except (TypeError, ValueError):
            rows.append(_row(surface, UNKNOWN, f"schema_version unreadable: {stored!r}"))
            continue
        if stored_int == binary_schema_version:
            rows.append(_row(surface, CURRENT, f"schema {stored_int} == binary {binary_schema_version}",
                             raw={"schema_version": stored_int}))
        elif stored_int > binary_schema_version:
            rows.append(_row(surface, STALE,
                             f"snapshot schema {stored_int} > binary {binary_schema_version} "
                             "(binary is OLDER than the snapshot — refresh the engine, don't rebuild down)",
                             remediation="/brainiac-update", raw={"schema_version": stored_int}))
        else:
            rows.append(_row(surface, STALE,
                             f"snapshot schema {stored_int} < binary {binary_schema_version} (stale snapshot)",
                             remediation="brain snapshot (on the host, then re-stage)",
                             raw={"schema_version": stored_int}))
    return rows


# --------------------------------------------------------------------------
# Surface — marketplace CACHE freshness (local git rev-list only, no fetch).
# Deliberately separate from "published-marketplace freshness" per hardening:
# a local checkout that hasn't been refreshed must never be reported CURRENT
# just because it matches its own stale HEAD.
# --------------------------------------------------------------------------

def check_marketplace_cache(marketplace_dir: Path) -> dict:
    surface = "Marketplace cache freshness"
    if not (marketplace_dir / ".git").exists():
        return _row(surface, NOT_DETECTABLE, f"{marketplace_dir} is not a git checkout")
    try:
        head = subprocess.run(
            ["git", "-C", str(marketplace_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        upstream = subprocess.run(
            ["git", "-C", str(marketplace_dir), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if not upstream:
            return _row(surface, NOT_DETECTABLE, "no upstream tracking branch configured", raw={"head": head})
        behind = subprocess.run(
            ["git", "-C", str(marketplace_dir), "rev-list", "--count", f"HEAD..{upstream}"],
            capture_output=True, text=True, timeout=15,
        )
        if behind.returncode != 0:
            return _row(surface, UNKNOWN, behind.stderr.strip() or "git rev-list failed")
        count = int(behind.stdout.strip() or "0")
    except Exception as exc:
        return _row(surface, UNKNOWN, f"{type(exc).__name__}: {exc}")
    # HARDEN:codex-HIGH — this is LOCAL cache state only (no fetch was run),
    # so "0 commits behind the last-known origin ref" is NOT the same claim as
    # "current vs what's actually published". Never collapse the two.
    if count == 0:
        return _row(surface, CURRENT,
                    "0 commits behind local cache of origin — cache not refreshed this run; "
                    "run `brain update`/`git fetch` to compare against published",
                    raw={"commits_behind_cache": 0})
    return _row(surface, STALE,
                f"{count} commit(s) behind local cache of origin (cache not refreshed — "
                "run `brain update` to pull and compare against published)",
                remediation="git -C <marketplace-dir> pull  # or: /brainiac-update",
                raw={"commits_behind_cache": count})


# --------------------------------------------------------------------------
# Registry-drift visibility (PyPI publish addendum). OPT-IN ONLY (see
# run_doctor's ``registry_fetch`` param) — this is the one surface allowed to
# touch the network, and even then only via an injected fetcher, a single
# cached HTTPS metadata read, never by default and never inside a fixture
# test. Compares three numbers: the repo's latest git release tag, the
# locally-installed engine version, and the latest published PyPI version.
# Degrades to NOT_DETECTABLE/UNKNOWN silently offline — never raises, never
# gates (informational: a human decides whether "marketplace ahead of
# published engine" matters right now).
# --------------------------------------------------------------------------

def _latest_git_tag(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line:
            return line.lstrip("v")
    return None


def fetch_pypi_latest_version(dist_name: str = "brainiac-cli", timeout: float = 3.0) -> Optional[str]:
    """Real HTTPS fetcher — the one function in this module allowed to reach
    the network, and only ever called when a caller explicitly opts in
    (``brain doctor --check-registry``). Any failure (offline, DNS, 404
    pre-publish) degrades to ``None``, never an exception."""
    import urllib.request

    url = f"https://pypi.org/pypi/{dist_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("info", {}).get("version")
    except Exception:
        return None


def check_pypi_registry_drift(
    repo_root: Path, installed_version: str, *, fetch: Callable[[], Optional[dict]],
) -> dict:
    """``fetch`` returns a dict ``{"pypi_version": str|None}`` (or None on
    total failure) — injected so this stays testable without a live network
    call; ``brain doctor --check-registry`` wires up a real fetcher built on
    ``fetch_pypi_latest_version``."""
    surface = "PyPI registry drift"
    repo_tag = _latest_git_tag(repo_root)
    try:
        result = fetch() or {}
    except Exception as exc:
        return _row(surface, NOT_DETECTABLE, f"fetch failed: {type(exc).__name__}: {exc}",
                    raw={"repo_tag": repo_tag, "installed": installed_version})
    pypi_version = result.get("pypi_version")
    if pypi_version is None:
        return _row(surface, NOT_DETECTABLE,
                    "no PyPI metadata (offline, or brainiac-cli not yet published — "
                    "use the clone/dev install until it is)",
                    raw={"repo_tag": repo_tag, "installed": installed_version})
    detail = (f"repo tag {repo_tag or 'none'} / installed {installed_version} / "
              f"PyPI latest {pypi_version}")
    if _compare(repo_tag or "0.0.0", pypi_version) > 0 or _compare(installed_version, pypi_version) > 0:
        return _row(surface, UNMANAGED,
                    f"{detail} — marketplace/skills are AHEAD of the published PyPI engine; "
                    "do not publish clean-room export docs referencing an unpublished version",
                    raw={"repo_tag": repo_tag, "installed": installed_version, "pypi": pypi_version})
    return _row(surface, CURRENT if installed_version == pypi_version else UNMANAGED, detail,
                raw={"repo_tag": repo_tag, "installed": installed_version, "pypi": pypi_version})


# --------------------------------------------------------------------------
# VM leg (role-aware doctor, 2026-07-07 addendum to ADR-0005 Ruling 2) — the
# Cowork VM only ever sees the staged zero-install copy
# (cowork_workspace_install.sh: src/brain -> .brain/engine/brain, plus
# .brain/{skills,snapshot,model,maintain-state.json}). None of the HOST-only
# surfaces above (venv, pyproject SSOT, ~/.claude plugins, marketplace clone,
# Desktop store, tools/workspace_registry.py) exist there. These checks read
# ONLY what the staged workspace itself carries.
# --------------------------------------------------------------------------

def looks_like_vm_stage(repo_root: Optional[Path] = None) -> bool:
    """True when this engine copy structurally lacks the host-only inputs
    (no ``tools/workspace_registry.py`` companion script, no ``pyproject.toml``
    SSOT) — i.e. it is a staged zero-install copy, even when role wasn't
    explicitly passed. The staged VM shim (``.brain/brain``) runs
    ``python3 -m brain.cli "$@"`` directly and does not set ``$BRAIN_ROLE``, so
    this structural fallback is what keeps a role-less VM invocation from
    hitting the host-only code path."""
    root = repo_root or Path(__file__).resolve().parent.parent.parent
    # POSITIVE signal first: the staged copy is written to
    # `<vault>/.brain/engine/brain/` by tools/cowork_workspace_install.sh, so
    # a `.brain` path component IS the stage -- unambiguous, and true of
    # nothing else.
    #
    # The absence-based test below cannot stand alone: an ORDINARY PyPI wheel
    # in site-packages also lacks tools/ and pyproject.toml, so every
    # pip/uv/pipx install was misdetected as a VM stage and `brain doctor`
    # ran the VM leg -- telling a Windows laptop user to "run brain doctor on
    # the host Mac" and diagnosing a Cowork workspace that did not exist
    # (enterprise pilot, 2026-07-29). Keep it only where it is safe: a copy that is
    # neither an installed package nor a checkout.
    if ".brain" in root.parts or ".brain" in Path(__file__).resolve().parts:
        return True
    if _in_site_packages(Path(__file__).resolve()):
        return False
    return not (root / "tools" / "workspace_registry.py").exists() and _ssot_version(root) is None


def _in_site_packages(path: Path) -> bool:
    """True when this module was imported from an installed package tree
    (POSIX ``lib/pythonX.Y/site-packages`` or Windows ``Lib\\site-packages``),
    rather than a source checkout or a staged copy."""
    parts = {p.lower() for p in path.parts}
    return bool(parts & {"site-packages", "dist-packages"})


def _read_version_stamp(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.search(r'(?m)^__version__ = "([^"]+)"$', text)
    return m.group(1) if m else None


def check_vm_engine_stamp(engine_version: str) -> dict:
    surface = "Engine version (this staged copy)"
    if engine_version.startswith("0.0.0"):
        return _row(surface, STALE, f"brain.__version__ reads {engine_version!r} — stale/pre-stamp stage",
                    remediation="re-stage from the host: tools/cowork_workspace_install.sh",
                    raw={"version": engine_version})
    return _row(surface, CURRENT, f"brain {engine_version}", raw={"version": engine_version})


def check_vm_snapshot(vault: Path) -> dict:
    from . import config
    from .snapshot import snapshot_status

    surface = "Snapshot (read-only, .brain/snapshot)"
    snap_dir = config.snapshot_dir(vault)
    st = snapshot_status(snap_dir)
    if st.get("snapshot") != "present":
        return _row(surface, NOT_DETECTABLE, f"no snapshot published at {snap_dir}",
                    remediation="publish a snapshot on the host (`brain snapshot`) and re-sync the VM mount")
    age_s = st.get("age_seconds") or 0.0
    detail = (f"gen {st.get('generation')} age {st.get('age_human')} "
              f"({st.get('notes')} notes / {st.get('chunks')} chunks)")
    if age_s > 48 * 3600:
        return _row(surface, STALE, f"{detail} — older than 48h",
                    remediation="publish a fresh snapshot on the host (`brain snapshot`) and re-sync the VM mount",
                    raw=st)
    return _row(surface, CURRENT, detail, raw=st)


def check_vm_model_cache(vault: Path) -> dict:
    from . import config

    surface = "Model cache (.brain/model)"
    model_dir = Path(os.environ.get("BRAIN_MODEL_CACHE") or (config.brain_runtime_dir(vault) / "model"))
    if not model_dir.is_dir() or not any(model_dir.iterdir()):
        return _row(surface, STALE,
                    f"{model_dir} missing/empty — the VM has no HF egress, so semantic search "
                    "silently falls back to hash embeddings without this",
                    remediation="re-stage from the host: tools/cowork_workspace_install.sh")
    # A dangling symlink is not is_file(): an HF-cache snapshot staged without
    # dereferencing (field finding 2026-07-20, F1) looks "present" while every
    # file points at a blobs/ dir that was never copied.
    dangling = [p for p in model_dir.rglob("*") if p.is_symlink() and not p.exists()]
    if dangling:
        return _row(surface, STALE,
                    f"{model_dir} has {len(dangling)} dangling symlink(s) (e.g. {dangling[0].name}) — "
                    "the HF-cache snapshot was staged without dereferencing its blobs/ links",
                    remediation="re-stage with resolved copies: cp -RL <hf-snapshot>/. into the model dir "
                                "(tools/cowork_workspace_install.sh now does this)")
    n_files = sum(1 for p in model_dir.rglob("*") if p.is_file())
    if not any(p.name.startswith("model") and p.suffix == ".onnx" and p.stat().st_size > 1_000_000
               for p in model_dir.rglob("*.onnx")):
        return _row(surface, STALE,
                    f"{model_dir} present ({n_files} file(s)) but no model*.onnx >1MB — cache is incomplete",
                    remediation="re-stage from the host: tools/cowork_workspace_install.sh")
    return _row(surface, CURRENT, f"{model_dir} present ({n_files} file(s))")


# The pinned Cowork-VM interpreter (field finding 2026-07-18: cp311 wheels
# staged for the 3.10-only VM caused a 10-run EmbedderUnavailable outage).
# Keep in lockstep with tools/vendor_semantic_deps.py's VM_PYTHON.
_VM_PYTHON = (3, 10)

# Vendored-deps ABI check lives in doctor_vendor.py since 2026-08-15 (size ratchet);
# re-exported here so every existing caller keeps importing it from doctor.
from .doctor_vendor import (  # noqa: E402  (re-export)
    check_vendor_abi,
    _prune_retired_dirs,  # noqa: F401  (public re-export; existing callers use doctor._prune_retired_dirs)
    _running_vendor_arch,
)

# Staged VM binaries live in vmstaging.py since 2026-08-16 — same reason and
# same shape as the doctor_vendor split above (size ratchet). Re-exported so the
# row reads like every other staged-artifact surface.
from .vmstaging import (  # noqa: E402,F401  (re-exports — callers and
    # tests import these from `doctor`, which stays their one address
    # even though the code now lives beside its VM-staging siblings.
    _cowork_vault_dir,
    check_staged_skill_bundles,
    check_staged_vm_binaries,
    check_staged_workspaces,
)


def check_embedder_liveness() -> dict:
    """Probe whether the LIVE runtime can produce real semantic embeddings, or
    would silently degrade to the non-semantic HashEmbedder (DV-03, 2026-07-09).

    This is the one health surface older `brain doctor` was structurally blind
    to: version / schema / staging / model-files can ALL read green while
    `search` returns random results because onnxruntime isn't importable in the
    interpreter that actually runs `brain` (the exact Cowork-VM failure that
    lost a retrieval eval to a hash fallback). Note this is distinct from
    ``check_vm_model_cache`` — the model files can be present on disk yet the
    runtime still unable to load them. The cheap import probe stays read-only;
    when it passes, we then exercise a REAL 1-token query embed (a model load,
    no vault/index side effects) so "available" cannot false-green — the exact
    Cowork-VM gap where onnxruntime imported, model files were present, yet
    query-embed died because the model dir wasn't found."""
    from .embed import probe_auto_embedder

    surface = "Semantic embedder (live runtime)"
    state, backend = probe_auto_embedder()
    if state == "real":
        # The import probe only proves onnxruntime/tokenizers load — NOT that the
        # model resolves and embeds. Run an actual query embed to verify.
        try:
            from .embed import get_embedder

            vec = get_embedder("onnx").embed("probe", is_query=True)
            if not vec:
                raise RuntimeError("embed returned an empty vector")
            return _row(surface, CURRENT,
                        f"verified — a live query embed succeeded ({backend}, dim={len(vec)})",
                        raw={"state": state, "backend": backend, "probe_dim": len(vec)})
        except Exception as exc:
            return _row(surface, STALE,
                        f"onnxruntime imports but a REAL query embed FAILED "
                        f"({type(exc).__name__}: {exc}) — semantic search is dead "
                        f"despite the runtime looking present",
                        remediation="set $BRAIN_MODEL_CACHE to the staged model dir "
                                    "(.brain/model with onnx/model.onnx + tokenizer.json) "
                                    "or run `brain warmup`; then re-run `brain doctor`",
                        raw={"state": state, "backend": backend,
                             "error": f"{type(exc).__name__}: {exc}"})
    if state == "explicit-hash":
        # Deliberate offline/test choice — never gates, never alarms.
        return _row(surface, UNMANAGED,
                    "hash embedder selected explicitly ($BRAIN_EMBEDDER=hash) — "
                    "retrieval is non-semantic BY CHOICE, not a fault",
                    raw={"state": state, "backend": backend})
    # implicit-hash — the silent random-search failure. Gate the exit code.
    return _row(surface, STALE,
                "NO real semantic embedder — the auto-path would fall back to the "
                "non-semantic HashEmbedder, so `search` ranks with RANDOM vectors "
                "against a real-model index",
                remediation="install onnxruntime + tokenizers into the interpreter that "
                            "runs `brain` (the 'corporate' extras), or invoke the "
                            "onnxruntime-bundled frozen binary; then re-run `brain status`",
                raw={"state": state, "backend": backend})


def check_vm_maintain_heartbeat(vault: Path) -> dict:
    """VM-readable mirror of ``BrainCore._maintain_heartbeat_summary`` (the VM
    can read the heartbeat file even though only the host ever runs
    ``brain maintain``)."""
    import datetime as _dt

    from . import config

    from . import maintenance as maint

    surface = "Maintain heartbeat (.brain/maintain-state.json)"
    state = _read_json(config.maintain_state_path(vault))
    if not state:
        return _row(surface, NOT_DETECTABLE,
                    "no maintain-state.json yet — brain maintain (host-only ritual) has not run")
    today = _dt.date.today()
    stale, repeated = [], []
    escalated: list[str] = []  # ES-01: liveness + stuck-writer-lock, see maintenance.branch_escalation
    for branch, entry in state.items():
        if str(branch).startswith("_") or not isinstance(entry, dict):
            continue  # marker (e.g. "_retention"), not a branch
        last_run = entry.get("last_run")
        age_hours: Optional[float] = None
        if last_run:
            try:
                age_hours = (today - _dt.date.fromisoformat(last_run)).days * 24
            except ValueError:
                age_hours = None
        if branch == "daily" and (entry.get("failed") or (age_hours is not None and age_hours > 48)):
            stale.append(branch)
        # UNCHANGED gate: >=2 consecutive failures (a PULL surface stays
        # cheaply noisy at the pre-existing threshold — never raised to 3).
        if int(entry.get("consecutive_failures", 0) or 0) >= 2:
            repeated.append(branch)
        # ES-01 ADDITION: liveness (stale last_run) and a leaked writer-lock
        # skip streak — neither is visible to the consecutive_failures counter
        # above, because a process that never runs never increments it.
        esc = maint.branch_escalation(branch, entry, today)
        if esc["escalate"]:
            escalated.append(f"{branch} ({'; '.join(esc['reasons'])})")
    if stale:
        return _row(surface, STALE, f"stale branch(es): {stale}",
                    remediation="brain maintain runs host-side only — check the host's nightly scheduler")
    if repeated:
        return _row(surface, STALE, f"repeated-failure branch(es): {repeated}",
                    remediation="check the host's nightly maintenance logs")
    if escalated:
        return _row(surface, STALE, f"escalated branch(es): {escalated}",
                    remediation="check the host's nightly maintenance logs / a stuck writer lock")
    return _row(surface, CURRENT, f"{len(state)} branch(es) tracked, none stale/repeatedly-failing")


def check_corpus_invariants(vault: Path) -> dict:
    """WAT-01 dead-man's switch, lane 1: is the corpus-invariants watchdog
    actually alive, and is anything regressing?

    A dead fold cannot report its own death, so this reads the persisted
    maintain-state row from OUTSIDE the nightly — `brain doctor` is run ad
    hoc, by `brain health-report`, and by the weekly synthesis watchdog.
    STALE (the gating status) when the row is missing on a vault whose other
    branches run, when it has gone older than
    ``$BRAIN_INVARIANTS_MAX_AGE_DAYS`` (default 3), or when the last run
    recorded a regression past a metric's ratcheted floor."""
    from . import config
    from . import invariants as inv

    surface = "Corpus invariants watchdog (WAT-01)"
    state = _read_json(config.maintain_state_path(vault))
    if not state:
        return _row(surface, NOT_DETECTABLE,
                    "no maintain-state.json yet — brain maintain has not run here")
    live = inv.liveness_finding(state)
    if live:
        return _row(surface, STALE, live[1],
                    remediation="brain maintain   # then re-check; if the row stays "
                                "missing, this engine build predates WAT-01 — restage it",
                    raw={"age_days": inv.invariants_age_days(state),
                         "max_age_days": inv.max_age_days()})
    regs = inv.state_regressions(state)
    if regs:
        return _row(surface, STALE,
                    "; ".join(str(r.get("summary")) for r in regs),
                    remediation="brain health-report   # 'Corpus invariants' section",
                    raw={"regressions": regs})
    entry = state.get(inv.STATE_KEY) if isinstance(state.get(inv.STATE_KEY), dict) else {}
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    values = inv.metric_values(metrics)
    return _row(surface, CURRENT,
                f"last run {entry.get('last_run', '?')}, no regression "
                f"({', '.join(f'{k}={v}' for k, v in sorted(values.items())) or 'no values yet'})",
                raw={"values": values, "floors": entry.get("floors")})


def check_audit_content_drift(vault: Path) -> dict:
    """INT-02: notes whose bytes changed after the audit chain signed them.

    HOST surface, and deliberately key-free: ``content_drift`` only re-hashes
    files against hashes already in the log, so this row costs one vault read
    and never resolves the signing key (``verify-audit`` does that separately).

    Only UNEXPLAINED drift gates. Drift a human triaged into the disposition
    file is reported in the detail and subtracted from the verdict — pinned to
    the bytes it was ruled on, so the same file changing again comes straight
    back as unexplained. That file lives in the HOST-PRIVATE app-data dir since
    2026-08-07 (``config.audit_drift_dispositions_path``); on the old
    ``.brain/`` path a Cowork VM could write it and zero out this very row."""
    from . import audit as _audit
    from . import config

    surface = "Audit content drift (signed notes vs disk)"
    log_path = config.index_dir(vault) / "audit_chain.jsonl"
    if not log_path.is_file():
        return _row(surface, NOT_DETECTABLE, f"no audit chain at {log_path}")
    try:
        summary = _audit.drift_summary(Path(vault), _audit.AuditChain(log_path))
    except Exception as exc:  # noqa: BLE001 — an unreadable chain is a surface gap, not a crash
        return _row(surface, UNKNOWN, f"could not check drift: {type(exc).__name__}: {exc}",
                    remediation="brain verify-audit --check-content --json")
    total, unexplained = summary["total"], summary["unexplained"]
    explained = total - unexplained
    if unexplained:
        return _row(
            surface, STALE,
            f"{unexplained} signed note(s) changed after signing with no recorded "
            f"disposition ({explained} triaged, {total} total)",
            remediation="brain verify-audit --check-content --json  # then triage into "
                        "the host-private disposition file (brain doctor --json shows "
                        "its path) or restore the note",
            raw={"total": total, "unexplained": unexplained})
    detail = ("no drift — every signed note matches its signed bytes" if not total
              else f"0 unexplained ({explained} triaged historical drift record(s))")
    return _row(surface, CURRENT, detail, raw={"total": total, "unexplained": 0})


#: Quarantine buckets whose cause is an operator action on THIS host, not a
#: judgement call about the file. Named in the row so the fix is one step away.
_RECOVERABLE_BUCKETS = {
    "pdf_no_text_layer": "scanned PDF — needs the local OCR engine",
    "empty_or_low_text_density": "no text found — often a picture-only deck, needs OCR",
    "pdf_encrypted": "needs a password (a permissions-only file now opens by itself)",
}


def check_ingest_capability() -> dict:
    """Can this host actually READ the formats dropped into `inbox/`?

    A missing handler or a missing local OCR engine does not fail loudly at
    ingest time — the file is quarantined and the drop zone looks empty. This
    row makes the capability visible BEFORE anything is dropped, on a new
    vault as much as an old one."""
    from .ingest.handlers import capability_report
    from .ingest.handlers.base import ocr_lang

    surface = "Ingestion capability (handlers + local OCR)"
    try:
        caps = capability_report()
    except Exception as exc:  # noqa: BLE001 — a probe failure is a surface gap
        return _row(surface, UNKNOWN, f"could not probe handlers: {type(exc).__name__}: {exc}")
    missing = sorted({c["dependency"] for c in caps.values() if not c["available"]})
    lang = ocr_lang()
    raw = {"handlers_missing": missing, "ocr_languages": lang}
    if missing:
        return _row(surface, STALE,
                    f"{len(missing)} extraction dependency missing: {', '.join(missing)} — "
                    "files of those types will be quarantined, not ingested",
                    remediation=f"pip install {' '.join(missing)}  # into the engine's venv",
                    raw=raw)
    if lang is None:
        # Reported on every run, never silent — but NOT gating. OCR is an
        # optional LOCAL engine, so a host without it is unconfigured, not
        # broken, and gating here would paint every fresh install red before
        # a single document is dropped. What gates is real loss: the
        # "Quarantined drops" row above fires the moment a scan is actually
        # refused, and names this engine as the remedy.
        return _row(surface, UNMANAGED,
                    "no local OCR engine — a scanned PDF or a picture-only deck "
                    "cannot be read, and would be quarantined instead of ingested",
                    remediation="brew install tesseract tesseract-lang  # or the distro "
                                "package; then `pip install pytesseract` into the "
                                "engine's venv",
                    raw=raw)
    return _row(surface, CURRENT,
                f"every handler available; local OCR reads {lang}", raw=raw)


def check_quarantine(vault: Path) -> dict:
    """Documents the owner dropped in and did NOT get.

    A quarantined file is not in the vault and is not retrievable, and until
    2026-08-17 nothing said so within the month it happened. Any live item
    gates this row; anything filed under a hand-triaged ``_resolved/`` subtree
    is a decision already taken and is reported separately, never as debt."""
    surface = "Quarantined drops (dropped in, not ingested)"
    qdir = Path(vault) / "inbox" / "_quarantine"
    if not qdir.is_dir():
        return _row(surface, CURRENT, "nothing quarantined", raw={"live": 0})
    live: dict[str, int] = {}
    resolved = 0
    try:
        for f in qdir.rglob("*"):
            if not f.is_file() or f.name.endswith(".reason.txt"):
                continue
            if f.name in {".DS_Store", "DISPOSITION.md"} or f.name.endswith(".RESOLVED.txt"):
                continue
            if any(p.name.startswith("_resolved") for p in f.relative_to(qdir).parents):
                resolved += 1
                continue
            live[f.relative_to(qdir).parts[0]] = live.get(f.relative_to(qdir).parts[0], 0) + 1
    except OSError as exc:
        return _row(surface, UNKNOWN, f"could not read {qdir}: {exc}")
    total = sum(live.values())
    raw = {"live": total, "by_reason": live, "triaged": resolved}
    if not total:
        detail = "nothing quarantined"
        if resolved:
            detail += f" ({resolved} item(s) hand-triaged under _resolved/)"
        return _row(surface, CURRENT, detail, raw=raw)
    parts = ", ".join(f"{n}x {r}" for r, n in sorted(live.items(), key=lambda kv: -kv[1]))
    hints = [f"`{r}`: {_RECOVERABLE_BUCKETS[r]}" for r in live if r in _RECOVERABLE_BUCKETS]
    return _row(surface, STALE,
                f"{total} dropped file(s) never reached the vault — {parts}",
                remediation=("; ".join(hints) + "; " if hints else "")
                            + f"inspect {qdir} and its .reason.txt sidecars, fix the "
                              "cause, then move each file back to `inbox/` and run "
                              "`brain sync`",
                raw=raw)


def check_query_capture(vault: Path) -> dict:
    """Host-only ADR-0008 S04 ledger liveness without reading query content.

    ``querylog.status`` uses file metadata and bounded line counts only.  It
    returns before resolving the host location on a VM, but this check is
    intentionally wired only into the host doctor surface: a VM must neither
    read nor claim to verify the raw-query ledger.
    """
    from . import querylog

    surface = "Host query capture ledger"
    info = querylog.status(vault, role="host")
    state = info.get("state")
    ledger = info.get("ledger") if isinstance(info.get("ledger"), dict) else {}
    failures = int(info.get("failures", 0) or 0)
    consecutive = int(info.get("consecutive_failures", 0) or 0)
    if state == "disabled":
        return _row(surface, UNMANAGED,
                    "capture disabled by BRAIN_QUERY_CAPTURE_ENABLED=0",
                    raw={"capture": info})
    if state in {"error", "stale"} or consecutive:
        reason = info.get("reason") or info.get("last_failure_code") or state
        # TWO faults, ONE instruction until 2026-08-17: `stale` means only "no
        # host query in N days" (an idle vault says it of itself), while
        # `error`/failures mean capture BROKE. Sending an idle vault's owner
        # after containment/permissions is a hunt for a fault that is not there.
        if state == "stale" and not failures and not consecutive:
            fix = (f"no host query captured for this vault in "
                   f"{info.get('stale_after_days')} day(s) — this is inactivity, "
                   "NOT a broken ledger. Run a host retrieval query against it, "
                   "or raise $BRAIN_QUERY_CAPTURE_STALE_DAYS if it is meant to "
                   "sit idle.")
        else:
            fix = ("fix host query-log containment/owner-only permissions, then run "
                   "three host retrieval queries; the VM never owns this ledger")
        return _row(
            surface, STALE,
            f"state={state}; files={ledger.get('files', 0)}; bytes={ledger.get('bytes', 0)}; "
            f"records={ledger.get('records', 0)}; "
            f"age_seconds={ledger.get('age_seconds')}; failures={failures}; reason={reason}",
            remediation=fix,
            raw={"capture": info},
        )
    if state == "idle":
        return _row(surface, NOT_DETECTABLE,
                    "no host query has been captured yet (no traffic to assess)",
                    raw={"capture": info})
    if state == "active":
        return _row(
            surface, CURRENT,
            f"files={ledger.get('files', 0)}; bytes={ledger.get('bytes', 0)}; "
            f"records={ledger.get('records', 0)}; age_seconds={ledger.get('age_seconds')}; "
            f"historical_failures={failures}",
            raw={"capture": info},
        )
    return _row(surface, UNKNOWN, f"unrecognised capture state: {state!r}", raw={"capture": info})


# Host-only surfaces the VM leg structurally cannot check (never gate, never
# claimed as checked — ADR-0005 Ruling 2/4: a NOT_DETECTABLE row here, not a
# fake-green or a crash).
_HOST_ONLY_SURFACES = (
    "Host engine venv (~/.brainiac/venv)",
    "Version SSOT / dist/COMPAT (pyproject.toml, dist/)",
    "Installed CLI plugins (~/.claude/plugins)",
    "Marketplace cache freshness (~/.claude/plugins/marketplaces)",
    "Desktop/Cowork plugin-skill store",
    "Workspace registry (tools/workspace_registry.py)",
    "Host query capture ledger",
)


def run_doctor_vm(vault: Optional[str | os.PathLike[str]] = None) -> dict[str, Any]:
    """Role-aware doctor for the Cowork VM leg — read-only, derived entirely
    from what the staged workspace itself carries. Never raises: every
    host-only import this needs is already isolated behind ``check_vm_*``
    helpers that only touch the vault's own ``.brain/`` tree."""
    from . import __version__ as engine_version
    from . import config
    from .index import SCHEMA_VERSION

    vault_path = config.vault_root(vault)
    entries = [{"vault_path": str(vault_path), "target": "vm"}]

    rows: list[dict] = [check_vm_engine_stamp(engine_version)]
    rows.extend(check_staged_skill_bundles(entries, engine_version))
    rows.extend(check_staged_vm_binaries(entries, engine_version))
    rows.extend(check_workspace_schema(entries, SCHEMA_VERSION))
    rows.append(check_vm_snapshot(vault_path))
    rows.append(check_vm_model_cache(vault_path))
    # 2026-07-18 field report: name a staged-ABI mismatch ("vendor is cp311 but
    # interpreter is 3.10") instead of a bare EmbedderUnavailable downstream.
    #
    # Judge against _VM_PYTHON, NOT the running interpreter (2026-07-25): this
    # vendor dir is staged to be imported by the COWORK VM's pinned 3.10, and by
    # nothing else. Passing sys.version_info made a host-side `brain doctor`
    # compare cp310 wheels against the host's 3.14 and report STALE
    # ("the vendored wheels cannot import here") for a correctly-staged vault —
    # a false DEGRADED that propagated into health-latest.html and out through
    # COS's brief header, which reads that verdict per contract. On the VM the
    # two are equal, so this changes nothing where the check actually applies.
    # Matches the run_doctor() call site below, which already used _VM_PYTHON.
    # Arch-restricted (2026-08-15 field report): only vendor/<running arch>/ can
    # be imported here (the shim PYTHONPATHs exactly one arch dir), and on the
    # VirtioFS mount the full-tree walk — including a _retired-*/ corpse yard
    # this check discards — never completed inside a VM call budget, leaving
    # this row set (and the WAT-01 lane that pastes it into the weekly
    # synthesis session) blind.
    rows.append(check_vendor_abi(config.brain_runtime_dir(vault_path) / "vendor",
                                 _VM_PYTHON, arch=_running_vendor_arch()))
    rows.append(check_embedder_liveness())  # DV-03: model files present ≠ embedder loads
    rows.append(check_vm_maintain_heartbeat(vault_path))
    # WAT-01 dead-man's switch, lane 2: the WEEKLY SYNTHESIS watchdog runs
    # `--role vm doctor` and pastes its rows into the session as DATA, so the
    # invariants row has to exist on THIS row set or that lane is blind. It
    # reads the same `maintain-state.json` the heartbeat row above already
    # reads — no key, no index, no host-only import.
    rows.append(check_corpus_invariants(vault_path))
    rows.extend(_row(s, NOT_DETECTABLE,
                     "requires `brain doctor` on the host Mac — not checkable from this staged VM copy")
                for s in _HOST_ONLY_SURFACES)

    gating_stale = [r for r in rows if r["status"] in _GATING_STATUSES]
    return {
        "role": "vm",
        "ssot_version": engine_version,
        "rows": rows,
        "ok": len(gating_stale) == 0,
        "stale_count": len(gating_stale),
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_doctor(
    *,
    repo_root: Optional[Path] = None,
    brainiac_home: Optional[Path] = None,
    claude_home: Optional[Path] = None,
    app_support_dir: Optional[Path] = None,
    registry_entries: Optional[list[dict]] = None,
    marketplace_dir: Optional[Path] = None,
    marketplace_name: str = "brainiac",
    registry_fetch: Optional[Callable[[], Optional[dict]]] = None,
    vault: Optional[str | os.PathLike[str]] = None,
) -> dict[str, Any]:
    """Run every ADR-0005 Ruling 2 surface check and return a report dict.

    All path-ish parameters default to the real machine locations but accept
    overrides — tests pass fixture directories so this NEVER needs the live
    machine to be exercised.

    ``registry_fetch`` is OPT-IN (default ``None`` = the row is skipped
    entirely, exactly the pre-S07 row set — zero risk to every existing
    fixture test and zero network in the default/test path). Pass a callable
    (see ``fetch_pypi_latest_version``) to add the "PyPI registry drift" row;
    ``brain doctor --check-registry`` wires up the real HTTPS fetcher.
    """
    from . import __version__ as _unused  # noqa: F401 (import proves module loads)
    from .index import SCHEMA_VERSION

    repo_root = repo_root or Path(__file__).resolve().parent.parent.parent
    brainiac_home = brainiac_home or Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
    claude_home = claude_home or (Path.home() / ".claude")
    if app_support_dir is None:
        # Platform-aware (win32/darwin/linux) — same resolver connect.py's
        # `brain connect --client claude-desktop` writer uses, instead of a
        # second macOS-only guess here. Desktop's own per-user data dir is
        # the parent of claude_desktop_config.json on every platform (Electron
        # userData convention: %APPDATA%\Claude on Windows, ~/Library/
        # Application Support/Claude on macOS) — this also fixes the
        # extensions-installations.json lookup (check_mcpb_desktop_collision /
        # check_desktop_plugin_store both key off this same dir).
        from . import connect

        app_support_dir = connect.claude_desktop_config_path().parent
    # RC1/RC3/RC4: the marketplace's real on-disk location comes from
    # known_marketplaces.json → installLocation (a directory-source
    # marketplace is NOT under marketplaces/<name>). Resolve it once and reuse
    # for both the marketplace-cache row and the installed-plugin rows; keep
    # the hardcoded path as the last fallback.
    resolved_marketplace = marketplace_install_location(claude_home, marketplace_name)
    marketplace_dir = (
        marketplace_dir
        or resolved_marketplace
        or (claude_home / "plugins" / "marketplaces" / marketplace_name)
    )

    registry_unavailable = False
    if registry_entries is None:
        import sys as _sys

        try:
            _sys.path.insert(0, str(repo_root / "tools"))
            import workspace_registry as _wr

            registry_entries = _wr.list_entries()
        except Exception:
            # HARDEN: `tools/workspace_registry.py` is a host-only companion
            # script — never part of the staged zero-install engine
            # (cowork_workspace_install.sh copies only src/brain). A staged
            # VM copy invoking `brain doctor` with role=host (e.g. the shim
            # doesn't set $BRAIN_ROLE) must degrade to "can't check this
            # surface", never crash with a raw ModuleNotFoundError.
            registry_entries = []
            registry_unavailable = True

    # Repo-oriented surfaces (SSOT/stamp/dist/plugin-manifests) only mean
    # anything on a DEV CHECKOUT. From an installed engine (dist venv, uv
    # tool, pipx) repo_root resolves inside site-packages, so those rows came
    # back UNKNOWN — a gating status — and every health report generated by a
    # pinned engine read falsely DEGRADED (field finding 2026-07-20, caught in
    # the v0.19.3 pre-restage verification). Without a checkout the honest
    # answer is NOT_DETECTABLE, and drift comparisons fall back to the running
    # engine's own version.
    # RC4: when __file__-inference lands somewhere without a pyproject (a wheel
    # install resolves repo_root inside site-packages), retry via the
    # marketplace installLocation — the real checkout on a directory-source
    # install. This turns a falsely "installed engine, no checkout" host back
    # into a full dev-checkout doctor run (staged-workspace rows, SSOT drift).
    if not (repo_root / "pyproject.toml").is_file():
        retry = resolved_marketplace
        if retry and (retry / "pyproject.toml").is_file() and (retry / "src" / "brain").is_dir():
            repo_root = retry

    is_dev_checkout = (repo_root / "pyproject.toml").is_file() and (repo_root / "src" / "brain").is_dir()

    ssot = _ssot_version(repo_root) if is_dev_checkout else None
    rows: list[dict] = []

    if not is_dev_checkout:
        from . import __version__ as _engine_version
        rows.append(_row(
            "Version SSOT (pyproject.toml)", NOT_DETECTABLE,
            f"no dev checkout at {repo_root} — installed engine "
            f"(running {_engine_version}); repo drift surfaces skipped",
        ))
        ssot = _engine_version or "0.0.0"
    elif ssot is None:
        rows.append(_row("Version SSOT (pyproject.toml)", UNKNOWN, "no version found in pyproject.toml"))
        ssot = "0.0.0"  # keeps downstream comparisons from crashing; every row above is UNKNOWN/errored
    else:
        rows.append(_row("Version SSOT (pyproject.toml)", CURRENT, ssot, raw={"version": ssot}))

    resolved_brain = shutil.which("brain")
    if is_dev_checkout:
        rows.append(check_committed_stamp(repo_root, ssot))
    rows.append(check_host_venv(brainiac_home, ssot,
                                resolved_brain=Path(resolved_brain) if resolved_brain else None))
    rows.append(check_embedder_liveness())  # DV-03: the host also builds/queries
    if is_dev_checkout:
        rows.append(check_dist_compat(repo_root, ssot))
        rows.extend(check_plugin_manifests(repo_root, ssot))
    rows.extend(check_installed_cli_plugins(claude_home, ssot, marketplace_name,
                                            marketplace_dir=marketplace_dir))
    rows.extend(check_stale_name_plugins(claude_home))
    if registry_unavailable:
        rows.append(_row(
            "Workspace registry (tools/workspace_registry.py)", NOT_DETECTABLE,
            "unavailable in this checkout — looks like a staged zero-install VM "
            "copy (tools/ is host-only, never staged); staged-workspace/skill-bundle "
            "rows below are skipped here",
            remediation="run `brain doctor --role vm` for the VM-appropriate surfaces, "
                        "or run this on the full host checkout"))
    rows.extend(check_staged_workspaces(registry_entries, ssot))
    # 2026-07-18 field report: report staged-vendor ABI vs the PINNED VM
    # interpreter (the host's own python is irrelevant to linux wheels) for
    # every registered cowork workspace that has a vendor dir.
    for entry in registry_entries:
        if entry.get("target") == "host":
            continue
        vdir = Path(_cowork_vault_dir(entry)) / ".brain" / "vendor"
        if vdir.is_dir():
            rows.append(check_vendor_abi(vdir, _VM_PYTHON))
    rows.extend(_staged_payload_rows(registry_entries, ssot))
    rows.extend(check_workspace_schema(registry_entries, SCHEMA_VERSION))
    # ES-01: the host is the only leg that actually RUNS `brain maintain`, so
    # this is where a repeated-failure/stuck-lock/stale branch must gate the
    # exit code — `check_vm_maintain_heartbeat` is vault-path-generic despite
    # its name (it just reads maintain-state.json), reused here per
    # registered host vault (a VM-target entry's own copy is checked by
    # ``run_doctor_vm`` instead, against ITS vault path — never double-gated).
    for entry in registry_entries:
        if entry.get("target") != "host":
            continue
        vp = entry.get("vault_path")
        if vp:
            rows.append(check_vm_maintain_heartbeat(Path(vp)))
    rows.append(check_marketplace_cache(marketplace_dir))
    # ADR-0008 S04: only the host may inspect raw-query ledger health. Prefer
    # the explicitly requested CLI vault; otherwise surface every registered
    # host vault that has a usable path. A missing/non-vault path remains a
    # NOT_DETECTABLE omission instead of turning generic `brain doctor` into a
    # false failure on a machine with no current vault selected.
    from . import config

    capture_vaults: list[Path] = []
    raw_vaults: list[Any] = [vault] if vault is not None else [
        entry.get("vault_path") for entry in registry_entries
        if entry.get("target") == "host" and entry.get("vault_path")
    ]
    # A local developer may have a perfectly valid CWD/BRAIN_VAULT without a
    # registry entry yet. Probe that conventional default as well; vault_root
    # fails closed and the exception below preserves the no-vault omission.
    if vault is None:
        raw_vaults.append(None)
    for raw_vault in raw_vaults:
        try:
            resolved = config.vault_root(raw_vault)
        except Exception:
            continue
        if resolved not in capture_vaults:
            capture_vaults.append(resolved)
    for capture_vault in capture_vaults:
        rows.append(check_query_capture(capture_vault))
        # INT-02: same host-only, per-vault surface — a signature-only
        # verify-audit "ok" must not be the only thing a health readout sees.
        rows.append(check_audit_content_drift(capture_vault))
        # WAT-01: and neither must a corpus-invariants fold that quietly
        # stopped running — checked here precisely because doctor does not
        # depend on the nightly having fired.
        rows.append(check_corpus_invariants(capture_vault))
        # 2026-08-17: a document dropped in and refused is invisible from
        # every other surface until the month turns. Per vault, and gating.
        rows.append(check_quarantine(capture_vault))
    # Host capability, not per-vault: one engine reads for every vault, and a
    # missing OCR engine silently refuses every scan dropped into any of them.
    rows.append(check_ingest_capability())

    if registry_fetch is not None:
        rows.append(check_pypi_registry_drift(repo_root, ssot, fetch=registry_fetch))
    rows.extend([check_mcpb_desktop_collision(app_support_dir), *check_mcp_vault_paths()])
    # DEP-02: which COS bundle runs tonight, from the executing lane.
    rows.append(check_cos_deployed_skill())
    # Surface 11 — always LAST, always manual-required, never gates.
    rows.extend(check_desktop_plugin_store(app_support_dir, ssot))

    gating_stale = [r for r in rows if r["status"] in _GATING_STATUSES]
    ok = len(gating_stale) == 0

    return {
        "ssot_version": ssot,
        "rows": rows,
        "ok": ok,
        "stale_count": len(gating_stale),
    }


_STATUS_ICON = {
    CURRENT: "✅",  # ✅
    STALE: "⚠️",  # ⚠️
    UNKNOWN: "⚠️",
    UNMANAGED: "ℹ️",  # ℹ️
    MANUAL_REQUIRED: "\U0001f6e0️",  # 🛠️
    NOT_DETECTABLE: "➖",  # ➖
}


def render_human(report: dict[str, Any]) -> str:
    lines = [f"brain doctor — SSOT version {report['ssot_version']}", ""]
    surface_w = max((len(r["surface"]) for r in report["rows"]), default=8) + 2
    status_w = 16
    for r in report["rows"]:
        icon = _STATUS_ICON.get(r["status"], "?")
        line = f"{icon} {r['surface']:<{surface_w}}{r['status']:<{status_w}}{r['detail']}"
        lines.append(line)
        if r.get("remediation"):
            lines.append(f"    -> fix: {r['remediation']}")
    lines.append("")
    if report["ok"]:
        lines.append(f"OK: all required surfaces current ({len(report['rows'])} checked)")
    else:
        lines.append(f"STALE: {report['stale_count']} required surface(s) need attention (see -> fix above)")
    return "\n".join(lines)


def _demo() -> None:
    """ponytail self-check: an all-fixture run classifies every row and the
    exit-code gate only counts stale/unknown scriptable rows, never the
    always-manual-required Desktop row."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pyproject.toml").write_text('version = "1.2.3"\n', encoding="utf-8")
        brain_dir = root / "src" / "brain"
        brain_dir.mkdir(parents=True)
        (brain_dir / "_version.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
        for pname in PLUGIN_NAMES:
            pdir = root / "plugins" / pname / ".claude-plugin"
            pdir.mkdir(parents=True)
            (pdir / "plugin.json").write_text(json.dumps({"name": pname, "version": "1.2.3"}), encoding="utf-8")
        claude_home = root / "claude_home"
        app_support = root / "app_support"
        report = run_doctor(
            repo_root=root, brainiac_home=root / "brainiac_home",
            claude_home=claude_home, app_support_dir=app_support,
            registry_entries=[],
            marketplace_dir=claude_home / "plugins" / "marketplaces" / "brainiac",
        )
        assert report["ssot_version"] == "1.2.3"
        stamp_row = next(r for r in report["rows"] if "Committed stamp" in r["surface"])
        assert stamp_row["status"] == CURRENT
        desktop_rows = [r for r in report["rows"] if "Desktop/Cowork" in r["surface"]]
        assert all(r["status"] == MANUAL_REQUIRED for r in desktop_rows)
        # Manual-required rows never gate the exit code even though found.
        assert report["stale_count"] == sum(
            1 for r in report["rows"] if r["status"] in _GATING_STATUSES
        )
        text = render_human(report)
        assert "brain doctor" in text
    print("OK: doctor self-check passed")


if __name__ == "__main__":
    _demo()
