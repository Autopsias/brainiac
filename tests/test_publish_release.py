"""GV-02 — tools/publish_release.py: the one-command scriptable publish-prep
flow (package validate -> export -> contamination scan -> optional local tag).
Never touches the push URL; the human publish step (runbook §8) has no
Python surface to test here by design."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "tools"))
import publish_release  # noqa: E402


def test_check_mode_passes_against_the_real_repo():
    """--check runs the real package_clients validate + a real export, no
    denylist/tag required. Must succeed on the current committed state."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "publish_release.py"), "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "All scriptable release gates passed" in proc.stdout


def test_contamination_scan_refuses_without_denylist_flag():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "publish_release.py")],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "--denylist is required" in proc.stderr


def test_step_contamination_scan_reports_zero_hits_for_a_denylist_term_absent_from_export(tmp_path):
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("zzz-nonexistent-codename-zzz\n", encoding="utf-8")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "a.txt").write_text("nothing sensitive here\n", encoding="utf-8")

    counts = publish_release.step_contamination_scan(export_dir, denylist)
    assert counts["export_hit_count"] == 0


def test_step_contamination_scan_detects_a_real_hit(tmp_path):
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("supersecretcodename\n", encoding="utf-8")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "leaky.txt").write_text("this file mentions supersecretcodename by accident\n", encoding="utf-8")

    counts = publish_release.step_contamination_scan(export_dir, denylist)
    assert counts["export_hit_count"] > 0


def test_step_contamination_scan_refuses_missing_denylist(tmp_path):
    with pytest.raises(publish_release.PublishError, match="denylist not found"):
        publish_release.step_contamination_scan(tmp_path, tmp_path / "does-not-exist.txt")


def test_step_local_tag_refuses_when_tag_already_exists():
    # v0.9.1 is a real local tag in this repo already.
    with pytest.raises(publish_release.PublishError, match="already exists"):
        publish_release.step_local_tag("0.9.1")


def test_evidence_hits_are_informational_only_and_never_fail_main(tmp_path, monkeypatch):
    """A non-zero _evidence hit count must NOT fail the run — _evidence/ never
    ships in the export, and it carries known-benign synthetic-fixture terms
    (e.g. the v0.10.0-adjudicated "Contoso"/"Northwind"-style placeholders)
    that would trip a hard gate on every release. Only export_hit_count > 0
    is allowed to fail main(). Regression test for the gate-label mismatch
    found during the v0.10.0 code-review gate."""
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("totally-benign-fixture-term\n", encoding="utf-8")

    # Real REPO_ROOT/_evidence contains this term (planted here, then removed).
    evidence_dir = REPO_ROOT / "_evidence"
    planted = evidence_dir / "publish_release_test_plant.txt"
    planted.write_text("totally-benign-fixture-term\n", encoding="utf-8")
    try:
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        (export_dir / "clean.txt").write_text("nothing sensitive here\n", encoding="utf-8")

        counts = publish_release.step_contamination_scan(export_dir, denylist)
        assert counts["evidence_hit_count"] > 0
        assert counts["export_hit_count"] == 0
    finally:
        planted.unlink()

    # main() must only ever raise on export_hit_count, never evidence_hit_count.
    monkeypatch.setattr(
        publish_release, "step_contamination_scan",
        lambda export_dir, denylist: {"export_hit_count": 0, "evidence_hit_count": 999},
    )
    monkeypatch.setattr(publish_release, "step_package_validate", lambda: "OK")
    monkeypatch.setattr(publish_release, "step_export", lambda output_dir: output_dir)
    monkeypatch.setattr(sys, "argv", ["publish_release.py", "--denylist", str(denylist)])

    assert publish_release.main() == 0


def test_evidence_hit_print_is_labeled_informational_not_expected_zero():
    """The printed _evidence line must read as informational, not as an
    enforced expectation — it must not claim '(expected 0)' the way the
    export line correctly does."""
    source = (REPO_ROOT / "tools" / "publish_release.py").read_text(encoding="utf-8")
    assert "_evidence hits: {counts['evidence_hit_count']}" in source
    # The line printing evidence_hit_count must say "informational", and must
    # not be the old misleading "(expected 0)" wording.
    idx = source.index("_evidence hits: {counts['evidence_hit_count']}")
    snippet = source[idx - 20 : idx + 200]
    assert "informational" in snippet
