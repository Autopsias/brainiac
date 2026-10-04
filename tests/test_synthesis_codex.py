import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
import os
import sys
import contextlib
import io
import subprocess
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import brain_synthesis_codex as ws
from brain import frontmatter


class SynthesisTests(unittest.TestCase):
    def test_executable_entrypoint(self):
        result = subprocess.run([sys.executable, str(Path(ws.__file__)), "--help"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--allow-cloud-synthesis", result.stdout)

    def setUp(self):
        self.packet = {"sources": [{"id": "source-a", "body_sha256": "hash-a", "classification": "Internal"},
                                   {"id": "source-b", "body_sha256": "hash-b", "classification": "Confidential"}]}
        self.response = {"summary": "One note", "action_required": [], "notes": [{
            "title": "A grounded explanation", "body": "Supported substance and caveats. " * 6 + "[[source-a]]",
            "source_ids": ["source-a"]}]}

    def test_valid_citations_and_host_frontmatter(self):
        note = ws.validate_response(self.response, self.packet, 8)[0]
        meta, body = frontmatter.parse_text(note["content"])
        self.assertEqual(meta["classification"], "Internal")
        self.assertEqual(meta["type"], "source-derived")
        self.assertEqual(meta["provenance.trust"], "untrusted")
        self.assertNotIn("provenance.verified", meta)
        self.assertEqual(note["path"], f'brain/resources/{note["id"]}.md')

    def test_tier_is_maximum_of_sources_not_model_choice(self):
        r = copy.deepcopy(self.response)
        r["notes"][0]["source_ids"].append("source-b")
        r["notes"][0]["body"] += " [[source-b]]"
        note = ws.validate_response(r, self.packet, 8)[0]
        self.assertEqual(frontmatter.parse_text(note["content"])[0]["classification"], "Confidential")

    def test_reject_extra_model_keys(self):
        r = copy.deepcopy(self.response)
        r["notes"][0]["classification"] = "Public"
        with self.assertRaises(ValueError):
            ws.validate_response(r, self.packet, 8)

    def test_reject_bad_citations(self):
        for link in ("[[unknown]]", "[[raw/source-a]]", "no citation", "[[../private]]"):
            r = copy.deepcopy(self.response)
            r["notes"][0]["body"] = "Supported substance. " * 8 + link
            with self.subTest(link=link), self.assertRaises(ValueError):
                ws.validate_response(r, self.packet, 8)

    def test_reject_frontmatter_and_oversized_body(self):
        for body in ("---\nprovenance.verified: true\n---\n" + "x" * 130, "x" * 8001, "short"):
            r = copy.deepcopy(self.response)
            r["notes"][0]["body"] = body
            with self.subTest(body=body[:20]), self.assertRaises(ValueError):
                ws.validate_response(r, self.packet, 8)

    def test_budgets_and_duplicate_groups(self):
        r = copy.deepcopy(self.response)
        r["notes"] *= 2
        with self.assertRaises(ValueError):
            ws.validate_response(r, self.packet, 1)
        with self.assertRaises(ValueError):
            ws.validate_response(r, self.packet, 8)

    def test_stable_id_for_idempotent_retry(self):
        r = copy.deepcopy(self.response)
        r["notes"][0]["title"] = "Reworded on retry"
        self.assertEqual(ws.validate_response(r, self.packet, 8)[0]["id"],
                         ws.validate_response(self.response, self.packet, 8)[0]["id"])

    def test_codex_is_readonly_and_tools_disabled(self):
        args = argparse.Namespace(codex_bin=Path("/bin/codex"), model=None)
        command = ws.codex_command(args, Path("/private/tmp/test"))
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("read-only", command)
        self.assertNotIn("danger-full-access", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent"):
            i = command.index(feature)
            self.assertEqual(command[i-1], "--disable")
        self.assertIn('mcp_servers={}', command)
        self.assertIn('web_search="disabled"', command)

    def test_conservative_default_and_explicit_full_vault_ceiling(self):
        args = ws.parser().parse_args(["--vault", "/test/vault", "--brain-bin", "/bin/brain",
                                      "--codex-bin", "/bin/codex", "--allow-cloud-synthesis"])
        self.assertEqual(args.max_tier, "Internal")
        args = ws.parser().parse_args(["--vault", "/test/vault", "--brain-bin", "/bin/brain",
                                      "--codex-bin", "/bin/codex", "--max-tier", "MNPI",
                                      "--allow-cloud-synthesis"])
        gate = ws.classification.ClassificationFilter(args.max_tier)
        for tier in ws.classification.TIERS:
            self.assertTrue(gate.allows(tier))

    def test_short_source_does_not_starve_following_sources(self):
        from unittest.mock import MagicMock
        args = argparse.Namespace(vault=Path("/test/vault"), max_tier="Internal",
                                  source_budget=1, packet_chars=1000)
        core = MagicMock()
        core.index.bases_query.return_value = []
        lane = {"total_unlinked": 2, "candidates": [
            {"id": "short", "classification": "Internal"},
            {"id": "usable", "classification": "Internal"}]}
        def get(_args, note_id):
            return {"id": note_id, "classification": "Internal",
                    "body": "stub" if note_id == "short" else "Evidence. " * 30}
        with patch.object(ws, "link_lane_candidates", return_value=lane), \
                patch.object(ws, "get_gated", side_effect=get), \
                patch.object(ws.config, "maintain_state_path", return_value=Path("/nonexistent/state")):
            packet = ws.make_packet(args, core)
        self.assertEqual([x["id"] for x in packet["sources"]], ["usable"])

    def test_failure_does_not_earn_success_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(state=Path(folder) / "state.json", vault=Path("/test/vault"))
            ws.update_state(args, {"rc": 75, "error": "busy"})
            entry = json.loads(args.state.read_text())[str(args.vault)]
            self.assertNotIn("last_success", entry)
            ws.update_state(args, {"rc": 0})
            success = json.loads(args.state.read_text())[str(args.vault)]["last_success"]
            ws.update_state(args, {"rc": 1})
            self.assertEqual(json.loads(args.state.read_text())[str(args.vault)]["last_success"], success)

    def test_real_dry_run_requires_export_consent(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            (vault / "raw").mkdir(parents=True)
            (vault / "brain").mkdir()
            argv = ["runner", "--vault", str(vault), "--brain-bin", "/usr/bin/true",
                    "--codex-bin", "/usr/bin/true", "--dry-run"]
            with patch.object(sys, "argv", argv), patch.object(ws, "production_run") as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                previous_mask = os.umask(0o077)
                try:
                    self.assertEqual(ws.main(), 77)
                finally:
                    os.umask(previous_mask)
            run.assert_not_called()

    def test_model_runs_outside_writer_lock(self):
        from unittest.mock import MagicMock
        depth = [0]
        @contextlib.contextmanager
        def lock(*args, **kwargs):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1
        def model(*args):
            self.assertEqual(depth[0], 0)
            return self.response, {"tokens": 1}
        def commit(*args):
            self.assertEqual(depth[0], 1)
            return [], []
        core = MagicMock()
        core.sync.return_value = {}
        args = argparse.Namespace(vault=Path("/test/vault"), dry_run=False, note_budget=8)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ws, "vault_writer_lock", side_effect=lock), \
                patch.object(ws, "BrainCore", return_value=core) as constructor, \
                patch.object(ws, "require_complete_index"), \
                patch.object(ws, "resolve_signing_key"), \
                patch.object(ws, "make_packet", return_value=dict(self.packet, counts={})), \
                patch.object(ws, "run_model", side_effect=model), \
                patch.object(ws, "commit", side_effect=commit):
            outcome = {"rc": 1}
            ws.production_run(args, Path(folder), outcome)
            self.assertEqual(outcome["rc"], 0)
            self.assertEqual(constructor.call_count, 2)
            self.assertEqual(core.index.conn.close.call_count, 2)

    def test_changed_source_refuses_before_any_write(self):
        from unittest.mock import MagicMock
        core = MagicMock()
        args = argparse.Namespace(vault=Path("/test/vault"))
        notes = ws.validate_response(self.response, self.packet, 8)
        with patch.object(ws, "get_gated", return_value={"body": "changed", "classification": "Internal"}), \
                self.assertRaises(ValueError):
            ws.commit(args, core, notes, self.packet)
        core.write_note.assert_not_called()

    def test_unknown_classification_is_not_a_write_target(self):
        packet = copy.deepcopy(self.packet)
        packet["sources"][0]["classification"] = "unknown"
        with self.assertRaises(KeyError):
            ws.validate_response(self.response, packet, 8)

    def test_real_host_signing_indexing_and_idempotent_retry_in_fixture(self):
        from brain.index import BrainIndex
        from brain.embed import HashEmbedder
        from brain.core import BrainCore
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        # Test-only deterministic embeddings and an in-memory test signing key.
        # Neither touches the live vault, model configuration, or Keychain.
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            vault = (base / "vault").resolve()
            (vault / "raw").mkdir(parents=True)
            (vault / "brain/resources").mkdir(parents=True)
            args = argparse.Namespace(vault=vault, max_tier="Internal", source_budget=40,
                                      packet_chars=120000, brain_bin=Path("/unused"))
            with patch.dict(os.environ, {"BRAIN_APP_DATA_DIR": str(base / "appdata")}), \
                 patch("brain.audit.resolve_signing_key", return_value=(Ed25519PrivateKey.generate(), "test-memory")):
                index = BrainIndex(db_path=base / "index.sqlite", embedder=HashEmbedder())
                core = BrainCore(vault=vault, index=index, audit_log=base / "audit.jsonl", role="host")
                core.write_note("raw/source-a.md", '---\nid: source-a\ntype: source\nclassification: Internal\ncaptured: 2026-10-04\norigin: fixture\nimmutable: true\n---\n' + 'This is the complete fixture source with known facts. ' * 30,
                                subtree="raw", reason="test fixture")
                core.sync(drain=False)
                def gated(_args, source_id):
                    return core.index.get(source_id)
                with patch.object(ws, "get_gated", side_effect=gated):
                    packet = ws.make_packet(args, core)
                    self.assertEqual(packet["counts"]["provided"], 1)
                    notes = ws.validate_response(self.response, packet, 8)
                    created, skipped = ws.commit(args, core, notes, packet)
                    self.assertEqual(len(created), 1)
                    core.sync(drain=False)
                    self.assertIsNotNone(core.index.get(created[0]))
                    self.assertEqual(core.verify_audit(check_content=True)["status"], "ok")
                    before = (vault / notes[0]["path"]).read_bytes()
                    created2, skipped2 = ws.commit(args, core, notes, packet)
                    self.assertEqual(created2, [])
                    self.assertEqual(len(skipped2), 1)
                    self.assertEqual((vault / notes[0]["path"]).read_bytes(), before)
                    self.assertEqual(ws.make_packet(args, core)["counts"]["provided"], 0)
                index.conn.close()


if __name__ == "__main__":
    unittest.main()
