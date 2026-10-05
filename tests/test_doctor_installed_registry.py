import unittest
from pathlib import Path
from unittest.mock import patch

from brain import doctor_context, workspaces


class InstalledRegistryTests(unittest.TestCase):
    def test_installed_doctor_uses_canonical_registry_without_checkout(self):
        entries = [{'vault_path': '/fixture/vault'}]
        with patch.object(workspaces, 'list_entries', return_value=entries):
            self.assertEqual(doctor_context.resolve_registry_entries(Path('/missing/tools')),
                             (entries, False))

    def test_registry_failure_still_reports_unavailable(self):
        with patch.object(workspaces, 'list_entries', side_effect=OSError('unreadable')):
            self.assertEqual(doctor_context.resolve_registry_entries(Path('/missing/tools')),
                             ([], True))


if __name__ == '__main__':
    unittest.main()
