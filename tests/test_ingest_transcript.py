"""S06 (ING-04) — transcript capture route: provenance stamping (origin +
captured date + optional language), dedup, quarantine, and VM refusal."""
from __future__ import annotations

from pathlib import Path

from brain.core import BrainCore, RoleError
from brain.embed import HashEmbedder
from brain.index import BrainIndex
from brain.vectors import BruteForceBackend

LOREM = ("This is a genuinely long paragraph of real transcript prose used "
         "as fixture content so the density gate passes easily. ") * 3


def _mini_vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "raw").mkdir(parents=True)
    return v


def _core(tmp_path: Path, vault: Path, *, role: str = "host") -> BrainCore:
    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend(),
                      embedder=HashEmbedder())
    idx.rebuild(vault)
    return BrainCore(vault=vault, index=idx, audit_log=tmp_path / "audit.jsonl", role=role)


class TestTranscriptRoute:
    def test_verbal_origin_stamped(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "standup_2026-07-05.md"
        transcript.write_text(f"# Standup\n\n{LOREM}\n", encoding="utf-8")
        core = _core(tmp_path, vault)

        res = core.ingest_transcript(transcript, origin="verbal")
        assert res["ok"] is True
        assert res["duplicate"] is False
        note = (vault / res["note"]).read_text(encoding="utf-8")
        assert "origin: verbal" in note
        assert "type: source" in note
        assert "classification: Internal" in note
        assert f"captured: " in note
        archived = vault / res["archived"]
        assert archived.is_file()

    def test_audio_path_origin_and_filename_language_detected(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "meeting_2026-07-05_en.md"
        transcript.write_text(f"# Meeting\n\n{LOREM}\n", encoding="utf-8")
        core = _core(tmp_path, vault)

        res = core.ingest_transcript(
            transcript, origin="/Users/example/Recordings/meeting.m4a",
        )
        assert res["ok"] is True
        assert res["language"] == "en"
        note = (vault / res["note"]).read_text(encoding="utf-8")
        assert "language: en" in note
        assert "Recordings/meeting.m4a" in note

    def test_explicit_language_overrides_filename_detection(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "meeting_en.md"
        transcript.write_text(f"# Meeting\n\n{LOREM}\n", encoding="utf-8")
        core = _core(tmp_path, vault)

        res = core.ingest_transcript(transcript, origin="verbal", language="pt")
        assert res["language"] == "pt"

    def test_no_language_in_filename_omits_field(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "random-file-name.md"
        transcript.write_text(f"# Meeting\n\n{LOREM}\n", encoding="utf-8")
        core = _core(tmp_path, vault)

        res = core.ingest_transcript(transcript, origin="verbal")
        assert res.get("language") is None
        note = (vault / res["note"]).read_text(encoding="utf-8")
        assert "language:" not in note

    def test_duplicate_content_is_idempotent(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "standup.md"
        transcript.write_text(f"# Standup\n\n{LOREM}\n", encoding="utf-8")
        core = _core(tmp_path, vault)

        res1 = core.ingest_transcript(transcript, origin="verbal")
        assert res1["ok"] and not res1["duplicate"]

        # Same bytes, different filename -> dedup by content sha256.
        transcript2 = tmp_path / "standup-copy.md"
        transcript2.write_bytes(transcript.read_bytes())
        res2 = core.ingest_transcript(transcript2, origin="verbal")
        assert res2["ok"] is True
        assert res2["duplicate"] is True
        assert res2["existing_id"] == res1["id"]

    def test_empty_transcript_quarantines_via_density_gate(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "empty.md"
        transcript.write_text("hi", encoding="utf-8")
        core = _core(tmp_path, vault)

        res = core.ingest_transcript(transcript, origin="verbal")
        assert res["ok"] is False
        assert res["reason"] == "empty_or_low_text_density"
        assert not any((vault / "raw").glob("*.md"))

    def test_missing_file_reports_error_not_raise(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        core = _core(tmp_path, vault)
        res = core.ingest_transcript(tmp_path / "does-not-exist.md", origin="verbal")
        assert res["ok"] is False
        assert "transcript_read_error" in res["reason"]

    def test_hostile_origin_control_char_never_corrupts_frontmatter(self, tmp_path, audit_key_env):
        """A caller-supplied `origin` is untrusted input (may come from an
        agent/skill) — an embedded newline must never inject a bogus line
        into the signed frontmatter (S06 HARDENED, generalises the S05
        filename-control-char lesson to any string flowing into frontmatter)."""
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "hostile.md"
        transcript.write_text(f"# Hostile\n\n{LOREM}\n", encoding="utf-8")
        core = _core(tmp_path, vault)

        hostile_origin = "verbal\ninjected_key: evil"
        res = core.ingest_transcript(transcript, origin=hostile_origin)
        assert res["ok"] is True
        note = (vault / res["note"]).read_text(encoding="utf-8")

        from brain import frontmatter as fm

        meta, _ = fm.parse_text(note)
        # The real security property: the embedded newline must never have
        # forged a NEW top-level YAML key — "injected_key" as its own parsed
        # frontmatter field, not merely as a substring inside origin's value.
        assert "injected_key" not in meta
        assert meta["id"] == res["id"]

    def test_vm_role_refused_before_any_side_effect(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        transcript = tmp_path / "standup.md"
        transcript.write_text(f"# Standup\n\n{LOREM}\n", encoding="utf-8")
        core = _core(tmp_path, vault, role="vm")

        try:
            core.ingest_transcript(transcript, origin="verbal")
            assert False, "VM role must be refused"
        except RoleError:
            pass
        assert not any((vault / "raw").glob("*.md"))
        assert not (vault / "raw" / "originals").exists()
