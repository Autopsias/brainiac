"""An explicit engine-only update never invokes Claude plugin/hook mutations."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from brain.update_engine_only import run_engine_only_flow


class EngineOnlyTests(unittest.TestCase):
    def setUp(self):
        self.callbacks = SimpleNamespace(**{name: Mock() for name in (
            'run_doctor', 'render_human', 'refresh_engine_venv', 'reexec_after_engine_move',
            'rebuild_dist', 'restage_workspaces', 'render_before_after', 'compare',
            'probe_capability', 'refresh_marketplace', 'apply_plugin_action')})
        self.callbacks.run_doctor.return_value = {'rows': [], 'ok': True, 'stale_count': 0}
        self.callbacks.refresh_engine_venv.return_value = {'ok': True}
        self.runner = Mock()

    def execute(self, dry_run=False):
        return run_engine_only_flow(engine_src=None, brainiac_home=Path('/tmp/not-used'),
                                    claude_home=Path('/tmp/not-used'), run=self.runner,
                                    dry_run=dry_run, callbacks=self.callbacks)

    def test_engine_refresh_without_claude_operations(self):
        result = self.execute()
        self.assertTrue(result['ok'])
        self.callbacks.refresh_engine_venv.assert_called_once()
        self.callbacks.probe_capability.assert_not_called()
        self.callbacks.refresh_marketplace.assert_not_called()
        self.callbacks.apply_plugin_action.assert_not_called()
        self.assertTrue(result['steps']['session_hook']['skipped'])

    def test_dry_run_has_no_engine_mutations(self):
        self.execute(dry_run=True)
        self.callbacks.refresh_engine_venv.assert_not_called()
        self.callbacks.reexec_after_engine_move.assert_not_called()
        self.runner.assert_not_called()

    def test_engine_failure_stops_before_staging(self):
        self.callbacks.refresh_engine_venv.return_value = {'ok': False, 'detail': 'install failed'}
        result = self.execute()
        self.assertFalse(result['ok'])
        self.assertNotIn('workspace_restage', result['steps'])
        self.callbacks.rebuild_dist.assert_not_called()


if __name__ == '__main__':
    unittest.main()
