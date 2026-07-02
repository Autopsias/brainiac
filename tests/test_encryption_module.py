"""SEC-02 — dormant conditional encryption module + key custody (no file fallback)."""
from __future__ import annotations

import pytest

from brain import encryption as enc


@pytest.fixture
def enc_key_env(monkeypatch):
    monkeypatch.setenv("BRAIN_ENCRYPTION_KEY", enc.generate_key_b64())


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("BRAIN_ENCRYPTION", raising=False)
    assert enc.is_enabled() is False


def test_disabled_refuses_encrypt(monkeypatch, enc_key_env):
    monkeypatch.delenv("BRAIN_ENCRYPTION", raising=False)
    with pytest.raises(enc.EncryptionDisabled):
        enc.encrypt_bytes(b"secret")


def test_flip_on_round_trips(monkeypatch, enc_key_env):
    monkeypatch.setenv("BRAIN_ENCRYPTION", "on")
    assert enc.is_enabled() is True
    token = enc.encrypt_bytes(b"material non-public")
    assert token.startswith(enc.MAGIC)
    assert b"material non-public" not in token  # actually encrypted
    assert enc.decrypt_bytes(token) == b"material non-public"


def test_force_bypasses_disabled_flag(monkeypatch, enc_key_env):
    monkeypatch.delenv("BRAIN_ENCRYPTION", raising=False)  # OFF
    token = enc.encrypt_bytes(b"backup payload", force=True)  # backup path
    assert enc.decrypt_bytes(token) == b"backup payload"


def test_tamper_is_detected(monkeypatch, enc_key_env):
    token = bytearray(enc.encrypt_bytes(b"abc", force=True))
    token[-1] ^= 0xFF  # flip a ciphertext byte
    with pytest.raises(enc.EncryptionError):
        enc.decrypt_bytes(bytes(token))


def test_wrong_key_fails(monkeypatch):
    monkeypatch.setenv("BRAIN_ENCRYPTION_KEY", enc.generate_key_b64())
    token = enc.encrypt_bytes(b"abc", force=True)
    monkeypatch.setenv("BRAIN_ENCRYPTION_KEY", enc.generate_key_b64())  # different key
    with pytest.raises(enc.EncryptionError):
        enc.decrypt_bytes(token)


def test_no_key_fails_closed(monkeypatch):
    for var in ("BRAIN_ENCRYPTION_KEY", "BRAIN_ENCRYPTION_KEY_CMD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BRAIN_ENCRYPTION_KEYCHAIN_SERVICE", "profile-a-brain-enc-absent-xyz")
    with pytest.raises(enc.EncryptionKeyUnavailable):
        enc.encrypt_bytes(b"x", force=True)


def test_bad_key_length_rejected(monkeypatch):
    import base64
    monkeypatch.setenv("BRAIN_ENCRYPTION_KEY", base64.b64encode(b"tooshort").decode())
    with pytest.raises(enc.EncryptionKeyUnavailable):
        enc.encrypt_bytes(b"x", force=True)


def test_flip_list_documented():
    assert any("off-device" in t for t in enc.FLIP_LIST)
    assert len(enc.FLIP_LIST) >= 4
