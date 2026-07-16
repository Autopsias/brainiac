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
# ONB-02: seed a brand-new (empty) vault with generic sample notes
# --------------------------------------------------------------------------
# Fully generic content -- zero proper nouns (the release contamination scan
# is a hard gate). Plain filesystem writes, same posture as scaffold_overlay
# above: installer scaffolding, not captured content, so it never needs to go
# through the audited write_note path (a hand-authored note added directly to
# vault/brain/ is always valid -- Markdown+YAML is the substrate's single
# source of truth, the index is a derived cache). Host-only: the VM leg never
# writes directly into vault/brain/ even out-of-band (AGENTS.md §6 write
# split) -- run_full_init below skips this on client == "cowork".
_GENERATED_BRAIN_FILENAMES = {"backlinks.md", "catalog.md"}


def _existing_brain_note_count(vault: str | os.PathLike[str] | None) -> int:
    """Notes under ``vault/brain/`` excluding the top-level index.md and any
    generated file (backlinks.md, catalog.md) -- the "is this vault actually
    empty" check ``seed_sample_notes`` gates on."""
    brain_dir = config.vault_root(vault, allow_missing=True) / "brain"
    if not brain_dir.is_dir():
        return 0
    count = 0
    for p in brain_dir.rglob("*.md"):
        if p.name in _GENERATED_BRAIN_FILENAMES:
            continue
        if p.name == "index.md" and p.parent == brain_dir:
            continue
        count += 1
    return count


def _sample_notes(today: str) -> dict[str, str]:
    """``id -> full Markdown content`` for the 3 seeded sample notes: a
    welcome note (shape), a ``concept`` note (type + Counter-Arguments
    section), and their wikilinked partner (the "linked pair")."""
    return {
        "welcome-to-your-second-brain": f"""---
id: welcome-to-your-second-brain
title: "Welcome to your second brain"
type: note
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Welcome to your second brain

This is a sample note showing the note shape every file under `vault/brain/`
follows: YAML frontmatter (an `id`, a `title`, a `type`, a `classification`,
and `created`/`updated` dates) up top, then a Markdown body below the second
`---`.

Notes stay flat inside their PARA folder (`projects/`, `areas/`,
`resources/`, `archive/`) -- no nesting, no numbering. Structure comes from
**wikilinks**, not folders or tags: see [[example-linked-note]] for a small
worked pair, and [[example-concept]] for the `concept` note type.

Delete these three sample notes whenever you like -- they exist only to show
the shape before you write your own.
""",
        "example-concept": f"""---
id: example-concept
title: "Example concept note"
type: concept
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Example concept note

## Definition

A `concept` note captures one idea worth naming and reusing across other
notes -- a definition, a mental model, a recurring pattern.

## Context & Application

Link to a concept from wherever the idea applies, instead of re-explaining it
each time. See [[welcome-to-your-second-brain]] for the note-shape overview
this sample set demonstrates.

## Counter-Arguments

Reasons this concept might be wrong, incomplete, or context-dependent -- a
concept note without this section is warn-flagged by the validator as a
quality nudge. This sample note's own counter-argument: it isn't a real
concept, just a placeholder.

## Related Concepts

[[example-linked-note]]

## Sources
""",
        "example-linked-note": f"""---
id: example-linked-note
title: "Example linked note"
type: note
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Example linked note

This note and [[welcome-to-your-second-brain]] link to each other -- a small
worked example of the wikilink-first structure this vault uses instead of
folders or tags. It also links to [[example-concept]] to show a `note`
pointing at a `concept`.
""",
    }


def _sample_index(today: str) -> str:
    """A minimal top-level ``brain/index.md`` -- ``tools/validate.py`` hard-
    requires this file to exist (``vault/brain/index.md missing`` is an
    error, not a warning), and nothing else in the install path creates it
    for a genuinely brand-new vault, so seeding is the one place that can
    satisfy that gate. Create-if-absent only (see ``seed_sample_notes``) --
    never overwrites an owner's own index.md."""
    return f"""---
id: index
title: "Index"
type: index
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Index

Map of this vault. Start here, then follow the wikilinks.

## Sample notes

- [[welcome-to-your-second-brain]] -- the note shape (frontmatter, PARA folders, wikilinks)
- [[example-concept]] -- the `concept` note type
- [[example-linked-note]] -- a small wikilinked pair
"""


def seed_sample_notes(vault: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Write the 3 sample notes into ``vault/brain/resources/`` -- ONLY when
    the vault carries no real notes yet (idempotent: a second run against a
    now-populated vault is always a no-op, never a clobber). Also writes a
    minimal top-level ``brain/index.md`` create-if-absent (never overwrites
    an existing one) so the freshly seeded vault passes
    ``tools/validate.py``'s hard ``index.md missing`` gate."""
    v = config.vault_root(vault, allow_missing=True)
    existing = _existing_brain_note_count(v)
    if existing > 0:
        return {"performed": False,
                "reason": f"vault/brain/ already has {existing} note(s)",
                "created": []}
    today = _dt.date.today().isoformat()
    brain_dir = v / "brain"
    dest_dir = brain_dir / "resources"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # tools/validate.py hard-requires vault/raw/ to exist too (`vault/raw/
    # missing` is an error) -- an empty dir is a no-op to create and nothing
    # else in the install path creates it for a genuinely brand-new vault.
    (v / "raw").mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    index_path = brain_dir / "index.md"
    if not index_path.exists():
        index_path.write_text(_sample_index(today), encoding="utf-8")
        created.append("index.md")

    for note_id, content in _sample_notes(today).items():
        path = dest_dir / f"{note_id}.md"
        if path.exists():  # defensive: the emptiness gate above already
            continue        # implies these shouldn't exist yet
        path.write_text(content, encoding="utf-8")
        created.append(f"resources/{note_id}.md")
    return {"performed": True, "created": created}


# --------------------------------------------------------------------------
# ONB-01: brain init --full --import-from <dir> -- guided first ingest
# --------------------------------------------------------------------------
# Stages an external folder (e.g. an existing Obsidian vault) into
# vault/inbox/ and drives the STANDARD host ingest drain
# (brain.ingest.pipeline.run_ingest via BrainCore.ingest_dropzone) -- reuses
# the existing pipeline verbatim, never forks it. Host-only: refused
# (role_forbidden) before any filesystem side effect -- ingest_dropzone would
# refuse a VM leg anyway (BrainCore._require_host), but the check here runs
# BEFORE even the read-only dry-run scan, so a VM leg never touches the
# import folder at all.
#
# [HARDENED:codex] import safety: realpath-resolved overlap check in BOTH
# directions, symlinks never followed, a dry-run manifest gate (file count +
# bytes + per-extension breakdown) that requires explicit confirmation before
# anything is staged, and a default file-count/byte-size cap.
DEFAULT_IMPORT_FILE_CAP = 5000
DEFAULT_IMPORT_BYTES_CAP = 500 * 1024 * 1024


class ImportSafetyError(ValueError):
    """``--import-from`` failed a pre-flight safety check; nothing was staged."""


def _realpath(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(str(Path(p).expanduser())))


def validate_import_overlap(
    import_dir: str | os.PathLike[str], vault: str | os.PathLike[str] | None,
) -> None:
    """Reject either direction of overlap between ``import_dir`` and the
    resolved vault root.

    - ``import_dir`` inside (or equal to) the vault: would re-ingest the
      vault's own content (including its own ``inbox/``).
    - the vault inside ``import_dir``: the self-copy bomb -- the moment
      staging starts writing into ``vault/inbox/``, that new content becomes
      part of the very traversal source being walked.
    """
    imp = _realpath(import_dir)
    vlt = _realpath(config.vault_root(vault, allow_missing=True))
    if not imp.is_dir():
        raise ImportSafetyError(f"--import-from {imp} is not a directory")
    try:
        imp.relative_to(vlt)
    except ValueError:
        pass
    else:
        raise ImportSafetyError(
            f"--import-from {imp} is inside (or equal to) the vault {vlt}; refusing")
    try:
        vlt.relative_to(imp)
    except ValueError:
        pass
    else:
        raise ImportSafetyError(
            f"the vault {vlt} is inside --import-from {imp}; refusing -- this "
            "is the self-copy bomb (vault/inbox/ would become part of the "
            "traversal source once staging starts writing into it)")


def scan_import_dir(import_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Read-only walk of ``import_dir``: never follows a symlinked file or
    directory (HARDENED:codex). Returns a dry-run manifest -- file count,
    total bytes, per-extension breakdown -- plus the internal file list
    ``stage_import_files`` consumes to actually copy."""
    imp = Path(import_dir)
    files: list[tuple[Path, int]] = []
    total_bytes = 0
    by_extension: dict[str, int] = {}
    for root, dirnames, filenames in os.walk(imp, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [d for d in dirnames if not (root_path / d).is_symlink()]
        for name in filenames:
            fp = root_path / name
            if fp.is_symlink():
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            rel = fp.relative_to(imp)
            files.append((rel, size))
            total_bytes += size
            ext = fp.suffix.lower() or "(none)"
            by_extension[ext] = by_extension.get(ext, 0) + 1
    return {
        "import_dir": str(imp), "file_count": len(files), "total_bytes": total_bytes,
        "by_extension": by_extension, "_files": files,
    }


def check_import_caps(
    manifest: dict[str, Any], *, force: bool,
    file_cap: int | None = None, bytes_cap: int | None = None,
) -> None:
    # Resolved from the module globals AT CALL TIME (not as bound default
    # values) so a caller (or a test) can monkeypatch
    # DEFAULT_IMPORT_FILE_CAP/DEFAULT_IMPORT_BYTES_CAP and have it take effect.
    if file_cap is None:
        file_cap = DEFAULT_IMPORT_FILE_CAP
    if bytes_cap is None:
        bytes_cap = DEFAULT_IMPORT_BYTES_CAP
    if force:
        return
    if manifest["file_count"] > file_cap:
        raise ImportSafetyError(
            f"{manifest['file_count']} files exceeds the default cap ({file_cap}); "
            "pass --import-force to override")
    if manifest["total_bytes"] > bytes_cap:
        raise ImportSafetyError(
            f"{manifest['total_bytes']} bytes exceeds the default cap ({bytes_cap}); "
            "pass --import-force to override")


def build_import_dry_run(
    import_from: str | os.PathLike[str], vault: str | os.PathLike[str] | None,
    *, force: bool = False,
) -> dict[str, Any]:
    """Pre-flight: overlap + symlink-safe scan + cap check. Pure read-only
    filesystem inspection -- never stages or ingests anything."""
    validate_import_overlap(import_from, vault)
    manifest = scan_import_dir(import_from)
    check_import_caps(manifest, force=force)
    return manifest


def _flatten_relpath(rel: Path) -> str:
    """The ingest drain only scans the inbox ROOT (never recurses), so a
    nested import (e.g. an Obsidian vault's subfolders) is flattened into one
    filename per file -- joined with '__' so the original path stays visible
    and collisions across sibling subfolders are vanishingly unlikely (the
    dest-uniquification below is the actual guarantee)."""
    return "__".join(rel.parts) if len(rel.parts) > 1 else rel.parts[0]


def _unique_inbox_dest(inbox: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    dest = inbox / name
    i = 0
    while dest.exists():
        i += 1
        dest = inbox / f"{stem}.{i}{suffix}"
    return dest


def stage_import_files(
    manifest: dict[str, Any], import_from: str | os.PathLike[str],
    vault: str | os.PathLike[str] | None,
) -> list[str]:
    """Copy (never move) every file the dry-run manifest found into
    ``vault/inbox/``. The user's original folder is never touched."""
    v = config.vault_root(vault, allow_missing=True)
    imp = Path(import_from)
    inbox = v / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    for rel, _size in manifest["_files"]:
        src = imp / rel
        if src.is_symlink():
            continue
        dest = _unique_inbox_dest(inbox, _flatten_relpath(rel))
        shutil.copy2(src, dest)
        staged.append(dest.name)
    return staged


def stage_and_ingest_import(
    import_from: str | os.PathLike[str], vault: str | os.PathLike[str] | None,
    role: str, *, force: bool = False,
) -> dict[str, Any]:
    """Stage ``import_from`` into ``vault/inbox/`` then run the STANDARD host
    ingest drain (``BrainCore.ingest_dropzone`` -> ``ingest.pipeline.run_ingest``).

    Refused on ``role != host`` BEFORE any filesystem side effect --
    ``ingest_dropzone`` would refuse a VM leg on its own
    (``BrainCore._require_host``), but that only fires after staging already
    copied bytes into ``inbox/``; this check runs first so a VM leg never
    touches the import folder or the vault at all (same fail-closed shape as
    every other host-broker verb).
    """
    if role != config.ROLE_HOST:
        raise PermissionError(
            f"role={role!r} may not import + ingest a folder; this is a "
            "host-broker privilege (the VM leg is read + draft only). "
            "Run on the host.")
    manifest = build_import_dry_run(import_from, vault, force=force)
    staged = stage_import_files(manifest, import_from, vault)

    from .core import BrainCore

    core = BrainCore(vault=vault, role=role)
    ingest_report = core.ingest_dropzone()
    return {
        "import_dir": manifest["import_dir"], "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"], "by_extension": manifest["by_extension"],
        "staged": len(staged), "ingest": ingest_report,
    }


def render_import_dry_run(manifest: dict[str, Any]) -> str:
    lines = [
        f"import dry-run: {manifest['import_dir']}",
        f"  {manifest['file_count']} file(s), {manifest['total_bytes']} bytes total",
    ]
    for ext, n in sorted(manifest["by_extension"].items()):
        lines.append(f"    {ext}: {n}")
    lines.append("re-run with --yes to stage into vault/inbox/ and run the ingest drain")
    return "\n".join(lines)


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


def _build_index(vault: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Build the derived index for a freshly-seeded vault via a subprocess.

    ONB fix (2026-07-11): ``seed_sample_notes`` writes notes to ``vault/brain/``
    but nothing indexed them, so `brain init --full --apply` left a vault where
    the very first `brain search` returned zero hits (the documented "init then
    search" quickstart was broken). We shell out to `brain rebuild` rather than
    constructing a ``BrainCore`` here so this module stays index-free and light
    (importing BrainCore would pull the embedder into every `brain init`, even a
    dry-run/scaffold) — matching the module's "filesystem + subprocess only"
    contract. Invoked via ``python -m brain`` (see ``brain/__main__.py``) so it
    is PATH-independent. Soft-fails: a rebuild error is reported, never aborts
    init (a box without the embedder can still scaffold; the user reruns
    `brain rebuild` once the engine is whole).
    """
    argv = [sys.executable, "-m", "brain"]
    if vault is not None:
        argv += ["--vault", str(vault)]
    argv.append("rebuild")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    except Exception as exc:  # subprocess spawn failure, timeout, etc.
        return {"performed": False, "ok": False,
                "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode == 0:
        return {"performed": True, "ok": True}
    return {"performed": True, "ok": False,
            "reason": f"rebuild exit {proc.returncode}",
            "stderr": (proc.stderr or "").strip()[-500:]}


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
    repo_root = discover_repo_root()
    client = detect_client(role)
    ol_dir = ov.overlay_dir(vault, overlay_dir)
    tmpl = resolve_template_dir(template_dir, repo_root)

    steps: list[str] = [f"client detected: {client} (role={role})"]

    # ONB-02: seed 2-3 generic sample notes on a genuinely EMPTY vault, host
    # only (the VM leg never writes directly into vault/brain/ -- AGENTS.md
    # §6). Runs before the overlay scaffold; order between the two doesn't
    # matter, both are idempotent filesystem-only steps.
    if seed_vault and client == "host":
        seed_report = seed_sample_notes(vault)
    else:
        seed_report = {"performed": False,
                        "reason": "disabled (--no-seed-vault)" if not seed_vault
                                  else "seeding is host-only (vm role never writes "
                                       "directly into vault/brain/)",
                        "created": []}
    if seed_report["performed"]:
        steps.append(f"vault seed: wrote {len(seed_report['created'])} sample note(s)")
    else:
        steps.append(f"vault seed: skipped ({seed_report['reason']})")

    # ONB fix: a freshly SEEDED vault must be indexed on a real `--apply`
    # install, or the first `brain search` returns nothing (the documented
    # "init --apply then search" quickstart). Gated on --apply + host + seed
    # actually performed: --apply is the "really install this" signal (a bare
    # `brain init --full` stays a lighter scaffold — its docs carry an explicit
    # `brain rebuild`), a dry-run builds no index, and a non-empty vault (seed
    # skipped) keeps its own index rather than eating a surprise full re-embed
    # on a re-run. Index build is a subprocess (keeps this module BrainCore-free)
    # and soft-fails — a box without the embedder still inits, with a note to
    # rerun `brain rebuild`.
    if apply and client == "host" and seed_report["performed"]:
        index_report = _build_index(vault)
        if index_report["ok"]:
            steps.append("index build: rebuilt (seeded notes are searchable)")
        else:
            steps.append(f"index build: FAILED ({index_report['reason']}) "
                         "— run `brain rebuild` once the engine is available")
    else:
        index_report = {"performed": False,
                        "reason": "no seeded notes to index" if apply
                                  else "dry-run (no --apply)"}

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
        "seed": seed_report,
        "index": index_report,
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
    ]
    seed = report.get("seed") or {}
    if seed.get("performed"):
        lines.append(f"seed: wrote {len(seed['created'])} sample note(s)")
        for c in seed["created"]:
            lines.append(f"  + {c}")
    else:
        lines.append(f"seed: not performed ({seed.get('reason')})")
    imp = report.get("import")
    if imp:
        lines.append("")
        lines.append(f"import: {imp['import_dir']}")
        lines.append(f"  staged {imp['staged']}/{imp['file_count']} file(s), "
                     f"{imp['total_bytes']} bytes")
        ing = imp.get("ingest", {})
        lines.append(f"  ingest: {len(ing.get('processed', []))} processed, "
                     f"{len(ing.get('duplicates', []))} duplicate(s), "
                     f"{len(ing.get('quarantined', []))} quarantined")
    lines.append("")
    lines.append(f"overlay: {report['overlay']['overlay_dir']}")
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
        s = h.get("synthesis")
        if s:
            lines.append(f"  host leg task 2/2: task={s.get('task_id')}")
            lines.append(f"    already_registered: {s.get('already_registered')} "
                         f"({s.get('probe_detail')})")
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
