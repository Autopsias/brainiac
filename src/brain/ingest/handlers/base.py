"""Handler contract (ING-01/ING-02, ADR-0003 Ruling 1).

Every format handler implements one function contract: given a Path, return an
``ExtractResult``. Handlers never touch the vault, the index, or the audit
chain — that is the orchestrator's job (``brain.ingest.run_ingest``). This
keeps a handler pure and trivially testable: bytes in, Markdown (or a
quarantine reason) out.
"""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Below this many non-whitespace characters, a "successfully extracted"
# document is treated as empty/near-empty content — the OHRBench finding that
# upstream extraction failure (not the write path) is the dominant corpus-
# corruption vector. Quarantine, never sign a near-empty source.
MIN_CONTENT_CHARS = 40


@dataclass
class ExtractResult:
    """Outcome of one handler's extraction attempt.

    ``quarantine_reason`` is ``None`` on success. ``markdown``/``warnings``/
    ``metadata`` are always populated (possibly empty) so callers never branch
    on attribute presence.
    """

    markdown: str = ""
    warnings: list[str] = field(default_factory=list)
    quarantine_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.quarantine_reason is None

    @staticmethod
    def quarantine(reason: str, *, warnings: list[str] | None = None) -> "ExtractResult":
        return ExtractResult(quarantine_reason=reason, warnings=list(warnings or []))


#: Uncompressed ceiling for an OOXML container (.docx / .pptx). A Word or
#: PowerPoint file is a zip, and its compressed size says nothing about what
#: extraction has to hold in memory: measured 2026-09-02 on this checkout, a
#: 110 KB .docx expanded to 30.5 MB (278:1) and the handler produced 29.7 MB of
#: Markdown at a 68 MB RSS delta — the existing on-disk caps (100 MB docx /
#: 150 MB pptx) never saw it.
#:
#: 256 MB is STATED, not derived. There is no "largest legitimate deck" to
#: multiply here: `vault/raw/originals` does not exist in this checkout and
#: `vault/raw` holds zero .docx/.pptx files (measured 2026-09-01). It is a
#: ceiling well clear of any real document and well under what an extraction
#: bomb needs to hurt. If a genuine deck is ever refused by it, RAISE it
#: deliberately — never widen it to make one file pass.
MAX_OOXML_UNCOMPRESSED_BYTES = 256 * 1024 * 1024

#: The quarantine reason ``ooxml_expansion_gate`` raises. Distinct from the
#: plain-zip handler's ``zip_bomb_suspected`` on purpose: that one is backed
#: by a streamed real-byte count as well as a declared one, this is a
#: declared-size ceiling. Same family of threat, different strength of
#: evidence, so it says so rather than borrowing the stronger word. Like
#: ``zip_bomb_suspected`` it carries NO entry in
#: ``maintenance_retention._QUARANTINE_REMEDY``: a security refusal is an
#: owner judgement, never a mechanical auto-retry.
OOXML_EXPANSION_REASON = "ooxml_expansion_suspected"

#: Member-COUNT ceiling (LOW-02). Reuses ``zip.py``'s ``MAX_MEMBERS`` shape —
#: refuse before the caller's own reader (``docx.Document()`` /
#: ``Presentation()``) walks the parts — but scoped higher: a real .docx/.pptx
#: legitimately declares one part per embedded object — an image, a
#: diagram, a font — which a plain zip upload never does.
#: Measured 2026-09-02
#: (`_evidence/security-followup/s04-office-expansion.txt`, orchestrator-
#: reproduced): 100,000 ZERO-BYTE members in a 10,577,878-byte archive cost
#: 51.5 MB retained / 59.2 MB peak — the SAME peak the pre-s04
#: ``docx.Document()`` path already paid for that member count, so this is not
#: a new cost, only a bound on how large that count is allowed to grow.
MAX_OOXML_MEMBERS = 5_000

#: Its own reason word, same convention as ``OOXML_EXPANSION_REASON`` above —
#: never ``zip_too_many_members``, so a quarantine report never asserts the
#: plain-zip handler's streamed check ran here.
OOXML_TOO_MANY_MEMBERS_REASON = "ooxml_too_many_members"


def _declared_member_count(path) -> int | None:
    """Entry count from the zip's own end-of-central-directory record.

    WHY THIS EXISTS AT ALL, measured 2026-09-04: the member cap used to be
    checked AFTER ``zipfile.ZipFile(path)``, and that is too late. ZipFile
    reads the whole central directory in its constructor, so 100,001 zero-byte
    members cost their full 59.2 MB peak BEFORE ``len(infolist())`` could be
    asked — identical to the unbounded case. The cap bounded the downstream
    reader and nothing else. Reading the count from the EOCD record costs one
    64 KB tail read.

    Returns ``None`` when the count cannot be determined (no EOCD found, a
    short file, an OS error). The caller then falls back to the
    post-construction check, which is still correct — just not cheap. Never
    raises.
    """
    import struct

    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            # The EOCD is last, but a trailing comment may follow it, and the
            # comment length field is 16 bits — so 64 KB plus the record itself
            # is the whole search space, never more.
            tail_len = min(size, 65536 + 22)
            fh.seek(size - tail_len)
            tail = fh.read(tail_len)
        at = tail.rfind(b"PK\x05\x06")
        if at < 0 or len(tail) - at < 22:
            return None
        total = struct.unpack_from("<H", tail, at + 10)[0]
        if total != 0xFFFF:
            return total
        # ZIP64: the 16-bit field is saturated and the real count lives in the
        # zip64 EOCD record, which precedes this one.
        at64 = tail.rfind(b"PK\x06\x06", 0, at)
        if at64 < 0 or len(tail) - at64 < 40:
            return None
        return struct.unpack_from("<Q", tail, at64 + 32)[0]
    except (OSError, struct.error):
        return None


def ooxml_expansion_gate(
    path: Path, *, cap: int | None = None, member_cap: int | None = None,
) -> "ExtractResult | None":
    """Refuse an OOXML file whose members DECLARE more than ``cap`` bytes, or
    whose member COUNT exceeds ``member_cap``.

    Reads the central directory only — ``ZipInfo.file_size`` — and stops at the
    FIRST member that carries the running sum past the cap, so a bomb is
    refused without decompressing any of it. Returns ``None`` when the file is
    within bounds, or is not a readable zip at all: in that case the handler's
    own open path produces its own error, which is a better message than
    anything this gate could invent.

    **A DECLARED-SIZE CEILING, deliberately, and here is why it is a bound and
    not a hope.** A central directory is attacker-written metadata, so the
    obvious objection is a file whose ``file_size`` fields UNDERSTATE the
    payload. Measured on this checkout, CPython 3.13 (2026-09-02, recorded in
    `_evidence/security-followup/s04-office-expansion.txt`): ``ZipExtFile``
    carries ``_left = zinfo.file_size`` from the CENTRAL directory and stops
    decompressing there, so an understated member yields at most its declared
    byte count and then raises ``BadZipFile: Bad CRC-32``. Both readers behind
    this gate go through ``zipfile`` (``docx.opc.phys_pkg.ZipFileSystem``,
    ``pptx.opc.serialized.ZipPkgReader``), so declared size bounds real output
    for them. ZIP64 does not open a hole either — ``file_size`` is already the
    64-bit value once ``infolist()`` has parsed the extra field.
    ``tests/test_ingest_bounds_and_empty_chain.py`` pins that behaviour with a
    hand-patched mismatched ``file_size``; if a future Python or a reader that
    bypasses ``zipfile`` breaks it, that test fails and this ceiling has to be
    re-derived as a streamed one.

    **Its own reason word, NOT the plain-zip handler's.** ``zip.py`` refuses on
    declared size AND counts real output bytes as they arrive
    (``_read_member_bounded``); this gate does the first only. The outcomes
    happen to coincide today for the reason above, but the two are not the same
    check, and sharing ``zip_bomb_suspected`` would have the quarantine report
    assert a streamed defence this path does not perform.

    **Member COUNT is now bounded too (LOW-02), and the total-bytes ceiling
    above does NOT already cover it — corrected 2026-09-04, the prior wording
    here claimed it did.** A member that declares ``file_size == 0`` adds
    nothing to the running total regardless of how many of them a file
    carries, so 100,000 zero-byte members sail past ``cap`` at a running total
    of 0 while still costing real memory to enumerate (measured
    2026-09-02, `_evidence/security-followup/s04-office-expansion.txt`:
    51.5 MB retained / 59.2 MB peak on CPython 3.13.12 for a 10,577,878-byte
    archive — not a regression, since the pre-s04 ``docx.Document()`` path
    already paid the identical peak for that member count). ``member_cap``
    (default :data:`MAX_OOXML_MEMBERS`) refuses such a file — and refuses it
    from the archive's own end-of-central-directory count, BEFORE
    ``zipfile.ZipFile`` builds one object per member. That ordering IS the
    fix: checked after construction the cap still refused, but the peak stayed
    at the same 59.2 MB, because the constructor had already paid it (measured
    2026-09-04). Read first, the same file peaks under 1 MB. Per-member size stays unbounded here on purpose —
    the total above already caps what THAT would buy, since every member's
    ``file_size`` feeds the same running sum.
    """
    import zipfile

    # Resolved at CALL time, not bound as a default: a default argument would
    # freeze the module constant at import and leave the handler wiring
    # untestable without writing a 256 MB fixture.
    if cap is None:
        cap = MAX_OOXML_UNCOMPRESSED_BYTES
    if member_cap is None:
        member_cap = MAX_OOXML_MEMBERS
    # CHEAP FIRST, and it has to be first: see `_declared_member_count`. The
    # post-construction check below stays as the backstop for an archive whose
    # EOCD cannot be read.
    declared = _declared_member_count(path)
    if declared is not None and declared > member_cap:
        return ExtractResult.quarantine(
            OOXML_TOO_MANY_MEMBERS_REASON,
            warnings=[f"{declared} members exceeds cap {member_cap}"],
        )
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > member_cap:
                return ExtractResult.quarantine(
                    OOXML_TOO_MANY_MEMBERS_REASON,
                    warnings=[
                        f"{len(infos)} members exceeds cap {member_cap}"
                    ],
                )
            total = 0
            for info in infos:
                total += info.file_size
                if total > cap:
                    return ExtractResult.quarantine(
                        OOXML_EXPANSION_REASON,
                        warnings=[
                            f"declared uncompressed total exceeds {cap} bytes at "
                            f"member {info.filename!r} (running total {total}; "
                            f"file is {path.stat().st_size} bytes on disk)"
                        ],
                    )
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def density_gate(markdown: str, *, min_chars: int = MIN_CONTENT_CHARS) -> str | None:
    """Generic extraction-quality gate (HARDENED:grill): empty-text / low text
    density detector shared by every handler. Strips table-unparsed fences and
    page markers before counting so a document that is ENTIRELY scanned pages
    or an unparsed table dump does not slip past on marker text alone.
    Returns a quarantine reason, or ``None`` if the content passes."""
    import re

    stripped = re.sub(r"^#{1,3}\s.*$", "", markdown, flags=re.MULTILINE)
    stripped = stripped.replace("```", "")
    content_chars = len(stripped.strip())
    if content_chars < min_chars:
        return "empty_or_low_text_density"
    return None


try:  # LOCAL-only OCR binding. ADR-0003 Ruling 1(g): never a pip dependency,
    import pytesseract  # never a cloud fallback — absent it, OCR simply degrades.
    _HAS_PYTESSERACT = True
except ImportError:  # pragma: no cover - exercised via degraded-deps test
    _HAS_PYTESSERACT = False

_OCR_LANG: str | None = None
_OCR_LANG_PROBED = False


def ocr_available() -> bool:
    """True iff the local OCR engine (binding + tesseract binary) answers."""
    return _HAS_PYTESSERACT and ocr_lang() is not None


def ocr_lang() -> str | None:
    """The traineddata languages to OCR with, or ``None`` when the local
    tesseract binary is absent/unusable. ``$BRAIN_OCR_LANG`` overrides; the
    default picks whichever of eng/por this host actually installed, so a
    Portuguese contract is not read through an English-only model. Probed
    once per process — the answer cannot change under a running drain."""
    global _OCR_LANG, _OCR_LANG_PROBED
    if _OCR_LANG_PROBED:
        return _OCR_LANG
    import os

    _OCR_LANG_PROBED = True
    env = os.environ.get("BRAIN_OCR_LANG")
    if env:
        _OCR_LANG = env
    elif not _HAS_PYTESSERACT:
        _OCR_LANG = None
    else:
        try:
            installed = set(pytesseract.get_languages(config=""))
        except Exception:  # no tesseract binary, or it refused to answer
            installed = set()
        _OCR_LANG = "+".join(lang for lang in ("eng", "por") if lang in installed) or None
    return _OCR_LANG


#: What an image handler writes when OCR returned nothing. An image with no
#: readable text is still INGESTED (its dimensions and format are a real
#: record, and quarantining a photo helps nobody) — but the resulting note is
#: a FAILED EXTRACTION, not a document, and the ingest pipeline reads this
#: marker to know that re-offering the same bytes is a RETRY rather than a
#: duplicate. Kept here, next to `ocr_image`, so the writer and the reader can
#: never drift apart.
NO_TEXT_MARKER = "[no text detected]"


#: Seconds one OCR call may run before tesseract is killed. Untrusted bytes
#: reach this: a page-sized scan is seconds, but an attacker-shaped or simply
#: pathological image can hold the process for as long as it likes, and this
#: runs inside the hourly `brain-nightly` under the single-writer lock — so a
#: hang here is not one slow file, it is every vault on the host waiting.
#: `pytesseract` kills the child and raises on expiry, which the handler below
#: already degrades to a warning. Raise it with `$BRAIN_OCR_TIMEOUT_SECONDS`
#: for a genuinely huge scan; 0 restores the old unbounded behaviour.
OCR_TIMEOUT_SECONDS = 120


def ocr_timeout() -> int:
    raw = os.environ.get("BRAIN_OCR_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return OCR_TIMEOUT_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return OCR_TIMEOUT_SECONDS


def ocr_image(img: Any) -> tuple[str, list[str]]:
    """LOCAL-only OCR of one PIL image. Never raises: a missing binding, a
    missing tesseract binary, a timeout, or any engine failure all degrade to
    empty text plus a warning — there is no cloud fallback to reach for, so a
    failure here is reported, never fatal to the ingest."""
    if not _HAS_PYTESSERACT:
        return "", ["ocr_unavailable: pytesseract not installed"]
    lang = ocr_lang()
    if lang is None:
        return "", ["ocr_unavailable: no local tesseract binary / traineddata"]
    try:
        text = pytesseract.image_to_string(
            img, lang=lang, timeout=ocr_timeout())
        return text.strip(), []
    except Exception as exc:
        return "", [f"ocr_unavailable: {type(exc).__name__}: {exc}"]


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def strip_control_chars(name: str) -> str:
    """Strip control chars (incl. embedded newlines) from an untrusted name
    (zip member, email attachment/header, HTML title, ...) before it flows
    into generated Markdown body text or a report entry (S06 HARDENED — the
    S05 lesson was frontmatter; a control char in body text can still forge a
    fake heading/table row in the rendered note or a report line)."""
    if not name:
        return name
    return _CONTROL_CHARS.sub("", name)


class Handler(ABC):
    """One handler per file extension family."""

    #: lower-cased extensions this handler claims, e.g. (".pdf",)
    extensions: tuple[str, ...] = ()
    #: human label for capability-probe reporting, e.g. "pypdf"
    dependency_name: str = ""

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """True iff this handler's extraction dependency import-succeeds."""

    @classmethod
    @abstractmethod
    def extract(cls, path: Path) -> ExtractResult:
        """Extract ``path`` to Markdown. MUST NOT raise on malformed input —
        catch and return a quarantine ``ExtractResult`` instead (a crash here
        would abort the whole drain, not just this one file)."""
