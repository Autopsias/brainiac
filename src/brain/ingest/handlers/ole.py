"""Encrypted-OOXML detection over an OLE compound file (2026-09-27).

Split out of ``base.py`` when the DIFAT-chain walk took that file past the
500-line limit. Stdlib only, never raises, and bounded in every read: see
:func:`is_encrypted_office`.
"""
from __future__ import annotations

import struct
from pathlib import Path

#: The only names that can carry an encrypted OOXML package these handlers
#: would otherwise read. A legacy `.doc`/`.ppt`/`.xls` is OLE by nature and
#: is never judged here (review 2026-09-27).
OOXML_SUFFIXES = (".docx", ".xlsx", ".xlsm", ".pptx")

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")
_OLE_STREAM = 2
_OLE_LAST = 0xFFFFFFFA        # sector ids at or above this are markers
_OLE_MAX_DIR_SECTORS = 64     # an encrypted package's directory has ~10 entries
_OLE_MAX_DIFAT_SECTORS = 64   # 109 + 64*127 FAT sectors: >500 MB, past every cap


def _ole_difat(fh, hdr: bytes, size: int) -> list[int]:
    """Every FAT sector id: the header's 109, then the DIFAT CHAIN.

    THE CHAIN WAS MISSING (review 2026-09-27): past 109 FAT sectors (~7 MB at
    512-byte sectors, e.g. an Office transacted save) the directory can sit
    where only a DIFAT sector names its FAT, and the walk read such a file as
    not encrypted. Bounded by the header's own count AND
    ``_OLE_MAX_DIFAT_SECTORS``; a chain that revisits a sector or overruns
    either bound raises ``ValueError`` — a malformed file, read as "no"."""
    per = size // 4
    difat = list(struct.unpack_from("<109I", hdr, 0x4C))
    nxt, count = struct.unpack_from("<II", hdr, 0x44)
    seen: set[int] = set()
    while nxt < _OLE_LAST:
        if nxt in seen or len(seen) >= min(count, _OLE_MAX_DIFAT_SECTORS):
            raise ValueError("cyclic or oversized DIFAT chain")
        seen.add(nxt)
        fh.seek((nxt + 1) * size)
        ids = struct.unpack(f"<{per}I", fh.read(size))
        difat += ids[:-1]
        nxt = ids[-1]
    return difat


def _ole_stream_names(fh) -> set[bytes]:
    """The STREAM names in an OLE directory, walked through the FAT.

    Bounded: ``_OLE_MAX_DIR_SECTORS`` directory sectors, and the DIFAT bounds
    of :func:`_ole_difat`. A truncated or cyclic file raises; callers read
    "no"."""
    hdr = fh.read(512)
    shift = struct.unpack_from("<H", hdr, 0x1E)[0]
    if shift not in (9, 12):
        return set()
    size, names = 1 << shift, set()
    per = size // 4
    difat = _ole_difat(fh, hdr, size)
    sect = struct.unpack_from("<I", hdr, 0x30)[0]
    for _ in range(_OLE_MAX_DIR_SECTORS):
        if sect >= _OLE_LAST or sect // per >= len(difat):
            break
        fh.seek((sect + 1) * size)
        block = fh.read(size)
        for off in range(0, len(block) - 127, 128):
            length = struct.unpack_from("<H", block, off + 64)[0]
            if block[off + 66] == _OLE_STREAM and 2 <= length <= 64:
                names.add(block[off:off + length - 2])
        fat = difat[sect // per]
        if fat >= _OLE_LAST:
            break
        fh.seek((fat + 1) * size + (sect % per) * 4)
        sect = struct.unpack("<I", fh.read(4))[0]
    return names


def is_encrypted_office(path) -> bool:
    """True when ``path`` is an ENCRYPTED OOXML package, never raises.

    WHY THIS EXISTS, measured 2026-09-27: a rights-protected CSIRT deck named
    `.pptx` is not a zip at all. Office wraps an encrypted .docx/.xlsx/.pptx
    in an OLE compound file (magic ``D0 CF 11 E0``) whose ``EncryptedPackage``
    stream holds the real package, so python-pptx answered
    ``PackageNotFoundError`` and the file quarantined as a generic
    ``pptx_extraction_error``. A mail lane then re-fetched it on nine runs.

    Three conditions, all required: an OOXML name, the OLE magic, and a STREAM
    ENTRY named ``EncryptedPackage`` in the OLE DIRECTORY — read through the
    FAT, so the same words in a document's body text never count (review
    2026-09-27). NOT ``EncryptionInfo`` too: that stream is the PASSWORD shape,
    and the real deck (IRM, ``DRMEncryptedDataSpace``) has none — requiring
    both misses the file this was written for. The magic alone is not enough
    either: a legacy binary ``.ppt`` renamed ``.pptx`` is OLE and unencrypted,
    and it keeps its old extraction-error reason.

    Stated limit: a file under Office's built-in default password opens
    without a prompt yet reads encrypted here (decrypting needs a library).
    """
    if Path(path).suffix.lower() not in OOXML_SUFFIXES:
        return False
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != _OLE_MAGIC:
                return False
            fh.seek(0)
            return _ENCRYPTED_PACKAGE in _ole_stream_names(fh)
    except (OSError, struct.error, IndexError, ValueError):
        return False


__all__ = ["OOXML_SUFFIXES", "is_encrypted_office"]
