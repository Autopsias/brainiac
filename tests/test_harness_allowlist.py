"""SEC-01 — trusted-harness allowlist register is well-formed and fail-closed.

Openness != 'any app'. A harness is ALLOWED only when its vendor posture is
VERIFIED; PENDING/REJECTED default-deny. val-03's cross-harness set must equal
the VERIFIED subset of this register.
"""
from __future__ import annotations

import pytest

from brain import egress


def test_allowlist_loads_and_is_well_formed():
    data = egress.load_allowlist()
    ids = {h["id"] for h in data["harnesses"]}
    # the three distinct vendor contracts named in the hardening
    assert {"claude-desktop", "codex-cli", "gemini-cli"} <= ids
    vendors = {h["vendor"] for h in data["harnesses"]}
    assert {"Anthropic", "OpenAI", "Google"} <= vendors


def test_pending_posture_is_default_deny():
    # Until contractually VERIFIED, no harness is 'allowed' (we cannot assert
    # no-train/ZDR coverage we have not confirmed).
    for hid in ("claude-desktop", "codex-cli", "gemini-cli"):
        assert egress.is_allowed(hid) is False  # all PENDING today


def test_unknown_harness_is_denied():
    assert egress.is_allowed("some-random-app") is False


def test_every_entry_has_verification_step_and_owner():
    data = egress.load_allowlist()
    for h in data["harnesses"]:
        assert h["verification_step"].strip()
        assert h["owner"].strip()
        assert h["posture_status"] in {"VERIFIED", "PENDING", "REJECTED"}


def test_posture_summary_counts():
    s = egress.posture_summary()
    assert s["total"] == s["by_status"]["VERIFIED"] + s["by_status"]["PENDING"] + s["by_status"]["REJECTED"]


def test_malformed_register_fails_closed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"harnesses": "not-a-list"}', encoding="utf-8")
    with pytest.raises(ValueError):
        egress.load_allowlist(bad)
