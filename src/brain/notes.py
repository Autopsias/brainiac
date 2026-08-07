"""Note model + vault scanning. Markdown files are the single source of truth."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import frontmatter


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# TMP-02: bitemporal frontmatter (ADR-0003 Ruling 2) — mirrors the id-resolution
# tools/validate.py already does (link_id) so a "[[id]]"/"[[id|alias]]" wikilink
# value indexes the same as a bare id.
_WIKILINK = re.compile(r"^\[\[([^\]|]+)(?:\|[^\]]*)?\]\]$")


def _bitemporal_link(val: object) -> str:
    if not isinstance(val, str) or not val.strip():
        return ""
    m = _WIKILINK.match(val.strip())
    return m.group(1).strip() if m else val.strip()


def _bitemporal_bool(val: object) -> str:
    """Normalize a raw frontmatter bool to "true"/"false"/"" (unset) for the
    index column — never a Python bool, so SQL equality stays trivial."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "yes"):
            return "true"
        if v in ("false", "no"):
            return "false"
    return ""


# Bare-slug charset for note ids that become filesystem paths (kebab slugs like
# `arctic-embed-choice`, `2026-06-27-arctic-benchmark`, `draft-ab12cd34ef56`).
# No separators, no leading dot, and '..' is rejected explicitly below.
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


#: Longest id we will turn into a path component, in ENCODED BYTES. Filesystem
#: components cap at 255 bytes; this leaves room for the suffixes and stamps
#: callers append (``<id>.anchor.json``, ``<ts>-<id>.md.refused``, …).
MAX_SLUG_BYTES = 180


def safe_slug(ident: object) -> str:
    """Validate an (untrusted) note id as a bare, path-safe slug — fail closed.

    Trust-boundary guard (C-1/C-2): an id from --id or untrusted YAML
    frontmatter becomes ``<dir>/<id>.md``, so anything but a bare slug
    (separators, '..', absolute paths, control chars, empty) is a traversal
    vector and is REFUSED with ValueError — never silently renamed.

    The id is NFC-normalized BEFORE validation so a decomposed variant cannot
    reconstitute '..' or '/' after the check (fullwidth forms are not in the
    allowed charset, so they are rejected outright).

    LENGTH is part of being path-safe. A 10,000-character all-ASCII id passes
    every character rule, becomes a filename, and raises ``ENAMETOOLONG`` at the
    write — which, on the broker's apply/recovery path, aborted the operation
    and left the journal to retry it forever. The cap is on ENCODED BYTES
    (a filesystem component limit is bytes, and NFC still leaves multibyte
    characters), and it is deliberately below the usual 255 so the suffixes and
    timestamp prefixes this codebase appends — ``.anchor.json``,
    ``.refused.json``, ``<YYYYMMDDTHHMMSS>-`` — still fit.
    """
    s = unicodedata.normalize("NFC", str(ident))
    if not s or ".." in s or _SLUG_RE.fullmatch(s) is None:
        raise ValueError(
            f"unsafe note id {ident!r}: must be a bare slug "
            "([A-Za-z0-9._-], no leading '.', no '/', no '..')"
        )
    n = len(s.encode("utf-8"))
    if n > MAX_SLUG_BYTES:
        raise ValueError(
            f"unsafe note id {str(ident)[:40]!r}…: {n} encoded bytes exceeds the "
            f"{MAX_SLUG_BYTES}-byte path-component limit"
        )
    return s


@dataclass
class Note:
    id: str
    title: str
    type: str
    classification: str  # RAW frontmatter value (may be missing -> "" ; filter applies default-deny)
    zone: str            # "raw" | "brain"
    path: Path
    body: str
    meta: dict[str, Any] = field(default_factory=dict)
    created: str = ""
    updated: str = ""
    sha256: str = ""
    content_hash: str = ""  # sha256 of the FULL on-disk file text (change detection)
    # TMP-02 bitemporal keys (ADR-0003 Ruling 2) — all optional, "" when absent.
    document_date: str = ""
    effective_date: str = ""
    superseded_date: str = ""
    is_latest_version: str = ""   # "true" | "false" | "" (unset -> treated as current)
    superseded_by: str = ""
    previous_version: str = ""    # previous_version, falling back to `replaces`

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "classification": self.classification,
            "zone": self.zone,
            "path": self.path.as_posix(),
            "created": self.created,
            "updated": self.updated,
            "sha256": self.sha256,
            "content_hash": self.content_hash,
            "body": self.body,
            "document_date": self.document_date,
            "effective_date": self.effective_date,
            "superseded_date": self.superseded_date,
            "is_latest_version": self.is_latest_version,
            "superseded_by": self.superseded_by,
            "previous_version": self.previous_version,
        }


def _zone_of(path: Path, vault: Path) -> str:
    try:
        rel = path.relative_to(vault)
    except ValueError:
        return "brain"
    return rel.parts[0] if rel.parts else "brain"


def load_note(path: Path, vault: Path) -> Note | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        # H-3: a single bad-encoding (or unreadable) file must not abort a
        # whole-vault rebuild/sync. Skip it with a warning rather than
        # silently mangling its content (no errors="replace") — an excluded
        # note is honest; a mojibake-indexed one is not.
        warnings.warn(f"skipping unreadable note {path}: {exc}", stacklevel=2)
        return None
    return note_from_text(text, path, vault)


def note_from_text(text: str, path: Path, vault: Path) -> Note | None:
    """Build a :class:`Note` from bytes ALREADY IN HAND (INT-01).

    ``load_note`` re-opens the file to read it. A caller that has verified a
    byte buffer (the COS approved queue hashes its payload against a
    host-signed anchor) must never re-open the path afterwards — that re-opens
    the substitution window it just closed — so it parses the verified buffer
    through here instead."""
    meta, body = frontmatter.parse_text(text)
    if not meta:
        return None
    zone = _zone_of(path, vault)
    nid = str(meta.get("id") or path.stem)
    return Note(
        content_hash=sha256_text(text),
        id=nid,
        title=str(meta.get("title") or nid),
        type=str(meta.get("type") or ("source" if zone == "raw" else "note")),
        classification=str(meta.get("classification") or ""),
        zone=zone,
        path=path,
        body=body,
        meta=meta,
        created=str(meta.get("created") or meta.get("captured") or ""),
        updated=str(meta.get("updated") or meta.get("captured") or ""),
        sha256=str(meta.get("sha256") or sha256_text(body)),
        document_date=str(meta.get("document_date") or ""),
        effective_date=str(meta.get("effective_date") or ""),
        superseded_date=str(meta.get("superseded_date") or ""),
        is_latest_version=_bitemporal_bool(meta.get("is_latest_version")),
        superseded_by=_bitemporal_link(meta.get("superseded_by")),
        previous_version=(_bitemporal_link(meta.get("previous_version"))
                          or _bitemporal_link(meta.get("replaces"))),
    )



# INT-03: top-level dirs holding MACHINE OUTPUT — run reports, review-gate
# drafting workspaces, decision cards a fold wrote for itself. They are
# operational artifacts, not knowledge: nothing validates them (tools/
# validate.py never looks at them), they carry no `classification:`, and
# indexing them put 78 successive revisions of one in-flight draft into
# retrieval alongside the curated note that supersedes them. A cos-ops
# artifact that turns out to BE knowledge gets promoted into brain/ or raw/
# through the normal audited write path — that promotion, not a second
# indexing scope, is how it becomes retrievable.
MACHINE_OUTPUT_DIRS = ("cos-ops",)


def scan_vault(vault: Path, *, stats: dict | None = None) -> Iterator[Note]:
    """Yield every note under vault/, skipping the .brain/ runtime cache, the
    top-level inbox/ drop zone, the overlay/ personalization layer, machine
    output dirs (``MACHINE_OUTPUT_DIRS``), archived ingestion originals under
    raw/originals/, and the generated backlinks.md.

    Pass ``stats`` (a dict) to receive the exclusion counts — ``sync``/
    ``rebuild`` report them so a whole excluded tree is never silent."""
    excluded_machine = 0
    for p in sorted(vault.rglob("*.md")):
        sp = p.as_posix()
        if "/.brain/" in sp:
            continue
        try:
            rel_parts = p.relative_to(vault).parts
        except ValueError:
            rel_parts = ()
        # ADR-0003 Ruling 1: the ingestion drop zone is a visible top-level dir
        # (not hidden like .brain/), but is never indexed — only the
        # extracted raw/ source a handler promotes is a real note.
        # C4: anchored to the vault-relative TOP-LEVEL path segment only — a
        # prior unanchored "/inbox/" substring match wrongly excluded any note
        # under a directory named "inbox" at ANY depth (e.g.
        # brain/resources/inbox/reading-list.md), silently dropping it from
        # the index.
        if rel_parts and rel_parts[0] == "inbox":
            continue
        # The overlay is the owner's PERSONALIZATION layer (voice/brand/
        # keywords/people/cos settings), read from the filesystem by the
        # drafting skills -- configuration, not knowledge. It carries
        # `overlay_type:`, never the note schema's `classification:`, so
        # indexing it both polluted retrieval with settings files and ranked
        # every one of them as MNPI (unlabelled => most restrictive). A fresh
        # `brain init` vault indexed 8 notes of which 4 were overlay config
        # (reported by a Windows pilot user, 2026-07-29). Same posture as
        # .brain/: present on disk, never a search hit.
        if rel_parts and rel_parts[0] == "overlay":
            continue
        # INT-03 — machine output (see MACHINE_OUTPUT_DIRS). Counted, not
        # silently dropped: an excluded tree that nobody reports is how 146
        # unvalidated files ended up in a retrieval index unnoticed.
        if rel_parts and rel_parts[0] in MACHINE_OUTPUT_DIRS:
            excluded_machine += 1
            continue
        # C5: raw/originals/ holds the archived, immutable ORIGINAL file a
        # handler claimed during ingestion (e.g. a promoted .md's own source
        # copy) — it is evidence, never a real note, and must not be
        # double-indexed alongside the promoted raw/<slug>.md note.
        if rel_parts[:2] == ("raw", "originals"):
            continue
        if p.name == "backlinks.md":
            continue
        note = load_note(p, vault)
        if note is not None:
            yield note
    if stats is not None:
        stats["excluded_machine_output"] = excluded_machine
