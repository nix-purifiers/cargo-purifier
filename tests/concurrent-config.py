# SPDX-License-Identifier: MIT
"""Hold a real wrapper writer at a FIFO while native Cargo reads its config."""
import argparse
import errno
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('tools', 'cargo', 'fixture', 'vendor'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
        root = Path(temporary)
        project = root / 'workspace'
        shutil.copytree(args.fixture, project)
        for path in (project, *project.rglob('*')):
            path.chmod(0o755 if path.is_dir() else 0o644)
        state = project / 'target/cargo-purifier'
        state.mkdir(parents=True)
        vendor = root / 'vendor'
        vendor.mkdir()
        for path in args.vendor.iterdir():
            if path.name != '.cargo':
                (vendor / path.name).symlink_to(path)
        (vendor / '.cargo').mkdir()
        template = (args.vendor / '.cargo/config.toml').read_text()
        (vendor / '.cargo/config.toml').write_text(template)
        config = state / 'config.toml'
        (state / 'vendor').symlink_to(vendor)
        environment = dict(os.environ, HOME=str(root / 'home'),
                           CARGO_HOME=str(root / 'cargo-home'),
                           CARGO_PURIFIER_REAL_CARGO=str(args.cargo),
                           PATH=str(args.tools / 'bin') + ':' + os.environ['PATH'])
        digest = subprocess.check_output(
            [str(args.tools / 'bin/cargo-purifier'), '_vendor-stamp',
             str(project / 'Cargo.lock'), str(project / 'cargo-purifier.lock.json')],
            env=environment, text=True)
        (state / 'stamp').write_text(digest)
        worktree = Path(subprocess.check_output(
            [str(args.tools / 'bin/cargo-purifier'), '_writable-vendor', str(state), str(vendor)],
            cwd=project, env=environment, text=True).strip())
        original = template.replace('directory = "cargo-vendor-dir"',
                                    f'directory = "{worktree}"').encode()
        config.write_bytes(original)
        reader = [str(args.cargo), '--config', str(config), 'metadata',
                  '--offline', '--locked', '--format-version', '1']

        def read_graph():
            return subprocess.run(reader, cwd=project, env=environment,
                                  capture_output=True, text=True, timeout=30)

        before = read_graph()
        assert before.returncode == 0, before.stderr
        fifo = worktree / '.cargo/config.toml'
        fifo.unlink()
        os.mkfifo(fifo)
        writer = subprocess.Popen([str(args.tools / 'bin/cargo'), 'metadata',
                                   '--locked', '--format-version', '1'],
                                  cwd=project, env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        descriptor = None
        try:
            deadline = time.monotonic() + 30
            while descriptor is None:
                if writer.poll() is not None:
                    raise AssertionError(writer.communicate())
                try:
                    descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as error:
                    if error.errno != errno.ENXIO:
                        raise
                    if time.monotonic() > deadline:
                        raise AssertionError('wrapper did not reach config generation')
                    time.sleep(0.01)
            # Opening the FIFO proves sed has started, after its output file
            # was opened. No timing guess controls the competing reader.
            visible = config.read_bytes()
            during = read_graph()
            os.write(descriptor, template.encode())
            os.close(descriptor)
            descriptor = None
            _, stderr = writer.communicate(timeout=30)
            assert writer.returncode == 0, stderr
            assert visible == original, 'writer exposed a partial vendor configuration'
            assert during.returncode == 0, during.stderr
            print('Concurrent native Cargo reader retained the complete offline graph')
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if writer.poll() is None:
                writer.kill()
                writer.communicate()

        # Failure to generate a replacement must leave the published config
        # untouched, with no temporary files left behind.
        published_state = sorted(path.name for path in state.iterdir())
        fifo.unlink()
        failed = subprocess.run([str(args.tools / 'bin/cargo'), 'metadata',
                                 '--locked', '--format-version', '1'],
                                cwd=project, env=environment,
                                capture_output=True, text=True, timeout=30)
        assert failed.returncode != 0, 'missing vendor template unexpectedly succeeded'
        assert config.read_bytes() == original, 'failed writer damaged published config'
        assert sorted(path.name for path in state.iterdir()) == published_state
        assert read_graph().returncode == 0
        print('Failed config generation preserved the published graph and cleaned up')


if __name__ == '__main__':
    main()
