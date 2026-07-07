"""Per-user personalization overlay (PER-01 / PER-02).

The substrate (`vault/brain/`, `vault/raw/`) is generic — it carries NO
hard-coded owner identity. Brand/voice/keyword/people content that used to be
wired straight into the kernel is a **data-driven slot** any new owner fills
with their own: the overlay.

Layout (the generic, owner-agnostic shape):

    overlay/
    ├── voice/      *.md  — durable writing voice (tone, register, sign-offs)
    ├── brand/      *.md  — naming/anonymisation/title conventions
    ├── keywords/   *.md  — glossary / acronym / codename decoder ring
    └── people/     *.md  — the always-on people this owner's notes reference

Each file carries a small frontmatter block (`overlay_type: <category>`) so a
validator can check shape without guessing from folder name alone. See
`overlay/README.md` for the full schema + starter scaffold.

This module is intentionally **filesystem-only** — it never constructs a
`BrainCore` or opens the index. `brain init --validate-overlay` has to work on
a brand-new install before any index exists, so overlay validation must not
depend on one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import frontmatter

CATEGORIES: tuple[str, ...] = ("voice", "brand", "keywords", "people")

# AUT-01/AUT-03 (ADR-0003 Ruling c/e): the HTML brief/digest renderers are
# pure-render — all overlay I/O happens here, once, before the render call.
# Two OPTIONAL frontmatter keys on a brand/*.md file (alongside the existing
# overlay_type/title/updated) let an owner brand the generated HTML without
# any new overlay category or schema ceremony. Absent -> neutral fallback
# (zero hard-coded owner content is the kernel/overlay contract).
_DEFAULT_BRAND_TITLE = "Brain Brief"
_DEFAULT_ACCENT = "#2563eb"
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def overlay_dir(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the active overlay directory.

    Precedence: ``explicit`` arg (``--overlay-dir``) > ``$BRAIN_OVERLAY_DIR`` >
    ``<vault>/overlay`` (the overlay travels with the user's vault, alongside
    ``raw/`` and ``brain/`` — see AGENTS.md §1).
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BRAIN_OVERLAY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    from . import config

    return config.vault_root(vault) / "overlay"


def _validate_category_file(path: Path, category: str) -> list[str]:
    """Return a list of human-readable issues for one overlay file (empty = OK)."""
    issues: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - unreadable file is rare
        return [f"{path.name}: unreadable ({type(exc).__name__}: {exc})"]

    meta, body = frontmatter.parse_text(text)
    if not meta:
        issues.append(f"{path.name}: missing or unparseable frontmatter")
        return issues

    declared = meta.get("overlay_type")
    if declared != category:
        issues.append(
            f"{path.name}: overlay_type={declared!r} does not match its "
            f"directory ({category!r})"
        )
    if not body.strip():
        issues.append(f"{path.name}: frontmatter present but body is empty")
    return issues


def validate_overlay(path: Path) -> dict[str, Any]:
    """Validate an overlay directory's shape. Pure filesystem check.

    Required shape: ``path`` exists and contains, for EACH of ``CATEGORIES``,
    a subdirectory with at least one ``*.md`` file whose frontmatter declares
    ``overlay_type: <category>``. Returns a report dict (never raises on a
    malformed/missing overlay — that is what ``valid: false`` is for).
    """
    if not path.exists():
        return {
            "overlay_dir": str(path),
            "exists": False,
            "valid": False,
            "categories": {
                c: {"present": False, "file_count": 0, "issues": [f"{c}/ missing (overlay dir does not exist)"]}
                for c in CATEGORIES
            },
            "errors": [f"overlay dir does not exist: {path}"],
        }

    categories: dict[str, Any] = {}
    errors: list[str] = []

    for cat in CATEGORIES:
        cat_dir = path / cat
        issues: list[str] = []
        file_count = 0
        present = cat_dir.is_dir()
        if not present:
            issues.append(f"missing category directory: {cat}/")
        else:
            md_files = sorted(cat_dir.glob("*.md"))
            file_count = len(md_files)
            if file_count == 0:
                issues.append(f"{cat}/ exists but has no .md files")
            for f in md_files:
                issues.extend(_validate_category_file(f, cat))
        categories[cat] = {"present": present, "file_count": file_count, "issues": issues}
        errors.extend(f"{cat}: {issue}" for issue in issues)

    return {
        "overlay_dir": str(path),
        "exists": True,
        "valid": len(errors) == 0,
        "categories": categories,
        "errors": errors,
    }


def resolve_brand(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve brand data for the HTML brief/digest renderers (AUT-01/AUT-03).

    Reads the FIRST ``overlay/brand/*.md`` file (sorted, deterministic) if
    present and takes its ``title`` plus two optional keys — ``owner_name``,
    ``accent_color`` (a ``#rgb``/``#rrggbb`` hex string; anything else is
    ignored, never trusted into CSS unvalidated). Never raises; a missing
    overlay, empty ``brand/`` category, or unreadable file all fall back to
    the NEUTRAL default below — the renderer must never depend on an owner
    overlay existing.
    """
    result: dict[str, Any] = {
        "present": False,
        "title": _DEFAULT_BRAND_TITLE,
        "owner_name": None,
        "accent_color": _DEFAULT_ACCENT,
    }
    brand_dir = overlay_dir(vault, explicit) / "brand"
    brand_files = sorted(brand_dir.glob("*.md")) if brand_dir.is_dir() else []
    if not brand_files:
        return result
    try:
        text = brand_files[0].read_text(encoding="utf-8")
    except Exception:
        return result

    meta, _body = frontmatter.parse_text(text)
    if not meta:
        return result

    title = meta.get("title")
    owner_name = meta.get("owner_name")
    accent = meta.get("accent_color")
    if isinstance(title, str) and title.strip():
        result["title"] = title.strip()
    if isinstance(owner_name, str) and owner_name.strip():
        result["owner_name"] = owner_name.strip()
    if isinstance(accent, str) and _HEX_COLOR_RE.match(accent.strip()):
        result["accent_color"] = accent.strip()
    result["present"] = True
    return result
