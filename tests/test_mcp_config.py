"""`brain mcp-config` — prints the MCP-client entry to run brain-mcp against a
vault. Pure string generation (no index/key), so it works on a bare vault dir.
"""
import json

from brain import cli


def test_mcp_config_json_emits_valid_entry(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path))
    rc = cli.main(["mcp-config", "--name", "myvault", "--max-tier", "Restricted", "--json"])
    assert rc == 0
    entry = json.loads(capsys.readouterr().out)
    assert list(entry) == ["myvault"]
    cfg = entry["myvault"]
    assert cfg["command"].endswith("brain-mcp")
    assert cfg["env"]["BRAIN_MAX_EGRESS_TIER"] == "Restricted"
    assert cfg["env"]["BRAIN_VAULT"].endswith(tmp_path.name)


def test_mcp_config_omits_model_cache_when_unstaged(tmp_path, capsys, monkeypatch):
    # A bare vault (no .brain/model) must not emit a BRAIN_MODEL_CACHE pointing
    # at a nonexistent dir — the host resolves its own model cache.
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path))
    cli.main(["mcp-config", "--json"])
    cfg = json.loads(capsys.readouterr().out)["brainiac"]
    assert "BRAIN_MODEL_CACHE" not in cfg["env"]


def test_mcp_config_includes_model_cache_when_staged(tmp_path, capsys, monkeypatch):
    (tmp_path / ".brain" / "model").mkdir(parents=True)
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path))
    cli.main(["mcp-config", "--json"])
    cfg = json.loads(capsys.readouterr().out)["brainiac"]
    assert cfg["env"]["BRAIN_MODEL_CACHE"].endswith("/.brain/model")


def test_mcp_config_allowed_on_vm_role(tmp_path, capsys, monkeypatch):
    # It's informational — must not be refused by the VM trust gate.
    monkeypatch.setenv("BRAIN_VAULT", str(tmp_path))
    rc = cli.main(["--role", "vm", "mcp-config", "--json"])
    assert rc == 0
    assert "brainiac" in json.loads(capsys.readouterr().out)
