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

# The chain class and the drift-disposition triage live in their own modules
# since the 2026-08-16 size ratchet; re-exported so every `brain.audit.<name>`
# caller (core, conftest, tests) is unchanged.
from .audit_chain import AuditChain as AuditChain  # noqa: E402,F401
from .audit_drift import (  # noqa: E402,F401  (facade re-export)
    DRIFT_DISPOSITIONS_FILENAME as DRIFT_DISPOSITIONS_FILENAME,
    drift_dispositions_path as drift_dispositions_path,
    drift_summary as drift_summary,
    legacy_drift_dispositions_path as legacy_drift_dispositions_path,
    load_drift_dispositions as load_drift_dispositions,
    match_disposition as match_disposition,
    migrate_drift_dispositions as migrate_drift_dispositions,
)
