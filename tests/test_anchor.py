"""SEC-03 — off-host audit-chain anchor: consistent => ok; rewrite => divergence."""
from __future__ import annotations

import json

from brain import anchor as A
from brain.audit import AuditChain


def _chain(tmp_path):
    c = AuditChain(tmp_path / "audit.jsonl")
    c.append("write", "brain/a.md", "first")
    c.append("write", "brain/b.md", "second")
    c.append("write", "brain/c.md", "third")
    return c


def test_anchor_then_verify_ok(tmp_path, audit_key_env):
    c = _chain(tmp_path)
    adir = tmp_path / "offhost"
    rec = A.anchor(c.log_path, adir)
    assert rec["record"]["entry_count"] == 3
    res = A.verify_against_anchor(c.log_path, adir)
    assert res["status"] == "ok" and res["checked"] == 1


def test_no_anchor_is_distinct(tmp_path, audit_key_env):
    c = _chain(tmp_path)
    res = A.verify_against_anchor(c.log_path, tmp_path / "empty")
    assert res["status"] == "no-anchor"


def test_silent_rewrite_is_detected(tmp_path, audit_key_env):
    c = _chain(tmp_path)
    adir = tmp_path / "offhost"
    A.anchor(c.log_path, adir)  # anchor head @ 3 entries

    # Adversary holds the key: rewrite entry 1's reason and RE-SIGN the whole
    # chain so brain.audit.verify() would still pass — but the off-host head
    # recorded @3 no longer matches the recomputed head.
    lines = c.log_path.read_text().splitlines()
    obj = json.loads(lines[0])
    obj["reason"] = "REWRITTEN"
    # re-link + re-sign from the tampered entry forward
    from brain.audit import _canonical, _sha256, resolve_signing_key
    key, _ = resolve_signing_key()
    import base64
    prev = A.NULL_PREV_HASH
    new_lines = []
    rebuilt = [obj] + [json.loads(l) for l in lines[1:]]
    for o in rebuilt:
        payload = {k: v for k, v in o.items() if k != "sig"}
        payload["prev_hash"] = prev
        sig = base64.urlsafe_b64encode(key.sign(_canonical(payload).encode())).decode("ascii")
        full = _canonical({**payload, "sig": sig})
        new_lines.append(full)
        prev = _sha256(full)
    c.log_path.write_text("\n".join(new_lines) + "\n")

    # The internal chain verify now PASSES (fully re-signed) ...
    assert c.verify()["status"] == "ok"
    # ... but the off-host anchor catches the silent rewrite.
    res = A.verify_against_anchor(c.log_path, adir)
    assert res["status"] == "divergence"
    assert res["divergences"][0]["error"] == "head_mismatch"


def test_chain_truncation_is_detected(tmp_path, audit_key_env):
    c = _chain(tmp_path)
    adir = tmp_path / "offhost"
    A.anchor(c.log_path, adir)  # anchored @ 3
    # Truncate the live chain to 2 entries.
    lines = c.log_path.read_text().splitlines()
    c.log_path.write_text("\n".join(lines[:2]) + "\n")
    res = A.verify_against_anchor(c.log_path, adir)
    assert res["status"] == "divergence"
    assert res["divergences"][0]["error"] == "chain_shorter_than_anchor"
