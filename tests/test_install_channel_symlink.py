"""PATH launchers must select the owning package manager, not pip --user."""
from pathlib import Path
import tempfile
import unittest
from brain.doctor_plugins import CHANNEL_PIPX, CHANNEL_PIP_USER, detect_install_channel


class ChannelTests(unittest.TestCase):
    def test_pipx_symlink_resolves_to_owning_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'pipx/venvs/brainiac-cli/bin/brain'
            target.parent.mkdir(parents=True)
            target.touch()
            launcher = root / 'brain'
            try:
                launcher.symlink_to(target)
            except OSError as exc:
                self.skipTest(f'Symlink creation unavailable: {exc}')
            self.assertEqual(detect_install_channel(launcher), CHANNEL_PIPX)

    def test_ordinary_user_launcher_remains_user_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / 'brain'
            launcher.touch()
            self.assertEqual(detect_install_channel(launcher), CHANNEL_PIP_USER)


if __name__ == '__main__':
    unittest.main()
