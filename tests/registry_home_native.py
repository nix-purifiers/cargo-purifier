# SPDX-License-Identifier: MIT
"""Check native registry-location semantics through an installed Cargo wrapper."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from registry_fixture import registry


def verify_registry_cargo_home(tools, root, environment):
    root = Path(root).resolve()
    root.mkdir()
    with registry() as (address, archives, records, uploads):
        producer, consumer = root / 'producer', root / 'consumer'
        index = 'sparse+' + address + '/index/'
        configuration = ('[registries.fixture]\nindex="' + index + '"\n'
                         'credential-provider="cargo:token"\n')
        for project in (producer, consumer):
            (project / '.cargo').mkdir(parents=True)
            (project / '.cargo/config.toml').write_text(configuration)
            (project / 'src').mkdir()
        (producer / 'Cargo.toml').write_text(
            '[package]\nname="registry-home-probe"\nversion="1.0.0"\nedition="2021"\n')
        (producer / 'src/lib.rs').write_text('pub fn value() -> u8 { 42 }\n')
        (producer / 'build.rs').write_text('''use std::{env, path::PathBuf};
fn main() {
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let home = PathBuf::from(env::var_os("CARGO_HOME").unwrap());
    assert!(manifest.starts_with(&home), "registry source outside Cargo home: {} != {}", manifest.display(), home.display());
    std::fs::write(manifest.join("generated-by-build-script"), "writable").unwrap();
    println!("cargo:warning=registry source is inside effective Cargo home");
}
''')
        (consumer / 'Cargo.toml').write_text(
            '[package]\nname="registry-home-consumer"\nversion="1.0.0"\nedition="2021"\n'
            '[dependencies]\nregistry-home-probe={version="=1.0.0",registry="fixture"}\n')
        (consumer / 'src/lib.rs').write_text('pub fn value() -> u8 { registry_home_probe::value() }\n')
        caller = root / 'caller-home'
        caller.mkdir()
        env = dict(environment, CARGO_HOME=str(caller), CARGO_REGISTRIES_FIXTURE_TOKEN='fixture-token')
        native = dict(env, CARGO_PURIFIER='0', CARGO_TARGET_DIR=str(root / 'native-target'))

        commands = []

        def run(*arguments, cwd=consumer, env=env):
            command = [str(argument) for argument in arguments]
            result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
            name = 'command-' + str(len(commands))
            (root / (name + '.stdout')).write_text(result.stdout)
            (root / (name + '.stderr')).write_text(result.stderr)
            commands.append({'command': command, 'cwd': str(cwd), 'exitCode': result.returncode,
                             'stdout': name + '.stdout', 'stderr': name + '.stderr'})
            (root / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
            result.check_returncode()
            return result

        def verify_build_script_output(target):
            marker = 'cargo:warning=registry source is inside effective Cargo home'
            outputs = [path for path in target.rglob('output') if path.is_file()
                       and marker in path.read_text()]
            assert len(outputs) == 1, 'missing actual registry location build-script output'

        run(tools / 'bin/cargo', 'publish', '--registry', 'fixture', '--allow-dirty',
            '--no-verify', cwd=producer, env=native)
        assert uploads == [('registry-home-probe', '1.0.0')]
        # The Nix daemon can live outside this process's loopback namespace.
        # Prefetch the exact archive accepted by the native registry, using the
        # fixed-output name/hash that the normal fetchurl derivation requests.
        coordinate = ('registry-home-probe', '1.0.0')
        archive = root / 'crate-registry-home-probe-1.0.0.tar.gz'
        archive.write_bytes(archives[coordinate])
        checksum = records[coordinate]['cksum']
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum
        cached = Path(run('nix-store', '--add-fixed', 'sha256', archive).stdout.strip())
        assert hashlib.sha256(cached.read_bytes()).hexdigest() == checksum
        expected = run('nix-store', '--print-fixed-path', 'sha256', checksum, archive.name).stdout.strip()
        assert str(cached) == expected, 'prefetch differs from the declared flat fixed-output identity'
        (root / 'published-archive.json').write_text(json.dumps({
            'registryIndex': records[coordinate], 'archive': str(archive),
            'size': archive.stat().st_size, 'sha256': checksum,
            'cachedPath': str(cached), 'expectedFixedOutputPath': expected,
        }, indent=2) + '\n')
        run(tools / 'bin/cargo', 'build', env=native)
        verify_build_script_output(root / 'native-target')
        run(tools / 'bin/cargo-purifier', 'lock', '--no-inputs')
        locks = {name: (consumer / name).read_bytes()
                 for name in ('Cargo.lock', 'cargo-purifier.lock.json')}
        run(tools / 'bin/cargo', 'build', '--locked',
                      env=dict(env, CARGO_TARGET_DIR=str(root / 'wrapped-target')))
        verify_build_script_output(root / 'wrapped-target')
        assert all((consumer / name).read_bytes() == data for name, data in locks.items())
        assert not (caller / 'config.toml').exists()
        print('PASS cargo.composition.registry-source-under-effective-cargo-home')
