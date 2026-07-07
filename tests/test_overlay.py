"""PER-01/PER-02 — the generic personalization overlay + `brain init
--validate-overlay`.

`brain.overlay` is filesystem-only by design (no BrainCore, no index) so the
minimal `brain init --validate-overlay` slice works on a brand-new install
before any index exists. These tests exercise the validator directly AND
through the CLI (which must dispatch `init` BEFORE constructing BrainCore).
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from brain import cli, overlay as ov

CATEGORIES = ("voice", "brand", "keywords", "people")


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _json(argv) -> tuple[int, dict]:
    code, out = _run(argv)
    return code, json.loads(out)


def _write_overlay_file(root: Path, category: str, name: str = "x.md",
                        overlay_type: str | None = None, body: str = "some content\n") -> Path:
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    declared = category if overlay_type is None else overlay_type
    text = f"---\noverlay_type: {declared}\ntitle: \"x\"\n---\n\n{body}"
    p = cat_dir / name
    p.write_text(text, encoding="utf-8")
    return p


def _filled_overlay(root: Path) -> Path:
    for cat in CATEGORIES:
        _write_overlay_file(root, cat)
    return root


# -- the repo's own shipped template + worked example are real fixtures -----

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repo_template_validates_clean():
    report = ov.validate_overlay(REPO_ROOT / "overlay" / "template")
    assert report["valid"] is True, report["errors"]
    for cat in CATEGORIES:
        assert report["categories"][cat]["present"]
        assert report["categories"][cat]["file_count"] >= 1


# -- overlay_dir() resolution precedence -------------------------------------

def test_overlay_dir_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_OVERLAY_DIR", str(tmp_path / "env-overlay"))
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path / "vault"))
    resolved = ov.overlay_dir(explicit=str(tmp_path / "explicit-overlay"))
    assert resolved == (tmp_path / "explicit-overlay").resolve()


def test_overlay_dir_env_wins_over_vault_default(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_OVERLAY_DIR", str(tmp_path / "env-overlay"))
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path / "vault"))
    resolved = ov.overlay_dir()
    assert resolved == (tmp_path / "env-overlay").resolve()


def test_overlay_dir_defaults_under_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_OVERLAY_DIR", raising=False)
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path / "vault"))
    resolved = ov.overlay_dir()
    assert resolved == (tmp_path / "vault" / "overlay").resolve()


# -- validate_overlay() shape checks -----------------------------------------

def test_missing_overlay_dir_is_invalid(tmp_path):
    report = ov.validate_overlay(tmp_path / "nope")
    assert report["exists"] is False
    assert report["valid"] is False
    for cat in CATEGORIES:
        assert report["categories"][cat]["present"] is False


def test_fully_filled_overlay_is_valid(tmp_path):
    root = _filled_overlay(tmp_path / "overlay")
    report = ov.validate_overlay(root)
    assert report["valid"] is True
    assert report["errors"] == []
    for cat in CATEGORIES:
        assert report["categories"][cat]["file_count"] == 1


def test_missing_one_category_is_invalid(tmp_path):
    root = tmp_path / "overlay"
    for cat in ("voice", "brand", "keywords"):  # people missing
        _write_overlay_file(root, cat)
    report = ov.validate_overlay(root)
    assert report["valid"] is False
    assert report["categories"]["people"]["present"] is False
    assert any("people" in e for e in report["errors"])


def test_empty_category_dir_is_invalid(tmp_path):
    root = _filled_overlay(tmp_path / "overlay")
    (root / "people").glob("*.md")
    for f in (root / "people").glob("*.md"):
        f.unlink()
    report = ov.validate_overlay(root)
    assert report["valid"] is False
    assert report["categories"]["people"]["present"] is True
    assert report["categories"]["people"]["file_count"] == 0
    assert any("no .md files" in e for e in report["errors"])


def test_wrong_overlay_type_is_invalid(tmp_path):
    root = _filled_overlay(tmp_path / "overlay")
    _write_overlay_file(root, "voice", name="bad.md", overlay_type="brand")
    report = ov.validate_overlay(root)
    assert report["valid"] is False
    assert any("overlay_type" in e for e in report["errors"])


def test_missing_frontmatter_is_invalid(tmp_path):
    root = _filled_overlay(tmp_path / "overlay")
    (root / "voice" / "no-frontmatter.md").write_text("just prose, no frontmatter\n")
    report = ov.validate_overlay(root)
    assert report["valid"] is False
    assert any("frontmatter" in e for e in report["errors"])


def test_empty_body_is_invalid(tmp_path):
    root = tmp_path / "overlay"
    for cat in CATEGORIES:
        _write_overlay_file(root, cat, body="")
    report = ov.validate_overlay(root)
    assert report["valid"] is False
    assert any("empty" in e for e in report["errors"])


# -- CLI: `brain init --validate-overlay` ------------------------------------

def test_cli_init_requires_validate_overlay_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path / "vault"))
    code, out = _run(["init"])
    assert code == 2
    assert "validate-overlay" in out


def test_cli_init_validate_overlay_json_pass(tmp_path):
    root = _filled_overlay(tmp_path / "overlay")
    code, payload = _json(["init", "--validate-overlay", "--overlay-dir", str(root), "--json"])
    assert code == 0
    assert payload["valid"] is True


def test_cli_init_validate_overlay_json_fail(tmp_path):
    code, payload = _json(
        ["init", "--validate-overlay", "--overlay-dir", str(tmp_path / "missing"), "--json"])
    assert code == 1
    assert payload["valid"] is False


def test_cli_init_validate_overlay_human_output(tmp_path):
    root = _filled_overlay(tmp_path / "overlay")
    code, out = _run(["init", "--validate-overlay", "--overlay-dir", str(root)])
    assert code == 0
    assert "valid: True" in out
    for cat in CATEGORIES:
        assert f"{cat}/: ok" in out


def test_cli_init_never_constructs_braincore(tmp_path, monkeypatch):
    """`init` must not need a real vault/index — it is filesystem-only and has
    to work before any index exists (PER-02's "first run" requirement)."""
    def _boom(*a, **kw):  # pragma: no cover - should never be called
        raise AssertionError("BrainCore must not be constructed for `init`")

    monkeypatch.setattr(cli, "BrainCore", _boom)
    root = _filled_overlay(tmp_path / "overlay")
    code, _ = _run(["init", "--validate-overlay", "--overlay-dir", str(root)])
    assert code == 0


def test_cli_init_allowed_on_vm_role(tmp_path):
    """`init` is filesystem-only and read-only — it should not be refused on
    the read+draft-only VM leg (unlike the host-broker maintenance rituals)."""
    root = _filled_overlay(tmp_path / "overlay")
    code, payload = _json(
        ["--role", "vm", "init", "--validate-overlay", "--overlay-dir", str(root), "--json"])
    assert code == 0
    assert payload["valid"] is True


def test_cli_init_overlay_dir_default_resolves_under_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _filled_overlay(vault / "overlay")
    monkeypatch.setenv("BRAIN_VAULT", str(vault))
    code, payload = _json(["init", "--validate-overlay", "--json"])
    assert code == 0
    assert payload["valid"] is True
    assert payload["overlay_dir"] == str((vault / "overlay").resolve())
