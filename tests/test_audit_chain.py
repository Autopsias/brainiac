"""Ed25519 audit hash-chain on the write path (CORE-03)."""
from __future__ import annotations

import json

import pytest

from brain.audit import AuditChain, KeyUnavailable
from brain.core import BrainCore
from brain.index import BrainIndex
from brain.vectors import get_backend


def test_append_and_verify_ok(tmp_path, audit_key_env):
    chain = AuditChain(tmp_path / "audit.jsonl")
    chain.append("write", "brain/a.md", "first")
    chain.append("edit", "brain/a.md", "second")
    chain.append("write", "brain/b.md", "third")
    res = chain.verify()
    assert res["status"] == "ok"
    assert res["entries_checked"] == 3
    assert res["errors"] == []


def test_tamper_breaks_chain(tmp_path, audit_key_env):
    log = tmp_path / "audit.jsonl"
    chain = AuditChain(log)
    chain.append("write", "brain/a.md", "first")
    chain.append("write", "brain/b.md", "second")
    # Tamper: rewrite the reason of the first entry.
    lines = log.read_text().splitlines()
    obj = json.loads(lines[0])
    obj["reason"] = "TAMPERED"
    lines[0] = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    log.write_text("\n".join(lines) + "\n")
    res = chain.verify()
    assert res["status"] == "tampered"
    assert any(e["error"] in ("invalid_signature", "prev_hash_mismatch") for e in res["errors"])


def test_no_key_fails_closed(tmp_path, monkeypatch):
    # Ensure NO key path resolves.
    for var in ("BRAIN_AUDIT_KEY_PEM", "BRAIN_AUDIT_KEY_CMD"):
        monkeypatch.delenv(var, raising=False)
    # Point keychain service at a name that won't exist; on non-darwin this is moot.
    monkeypatch.setenv("BRAIN_AUDIT_KEYCHAIN_SERVICE", "profile-a-brain-test-absent-xyz")
    chain = AuditChain(tmp_path / "audit.jsonl")
    with pytest.raises(KeyUnavailable):
        chain.append("write", "brain/a.md", "should fail closed")
    assert not (tmp_path / "audit.jsonl").exists()


def test_write_note_is_audited(tmp_path, sample_vault, audit_key_env):
    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=get_backend("brute-force"))
    core = BrainCore(vault=sample_vault, index=idx, audit_log=tmp_path / "audit.jsonl")
    res = core.write_note("brain/resources/new-note.md",
                          "---\nid: new-note\nclassification: Internal\n---\nbody\n",
                          reason="test write")
    assert (sample_vault / "brain/resources/new-note.md").exists()
    assert res["audit"]["appended"] is True
    assert core.verify_audit()["status"] == "ok"


def test_non_ed25519_key_rejected(tmp_path, monkeypatch):
    # F-08: an RSA key must be rejected as KeyUnavailable, not fail at sign time.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv("BRAIN_AUDIT_KEY_PEM", pem.decode("utf-8"))
    chain = AuditChain(tmp_path / "audit.jsonl")
    with pytest.raises(KeyUnavailable) as exc:
        chain.append("write", "brain/a.md", "rsa key")
    assert "Ed25519" in str(exc.value)


def test_concurrent_appends_do_not_fork_chain(tmp_path, audit_key_env):
    # F-07: many threads appending under the lock must keep a valid linear chain.
    import threading

    chain = AuditChain(tmp_path / "audit.jsonl")
    errors: list = []

    def worker(i):
        try:
            chain.append("write", f"brain/n{i}.md", f"concurrent {i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    res = chain.verify()
    assert res["status"] == "ok", res
    assert res["entries_checked"] == 12


def test_write_failure_records_compensating_entry(tmp_path, sample_vault, audit_key_env):
    # F-06: if the file write fails after signing, a write_failed entry is added
    # and the chain still verifies (no phantom completed-write claim left alone).
    from brain.core import BrainCore
    from brain.index import BrainIndex
    from brain.vectors import get_backend

    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=get_backend("brute-force"))
    core = BrainCore(vault=sample_vault, index=idx, audit_log=tmp_path / "audit.jsonl")
    # An absolute path that escapes the vault is rejected before signing; instead
    # force a write failure by pointing at a path whose parent is a file.
    (sample_vault / "brain" / "afile").write_text("x")
    with pytest.raises(Exception):
        core.write_note("brain/afile/cannot.md", "body", reason="should fail at write")
    res = core.verify_audit()
    assert res["status"] == "ok"
    # both the attempt and the failure are recorded
    log = (tmp_path / "audit.jsonl").read_text()
    assert '"verb":"write"' in log
    assert '"verb":"write_failed"' in log


def test_write_note_fails_closed_no_file(tmp_path, sample_vault, monkeypatch):
    for var in ("BRAIN_AUDIT_KEY_PEM", "BRAIN_AUDIT_KEY_CMD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BRAIN_AUDIT_KEYCHAIN_SERVICE", "profile-a-brain-test-absent-xyz")
    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=get_backend("brute-force"))
    core = BrainCore(vault=sample_vault, index=idx, audit_log=tmp_path / "audit.jsonl")
    with pytest.raises(KeyUnavailable):
        core.write_note("brain/resources/x.md", "body", reason="no key")
    # fail closed: the note must NOT have been written
    assert not (sample_vault / "brain/resources/x.md").exists()


def test_provision_present_never_rotates(monkeypatch, audit_key_env):
    # With a resolvable key (env PEM via audit_key_env), provision must be a
    # no-op that reports "present" — never generate/store a new key.
    from brain import audit

    def boom(*a, **k):
        raise AssertionError("provision must not touch the secret store when a key resolves")

    monkeypatch.setattr(audit.subprocess, "run", boom)
    res = audit.provision_signing_key()
    assert res["status"] == "present"
    assert res["source"] == "env:BRAIN_AUDIT_KEY_PEM"


def test_provision_creates_when_absent(monkeypatch):
    # No key anywhere -> provision generates one and stores it (store call mocked).
    from brain import audit

    for var in ("BRAIN_AUDIT_KEY_PEM", "BRAIN_AUDIT_KEY_CMD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BRAIN_AUDIT_KEYCHAIN_SERVICE", "profile-a-brain-test-absent-xyz")
    monkeypatch.setattr(audit.sys, "platform", "darwin")
    monkeypatch.setattr(audit.shutil, "which", lambda _: "/usr/bin/security")

    calls = []

    class FakeDone:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[1] == "find-generic-password":
            r = FakeDone()
            r.returncode = 44  # not found
            return r
        assert cmd[1] == "add-generic-password"
        # stored value must be single-line hex (the read path hex-decodes)
        assert cmd[cmd.index("-w") + 1].strip().isalnum()
        return FakeDone()

    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    res = audit.provision_signing_key()
    assert res["status"] == "created"
    assert any(c[1] == "add-generic-password" for c in calls)
