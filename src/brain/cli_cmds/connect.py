"""Execute client-wiring commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .. import connect as _connect
from .. import cli as shared

_emit = shared._emit


def _connect_confirm(preview: str, args) -> bool:
    """Apply the one confirmation gate used by every connect mutation."""
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        return False
    sys.stdout.write(preview + "\n")
    return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


def _connect_file_step(plan: _connect.ConnectPlan, args, *, remove: bool) -> dict:
    """Apply a diff-first file connect plan after confirmation."""
    if plan.action == "noop":
        detail = (
            {"detail": "nothing to unwire"} if remove else {"already_connected": True}
        )
        return {"path": str(plan.target_path), "action": "noop", "diff": "", **detail}
    preview = plan.diff or f"(would create {plan.target_path})"
    if not _connect_confirm(preview, args):
        return {
            "path": str(plan.target_path),
            "action": plan.action,
            "diff": plan.diff,
            "confirmed": False,
            "detail": "not confirmed — pass --yes to proceed non-interactively",
        }
    if remove:
        _connect.apply_remove_marked_block(plan)
    elif plan.target_path.suffix == ".json":
        _connect.apply_json_merge(plan)
    else:
        _connect.apply_marked_block(plan)
    return {
        "path": str(plan.target_path),
        "action": plan.action,
        "diff": plan.diff,
        "confirmed": True,
    }


def _restore_file(path: Path, args) -> tuple[list[dict], bool]:
    """Restore one JSON client configuration from its connect backup."""
    found = _connect.plan_restore_from_backup(path)
    if not found["ok"]:
        return [{"path": str(path), "action": "noop", "detail": found["reason"]}], True
    if not _connect_confirm(f"restore {path} from backup {found['backup']}", args):
        step = {
            "path": str(path),
            "action": "restore",
            "confirmed": False,
            "detail": "not confirmed — pass --yes to proceed non-interactively",
        }
        return [step], False
    _connect.apply_restore_from_backup(path, Path(found["backup"]))
    return [
        {
            "path": str(path),
            "action": "restore",
            "backup": found["backup"],
            "confirmed": True,
        }
    ], True


def _connect_desktop(args, vault: str, target_dir: Path) -> tuple[list[dict], bool]:
    """Wire the Claude Desktop MCP configuration."""
    path = _connect.claude_desktop_config_path()
    if args.remove:
        return _restore_file(path, args)
    step = _connect_file_step(
        _connect.plan_claude_desktop(path, vault, args.name, args.max_tier),
        args,
        remove=False,
    )
    return [step], bool(step.get("already_connected") or step.get("confirmed", False))


def _connect_gemini(args, vault: str, target_dir: Path) -> tuple[list[dict], bool]:
    """Wire the Gemini project configuration."""
    path = target_dir / ".gemini" / "settings.json"
    if args.remove:
        return _restore_file(path, args)
    step = _connect_file_step(_connect.plan_gemini(path), args, remove=False)
    return [step], bool(step.get("already_connected") or step.get("confirmed", False))


def _connect_codex(args, vault: str, target_dir: Path) -> tuple[list[dict], bool]:
    """Wire the Codex project instruction file."""
    path = target_dir / "AGENTS.md"
    plan = (
        _connect.plan_remove_marked_block(path)
        if args.remove
        else _connect.plan_marked_block(path)
    )
    step = _connect_file_step(plan, args, remove=args.remove)
    ok = (
        step.get("already_connected")
        or step.get("confirmed", False)
        or step["action"] == "noop"
    )
    return [step], bool(ok)


def _connect_claude_code(args, vault: str, target_dir: Path) -> tuple[list[dict], bool]:
    """Wire the Claude Code plugin plus its project instruction file."""
    steps: list[dict] = []
    ok = True
    claude_home = Path.home() / ".claude"
    if args.remove:
        available = _connect.claude_plugin_cli_available()
        preview = f"claude plugin uninstall {_connect.KERNEL_PLUGIN}@{_connect.MARKETPLACE_NAME}"
        if available and _connect_confirm(preview, args):
            result = _connect.run_claude_code_plugin_uninstall(claude_home=claude_home)
            steps.append({"kind": "plugin", "confirmed": True, **result})
            ok = result["ok"]
        elif not available:
            steps.append(
                {
                    "kind": "plugin",
                    "detail": "`claude plugin` CLI not available; "
                    f"run manually: {preview}",
                }
            )
        else:
            steps.append(
                {
                    "kind": "plugin",
                    "confirmed": False,
                    "detail": "not confirmed — pass --yes to proceed non-interactively",
                }
            )
            ok = False
    elif _connect.is_plugin_installed(claude_home):
        steps.append({"kind": "plugin", "action": "noop", "already_connected": True})
    else:
        available = _connect.claude_plugin_cli_available()
        commands = _connect.claude_code_plugin_commands(args.marketplace_source)
        if not available:
            steps.append(
                {
                    "kind": "plugin",
                    "action": "manual",
                    "detail": "`claude` plugin CLI not detected/usable — run these two "
                    "commands yourself (guided, not one-command, for this client):",
                    "commands": [" ".join(command) for command in commands],
                }
            )
        elif _connect_confirm(
            "\n".join(" ".join(command) for command in commands), args
        ):
            result = _connect.run_claude_code_plugin_install(
                marketplace_source=args.marketplace_source, claude_home=claude_home
            )
            steps.append({"kind": "plugin", "confirmed": True, **result})
            ok = result["ok"]
        else:
            steps.append(
                {
                    "kind": "plugin",
                    "confirmed": False,
                    "commands": [" ".join(command) for command in commands],
                    "detail": "not confirmed — pass --yes to proceed non-interactively",
                }
            )
            ok = False
    path = target_dir / "CLAUDE.md"
    plan = (
        _connect.plan_remove_marked_block(path)
        if args.remove
        else _connect.plan_marked_block(path)
    )
    step = _connect_file_step(plan, args, remove=args.remove)
    steps.append(step)
    file_ok = (
        step.get("already_connected")
        or step.get("confirmed", False)
        or step["action"] == "noop"
    )
    return steps, bool(ok and file_ok)


def _render_connect(report: dict[str, Any]) -> str:
    """Render a connect result without changing its JSON contract."""
    client = report["client"]
    lines = [
        f"brain connect --client {client}{' --remove' if report['removed'] else ''} — "
        f"{'OK' if report['ok'] else 'INCOMPLETE'}"
    ]
    for step in report["steps"]:
        label = step.get("path", step.get("kind"))
        if step.get("already_connected"):
            lines.append(f"  {label}: already connected")
        elif step.get("confirmed"):
            lines.append(f"  {label}: wired")
        else:
            lines.append(f"  {label}: {step.get('detail', step.get('action'))}")
            if step.get("diff"):
                lines.append(step["diff"])
            lines.extend(f"    {command}" for command in step.get("commands", []))
    return "\n".join(lines)


def command_connect(args, ctx) -> int:
    """Dispatch one connect client through its isolated mutation plan."""
    target_dir = Path(args.target).resolve()
    vault = str(Path(ctx.config.vault_root(args.vault)).resolve())
    handlers = {
        "claude-desktop": _connect_desktop,
        "gemini": _connect_gemini,
        "codex": _connect_codex,
        "claude-code": _connect_claude_code,
    }
    steps, ok = handlers[args.client](args, vault, target_dir)
    report = {"client": args.client, "removed": args.remove, "steps": steps, "ok": ok}
    _emit(
        report if args.json else None,
        args.json,
        None if args.json else _render_connect(report),
    )
    return 0 if ok else 2


def _run_connect(args, ctx) -> int:
    return command_connect(args, ctx)


_HANDLERS = {
    "connect": _run_connect,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
