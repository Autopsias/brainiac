"""``brain doctor`` (ADR-0005 Ruling 2, DV-02) — READ-ONLY health + version
table across every Brainiac surface.

Pure inspection: this module never writes a file, never calls a subprocess
that mutates state, and never reaches the network beyond a local
``git rev-list`` against the already-cloned marketplace checkout (no fetch).
Safe to run anywhere, any number of times.

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
import subprocess
from pathlib import Path
from typing import Any, Optional

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
# Surface 3 — host engine venv (~/.brainiac/venv/bin/brain --version)
# --------------------------------------------------------------------------

def check_host_venv(brainiac_home: Path, ssot: str) -> dict:
    brain_bin = brainiac_home / "venv" / "bin" / "brain"
    if not brain_bin.exists():
        return _row("Host engine venv", NOT_DETECTABLE,
                    f"{brain_bin} not found (no host install here)",
                    remediation="/brainiac-install")
    try:
        out = subprocess.run(
            [str(brain_bin), "--version"], capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        return _row("Host engine venv", UNKNOWN, f"{type(exc).__name__}: {exc}")
    text = (out.stdout or out.stderr or "").strip()
    m = re.search(r"(\d+\.\d+\.\d+\S*)", text)
    installed = m.group(1) if m else text
    if not installed:
        return _row("Host engine venv", UNKNOWN, "empty --version output")
    if installed == ssot:
        return _row("Host engine venv", CURRENT, f"{installed} == SSOT {ssot}",
                    raw={"installed": installed})
    return _row("Host engine venv", STALE, f"installed {installed} != SSOT {ssot}",
                remediation="/brainiac-update", raw={"installed": installed})


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

PLUGIN_NAMES = ("brainiac-manager", "profile-a-kernel", "profile-a-extras")


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
    claude_home: Path, ssot: str, marketplace_name: str = "profile-a-marketplace",
) -> list[dict]:
    rows = []
    marketplace_dir = claude_home / "plugins" / "marketplaces" / marketplace_name
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
            for pjson in sessions_root.glob(f"*/*/rpm/plugin_*/.claude-plugin/plugin.json"):
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
        if _compare(str(version), ssot) < 0:
            remediation = ("in a Cowork session use /skill-creator to repackage + "
                           "Save-and-Replace the stale skill(s); re-run brain doctor on "
                           "the host to confirm it took")
        else:
            remediation = "looks current — no action needed"
        rows.append(_row(surface, MANUAL_REQUIRED, detail, remediation=remediation,
                         raw={"version": version, "candidates": len(candidates),
                              "newest_mtime": mtime_str}))
    return rows


# --------------------------------------------------------------------------
# Surface 8 — staged Cowork workspaces (tools/workspace_registry.py entries)
# --------------------------------------------------------------------------

def _cowork_vault_dir(entry: dict) -> str:
    """The dir a cowork-vm entry's `.brain` actually lives under: the
    registry's ``vault_path`` — the same field ``cowork_workspace_install.sh``
    treats as ``$VAULT`` and the Cowork VM reads. ``workspace_path`` is the
    PARENT checkout dir; its own `.brain` (if any) is the unrelated host
    stage — reading it here is exactly the false-green bug (a stale
    cowork-vm engine at `vault_path/.brain` hid behind a current
    `workspace_path/.brain`). Falls back to ``workspace_path`` only if
    ``vault_path`` is absent (malformed/legacy entry)."""
    return entry.get("vault_path") or entry.get("workspace_path", "")


def check_staged_workspaces(registry_entries: list[dict], ssot: str) -> list[dict]:
    rows = []
    for entry in registry_entries:
        if entry.get("target") == "host":
            continue  # host entries ARE the checkout; surfaces 1-4 already cover it
        vault_dir = _cowork_vault_dir(entry)
        surface = f"Staged workspace ({vault_dir})"
        stamp_path = Path(vault_dir) / ".brain" / "engine" / "brain" / "_version.py"
        if not stamp_path.exists():
            rows.append(_row(surface, NOT_DETECTABLE,
                             f"{stamp_path} not found — workspace may be gone or never staged",
                             remediation="/brainiac-cowork-setup"))
            continue
        text = stamp_path.read_text(encoding="utf-8")
        m = re.search(r'(?m)^__version__ = "([^"]+)"$', text)
        if not m:
            rows.append(_row(surface, UNKNOWN, f"{stamp_path}: no __version__ line"))
            continue
        staged = m.group(1)
        if staged == ssot:
            rows.append(_row(surface, CURRENT, f"staged {staged} == SSOT {ssot}",
                             raw={"staged": staged}))
        else:
            rows.append(_row(surface, STALE, f"staged {staged} != SSOT {ssot}",
                             remediation="/brainiac-update", raw={"staged": staged}))
    return rows


# --------------------------------------------------------------------------
# Surface — staged Cowork skill bundles (cw-02): the .brain/skills/*.skill
# zips landed by cowork_workspace_install.sh each carry a VERSION file
# (tools/package_clients.py build_cowork_zips). A separate row from the
# engine stamp above so a version-matched engine with a stale/missing skill
# bundle is still visible (best-effort — reads whichever zip is alphabetically
# first; every zip in one install pass is written from the same SSOT, so one
# representative sample is enough to catch drift).
# --------------------------------------------------------------------------

def check_staged_skill_bundles(registry_entries: list[dict], ssot: str) -> list[dict]:
    import zipfile

    rows = []
    for entry in registry_entries:
        if entry.get("target") == "host":
            continue
        vault_dir = _cowork_vault_dir(entry)
        surface = f"Staged skill bundles ({vault_dir})"
        skills_dir = Path(vault_dir) / ".brain" / "skills"
        if not skills_dir.is_dir():
            rows.append(_row(surface, NOT_DETECTABLE,
                             f"{skills_dir} not found — workspace may be gone or never staged",
                             remediation="tools/cowork_workspace_install.sh"))
            continue
        zips = sorted(skills_dir.glob("*.skill"))
        if not zips:
            rows.append(_row(surface, NOT_DETECTABLE, f"no .skill bundles found in {skills_dir}",
                             remediation="tools/cowork_workspace_install.sh"))
            continue
        sample = zips[0]
        try:
            with zipfile.ZipFile(sample) as zf:
                version_member = f"{sample.stem}/VERSION"
                if version_member not in zf.namelist():
                    rows.append(_row(surface, UNKNOWN,
                                     f"{sample.name}: no VERSION marker (pre-cw-02 bundle?)",
                                     remediation="tools/cowork_workspace_install.sh"))
                    continue
                staged = zf.read(version_member).decode("utf-8").strip()
        except (OSError, zipfile.BadZipFile) as exc:
            rows.append(_row(surface, UNKNOWN, f"{sample.name}: unreadable ({exc})"))
            continue
        if staged == ssot:
            rows.append(_row(surface, CURRENT, f"staged {staged} == SSOT {ssot} (sample: {sample.name})",
                             raw={"staged": staged}))
        else:
            rows.append(_row(surface, STALE, f"staged {staged} != SSOT {ssot} (sample: {sample.name})",
                             remediation="tools/cowork_workspace_install.sh (re-stage engine + skills)",
                             raw={"staged": staged}))
    return rows


# --------------------------------------------------------------------------
# Surface 10 — index / snapshot schema (per staged workspace, if a snapshot
# dir exists there) — separate row from the version stamp so a version-match
# with a schema skew is still visible.
# --------------------------------------------------------------------------

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
    return not (root / "tools" / "workspace_registry.py").exists() and _ssot_version(root) is None


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
    n_files = sum(1 for p in model_dir.rglob("*") if p.is_file())
    return _row(surface, CURRENT, f"{model_dir} present ({n_files} file(s))")


def check_vm_maintain_heartbeat(vault: Path) -> dict:
    """VM-readable mirror of ``BrainCore._maintain_heartbeat_summary`` (the VM
    can read the heartbeat file even though only the host ever runs
    ``brain maintain``)."""
    import datetime as _dt

    from . import config

    surface = "Maintain heartbeat (.brain/maintain-state.json)"
    state = _read_json(config.maintain_state_path(vault))
    if not state:
        return _row(surface, NOT_DETECTABLE,
                    "no maintain-state.json yet — brain maintain (host-only ritual) has not run")
    today = _dt.date.today()
    stale, repeated = [], []
    for branch, entry in state.items():
        if not isinstance(entry, dict):
            continue
        last_run = entry.get("last_run")
        age_hours: Optional[float] = None
        if last_run:
            try:
                age_hours = (today - _dt.date.fromisoformat(last_run)).days * 24
            except ValueError:
                age_hours = None
        if branch == "daily" and (entry.get("failed") or (age_hours is not None and age_hours > 48)):
            stale.append(branch)
        if int(entry.get("consecutive_failures", 0) or 0) >= 2:
            repeated.append(branch)
    if stale:
        return _row(surface, STALE, f"stale branch(es): {stale}",
                    remediation="brain maintain runs host-side only — check the host's nightly scheduler")
    if repeated:
        return _row(surface, STALE, f"repeated-failure branch(es): {repeated}",
                    remediation="check the host's nightly maintenance logs")
    return _row(surface, CURRENT, f"{len(state)} branch(es) tracked, none stale/repeatedly-failing")


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
    rows.extend(check_workspace_schema(entries, SCHEMA_VERSION))
    rows.append(check_vm_snapshot(vault_path))
    rows.append(check_vm_model_cache(vault_path))
    rows.append(check_vm_maintain_heartbeat(vault_path))
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
    marketplace_name: str = "profile-a-marketplace",
) -> dict[str, Any]:
    """Run every ADR-0005 Ruling 2 surface check and return a report dict.

    All path-ish parameters default to the real machine locations but accept
    overrides — tests pass fixture directories so this NEVER needs the live
    machine to be exercised.
    """
    from . import __version__ as _unused  # noqa: F401 (import proves module loads)
    from .index import SCHEMA_VERSION

    repo_root = repo_root or Path(__file__).resolve().parent.parent.parent
    brainiac_home = brainiac_home or Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
    claude_home = claude_home or (Path.home() / ".claude")
    app_support_dir = app_support_dir or (Path.home() / "Library" / "Application Support" / "Claude")
    marketplace_dir = marketplace_dir or (claude_home / "plugins" / "marketplaces" / marketplace_name)

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

    ssot = _ssot_version(repo_root)
    rows: list[dict] = []

    if ssot is None:
        rows.append(_row("Version SSOT (pyproject.toml)", UNKNOWN, "no version found in pyproject.toml"))
        ssot = "0.0.0"  # keeps downstream comparisons from crashing; every row above is UNKNOWN/errored
    else:
        rows.append(_row("Version SSOT (pyproject.toml)", CURRENT, ssot, raw={"version": ssot}))

    rows.append(check_committed_stamp(repo_root, ssot))
    rows.append(check_host_venv(brainiac_home, ssot))
    rows.append(check_dist_compat(repo_root, ssot))
    rows.extend(check_plugin_manifests(repo_root, ssot))
    rows.extend(check_installed_cli_plugins(claude_home, ssot, marketplace_name))
    if registry_unavailable:
        rows.append(_row(
            "Workspace registry (tools/workspace_registry.py)", NOT_DETECTABLE,
            "unavailable in this checkout — looks like a staged zero-install VM "
            "copy (tools/ is host-only, never staged); staged-workspace/skill-bundle "
            "rows below are skipped here",
            remediation="run `brain doctor --role vm` for the VM-appropriate surfaces, "
                        "or run this on the full host checkout"))
    rows.extend(check_staged_workspaces(registry_entries, ssot))
    rows.extend(check_staged_skill_bundles(registry_entries, ssot))
    rows.extend(check_workspace_schema(registry_entries, SCHEMA_VERSION))
    rows.append(check_marketplace_cache(marketplace_dir))
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
            marketplace_dir=claude_home / "plugins" / "marketplaces" / "profile-a-marketplace",
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
