"""The append-only Ed25519 audit chain (class ``AuditChain``)."""
from __future__ import annotations

import base64
import json
import os
import re as re
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path


# --------------------------------------------------------------------------
# chain
# --------------------------------------------------------------------------
class AuditChain:
    """Append-only Ed25519 hash chain over write events."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)

    def _lines(self) -> list[str]:
        """Every record in the log, read as RAW BYTES and split strictly on
        ``b"\n"`` — no ``strip()``, no re-encode, no trailing-newline trim.

        Python text mode does not do this. ``read_text().splitlines()`` also
        terminates a line on ``\r``, ``\v``, ``\f``, ``\x1c-\x1e``, U+2028 and
        U+2029, and universal newlines rewrites ``\r\n`` to ``\n`` before you
        ever see it. So swapping ONE ``0x0b`` in for a record separator gives a
        log whose bytes hold a single merged record while a text reader still
        sees two — every surviving signature verifies, the prev_hash chain
        links, and the verifier has attested to bytes that are not on disk.
        Bytes and ``b"\n"`` only: the verifier must see exactly what is there.

        Undecodable bytes come back through ``surrogateescape`` rather than
        raising, so a corrupted record fails verification as data — most often
        ``invalid_signature`` (the signed payload changed but stayed valid
        JSON), sometimes ``not_canonical``/``parse_failure`` when the damage
        breaks JSON syntax or the round-trip — instead of taking down the
        whole verification. This depends on ``_sha256`` (``audit.py``) matching
        the same leniency on the way OUT: see ``append``'s comment below and
        the s08 review, 2026-09-04.
        """
        if not self.log_path.exists():
            return []
        return [chunk.decode("utf-8", "surrogateescape")
                for chunk in self.log_path.read_bytes().split(b"\n")]

    @staticmethod
    def _is_entry(line: str) -> bool:
        return line.lstrip().startswith("{")

    def _last_entry(self) -> Optional[str]:
        # F-09 (known, deferred to scale-hardening): O(n) — reads the whole log
        # on every append. Fine for S02 volumes; tail-seek/cache before cutover.
        for line in reversed(self._lines()):
            if self._is_entry(line):
                return line
        return None

    def head(self) -> str:
        """The value the NEXT entry's ``prev_hash`` will carry — i.e. the chain
        tip. ``NULL_PREV_HASH`` on an empty or absent log.

        Exists so a caller can bind a decision to a chain STATE and refuse if
        the chain moved between deciding and writing (DLV-11's batch-level
        expected head). It is deliberately the same expression ``append`` uses
        one line below, rather than a second notion of "where the chain is".
        """
        prev = self._last_entry()
        return _sha256(prev) if prev else NULL_PREV_HASH

    def append(self, verb: str, path: str, reason: str, ts: str | None = None,
               content_sha256: str | None = None) -> dict:
        """Sign + append one entry. Raises KeyUnavailable (fail closed) if no key.

        The compute-prev_hash + write section runs under an exclusive cross-process
        lock (F-07) so concurrent writers cannot fork the chain.

        ``content_sha256`` (when given) binds the exact written bytes into the
        signed payload, so a later edit of the note is detectable via
        ``content_drift`` even though the entry's own signature stays valid.
        """
        key, source = _audit.resolve_signing_key()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.log_path):
            prev = self._last_entry()
            prev_hash = _sha256(prev) if prev else NULL_PREV_HASH
            ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            payload = {
                "format": JSONL_FORMAT,
                "ts": ts,
                "verb": verb.strip(),
                "path": path.strip(),
                "reason": reason.strip(),
                "prev_hash": prev_hash,
            }
            if content_sha256 is not None:
                payload["content_sha256"] = content_sha256.strip()
            # STRICT utf-8 on the way IN, deliberately, and it is not the
            # `surrogateescape` used to READ the log back (`_lines`, `_sha256`
            # in audit.py). Reading has to be lenient — a byte already on disk
            # must be reportable as `parse_failure` rather than crashing the
            # whole verification. Writing has to be strict: a record this
            # cannot canonically encode raises here and the append FAILS, which
            # is the safe direction. Signing it under some repaired encoding
            # would put a record in the chain whose bytes no longer match what
            # was signed. (raised as an advisory by the s08 review, 2026-09-04)
            sig = base64.urlsafe_b64encode(
                key.sign(_canonical(payload).encode("utf-8"))
            ).decode("ascii")
            full = _canonical({**payload, "sig": sig})
            _append_record(self.log_path, full)
        return {"appended": True, "ts": ts, "verb": verb, "path": path, "source": source}

    def verify(self, public_key_pem_bytes: bytes | None = None) -> dict:
        """Walk the chain; verify prev_hash linkage, signatures, byte-canonicality.

        A ``tampered`` verdict carries a ``diagnosis`` when the damage has a
        known benign cause — see ``_text_mode_crlf_diagnosis``. The verdict
        itself never softens: the bytes on disk really are not canonical, and
        a verifier that talks itself out of that is not a verifier.
        """
        _require_crypto()
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        if public_key_pem_bytes is None:
            public_key_pem_bytes = public_key_pem()
        pub = load_pem_public_key(public_key_pem_bytes)

        lines = self._lines()
        errors, checked = self._verify_lines(lines, pub)
        status = ("ok" if not errors and checked
                  else ("empty" if not checked else "tampered"))
        out: dict = {"status": status, "entries_checked": checked,
                     "errors": errors}
        if status == "tampered":
            diagnosis = self._text_mode_crlf_diagnosis(lines, pub)
            if diagnosis:
                out["diagnosis"] = diagnosis
        return out

    def _text_mode_crlf_diagnosis(self, lines: list[str], pub) -> str | None:
        """Name a `tampered` verdict caused by a TEXT-MODE writer, not tampering.

        Until 2026-09-02 `append` wrote through `log_path.open("a")`. On a
        Windows host — a supported target, `docs/substrate-spec.md`'s build
        matrix — Python text mode translates every `\n` to `\r\n` on the way
        out, and the reader of the day (`read_text().splitlines()` + `strip()`)
        absorbed the `\r` on the way back, so the round trip agreed with
        itself. The reader is strict now, by design (a `0x0b` swapped in for a
        record separator used to give a log whose bytes hold one merged record
        while a text reader saw two). The cost of that strictness is this: an
        INTACT chain written on Windows now reports `not_canonical` on every
        entry plus a `prev_hash_mismatch` cascade, which is the loudest alarm
        this system owns, pointed at nothing.

        PROVEN, never guessed: the diagnosis is returned only if dropping one
        trailing `\r` per record makes the whole chain verify clean. A real
        tamper does not repair itself under that transform, and this says
        nothing at all unless it does. Zero such logs exist on the reference
        host (9 chains, 3.4 MB largest, measured 2026-09-02) — this is for the
        Windows install nobody can inspect from here.

        The verdict stays `tampered`. Only the explanation is added.
        """
        if not any(line.endswith("\r") for line in lines if line):
            return None
        healed = [line[:-1] if line.endswith("\r") else line for line in lines]
        errors, checked = self._verify_lines(healed, pub)
        if errors or not checked:
            return None
        return ("every record carries a trailing CR and the chain verifies "
                "clean without it: this log was APPENDED IN TEXT MODE on a "
                "Windows host by an engine older than 2026-09-02, and is "
                "intact — not tampered with. Rewrite it with LF line endings "
                "(the current writer is binary and can no longer produce "
                "this), then re-run verify-audit.")

    def _verify_lines(self, lines: list[str], pub) -> tuple[list[dict], int]:
        """The chain walk over EXACTLY these record strings."""
        from cryptography.exceptions import InvalidSignature

        errors: list[dict] = []
        prev_hash = NULL_PREV_HASH
        checked = 0
        for s in lines:
            if not self._is_entry(s):
                continue
            idx = checked
            checked += 1
            try:
                obj = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                errors.append({"idx": idx, "error": "parse_failure"})
                prev_hash = _sha256(s)
                continue
            keys = set(obj) if isinstance(obj, dict) else set()
            if not (_REQUIRED_KEYS <= keys <= (_REQUIRED_KEYS | _OPTIONAL_KEYS)):
                errors.append({"idx": idx, "error": "unexpected_keys"})
                prev_hash = _sha256(s)
                continue
            if obj.get("format") != JSONL_FORMAT:
                errors.append({"idx": idx, "error": "bad_format"})
            if _canonical(obj) != s:
                errors.append({"idx": idx, "error": "not_canonical"})
            if obj["prev_hash"] != prev_hash:
                errors.append({"idx": idx, "error": "prev_hash_mismatch",
                               "expected": prev_hash[:16], "got": obj["prev_hash"][:16]})
            signed = {k: v for k, v in obj.items() if k != "sig"}
            try:
                pub.verify(base64.urlsafe_b64decode(obj["sig"] + "=="),
                           _canonical(signed).encode("utf-8"))
            except (InvalidSignature, Exception):
                errors.append({"idx": idx, "error": "invalid_signature"})
            prev_hash = _sha256(s)

        return errors, checked

    def latest_signed_hashes(self) -> dict[str, str]:
        """Path -> newest ``content_sha256`` the chain signed for it.

        ``write``/``ingest``/``bind`` verbs set it (``bind`` records a content
        hash for a path an older entry wrote WITHOUT one, on the evidence of
        the note's own capture-time ``sha256:`` field — the bytes are
        unchanged, only the chain's knowledge of them is new); a later
        ``delete``/``write_failed`` drops the path. Entries with no
        ``content_sha256`` (legacy) are skipped: the chain never bound their
        bytes, so it cannot speak for them. Shared by ``content_drift`` and
        the sync-side downgrade guard (VULN-3387)."""
        latest: dict[str, str] = {}
        for s in self._lines():
            if not self._is_entry(s):
                continue
            try:
                obj = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                continue
            csha = obj.get("content_sha256")
            verb = obj.get("verb")
            path = obj.get("path")
            if not isinstance(path, str):
                continue
            if verb in ("write", "ingest", "bind") and isinstance(csha, str):
                latest[path] = csha
            elif verb in ("delete", "write_failed"):
                latest.pop(path, None)
        return latest

    def content_drift(self, vault: Path, *, dispositions: dict | None = None) -> list[dict]:
        """Notes whose CURRENT bytes differ from the last `content_sha256` the
        chain signed for them — i.e. edited (or deleted) after commit without a
        new signed write. Entries with no `content_sha256` (legacy) are skipped:
        the chain never bound their content, so it cannot speak to drift.

        This is what turns the signed hash into a real tamper check: the entry's
        own signature can be perfectly valid while the file it describes has
        been changed on disk. Returns one record per drifted/missing path.

        Every record carries a ``disposition`` — ``None`` for UNEXPLAINED drift
        (the number health surfaces gate on) or the recorded label when a
        triaged disposition file explains it (see ``match_disposition``). Pass
        ``dispositions`` to override the on-disk file (tests); the default
        loads ``<vault>/.brain/audit-drift-dispositions.json``."""
        vault = Path(vault)
        if dispositions is None:
            dispositions = load_drift_dispositions(vault)
        latest = self.latest_signed_hashes()
        drift: list[dict] = []
        for path, expected in latest.items():
            fp = vault / path
            if not fp.is_file():
                drift.append({"path": path, "issue": "missing",
                              "expected_sha256": expected})
                continue
            # RAW BYTES (M-7). This used to be `_sha256(fp.read_text(...))`,
            # and text mode strips `\r` before hashing: a CR-only edit after
            # signing was invisible, and a note legitimately written with CRLF
            # raised a false alarm forever. `write_note` signs
            # `sha256(content.encode("utf-8"))` and writes those same bytes, so
            # the byte hash is the writer's own convention read back honestly.
            actual = sha256_file(fp)
            if actual != expected:
                drift.append({"path": path, "issue": "content_drift",
                              "expected_sha256": expected, "actual_sha256": actual})
        for rec in drift:
            rec["disposition"], rec["disposition_reason"] = _drift_disposition_label(
                rec, dispositions)
        return drift

    def content_coverage(self) -> dict:
        """``{"paths": n, "covered": n, "uncovered": n}`` — how much of the
        chain ``content_drift`` can actually speak for.

        A path whose write entries never carried a ``content_sha256`` is
        skipped by ``content_drift`` entirely: the chain never bound its bytes,
        so it cannot drift, and an edit to it is undetectable. That is a
        BLIND SPOT, not a clean bill, and the drift row must say so — on the
        live reference vault 1637 of 3215 live paths (50.9%) were unbound,
        which is why only one side of a hand-edited PAIR was ever reported
        (2026-08-24 investigation of the F10 repair script).

        Deliberately no backfill: signing today's bytes as the baseline would
        bless every edit already made to those paths."""
        live: set[str] = set()
        covered: set[str] = set()
        for s in self._lines():
            if not self._is_entry(s):
                continue
            try:
                obj = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                continue
            path, verb = obj.get("path"), obj.get("verb")
            if not isinstance(path, str):
                continue
            if verb in ("write", "ingest", "bind"):
                live.add(path)
                if isinstance(obj.get("content_sha256"), str):
                    covered.add(path)
            elif verb in ("delete", "write_failed"):
                live.discard(path)
                covered.discard(path)
        return {"paths": len(live), "covered": len(covered),
                "uncovered": len(live) - len(covered)}


# --------------------------------------------------------------------------
# the log file itself
# --------------------------------------------------------------------------
def _append_record(log_path: Path, record: str) -> None:
    """Append one record + ``\n``, owner-only and never through a symlink.

    ``O_NOFOLLOW`` because the audit log is the one file whose bytes are the
    evidence: a symlink planted at its name would hand every signed append to
    whatever it points at. ``SECURE_FILE_MODE`` because the log carries every
    note path this vault has ever written — on a shared machine that is a
    readable map of the corpus, and it was world-readable until now. The
    ``fchmod`` tightens a log created before this change too; the open mode
    alone only applies at creation.
    """
    from . import config as _config

    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_BINARY"):
        flags |= getattr(os, name, 0)
    fd = os.open(str(log_path), flags, _config.SECURE_FILE_MODE)
    try:
        try:
            os.fchmod(fd, _config.SECURE_FILE_MODE)
        except (OSError, AttributeError):
            pass  # Windows / exotic fs: the mode bits are best-effort there
        data = (record + "\n").encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# drift dispositions (INT-02)
# --------------------------------------------------------------------------
# A vault that predates drift VISIBILITY carries a historical drift trail:
# notes edited outside the audited write path before anything reported it.
# Deleting or re-signing those would destroy the evidence, and leaving them
# uncounted would make every future real drift invisible in the noise. So they
# are TRIAGED once into a disposition file, and the health surfaces subtract
# only what that file explains.
#
# A disposition is PINNED to the exact bytes it was recorded against: the file
# drifting AGAIN produces a different `actual_sha256`, no longer matches, and
# comes back as unexplained. Absorbing a whole path forever is exactly the
# failure mode this instrument exists to prevent.
#
# WHERE IT LIVES (changed 2026-08-07): the host-private app-data dir, NOT
# `<vault>/.brain/`. This file decides whether tampering counts as explained,
# and a match needs only path + issue + observed hash -- all of which whoever
# edited the note already knows. On the old path, the Cowork VM could write it,
# so the untrusted leg could silence the host's own tamper alarm. Pinning to
# bytes is what stops a disposition absorbing a path forever; being off the
# mount is what stops it being forged. Both are needed.

# Parent-namespace binds, deferred past this module's own defs (circular-import
# safety, whichever of brain.audit / brain.audit_chain loads first).
from .audit import (  # noqa: E402
    JSONL_FORMAT as JSONL_FORMAT,
    KeyUnavailable as KeyUnavailable,
    NULL_PREV_HASH as NULL_PREV_HASH,
    _OPTIONAL_KEYS as _OPTIONAL_KEYS,
    _REQUIRED_KEYS as _REQUIRED_KEYS,
    _canonical as _canonical,
    _require_crypto as _require_crypto,
    _exclusive_lock as _exclusive_lock,
    _sha256 as _sha256,
    public_key_pem as public_key_pem,
    resolve_signing_key as resolve_signing_key,
)
from .notes import sha256_file as sha256_file  # noqa: E402
from .audit_drift import load_drift_dispositions as load_drift_dispositions  # noqa: E402
from .audit_drift import match_disposition as match_disposition  # noqa: E402
from .audit_drift import (  # noqa: E402
    drift_disposition_label as _drift_disposition_label,
)

# Facade bind for the monkeypatch contract: tests patch
# brain.audit.resolve_signing_key / provision_signing_key, so call sites here
# resolve through the parent namespace at call time.
from . import audit as _audit  # noqa: E402
