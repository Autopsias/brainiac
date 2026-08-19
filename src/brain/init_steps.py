"""Full initialization steps."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable


InitCallback = Callable[..., Any]


@dataclass(frozen=True)
class InitStepCallbacks:
    """Source callbacks used by full initialization."""

    discover_repo_root: InitCallback
    detect_client: InitCallback
    overlay_dir: InitCallback
    resolve_template_dir: InitCallback
    seed_sample_notes: InitCallback
    build_index: InitCallback
    scaffold_overlay: InitCallback
    validate_overlay: InitCallback
    provision_signing_key: InitCallback
    resolve_manifest_path: InitCallback
    load_registrar: InitCallback
    register_tasks: InitCallback


def _seed_step(
    callbacks: InitStepCallbacks,
    vault: str | os.PathLike[str] | None,
    client: str,
    seed_vault: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Run the optional host vault seed."""
    if seed_vault and client == "host":
        seed_report = callbacks.seed_sample_notes(vault)
    else:
        seed_report = {
            "performed": False,
            "reason": (
                "disabled (--no-seed-vault)"
                if not seed_vault
                else "seeding is host-only (vm role never writes directly "
                "into vault/brain/)"
            ),
            "created": [],
        }
    if seed_report["performed"]:
        steps = [f"vault seed: wrote {len(seed_report['created'])} sample note(s)"]
    else:
        steps = [f"vault seed: skipped ({seed_report['reason']})"]
    return seed_report, steps


def _index_step(
    callbacks: InitStepCallbacks,
    vault: str | os.PathLike[str] | None,
    client: str,
    apply: bool,
    seed_report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Build the seeded index when the install promises an applied host, then
    put the seeded notes in the audit chain (85ec832: a new vault was born
    with its seed notes outside the chain). Signing runs after the index
    build and only when it succeeded, because ``brain write`` indexes as it
    signs and has nothing to write into otherwise."""
    if apply and client == "host" and seed_report["performed"]:
        index_report = callbacks.build_index(vault)
        if index_report["ok"]:
            steps = ["index build: rebuilt (seeded notes are searchable)"]
        else:
            steps = [
                f"index build: FAILED ({index_report['reason']}) "
                "— run `brain rebuild` once the engine is available"
            ]
        if index_report["ok"]:
            from . import init_seed

            sign_report = init_seed._sign_seeded_notes(
                vault, seed_report["created"])
            steps.append(
                f"audit chain: signed {len(sign_report['signed'])} seeded note(s)"
                if sign_report["ok"] else
                f"audit chain: signed {len(sign_report['signed'])}, "
                f"{len(sign_report['failed'])} unsigned — `brain doctor` reports "
                "them; sign with `brain write` once a signing key is available")
        else:
            sign_report = {"performed": False,
                           "reason": "index build failed — nothing signed into"}
        return index_report, sign_report, steps
    return {
        "performed": False,
        "reason": "no seeded notes to index" if apply else "dry-run (no --apply)",
    }, {
        "performed": False,
        "reason": "no seeded notes to sign" if apply else "dry-run (no --apply)",
    }, []


def _overlay_step(
    callbacks: InitStepCallbacks,
    active_overlay_dir: Any,
    resolved_template_dir: Any,
    scaffold: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Scaffold and validate the active overlay."""
    overlay_report: dict[str, Any] = {"overlay_dir": str(active_overlay_dir)}
    if scaffold:
        scaffold_report = callbacks.scaffold_overlay(
            active_overlay_dir,
            resolved_template_dir,
        )
        if scaffold_report["performed"]:
            steps = [
                f"overlay scaffold: created {len(scaffold_report['created'])} file(s), "
                f"skipped {len(scaffold_report['skipped'])} filled category(ies)"
            ]
        else:
            steps = [f"overlay scaffold: skipped ({scaffold_report.get('reason')})"]
    else:
        scaffold_report = {
            "performed": False,
            "reason": "disabled (--no-scaffold-overlay)",
            "created": [],
            "skipped": [],
        }
        steps = ["overlay scaffold: disabled"]
    overlay_report["scaffold"] = scaffold_report
    validation = callbacks.validate_overlay(active_overlay_dir)
    overlay_report["validation"] = validation
    steps.append(f"overlay validation: {'valid' if validation['valid'] else 'INVALID'}")
    return overlay_report, validation, steps


def _key_step(
    callbacks: InitStepCallbacks,
    client: str,
    apply: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Provision the host audit key when applying an install."""
    if client == "host" and apply:
        try:
            key_report = callbacks.provision_signing_key()
        except Exception as exc:
            key_report = {"status": "unavailable", "error": str(exc)}
        return key_report, [f"audit key: {key_report['status']}"]
    return {"status": "skipped (vm role or dry-run)"}, []


def _task_step(
    callbacks: InitStepCallbacks,
    client: str,
    repo_root: Any,
    vault: str | os.PathLike[str] | None,
    manifest: str | os.PathLike[str] | None,
    register_tasks: bool,
    apply: bool,
    save_cowork_prompt: str | os.PathLike[str] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Register scheduled tasks through the selected client leg."""
    if not register_tasks:
        return {"registrar": "disabled"}, [
            "task registration: disabled (--no-register-tasks)"
        ]
    manifest_path = callbacks.resolve_manifest_path(manifest, repo_root, vault)
    registrar = callbacks.load_registrar(repo_root)
    tasks_report = callbacks.register_tasks(
        client=client,
        registrar=registrar,
        manifest_path=manifest_path,
        apply=apply,
        save_cowork_prompt=save_cowork_prompt,
    )
    return tasks_report, [
        f"task registration ({client}): registrar={tasks_report.get('registrar')}"
    ]


def run_full_init(
    *,
    vault: str | os.PathLike[str] | None,
    overlay_dir: str | os.PathLike[str] | None,
    role: str,
    scaffold: bool = True,
    template_dir: str | os.PathLike[str] | None = None,
    register_tasks: bool = True,
    apply: bool = False,
    manifest: str | os.PathLike[str] | None = None,
    save_cowork_prompt: str | os.PathLike[str] | None = None,
    seed_vault: bool = True,
    callbacks: InitStepCallbacks,
) -> dict[str, Any]:
    """Run the full initialization sequence."""
    repo_root = callbacks.discover_repo_root()
    client = callbacks.detect_client(role)
    active_overlay_dir = callbacks.overlay_dir(vault, overlay_dir)
    resolved_template_dir = callbacks.resolve_template_dir(template_dir, repo_root)
    steps: list[str] = [f"client detected: {client} (role={role})"]
    seed_report, seed_steps = _seed_step(callbacks, vault, client, seed_vault)
    steps.extend(seed_steps)
    index_report, sign_report, index_steps = _index_step(
        callbacks,
        vault,
        client,
        apply,
        seed_report,
    )
    steps.extend(index_steps)
    overlay_report, validation, overlay_steps = _overlay_step(
        callbacks,
        active_overlay_dir,
        resolved_template_dir,
        scaffold,
    )
    steps.extend(overlay_steps)
    key_report, key_steps = _key_step(callbacks, client, apply)
    steps.extend(key_steps)
    tasks_report, task_steps = _task_step(
        callbacks,
        client,
        repo_root,
        vault,
        manifest,
        register_tasks,
        apply,
        save_cowork_prompt,
    )
    steps.extend(task_steps)

    host_leg = tasks_report.get("host") or {}
    apply_result = host_leg.get("apply_result")
    task_hard_fail = (
        isinstance(apply_result, dict)
        and apply_result.get("exit_code") not in (0, None)
    )
    index_hard_fail = bool(index_report.get("performed")) and not index_report.get("ok")
    ok = bool(validation["valid"]) and not task_hard_fail and not index_hard_fail

    return {
        "action": "init-full",
        "ok": ok,
        "client": client,
        "role": role,
        "repo_root": str(repo_root) if repo_root else None,
        "seed": seed_report,
        "index": index_report,
        # Deliberately NOT folded into `ok`, unlike the index build above: an
        # empty index is silent (retrieval just returns nothing), whereas an
        # unsigned note is already reported loudly by `invariants.unsigned_notes`
        # and by `brain doctor`. A box with no signing key should still init.
        "audit_sign": sign_report,
        "overlay": overlay_report,
        "audit_key": key_report,
        "tasks": tasks_report,
        "steps": steps,
    }


def _render_seed(report: dict[str, Any]) -> list[str]:
    """Render the seed section of an initialization report."""
    seed = report.get("seed") or {}
    if seed.get("performed"):
        lines = [f"seed: wrote {len(seed['created'])} sample note(s)"]
        for created in seed["created"]:
            lines.append(f"  + {created}")
    else:
        lines = [f"seed: not performed ({seed.get('reason')})"]
    return lines


def _render_import(report: dict[str, Any]) -> list[str]:
    """Render the optional import section of an initialization report."""
    import_report = report.get("import")
    if not import_report:
        return []
    lines = [f"import: {import_report['import_dir']}"]
    lines.append(
        f"  staged {import_report['staged']}/{import_report['file_count']} file(s), "
        f"{import_report['total_bytes']} bytes"
    )
    ingest = import_report.get("ingest", {})
    lines.append(
        f"  ingest: {len(ingest.get('processed', []))} processed, "
        f"{len(ingest.get('duplicates', []))} duplicate(s), "
        f"{len(ingest.get('quarantined', []))} quarantined"
    )
    return lines


def _render_overlay(report: dict[str, Any]) -> list[str]:
    """Render the overlay and validation sections of an initialization report."""
    lines = [f"overlay: {report['overlay']['overlay_dir']}"]
    scaffold_report = report["overlay"].get("scaffold", {})
    if scaffold_report.get("performed"):
        lines.append(
            f"  scaffold: +{len(scaffold_report['created'])} created, "
            f"{len(scaffold_report['skipped'])} category(ies) already filled"
        )
        for created in scaffold_report["created"]:
            lines.append(f"    + {created}")
    else:
        lines.append(f"  scaffold: not performed ({scaffold_report.get('reason')})")
    validation = report["overlay"]["validation"]
    lines.append(f"  valid: {validation['valid']}")
    for category, info in validation["categories"].items():
        status = "ok" if not info["issues"] else "ISSUES"
        lines.append(f"    {category}/: {status} ({info['file_count']} file(s))")
        for issue in info["issues"]:
            lines.append(f"      - {issue}")
    return lines


def _render_tasks(report: dict[str, Any]) -> list[str]:
    """Render the task registration section of an initialization report."""
    tasks = report["tasks"]
    lines = ["", f"tasks: registrar={tasks.get('registrar')}"]
    if tasks.get("manifest"):
        lines.append(f"  manifest: {tasks['manifest']}")
    if "host" in tasks:
        host = tasks["host"]
        lines.append(
            f"  host leg ({host.get('detected_os')}): task={host.get('task_id')} "
            f"action={host.get('action')} apply={tasks.get('apply')}"
        )
        lines.append(f"    already_registered: {host.get('already_registered')}")
        lines.append(f"    result: {host.get('apply_result')}")
        synthesis = host.get("synthesis")
        if synthesis:
            lines.append(f"  host leg task 2/2: task={synthesis.get('task_id')}")
            lines.append(
                f"    already_registered: {synthesis.get('already_registered')} "
                f"({synthesis.get('probe_detail')})"
            )
    if "cowork" in tasks:
        cowork = tasks["cowork"]
        lines.append(
            f"  cowork leg: {len(cowork['vm_eligible_tasks'])} poke-only "
            f"trigger(s) to register: {', '.join(cowork['vm_eligible_tasks'])}"
        )
        if cowork.get("saved_to"):
            lines.append(f"    paste-prompt saved to: {cowork['saved_to']}")
        else:
            lines.append(
                "    (paste-prompt in the --json report; re-run with "
                "--save-cowork-prompt <path> to write it out)"
            )
    if tasks.get("hint"):
        lines.append(f"  hint: {tasks['hint']}")
    return lines


def render_human(report: dict[str, Any]) -> str:
    """Render the human-readable initialization report."""
    lines = [
        f"brain init (full) — client={report['client']} role={report['role']}",
        f"ok: {report['ok']}",
        "",
    ]
    lines.extend(_render_seed(report))
    import_lines = _render_import(report)
    if import_lines:
        lines.append("")
        lines.extend(import_lines)
    lines.append("")
    lines.extend(_render_overlay(report))
    lines.extend(_render_tasks(report))
    return "\n".join(lines)
