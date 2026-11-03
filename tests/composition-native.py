#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Independent Nix producer, normal Cargo workflow, and pure consumer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from registry_fixture import registry

sdk = Path(__file__).resolve().parents[1]
tools = Path(sys.argv[1]).resolve()


def run(*args, cwd=None, env=None):
    return subprocess.check_output([str(arg) for arg in args], cwd=cwd, env=env, text=True)


snapshot = json.loads(run('nix', 'flake', 'archive', '--json', '--no-write-lock-file', sdk))['path']
system = run('nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem').strip()
with tempfile.TemporaryDirectory(prefix='cargo-composition-') as temporary:
    # Nix refuses a path: flake through a symlink, such as macOS's /tmp.
    root = Path(temporary).resolve()
    producer, consumer = root / 'producer', root / 'consumer'
    producer.mkdir()
    (consumer / 'src').mkdir(parents=True)
    producer_flake = '''{
      inputs.sdk.url = "path:SDK";
      outputs = { sdk, ... }: let
        pkgs = import sdk.inputs.nixpkgs {
          system = "SYSTEM";
          overlays = [ sdk.overlays.default ];
        };
        project = pkgs.cargo-purifier.buildCargoProject {
          src = sdk.outPath + "/tests/fixtures/workspace";
        };
      in { packages.SYSTEM.default = project.release.greet; };
    }'''.replace('SDK', snapshot).replace('SYSTEM', system)
    (producer / 'flake.nix').write_text(producer_flake)
    run('nix', 'flake', 'lock', 'path:' + str(producer), cwd=producer)
    original = '[package]\nname="consumer"\nversion="1.0.0"\nedition="2021"\n[dependencies]\ngreet="99"\n'
    (consumer / 'Cargo.toml').write_text(original)
    (consumer / 'src/main.rs').write_text('fn main(){println!("{}",greet::greeting(42))}\n')
    (consumer / 'src/lib.rs').write_text('pub fn greeting()->String{greet::greeting(42)}\n')
    environment = dict(os.environ, PATH=str(tools / 'bin') + ':' + os.environ['PATH'])
    run(tools / 'bin/cargo-purifier', 'lock', '--input', 'path:' + str(producer),
        cwd=consumer, env=environment)
    assert (consumer / 'Cargo.toml').read_text() == original
    lock_path = consumer / 'cargo-purifier.lock.json'
    first = lock_path.read_bytes()
    lock = json.loads(first)
    assert lock['projectInputs'][0]['narHash']
    assert not lock['git'] and not lock['registries']
    assert not lock.get('provided')
    assert '-nix.h' not in (consumer / 'Cargo.lock').read_text()
    assert 'checksum' not in (consumer / 'Cargo.lock').read_text()
    run(tools / 'bin/cargo-purifier', 'lock', cwd=consumer, env=environment)
    assert lock_path.read_bytes() == first, 'relocking moved a Nix source'
    run(tools / 'bin/cargo-purifier', 'prefetch', cwd=consumer, env=environment)
    expected = 'hello from 42 crates\n'
    saved_native_lock = (consumer / 'Cargo.lock').read_bytes()
    assert run(tools / 'bin/cargo', 'run', '--quiet', cwd=consumer, env=environment) == expected
    assert (consumer / 'Cargo.lock').read_bytes() == saved_native_lock
    assert (consumer / 'Cargo.toml').read_text() == original
    run(tools / 'bin/cargo', 'remove', 'greet', cwd=consumer, env=environment)
    run(tools / 'bin/cargo-purifier', 'lock', cwd=consumer, env=environment)
    run(tools / 'bin/cargo', 'add', 'greet@99', cwd=consumer, env=environment)
    assert '-nix.h' not in (consumer / 'Cargo.toml').read_text()
    run(tools / 'bin/cargo-purifier', 'lock', cwd=consumer, env=environment)
    assert run(tools / 'bin/cargo', 'run', '--quiet', cwd=consumer, env=environment) == expected
    original = (consumer / 'Cargo.toml').read_text()
    (consumer / 'flake.nix').write_text('''{
      inputs.sdk.url = "path:SDK";
      outputs = { sdk, ... }: let
        pkgs = import sdk.inputs.nixpkgs {
          system = "SYSTEM";
          overlays = [ sdk.overlays.default ];
        };
      in {
        packages.SYSTEM.default =
          (pkgs.cargo-purifier.buildCargoProject { src = ./.; }).packages.consumer;
      };
    }'''.replace('SDK', snapshot).replace('SYSTEM', system))
    run('nix', 'flake', 'lock', 'path:' + str(consumer), cwd=consumer)
    built = json.loads(run('nix', 'build', '--no-link', '--json', 'path:' + str(consumer)))
    assert run(Path(built[0]['outputs']['out']) / 'bin/consumer') == expected
    assert (consumer / 'Cargo.toml').read_text() == original
    with registry() as (address, archives, records, uploads):
        configuration = consumer / '.cargo/config.toml'
        configuration.parent.mkdir(exist_ok=True)
        configuration.write_text('[registries.projected]\nindex=' +
                                 json.dumps('sparse+' + address + '/index/') + '\n')
        publication_env = dict(environment, CARGO_REGISTRIES_PROJECTED_TOKEN='fixture-token')
        dry_run = json.loads(run(tools / 'bin/cargo-purifier', 'publish', '--dry-run',
                                 cwd=consumer, env=environment))
        assert '-nix.h' in dry_run['vers']
        assert all('-nix.h' in item['version_req'] for item in dry_run['deps'])
        run(tools / 'bin/cargo-purifier', 'publish', '--registry', 'projected',
            cwd=consumer, env=publication_env)
        count = len(uploads)
        assert count >= 3, 'the dependency closure must be published with the root'
        run(tools / 'bin/cargo-purifier', 'publish', '--registry', 'projected',
            cwd=consumer, env=publication_env)
        assert len(uploads) == count, 'identical coordinates must be reused'
        ordinary = root / 'ordinary'
        ordinary.mkdir()
        ordinary_env = dict(environment, CARGO_PURIFIER='0', CARGO_HOME=str(ordinary / 'home'),
                            CARGO_REGISTRIES_PROJECTED_INDEX='sparse+' + address + '/index/')
        (ordinary / 'src').mkdir()
        (ordinary / 'Cargo.toml').write_text('[package]\nname="ordinary"\nversion="1.0.0"\n'
            'edition="2021"\n[dependencies]\nconsumer={version=' + json.dumps('=' + dry_run['vers']) +
            ',registry="projected"}\n')
        (ordinary / 'src/main.rs').write_text('fn main(){println!("{}",consumer::greeting())}\n')
        assert run(tools / 'bin/cargo', 'run', '--quiet', cwd=ordinary, env=ordinary_env) == expected
        archives[('consumer', dry_run['vers'])] += b'corrupt'
        refused = subprocess.run([str(tools / 'bin/cargo-purifier'), 'publish', '--registry', 'projected'],
                                 cwd=consumer, env=publication_env, text=True, capture_output=True)
        assert refused.returncode and 'different bytes' in refused.stderr
        assert len(uploads) == count
    run(tools / 'bin/cargo', 'remove', 'greet', cwd=consumer, env=environment)
    (consumer / 'src/main.rs').write_text('fn main(){println!("ordinary")}\n')
    (consumer / 'src/lib.rs').write_text('')
    run(tools / 'bin/cargo-purifier', 'lock', '--no-inputs', cwd=consumer, env=environment)
    cleared = json.loads(lock_path.read_text())
    assert not cleared.get('projectInputs') and not cleared.get('provided')
    assert not list((consumer / 'target/cargo-purifier/inputs').glob('[0-9]*'))
    assert not (consumer / 'target/cargo-purifier/prefetch').exists()
    assert run(tools / 'bin/cargo', 'run', '--quiet', cwd=consumer, env=environment) == 'ordinary\n'
    print('PASS cargo.composition.source-pinned-pure-consumer')
