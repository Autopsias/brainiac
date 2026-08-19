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

import datetime as _dt
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config
from . import overlay as ov
# The post-seed subprocess steps (index build, audit-chain signing) live in a
# sibling module so this one stays under its size bound; re-exported because
# `brain init`'s report and the tests both address them by these names.
from . import init_seed
from .init_seed import _build_index, _sign_seeded_notes  # noqa: F401
from .init_steps import (
    InitStepCallbacks,
    render_human as render_human_impl,
    run_full_init as run_full_init_impl,
)


# --------------------------------------------------------------------------
# repo / registrar / manifest / template discovery
# --------------------------------------------------------------------------

def packaged_assets_root() -> Path | None:
    """The wheel-shipped scaffold/registration assets (PYP-02).

    ``src/brain/_assets/`` MIRRORS the repo-root layout (``AGENTS.md``,
    ``templates/``, ``overlay/template/``, ``routines/manifest.json``,
    ``scripts/register_tasks.py`` + installer scripts), so every root-relative
    resolution below works identically against a checkout or the installed
    wheel. Synced by ``tools/package_clients.py`` — never hand-edited.
    """
    try:
        from importlib.resources import files
        root = Path(str(files("brain") / "_assets"))
    except Exception:  # pragma: no cover - stdlib present on >=3.9
        return None
    # ponytail: zipimport-backed installs (str() not a real path) fall through
    # to the checkout; pip installs wheels unpacked, so this is the normal path.
    if (root / "scripts" / "register_tasks.py").is_file():
        return root
    return None


def discover_repo_root() -> Path | None:
    """Best-effort locate the scaffold/registration asset root.

    Precedence: ``$BRAIN_REPO_ROOT`` (explicit override) > the wheel-shipped
    ``brain/_assets`` mirror (importlib.resources — PYP-02 resolution order:
    package resources first) > first ancestor of this file that carries
    ``scripts/register_tasks.py`` (plain source checkout on ``sys.path``) >
    ``None``.
    """
    env = os.environ.get("BRAIN_REPO_ROOT")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p.resolve()
    packaged = packaged_assets_root()
    if packaged is not None:
        return packaged
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
    seed_vault: bool = True,
) -> dict[str, Any]:
    """Full `brain init` orchestration. Filesystem + subprocess only.

    Returns a report dict with an ``ok`` verdict (overlay valid AND no hard
    task-registration failure). Never raises on a malformed overlay or a missing
    manifest — those surface as ``ok: false`` / a task note.
    """
    from . import audit

    callbacks = InitStepCallbacks(
        discover_repo_root=discover_repo_root,
        detect_client=detect_client,
        overlay_dir=ov.overlay_dir,
        resolve_template_dir=resolve_template_dir,
        seed_sample_notes=seed_sample_notes,
        # Late-bound on purpose: tests monkeypatch `init_seed._build_index`
        # by name; capturing the function object here would freeze the
        # pre-patch one and run a REAL rebuild against the fixture vault.
        build_index=lambda vault: init_seed._build_index(vault),
        scaffold_overlay=scaffold_overlay,
        validate_overlay=ov.validate_overlay,
        provision_signing_key=audit.provision_signing_key,
        resolve_manifest_path=resolve_manifest_path,
        load_registrar=load_registrar,
        register_tasks=_register_tasks,
    )
    return run_full_init_impl(
        vault=vault,
        overlay_dir=overlay_dir,
        role=role,
        scaffold=scaffold,
        template_dir=template_dir,
        register_tasks=register_tasks,
        apply=apply,
        manifest=manifest,
        save_cowork_prompt=save_cowork_prompt,
        seed_vault=seed_vault,
        callbacks=callbacks,
    )


def render_human(report: dict[str, Any]) -> str:
    """Compact human rendering of a run_full_init report."""
    return render_human_impl(report)

# The seeded sample notes live in init_samples.py and the ``--import-from``
# safety/staging leg in init_import.py since the 2026-08-16 size ratchet;
# re-exported so every `brain.init.<name>` caller is unchanged.
from .init_import import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_IMPORT_BYTES_CAP as DEFAULT_IMPORT_BYTES_CAP,
    DEFAULT_IMPORT_FILE_CAP as DEFAULT_IMPORT_FILE_CAP,
    ImportSafetyError as ImportSafetyError,
    _flatten_relpath as _flatten_relpath,
    _realpath as _realpath,
    _unique_inbox_dest as _unique_inbox_dest,
    build_import_dry_run as build_import_dry_run,
    check_import_caps as check_import_caps,
    render_import_dry_run as render_import_dry_run,
    scan_import_dir as scan_import_dir,
    stage_and_ingest_import as stage_and_ingest_import,
    stage_import_files as stage_import_files,
    validate_import_overlap as validate_import_overlap,
)
from .init_samples import (  # noqa: E402,F401  (facade re-export)
    _GENERATED_BRAIN_FILENAMES as _GENERATED_BRAIN_FILENAMES,
    _existing_brain_note_count as _existing_brain_note_count,
    _sample_index as _sample_index,
    _sample_notes as _sample_notes,
    seed_sample_notes as seed_sample_notes,
)

