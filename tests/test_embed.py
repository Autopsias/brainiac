"""DV-03 (2026-07-09): the silent hash-fallback guardrails.

`probe_auto_embedder` classifies which embedder the live runtime would use
(without side effects), and `config.apply_role_embedder_policy` makes the VM
leg fail closed on a dead embedder rather than answering semantic queries with
random hash vectors — the Cowork-VM failure these harden against.
"""
import os

from brain import config, embed


def _no_real_embedder(monkeypatch):
    for cls in (embed.OnnxEmbedder, embed.ArcticEmbedder,
                embed.CatalogEmbedder, embed.QwenEmbedder):
        monkeypatch.setattr(cls, "available", staticmethod(lambda: False))


# -- probe_auto_embedder ----------------------------------------------------

def test_probe_explicit_hash_is_a_choice_not_a_failure(monkeypatch):
    monkeypatch.setenv("BRAIN_EMBEDDER", "hash")
    state, _backend = embed.probe_auto_embedder()
    assert state == "explicit-hash"


def test_probe_real_when_onnx_available(monkeypatch):
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    monkeypatch.delenv("BRAIN_EMBED_MODEL", raising=False)
    monkeypatch.setattr(embed.OnnxEmbedder, "available", staticmethod(lambda: True))
    state, backend = embed.probe_auto_embedder()
    assert state == "real"
    assert backend == "onnx"


def test_probe_implicit_hash_when_nothing_available(monkeypatch):
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    monkeypatch.delenv("BRAIN_EMBED_MODEL", raising=False)
    _no_real_embedder(monkeypatch)
    state, _backend = embed.probe_auto_embedder()
    assert state == "implicit-hash"


def test_probe_has_no_side_effects(monkeypatch, capsys):
    """The read-only doctor must never trigger the stderr fallback warning or
    construct a HashEmbedder just by probing."""
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    monkeypatch.delenv("BRAIN_EMBED_MODEL", raising=False)
    _no_real_embedder(monkeypatch)
    embed.probe_auto_embedder()
    captured = capsys.readouterr()
    assert "FALLING BACK" not in captured.err


# -- config.apply_role_embedder_policy --------------------------------------

def test_vm_leg_defaults_to_fail_closed(monkeypatch):
    monkeypatch.delenv("BRAIN_REQUIRE_REAL_EMBEDDER", raising=False)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    config.apply_role_embedder_policy(config.ROLE_VM)
    assert os.environ.get("BRAIN_REQUIRE_REAL_EMBEDDER") == "1"


def test_vm_leg_respects_explicit_hash_choice(monkeypatch):
    monkeypatch.delenv("BRAIN_REQUIRE_REAL_EMBEDDER", raising=False)
    monkeypatch.setenv("BRAIN_EMBEDDER", "hash")
    config.apply_role_embedder_policy(config.ROLE_VM)
    assert "BRAIN_REQUIRE_REAL_EMBEDDER" not in os.environ


def test_vm_leg_does_not_override_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("BRAIN_REQUIRE_REAL_EMBEDDER", "0")
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    config.apply_role_embedder_policy(config.ROLE_VM)
    assert os.environ["BRAIN_REQUIRE_REAL_EMBEDDER"] == "0"  # setdefault never clobbers


def test_host_leg_is_a_noop(monkeypatch):
    monkeypatch.delenv("BRAIN_REQUIRE_REAL_EMBEDDER", raising=False)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    config.apply_role_embedder_policy(config.ROLE_HOST)
    assert "BRAIN_REQUIRE_REAL_EMBEDDER" not in os.environ
