"""Execute release-refresh commands."""

from __future__ import annotations

import json

from .. import cli as shared

_emit = shared._emit


def _run_update(args, ctx) -> int:
    config = ctx.config
    from .. import update as brain_update

    if config.is_managed() and not args.dry_run:
        _emit(
            "brain update is disabled on a managed endpoint "
            "($BRAIN_MANAGED) — updates are deployed centrally. "
            "Use --dry-run to preview what a managed rollout would change.",
            args.json,
        )
        return 1
    report = brain_update.run_update(
        marketplace_name=args.marketplace,
        engine_src=args.engine_src,
        dry_run=args.dry_run,
        skip_capability_probe=args.skip_capability_probe,
    )
    if args.json:
        _emit(report, True)
    else:
        lines = [
            f"brain update — {'DRY RUN — ' if args.dry_run else ''}"
            f"{'PASS' if report['ok'] else 'FAIL/INCOMPLETE'}",
            "",
        ]
        for step_name, step_val in report["steps"].items():
            lines.append(f"[{step_name}]")
            lines.append(
                step_val
                if isinstance(step_val, str)
                else json.dumps(step_val, indent=2)
            )
            lines.append("")
        if report.get("before_after_rendered"):
            lines.append(report["before_after_rendered"])
            lines.append("")
        lines.append(f"notes: {report.get('notes', '')}")
        if report.get("residual_human_steps"):
            lines.append("")
            lines.append("Residual human step(s):")
            for step in report["residual_human_steps"]:
                lines.append(f"  - {step}")
        _emit(None, False, "\n".join(lines))
    return 0 if report["ok"] else 1


_HANDLERS = {
    "update": _run_update,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
