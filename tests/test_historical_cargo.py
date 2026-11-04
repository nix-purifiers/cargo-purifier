# SPDX-License-Identifier: MIT
"""Native resolver/workspace API checks; not project compilation coverage."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.environ.get('CARGO_COMPAT_TOOLS', str(Path(__file__).resolve().parents[1] / 'tools')))
from cargo_graph import VersionSelector


class NativeCargoCompatibility(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cargo = os.environ['CARGO_COMPAT_NATIVE']
        self.environment = dict(os.environ, CARGO_HOME=str(self.root / 'home'), CARGO_PURIFIER='0')
        self.selector = VersionSelector(self.cargo, self.environment, self.root / 'selectors')

    def test_native_version_is_the_selected_sdk(self):
        version = subprocess.check_output([self.cargo, '--version'], text=True)
        self.assertIn('cargo ' + os.environ['CARGO_COMPAT_EXPECTED_VERSION'], version)

    def test_native_selection_and_incompatible_range(self):
        for requirement, versions, expected in [
                ('^1.0', ['1.0.0', '1.1.0', '2.0.0'], '1.1.0'),
                ('=1.0.0', ['1.0.0', '1.1.0'], '1.0.0'),
                ('>=1.0, <2', ['1.0.0', '1.5.0', '2.0.0'], '1.5.0')]:
            with self.subTest(requirement=requirement):
                self.assertEqual(self.selector.select(requirement, versions), expected)
        with self.assertRaisesRegex(ValueError, 'Cargo cannot select'):
            self.selector.select('^3', ['1.0.0', '2.0.0'])
        self.assertEqual(list((self.root / 'selectors').iterdir()), [])

    def test_native_workspace_root_without_lock_or_registry_access(self):
        project = self.root / 'workspace'
        (project / 'member/src').mkdir(parents=True)
        files = {
            'Cargo.toml': '[workspace]\nmembers=["member"]\n',
            'member/Cargo.toml': '[package]\nname="member"\nversion="0.1.0"\nauthors=[]\n'
                '[dependencies]\npurifier-impossible-registry-crate="=999.999.999"\n',
            'member/src/lib.rs': '',
        }
        for name, data in files.items():
            (project / name).write_text(data)
        manifest = project / 'member/Cargo.toml'
        command = os.environ.get('CARGO_COMPAT_COMMAND', str(Path(__file__).resolve().parents[1] / 'pkgs/cargo-purifier/cargo_purifier.py'))
        result = subprocess.check_output([sys.executable, command, '_workspace-root', str(manifest)],
                                         cwd=manifest.parent, env=self.environment, text=True)
        self.assertEqual(result.strip(), str(project / 'Cargo.toml'))
        self.assertFalse((project / 'Cargo.lock').exists())
        for name, data in files.items():
            self.assertEqual((project / name).read_text(), data)


if __name__ == '__main__':
    unittest.main()
