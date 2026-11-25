#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise external native locks through an actual installed Cargo wrapper."""
import json
import os
from pathlib import Path
import subprocess
import sys


def verify_external_native_lock(tools, root, environment):
    root = Path(root).resolve()
    (root / 'src').mkdir(parents=True)
    (root / 'Cargo.toml').write_text('[package]\nname="external-lock-fixture"\n'
                                    'version="1.0.0"\nedition="2021"\n')
    (root / 'src/lib.rs').write_text('pub fn value() -> u8 { 42 }\n')

    def run(*arguments, env=environment):
        return subprocess.check_output([str(arg) for arg in arguments], cwd=root,
                                       env=env, text=True)

    run(tools / 'bin/cargo-purifier', 'lock', '--no-inputs')
    capture = json.loads((root / 'cargo-purifier.lock.json').read_text())
    # Nix-input captures already use the transactional Python dispatch.
    assert 'projectInputs' not in capture
    native_lock = (root / 'Cargo.lock').read_bytes()
    external_lock = root.parent / (root.name + '-selected-Cargo.lock')
    external_lock.write_bytes(native_lock)
    original_lock = b'# Preserve the workspace-owned lock bytes.\n' + native_lock
    (root / 'Cargo.lock').write_bytes(original_lock)
    external_env = dict(environment, CARGO_PURIFIER_TOOL_LOCKFILE=str(external_lock))
    metadata = json.loads(run(tools / 'bin/cargo', 'metadata', '--offline', '--locked',
                              '--format-version', '1', env=external_env))
    assert metadata['workspace_members']
    assert (root / 'Cargo.lock').read_bytes() == original_lock, \
        'workspace lock changed after successful external-lock metadata'
    assert external_lock.read_bytes() == native_lock
    failed = subprocess.run([str(tools / 'bin/cargo'), 'check', '--features',
                             '__purifier_missing_feature__'], cwd=root,
                            env=external_env, text=True, capture_output=True)
    assert failed.returncode == 101 and '__purifier_missing_feature__' in failed.stderr
    assert (root / 'Cargo.lock').read_bytes() == original_lock, \
        'workspace lock changed after native Cargo failure'
    assert external_lock.read_bytes() == native_lock
    (root / 'Cargo.lock').unlink()
    run(tools / 'bin/cargo', 'metadata', '--offline', '--locked', '--format-version', '1',
        env=external_env)
    assert not (root / 'Cargo.lock').exists(), 'an originally absent workspace lock was left behind'
    assert external_lock.read_bytes() == native_lock
    print('PASS cargo.composition.external-native-lock-restoration')


if __name__ == '__main__':
    tools = Path(sys.argv[1]).resolve()
    environment = dict(os.environ, PATH=str(tools / 'bin') + ':' + os.environ['PATH'])
    verify_external_native_lock(tools, Path(sys.argv[2]), environment)
