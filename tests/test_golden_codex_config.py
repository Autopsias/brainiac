"""Scheduled golden execution must not inherit desktop model/plugins."""
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from brain.golden_ops import _try_codex_probe


class GoldenConfigTests(unittest.TestCase):
    def test_isolated_read_only_command_and_validated_fallback(self):
        calls = []

        def call(argv, timeout):
            calls.append((argv, timeout))
            return 1, "", "unavailable"

        result, error = _try_codex_probe(
            SimpleNamespace(vault=Path('/tmp/vault')),
            Path('/tmp/vault/eval/golden-probes.json'), timeout=30, call=call)
        self.assertIsNone(result)
        self.assertIn('unavailable', error)
        argv, timeout = calls[0]
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
        self.assertIn('mcp_servers={}', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertEqual(timeout, 30)

    def test_transient_child_result_requests_host_fallback(self):
        doc = {'score': None, 'disposition': 'transient', 'exit_code': 3}
        event = {'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': json.dumps(doc)}}
        result, error = _try_codex_probe(
            SimpleNamespace(vault=Path('/tmp/vault')),
            Path('/tmp/vault/eval/golden-probes.json'), timeout=30,
            call=lambda argv, timeout: (0, json.dumps(event), ''))
        self.assertIsNone(result)
        self.assertIn('transient', error)


if __name__ == '__main__':
    unittest.main()
