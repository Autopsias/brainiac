"""Ed25519 signed hash-chain audit log on the write path (CORE-03).

Ported FROM SCRATCH from the owner vault's ``_audit_chain.py`` *pattern* (JSONL
entries, sha256 prev_hash linkage, domain-separated canonical payload, byte
authentication of the last entry). It is independent code — no vault import.

KEY CUSTODY (CORE-03 hardening): the signing key is resolved from the OS secret
store — macOS **Keychain** / Windows **Credential Manager** — or an injected env
PEM for unattended/sandbox runs. There is **no file fallback**: if no key
resolves, signing FAILS CLOSED (a write is refused rather than written unsigned).

Resolution precedence (first hit wins, all yield identical PEM bytes):
  1. ``$BRAIN_AUDIT_KEY_PEM``  — literal PEM (unattended / Cowork sandbox / tests)
  2. ``$BRAIN_AUDIT_KEY_CMD``  — shell command whose stdout is the PEM
  3. macOS Keychain           — ``security find-generic-password -s <svc> -a <acct> -w``
  4. Windows Credential Manager — via the ``keyring`` package if installed
  (NO bare-file fallback — by design, unlike the vault's transition shim.)
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

JSONL_FORMAT = "brain-audit-chain-jsonl-v1"
NULL_PREV_HASH = "0" * 64
_REQUIRED_KEYS = frozenset({"format", "ts", "verb", "path", "reason", "prev_hash", "sig"})
# Optional signed fields — allowed but not mandatory, so audit logs written
# before this field existed still verify. `content_sha256` binds the exact
# bytes written into the signature, making a post-commit edit of the note
# detectable (AuditChain.content_drift). Backward-compatible by design.
_OPTIONAL_KEYS = frozenset({"content_sha256"})
KEYCHAIN_SERVICE_DEFAULT = "profile-a-brain-audit-key"


class AuditError(RuntimeError):
    pass


class KeyUnavailable(AuditError):
    """No signing key resolved from any OS-secret-store path. Fail closed."""


# --------------------------------------------------------------------------
# crypto
# --------------------------------------------------------------------------
def _require_crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise AuditError(
            "'cryptography' is required for the audit chain "
            "(pip install 'brainiac-cli[audit]')"
        ) from exc


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def generate_key_pem() -> tuple[bytes, bytes]:
    """Return (private_pem, public_pem) for a fresh Ed25519 keypair (setup/tests)."""
    _require_crypto()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


# --------------------------------------------------------------------------
# key custody — OS secret store, NO file fallback
# --------------------------------------------------------------------------
def _pem_from_keychain(service: str, account: str) -> Optional[bytes]:
    if sys.platform != "darwin" or not shutil.which("security"):
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.decode("utf-8", "replace").strip()
    if "BEGIN" not in text and text and re.fullmatch(r"[0-9A-Fa-f]+", text):
        try:
            return bytes.fromhex(text)
        except ValueError:
            return out.stdout
    return out.stdout


def _pem_from_credential_manager(service: str, account: str) -> Optional[bytes]:
    # Windows Credential Manager via the optional `keyring` package.
    if not sys.platform.startswith("win"):
        return None
    try:
        import keyring  # type: ignore

        secret = keyring.get_password(service, account)
        return secret.encode("utf-8") if secret else None
    except Exception:
        return None


def _pem_from_cmd(cmd: str) -> bytes:
    # NOTE: `cmd` is an OPERATOR-controlled env var (BRAIN_AUDIT_KEY_CMD), not
    # untrusted input. shell=True is intentional so custody backends can use a
    # pipeline (e.g. `age -d -i id key.pem.age`). Anyone able to set this env
    # var already has code execution, so shell=True adds no attack surface.
    try:
        # nosec B602 - operator-set env var (BRAIN_AUDIT_KEY_CMD), never untrusted
        # input; see the NOTE above and docs/SECURITY_NOTES.md.
        out = subprocess.run(cmd, shell=True, capture_output=True, timeout=20)  # noqa: S602  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeyUnavailable(f"BRAIN_AUDIT_KEY_CMD failed to run: {exc}") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise KeyUnavailable(
            f"BRAIN_AUDIT_KEY_CMD failed (exit {out.returncode})"
        )
    return out.stdout


def provision_signing_key() -> dict:
    """Create-if-absent the audit signing key in the OS secret store (host setup).

    Idempotent: an existing resolvable key is NEVER touched — rotation would
    make the whole signed chain read as tampered. The PEM is stored hex-encoded
    (single line; the read path in ``_pem_from_keychain`` hex-decodes). Returns
    ``{"status": "present", "source": ...}`` or
    ``{"status": "created", "store": ...}``. Raises KeyUnavailable when no OS
    secret store is available to write to.
    """
    try:
        _, source = resolve_signing_key()
        return {"status": "present", "source": source}
    except KeyUnavailable:
        pass

    service = os.environ.get("BRAIN_AUDIT_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE_DEFAULT)
    account = (
        os.environ.get("BRAIN_AUDIT_KEYCHAIN_ACCOUNT")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "default"
    )
    priv_pem, _pub = generate_key_pem()

    if sys.platform == "darwin" and shutil.which("security"):
        try:
            out = subprocess.run(
                ["security", "add-generic-password",
                 "-s", service, "-a", account, "-w", priv_pem.hex()],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeyUnavailable(f"keychain store failed to run: {exc}") from exc
        if out.returncode != 0:
            # exit 45 = item already exists (raced/ACL-hidden) — never overwrite.
            raise KeyUnavailable(
                f"keychain store failed (exit {out.returncode}): "
                f"{out.stderr.decode('utf-8', 'replace').strip()}"
            )
        return {"status": "created", "store": f"keychain:{service}"}

    if sys.platform.startswith("win"):
        try:
            import keyring  # type: ignore

            keyring.set_password(service, account, priv_pem.decode("utf-8"))
            return {"status": "created", "store": f"credential-manager:{service}"}
        except Exception as exc:
            raise KeyUnavailable(f"Credential Manager store failed: {exc}") from exc

    raise KeyUnavailable(
        "no OS secret store available to write to (need macOS Keychain or "
        "Windows Credential Manager). For a custom custody backend set "
        "BRAIN_AUDIT_KEY_PEM or BRAIN_AUDIT_KEY_CMD instead."
    )


def resolve_signing_key():
    """Resolve the Ed25519 private key from the OS secret store. Fail closed.

    Returns (private_key_obj, source_label). Raises KeyUnavailable if nothing
    resolves — there is intentionally NO bare-key-file fallback.
    """
    _require_crypto()
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    service = os.environ.get("BRAIN_AUDIT_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE_DEFAULT)
    account = (
        os.environ.get("BRAIN_AUDIT_KEYCHAIN_ACCOUNT")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "default"
    )

    pem: Optional[bytes] = None
    source: Optional[str] = None

    # Managed endpoints ignore ad-hoc env/shell key custody — only the OS
    # keystore below is trusted (no PEM in a process env, no shell pipeline).
    from . import config as _config
    allow_env_custody = not _config.is_managed()

    env_pem = os.environ.get("BRAIN_AUDIT_KEY_PEM") if allow_env_custody else None
    if env_pem:
        pem, source = env_pem.encode("utf-8"), "env:BRAIN_AUDIT_KEY_PEM"

    if pem is None and allow_env_custody and os.environ.get("BRAIN_AUDIT_KEY_CMD"):
        pem, source = _pem_from_cmd(os.environ["BRAIN_AUDIT_KEY_CMD"]), "env:BRAIN_AUDIT_KEY_CMD"

    if pem is None:
        kc = _pem_from_keychain(service, account)
        if kc:
            pem, source = kc, f"keychain:{service}"

    if pem is None:
        cm = _pem_from_credential_manager(service, account)
        if cm:
            pem, source = cm, f"credential-manager:{service}"

    if pem is None:
        raise KeyUnavailable(
            "No audit signing key resolved. Tried, in order: $BRAIN_AUDIT_KEY_PEM, "
            "$BRAIN_AUDIT_KEY_CMD, macOS Keychain, Windows Credential Manager. "
            "There is NO file fallback by design — the write path fails closed. "
            "Store the key in the OS secret store (or inject the PEM for unattended runs)."
        )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        key = load_pem_private_key(pem, password=None)
    except Exception as exc:
        raise KeyUnavailable(f"resolved key [{source}] is not a valid PEM private key: {exc}") from exc
    # F-08: assert the key TYPE. load_pem_private_key accepts RSA/ECDSA/Ed25519;
    # a non-Ed25519 key would load here and only fail with a confusing TypeError
    # at sign() time. Fail closed with a clear message instead.
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyUnavailable(
            f"resolved key [{source}] is {type(key).__name__}, expected Ed25519PrivateKey"
        )
    return key, source


@contextlib.contextmanager
def _exclusive_lock(log_path: Path) -> Iterator[None]:
    """Cross-process exclusive lock for the append read-modify-write (F-07).

    Without this, two concurrent writers can both read the same last entry and
    emit two entries with an identical prev_hash, breaking the chain (the exact
    race that hit the owner vault on 2026-06-21). Holds a lock on a sidecar
    ``<log>.lock`` for the whole compute-prev-hash + append section.

    Best-effort on exotic platforms with neither fcntl nor msvcrt (documented),
    but covered on macOS/Linux (host) and Windows.
    """
    lock_path = log_path.with_name(log_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    locked_with = None
    try:
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            locked_with = "fcntl"
        except ImportError:
            try:
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                locked_with = "msvcrt"
            except Exception:
                locked_with = None  # best-effort fallback
        yield
    finally:
        if locked_with == "fcntl":
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif locked_with == "msvcrt":
            try:
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        f.close()


def public_key_pem() -> bytes:
    """Public PEM derived from the resolved signing key (for verify/distribution)."""
    from cryptography.hazmat.primitives import serialization

    key, _ = resolve_signing_key()
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


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
        key, source = resolve_signing_key()
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
DRIFT_DISPOSITIONS_FILENAME = "audit-drift-dispositions.json"


def drift_dispositions_path(vault: Path) -> Path:
    """Host-private triage file, OFF the VM-visible mount (2026-08-07).

    Raises ``config.HostPathUnsafe`` when it cannot resolve somewhere the
    Cowork VM is unable to reach — see ``config.audit_drift_dispositions_path``
    for why this file in particular must be out of reach."""
    from . import config

    return config.audit_drift_dispositions_path(vault)


def legacy_drift_dispositions_path(vault: Path) -> Path:
    """Where this file lived until 2026-08-07: on the shared mount. Read ONLY
    by the one-time carry-forward below; nothing else may consult it again."""
    return Path(vault) / ".brain" / DRIFT_DISPOSITIONS_FILENAME


def migrate_drift_dispositions(vault: Path) -> str | None:
    """Carry a pre-2026-08-07 triage file forward to the host-private location.

    Copy, never move: the destination is the only thing read from now on, and
    deleting the operator's historical record on their behalf is not this
    function's call. Returns a one-line note when it acted, else ``None``.

    The carried-forward records came from a VM-writable path, so the copy is
    STAMPED (``migrated_from_mount``) rather than laundered into looking
    host-authored. Without this, a vault with triaged historical drift would
    report every previously-explained record as unexplained on first run after
    the upgrade — a false tamper alarm, which is the fastest way to teach an
    operator to ignore a real one."""
    legacy = legacy_drift_dispositions_path(vault)
    if not legacy.is_file():
        return None
    try:
        dest = drift_dispositions_path(vault)
    except Exception:  # noqa: BLE001 — unsafe destination: stay fail-closed
        return None
    if dest.exists():
        return None
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    records = raw.get("dispositions") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.parent.chmod(0o700)
        except OSError:
            pass
        dest.write_text(json.dumps(
            {"dispositions": records,
             "migrated_from_mount": legacy.as_posix()},
            indent=2), encoding="utf-8")
    except OSError:
        return None
    return (f"carried {len(records)} drift disposition(s) forward from the shared "
            f"mount to {dest} — they were recorded where a Cowork VM could write, "
            f"so re-check them if you have any reason to doubt that host")


def load_drift_dispositions(vault: Path) -> dict[str, dict]:
    """``{path: record}`` from the triage file; ``{}`` when absent, unreadable,
    or resolvable only to a VM-visible path. Fails CLOSED into "nothing is
    explained" — an unreadable or untrustworthy disposition file must never
    silently clear a drift count."""
    migrate_drift_dispositions(vault)
    try:
        path = drift_dispositions_path(vault)
    except Exception:  # noqa: BLE001 — HostPathUnsafe and anything else
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = raw.get("dispositions") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        return {}
    return {r["path"]: r for r in records
            if isinstance(r, dict) and isinstance(r.get("path"), str)}


def match_disposition(record: dict, dispositions: dict[str, dict]) -> dict | None:
    """The disposition explaining THIS drift record, or ``None``.

    Matching requires the same path, the same issue, and the same observed
    hash the disposition was recorded against — so a further edit re-surfaces
    as unexplained instead of hiding under an old ruling."""
    d = dispositions.get(str(record.get("path")))
    if not isinstance(d, dict) or not d.get("disposition"):
        return None
    if d.get("issue") != record.get("issue"):
        return None
    key = "expected_sha256" if record.get("issue") == "missing" else "actual_sha256"
    if d.get(key) != record.get(key):
        return None
    return d


def drift_summary(vault: Path, chain: "AuditChain") -> dict:
    """``{"total": n, "unexplained": n, "records": [...]}`` — the one place the
    unexplained count is derived, so every health surface gates on the same
    number.

    ponytail: full hash pass, no sampling — 0.3s over a 2,600-note vault on the
    reference deployment. If a vault ever gets big enough for that to hurt
    hourly, cache it on (path, mtime, size) rather than sampling: a sampled
    "0 drift" is the false all-clear this whole item exists to remove."""
    records = chain.content_drift(Path(vault))
    return {
        "total": len(records),
        "unexplained": sum(1 for r in records if not r.get("disposition")),
        "records": records,
    }
