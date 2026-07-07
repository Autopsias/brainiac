"""`brain init --full` — full first-run install orchestration (INS-02 / s09).

This EXTENDS the minimal PER-02 slice (`brain init --validate-overlay`, in
`brain.overlay` + `brain.cli`) into the single first-run command the installer
(`tools/cowork_workspace_install.sh` + the Intune package, INS-01) calls last:

  1. **Detect the client** from the trust role (host vs Cowork/VM).
  2. **Set up + validate the personalization overlay** — scaffold the generic
     `overlay/{voice,brand,keywords,people}/` layer from the shipped template
     when a category is empty (idempotent: never clobbers filled files), then
     run the same shape validator the `--validate-overlay` slice uses.
  3. **Drive per-client scheduled-task registration** through the s07 registrar
     (`scripts/register_tasks.py`):
       - **host** → register the ONE sanctioned OS task directly (launchd /
         Task Scheduler) via the registrar's host leg (read-only probe by
         default; `--apply` actually invokes the idempotent installer script).
       - **Cowork/VM** → PRINT the idempotent paste-prompt (the VM leg can
         never write/register from its read+draft role — persistence-budget.md
         locks the VM OS-scheduled count at 0), optionally saving it to a file.

Like `brain.overlay`, this module is **filesystem + subprocess only**: it never
constructs a `BrainCore` and never opens the index, so it works on a brand-new
install before any index exists. The `brain init` dispatch in `brain.cli` runs
BEFORE `BrainCore` construction for exactly this reason.

The s07 registrar is loaded by *file path* (importlib) rather than a package
import because `scripts/` is not part of the installed `brain` package. When the
registrar cannot be located (e.g. the bundled binary running far from the repo),
task registration degrades to a clear ``registrar_unavailable`` note rather than
crashing — overlay setup still completes, and the manifest path is surfaced so a
human can run the registrar by hand.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import config
from . import overlay as ov


# --------------------------------------------------------------------------
# repo / registrar / manifest / template discovery
# --------------------------------------------------------------------------

def discover_repo_root() -> Path | None:
    """Best-effort locate the brain repo root (holds ``scripts/`` + ``routines/``).

    Precedence: ``$BRAIN_REPO_ROOT`` > first ancestor of this file that carries
    ``scripts/register_tasks.py`` > ``None`` (bundled binary far from the repo).
    """
    env = os.environ.get("BRAIN_REPO_ROOT")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p.resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "register_tasks.py").exists():
            return parent
    return None


def load_registrar(repo_root: Path | None):
    """Load ``scripts/register_tasks.py`` as a module (or ``None`` if absent)."""
    if repo_root is None:
        return None
    path = repo_root / "scripts" / "register_tasks.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("brain._register_tasks", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_manifest_path(
    explicit: str | os.PathLike[str] | None,
    repo_root: Path | None,
    vault: str | os.PathLike[str] | None,
) -> Path | None:
    """Resolve the task manifest: explicit > ``$BRAIN_ROUTINES_MANIFEST`` >
    ``<vault>/.brain/routines/manifest.json`` (installer-landed) >
    ``<repo>/routines/manifest.json`` > ``None``."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    env = os.environ.get("BRAIN_ROUTINES_MANIFEST")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    try:
        installed = config.brain_runtime_dir(vault) / "routines" / "manifest.json"
    except Exception:  # pragma: no cover - vault_root is stable
        installed = None
    if installed and installed.exists():
        return installed
    if repo_root is not None:
        repo_manifest = repo_root / "routines" / "manifest.json"
        if repo_manifest.exists():
            return repo_manifest
    return None


def resolve_template_dir(
    explicit: str | os.PathLike[str] | None,
    repo_root: Path | None,
) -> Path | None:
    """Resolve the overlay template dir: explicit > ``<repo>/overlay/template`` >
    ``None`` (cannot scaffold)."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    if repo_root is not None:
        tmpl = repo_root / "overlay" / "template"
        if tmpl.is_dir():
            return tmpl
    return None


# --------------------------------------------------------------------------
# overlay scaffold (idempotent — never clobbers a filled category)
# --------------------------------------------------------------------------

def scaffold_overlay(overlay_dir: Path, template_dir: Path | None) -> dict[str, Any]:
    """Copy template files into any EMPTY overlay category. Idempotent.

    A category that already has ``*.md`` files is left untouched (``skipped``);
    an empty/missing category is filled from ``template_dir/<category>/*.md``
    (``created``). Returns a report; ``performed`` is False when no template is
    available (scaffolding is impossible, not an error — a user may fill the
    overlay by hand).
    """
    if template_dir is None:
        return {"performed": False, "reason": "no template dir available",
                "created": [], "skipped": []}
    created: list[str] = []
    skipped: list[str] = []
    for cat in ov.CATEGORIES:
        dst = overlay_dir / cat
        existing = list(dst.glob("*.md")) if dst.is_dir() else []
        if existing:
            skipped.append(cat)
            continue
        src = template_dir / cat
        if not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.glob("*.md")):
            target = dst / f.name
            if not target.exists():
                shutil.copy2(f, target)
                created.append(f"{cat}/{f.name}")
    return {"performed": True, "template_dir": str(template_dir),
            "created": created, "skipped": skipped}


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def detect_client(role: str) -> str:
    """Map the trust role to a client label. VM/Cowork is the read+draft leg."""
    return "cowork" if role == config.ROLE_VM else "host"


def _register_tasks(
    *, client: str, registrar, manifest_path: Path | None, apply: bool,
    save_cowork_prompt: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """Drive per-client registration through the s07 registrar (or degrade)."""
    if manifest_path is None:
        return {"registrar": "skipped", "reason": "no task manifest found",
                "hint": "pass --manifest <path> or land routines/manifest.json"}
    if registrar is None:
        return {"registrar": "unavailable",
                "manifest": str(manifest_path),
                "reason": "scripts/register_tasks.py not found from this install; "
                          "run it by hand against the manifest above",
                "hint": f"python3 scripts/register_tasks.py "
                        f"--client {client} --manifest {manifest_path}"}

    manifest = registrar.load_manifest(manifest_path)
    out: dict[str, Any] = {"registrar": "available", "manifest": str(manifest_path),
                           "client": client}
    if client == "host":
        out["host"] = registrar.register_host_leg(manifest, apply=apply)
        out["apply"] = apply
    else:  # cowork / vm — paste-prompt only, never host mutation
        prompt = registrar.build_cowork_prompt(manifest)
        out["cowork"] = {
            "vm_eligible_tasks": [t["id"] for t in manifest["tasks"] if t.get("vm_eligible")],
            "prompt": prompt,
        }
        if save_cowork_prompt:
            dest = Path(save_cowork_prompt)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(prompt, encoding="utf-8")
            out["cowork"]["saved_to"] = str(dest)
    return out


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
) -> dict[str, Any]:
    """Full `brain init` orchestration. Filesystem + subprocess only.

    Returns a report dict with an ``ok`` verdict (overlay valid AND no hard
    task-registration failure). Never raises on a malformed overlay or a missing
    manifest — those surface as ``ok: false`` / a task note.
    """
    repo_root = discover_repo_root()
    client = detect_client(role)
    ol_dir = ov.overlay_dir(vault, overlay_dir)
    tmpl = resolve_template_dir(template_dir, repo_root)

    steps: list[str] = [f"client detected: {client} (role={role})"]

    overlay_report: dict[str, Any] = {"overlay_dir": str(ol_dir)}
    if scaffold:
        sc = scaffold_overlay(ol_dir, tmpl)
        overlay_report["scaffold"] = sc
        if sc["performed"]:
            steps.append(
                f"overlay scaffold: created {len(sc['created'])} file(s), "
                f"skipped {len(sc['skipped'])} filled category(ies)")
        else:
            steps.append(f"overlay scaffold: skipped ({sc.get('reason')})")
    else:
        overlay_report["scaffold"] = {"performed": False, "reason": "disabled (--no-scaffold-overlay)",
                                      "created": [], "skipped": []}
        steps.append("overlay scaffold: disabled")

    validation = ov.validate_overlay(ol_dir)
    overlay_report["validation"] = validation
    steps.append(f"overlay validation: {'valid' if validation['valid'] else 'INVALID'}")

    # Provision the audit signing key BEFORE task registration so the nightly
    # plist render resolves a real key instead of MISSING_KEY_DRAIN_WILL_SKIP.
    # Create-if-absent only — provision_signing_key() never rotates. Host-only;
    # soft-fails (reported, never aborts init) so a storeless CI box still inits.
    key_report: dict[str, Any]
    if client == "host" and apply:
        from . import audit
        try:
            key_report = audit.provision_signing_key()
        except Exception as exc:
            key_report = {"status": "unavailable", "error": str(exc)}
        steps.append(f"audit key: {key_report['status']}")
    else:
        key_report = {"status": "skipped (vm role or dry-run)"}

    tasks_report: dict[str, Any]
    if register_tasks:
        manifest_path = resolve_manifest_path(manifest, repo_root, vault)
        registrar = load_registrar(repo_root)
        tasks_report = _register_tasks(
            client=client, registrar=registrar, manifest_path=manifest_path,
            apply=apply, save_cowork_prompt=save_cowork_prompt)
        steps.append(f"task registration ({client}): registrar={tasks_report.get('registrar')}")
    else:
        tasks_report = {"registrar": "disabled"}
        steps.append("task registration: disabled (--no-register-tasks)")

    # `ok` is driven by overlay validity + a HARD task failure only. Registrar
    # "unavailable"/"skipped" is a SOFT degradation (the report carries a hint to
    # finish registration by hand) — the common case for the bundled binary
    # running far from the repo, and NOT a reason to fail the whole install.
    host_leg = tasks_report.get("host") or {}
    apply_result = host_leg.get("apply_result")
    task_hard_fail = (
        isinstance(apply_result, dict) and apply_result.get("exit_code") not in (0, None)
    )
    ok = bool(validation["valid"]) and not task_hard_fail

    return {
        "action": "init-full",
        "ok": ok,
        "client": client,
        "role": role,
        "repo_root": str(repo_root) if repo_root else None,
        "overlay": overlay_report,
        "audit_key": key_report,
        "tasks": tasks_report,
        "steps": steps,
    }


def render_human(report: dict[str, Any]) -> str:
    """Compact human rendering of a run_full_init report."""
    lines = [
        f"brain init (full) — client={report['client']} role={report['role']}",
        f"ok: {report['ok']}",
        "",
        f"overlay: {report['overlay']['overlay_dir']}",
    ]
    sc = report["overlay"].get("scaffold", {})
    if sc.get("performed"):
        lines.append(f"  scaffold: +{len(sc['created'])} created, "
                     f"{len(sc['skipped'])} category(ies) already filled")
        for c in sc["created"]:
            lines.append(f"    + {c}")
    else:
        lines.append(f"  scaffold: not performed ({sc.get('reason')})")
    val = report["overlay"]["validation"]
    lines.append(f"  valid: {val['valid']}")
    for cat, info in val["categories"].items():
        status = "ok" if not info["issues"] else "ISSUES"
        lines.append(f"    {cat}/: {status} ({info['file_count']} file(s))")
        for issue in info["issues"]:
            lines.append(f"      - {issue}")

    t = report["tasks"]
    lines.append("")
    lines.append(f"tasks: registrar={t.get('registrar')}")
    if t.get("manifest"):
        lines.append(f"  manifest: {t['manifest']}")
    if "host" in t:
        h = t["host"]
        lines.append(f"  host leg ({h.get('detected_os')}): task={h.get('task_id')} "
                     f"action={h.get('action')} apply={t.get('apply')}")
        lines.append(f"    already_registered: {h.get('already_registered')}")
        lines.append(f"    result: {h.get('apply_result')}")
    if "cowork" in t:
        c = t["cowork"]
        lines.append(f"  cowork leg: {len(c['vm_eligible_tasks'])} poke-only "
                     f"trigger(s) to register: {', '.join(c['vm_eligible_tasks'])}")
        if c.get("saved_to"):
            lines.append(f"    paste-prompt saved to: {c['saved_to']}")
        else:
            lines.append("    (paste-prompt in the --json report; re-run with "
                         "--save-cowork-prompt <path> to write it out)")
    if t.get("hint"):
        lines.append(f"  hint: {t['hint']}")
    return "\n".join(lines)
