"""DORMANT conditional at-rest encryption module (SEC-02).

Design v5 §2 walked back "encrypt the sensitive zone = load-bearing": on a
single-user machine with FileVault/BitLocker on, broad app-level encryption
above FDE is over-engineering. So this module exists but is **OFF by default**.
It switches on ONLY for a documented trigger (the flip-list), the most important
being an **off-device backup/sync** of a decrypted-readable copy — which is the
one place encryption genuinely matters (used by brain.backup, SEC-03).

Primitive: AES-256-GCM (authenticated). Key custody mirrors the audit chain —
OS secret store / injected env, **NO file fallback** — and uses a *separate* key
from the signing key (encryption != signing). Fail-closed: if encryption is
requested but no key resolves, the operation refuses rather than writing
plaintext.

    from_env:   $BRAIN_ENCRYPTION_KEY  (base64 of 32 raw bytes)
    from_cmd:   $BRAIN_ENCRYPTION_KEY_CMD (stdout = base64 key)
    keychain:   macOS `security` / Windows Credential Manager (keyring)
    (NO bare-file fallback — by design.)
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from typing import Optional

MAGIC = b"BRAINENCv1"          # token header so a wrong-format blob fails loudly
NONCE_LEN = 12                  # AES-GCM standard nonce
KEY_LEN = 32                    # AES-256
KEYCHAIN_SERVICE_DEFAULT = "profile-a-brain-encryption-key"

# The flip-list (design v5 §2). Encryption is OFF unless one of these holds.
FLIP_LIST: tuple[str, ...] = (
    "off-device backup or cloud sync of a decrypted-readable copy "
    "(iCloud/OneDrive/Dropbox/network share)",
    "genuinely regulated data (PCI mandated; MNPI/PII under a flagged regime)",
    "shared / multi-user machine (OS permissions are the weak point)",
    "a cyber team contractually mandates app-level encryption at rest",
)


class EncryptionError(RuntimeError):
    pass


class EncryptionKeyUnavailable(EncryptionError):
    """No encryption key resolved from any OS-secret-store path. Fail closed."""


class EncryptionDisabled(EncryptionError):
    """Encryption is OFF (default) and was invoked without an explicit override."""


def is_enabled() -> bool:
    """True iff at-rest app-encryption has been flipped ON for this install.

    OFF by default. Enable with ``BRAIN_ENCRYPTION=on`` (or 1/true/yes). The
    backup path may still FORCE encryption regardless of this flag, because an
    off-device backup is the one place encryption matters even when the at-rest
    baseline stays FDE-only.
    """
    return os.environ.get("BRAIN_ENCRYPTION", "").strip().lower() in {"on", "1", "true", "yes"}


def _require_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise EncryptionError(
            "'cryptography' is required for the encryption module "
            "(pip install 'profile-a-brain[audit]')"
        ) from exc


def _key_from_keychain(service: str, account: str) -> Optional[bytes]:
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
    return out.stdout.strip()


def _key_from_credential_manager(service: str, account: str) -> Optional[bytes]:
    if not sys.platform.startswith("win"):
        return None
    try:
        import keyring  # type: ignore

        secret = keyring.get_password(service, account)
        return secret.encode("utf-8") if secret else None
    except Exception:
        return None


def _decode_key(raw: bytes, source: str) -> bytes:
    """Accept a base64-encoded 32-byte key (or raw 32 bytes). Fail closed on a
    wrong-length key — never derive/pad silently."""
    raw = raw.strip()
    if len(raw) == KEY_LEN:
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise EncryptionKeyUnavailable(
            f"encryption key [{source}] is not base64 nor {KEY_LEN} raw bytes: {exc}"
        ) from exc
    if len(decoded) != KEY_LEN:
        raise EncryptionKeyUnavailable(
            f"encryption key [{source}] decodes to {len(decoded)} bytes; need {KEY_LEN} (AES-256)"
        )
    return decoded


def resolve_encryption_key() -> tuple[bytes, str]:
    """Resolve the 32-byte AES key from the OS secret store / env. Fail closed.

    Returns (key_bytes, source_label). Raises EncryptionKeyUnavailable if nothing
    resolves — there is intentionally NO bare-key-file fallback.
    """
    service = os.environ.get("BRAIN_ENCRYPTION_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE_DEFAULT)
    account = (
        os.environ.get("BRAIN_ENCRYPTION_KEYCHAIN_ACCOUNT")
        or os.environ.get("USER") or os.environ.get("USERNAME") or "default"
    )

    env_key = os.environ.get("BRAIN_ENCRYPTION_KEY")
    if env_key:
        return _decode_key(env_key.encode("utf-8"), "env:BRAIN_ENCRYPTION_KEY"), "env:BRAIN_ENCRYPTION_KEY"

    cmd = os.environ.get("BRAIN_ENCRYPTION_KEY_CMD")
    if cmd:
        try:
            # `cmd` is an OPERATOR-controlled env var (BRAIN_ENCRYPTION_KEY_CMD),
            # not untrusted input -- mirrors audit._pem_from_cmd's BRAIN_AUDIT_
            # KEY_CMD rationale. shell=True is intentional so custody backends
            # can use a pipeline (e.g. `age -d -i id key.pem.age`); anyone able
            # to set this env var already has code execution, so shell=True adds
            # no attack surface. nosec B602 - see docs/SECURITY_NOTES.md.
            out = subprocess.run(cmd, shell=True, capture_output=True, timeout=20)  # noqa: S602
        except (OSError, subprocess.SubprocessError) as exc:
            raise EncryptionKeyUnavailable(f"BRAIN_ENCRYPTION_KEY_CMD failed: {exc}") from exc
        if out.returncode != 0 or not out.stdout.strip():
            raise EncryptionKeyUnavailable(f"BRAIN_ENCRYPTION_KEY_CMD failed (exit {out.returncode})")
        return _decode_key(out.stdout, "env:BRAIN_ENCRYPTION_KEY_CMD"), "env:BRAIN_ENCRYPTION_KEY_CMD"

    kc = _key_from_keychain(service, account)
    if kc:
        return _decode_key(kc, f"keychain:{service}"), f"keychain:{service}"

    cm = _key_from_credential_manager(service, account)
    if cm:
        return _decode_key(cm, f"credential-manager:{service}"), f"credential-manager:{service}"

    raise EncryptionKeyUnavailable(
        "No encryption key resolved. Tried, in order: $BRAIN_ENCRYPTION_KEY, "
        "$BRAIN_ENCRYPTION_KEY_CMD, macOS Keychain, Windows Credential Manager. "
        "There is NO file fallback by design — encryption fails closed. Store a "
        "base64 32-byte key in the OS secret store (or inject it for unattended runs)."
    )


def generate_key_b64() -> str:
    """Mint a fresh base64-encoded AES-256 key (for setup/tests)."""
    return base64.b64encode(os.urandom(KEY_LEN)).decode("ascii")


def encrypt_bytes(data: bytes, *, force: bool = False) -> bytes:
    """Encrypt ``data`` to a self-describing token (MAGIC || nonce || ciphertext).

    Refuses unless encryption is enabled (``is_enabled()``) OR ``force=True`` (the
    backup path forces it). Fails closed if no key resolves.
    """
    if not force and not is_enabled():
        raise EncryptionDisabled(
            "at-rest encryption is OFF (default). Set BRAIN_ENCRYPTION=on to enable, "
            "or call with force=True (the off-device backup path). Flip-list: "
            + "; ".join(FLIP_LIST)
        )
    _require_crypto()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, _src = resolve_encryption_key()
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, data, MAGIC)
    return MAGIC + nonce + ct


def decrypt_bytes(token: bytes) -> bytes:
    """Decrypt a token produced by :func:`encrypt_bytes`. Authenticated — a
    tampered token (or wrong key) raises EncryptionError, never returns garbage."""
    _require_crypto()
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not token.startswith(MAGIC):
        raise EncryptionError("not a brain encryption token (bad magic header)")
    body = token[len(MAGIC):]
    if len(body) < NONCE_LEN + 16:
        raise EncryptionError("encryption token too short / truncated")
    nonce, ct = body[:NONCE_LEN], body[NONCE_LEN:]
    key, _src = resolve_encryption_key()
    try:
        return AESGCM(key).decrypt(nonce, ct, MAGIC)
    except InvalidTag as exc:
        raise EncryptionError(
            "decryption failed: authentication tag mismatch (wrong key or tampered token)"
        ) from exc
