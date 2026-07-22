"""LNK-01 — capture-time auto-linking.

Root cause (operator-approved, 2026-07-20): 49 of 61 isolated notes in the
live vault are swept documents/transcripts that NAME their origin (attendee
names in transcript titles/bodies, a source project token) yet land with
zero wikilinks. This module proposes CONSERVATIVE wikilink additions from
that already-present evidence and applies them to the note BODY at capture
time — before the note is signed, so the signed note is born linked.

Two evidence-gated rules, never speculative:
  1. transcript/document text -> `type: person` note, on a FULL-NAME match
     (first+last both present) — a bare first name never links.
  2. transcript/document text -> `type: project`/`type: moc` note, only when
     exactly ONE candidate matches (ambiguous -> no link).

Zero matches -> zero links -> the note lands exactly as it does today.
A linking failure (bad frontmatter elsewhere, scan error, ...) never blocks
a capture: every entry point here is exception-safe by construction.

Config: ``$BRAIN_AUTOLINK=off`` disables (default on) — same on/off idiom as
``maintenance.NOTIFY_ENV``.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from .graph import parse_wikilinks
from .notes import scan_vault

AUTOLINK_ENV = "BRAIN_AUTOLINK"


def enabled() -> bool:
    return os.environ.get(AUTOLINK_ENV, "").strip().lower() != "off"


def _fold(text: str) -> str:
    """Accent-fold + lowercase into a hyphen-joined slug (same normalize/fold
    idiom as ``cos._is_keeper_counterparty``, reused rather than duplicated
    with different edge-case behaviour)."""
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


def _typed_notes(vault: Path, types: tuple[str, ...]) -> list[tuple[str, str]]:
    """[(note_id, folded-title-slug)] for every brain/ note of one of ``types``."""
    out: list[tuple[str, str]] = []
    for note in scan_vault(vault):
        if note.zone == "brain" and note.type in types:
            out.append((note.id, _fold(note.title)))
    return out


def find_person_links(text: str, vault: Path) -> list[str]:
    """Full-name matches of existing ``type: person`` notes in ``text``.

    A person note's title must fold to at least two hyphen-separated parts
    (first + last) to be eligible at all — a person note on file with only a
    first name can never be matched, and a bare first name mentioned in
    ``text`` never links on its own."""
    folded_text = _fold(text)
    hits: list[str] = []
    for note_id, name_slug in _typed_notes(vault, ("person",)):
        parts = [p for p in name_slug.split("-") if p]
        if len(parts) < 2:
            continue
        if name_slug in folded_text:
            hits.append(note_id)
    return hits


def find_project_link(text: str, vault: Path) -> list[str]:
    """Origin-context match against ``type: project``/``type: moc`` notes.

    Returns a match only when exactly one candidate's folded title is a
    substring of the folded ``text`` — an ambiguous (0 or >=2) result links
    nothing."""
    folded_text = _fold(text)
    candidates = [
        note_id for note_id, title_slug in _typed_notes(vault, ("project", "moc"))
        if title_slug and title_slug in folded_text
    ]
    return candidates if len(candidates) == 1 else []


_RELATED_HEADING_RE = re.compile(r"^## Related[ \t]*$", re.MULTILINE)


def _append_related(body: str, note_ids: list[str]) -> str:
    new_lines = "\n".join(f"- [[{nid}]]" for nid in note_ids)
    m = _RELATED_HEADING_RE.search(body)
    if m:
        insert_at = m.end()
        return body[:insert_at] + "\n" + new_lines + body[insert_at:]
    return f"{body.rstrip(chr(10))}\n\n## Related\n{new_lines}\n"


def apply_autolinks(
    body: str, *, title: str = "", origin: str = "", vault: Path,
) -> tuple[str, list[str]]:
    """Propose + apply conservative wikilinks to ``body``. Returns
    ``(new_body, added_note_ids)`` — ``added_note_ids`` is empty (and
    ``new_body is body``) on zero matches OR on any internal failure; never
    raises, so every capture-time caller can call this unconditionally."""
    if not enabled():
        return body, []
    try:
        search_text = "\n".join(t for t in (title, body) if t)
        candidates = list(find_person_links(search_text, vault))
        candidates.extend(find_project_link("\n".join(t for t in (title, origin) if t), vault))
        existing = set(parse_wikilinks(body))
        new_ids = [nid for nid in dict.fromkeys(candidates) if nid not in existing]
        if not new_ids:
            return body, []
        return _append_related(body, new_ids), new_ids
    except Exception:  # noqa: BLE001 — linking never blocks a capture
        return body, []
