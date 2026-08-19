"""Verify vendor wheel records."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import zipfile

from tools.vendor_semantic_deps import _dist_info_dir


def _read_record(
    zf: zipfile.ZipFile, distribution: str, record_name: str
) -> tuple[list[str], dict[str, tuple[str, str]]]:
    names = [info.filename for info in zf.infolist() if not info.is_dir()]
    if len(names) != len(set(names)):
        raise ValueError(f"vendored {distribution} wheel contains duplicate ZIP members")
    if record_name not in names:
        raise ValueError(f"vendored {distribution} wheel has no expected {record_name}")
    try:
        record_text = zf.read(record_name).decode("utf-8")
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"vendored {distribution} wheel has an invalid RECORD") from exc

    recorded: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3:
            raise ValueError(f"vendored {distribution} wheel has a malformed RECORD row")
        path, digest_field, size_field = row
        if not path or path in recorded:
            raise ValueError(f"vendored {distribution} wheel has duplicate/empty RECORD paths")
        recorded[path] = (digest_field, size_field)
    if set(recorded) != set(names):
        missing = sorted(set(names) - set(recorded))
        extra = sorted(set(recorded) - set(names))
        raise ValueError(
            f"vendored {distribution} wheel RECORD membership mismatch "
            f"(unrecorded={missing}, missing={extra})"
        )
    return names, recorded


def _verify_members(
    zf: zipfile.ZipFile,
    names: list[str],
    recorded: dict[str, tuple[str, str]],
    distribution: str,
    record_name: str,
) -> None:
    for path in names:
        digest_field, size_field = recorded[path]
        if path == record_name:
            if digest_field or size_field:
                raise ValueError(f"vendored {distribution} wheel RECORD self-row must be unhashed")
            continue
        if not digest_field.startswith("sha256=") or not size_field:
            raise ValueError(f"vendored {distribution} wheel RECORD lacks sha256/size for {path!r}")
        body = zf.read(path)
        expected_digest = digest_field.removeprefix("sha256=")
        actual_digest = base64.urlsafe_b64encode(
            hashlib.sha256(body).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ValueError(f"vendored {distribution} wheel RECORD hash mismatch for {path!r}")
        try:
            expected_size = int(size_field)
        except ValueError as exc:
            raise ValueError(
                f"vendored {distribution} wheel RECORD has invalid size for {path!r}"
            ) from exc
        if expected_size != len(body):
            raise ValueError(f"vendored {distribution} wheel RECORD size mismatch for {path!r}")


def _verify_record(zf: zipfile.ZipFile, *, distribution: str) -> None:
    """Verify every extracted file against the wheel's signed-by-hash RECORD.

    RECORD does not establish publisher identity—the outer trusted SHA256 lock
    does that—but it independently detects corrupt members and prevents files
    omitted from the wheel manifest from reaching ``vendor/``.
    """
    record_name = f"{_dist_info_dir(distribution)}/RECORD"
    names, recorded = _read_record(zf, distribution, record_name)
    _verify_members(zf, names, recorded, distribution, record_name)
