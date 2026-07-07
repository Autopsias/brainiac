"""DV-01 (ADR-0005 Ruling 1) — from-source version reporting regression.

The zero-install Cowork VM imports ``brain`` from a staged source copy with
NO package metadata, so ``importlib.metadata`` can never answer there. These
tests prove that a git-ls-files copy of ``src/brain`` (exactly what the
clean-room export and the workspace stager ship — tracked files only) reports
the real pyproject version via the COMMITTED ``src/brain/_version.py`` stamp,
never ``0.0.0+unknown``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Simulate the zero-install VM inside the subprocess: deny package metadata
# BEFORE brain is imported, so importlib.metadata can never answer and the
# committed stamp fallback is the only real-version path left.
NO_METADATA_STUB = """\
import importlib.metadata as _md

def _deny(name):
    raise _md.PackageNotFoundError(name)

_md.version = _deny  # zero-install VM: no package metadata, ever
import brain
print(brain.__version__)
"""


def pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, "pyproject.toml has no version"
    return m.group(1)


def export_brain_pkg(dest_root: Path) -> Path:
    """Copy src/brain the way the shipped artifacts are produced: from git's
    file list (tracked + staged/new, never gitignored) — a stamp that exists
    only as an ignored/generated file would be dropped here, exactly as
    export_cleanroom.py / the release commit would drop it."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-c", "-o", "--exclude-standard", "--", "src/brain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "src/brain/_version.py" in out, (
        "src/brain/_version.py is not visible to git — the clean-room export "
        "(git ls-files) would drop it and the shipped VM would report 0.0.0+unknown"
    )
    pkg_root = dest_root / "engine"
    for rel in out:
        src = REPO_ROOT / rel
        dst = pkg_root / Path(rel).relative_to("src")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return pkg_root


def run_from_source(pkg_root: Path, cwd: Path) -> str:
    env = {**os.environ, "PYTHONPATH": str(pkg_root)}
    out = subprocess.run(
        [sys.executable, "-c", NO_METADATA_STUB],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )
    return out.stdout.strip()


def test_from_source_import_reports_real_version(tmp_path):
    pkg_root = export_brain_pkg(tmp_path)
    reported = run_from_source(pkg_root, tmp_path)
    assert reported == pyproject_version()
    assert not reported.startswith("0.0.0"), f"from-source import fell through to the unknown fallback: {reported}"


def test_committed_stamp_matches_pyproject_ssot():
    """Lockstep guard (ADR-0005 Ruling 1): a stale committed stamp is worse
    than 0.0.0+unknown — it reports a confidently wrong version."""
    stamp_text = (REPO_ROOT / "src" / "brain" / "_version.py").read_text(encoding="utf-8")
    m = re.search(r'(?m)^__version__ = "([^"]+)"$', stamp_text)
    assert m, "src/brain/_version.py has no __version__ line"
    assert m.group(1) == pyproject_version()
