"""Staged-workspace, marketplace-cache, and registry-drift doctor checks."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

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
            # "I cannot see it" vs "I looked, and it is not there": merging
            # them hid a real defect (2026-08-17) — the registry
            # claimed a Cowork workspace with no engine in it, so Cowork got
            # `brain: command not found` while host doctor said not-detectable.
            exists = Path(vault_dir).is_dir()
            rows.append(_row(
                surface, STALE if exists else NOT_DETECTABLE,
                (f"registry claims a Cowork workspace but NO engine is staged "
                 f"there ({stamp_path} missing)" if exists
                 else f"{vault_dir} not found — workspace may be gone"),
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
            # Same distinction as the engine row above.
            exists = Path(vault_dir).is_dir()
            rows.append(_row(
                surface, STALE if exists else NOT_DETECTABLE,
                f"{skills_dir} not found — "
                + ("no skill bundles staged" if exists else "workspace may be gone"),
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
# Parent-namespace binds, deferred past this module's own defs.
from .doctor import (  # noqa: E402
    CURRENT as CURRENT,
    MANUAL_REQUIRED as MANUAL_REQUIRED,
    NOT_DETECTABLE as NOT_DETECTABLE,
    STALE as STALE,
    UNMANAGED as UNMANAGED,
    UNKNOWN as UNKNOWN,
    _read_json as _read_json,
    _row as _row,
    _compare as _compare,
)
from .doctor_plugins import _version_tuple as _version_tuple  # noqa: E402
