"""Execute installation health commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .. import connect as _connect
from .. import cli as shared

_emit = shared._emit


def _prepare_init_import(
    args, ctx, brain_init
) -> tuple[dict[str, Any] | None, int | None]:
    """Stage a confirmed onboarding import through the host ingest path."""
    role = ctx.role
    config = ctx.config
    if not args.import_from:
        return None, None
    # ONB-01: refuse before even scanning the import folder on the VM.
    if role == config.ROLE_VM:
        msg = {
            "error": "role_forbidden",
            "role": role,
            "cmd": "init --import-from",
            "detail": "'init --import-from' stages + ingests a folder via "
            "the host ingest drain; the VM leg is read + draft "
            "only. Run it on the host.",
        }
        _emit(
            msg
            if args.json
            else "refused: 'init --import-from' is host-broker only "
            "(role=vm is read+draft). Run it on the host.",
            args.json,
        )
        return None, 4
    try:
        dry_run = brain_init.build_import_dry_run(
            args.import_from, args.vault, force=args.import_force
        )
    except brain_init.ImportSafetyError as exc:
        _emit(
            {"error": "import_safety", "detail": str(exc)}
            if args.json
            else f"import refused: {exc}",
            args.json,
        )
        return None, 2
    proceed = args.yes
    dry_run_printed = False
    if not proceed and not args.json and sys.stdin.isatty():
        sys.stdout.write(brain_init.render_import_dry_run(dry_run) + "\n")
        dry_run_printed = True
        proceed = input("Proceed with staging + ingest? [y/N] ").strip().lower() in (
            "y",
            "yes",
        )
    if not proceed:
        if args.json:
            _emit(
                {
                    "action": "init-import-dry-run",
                    "manifest": {
                        key: value for key, value in dry_run.items() if key != "_files"
                    },
                    "hint": "re-run with --yes to stage + ingest",
                },
                True,
            )
        else:
            human = "aborted: pass --yes to proceed non-interactively"
            if not dry_run_printed:
                human = brain_init.render_import_dry_run(dry_run) + "\n\n" + human
            _emit(None, False, human)
        return None, 2
    report = brain_init.stage_and_ingest_import(
        args.import_from, args.vault, role, force=args.import_force
    )
    return report, None


def _run_full_init(args, ctx) -> int:
    """Run the full first-install orchestration."""
    from .. import init as brain_init

    import_report, exit_code = _prepare_init_import(args, ctx, brain_init)
    if exit_code is not None:
        return exit_code
    report = brain_init.run_full_init(
        vault=args.vault,
        overlay_dir=args.overlay_dir,
        role=ctx.role,
        scaffold=args.scaffold_overlay,
        template_dir=args.template_dir,
        register_tasks=args.register_tasks,
        apply=args.apply,
        manifest=args.manifest,
        save_cowork_prompt=args.save_cowork_prompt,
        seed_vault=args.seed_vault,
    )
    if import_report is not None:
        report["import"] = import_report
    _emit(
        report if args.json else None,
        args.json,
        None if args.json else brain_init.render_human(report),
    )
    return 0 if report["ok"] else 1


def _render_overlay_report(report: dict[str, Any]) -> str:
    """Render the overlay validator's human-readable result."""
    lines = [f"overlay: {report['overlay_dir']}", f"valid: {report['valid']}"]
    for category, info in report["categories"].items():
        status = "ok" if not info["issues"] else "ISSUES"
        lines.append(f"  {category}/: {status} ({info['file_count']} file(s))")
        lines.extend(f"    - {issue}" for issue in info["issues"])
    lines.extend(f"  warning: {warning}" for warning in report.get("warnings", []))
    return "\n".join(lines)


def _run_init(args, ctx) -> int:
    from .. import overlay as ov

    if args.full:
        return _run_full_init(args, ctx)

    if not args.validate_overlay:
        detail = (
            "brain init: choose a mode — --validate-overlay (PER-02 shape "
            "check) or --full (INS-02 full install orchestration: "
            "overlay + per-client task registration). "
            "Run: brain init --validate-overlay | brain init --full"
        )
        _emit(
            {"error": "no_mode", "detail": detail} if args.json else detail, args.json
        )
        return 2
    path = ov.overlay_dir(args.vault, args.overlay_dir)
    report = ov.validate_overlay(path)
    if args.json:
        _emit(report, True)
    else:
        _emit(None, False, _render_overlay_report(report))
    return 0 if report["valid"] else 1


def _run_doctor(args, ctx) -> int:
    role = ctx.role
    config = ctx.config
    from .. import doctor as brain_doctor

    # Role-aware (2026-07-07 addendum, ADR-0005 Ruling 2): the VM leg only
    # ever sees the staged zero-install copy, so it gets its own surface
    # set. Structural fallback covers the staged shim, which invokes
    # `python3 -m brain.cli "$@"` directly and never sets $BRAIN_ROLE.
    vm_posture = role == config.ROLE_VM or brain_doctor.looks_like_vm_stage()
    if vm_posture:
        report = brain_doctor.run_doctor_vm(vault=args.vault)
    else:
        registry_fetch = None
        if getattr(args, "check_registry", False):

            def registry_fetch():  # noqa: E306 - single cached HTTPS read, opt-in only
                return {"pypi_version": brain_doctor.fetch_pypi_latest_version()}

        report = brain_doctor.run_doctor(
            registry_fetch=registry_fetch, vault=args.vault
        )
    _emit(
        report if args.json else None,
        args.json,
        None if args.json else brain_doctor.render_human(report),
    )
    return 0 if report["ok"] else 1


def _run_alerts(args, ctx) -> int:
    role = ctx.role
    config = ctx.config
    from .. import alerts as brain_alerts

    # A vault is OPTIONAL here, and demanding one was a real bug: the host
    # role sweeps the workspace registry and never needed a vault at all,
    # so `brain alerts` from any directory that is not a vault exited 3 and
    # the Codex hook reported "cannot check" (measured 2026-08-14). Resolve
    # leniently and let `collect` say what it could not reach — on the VM
    # leg an unresolved vault is a REPORTED gap, never a cheerful
    # "no alerts".
    try:
        alerts_vault = config.vault_root(args.vault)
    except config.VaultNotFoundError:
        alerts_vault = None
    report = brain_alerts.collect(role=role, vault=alerts_vault)
    if args.one_line:
        banner = brain_alerts.one_line(report)
        if banner:
            print(banner)
    else:
        _emit(
            report if args.json else None,
            args.json,
            None if args.json else brain_alerts.render_human(report),
        )
    # Exit 0 even with findings: this runs at session start in every
    # harness, and a non-zero exit reads as "the check itself broke".
    return 0


def _run_install_hook(args, ctx) -> int:
    """Place + register the SessionStart alert hook. HOST only.

    The Claude Code channel is the one the wiring table calls HARD, and until
    2026-08-20 nothing installed it: the script rode the wheel and every host
    but the author's got the soft AGENTS.md line instead."""
    from pathlib import Path

    from .. import session_hook
    from ..update import _packaged_script

    if ctx.role == "vm":
        print("install-hook is HOST-broker only (it writes the host's Claude "
              "Code config); refused on role=vm", file=sys.stderr)
        return 3

    claude_home = Path(args.claude_home).expanduser() if args.claude_home else (
        Path.home() / ".claude")
    result = session_hook.install(
        claude_home, _packaged_script(session_hook.HOOK_SCRIPT))
    _emit(
        result if args.json else None,
        args.json,
        None if args.json else session_hook.render_human(result),
    )
    return 0 if result["ok"] else 1


def _run_mcp_config(args, ctx) -> int:
    config = ctx.config

    vault = str(Path(config.vault_root(args.vault)).resolve())
    # Same entry shape `brain connect --client claude-desktop` WRITES
    # (connect.mcp_server_entry) — one builder, so print-only and
    # write-for-real can never drift apart (SUI-02 reconciliation).
    entry = _connect.mcp_server_entry(vault, args.name, args.max_tier)
    if args.json:
        _emit(entry, True)
    else:
        body = json.dumps(entry, indent=2)
        _emit(
            None,
            False,
            'Add this inside "mcpServers" in your MCP client config, then '
            "restart the client:\n"
            "  Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json\n"
            "  Claude Code:    ~/.claude.json (or `claude mcp add`)\n\n" + body,
        )
    return 0


def _run_provision_request(args, ctx) -> int:
    """PRV-10, VM side: stage a new-vault provisioning request marker.

    Dispatched BEFORE BrainCore construction (filesystem only; a brand-new
    vault has no index yet). VM_ALLOWED; the VM leg still never signs,
    registers, or touches the registry (`provision-drain` stays host-broker
    only).
    """
    from .. import provision

    res = provision.write_request(ctx.config.vault_root(args.vault), role=ctx.role)
    _emit(res if args.json else
          f"provision request: {res['status']}"
          + (f" — {res['note']}" if res.get("note") else ""), args.json)
    return 0


def _run_provision_drain(args, ctx) -> int:
    """PRV-10, host side: scan for pending requests and complete each.

    Also runs as a fold on the hourly maintain daily branch; refused on
    role=vm by the VM_ALLOWED gate in the facade.
    """
    from .. import provision

    res = provision.drain()
    if args.json:
        _emit(res, True)
    else:
        lines = [f"provision drain: {len(res['handled'])} request(s) handled, "
                 f"{len(res['roots'])} root(s) scanned"]
        for h in res["handled"]:
            lines.append(f"  {h.get('vault')}: {h.get('status')}"
                         + ("" if h.get("ok") else " (NOT ok)"))
        for s in res["stuck_claims"]:
            lines.append(f"  STUCK claim (crashed drain?): {s}")
        _emit(None, False, "\n".join(lines))
    return 0 if all(h.get("ok") for h in res["handled"]) else 1


_HANDLERS = {
    "init": _run_init,
    "doctor": _run_doctor,
    "alerts": _run_alerts,
    "install-hook": _run_install_hook,
    "mcp-config": _run_mcp_config,
    "provision-request": _run_provision_request,
    "provision-drain": _run_provision_drain,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
