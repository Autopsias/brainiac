"""Read one bzip2, lzma or zstd zip member with a hard limit on its output (F40).

``zipfile`` gives zlib an output limit and nobody else, so ``ZipExtFile.read(n)``
does not bound a bzip2, lzma or zstd member whose header lies about its size.
Measured on 3.14.6: a 602-byte bzip2 bundle that expands to 400 MiB peaked at
821 MiB RSS through ``zf.open(info).read(cap + 1)`` (deflate: 26 MiB). Here the
decompressor itself is told how many bytes to produce.
"""
from __future__ import annotations

import importlib
import struct
import zipfile
import zlib
from typing import Optional


def read_member_bounded(raw: bytes, info: zipfile.ZipInfo) -> Optional[bytes]:
    """The member's bytes, from the zip held in ``raw``, or None when the CRC-32 of what
    was decoded (at most ``info.file_size`` bytes) does not match (zipfile's "Bad CRC-32"),
    or the stream is corrupt, truncated, an unknown method or a codec this Python lacks.
    At most ``info.file_size`` bytes are produced whatever the stream holds; a declared
    size above the real one is accepted when the CRC matches, as zipfile does. The caller
    has judged ``file_size`` and had zipfile open the member (local header, encryption)."""
    try:
        start = info.header_offset
        name_len, extra_len = struct.unpack("<HH", raw[start + 26:start + 30])  # the local header's own lengths
        data_at = start + 30 + name_len + extra_len
        comp, limit = raw[data_at:data_at + info.compress_size], info.file_size
        if info.compress_type == zipfile.ZIP_BZIP2:
            import bz2
            data = bz2.BZ2Decompressor().decompress(comp, limit)
        elif info.compress_type == zipfile.ZIP_LZMA:
            import lzma
            # zipfile's framing: 2 version bytes, a 2-byte props size, the props, a raw LZMA1 stream.
            props_len = struct.unpack("<H", comp[2:4])[0]
            filt = getattr(lzma, "_decode_filter_properties")(lzma.FILTER_LZMA1, comp[4:4 + props_len])
            data = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filt]).decompress(comp[4 + props_len:], limit)
        elif info.compress_type == getattr(zipfile, "ZIP_ZSTANDARD", None):
            zstd = importlib.import_module("compression.zstd")  # 3.14+ only: by name, so ty on 3.12 sees no import
            data = zstd.ZstdDecompressor().decompress(comp, limit)
        else:
            return None
        return data if zlib.crc32(data) == info.CRC else None
    except Exception:  # OSError (bz2), LZMAError, ZstdError, struct.error, ImportError: all "unreadable"
        return None
