# SPDX-License-Identifier: MIT
"""Native publication and source attestation controls for missing optional readmes."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import tomlkit

tools = Path(os.environ.get('CARGO_PURIFIER_TEST_TOOLS', Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(tools))
import cargo_publication
import cargo_repository


class PublicationSources(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cargo-publication-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cargo = os.environ['CARGO_REPOSITORY_TEST_CARGO']

    def fixture(self, name, readme=False, missing=None):
        source = self.root / name
        (source / 'src').mkdir(parents=True)
        manifest = ('[package]\nname="publication-fixture"\nversion="1.0.0"\n'
                    'edition="2021"\nreadme="README.md"\n'
                    + ('license-file="MISSING_LICENSE"\n' if missing == 'license' else '')
                    + ('build="missing_build.rs"\n' if missing == 'build' else '')
                    + '[features]\ndefault=["native"]\nnative=[]\n')
        (source / 'Cargo.toml').write_text(manifest)
        if missing != 'src':
            (source / 'src/lib.rs').write_text('#[cfg(feature="native")] pub fn answer()->u32{42}\n')
        if readme:
            (source / 'README.md').write_bytes(b'Original publication documentation\n')
        # An intentionally inconsistent published-source fixture cannot be
        # authored by modern Cargo. Preserve its exact archive and checksum.
        archive = self.root / (name + '.crate')
        with tarfile.open(archive, 'w') as tar:
            for path in sorted(source.rglob('*')):
                if path.is_file():
                    data = path.read_bytes()
                    info = tarfile.TarInfo('publication-fixture-1.0.0/' + str(path.relative_to(source)))
                    info.size = len(data)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
        capture = {'archive': str(archive), 'checksum': hashlib.sha256(archive.read_bytes()).hexdigest()}
        (source / '.cargo-checksum.json').write_text(json.dumps({'files': {}, 'package': capture['checksum']}))
        return source, capture

    def request(self, source, captures=()):
        return {'identity': hashlib.sha256(b'native-publication-fixture').hexdigest(),
                'src': str(source), 'publicationSourceArchives': list(captures)}

    def project(self, source, captures, name):
        output = self.root / name
        cargo_repository.project(self.request(source, captures), output, self.root / (name + '.json'), self.cargo)
        return output

    def test_native_failure_repair_and_offline_consumer(self):
        source, capture = self.fixture('missing-readme')
        original = {str(path.relative_to(source)): path.read_bytes()
                    for path in source.rglob('*') if path.is_file()}
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.project(source, [], 'unattested')
        self.assertEqual(failure.exception.returncode, 101)
        output = self.project(source, [capture], 'attested')
        catalog = json.loads((output / '.catalog').read_text())
        entry, = catalog['packages']
        adjustment = entry['publicationMetadataAdjustment']
        self.assertEqual(adjustment['field'], 'package.readme')
        self.assertEqual(adjustment['sourceArchiveSha256'], capture['checksum'])
        self.assertEqual(adjustment['sourceManifestSha256'], hashlib.sha256(original['Cargo.toml']).hexdigest())
        manifest = cargo_repository.crate_manifest((output / entry['archive']).read_bytes())
        self.assertIs(manifest['package'].get('readme', False), False)
        self.assertEqual(manifest['features']['default'], ['native'])
        with tarfile.open(output / entry['archive']) as tar:
            self.assertEqual(tar.extractfile(entry['name'] + '-' + entry['version'] + '/src/lib.rs').read(), original['src/lib.rs'])
        self.assertEqual(original, {str(path.relative_to(source)): path.read_bytes()
                                  for path in source.rglob('*') if path.is_file()})
        consumer = self.root / 'consumer'
        (consumer / 'src').mkdir(parents=True)
        (consumer / '.cargo').mkdir()
        (consumer / 'Cargo.toml').write_text('[package]\nname="consumer"\nversion="1.0.0"\nedition="2021"\n[dependencies]\npublication-fixture="=' + entry['version'] + '"\n')
        (consumer / 'src/main.rs').write_text('fn main(){println!("{}",publication_fixture::answer())}\n')
        (consumer / '.cargo/config.toml').write_text('[source.crates-io]\nreplace-with="provided"\n[source.provided]\nlocal-registry=' + json.dumps(str(output)) + '\n')
        environment = dict(os.environ, CARGO_HOME=str(self.root / 'consumer-home'))
        actual = subprocess.check_output([self.cargo, 'run', '--offline', '--quiet'], cwd=consumer, env=environment, text=True)
        self.assertEqual(actual, '42\n')

    def test_original_readme_is_preserved(self):
        source, _ = self.fixture('existing-readme', readme=True)
        output = self.project(source, [], 'existing-publication')
        entry, = json.loads((output / '.catalog').read_text())['packages']
        self.assertNotIn('publicationMetadataAdjustment', entry)
        with tarfile.open(output / entry['archive']) as tar:
            self.assertEqual(tar.extractfile(entry['name'] + '-' + entry['version'] + '/README.md').read(), (source / 'README.md').read_bytes())

    def test_archive_and_source_guards(self):
        source, capture = self.fixture('guards')
        manifest = source / 'Cargo.toml'
        adjustments = cargo_publication.prepare([manifest], [capture])
        document = tomlkit.parse(manifest.read_text())
        original = tomlkit.parse(manifest.read_text())
        cargo_publication.omit(document, manifest, adjustments)
        self.assertEqual(original['package'].pop('readme'), 'README.md')
        self.assertEqual(document, original)
        self.assertIn('readme', tomlkit.parse(manifest.read_text())['package'])
        with self.assertRaisesRegex(ValueError, 'corrupt publication source archive'):
            cargo_publication.prepare([manifest], [dict(capture, checksum='0' * 64)])
        for name, mutation, message in [
            ('changed-code', lambda root: (root / 'src/lib.rs').write_text('pub fn changed(){}'), 'differs from original archive'),
            ('changed-manifest', lambda root: (root / 'Cargo.toml').write_text((root / 'Cargo.toml').read_text() + '[dependencies]\nchanged="1"\n'), 'differs from original archive'),
            ('added-build-input', lambda root: (root / 'build.rs').write_text('fn main(){}'), 'added files'),
            ('wrong-captured-checksum', lambda root: (root / '.cargo-checksum.json').write_text('{"files":{},"package":"wrong"}'), 'differs from captured registry checksum'),
        ]:
            with self.subTest(name=name):
                root, selected = self.fixture(name)
                mutation(root)
                with self.assertRaisesRegex(ValueError, message):
                    cargo_publication.prepare([root / 'Cargo.toml'], [selected])
        source, capture = self.fixture('vendor-readme-loss', readme=True)
        (source / 'README.md').unlink()
        with self.assertRaisesRegex(ValueError, 'original publication contains readme'):
            cargo_publication.prepare([source / 'Cargo.toml'], [capture])

    def test_required_inputs_remain_required(self):
        for missing in ['src', 'license', 'build']:
            with self.subTest(missing=missing):
                source, capture = self.fixture('missing-' + missing, missing=missing)
                with self.assertRaises(subprocess.CalledProcessError):
                    self.project(source, [capture], 'required-' + missing)


if __name__ == '__main__':
    unittest.main()
