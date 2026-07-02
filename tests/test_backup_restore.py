"""SEC-03 — encrypted off-device backup + restore round-trip (byte-identity)."""
from __future__ import annotations

import tarfile
from pathlib import Path

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
    assert b"secret Atlas counterparty" not in archive.read_bytes()

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


# --------------------------------------------------------------------------
# tar-extraction hardening: a crafted archive with a symlink member pointing
# outside dest_dir must be REJECTED (raises), never followed/extracted.
# --------------------------------------------------------------------------
def _make_symlink_escape_archive(tmp_path: Path) -> tuple[Path, Path]:
    """Build a plain (unencrypted) tar.gz whose single member is a symlink
    named ``evil`` that points at a file OUTSIDE the eventual dest_dir."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    victim = outside / "victim.txt"
    victim.write_text("should never be reachable through the restore\n", encoding="utf-8")

    archive_path = tmp_path / "malicious.tar.gz"  # NOT .enc -> restore_backup treats as plaintext
    with tarfile.open(archive_path, "w:gz") as tar:
        info = tarfile.TarInfo(name="evil")
        info.type = tarfile.SYMTYPE
        info.linkname = "../outside/victim.txt"
        tar.addfile(info)
    return archive_path, victim


def test_restore_rejects_symlink_member_escaping_dest(tmp_path):
    archive_path, victim = _make_symlink_escape_archive(tmp_path)
    dest = tmp_path / "restored"

    with pytest.raises(Exception):
        backup.restore_backup(archive_path, dest)

    # nothing from the malicious member was ever materialised in dest, and the
    # victim file outside dest was never touched/linked-to.
    assert not (dest / "evil").exists()
    assert victim.read_text(encoding="utf-8") == "should never be reachable through the restore\n"


def test_restore_rejects_symlink_member_escaping_dest_pre312_fallback(tmp_path, monkeypatch):
    """Force the manual (pre-3.12 / no tarfile.data_filter) validation path and
    confirm it independently rejects the same crafted archive."""
    archive_path, victim = _make_symlink_escape_archive(tmp_path)
    dest = tmp_path / "restored-fallback"

    monkeypatch.setattr(backup, "_HAS_TAR_DATA_FILTER", False)
    with pytest.raises(ValueError, match="not a regular file or directory"):
        backup.restore_backup(archive_path, dest)

    assert not (dest / "evil").exists()
    assert victim.read_text(encoding="utf-8") == "should never be reachable through the restore\n"
