#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real Cargo/plugin dispatch, inherited sources, and native mutation ownership.

Run with a tools package and an independent SDK source checkout. Initial
capture may use the caller's Cargo cache/network; all runtime cases start
with a Cargo home containing no registry index or downloaded sources.
"""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

tools, sdk_source = (Path(argument).resolve() for argument in sys.argv[1:3])
evidence = Path(os.environ['DELEGATION_EVIDENCE_DIR'])
evidence.mkdir(parents=True, exist_ok=True)
events = []


def call(arguments, cwd, environment, success=True):
    result = subprocess.run([str(value) for value in arguments], cwd=cwd, env=environment,
                            text=True, capture_output=True, timeout=300)
    number = len(events)
    (evidence / f'{number}.stdout').write_text(result.stdout)
    (evidence / f'{number}.stderr').write_text(result.stderr)
    events.append({'command': [str(value) for value in arguments], 'cwd': str(cwd),
                   'cargoHome': environment.get('CARGO_HOME'), 'exitCode': result.returncode,
                   'stdoutSha256': hashlib.sha256(result.stdout.encode()).hexdigest(),
                   'stderrSha256': hashlib.sha256(result.stderr.encode()).hexdigest()})
    (evidence / 'commands.json').write_text(json.dumps(events, indent=2) + '\n')
    if success and result.returncode:
        raise AssertionError(f'{arguments}: {result.returncode}\n{result.stdout}\n{result.stderr}')
    return result


with tempfile.TemporaryDirectory(prefix='cargo-delegation-') as temporary:
    root = Path(temporary).resolve()
    project = root / 'ordinary'
    shutil.copytree(sdk_source / 'tests/fixtures/workspace/greet', project)
    fixture = tomllib.loads((sdk_source / 'tests/fixtures/workspace/Cargo.lock').read_text())
    itoa = next(package for package in fixture['package'] if package['name'] == 'itoa')
    (project / 'Cargo.lock').write_text(
        'version=4\n[[package]]\nname="greet"\nversion="0.1.0"\ndependencies=["itoa"]\n'
        '[[package]]\nname="itoa"\nversion=' + json.dumps(itoa['version']) + '\nsource=' +
        json.dumps(itoa['source']) + '\nchecksum=' + json.dumps(itoa['checksum']) + '\n')
    environment = dict(os.environ, PATH=str(tools / 'bin') + ':' + os.environ['PATH'])
    for key in ('CARGO', 'CARGO_PURIFIER_REAL_CARGO', 'CARGO_PURIFIER_CALLER_PATH'):
        environment.pop(key, None)
    environment['CARGO_TARGET_DIR'] = str(root / 'target')
    environment['CARGO_BUILD_JOBS'] = '2'
    environment['RUST_TEST_THREADS'] = '1'
    seed = dict(environment)
    call([tools / 'bin/cargo-purifier', 'lock'], project, seed)
    call([tools / 'bin/cargo-purifier', 'prefetch'], project, seed)
    original_manifest = (project / 'Cargo.toml').read_bytes()
    original_lock = (project / 'Cargo.lock').read_bytes()
    capture = (project / 'cargo-purifier.lock.json').read_bytes()
    home, binaries = root / 'empty-home', root / 'path-bin'
    (home / 'bin').mkdir(parents=True)
    binaries.mkdir()
    environment.update(CARGO_HOME=str(home), CARGO_NET_OFFLINE='true',
                       PATH=str(binaries) + ':' + environment['PATH'])
    selected = call([tools / 'bin/cargo-purifier', '_native-cargo'], project, environment).stdout.strip()
    expected_version = call([selected, '--version'], project, environment).stdout
    plugin = '''#!PYTHON
import json, os, pathlib, subprocess, sys
report = {'origin': ORIGIN, 'argv': sys.argv[1:], 'cwd': os.getcwd(),
          'path': os.environ['PATH'], 'cargo': os.environ['CARGO'],
          'rustc': os.environ.get('RUSTC'),
          'callerPath': os.environ.get('CARGO_PURIFIER_CALLER_PATH'),
          'configSentinel': os.environ.get('DELEGATION_SENTINEL')}
target = pathlib.Path(os.environ['DELEGATION_REPORT'])
target.write_text(json.dumps(report))
cargo = os.environ['CARGO']
report['cargoVersion'] = subprocess.check_output([cargo, '--version'], text=True)
if sys.argv[2] == 'edit':
    manifest = pathlib.Path('Cargo.toml')
    manifest.write_text(manifest.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
    result = subprocess.run([cargo, 'metadata', '--offline', '--format-version', '1'], capture_output=True, text=True)
else:
    result = subprocess.run([cargo, 'metadata', '--offline', '--locked', '--format-version', '1'], capture_output=True, text=True)
report['metadataExit'] = result.returncode
report['metadataError'] = result.stderr
if not result.returncode and sys.argv[2] != 'edit':
    tests = subprocess.run([cargo, 'test', '--offline', '--locked', '--quiet'], capture_output=True, text=True)
    report['testExit'] = tests.returncode
    report['testOutput'] = tests.stdout + tests.stderr
target.write_text(json.dumps(report))
sys.exit(result.returncode or (77 if sys.argv[2] == 'edit' else report['testExit']))
'''.replace('PYTHON', sys.executable)
    for directory, origin in ((home / 'bin', 'cargo-home'), (binaries, 'path')):
        path = directory / 'cargo-delegation-probe'
        path.write_text(plugin.replace('ORIGIN', repr(origin)))
        path.chmod(0o755)
    report = root / 'report.json'
    environment['DELEGATION_REPORT'] = str(report)

    if '--provided-only' not in sys.argv[3:]:
        # Let native Cargo tell us its winner and argv, even though its unprepared
        # fresh home cannot resolve itoa. The wrapper must select that same plugin.
        native = call([selected, 'delegation-probe', 'read', 'with spaces', '--literal'],
                      project, environment, success=False)
        control = json.loads(report.read_text())
        assert native.returncode and control['metadataExit']
        call([tools / 'bin/cargo', 'delegation-probe', 'read', 'with spaces', '--literal'], project, environment)
        actual = json.loads(report.read_text())
        (evidence / 'native-dispatch-control.json').write_text(json.dumps(control, indent=2) + '\n')
        (evidence / 'prepared-plugin.json').write_text(json.dumps(actual, indent=2) + '\n')
        assert actual['origin'] == control['origin']
        assert actual['argv'] == control['argv']
        assert actual['cwd'] == control['cwd']
        assert actual['cargoVersion'] == expected_version
        assert actual['rustc'] == environment.get('RUSTC')
        # The opt-in stable package deliberately prefixes its SDK/compiler. The
        # default package must retain the exact caller PATH, not wrapper utilities.
        if not tools.name.endswith('-current-stable'):
            assert actual['path'] == environment['PATH']
        else:
            assert actual['path'] == actual['callerPath']
            assert set(environment['PATH'].split(os.pathsep)) <= set(actual['path'].split(os.pathsep))
        assert actual['metadataExit'] == actual['testExit'] == 0
        assert not list((home / 'registry').glob('cache/**/*.crate'))
        assert not list((home / 'registry').glob('src/**/Cargo.toml'))
        assert not list((home / 'registry').glob('index/**/.cache/**/itoa'))
        assert (project / 'Cargo.toml').read_bytes() == original_manifest
        assert (project / 'Cargo.lock').read_bytes() == original_lock

        config = project / '.cargo/config.toml'
        config.parent.mkdir()
        marker = root / 'must-not-run'
        config.write_text('[alias]\nprobe-alias="delegation-probe read"\n'
                          'cycle-one="cycle-two"\ncycle-two="cycle-one"\n'
                          'shell-probe=' + json.dumps('!' + sys.executable + ' -c "open(\'' +
                                                      str(marker) + '\',\'w\').write(\'bad\')"') + '\n')
        call([tools / 'bin/cargo', 'probe-alias', 'alias argument'], project, environment)
        assert json.loads(report.read_text())['argv'] == ['delegation-probe', 'read', 'alias argument']
        cli_alias = ['--config', 'alias.cli-probe="delegation-probe read"', 'cli-probe', 'CLI argument']
        report.unlink()
        native_alias = call([selected, *cli_alias], project, environment, success=False)
        native_executed = report.exists()
        if native_executed:
            native_argv = json.loads(report.read_text())['argv']
            report.unlink()
        wrapped_alias = call([tools / 'bin/cargo', *cli_alias], project, environment, success=False)
        assert wrapped_alias.returncode == native_alias.returncode
        assert report.exists() == native_executed
        if native_executed:
            assert json.loads(report.read_text())['argv'] == native_argv
        (evidence / 'cli-alias-native-behavior.json').write_text(json.dumps({
            'nativeExitCode': native_alias.returncode, 'wrappedExitCode': wrapped_alias.returncode,
            'pluginExecuted': native_executed}, indent=2) + '\n')

        # Cargo's own global --config is not automatically a nested plugin Cargo
        # option. Measure the selected SDK's behavior instead of guessing that it
        # propagates. Both invocations retain the original global arguments.
        context = ['--config', 'source.crates-io.replace-with="cli-intentionally-missing"',
                   '--config', 'env.DELEGATION_SENTINEL="from-cli"', 'delegation-probe', 'read']
        native_context_env = dict(environment, CARGO_HOME=seed.get('CARGO_HOME', str(root / 'seed-home')))
        native_context = call([selected, *context], project, native_context_env, success=False)
        context_control = json.loads(report.read_text())
        wrapped_context = call([tools / 'bin/cargo', *context], project, environment, success=False)
        context_actual = json.loads(report.read_text())
        assert wrapped_context.returncode == native_context.returncode == 0
        assert context_actual['argv'] == context_control['argv']
        assert context_actual['configSentinel'] == context_control['configSentinel']
        assert context_actual['metadataExit'] == context_control['metadataExit'] == 0
        (evidence / 'cli-context-native-behavior.json').write_text(json.dumps({
            'native': context_control, 'wrapped': context_actual}, indent=2) + '\n')
        for alias in ('shell-probe', 'cycle-one'):
            result = call([tools / 'bin/cargo', alias], project, environment, success=False)
            assert result.returncode and not marker.exists()

        hijack = binaries / 'cargo-build'
        hijack.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        hijack.chmod(0o755)
        call([tools / 'bin/cargo', 'build', '--offline', '--locked', '--quiet'], project, environment)
        assert not marker.exists(), 'an external file replaced native Cargo build'

        # Mutation is native ownership: no finally block may discard either file,
        # reinterpret the exit status, or silently move the recorded capture.
        result = call([tools / 'bin/cargo', 'delegation-probe', 'edit'], project, environment, success=False)
        assert result.returncode == 77
        assert tomllib.loads((project / 'Cargo.toml').read_text())['package']['version'] == '0.2.0'
        assert next(package for package in tomllib.loads((project / 'Cargo.lock').read_text())['package']
                    if package['name'] == 'greet')['version'] == '0.2.0'
        assert (project / 'cargo-purifier.lock.json').read_bytes() == capture
        assert call([tools / 'bin/cargo', 'metadata', '--offline', '--locked'],
                    project, environment, success=False).returncode
        # Reset only our test fixture after observing the preserved native edits.
        (project / 'Cargo.toml').write_bytes(original_manifest)
        (project / 'Cargo.lock').write_bytes(original_lock)

        call([tools / 'bin/cargo', 'nextest', 'run', '--offline', '--locked', '--test-threads', '1'],
             project, environment)
        if '--ordinary-only' in sys.argv[3:]:
            print(json.dumps({'ordinaryPlugin': True, 'nativeDispatch': True, 'aliases': True,
                              'nativeMutationPreserved': True, 'actualNextest': True,
                              'providedControlsExecuted': False}))
            sys.exit(0)

    # An independent, already locked SDK produces a real Nix Cargo projection.
    # Export a standalone ffi producer: exporting a package from the full
    # workspace would also export its other crates, including our used itoa.
    snapshot = json.loads(call(['nix', 'flake', 'archive', '--json', '--no-write-lock-file', sdk_source],
                               root, environment).stdout)['path']
    system = call(['nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem'], root, environment).stdout.strip()
    producer = root / 'producer'
    producer.mkdir()
    shutil.copytree(sdk_source / 'tests/fixtures/workspace/ffi', producer / 'crate')
    call([tools / 'bin/cargo-purifier', 'lock'], producer / 'crate', seed)
    (producer / 'flake.nix').write_text('''{
      inputs.sdk.url = "path:SDK";
      outputs = { sdk, ... }: let
        pkgs = import sdk.inputs.nixpkgs { system="SYSTEM"; overlays=[sdk.overlays.default]; };
        project = pkgs.cargo-purifier.buildCargoProject { src=./crate; };
      in { packages.SYSTEM.default=project.release.ffi; };
    }'''.replace('SDK', snapshot).replace('SYSTEM', system))
    call(['nix', 'flake', 'lock', 'path:' + str(producer)], root, environment)
    call([tools / 'bin/cargo-purifier', 'lock', '--input', 'path:' + str(producer)], project, seed)
    call([tools / 'bin/cargo-purifier', 'prefetch'], project, seed)
    call([tools / 'bin/cargo', 'delegation-probe', 'read'], project, environment)
    assert json.loads(report.read_text())['testExit'] == 0, 'an unused catalog prohibited ordinary delegation'

    provided = root / 'provided'
    (provided / 'src').mkdir(parents=True)
    (provided / 'Cargo.toml').write_text('[package]\nname="consumer"\nversion="1.0.0"\nedition="2021"\n[dependencies]\nffi="99"\n')
    (provided / 'src/lib.rs').write_text('pub fn consumer() {}\n')
    call([tools / 'bin/cargo-purifier', 'lock', '--input', 'path:' + str(producer)], provided, seed)
    call([tools / 'bin/cargo-purifier', 'prefetch'], provided, seed)
    before = {name: (provided / name).read_bytes() for name in ('Cargo.toml', 'Cargo.lock', 'cargo-purifier.lock.json')}
    report.unlink()
    result = call([tools / 'bin/cargo', 'delegation-probe', 'read'], provided, environment, success=False)
    assert result.returncode and 'synthetic Nix-provided dependency bindings' in result.stderr
    assert not report.exists(), 'the unsupported plugin was executed'
    assert before == {name: (provided / name).read_bytes() for name in before}
    print(json.dumps({'ordinaryControlsExecuted': '--provided-only' not in sys.argv[3:],
                      'unusedCatalog': True, 'providedRefusedBeforeExecution': True}))
