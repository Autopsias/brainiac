"""Note model + vault scanning. Markdown files are the single source of truth."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import frontmatter


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        }


def _zone_of(path: Path, vault: Path) -> str:
    try:
        rel = path.relative_to(vault)
    except ValueError:
        return "brain"
    return rel.parts[0] if rel.parts else "brain"


def load_note(path: Path, vault: Path) -> Note | None:
    text = path.read_text(encoding="utf-8")
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
    )


def scan_vault(vault: Path) -> Iterator[Note]:
    """Yield every note under vault/, skipping the .brain/ runtime cache and the
    generated backlinks.md."""
    for p in sorted(vault.rglob("*.md")):
        sp = p.as_posix()
        if "/.brain/" in sp:
            continue
        if p.name == "backlinks.md":
            continue
        note = load_note(p, vault)
        if note is not None:
            yield note
