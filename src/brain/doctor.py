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
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from .doctor_checks import build_doctor_rows

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


from .doctor_vendor import (  # noqa: E402  (re-export)
    check_vendor_abi,
    _prune_retired_dirs,  # noqa: F401  (public re-export; existing callers use doctor._prune_retired_dirs)
    _running_vendor_arch,
)


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
    from .vmstaging import check_staged_vm_binaries

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

def _unreadable_registry_row(repo_root: Path) -> dict:
    """The row for a `tools/workspace_registry.py` that would not import.

    On a staged VM copy tools/ is absent BY DESIGN, so NOT_DETECTABLE is the
    honest answer. Anywhere else it means every staged-workspace and
    skill-bundle row was SKIPPED — and a skipped check must never be summarised
    as "all required surfaces current" (measured 2026-08-18), so it becomes
    UNKNOWN, which gates the verdict where NOT_DETECTABLE does not.
    """
    if looks_like_vm_stage():
        return _row(
            "Workspace registry (tools/workspace_registry.py)", NOT_DETECTABLE,
            "unavailable in this checkout — looks like a staged zero-install VM "
            "copy (tools/ is host-only, never staged); staged-workspace/skill-bundle "
            "rows below are skipped here",
            remediation="run `brain doctor --role vm` for the VM-appropriate "
                        "surfaces, or run this on the full host checkout")
    return _row(
        "Workspace registry (tools/workspace_registry.py)", UNKNOWN,
        f"could NOT be loaded from {repo_root / 'tools'} on a host install — every "
        "staged-workspace and skill-bundle row below was SKIPPED, so this run "
        "cannot tell you whether your Cowork workspaces are current",
        remediation="re-run from the engine source checkout, or pass --engine-src "
                    "<checkout>; until then treat staged-workspace freshness as "
                    "UNVERIFIED")


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
    from . import __version__ as engine_version
    # The surface-resolution stages live in :mod:`brain.doctor_context` (s18)
    # and are imported HERE, lazily, because that module imports this one for
    # ``marketplace_install_location`` — a module-level import would cycle.
    from . import doctor_context as _ctx
    from .index import SCHEMA_VERSION

    repo_root = repo_root or Path(__file__).resolve().parent.parent.parent
    brainiac_home = brainiac_home or _ctx.brainiac_home_default()
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
    marketplace_dir, resolved_marketplace = _ctx.resolve_marketplace_dir(
        claude_home, marketplace_dir, marketplace_name)

    # RC4: a wheel install resolves repo_root inside site-packages, so retry via
    # the marketplace installLocation — the real checkout on a directory-source
    # install. HOISTED 2026-08-18: this MUST precede the registry import below.
    # Sitting after it, every wheel install looked the registry up under the
    # site-packages root, failed, and reported "unavailable in this checkout"
    # while this same correction then found the real checkout — so `brain
    # update` skipped every staged-workspace row and still printed "all
    # required surfaces current" over two workspaces two releases behind.
    repo_root = _ctx.resolve_repo_root(repo_root, resolved_marketplace)

    registry_unavailable = False
    if registry_entries is None:
        _ctx.sys_path_with_tools(repo_root)
        registry_entries, registry_unavailable = _ctx.resolve_registry_entries(
            repo_root)

    # Repo-oriented surfaces (SSOT/stamp/dist/plugin-manifests) only mean
    # anything on a DEV CHECKOUT. From an installed engine (dist venv, uv
    # tool, pipx) repo_root resolves inside site-packages, so those rows came
    # back UNKNOWN — a gating status — and every health report generated by a
    # pinned engine read falsely DEGRADED (field finding 2026-07-20, caught in
    # the v0.19.3 pre-restage verification). Without a checkout the honest
    # answer is NOT_DETECTABLE, and drift comparisons fall back to the running
    # engine's own version.
    is_dev_checkout = (repo_root / "pyproject.toml").is_file() and (repo_root / "src" / "brain").is_dir()

    resolved_brain = _ctx.resolved_brain_bin()
    assert app_support_dir is not None
    assert registry_entries is not None
    assert marketplace_dir is not None

    context, checks = _ctx.build_context_and_checks(
        repo_root=repo_root, brainiac_home=brainiac_home, claude_home=claude_home,
        app_support_dir=app_support_dir, registry_entries=registry_entries,
        marketplace_dir=marketplace_dir, marketplace_name=marketplace_name,
        registry_fetch=registry_fetch, vault=vault, resolved_brain=resolved_brain,
        is_dev_checkout=is_dev_checkout, schema_version=SCHEMA_VERSION,
        engine_version=engine_version, registry_unavailable=registry_unavailable,
        vm_python=_VM_PYTHON)
    ssot, rows = build_doctor_rows(context, checks)
    from .vmstaging import check_staged_vm_binaries

    rows.extend(check_staged_vm_binaries(registry_entries, ssot))

    gating_stale = [r for r in rows if r["status"] in _GATING_STATUSES]
    return {
        "ssot_version": ssot,
        "rows": rows,
        "ok": not gating_stale,
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



# The plugin/marketplace, staged-workspace, VM-stage, and liveness checks live
# in their own modules since the 2026-08-16 size ratchet; re-exported so every
# `brain.doctor.<name>` caller is unchanged.
from .doctor_health import (  # noqa: E402,F401  (facade re-export)
    _RECOVERABLE_BUCKETS as _RECOVERABLE_BUCKETS,
    check_audit_content_drift as check_audit_content_drift,
    check_corpus_invariants as check_corpus_invariants,
    check_embedder_liveness as check_embedder_liveness,
    check_ingest_capability as check_ingest_capability,
    check_quarantine as check_quarantine,
    check_query_capture as check_query_capture,
    check_vm_maintain_heartbeat as check_vm_maintain_heartbeat,
)
from .doctor_plugins import (  # noqa: E402,F401  (facade re-export)
    CHANNEL_EDITABLE as CHANNEL_EDITABLE,
    CHANNEL_PIPX as CHANNEL_PIPX,
    CHANNEL_PIP_USER as CHANNEL_PIP_USER,
    CHANNEL_PYPI_UV as CHANNEL_PYPI_UV,
    CHANNEL_UNKNOWN as CHANNEL_UNKNOWN,
    CHANNEL_VENV_WHEEL as CHANNEL_VENV_WHEEL,
    OLD_MARKETPLACE_NAME as OLD_MARKETPLACE_NAME,
    OLD_TO_NEW_PLUGIN_NAMES as OLD_TO_NEW_PLUGIN_NAMES,
    PLUGIN_NAMES as PLUGIN_NAMES,
    STALE_NAME_RECOVERY as STALE_NAME_RECOVERY,
    _CHANNEL_UPGRADE_CMD as _CHANNEL_UPGRADE_CMD,
    _running_engine_version as _running_engine_version,
    _version_tuple as _version_tuple,
    check_desktop_plugin_store as check_desktop_plugin_store,
    check_dist_compat as check_dist_compat,
    check_host_venv as check_host_venv,
    check_installed_cli_plugins as check_installed_cli_plugins,
    check_mcp_vault_paths as check_mcp_vault_paths,
    check_mcpb_desktop_collision as check_mcpb_desktop_collision,
    check_plugin_manifests as check_plugin_manifests,
    check_stale_name_plugins as check_stale_name_plugins,
    detect_install_channel as detect_install_channel,
)
from .doctor_staging import (  # noqa: E402,F401  (facade re-export)
    _cowork_vault_dir as _cowork_vault_dir,
    _latest_git_tag as _latest_git_tag,
    check_cos_deployed_skill as check_cos_deployed_skill,
    check_marketplace_cache as check_marketplace_cache,
    check_pypi_registry_drift as check_pypi_registry_drift,
    check_staged_skill_bundles as check_staged_skill_bundles,
    check_staged_workspaces as check_staged_workspaces,
    check_workspace_schema as check_workspace_schema,
    fetch_pypi_latest_version as fetch_pypi_latest_version,
)
from .doctor_vm import (  # noqa: E402,F401  (facade re-export)
    _VM_PYTHON as _VM_PYTHON,
    _in_site_packages as _in_site_packages,
    _read_version_stamp as _read_version_stamp,
    check_vm_engine_stamp as check_vm_engine_stamp,
    check_vm_model_cache as check_vm_model_cache,
    check_vm_snapshot as check_vm_snapshot,
    looks_like_vm_stage as looks_like_vm_stage,
)


# The ``__main__`` guard lives at the very END of the module, AFTER the
# facade re-exports above: under runpy (``python -m brain.<mod>``) the guard
# fires at its source position, and every facade name must already be bound
# by then (2026-08-16 size-ratchet fix).
if __name__ == "__main__":
    _demo()
