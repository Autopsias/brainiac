"""ZIP handler — bounded, Zip-Slip-hardened member expansion. No file in a
ZIP is EVER extracted to a path derived from its own name: every member's
bytes are read in-memory here and handed to the pipeline as
``metadata["nested"]`` entries (name, data); the pipeline
(``pipeline._process_nested``) is the only thing that ever writes them to
disk, and it does so under a SYNTHETIC generated name, never the archive's
own path (S06 HARDENED:codex-verify-r2).

Caps are checked from the central directory (``ZipInfo.file_size`` /
``.external_attr``) BEFORE any member is decompressed, and each member's
actual decompressed byte count is also counted DURING extraction (a
malformed/lying declared size must not buy a bigger decompression than the
cap allows) — "before/during, never after" per the S06 brief.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any

from .base import ExtractResult, Handler, density_gate, strip_control_chars

MAX_MEMBERS = 500
MAX_MEMBER_BYTES = 200 * 1024 * 1024            # per-member cap (matches pipeline.MAX_INGEST_BYTES)
MAX_TOTAL_DECLARED_BYTES = 500 * 1024 * 1024    # sum of DECLARED (pre-decompress) sizes

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_S_IFLNK = 0xA000
_S_IFREG = 0x8000


def _unsafe_zip_member_reason(info: "zipfile.ZipInfo") -> str | None:
    """Zip-Slip hardening: reject absolute paths, ``..`` traversal, a
    Windows-drive-rooted name, and symlink/hardlink/non-regular members —
    BEFORE any member is opened for reading. A zip's own path separator is
    always ``/`` per the spec; a member trying to look like a Windows
    absolute path (drive letter, or a literal backslash root) is just as
    hostile as a POSIX one."""
    name = info.filename
    if not name or not name.strip():
        return "empty_member_name"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or posixpath.isabs(normalized):
        return "absolute_path"
    if _WINDOWS_DRIVE.match(normalized):
        return "windows_drive_path"
    if any(part == ".." for part in normalized.split("/")):
        return "path_traversal"
    # external_attr's high 16 bits carry unix st_mode ONLY when the member was
    # created on a unix host (create_system == 3); on other systems (e.g. 0 =
    # FAT/Windows) those bits are meaningless and must not be interpreted.
    if info.create_system == 3:
        mode = (info.external_attr >> 16) & 0xF000
        if mode == _S_IFLNK:
            return "symlink_member"
        if mode not in (0, _S_IFREG):
            return "non_regular_member"
    return None


def _read_member_bounded(zf: "zipfile.ZipFile", info: "zipfile.ZipInfo", cap: int) -> bytes | None:
    """Stream-decompress ``info`` in chunks, counting REAL output bytes as
    they arrive. Returns ``None`` if the actual decompressed size exceeds
    ``cap`` (defends against a declared ``file_size`` that lies) instead of
    ever fully materializing an over-cap member."""
    chunks: list[bytes] = []
    total = 0
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                return None
            chunks.append(chunk)
    return b"".join(chunks)


def _open_zip(path: Path) -> zipfile.ZipFile | ExtractResult:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        return ExtractResult.quarantine(
            "zip_corrupt",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )
    except OSError as exc:
        return ExtractResult.quarantine(
            "zip_read_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )


def _validated_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo] | ExtractResult:
    infos = [info for info in zf.infolist() if not info.is_dir()]
    if not infos:
        return ExtractResult.quarantine("zip_empty")
    if len(infos) > MAX_MEMBERS:
        return ExtractResult.quarantine(
            "zip_too_many_members",
            warnings=[f"{len(infos)} members exceeds cap {MAX_MEMBERS}"],
        )
    total_declared = 0
    for info in infos:
        reason = _unsafe_zip_member_reason(info)
        if reason:
            return ExtractResult.quarantine(
                "zip_unsafe_member",
                warnings=[f"{strip_control_chars(info.filename)}: {reason}"],
            )
        if info.file_size > MAX_MEMBER_BYTES:
            return ExtractResult.quarantine(
                "zip_member_too_large",
                warnings=[
                    f"{strip_control_chars(info.filename)}: declared {info.file_size} bytes"
                ],
            )
        total_declared += info.file_size
        if total_declared > MAX_TOTAL_DECLARED_BYTES:
            return ExtractResult.quarantine(
                "zip_bomb_suspected",
                warnings=[
                    f"declared total {total_declared} bytes exceeds cap "
                    f"{MAX_TOTAL_DECLARED_BYTES}"
                ],
            )
    return infos


def _extract_members(
    zf: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
) -> tuple[list[dict[str, Any]], list[str]] | ExtractResult:
    nested: list[dict[str, Any]] = []
    listing: list[str] = []
    for info in infos:
        try:
            data = _read_member_bounded(zf, info, MAX_MEMBER_BYTES)
        except Exception as exc:
            return ExtractResult.quarantine(
                "zip_extraction_error",
                warnings=[
                    f"{strip_control_chars(info.filename)}: {type(exc).__name__}: {exc}"
                ],
            )
        if data is None:
            return ExtractResult.quarantine(
                "zip_bomb_suspected",
                warnings=[
                    f"{strip_control_chars(info.filename)}: decompressed beyond declared size"
                ],
            )
        basename = strip_control_chars(Path(info.filename).name) or "member"
        nested.append({"name": basename, "data": data})
        listing.append(f"- `{basename}` ({len(data)} bytes)")
    return nested, listing


def _archive_listing(nested: list[dict[str, Any]], listing: list[str]) -> ExtractResult:
    body = "## Archive contents\n\n" + "\n".join(listing) + "\n"
    reason = density_gate(body, min_chars=1)
    if reason:
        return ExtractResult.quarantine(reason)
    return ExtractResult(
        markdown=body,
        metadata={"nested": nested, "member_count": len(nested)},
    )


class ZipHandler(Handler):
    extensions = (".zip",)
    dependency_name = "stdlib"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        zf = _open_zip(path)
        if isinstance(zf, ExtractResult):
            return zf
        with zf:
            infos = _validated_members(zf)
            if isinstance(infos, ExtractResult):
                return infos
            extracted = _extract_members(zf, infos)
            if isinstance(extracted, ExtractResult):
                return extracted
        return _archive_listing(*extracted)
