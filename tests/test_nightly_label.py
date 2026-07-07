"""Per-vault nightly label — the fix for two host vaults clobbering one shared
launchd job. Source of truth is brain.config.nightly_label; register_tasks.py
(and the shell installers) must agree with it."""
import importlib.util
import pathlib

from brain.config import nightly_label, vault_slug8

REPO = pathlib.Path(__file__).resolve().parent.parent


def _register_tasks():
    spec = importlib.util.spec_from_file_location(
        "register_tasks", REPO / "scripts" / "register_tasks.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_label_is_per_vault_stable_and_formatted(tmp_path):
    a, b = tmp_path / "vaultA", tmp_path / "vaultB"
    a.mkdir()
    b.mkdir()
    la, lb = nightly_label(str(a)), nightly_label(str(b))
    # distinct vaults -> distinct labels (the whole point: no clobber)
    assert la != lb
    # stable for the same vault
    assert la == nightly_label(str(a))
    # format + the 8-hex slug that ties the label to the vault's app-data dir
    assert la.startswith("com.brainiac.nightly.")
    assert la.rsplit(".", 1)[-1] == vault_slug8(str(a))
    assert len(vault_slug8(str(a))) == 8


def test_register_tasks_labels_match_config(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    rt = _register_tasks()
    # macOS launchd label is the single canonical scheme
    assert rt.mac_label(str(v)) == nightly_label(str(v))
    # Windows task name carries the same per-vault slug
    assert rt.win_task_name(str(v)).endswith(vault_slug8(str(v)))
    # no vault -> no clobber-prone shared default silently returned
    assert "<BRAIN_VAULT-unset>" in rt.mac_label(None)
