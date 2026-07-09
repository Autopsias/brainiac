"""DV-04: the shared offline-semantic-deps stager (shim writer + vendor stager).

Network-free — the real per-arch pip download is exercised by the tool's own
`--demo` self-check and by hand during install; here we cover the deterministic
logic (shim contents, present-detection, skip-when-present).
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
import vendor_semantic_deps as vsd  # noqa: E402


def _seed_arch(arch_dir: Path) -> None:
    (arch_dir / "tokenizers").mkdir(parents=True, exist_ok=True)
    (arch_dir / "tokenizers" / "tokenizers.abi3.so").write_bytes(b"")
    (arch_dir / "sqlite_vec").mkdir(parents=True, exist_ok=True)
    (arch_dir / "sqlite_vec" / "vec0.so").write_bytes(b"")


def test_write_shim_is_executable_and_puts_vendor_on_path(tmp_path):
    p = vsd.write_shim(tmp_path)
    assert p == tmp_path / "brain"
    assert p.stat().st_mode & 0o111
    body = p.read_text(encoding="utf-8")
    assert "vendor/$ARCH" in body
    assert "python3 -m brain.cli" in body


def test_has_deps_requires_both_native_libs(tmp_path):
    arch = tmp_path / "vendor" / "aarch64"
    assert not vsd._has_deps(arch)
    (arch / "tokenizers").mkdir(parents=True)
    (arch / "tokenizers" / "tokenizers.abi3.so").write_bytes(b"")
    assert not vsd._has_deps(arch)  # sqlite_vec still missing
    _seed_arch(arch)
    assert vsd._has_deps(arch)


def test_stage_vendor_skips_present_arch_without_downloading(tmp_path, monkeypatch):
    _seed_arch(tmp_path / "vendor" / "aarch64")
    monkeypatch.setattr(vsd, "_download_and_unpack",
                        lambda *a, **k: pytest.fail("must not download an already-present arch"))
    assert vsd.stage_vendor(tmp_path, archs=("aarch64",)) == {"aarch64": "present"}


def test_stage_vendor_reports_failed_when_download_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(vsd, "_download_and_unpack", lambda *a, **k: False)
    assert vsd.stage_vendor(tmp_path, archs=("x86_64",)) == {"x86_64": "failed"}
