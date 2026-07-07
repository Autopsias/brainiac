"""GV-02 (HARDEN:consensus-CRITICAL) — clean-room export VM staging smoke test.

The dev-tree tests (``tests/test_version_stamp.py``) prove a git-ls-files copy
of ``src/brain`` reports the real version. That is necessary but not
sufficient: it never runs ``tools/export_cleanroom.py`` itself, so it cannot
catch a regression in the EXPORT step (an exclude-prefix change, a manifest
bug, a stale COMPAT regeneration) between "the dev tree is fine" and "the
artifact we actually ship is fine".

This test is the one that closes that gap: it runs the REAL exporter to
produce a clean-room export tree, stages a zero-install VM from THAT tree
exactly the way ``tools/cowork_workspace_install.sh`` part (a) does (copy
``src/brain`` into a staging dir, run ``python3 -m brain.cli`` via
``PYTHONPATH`` only — no pip install, no package metadata), and asserts
``brain --version`` matches the exported ``pyproject.toml`` version. This
becomes a release gate in s06 (the next session touching the release
pipeline).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version(pyproject_path: Path) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, f"{pyproject_path}: no version = \"...\" found"
    return m.group(1)


def test_exported_tree_stages_a_zero_install_vm_reporting_the_real_version(tmp_path):
    export_dir = tmp_path / "export"
    stage_dir = tmp_path / "vm-stage"

    # 1. Run the REAL clean-room exporter (git ls-files + exclude prefixes +
    # cowork zip regeneration + the exported-stamp assertion it already runs
    # internally) — never a hand-rolled copy of src/brain.
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "export_cleanroom.py"), "--output", str(export_dir)],
        check=True, cwd=REPO_ROOT, capture_output=True, text=True,
    )
    exported_pyproject = export_dir / "pyproject.toml"
    assert exported_pyproject.exists(), "export must include pyproject.toml"
    exported_version = _pyproject_version(exported_pyproject)

    exported_engine_src = export_dir / "src" / "brain"
    assert exported_engine_src.is_dir(), "export must include src/brain/"
    assert (exported_engine_src / "_version.py").exists(), (
        "export must include the committed version stamp (ADR-0005 Ruling 1) — "
        "export_cleanroom.py's own assert_exported_version_stamp should have caught this first"
    )

    # 2. Stage a zero-install VM from the EXPORTED tree — mirrors
    # cowork_workspace_install.sh part (a) exactly: cp -R src/brain into a
    # staging dir, run via PYTHONPATH only, no pip install, no package
    # metadata. This is the step a dev-tree-only test cannot exercise: it
    # proves the version stamp survives the actual export -> stage path, not
    # just "the source file happens to be correct in the working tree".
    stage_dir.mkdir(parents=True)
    subprocess.run(
        ["cp", "-R", str(exported_engine_src), str(stage_dir / "brain")],
        check=True,
    )

    # 3. `brain --version` from the staged copy, with package metadata denied
    # (the same zero-install posture test_version_stamp.py's NO_METADATA_STUB
    # exercises) — importlib.metadata can never answer for a staged VM copy,
    # so a correct report here can only come from the committed stamp.
    no_metadata_stub = (
        "import importlib.metadata as _md\n"
        "def _deny(name):\n"
        "    raise _md.PackageNotFoundError(name)\n"
        "_md.version = _deny\n"
        "import brain\n"
        "print(brain.__version__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", no_metadata_stub],
        cwd=str(stage_dir),
        env={"PYTHONPATH": str(stage_dir)},
        capture_output=True, text=True, check=True,
    )
    reported_version = result.stdout.strip()

    assert reported_version == exported_version, (
        f"staged zero-install VM reported {reported_version!r}, expected the exported "
        f"pyproject version {exported_version!r} — the clean-room export -> VM-stage path "
        "does not reliably carry the version stamp"
    )
    assert not reported_version.startswith("0.0.0"), (
        f"staged VM fell through to the unknown-version fallback: {reported_version!r}"
    )

    # Also confirm via the ACTUAL CLI entry point (brain.cli's --version flag,
    # not just brain.__version__) — this is what a real VM session runs. Must
    # deny package metadata here too (same as the no_metadata_stub above):
    # a bare `-m brain.cli` subprocess still sees the DEV VENV'S OWN
    # site-packages on sys.path, and if `profile-a-brain` happens to be
    # pip/editable-installed there under a stale version, importlib.metadata
    # resolves that stale dist-info — even though the module actually
    # imported is the staged copy. That is not a real VM (which has no
    # installed dist at all); denying metadata here restores that posture.
    cli_stub = (
        "import importlib.metadata as _md\n"
        "def _deny(name):\n"
        "    raise _md.PackageNotFoundError(name)\n"
        "_md.version = _deny\n"
        "from brain.cli import main\n"
        "raise SystemExit(main(['--version']))\n"
    )
    cli_result = subprocess.run(
        [sys.executable, "-c", cli_stub],
        cwd=str(stage_dir),
        env={"PYTHONPATH": str(stage_dir)},
        capture_output=True, text=True, check=True,
    )
    assert exported_version in cli_result.stdout, (
        f"`brain --version` CLI output {cli_result.stdout!r} does not contain the exported "
        f"version {exported_version!r}"
    )
