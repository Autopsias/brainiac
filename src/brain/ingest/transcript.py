"""ING-04 — transcript capture route (ADR-0003 Ruling 1 companion, S06).

Meeting transcripts are produced EXTERNALLY (the transcriber MCP/CLI — never
in-kernel, AGENTS.md) and are already Markdown, so unlike the binary
drop-zone (``ingest/pipeline.py``) there is no extraction step and no
"original binary vs. extracted Markdown" split: the transcript file's own
text IS the note body. What the generic drop-zone pipeline genuinely CANNOT
express is real-world provenance — its own ``origin`` always points at an
archived COPY of whatever was dropped, never at the real-world thing the
content was recorded from. This module's whole job is exactly that gap: an
EXPLICIT, caller-supplied ``origin`` (a source audio/video file path, or the
literal string ``"verbal"`` for a no-recording capture) plus an optional
``language`` stamp detected from the filename (never guessed from prose).

Reuses ``ingest.pipeline``'s hardened building blocks (create-exclusive
archival, control-char-safe frontmatter, the shared density gate, the SAME
content-hash dedup manifest as the binary pipeline) rather than
reimplementing them — one dedup universe across every ingest surface.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path
from typing import Any

from . import pipeline as _pipeline
from .handlers.base import density_gate
from ..notes import safe_slug

MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024
_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")

# Presence-based ONLY — "language if present in the filename" (S06 brief)
# means a literal recognised code as its own path segment (e.g.
# "standup_2026-07-05_en.md", "reuniao.pt.md"), never an inference from the
# transcript's prose. A wrong guess is worse than an absent field.
_LANG_CODES = ("en", "pt", "es", "fr", "de", "it", "nl")
_LANG_RE = re.compile(r"(?:^|[._-])(" + "|".join(_LANG_CODES) + r")(?:[._-]|$)", re.IGNORECASE)


def detect_language(filename: str) -> str | None:
    m = _LANG_RE.search(Path(filename).stem)
    return m.group(1).lower() if m else None


def _read_transcript(path: Path) -> tuple[bytes, str] | dict[str, Any]:
    """Read, decode, and density-check one transcript source."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"ok": False, "reason": f"transcript_read_error:{type(exc).__name__}: {exc}"}
    if size > MAX_TRANSCRIPT_BYTES:
        return {"ok": False, "reason": "file_too_large"}
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return {"ok": False, "reason": f"transcript_read_error:{type(exc).__name__}: {exc}"}
    text = _decode_transcript(raw_bytes)
    if text is None:
        return {"ok": False, "reason": "transcript_decode_error"}
    reason = density_gate(text)
    if reason:
        return {"ok": False, "reason": reason}
    return raw_bytes, text


def _decode_transcript(raw_bytes: bytes) -> str | None:
    for encoding in _ENCODINGS:
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _transcript_meta(
    *,
    slug: str,
    today: str,
    origin: str,
    body_sha: str,
    classification: str,
    language: str | None,
    document_date: str | None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": slug,
        "type": "source",
        "classification": classification,
        "captured": today,
        "origin": origin,
        "sha256": body_sha,
        "immutable": True,
    }
    if language:
        meta["language"] = language
    if document_date:
        meta["document_date"] = document_date
    return meta


def _existing_note_result(
    *,
    note_path: Path,
    body_sha: str,
    slug: str,
    path: Path,
    original_sha: str,
    vault: Path,
    manifest: dict[str, str],
) -> dict[str, Any] | None:
    """Resolve an id collision, returning ``None`` when no note exists."""
    if not note_path.exists():
        return None
    from .. import frontmatter as fm

    existing_meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
    if str(existing_meta.get("sha256", "")) != body_sha:
        return {
            "ok": False,
            "reason": "note_id_collision",
            "detail": f"raw/{slug}.md already exists with different content",
        }
    manifest[original_sha] = slug
    _pipeline._save_manifest(vault, manifest)
    return {"ok": True, "duplicate": True, "existing_id": slug, "file": str(path)}


def ingest_transcript(
    core: Any, path: str | Path, *, origin: str, language: str | None = None,
    document_date: str | None = None, classification: str = "Internal",
) -> dict[str, Any]:
    """Promote one transcript through the audited raw-note write path."""
    path = Path(path)
    vault = core.vault
    loaded = _read_transcript(path)
    if isinstance(loaded, dict):
        return loaded
    raw_bytes, text = loaded
    original_sha = hashlib.sha256(raw_bytes).hexdigest()
    manifest = _pipeline._load_manifest(vault)
    if original_sha in manifest:
        return {"ok": True, "duplicate": True, "existing_id": manifest[original_sha], "file": str(path)}
    today = _dt.date.today().isoformat()
    stem = _pipeline._slugify_stem(path.stem)
    slug = safe_slug(f"{today}-{stem}")
    archive_subdir = vault / "raw" / "originals" / f"{today}-{stem}"
    archive_path = archive_subdir / _pipeline._sanitize_archive_name(path.name)
    arch_status = _pipeline._create_exclusive_or_collision(archive_path, raw_bytes, known_sha=original_sha)
    if arch_status == "collision":
        return {
            "ok": False, "reason": "archive_collision",
            "detail": f"archived-original target already holds different content: {archive_path}",
        }
    from .. import autolink
    text, autolink_added = autolink.apply_autolinks(
        text, title=path.stem, origin=origin, vault=vault,
    )
    body_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    lang = language or detect_language(path.name)
    meta = _transcript_meta(
        slug=slug, today=today, origin=origin, body_sha=body_sha,
        classification=classification, language=lang, document_date=document_date,
    )
    note_rel = f"raw/{slug}.md"
    note_path = vault / note_rel
    existing = _existing_note_result(
        note_path=note_path, body_sha=body_sha, slug=slug, path=path,
        original_sha=original_sha, vault=vault, manifest=manifest,
    )
    if existing is not None:
        return existing
    content = _pipeline._build_frontmatter(meta, text)
    core.write_note(
        note_rel, content,
        reason=f"ingest-transcript {path.name} -> raw/{slug}.md (origin={origin})",
        subtree="raw",
    )
    manifest[original_sha] = slug
    _pipeline._save_manifest(vault, manifest)
    return {
        "ok": True, "duplicate": False, "id": slug, "note": note_rel,
        "archived": str(archive_path.relative_to(vault)),
        "classification": classification, "origin": origin, "language": lang,
        "file": str(path), "autolink_added": autolink_added,
    }
