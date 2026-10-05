"""The model can invoke one measurement, never arbitrary host operations."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from brain.golden_executor import Executor, measure, serve


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = Executor(Path('/tmp/vault'), Path('/tmp/probes.json'), 30)

    def test_only_measurement_tool_is_exposed(self):
        tool = self.executor.dispatch('tools/list', {})['tools']
        self.assertEqual([item['name'] for item in tool], ['run_golden_probe'])
        self.assertFalse(tool[0]['inputSchema']['additionalProperties'])

    def test_reject_arbitrary_commands_and_arguments(self):
        for params in ({'name': 'shell'}, {'name': 'run_golden_probe', 'arguments': {'vault': '/'}},
                       {'name': 'run_golden_probe', 'arguments': {'command': 'rm'}}):
            with self.assertRaises(ValueError):
                self.executor.dispatch('tools/call', params)

    def test_measurement_runs_once_and_is_cached(self):
        doc = {'score': 1.0, 'disposition': 'ok', 'exit_code': 0}
        with patch('brain.golden_executor.measure', return_value=doc) as runner:
            for _ in range(2):
                result = self.executor.dispatch('tools/call', {'name': 'run_golden_probe', 'arguments': {}})
                self.assertEqual(result['structuredContent'], doc)
            runner.assert_called_once_with(Path('/tmp/vault'), Path('/tmp/probes.json'), 30)

    def test_protocol_notifications_do_not_run_measurement(self):
        incoming = io.StringIO(json.dumps({'method': 'notifications/initialized'})+'\n'+
                               json.dumps({'id': 1, 'method': 'ping'})+'\n')
        outgoing = io.StringIO()
        with patch('brain.golden_executor.measure') as runner:
            serve(self.executor, incoming, outgoing)
            runner.assert_not_called()
        self.assertEqual(json.loads(outgoing.getvalue())['result'], {})

    def test_measure_refuses_process_document_mismatch(self):
        from types import SimpleNamespace
        doc = {'score': 1.0, 'disposition': 'ok', 'exit_code': 0}
        with patch('brain.golden_executor.subprocess.run', return_value=SimpleNamespace(
                returncode=1, stdout=json.dumps(doc))):
            with self.assertRaises(ValueError):
                measure(Path('/tmp/vault'), Path('/tmp/probes.json'), 30)


if __name__ == '__main__':
    unittest.main()
