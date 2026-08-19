"""Claude plugin CLI preflight, marketplace refresh, and plugin actions."""
from __future__ import annotations

import subprocess
from typing import Any, Optional

from .update_channels import Runner

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
    claude_bin = _update.resolve_claude_bin()
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
    claude_bin = _update.resolve_claude_bin() or "claude"
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
    cmp_ = _update._compare(installed, marketplace)
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
    claude_bin = _update.resolve_claude_bin() or "claude"
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

# Parent-namespace bind, deferred past this module's own defs (call-time
# resolution keeps `monkeypatch.setattr(update, "resolve_claude_bin", ...)`
# working exactly as before the split).
from . import update as _update  # noqa: E402
