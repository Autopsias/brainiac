"""SEC-03 — encrypted off-device backup + restore round-trip (byte-identity)."""
from __future__ import annotations

import pytest

from brain import backup, encryption as enc


@pytest.fixture
def enc_key_env(monkeypatch):
    monkeypatch.setenv("BRAIN_ENCRYPTION_KEY", enc.generate_key_b64())


def test_encrypted_backup_round_trips(sample_vault, tmp_path, enc_key_env):
    offdev = tmp_path / "offdevice"
    man = backup.create_backup(sample_vault, offdev)
    assert man.encrypted is True
    assert man.files >= 5
    from pathlib import Path
    archive = Path(man.archive)
    assert archive.suffix == ".enc"
    # the ciphertext must NOT contain the restricted plaintext
    assert b"secret Meridian counterparty" not in archive.read_bytes()

    dest = tmp_path / "restored"
    res = backup.restore_backup(archive, dest)
    # byte-identity of the plaintext archive proves a clean round-trip
    assert res["plaintext_sha256"] == man.plaintext_sha256
    assert res["files"] == man.files
    # restored markdown matches the source content
    src = (sample_vault / "brain/projects/restricted-deal.md").read_text()
    out = (dest / "brain/projects/restricted-deal.md").read_text()
    assert src == out


def test_backup_excludes_runtime_index(sample_vault, tmp_path, enc_key_env):
    # a .brain runtime dir must NOT be shipped off-device
    (sample_vault / ".brain").mkdir()
    (sample_vault / ".brain" / "index.sqlite").write_text("derived-junk")
    man = backup.create_backup(sample_vault, tmp_path / "off")
    dest = tmp_path / "restored"
    backup.restore_backup(man.archive, dest)
    assert not (dest / ".brain").exists()


def test_backup_fails_closed_without_key(sample_vault, tmp_path, monkeypatch):
    for var in ("BRAIN_ENCRYPTION_KEY", "BRAIN_ENCRYPTION_KEY_CMD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BRAIN_ENCRYPTION_KEYCHAIN_SERVICE", "profile-a-brain-enc-absent-xyz")
    with pytest.raises(enc.EncryptionKeyUnavailable):
        backup.create_backup(sample_vault, tmp_path / "off")
    # nothing encrypted should have been written
    off = tmp_path / "off"
    if off.exists():
        assert not list(off.glob("*.enc"))


def test_tampered_encrypted_backup_refuses_restore(sample_vault, tmp_path, enc_key_env):
    man = backup.create_backup(sample_vault, tmp_path / "off")
    from pathlib import Path
    archive = Path(man.archive)
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0xFF
    archive.write_bytes(bytes(data))
    with pytest.raises(enc.EncryptionError):
        backup.restore_backup(archive, tmp_path / "restored")
