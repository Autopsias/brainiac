"""cw-02 — one host command re-stages BOTH the engine and the current-version
skill bundles, with matching version stamps (ADR-0005 Ruling 2 surface 8 +
the new staged-skill-bundle surface).

This is a real end-to-end run of `tools/cowork_workspace_install.sh` against
this checkout (not a mock) — the cheapest test that actually proves the two
artifacts a re-stage lands (`.brain/engine/brain/_version.py` and the
`.brain/skills/*.skill` VERSION marker) agree with each other and with the
pyproject SSOT. Skips gracefully when the host has no model cache staged
(e.g. a fresh CI box that never ran the installer) — mirrors the skip
pattern in tests/test_indexing.py rather than asserting the live machine's
optional prerequisites into existence.
"""
from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "tools" / "cowork_workspace_install.sh"
MODEL_CACHE = REPO_ROOT / "vault" / ".brain" / "model"


def _ssot_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, "pyproject.toml has no version = \"...\""
    return m.group(1)


@pytest.mark.skipif(
    not (MODEL_CACHE / "tokenizer.json").exists(),
    reason="no bundled model cache staged at vault/.brain/model — run install.sh first",
)
def test_restage_lands_matching_version_stamp_on_engine_and_skills(tmp_path):
    workspace_vault = tmp_path / "cowork-ws" / "vault"
    (workspace_vault / "brain").mkdir(parents=True)
    (workspace_vault / "raw").mkdir(parents=True)
    (workspace_vault / "brain" / "hello.md").write_text(
        "---\ntitle: Hello\ntags: [test]\n---\n\n# Hello\n\nMinimal fixture note.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), str(workspace_vault), str(MODEL_CACHE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"cowork_workspace_install.sh failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    ssot = _ssot_version()

    # Engine stamp.
    stamp_path = workspace_vault / ".brain" / "engine" / "brain" / "_version.py"
    assert stamp_path.exists(), "restage did not land an engine version stamp"
    m = re.search(r'(?m)^__version__ = "([^"]+)"$', stamp_path.read_text(encoding="utf-8"))
    assert m, f"{stamp_path}: no __version__ line"
    engine_version = m.group(1)
    assert engine_version == ssot, f"staged engine {engine_version} != SSOT {ssot}"

    # Skill bundle VERSION marker — one host command must refresh both
    # (not just the engine) in the same pass.
    skills_dir = workspace_vault / ".brain" / "skills"
    zips = sorted(skills_dir.glob("*.skill"))
    assert zips, f"no .skill bundles staged in {skills_dir}"
    with zipfile.ZipFile(zips[0]) as zf:
        version_member = f"{zips[0].stem}/VERSION"
        assert version_member in zf.namelist(), f"{zips[0].name} missing VERSION marker"
        skill_version = zf.read(version_member).decode("utf-8").strip()
    assert skill_version == ssot, f"staged skill bundle {skill_version} != SSOT {ssot}"
    assert skill_version == engine_version, "engine and skill bundle version stamps disagree"

    # brain doctor (dv-02 + cw-02) must report both staged surfaces as CURRENT
    # for this workspace, proving the doctor-visibility half of cw-02 too.
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from brain import doctor

    entries = [{"target": "cowork-vm", "workspace_path": str(workspace_vault)}]
    ws_rows = doctor.check_staged_workspaces(entries, ssot)
    skill_rows = doctor.check_staged_skill_bundles(entries, ssot)
    assert ws_rows[0]["status"] == doctor.CURRENT
    assert skill_rows[0]["status"] == doctor.CURRENT
