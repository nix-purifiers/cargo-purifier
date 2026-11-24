# SPDX-License-Identifier: MIT
"""Materialize Nix crate inputs for Cargo without recording their checksums."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

import tomlkit

from cargo_graph import VersionSelector
from cargo_repository import dependency_tables, bind_supplied, crate_manifest, extract_crate, writable_copy, write_document


def input_packages(views):
    result = {}
    for view in views:
        repository = Path(view['repository'])
        catalog = json.loads(Path(view.get('catalog', repository / '.catalog')).read_text())
        if catalog.get('format') != 'cargo':
            raise ValueError(f'not a Cargo repository: {repository}')
        for entry in catalog['packages']:
            coordinate = entry['name'], entry['version']
            expected = entry['name'] + '-' + entry['version'] + '.crate'
            if Path(expected).name != expected or entry['archive'] != expected:
                raise ValueError('invalid Cargo input archive path')
            data = (repository / expected).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry['checksum']:
                raise ValueError(f'corrupt Cargo input {coordinate}')
            manifest = crate_manifest(data)
            if (manifest['package']['name'], manifest['package']['version']) != coordinate:
                raise ValueError(f'inconsistent Cargo input {coordinate}')
            if coordinate in result and result[coordinate][0] != entry:
                raise ValueError(f'conflicting Cargo input {coordinate}')
            result[coordinate] = entry, data
    return result


def lock_checksums(data, entries, strip=False):
    """The stored lock omits supplied hashes; Cargo sees a hydrated copy."""
    checksums = {(entry['name'], entry['version']): entry['checksum'] for entry in entries}
    document = tomlkit.parse(data.decode())
    for package in document.get('package', []):
        coordinate = str(package['name']), str(package['version'])
        if coordinate not in checksums or 'source' not in package:
            continue
        if strip:
            package.pop('checksum', None)
        else:
            actual = package.get('checksum')
            if actual is not None and actual != checksums[coordinate]:
                raise ValueError(f'Cargo input checksum conflicts with Nix: {coordinate}')
            package['checksum'] = checksums[coordinate]
    return tomlkit.dumps(document).encode()


def candidates_of(entries):
    candidates = {}
    for entry in entries:
        candidates.setdefault(entry['name'], []).append(entry)
    return candidates


def prepare_vendor(base, views, out, cargo):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    base = Path(base)
    for source in base.iterdir():
        if source.is_dir() and source.name != '.cargo':
            writable_copy(source, out / source.name)
    inputs = input_packages(views)
    for coordinate, (entry, data) in inputs.items():
        extracted = extract_crate(data, out, *coordinate)
        checksum = extracted / '.cargo-checksum.json'
        checksum.write_text(json.dumps({'files': {}, 'package': entry['checksum']}))
    entries = [entry for entry, _ in inputs.values()]
    with tempfile.TemporaryDirectory(prefix='cargo-vendor-inputs-') as temporary:
        environment = dict(os.environ, CARGO_PURIFIER='0', CARGO_HOME=str(Path(temporary) / 'home'))
        selector = VersionSelector(cargo, environment, Path(temporary) / 'selectors')
        candidates = candidates_of(entries)
        manifests = list(out.glob('*/Cargo.toml')) + list(out.glob('.package-registries/*/*/Cargo.toml'))
        for manifest in manifests:
            if not manifest.is_file():
                continue
            document = tomlkit.parse(manifest.read_text())
            # Supplied manifests already carry exact canonical dependencies.
            package = document['package']
            if (str(package['name']), str(package['version'])) in inputs:
                continue
            bind_supplied(document, candidates, selector)
            write_document(manifest, document)
    config = tomlkit.parse((base / '.cargo/config.toml').read_text())
    sources = config.setdefault('source', tomlkit.table())
    # Keep registry/Git replacement chains distinct. Supplied crates belong
    # to the explicit crates.io patch namespace, not every source directory.
    current, visited = 'crates-io', set()
    destination = None
    while current not in visited:
        visited.add(current)
        table = sources.get(current, {})
        if 'directory' in table:
            value = str(table['directory'])
            if value == 'cargo-vendor-dir':
                destination = out
            elif value.startswith('cargo-vendor-dir/'):
                destination = out / value.removeprefix('cargo-vendor-dir/')
            else:
                destination = Path(value.replace('@vendor@', str(out)))
            break
        current = table.get('replace-with')
        if not current:
            break
    if destination is None:
        sources.setdefault('crates-io', tomlkit.table())['replace-with'] = 'purifier-inputs'
        sources['purifier-inputs'] = {'directory': '@vendor@/.provided-registry'}
        destination = out / '.provided-registry'
    if not destination.resolve().is_relative_to(out.resolve()):
        raise ValueError('provided registry namespace escapes vendor output')
    destination.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        name = entry['name'] + '-' + entry['version']
        source, target = out / name, destination / name
        if source != target:
            if target.exists():
                actual = json.loads((target / '.cargo-checksum.json').read_text()).get('package')
                if actual != entry['checksum']:
                    raise ValueError('conflicting provided registry crate: ' + name)
            else:
                target.symlink_to(os.path.relpath(source, destination))
    patches = config.setdefault('patch', tomlkit.table()).setdefault('crates-io', tomlkit.table())
    for entry in entries:
        alias = 'purifier-' + hashlib.sha256((entry['name'] + '@' + entry['version']).encode()).hexdigest()[:16]
        patches[alias] = {'package': entry['name'],
                          'path': '@vendor@/' + entry['name'] + '-' + entry['version']}
    (out / '.cargo').mkdir()
    (out / '.cargo/config.toml').write_text(tomlkit.dumps(config))
    (out / '.provided.json').write_text(json.dumps(entries, sort_keys=True))
    (out / 'Cargo.lock').write_bytes(lock_checksums((base / 'Cargo.lock').read_bytes(), entries))


def materialize(root, vendor, cargo, check=True):
    root, vendor = Path(root).resolve(), Path(vendor).resolve()
    entries = json.loads((vendor / '.provided.json').read_text())
    candidates = candidates_of(entries)
    with tempfile.TemporaryDirectory(prefix='cargo-workspace-inputs-') as temporary:
        temporary = Path(temporary)
        environment = dict(os.environ, CARGO_PURIFIER='0', CARGO_HOME=str(temporary / 'home'))
        selector = VersionSelector(cargo, environment, temporary / 'selectors')
        manifests, pending = set(), [root / 'Cargo.toml']
        while pending:
            manifest = pending.pop()
            if manifest in manifests:
                continue
            metadata = json.loads(subprocess.check_output(
                [cargo, 'metadata', '--offline', '--no-deps', '--format-version', '1',
                 '--manifest-path', str(manifest)], cwd=manifest.parent, env=environment, text=True))
            for package in metadata['packages']:
                path = Path(package['manifest_path'])
                if path not in manifests:
                    document = tomlkit.parse(path.read_text())
                    bind_supplied(document, candidates, selector)
                    write_document(path, document)
                    manifests.add(path)
                for dependency in package['dependencies']:
                    if dependency.get('path'):
                        child = Path(dependency['path']) / 'Cargo.toml'
                        if child not in manifests:
                            pending.append(child)
            workspace = Path(metadata['workspace_root']) / 'Cargo.toml'
            if workspace not in manifests:
                document = tomlkit.parse(workspace.read_text())
                bind_supplied(document, candidates, selector)
                write_document(workspace, document)
                manifests.add(workspace)
        lock = root / 'Cargo.lock'
        lock.write_bytes(lock_checksums(lock.read_bytes(), entries))
        configuration = temporary / 'config.toml'
        configuration.write_text((vendor / '.cargo/config.toml').read_text()
                                 .replace('"cargo-vendor-dir"', json.dumps(str(vendor)))
                                 .replace('@vendor@', str(vendor)))
        hydrate_lock(root, vendor, cargo, configuration, entries, check)




def workspace_manifests(root, cargo):
    """Discover workspace members and local path dependencies with Cargo."""
    root = Path(root).resolve()
    manifests, pending = set(), [root / 'Cargo.toml']
    environment = dict(os.environ, CARGO_PURIFIER='0')
    while pending:
        manifest = pending.pop()
        if manifest in manifests:
            continue
        metadata = json.loads(subprocess.check_output(
            [cargo, 'metadata', '--offline', '--no-deps', '--format-version', '1',
             '--manifest-path', str(manifest)], cwd=manifest.parent, env=environment, text=True))
        manifests.add(Path(metadata['workspace_root']) / 'Cargo.toml')
        for package in metadata['packages']:
            path = Path(package['manifest_path'])
            manifests.add(path)
            for dependency in package['dependencies']:
                if dependency.get('path'):
                    child = Path(dependency['path']) / 'Cargo.toml'
                    if child not in manifests:
                        pending.append(child)
    return manifests


def restore_bindings(original, changed, entries):
    """Keep native dependency edits, replacing only our generated bindings."""
    candidates = candidates_of(entries)
    original = tomlkit.parse(original.decode())
    changed = tomlkit.parse(changed.decode())
    old_tables = {(kind, target): table for table, kind, target in dependency_tables(original)}
    tables = list(dependency_tables(changed))
    if 'dependencies' in original.get('workspace', {}):
        old_tables[('workspace', None)] = original['workspace']['dependencies']
    if 'dependencies' in changed.get('workspace', {}):
        tables.append((changed['workspace']['dependencies'], 'workspace', None))
    for table, kind, target in tables:
        old = old_tables.get((kind, target), {})
        for alias, value in list(table.items()):
            spec = {'version': str(value)} if isinstance(value, str) else value
            name = spec.get('package', alias)
            choices = candidates.get(name, [])
            if not choices:
                continue
            if alias in old:
                previous = old[alias]
                previous = {'version': str(previous)} if isinstance(previous, str) else previous
                replacement = tomlkit.inline_table()
                replacement.update({key: item for key, item in spec.items()
                                    if key not in {'version', 'path', 'git', 'branch', 'tag', 'rev', 'registry', 'registry-index'}})
                for key in ('version', 'path', 'git', 'branch', 'tag', 'rev', 'registry', 'registry-index', 'workspace'):
                    if key in previous:
                        replacement[key] = previous[key]
                table[alias] = replacement
            else:
                matching = [entry for entry in choices if entry['version'] == str(spec.get('version', '')).removeprefix('=')]
                if len(matching) != 1:
                    raise ValueError(f'cannot restore source version for new dependency {alias}')
                replacement = tomlkit.inline_table()
                replacement.update({key: item for key, item in spec.items() if key not in {'version', 'path'}})
                replacement['version'] = matching[0]['sourceVersion']
                table[alias] = replacement
    return tomlkit.dumps(changed).encode()


@contextmanager
def borrowed_manifests(root, entries, cargo, mutation=False):
    originals = {path: path.read_bytes() for path in workspace_manifests(root, cargo)}
    with tempfile.TemporaryDirectory(prefix='cargo-bindings-') as temporary:
        environment = dict(os.environ, CARGO_PURIFIER='0', CARGO_HOME=str(Path(temporary) / 'home'))
        selector = VersionSelector(cargo, environment, Path(temporary) / 'selectors')
        candidates = candidates_of(entries)
        try:
            for path, original in originals.items():
                document = tomlkit.parse(original.decode())
                bind_supplied(document, candidates, selector)
                write_document(path, document)
            yield
        except BaseException:
            for path, original in originals.items():
                path.write_bytes(original)
            raise
        else:
            for path, original in originals.items():
                changed = path.read_bytes()
                path.write_bytes(restore_bindings(original, changed, entries) if mutation else original)




def portable_lock(data, entries):
    """Keep ordinary Cargo records; Nix supplies the other package records."""
    document = tomlkit.parse(data.decode())
    supplied = {(entry['name'], entry['version']) for entry in entries}
    packages = list(document.get('package', []))
    by_name = {}
    for package in packages:
        by_name.setdefault(str(package['name']), []).append(package)
    retained = tomlkit.aot()
    for package in packages:
        coordinate = str(package['name']), str(package['version'])
        if coordinate in supplied and 'source' not in package:
            continue
        dependencies = []
        for dependency in package.get('dependencies', []):
            parts = str(dependency).split(' ')
            choices = by_name.get(parts[0], [])
            if len(parts) > 1:
                choices = [choice for choice in choices if str(choice['version']) == parts[1]]
            if len(choices) == 1 and 'source' not in choices[0] and \
                    (str(choices[0]['name']), str(choices[0]['version'])) in supplied:
                continue
            dependencies.append(dependency)
        if dependencies:
            package['dependencies'] = dependencies
        else:
            package.pop('dependencies', None)
        retained.append(package)
    document['package'] = retained
    if 'unused' in document.get('patch', {}):
        remaining = tomlkit.aot()
        supplied_names = {entry['name'] for entry in entries}
        for patch in document['patch']['unused']:
            if str(patch['name']) in supplied_names and '-nix.h' in str(patch['version']):
                continue
            remaining.append(patch)
        if remaining:
            document['patch']['unused'] = remaining
        else:
            document.pop('patch')
    return tomlkit.dumps(document).encode()


def hydrate_lock(root, vendor, cargo, configuration, entries, check=True):
    """The vendor contains only captured ordinary versions and Nix inputs."""
    root = Path(root)
    lock = root / 'Cargo.lock'
    original = lock.read_bytes()
    environment = dict(os.environ, CARGO_PURIFIER='0')
    subprocess.run([cargo, '--config', str(configuration), 'generate-lockfile', '--offline'],
                   cwd=root, env=environment, check=True)
    if check and tomllib.loads(portable_lock(lock.read_bytes(), entries).decode()) != tomllib.loads(portable_lock(original, entries).decode()):
        lock.write_bytes(original)
        raise ValueError('the locked Cargo graph changed; run cargo-purifier lock')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='action', required=True)
    vendor = commands.add_parser('vendor')
    vendor.add_argument('request')
    vendor.add_argument('out')
    vendor.add_argument('cargo')
    workspace = commands.add_parser('materialize')
    workspace.add_argument('root')
    workspace.add_argument('vendor')
    workspace.add_argument('cargo')
    options = parser.parse_args()
    if options.action == 'vendor':
        request = json.loads(Path(options.request).read_text())
        prepare_vendor(request['base'], request['dependencies'], options.out, options.cargo)
    else:
        materialize(options.root, options.vendor, options.cargo)
