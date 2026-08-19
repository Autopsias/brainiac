"""The append-only Ed25519 audit chain (class ``AuditChain``)."""
from __future__ import annotations

import base64
import json
import os
import re
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
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _is_entry(line: str) -> bool:
        return line.lstrip().startswith("{")

    def _last_entry(self) -> Optional[str]:
        # F-09 (known, deferred to scale-hardening): O(n) — reads the whole log
        # on every append. Fine for S02 volumes; tail-seek/cache before cutover.
        for line in reversed(self._lines()):
            s = line.strip()
            if self._is_entry(s):
                return s
        return None

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
            sig = base64.urlsafe_b64encode(
                key.sign(_canonical(payload).encode("utf-8"))
            ).decode("ascii")
            full = _canonical({**payload, "sig": sig})
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(full + "\n")
        return {"appended": True, "ts": ts, "verb": verb, "path": path, "source": source}

    def verify(self, public_key_pem_bytes: bytes | None = None) -> dict:
        """Walk the chain; verify prev_hash linkage, signatures, byte-canonicality."""
        _require_crypto()
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        if public_key_pem_bytes is None:
            public_key_pem_bytes = public_key_pem()
        pub = load_pem_public_key(public_key_pem_bytes)

        errors: list[dict] = []
        prev_hash = NULL_PREV_HASH
        checked = 0
        for raw in self._lines():
            s = raw.strip()
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

        return {
            "status": "ok" if not errors and checked else ("empty" if not checked else "tampered"),
            "entries_checked": checked,
            "errors": errors,
        }

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
        latest: dict[str, str] = {}
        for raw in self._lines():
            s = raw.strip()
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
            if verb in ("write", "ingest") and isinstance(csha, str):
                latest[path] = csha
            elif verb in ("delete", "write_failed"):
                latest.pop(path, None)
        drift: list[dict] = []
        for path, expected in latest.items():
            fp = vault / path
            if not fp.is_file():
                drift.append({"path": path, "issue": "missing",
                              "expected_sha256": expected})
                continue
            actual = _sha256(fp.read_text(encoding="utf-8"))
            if actual != expected:
                drift.append({"path": path, "issue": "content_drift",
                              "expected_sha256": expected, "actual_sha256": actual})
        for rec in drift:
            match = match_disposition(rec, dispositions)
            rec["disposition"] = (match or {}).get("disposition")
            rec["disposition_reason"] = (match or {}).get("reason")
        return drift


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
from .audit_drift import load_drift_dispositions as load_drift_dispositions  # noqa: E402
from .audit_drift import match_disposition as match_disposition  # noqa: E402

# Facade bind for the monkeypatch contract: tests patch
# brain.audit.resolve_signing_key / provision_signing_key, so call sites here
# resolve through the parent namespace at call time.
from . import audit as _audit  # noqa: E402
