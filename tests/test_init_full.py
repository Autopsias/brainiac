"""INS-02 — `brain init --full` full install orchestration.

`brain.init` is filesystem + subprocess only (no BrainCore, no index) so
`brain init --full` works on a brand-new install. These tests exercise the
orchestration module directly AND through the CLI. The host leg runs only its
read-only probe (apply=False) — no launchd/Task Scheduler mutation.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from brain import cli, config
from brain import init as brain_init
from brain import overlay as ov

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "overlay" / "template"
CATEGORIES = ("voice", "brand", "keywords", "people")


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _json(argv) -> tuple[int, dict]:
    code, out = _run(argv)
    return code, json.loads(out)


# -- discovery helpers -------------------------------------------------------

def test_discover_repo_root_finds_this_repo():
    assert brain_init.discover_repo_root() == REPO_ROOT


def test_discover_repo_root_env_override(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    monkeypatch.setenv("BRAIN_REPO_ROOT", str(tmp_path))
    assert brain_init.discover_repo_root() == tmp_path.resolve()


def test_resolve_template_dir_defaults_to_repo():
    assert brain_init.resolve_template_dir(None, REPO_ROOT) == TEMPLATE_DIR


def test_resolve_template_dir_none_when_no_repo():
    assert brain_init.resolve_template_dir(None, None) is None


def test_resolve_manifest_repo_default(tmp_path, monkeypatch):
    # Isolate from the developer machine: with vault=None the "installed"
    # candidate resolves via cwd/$BRAIN_VAULT, and a live checkout may carry a
    # real vault/.brain/routines/manifest.json that would (correctly) win.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BRAIN_VAULT", raising=False)
    monkeypatch.delenv("BRAIN_ROUTINES_MANIFEST", raising=False)
    p = brain_init.resolve_manifest_path(None, REPO_ROOT, None)
    assert p == REPO_ROOT / "routines" / "manifest.json"


def test_resolve_manifest_prefers_installed_over_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_ROUTINES_MANIFEST", raising=False)
    vault = tmp_path / "vault"
    installed = vault / ".brain" / "routines" / "manifest.json"
    installed.parent.mkdir(parents=True)
    installed.write_text("{}", encoding="utf-8")
    assert brain_init.resolve_manifest_path(None, REPO_ROOT, str(vault)) == installed


def test_detect_client_maps_role():
    assert brain_init.detect_client(config.ROLE_HOST) == "host"
    assert brain_init.detect_client(config.ROLE_VM) == "cowork"


# -- scaffold_overlay --------------------------------------------------------

def test_scaffold_fills_empty_overlay(tmp_path):
    ol = tmp_path / "overlay"
    rep = brain_init.scaffold_overlay(ol, TEMPLATE_DIR)
    assert rep["performed"] is True
    assert len(rep["created"]) == 4
    report = ov.validate_overlay(ol)
    assert report["valid"] is True


def test_scaffold_is_idempotent(tmp_path):
    ol = tmp_path / "overlay"
    brain_init.scaffold_overlay(ol, TEMPLATE_DIR)
    rep2 = brain_init.scaffold_overlay(ol, TEMPLATE_DIR)
    assert rep2["created"] == []
    assert sorted(rep2["skipped"]) == sorted(CATEGORIES)


def test_scaffold_no_template_is_not_an_error(tmp_path):
    rep = brain_init.scaffold_overlay(tmp_path / "overlay", None)
    assert rep["performed"] is False
    assert rep["created"] == []


def test_scaffold_preserves_a_filled_category(tmp_path):
    ol = tmp_path / "overlay"
    voice = ol / "voice"
    voice.mkdir(parents=True)
    (voice / "mine.md").write_text(
        "---\noverlay_type: voice\n---\n\nmy own voice\n", encoding="utf-8")
    rep = brain_init.scaffold_overlay(ol, TEMPLATE_DIR)
    assert "voice" in rep["skipped"]
    assert (voice / "mine.md").exists()  # untouched
    assert not (voice / "voice-profile.md").exists()  # not clobbered/added


# -- run_full_init: host -----------------------------------------------------

def test_full_init_host(tmp_path):
    vault = tmp_path / "vault"
    rep = brain_init.run_full_init(
        vault=str(vault), overlay_dir=None, role=config.ROLE_HOST,
        template_dir=str(TEMPLATE_DIR))
    assert rep["client"] == "host"
    assert rep["ok"] is True
    assert rep["overlay"]["validation"]["valid"] is True
    assert rep["tasks"]["registrar"] == "available"
    assert rep["tasks"]["host"]["task_id"] == "brain-nightly"
    # dry-run default: no OS mutation was applied
    assert rep["tasks"]["apply"] is False


# -- run_full_init: cowork/VM ------------------------------------------------

def test_full_init_cowork_prints_paste_prompt(tmp_path):
    vault = tmp_path / "vault"
    save = tmp_path / "prompt.md"
    rep = brain_init.run_full_init(
        vault=str(vault), overlay_dir=None, role=config.ROLE_VM,
        template_dir=str(TEMPLATE_DIR), save_cowork_prompt=str(save))
    assert rep["client"] == "cowork"
    assert rep["ok"] is True
    cw = rep["tasks"]["cowork"]
    # AUT-04/s11: autoresearch-cascade moved to runtime=host, vm_eligible=false
    # (it needs the dev repo's eval/+src/, not something a Cowork VM session
    # can run — routines/manifest.json command_notes).
    assert set(cw["vm_eligible_tasks"]) == {
        "promotion-scan", "ingestion-digest-weekly"}
    assert save.exists() and save.read_text(encoding="utf-8").strip()
    assert cw["saved_to"] == str(save)


# -- run_full_init: degraded / disabled paths --------------------------------

def test_full_init_register_disabled(tmp_path):
    rep = brain_init.run_full_init(
        vault=str(tmp_path / "vault"), overlay_dir=None, role=config.ROLE_HOST,
        template_dir=str(TEMPLATE_DIR), register_tasks=False)
    assert rep["tasks"]["registrar"] == "disabled"
    assert rep["ok"] is True


def test_full_init_registrar_unavailable_is_soft(tmp_path, monkeypatch):
    """A bundled binary far from the repo has no scripts/ — registrar
    unavailable must NOT flip the whole install to not-ok; the overlay still
    installs and a hint is surfaced."""
    monkeypatch.setattr(brain_init, "load_registrar", lambda root: None)
    rep = brain_init.run_full_init(
        vault=str(tmp_path / "vault"), overlay_dir=None, role=config.ROLE_HOST,
        template_dir=str(TEMPLATE_DIR),
        manifest=str(REPO_ROOT / "routines" / "manifest.json"))
    assert rep["tasks"]["registrar"] == "unavailable"
    assert rep["tasks"]["hint"]
    assert rep["ok"] is True  # overlay valid => install still ok


def test_full_init_no_scaffold_on_empty_is_invalid(tmp_path):
    rep = brain_init.run_full_init(
        vault=str(tmp_path / "vault"), overlay_dir=None, role=config.ROLE_HOST,
        scaffold=False, register_tasks=False)
    assert rep["overlay"]["validation"]["valid"] is False
    assert rep["ok"] is False


# -- CLI: `brain init --full` ------------------------------------------------

def test_cli_init_bare_offers_both_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path / "vault"))
    code, out = _run(["init"])
    assert code == 2
    assert "validate-overlay" in out
    assert "--full" in out


def test_cli_init_full_host_json(tmp_path):
    vault = tmp_path / "vault"
    code, payload = _json(
        ["--vault", str(vault), "init", "--full",
         "--template-dir", str(TEMPLATE_DIR), "--json"])
    assert code == 0
    assert payload["ok"] is True
    assert payload["client"] == "host"


def test_cli_init_full_vm_json(tmp_path):
    vault = tmp_path / "vault"
    code, payload = _json(
        ["--role", "vm", "--vault", str(vault), "init", "--full",
         "--template-dir", str(TEMPLATE_DIR), "--json"])
    assert code == 0
    assert payload["client"] == "cowork"
    assert "cowork" in payload["tasks"]


def test_cli_init_full_never_constructs_braincore(tmp_path, monkeypatch):
    def _boom(*a, **kw):  # pragma: no cover - should never be called
        raise AssertionError("BrainCore must not be constructed for `init --full`")

    monkeypatch.setattr(cli, "BrainCore", _boom)
    code, _ = _run(["--vault", str(tmp_path / "vault"), "init", "--full",
                    "--template-dir", str(TEMPLATE_DIR)])
    assert code == 0


def test_cli_init_full_human_output(tmp_path):
    code, out = _run(["--vault", str(tmp_path / "vault"), "init", "--full",
                      "--template-dir", str(TEMPLATE_DIR)])
    assert code == 0
    assert "brain init (full)" in out
    assert "client=host" in out
