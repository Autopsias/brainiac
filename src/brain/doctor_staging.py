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


# `check_staged_workspaces` and `check_staged_skill_bundles` are RE-EXPORTS,
# not definitions. They were defined here, hardcoding
# `Path(vault_dir) / ".brain"`. Closed Stacks (2026-08-29) rewrote both in
# `vmstaging.py` against the one relocation-aware resolver and pinned them
# with tests --- but `doctor.py`'s facade still re-exported THESE, so
# `run_doctor` kept running the co-located pair and the fixed pair was dead
# code. Measured 2026-08-31 on a vault `brain provision-local` had just
# wired correctly: two GATING `stale` rows and `brain doctor` exit 1, while
# `vmstaging`'s own resolver read the same vault as `current`. Re-exported
# rather than re-pointed in `doctor.py` so there is ONE definition and no
# second copy to drift. `check_workspace_schema` below keeps its own
# `<vault>/.brain`: the snapshot is not a STAYS-on-the-mount directory.
from .vmstaging import (  # noqa: E402,F401  (facade re-export)
    check_staged_skill_bundles as check_staged_skill_bundles,
    check_staged_workspaces as check_staged_workspaces,
)


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
