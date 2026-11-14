"""Note model + vault scanning. Markdown files are the single source of truth."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import unicodedata
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import frontmatter
from .provenance import index_frontmatter_json


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: "Path | str") -> str:
    """The hash a note's chain entry is compared against — over its RAW BYTES.

    `write_note` signs `sha256_text(content)` and writes exactly those bytes,
    so this is the same digest read back honestly. It must never go through
    `read_text()`: text mode deletes every `\r` on the way in, which made a
    CR-only edit after signing invisible and a legitimately CRLF note report
    drift forever (M-7, 2026-09-02). One definition, because every caller that
    compares a file to `content_sha256` has to use the same one."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
    #: sha256 of the RAW BYTES of the same read (M-7). ``content_hash`` is
    #: over the DECODED text, and text mode deletes every ``\r``, so it can
    #: never match the audit chain's ``content_sha256`` on a CRLF note.
    #: Empty when the note was not loaded from a file — a caller that needs
    #: proof must treat empty as "cannot prove", never as a match.
    raw_hash: str = ""
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
            # PV-01: the parsed frontmatter, bounded, so a result row can carry
            # the email header line without re-reading the file (the Cowork VM
            # reads only the published index snapshot).
            "frontmatter": index_frontmatter_json(self.meta),
        }


def _zone_of(path: Path, vault: Path) -> str:
    try:
        rel = path.relative_to(vault)
    except ValueError:
        return "brain"
    return rel.parts[0] if rel.parts else "brain"


class _UnsafeNote(Exception):
    """The file is not a regular file, or its real location is outside the
    vault or inside an excluded tree. Distinct from "no frontmatter"."""


class VaultRootChanged(OSError):
    """The vault root was replaced mid-scan. NOT an _UnsafeNote: a short scan
    must abort, or sync would delete every index row the scan never listed."""


def _fd_real_path(fd: int, fallback: str) -> str:
    """The real path of an OPEN descriptor, so a swapped parent directory is
    seen. Windows has neither call: ``realpath(fallback)`` is advisory there."""
    if sys.platform == "darwin":
        import fcntl

        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        return raw.split(b"\0", 1)[0].decode("utf-8")
    if sys.platform.startswith("linux"):
        return os.readlink(f"/proc/self/fd/{fd}")
    return os.path.realpath(fallback)


def _open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


def _vault_root_id(vault: Path) -> tuple[str, int, int]:
    """``(real path, st_dev, st_ino)`` of the vault root from ONE open. Resolve
    it once per scan: a root swapped mid-scan must not become the trusted root.
    Taken from the descriptor because macOS returns the on-disk letter case,
    which `realpath` does not. Raises loudly when the call fails, so a broken
    platform call stops the scan instead of skipping every note."""
    if sys.platform == "win32":
        # Windows refuses os.open on a directory; stat + realpath is all it has.
        st = os.stat(vault)
        return os.path.realpath(vault), st.st_dev, st.st_ino
    dfd = os.open(vault, os.O_RDONLY)
    try:
        st = os.fstat(dfd)
        return _fd_real_path(dfd, str(vault)), st.st_dev, st.st_ino
    finally:
        os.close(dfd)


def _read_note_bytes(path: Path, vault: Path, root: tuple[str, int, int] | None = None) -> bytes:
    """Read a note's bytes from ONE descriptor, refusing anything that is not a
    regular file whose real path sits inside the vault, outside the excluded
    trees. A symlink whose target stays inside keeps working. Pass ``root`` (from
    ``_vault_root_id``) to reuse one root for a whole scan."""
    root_path, root_dev, root_ino = root or _vault_root_id(vault)
    fd = os.open(path, _open_flags())
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _UnsafeNote("not a regular file")
        now = os.stat(vault)
        if (now.st_dev, now.st_ino) != (root_dev, root_ino):
            raise VaultRootChanged("the vault root changed during the scan")
        try:
            real = _fd_real_path(fd, str(path))
        except OSError as exc:  # unknown location: refuse, never "unreadable"
            raise _UnsafeNote(f"cannot resolve the real path: {exc}") from exc
        if os.path.commonpath([real, root_path]) != root_path or real == root_path:
            raise _UnsafeNote(f"real path {real} is outside the vault")
        if _scan_exclusion(Path(real), Path(root_path)) is not None:
            raise _UnsafeNote(f"real path {real} is in an excluded tree")
        with os.fdopen(os.dup(fd), "rb") as fh:
            return fh.read()
    except ValueError as exc:  # commonpath across drives (Windows)
        raise _UnsafeNote(str(exc)) from exc
    finally:
        os.close(fd)


def _load(path: Path, vault: Path, root: tuple[str, int, int] | None = None) -> tuple[Note | None, bool]:
    """``(note, unsafe)``: ``unsafe`` is True only for a refused file."""
    try:
        # ONE read. `raw_hash` is derived from these exact bytes rather than
        # re-opening the path later: re-opening to authenticate bytes already
        # parsed is the substitution window `note_from_text` below exists to
        # close, and the downgrade guard used to do exactly that (adversarial
        # review round 5, 2026-09-04).
        raw = _read_note_bytes(path, vault, root)
        # `str.splitlines`-style translation, so `content_hash` keeps the exact
        # convention `read_text` gave it — the index's change detection,
        # fingerprint and graph manifest all key on that value.
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except VaultRootChanged:
        raise
    except _UnsafeNote as exc:
        warnings.warn(f"refusing unsafe note {path}: {exc}", stacklevel=3)
        return None, True
    except (UnicodeDecodeError, OSError) as exc:
        # H-3: a single bad-encoding (or unreadable) file must not abort a
        # whole-vault rebuild/sync. Skip it with a warning rather than
        # silently mangling its content (no errors="replace") — an excluded
        # note is honest; a mojibake-indexed one is not.
        warnings.warn(f"skipping unreadable note {path}: {exc}", stacklevel=3)
        return None, False
    note = note_from_text(text, path, vault)
    if note is not None:
        note.raw_hash = hashlib.sha256(raw).hexdigest()
    return note, False


def load_note(path: Path, vault: Path) -> Note | None:
    """Load one note. A file whose real path is outside the vault or inside an
    excluded tree (.brain/, inbox/, overlay/, cos-ops/, raw/originals/) is
    refused with a warning and returns None."""
    return _load(path, vault)[0]


def read_note_text_safe(path: Path, vault: Path, root: tuple[str, int, int] | None = None) -> str | None:
    """The newline-normalised text of a contained regular file, or None when it
    is refused or unreadable. For callers that re-read a note after ``scan_vault``."""
    try:
        return _read_note_bytes(path, vault, root).decode("utf-8").replace(
            "\r\n", "\n").replace("\r", "\n")
    except VaultRootChanged:
        raise
    except (_UnsafeNote, UnicodeDecodeError, OSError):
        return None


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


def _scan_exclusion(p: Path, vault: Path) -> str | None:
    """``"machine"`` for a cos-ops file (counted), ``"skip"`` for any other
    excluded path, ``None`` for a path that is a candidate note."""
    if "/.brain/" in p.as_posix():
        return "skip"
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
    # The overlay is the owner's PERSONALIZATION layer (voice/brand/
    # keywords/people/cos settings), read from the filesystem by the
    # drafting skills -- configuration, not knowledge. It carries
    # `overlay_type:`, never the note schema's `classification:`, so
    # indexing it both polluted retrieval with settings files and ranked
    # every one of them as MNPI (unlabelled => most restrictive). A fresh
    # `brain init` vault indexed 8 notes of which 4 were overlay config
    # (reported by a Windows pilot user, 2026-07-29). Same posture as
    # .brain/: present on disk, never a search hit.
    if rel_parts and rel_parts[0] in ("inbox", "overlay"):
        return "skip"
    # INT-03 — machine output (see MACHINE_OUTPUT_DIRS). Counted, not
    # silently dropped: an excluded tree that nobody reports is how 146
    # unvalidated files ended up in a retrieval index unnoticed.
    if rel_parts and rel_parts[0] in MACHINE_OUTPUT_DIRS:
        return "machine"
    # C5: raw/originals/ holds the archived, immutable ORIGINAL file a
    # handler claimed during ingestion (e.g. a promoted .md's own source
    # copy) — it is evidence, never a real note, and must not be
    # double-indexed alongside the promoted raw/<slug>.md note.
    if rel_parts[:2] == ("raw", "originals") or p.name == "backlinks.md":
        return "skip"
    return None


def scan_vault(vault: Path, *, stats: dict | None = None, root: tuple[str, int, int] | None = None) -> Iterator[Note]:
    """Yield every note under vault/, skipping the .brain/ runtime cache, the
    top-level inbox/ drop zone, the overlay/ personalization layer, machine
    output dirs (``MACHINE_OUTPUT_DIRS``), archived ingestion originals under
    raw/originals/, and the generated backlinks.md.

    Pass ``stats`` (a dict) to receive the exclusion counts — ``sync``/
    ``rebuild`` report them so a whole excluded tree is never silent."""
    excluded_machine = excluded_unsafe = 0
    # Probe + pin once; raises if the platform call fails. A vault folder that does
    # not exist yet scans as empty (rglob yields nothing), as it always did.
    if root is None and vault.is_dir():
        root = _vault_root_id(vault)
    for p in sorted(vault.rglob("*.md")):
        verdict = _scan_exclusion(p, vault)
        if verdict == "machine":
            excluded_machine += 1
        if verdict:
            continue
        note, unsafe = _load(p, vault, root)
        excluded_unsafe += unsafe
        if note is not None:
            yield note
    if stats is not None:
        stats["excluded_machine_output"] = excluded_machine
        stats["excluded_unsafe_note"] = excluded_unsafe
