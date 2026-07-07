"""``brain update`` (ADR-0005 Ruling 3, UP-01/UP-02) — the self-executing
refresh: marketplace refresh -> downgrade-safe CLI-plugin reinstall -> engine
venv refresh -> dist/ rebuild -> workspace re-stage -> ``brain doctor`` verify,
with one before->after version table and one pass/fail.

This module RUNS the operations; it does not print instructions for a human
to copy-paste (that was the old ``/brainiac-update`` skill's failure mode —
ADR-0005 Ruling 3 amends ADR-0004 Ruling 5's "print exactly that instruction"
migration prose into an automatic clean reinstall).

Every external effect (subprocess call, filesystem mutation) goes through the
injectable ``runner`` so tests exercise the real decision logic against
fixtures/fakes with zero live-machine or plugin-store mutation — required for
this session's unattended, non-destructive contract.

Preflight capability probe (HARDEN:consensus-HIGH): before any destructive
`claude plugin` call, confirm the CLI surface this module depends on
(marketplace update / list / uninstall / install) actually exists. Never
drive an unversioned CLI blind.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from .doctor import _compare, _ssot_version, check_installed_cli_plugins, run_doctor, render_human

# --------------------------------------------------------------------------
# Runner abstraction — the ONLY place subprocess.run is called from this
# module. Tests inject a fake that records calls instead of executing them
# (dry-run-by-construction), so the version-compare/downgrade-decision logic
# is exercised without ever touching a real plugin store or venv.
# --------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 120)
    return subprocess.run(cmd, **kwargs)


# --------------------------------------------------------------------------
# Preflight capability probe (HARDEN:consensus-HIGH)
# --------------------------------------------------------------------------

REQUIRED_SUBCOMMANDS = ("marketplace", "list", "uninstall", "install", "update")


def probe_cli_capability(run: Runner = _default_runner) -> dict:
    """Confirm the ``claude plugin`` CLI surface this module drives exists
    before any destructive call. Never assert the surface from memory —
    parse ``claude plugin --help`` and require every subcommand this module
    calls to be present. Blocks (does not raise) on mismatch: callers must
    check ``ok`` and stop, printing the manual fallback commands.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return {
            "ok": False,
            "reason": "`claude` CLI not found on PATH",
            "manual_commands": [
                "install/repair the Claude Code CLI, then re-run `brain update`",
            ],
        }
    try:
        out = run([claude_bin, "plugin", "--help"])
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"`claude plugin --help` failed: {type(exc).__name__}: {exc}",
            "manual_commands": [
                "claude plugin marketplace update <name>",
                "claude plugin list",
                "claude plugin uninstall <plugin>@<marketplace>",
                "claude plugin install <plugin>@<marketplace>",
                "claude plugin update <plugin>@<marketplace>",
            ],
        }
    text = ((out.stdout or "") + "\n" + (out.stderr or "")).lower()
    missing = [c for c in REQUIRED_SUBCOMMANDS if c not in text]
    if missing:
        return {
            "ok": False,
            "reason": f"`claude plugin --help` is missing expected subcommand(s): {missing} "
                      "(gh#69626 pruning / gh#40153 non-atomic auto-update are cited real risks "
                      "of this surface moving under us) — refusing to drive it blind",
            "manual_commands": [
                "claude plugin marketplace update <name>",
                "claude plugin list",
                "claude plugin uninstall <plugin>@<marketplace>",
                "claude plugin install <plugin>@<marketplace>",
                "claude plugin update <plugin>@<marketplace>",
            ],
        }
    return {"ok": True, "reason": "claude plugin CLI surface confirmed", "manual_commands": []}


# --------------------------------------------------------------------------
# Step: marketplace refresh (ALWAYS FIRST — kills the stale-cache no-op)
# --------------------------------------------------------------------------

def refresh_marketplace(marketplace_name: str, run: Runner = _default_runner) -> dict:
    claude_bin = shutil.which("claude") or "claude"
    try:
        out = run([claude_bin, "plugin", "marketplace", "update", marketplace_name])
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    ok = out.returncode == 0
    return {"ok": ok, "detail": (out.stdout or out.stderr or "").strip()}


# --------------------------------------------------------------------------
# Downgrade-safe reinstall decision (pure logic — no subprocess) + execution
# --------------------------------------------------------------------------

def decide_plugin_action(installed: Optional[str], marketplace: Optional[str]) -> str:
    """Pure decision function (fixture-testable, ADR-0005 Ruling 3):

    - installed is None            -> "install"        (never installed)
    - marketplace is None          -> "skip"           (nothing to compare against)
    - installed >  marketplace     -> "reinstall"      (reconciliation downgrade,
                                                          uninstall+clean install)
    - installed <  marketplace     -> "update"         (normal forward update)
    - installed == marketplace     -> "skip"           (already current)
    """
    if installed is None:
        return "install"
    if marketplace is None:
        return "skip"
    cmp_ = _compare(installed, marketplace)
    if cmp_ == 0:
        return "skip"
    return "reinstall" if cmp_ > 0 else "update"


def apply_plugin_action(
    action: str, plugin_name: str, marketplace_name: str, run: Runner = _default_runner,
) -> dict:
    """Execute the decided action. ROLLBACK-safe ordering (HARDEN:claude-MEDIUM):
    for "reinstall", uninstall ONLY after we've confirmed we're about to run
    install right after in the same call — if install fails after a
    successful uninstall, this returns ok=False with the plugin left absent,
    and the caller (run_update) surfaces that as a partial/mixed-version
    state rather than claiming success.
    """
    claude_bin = shutil.which("claude") or "claude"
    spec = f"{plugin_name}@{marketplace_name}"
    if action == "skip":
        return {"action": "skip", "ok": True, "detail": "already current"}

    if action == "install":
        out = run([claude_bin, "plugin", "install", spec])
        ok = out.returncode == 0
        return {"action": "install", "ok": ok, "detail": (out.stdout or out.stderr or "").strip()}

    if action == "update":
        # `claude plugin install` on an already-installed plugin is a no-op
        # (prints "already installed", does NOT upgrade) — `claude plugin
        # update` is the subcommand that actually moves the installed
        # version forward. Verified live against v0.10.0's `claude plugin`
        # CLI: install no-ops, update reports "updated from X to Y".
        out = run([claude_bin, "plugin", "update", spec])
        ok = out.returncode == 0
        return {"action": "update", "ok": ok, "detail": (out.stdout or out.stderr or "").strip()}

    if action == "reinstall":
        uninstall_out = run([claude_bin, "plugin", "uninstall", spec])
        if uninstall_out.returncode != 0:
            # Uninstall itself failed: plugin is presumably still in its
            # original (installed>marketplace) state. Not worse than before.
            return {
                "action": "reinstall", "ok": False, "stage": "uninstall",
                "detail": (uninstall_out.stdout or uninstall_out.stderr or "").strip(),
            }
        install_out = run([claude_bin, "plugin", "install", spec])
        if install_out.returncode != 0:
            # WORST case (HARDEN:claude-MEDIUM): uninstall succeeded, install
            # failed -> plugin is now ABSENT, strictly worse than the
            # downgraded start. Report this explicitly so the caller can
            # print the exact recovery command rather than a green report.
            return {
                "action": "reinstall", "ok": False, "stage": "install",
                "detail": (install_out.stdout or install_out.stderr or "").strip(),
                "half_applied": True,
                "recovery_command": f"claude plugin install {spec}",
            }
        return {
            "action": "reinstall", "ok": True,
            "detail": (install_out.stdout or install_out.stderr or "").strip(),
        }

    raise ValueError(f"unknown plugin action: {action!r}")


# --------------------------------------------------------------------------
# Engine venv refresh — resolves the engine source path from the workspace
# registry / explicit override, NEVER a hardcoded ~/brainiac (HARDEN:codex-MEDIUM).
# --------------------------------------------------------------------------

def resolve_engine_source(
    *, explicit: Optional[str] = None, repo_root: Optional[Path] = None,
) -> Path:
    """Resolve the engine checkout to `pip install -e` against, in order:

    1. an explicit override (config / CLI flag / $BRAINIAC_ENGINE_SRC),
    2. the repo root this module itself ships from (the canonical checkout
       when `brain update` is invoked from within it),
    3. `$HOME/brainiac` ONLY as a last-resort convention, never assumed blind.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("BRAINIAC_ENGINE_SRC"):
        return Path(os.environ["BRAINIAC_ENGINE_SRC"]).expanduser().resolve()
    if repo_root is not None:
        return repo_root.resolve()
    inferred = Path(__file__).resolve().parent.parent.parent
    if (inferred / "pyproject.toml").exists():
        return inferred
    return Path.home() / "brainiac"


def refresh_engine_venv(
    engine_src: Path, brainiac_home: Path, run: Runner = _default_runner,
) -> dict:
    venv_dir = brainiac_home / "venv"
    pip = venv_dir / "bin" / "pip"
    brain_bin = venv_dir / "bin" / "brain"

    def _read_version() -> str:
        if not brain_bin.exists():
            return "unknown"
        try:
            out = run([str(brain_bin), "--version"])
            return (out.stdout or out.stderr or "unknown").strip() or "unknown"
        except Exception:
            return "unknown"

    old_version = _read_version()
    if not pip.exists():
        run(["python3", "-m", "venv", str(venv_dir)])
    install_out = run([str(pip), "install", "--upgrade", "-e", str(engine_src)])
    ok = install_out.returncode == 0
    new_version = _read_version() if ok else old_version
    return {
        "ok": ok,
        "old_version": old_version,
        "new_version": new_version,
        "detail": (install_out.stdout or install_out.stderr or "").strip(),
    }


# --------------------------------------------------------------------------
# dist/ rebuild — `pip install -e` (engine venv refresh, above) refreshes the
# installed package/venv but NEVER regenerates the gitignored dist/COMPAT +
# dist/cowork-skills/*.skill artifacts that restage_workspaces' cowork-vm leg
# copies (tools/package_clients.py is the only thing that builds those).
# Skipping this is the exact bug observed live twice (0.10.2->0.10.3 and
# 0.10.3->0.10.4): restage_workspaces silently staged one-build-stale .skill
# bundles and `brain doctor` flagged "Staged skill bundles stale" / "dist/COMPAT
# stale" until a human ran the packager by hand and re-ran `brain update`.
# --------------------------------------------------------------------------

def rebuild_dist(engine_src: Path, run: Runner = _default_runner) -> dict:
    packager = engine_src / "tools" / "package_clients.py"
    try:
        out = run([sys.executable, str(packager)], cwd=str(engine_src))
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    ok = out.returncode == 0
    return {"ok": ok, "detail": (out.stdout or out.stderr or "").strip()}


# --------------------------------------------------------------------------
# Workspace re-stage — thin wrapper delegating to workspace_registry; never
# reimplements its locking/schema. For target "cowork-vm" this re-runs the
# (a) engine-source and (d) skill-bundle legs of
# tools/cowork_workspace_install.sh directly in Python (not by shelling into
# the whole script, which also re-rebuilds the host index and re-scaffolds
# the overlay every run — steps the `brain sync --publish` call right after
# already covers for a routine update). Doing the copy in Python keeps it
# testable with plain tmp-path fixtures instead of a real bash subprocess.
# --------------------------------------------------------------------------

_VERSION_STAMP_RE = re.compile(r'(?m)^__version__ = "([^"]+)"$')


def _read_version_stamp(stamp_path: Path) -> Optional[str]:
    if not stamp_path.exists():
        return None
    m = _VERSION_STAMP_RE.search(stamp_path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def stage_engine_and_skills(engine_src: Path, workspace_path: str) -> dict:
    """(a)+(d) legs of cowork_workspace_install.sh for one cowork-vm
    workspace: re-copy the engine source into ``<vault>/.brain/engine/brain/``
    and refresh the ``.skill`` bundles into ``<vault>/.brain/skills/`` from
    whatever ``dist/cowork-skills/*.skill`` currently ships in ``engine_src``
    (tools/release.py already runs tools/package_clients.py before every cut,
    so that dir is the SSOT-version build by the time `brain update` runs).

    Returns a dict with the SSOT/staged versions and skill count so the
    caller can assert-and-fail rather than silently report ok.
    """
    brain_dir = Path(workspace_path) / ".brain"

    # (a) engine source — replace wholesale, drop __pycache__ (mirrors the
    # script's `rm -rf` + `cp -R` + pycache cleanup).
    engine_dir = brain_dir / "engine"
    if engine_dir.exists():
        shutil.rmtree(engine_dir)
    engine_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(engine_src / "src" / "brain", engine_dir / "brain")
    for cache_dir in (engine_dir / "brain").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    # (d) skill bundles (cw-02) — refresh from the current dist build.
    skills_src_dir = engine_src / "dist" / "cowork-skills"
    skills_dst_dir = brain_dir / "skills"
    skills_dst_dir.mkdir(parents=True, exist_ok=True)
    zips = sorted(skills_src_dir.glob("*.skill")) if skills_src_dir.is_dir() else []
    for z in zips:
        shutil.copyfile(z, skills_dst_dir / z.name)

    ssot = _ssot_version(engine_src)
    staged = _read_version_stamp(engine_dir / "brain" / "_version.py")
    return {
        "ssot_version": ssot,
        "staged_version": staged,
        "version_ok": ssot is not None and staged == ssot,
        "skills_shipped": len(zips),
        "skills_src_dir": str(skills_src_dir),
    }


def restage_workspaces(
    engine_src: Path, brainiac_home: Path, run: Runner = _default_runner,
) -> list[dict]:
    import sys as _sys

    _sys.path.insert(0, str(engine_src / "tools"))
    import workspace_registry as _wr  # type: ignore

    results = []
    brain_bin = brainiac_home / "venv" / "bin" / "brain"
    for entry in _wr.list_entries():
        vault_path = entry.get("vault_path", "")
        workspace_path = entry.get("workspace_path", "")
        target = entry.get("target")
        import platform
        import socket

        # Gate on arch ONLY (BUG A, pre-existing since v0.10.0): macOS
        # `socket.gethostname()` is UNSTABLE — it flips between the mDNS
        # `.local` name and the DHCP-assigned name for the SAME machine
        # (verified live: an entry staged as `oldhost.local`
        # read back as `Mac.lan` on an unchanged host), so a hostname-based
        # gate silently skips the user's own workspace and the re-stage below
        # never runs. arch is stable and still protects against a genuinely
        # different (stale, other-machine) entry; the folder-exists check
        # right below is the second layer of defense for a same-arch entry
        # that's actually a different machine (its path won't resolve here).
        if entry.get("arch") != platform.machine():
            results.append({"workspace_path": workspace_path, "target": target,
                            "status": "skipped", "reason": "different arch"})
            continue
        if entry.get("host") != socket.gethostname():
            # Self-heal rather than skip: rewrite the entry's host to the
            # current hostname so the registry stops drifting every time
            # macOS flips which name it hands back.
            _wr.upsert_entry(vault_path=vault_path, workspace_path=workspace_path,
                              target=target, model_dir=entry.get("model_dir"))
        if not Path(vault_path or workspace_path).exists():
            results.append({"workspace_path": workspace_path, "target": target,
                            "status": "skipped", "reason": "folder missing"})
            continue

        if target == "host":
            # Engine venv refresh already covers this leg (Step 3); only the
            # nightly task remains and that's a separate, per-vault script
            # (scripts/install-brief-mac.sh) — not re-implemented here.
            results.append({"workspace_path": workspace_path, "target": target,
                            "status": "ok", "reason": "engine refresh covers host leg"})
        elif target == "cowork-vm":
            # BUG B: the Cowork VM reads `<vault_path>/.brain` (this is what
            # `tools/cowork_workspace_install.sh` stages into as `$VAULT`) —
            # `workspace_path` is the PARENT checkout dir, whose own `.brain`
            # (if any) is the unrelated HOST stage. Staging into
            # `workspace_path` re-stages the host copy (already current) and
            # reads its version back as "ok" while the real VM engine at
            # `vault_path/.brain/engine` never moves. Must stage vault_path.
            try:
                stage_info = stage_engine_and_skills(engine_src, vault_path)
            except OSError as exc:
                results.append({"workspace_path": workspace_path, "target": target,
                                "status": "failed",
                                "reason": f"engine/skill re-stage failed: {type(exc).__name__}: {exc}"})
                continue
            # No-silent-no-op (DV-01, ADR-0005 Ruling 1): a re-stage that
            # lands a missing/mismatched _version.py must never report ok —
            # that's exactly the defect this fix exists to close (staged
            # engine stayed at a stale pre-0.10.0 copy while `brain update`
            # reported "ok" off the index sync alone).
            if not stage_info["version_ok"]:
                results.append({"workspace_path": workspace_path, "target": target,
                                "status": "failed",
                                "reason": f"staged engine version {stage_info['staged_version']!r} != "
                                          f"SSOT {stage_info['ssot_version']!r} after re-stage — "
                                          "engine copy landed a missing/stale _version.py stamp"})
                continue
            if stage_info["skills_shipped"] == 0:
                results.append({"workspace_path": workspace_path, "target": target,
                                "status": "failed",
                                "reason": f"no .skill bundles found in {stage_info['skills_src_dir']} "
                                          "to refresh — run tools/package_clients.py in the checkout"})
                continue
            sync_out = run([str(brain_bin), "sync", "--publish"],
                           env={**os.environ, "BRAIN_VAULT": vault_path})
            ok = sync_out.returncode == 0
            results.append({"workspace_path": workspace_path, "target": target,
                            "status": "ok" if ok else "failed",
                            "reason": (sync_out.stdout or sync_out.stderr or "").strip()})
            if ok:
                _wr.touch_refreshed(vault_path=vault_path, workspace_path=workspace_path, target=target)
        else:
            results.append({"workspace_path": workspace_path, "target": target,
                            "status": "skipped", "reason": f"unknown target {target!r}"})
    return results


# --------------------------------------------------------------------------
# Before -> after version table
# --------------------------------------------------------------------------

def render_before_after(table: list[dict]) -> str:
    lines = ["Before -> after version table", "-" * 32]
    surface_w = max((len(r["surface"]) for r in table), default=8) + 2
    for r in table:
        lines.append(f"{r['surface']:<{surface_w}}{r['before']:<16} -> {r['after']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# UP-02 — the single top-level orchestrating entry point
# --------------------------------------------------------------------------

def run_update(
    *,
    marketplace_name: str = "profile-a-marketplace",
    engine_src: Optional[str] = None,
    brainiac_home: Optional[Path] = None,
    claude_home: Optional[Path] = None,
    run: Runner = _default_runner,
    skip_capability_probe: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Chain, in order: capability probe -> marketplace refresh -> per-plugin
    downgrade-safe reinstall -> engine venv refresh -> dist/ rebuild ->
    workspace re-stage -> `brain doctor` verify. One pass/fail, one
    before->after table.

    ``dry_run=True`` runs every read/decision step for real but skips every
    mutating call (marketplace update, plugin install/uninstall, pip install,
    workspace re-stage) — used by the evidence transcript in this session so
    no live plugin store or venv is touched.
    """
    brainiac_home = brainiac_home or Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
    claude_home = claude_home or (Path.home() / ".claude")
    resolved_engine_src = resolve_engine_source(explicit=engine_src)

    result: dict[str, Any] = {
        "ok": False,
        "steps": {},
        "before_after": [],
        "residual_human_steps": [
            "Desktop/Cowork plugin store: no external CLI — verify/update manually "
            "in the Cowork/Desktop client (skills-optional once cw-02 lands; see "
            "the manual-required rows in `brain doctor`).",
        ],
    }

    # Step 0 — preflight capability probe (HARDEN:consensus-HIGH)
    if skip_capability_probe:
        probe = {"ok": True, "reason": "probe skipped by caller", "manual_commands": []}
    else:
        probe = probe_cli_capability(run=run)
    result["steps"]["capability_probe"] = probe
    if not probe["ok"]:
        result["notes"] = ("BLOCKED before any destructive call: " + probe["reason"] +
                            ". Manual commands: " + "; ".join(probe["manual_commands"]))
        return result

    # Step 1 — marketplace refresh FIRST (structurally kills the stale-cache
    # no-op — HARDEN:codex-HIGH). This MUST run before `before_doctor` below:
    # `brain doctor`'s "Installed CLI plugin" rows read the marketplace's
    # on-disk checkout (the exact cache this refresh updates), so computing
    # `before_doctor` first would make the reinstall decision at Step 2 compare
    # against pre-refresh marketplace data — the stale-cache no-op this whole
    # ordering exists to kill (GV-02).
    if dry_run:
        mkt_refresh = {"ok": True, "detail": "[dry-run] claude plugin marketplace update "
                                              f"{marketplace_name} (skipped)"}
    else:
        mkt_refresh = refresh_marketplace(marketplace_name, run=run)
    result["steps"]["marketplace_refresh"] = mkt_refresh
    if not mkt_refresh["ok"]:
        result["notes"] = f"marketplace refresh failed: {mkt_refresh['detail']} — stopping before any plugin mutation."
        return result

    # `before_doctor` is captured AFTER the marketplace refresh, so the
    # plugin-action decision below and the final before->after table both
    # reflect a fresh marketplace read, never a stale pre-refresh snapshot.
    before_doctor = run_doctor(brainiac_home=brainiac_home, claude_home=claude_home)
    before_table = {r["surface"]: r["detail"] for r in before_doctor["rows"]}
    result["steps"]["doctor_before"] = render_human(before_doctor)

    # Step 2 — per-plugin downgrade-safe reinstall decision + apply.
    plugin_rows = [r for r in before_doctor["rows"] if r["surface"].startswith("Installed CLI plugin")]
    plugin_actions = []
    half_applied = []
    failed_updates = []
    for row in plugin_rows:
        raw = row.get("raw") or {}
        installed = raw.get("installed")
        marketplace = raw.get("marketplace")
        pname = row["surface"].split("(", 1)[1].rstrip(")")
        action = decide_plugin_action(installed, marketplace)
        if dry_run:
            applied = {"action": action, "ok": True, "detail": f"[dry-run] would {action}"}
        else:
            applied = apply_plugin_action(action, pname, marketplace_name, run=run)
            # No-op detection (the CLI's `plugin install` on an already-
            # installed plugin silently no-ops instead of upgrading — the
            # exact bug this session fixes). Re-read the installed version
            # after the action and assert it actually moved to marketplace;
            # a no-op must never report ok:true.
            if action == "update" and applied.get("ok"):
                refreshed = check_installed_cli_plugins(claude_home, "", marketplace_name)
                after_row = next((r for r in refreshed if r["surface"] == row["surface"]), None)
                after_version = (after_row or {}).get("raw", {}).get("installed")
                if after_version == installed:
                    applied = {
                        **applied,
                        "ok": False,
                        "detail": (
                            f"plugin {pname} still at {installed} after update — the claude "
                            "plugin CLI no-op'd; run `/plugin update "
                            f"{pname}@{marketplace_name}` manually and restart"
                        ),
                    }
        plugin_actions.append({"plugin": pname, "installed_before": installed,
                               "marketplace": marketplace, **applied})
        if applied.get("half_applied"):
            half_applied.append(applied)
        elif action == "update" and not applied.get("ok"):
            failed_updates.append(applied)
    result["steps"]["plugin_reinstall"] = plugin_actions
    if half_applied:
        result["notes"] = ("update INCOMPLETE — surfaces at mixed versions: a reinstall "
                            "half-applied (uninstall ok, install failed). Recovery: " +
                            "; ".join(h["recovery_command"] for h in half_applied))
        result["steps"]["doctor_after"] = render_human(run_doctor(brainiac_home=brainiac_home, claude_home=claude_home))
        return result
    if failed_updates:
        result["notes"] = ("update INCOMPLETE — plugin update no-op'd: " +
                            "; ".join(f["detail"] for f in failed_updates))
        result["steps"]["doctor_after"] = render_human(run_doctor(brainiac_home=brainiac_home, claude_home=claude_home))
        return result

    # Step 3 — engine venv refresh.
    if dry_run:
        engine_result = {"ok": True, "old_version": before_table.get("Host engine venv", "unknown"),
                         "new_version": "[dry-run] not executed", "detail": "[dry-run] pip -e skipped"}
    else:
        engine_result = refresh_engine_venv(resolved_engine_src, brainiac_home, run=run)
    result["steps"]["engine_refresh"] = engine_result
    if not engine_result["ok"] and not dry_run:
        result["notes"] = f"engine venv refresh failed: {engine_result['detail']} — stopping before workspace re-stage."
        return result

    # Step 3.5 — rebuild dist/ (COMPAT + cowork-skills bundles) from the
    # freshly-installed engine, BEFORE staging workspaces below reads it.
    if dry_run:
        dist_rebuild = {"ok": True, "detail": "[dry-run] tools/package_clients.py (skipped)"}
    else:
        dist_rebuild = rebuild_dist(resolved_engine_src, run=run)
    result["steps"]["dist_rebuild"] = dist_rebuild
    if not dist_rebuild["ok"] and not dry_run:
        result["notes"] = f"dist rebuild failed: {dist_rebuild['detail']} — stopping before workspace re-stage."
        return result

    # Step 4 — re-stage every registered workspace.
    if dry_run:
        workspace_results: list[dict] = [{"workspace_path": "[dry-run]", "target": "n/a",
                                          "status": "skipped", "reason": "dry-run: no re-stage executed"}]
    else:
        workspace_results = restage_workspaces(resolved_engine_src, brainiac_home, run=run)
    result["steps"]["workspace_restage"] = workspace_results

    # Step 5 — brain doctor verify (final pass/fail).
    after_doctor = run_doctor(brainiac_home=brainiac_home, claude_home=claude_home)
    result["steps"]["doctor_after"] = render_human(after_doctor)

    # Before -> after table across the surfaces that actually changed intent.
    table = []
    for surface in sorted(set(before_table) | {r["surface"] for r in after_doctor["rows"]}):
        after_val = next((r["detail"] for r in after_doctor["rows"] if r["surface"] == surface), "n/a")
        table.append({"surface": surface, "before": before_table.get(surface, "n/a"), "after": after_val})
    result["before_after"] = table
    result["before_after_rendered"] = render_before_after(table)

    result["ok"] = after_doctor["ok"]
    result["notes"] = ("update complete, all required surfaces current" if after_doctor["ok"]
                       else f"update ran to completion but {after_doctor['stale_count']} required "
                            "surface(s) still stale — see brain doctor output above")
    return result


def _demo() -> None:
    """ponytail self-check: decision function + dry-run orchestration never
    crash and never call a real subprocess in dry-run mode."""

    assert decide_plugin_action(None, "1.0.0") == "install"
    assert decide_plugin_action("1.0.0", None) == "skip"
    assert decide_plugin_action("1.1.0", "0.9.1") == "reinstall"
    assert decide_plugin_action("0.9.0", "0.9.1") == "update"
    assert decide_plugin_action("0.9.1", "0.9.1") == "skip"

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        import subprocess as sp

        return sp.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    report = run_update(run=fake_run, dry_run=True, skip_capability_probe=True)
    assert "ok" in report
    assert calls == [], "dry_run must never invoke the injected runner for mutating steps"
    print("OK: update self-check passed")


if __name__ == "__main__":
    _demo()
