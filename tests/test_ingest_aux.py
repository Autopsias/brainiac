"""S06 (ING-03) — auxiliary handlers: image, .eml, .html, .zip.

Covers: happy path per handler, quarantine (missing dep / corrupt / empty),
zip bounded-recursion (member/eml-attachment re-entry into the dispatcher),
zip-bomb guard (declared-size cap + streaming decompressed-size cap), and
Zip-Slip hardening (path traversal / absolute path / symlink members).

Offline + deterministic, mirrors tests/test_ingest.py's own conventions
(HashEmbedder + BruteForceBackend, env-injected audit key).
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from brain.core import BrainCore
from brain.embed import HashEmbedder
from brain.index import BrainIndex
from brain.vectors import BruteForceBackend

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _mini_vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "raw").mkdir(parents=True)
    (v / "inbox").mkdir(parents=True)
    return v


def _host_core(tmp_path: Path, vault: Path) -> BrainCore:
    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend(),
                      embedder=HashEmbedder())
    idx.rebuild(vault)
    return BrainCore(vault=vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="host")


def _make_png(path: Path, *, size=(64, 64), color=(200, 30, 30)) -> None:
    img = Image.new("RGB", size, color=color)
    img.save(path, format="PNG")


LOREM = ("This is a genuinely long paragraph of real prose used as fixture "
          "content so the extraction-quality density gate passes easily. ") * 3


def _make_eml(path: Path, *, subject="Fixture subject", body=LOREM,
              attachments: list[tuple[str, bytes]] | None = None) -> None:
    import email.message

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>"
    msg["Date"] = "Sun, 05 Jul 2026 10:00:00 +0000"
    msg.set_content(body)
    for name, data in attachments or []:
        maintype = "application"
        subtype = "octet-stream"
        if name.endswith(".txt"):
            maintype, subtype = "text", "plain"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    path.write_bytes(bytes(msg))


def _make_html(path: Path, *, title="Fixture Page", body=LOREM) -> None:
    path.write_text(
        f"<html><head><title>{title}</title></head><body><p>{body}</p></body></html>",
        encoding="utf-8",
    )


def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_image_ingest_metadata_only(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        _make_png(vault / "inbox" / "screenshot.png")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert len(res["processed"]) == 1, res
        note = (vault / res["processed"][0]["note"]).read_text(encoding="utf-8")
        assert "## OCR (verbatim)" in note
        assert "## Image metadata" in note
        assert "PNG" in note
        assert "64 x 64" in note
        assert not (vault / "inbox" / "screenshot.png").exists()

    def test_eml_ingest_headers_body_and_attachment_reenters_dispatcher(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        _make_eml(
            vault / "inbox" / "message.eml",
            attachments=[("notes.txt", LOREM.encode("utf-8"))],
        )
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert len(res["processed"]) == 2, res  # the .eml itself + its .txt attachment
        eml_entry = next(p for p in res["processed"] if p["file"] == "message.eml")
        note = (vault / eml_entry["note"]).read_text(encoding="utf-8")
        assert "Fixture subject" in note
        assert "## Attachments" in note
        assert "notes.txt" in note
        att_entry = next(p for p in res["processed"] if p["file"] == "notes.txt")
        assert att_entry.get("parent") == eml_entry["id"]
        att_note = (vault / att_entry["note"]).read_text(encoding="utf-8")
        assert "genuinely long paragraph" in att_note

    def test_html_ingest_extracts_title_and_readable_text(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        _make_html(vault / "inbox" / "page.html")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert len(res["processed"]) == 1, res
        note = (vault / res["processed"][0]["note"]).read_text(encoding="utf-8")
        assert "Fixture Page" in note
        assert "genuinely long paragraph" in note

    def test_zip_ingest_expands_members_into_the_dispatcher(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        _make_zip(vault / "inbox" / "bundle.zip", {
            "notes.txt": LOREM.encode("utf-8"),
            "more/readme.txt": (LOREM + " second file.").encode("utf-8"),
        })
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        # the zip's own manifest note + 2 member notes
        assert len(res["processed"]) == 3, res
        zip_entry = next(p for p in res["processed"] if p["file"] == "bundle.zip")
        note = (vault / zip_entry["note"]).read_text(encoding="utf-8")
        assert "## Archive contents" in note
        assert "notes.txt" in note and "readme.txt" in note
        member_entries = [p for p in res["processed"] if p.get("parent") == zip_entry["id"]]
        assert len(member_entries) == 2
        # Zip-Slip: the "more/" directory component of "more/readme.txt"
        # must never have been used as a filesystem path — the archived
        # original's basename is the sanitized member basename only, and its
        # containing dir is the synthetic per-ingest archive subdir, not a
        # reconstruction of the zip's internal directory structure.
        readme_entry = next(e for e in member_entries if "readme" in e["archived"])
        assert Path(readme_entry["archived"]).name.endswith("readme.txt")
        assert "more" not in Path(readme_entry["archived"]).parts


# ---------------------------------------------------------------------------
# quarantine
# ---------------------------------------------------------------------------

class TestQuarantine:
    def test_missing_pillow_quarantines(self, tmp_path, audit_key_env, monkeypatch):
        from brain.ingest.handlers import image as image_mod

        monkeypatch.setattr(image_mod, "_HAS_PIL", False)
        vault = _mini_vault(tmp_path)
        (vault / "inbox" / "pic.png").write_bytes(b"not a real png")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "missing_dependency:Pillow"

    def test_corrupt_image_quarantines_not_crashes(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        (vault / "inbox" / "pic.png").write_bytes(b"not a real png at all")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()  # must not raise
        assert res["quarantined"][0]["reason"] == "image_open_error"

    def test_malformed_eml_bytes_never_crash(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        (vault / "inbox" / "broken.eml").write_bytes(b"\x00\x01\x02not an eml")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()  # must not raise, whichever way it lands
        assert res["processed"] or res["quarantined"]
        if res["processed"]:
            # stdlib's email parser is extremely lenient (rarely raises) — a
            # non-email blob still produces a well-formed note (headers
            # honestly empty, the garbage bytes surface verbatim as the
            # "body") rather than crashing or fabricating content.
            note = (vault / res["processed"][0]["note"]).read_text(encoding="utf-8")
            assert "(no subject)" in note
            assert "## Body" in note

    def test_empty_html_quarantines(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        (vault / "inbox" / "blank.html").write_text("<html><body></body></html>", encoding="utf-8")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["processed"] == []
        assert res["quarantined"][0]["reason"] in ("empty_or_low_text_density",)

    def test_corrupt_zip_quarantines_not_crashes(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        (vault / "inbox" / "broken.zip").write_bytes(b"PK\x03\x04not a real zip")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()  # must not raise
        assert res["quarantined"][0]["reason"] == "zip_corrupt"

    def test_empty_zip_quarantines(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        with zipfile.ZipFile(vault / "inbox" / "empty.zip", "w"):
            pass
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "zip_empty"

    def test_unknown_extension_still_quarantines(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        (vault / "inbox" / "mystery.xyz").write_bytes(b"unknown format")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "no_handler_for_extension"


# ---------------------------------------------------------------------------
# zip bounds + Zip-Slip hardening (HARDENED:grill / codex-verify-r2)
# ---------------------------------------------------------------------------

class TestZipBombAndSlip:
    def test_zip_member_too_large_quarantines(self, tmp_path, audit_key_env, monkeypatch):
        from brain.ingest.handlers import zip as zip_mod

        monkeypatch.setattr(zip_mod, "MAX_MEMBER_BYTES", 10)
        vault = _mini_vault(tmp_path)
        _make_zip(vault / "inbox" / "big.zip", {"big.txt": b"x" * 1000})
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "zip_member_too_large"

    def test_zip_declared_total_cap_quarantines(self, tmp_path, audit_key_env, monkeypatch):
        from brain.ingest.handlers import zip as zip_mod

        monkeypatch.setattr(zip_mod, "MAX_TOTAL_DECLARED_BYTES", 100)
        vault = _mini_vault(tmp_path)
        _make_zip(vault / "inbox" / "many.zip", {
            "a.txt": b"x" * 60, "b.txt": b"y" * 60,
        })
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "zip_bomb_suspected"

    def test_streaming_reader_aborts_when_actual_bytes_exceed_cap(self, tmp_path):
        """Unit-level defense-in-depth: ``_read_member_bounded`` must abort as
        soon as the REAL decompressed byte count exceeds ``cap``, without
        ever fully materializing the member — this backstops the pre-scan
        declared-size check rather than trusting it alone (the "before AND
        during decompression" requirement)."""
        from brain.ingest.handlers.zip import _read_member_bounded

        payload = b"z" * 5000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("member.bin", payload)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            info = zf.infolist()[0]
            result = _read_member_bounded(zf, info, cap=100)
        assert result is None, "streaming read must abort once actual bytes exceed the cap"

    def test_too_many_members_quarantines(self, tmp_path, audit_key_env, monkeypatch):
        from brain.ingest.handlers import zip as zip_mod

        monkeypatch.setattr(zip_mod, "MAX_MEMBERS", 2)
        vault = _mini_vault(tmp_path)
        _make_zip(vault / "inbox" / "many.zip", {
            "a.txt": b"a", "b.txt": b"b", "c.txt": b"c",
        })
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "zip_too_many_members"

    def test_path_traversal_member_quarantines_whole_archive(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        path = vault / "inbox" / "traversal.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("good.txt", LOREM)
            zf.writestr("../../etc/evil.txt", "pwned")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["processed"] == []
        assert res["quarantined"][0]["reason"] == "zip_unsafe_member"
        assert not (tmp_path / "etc").exists(), "a traversal member must never write outside the claim area"

    def test_absolute_path_member_quarantines_whole_archive(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        path = vault / "inbox" / "abspath.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("/etc/evil.txt", "pwned")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "zip_unsafe_member"

    def test_symlink_member_quarantines_whole_archive(self, tmp_path, audit_key_env):
        vault = _mini_vault(tmp_path)
        path = vault / "inbox" / "symlink.zip"
        with zipfile.ZipFile(path, "w") as zf:
            info = zipfile.ZipInfo("link")
            info.create_system = 3  # unix
            info.external_attr = (0xA000 | 0o777) << 16  # S_IFLNK
            zf.writestr(info, "/etc/passwd")
        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        assert res["quarantined"][0]["reason"] == "zip_unsafe_member"

    def test_nested_recursion_depth_bound(self, tmp_path, audit_key_env, monkeypatch):
        from brain.ingest import pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "MAX_NESTED_DEPTH", 1)
        vault = _mini_vault(tmp_path)

        # zip-in-zip-in-zip: depth 0 (top-level) -> 1 (first nested) -> 2 (blocked)
        inner_inner = io.BytesIO()
        with zipfile.ZipFile(inner_inner, "w") as zf:
            zf.writestr("leaf.txt", LOREM)
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("inner_inner.zip", inner_inner.getvalue())
        _make_zip(vault / "inbox" / "outer.zip", {"inner.zip": inner.getvalue()})

        core = _host_core(tmp_path, vault)
        res = core.ingest_dropzone()
        # outer.zip promotes, inner.zip (depth 1) promotes, inner_inner.zip's
        # own member (depth 2) is blocked by the depth bound.
        assert any(p["file"] == "outer.zip" for p in res["processed"])
        assert any(item.get("reason") == "nested_depth_exceeded"
                   for item in res.get("skipped", []))
