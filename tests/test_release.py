"""GV-01 (ADR-0005 Ruling 5) — monotonic-version guard regression tests.

The whole downgrade mess (plugins re-tagged backwards once) came from
renumbering versions downward. This guard refuses any release whose version
is not strictly greater than the release baseline. Covers both call sites:
``tools/release.py`` (bump/set) and ``tools/package_clients.py
--validate-only``, which reuses the same guard functions rather than
duplicating the rule.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "tools"))
import release  # noqa: E402


# ---------------------------------------------------------------------------
# monotonic_baseline — tag selection, mixed-scheme tag sets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tags,current,expected_baseline",
    [
        # HARDEN:consensus-CRITICAL — mixed scheme: legacy opaque tags (v1,
        # v2) must be structurally ignored, never mistaken for semver 1.x/2.x.
        (["v1", "v2", "v0.9.0", "v0.9.1", "v0.10.0"], "0.9.1", "0.10.0"),
        (["v1", "v2"], "0.3.0", "0.3.0"),  # only legacy tags -> fall back to pyproject
        ([], "0.9.1", "0.9.1"),  # no tags at all
        (["v0.9.0", "v0.9.1"], "0.9.1", "0.9.1"),
        # legacy-export-* renamed tags (this session's own reconciliation) must
        # also never match the semver shape.
        (["legacy-export-v1", "legacy-export-v2", "v0.9.1"], "0.9.1", "0.9.1"),
    ],
)
def test_monotonic_baseline_ignores_legacy_tags(tags, current, expected_baseline):
    assert release.monotonic_baseline(current, tags=tags) == expected_baseline


def test_monotonic_baseline_never_string_compares():
    # 0.10.0 must beat 0.9.1 numerically, never stringwise ("0.10.0" < "0.9.1").
    baseline = release.monotonic_baseline("0.9.1", tags=["v0.9.1", "v0.10.0"])
    assert baseline == "0.10.0"


# ---------------------------------------------------------------------------
# assert_monotonic — the actual guard
# ---------------------------------------------------------------------------

def test_assert_monotonic_refuses_equal_version():
    with pytest.raises(release.ReleaseError, match="0.9.1.*0.9.1"):
        release.assert_monotonic("0.9.1", "0.9.1")


def test_assert_monotonic_refuses_lower_version():
    with pytest.raises(release.ReleaseError) as exc:
        release.assert_monotonic("0.9.0", "0.9.1")
    msg = str(exc.value)
    assert "0.9.0" in msg and "0.9.1" in msg, "error must name both the refused target and the baseline"


def test_assert_monotonic_accepts_strictly_greater_version():
    release.assert_monotonic("0.10.0", "0.9.1")  # must not raise


def test_assert_monotonic_never_string_compares():
    # Pure string order would say "0.10.0" < "0.9.1"; this must not refuse.
    release.assert_monotonic("0.10.0", "0.9.1")


# ---------------------------------------------------------------------------
# apply_release — end-to-end guard wiring (bump/set both route through it)
# ---------------------------------------------------------------------------

def test_apply_release_refuses_non_greater_version(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.9.1"\n', encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [Unreleased]\n\n- some change\n\n## [0.9.1] — 2026-07-01\n", encoding="utf-8")

    monkeypatch.setattr(release, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(release, "CHANGELOG_PATH", changelog)
    monkeypatch.setattr(release, "list_semver_tags", lambda cwd=None: ["v0.9.0", "v0.9.1"])

    with pytest.raises(release.ReleaseError, match="non-increasing"):
        release.apply_release("0.9.0", dry_run=True)


def test_apply_release_accepts_strictly_greater_version(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.9.1"\n', encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [Unreleased]\n\n- some change\n\n## [0.9.1] — 2026-07-01\n", encoding="utf-8")

    monkeypatch.setattr(release, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(release, "CHANGELOG_PATH", changelog)
    monkeypatch.setattr(release, "list_semver_tags", lambda cwd=None: ["v0.9.0", "v0.9.1"])

    release.apply_release("0.10.0", dry_run=True)  # must not raise; dry-run skips the packager subprocess


def test_release_cli_refuses_non_greater_version_end_to_end():
    """Full subprocess invocation of the real CLI entry point against the
    REAL repo (release.py's REPO_ROOT is derived from its own file location,
    not cwd, so there is no scratch-repo way to isolate this call) — proves
    the CLI, not just the importable function, refuses a downgrade. Targets
    a version below the real current baseline (0.9.1); real tags are legacy
    v1/v2 (ignored) + v0.9.0 + v0.9.1, so 0.9.0 must be refused."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "release.py"), "set", "0.9.0", "--dry-run"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "non-increasing" in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# package_clients.py --validate-only wiring (reuses release.py's guard)
# ---------------------------------------------------------------------------

def test_package_clients_validate_monotonic_version_passes_on_real_repo():
    import importlib

    pc = importlib.import_module("package_clients")
    importlib.reload(pc)
    version = pc.read_source_version()
    pc.validate_monotonic_version(version)  # must not raise on the real, current SSOT


def test_package_clients_validate_monotonic_version_fails_below_baseline(monkeypatch):
    import importlib

    pc = importlib.import_module("package_clients")
    importlib.reload(pc)

    monkeypatch.setattr(release, "list_semver_tags", lambda cwd=None: ["v0.9.0", "v0.9.1", "v0.10.0"])
    with pytest.raises(pc.ValidationError):
        pc.validate_monotonic_version("0.9.0")
