# SPDX-License-Identifier: MIT
"""Export Cargo-native crates and a registry index from a Nix source graph."""
from collections.abc import Mapping
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

import tomlkit
import cargo_publication

from cargo_graph import VersionSelector, digest, graph_versions

CRATES_IO = 'https://github.com/rust-lang/crates.io-index'
KINDS = {'dependencies': 'normal', 'dev-dependencies': 'dev', 'build-dependencies': 'build'}


def prefix(name):
    name = name.lower()
    if len(name) < 3:
        return str(len(name))
    if len(name) == 3:
        return '3/' + name[0]
    return name[:2] + '/' + name[2:4]


def crate_manifest(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        candidates = [entry for entry in archive if entry.name.count('/') == 1 and entry.name.endswith('/Cargo.toml')]
        if len(candidates) != 1:
            raise ValueError('crate must contain one normalized Cargo.toml')
        return tomllib.loads(archive.extractfile(candidates[0]).read().decode())


def index_record(manifest, checksum, provided):
    dependencies = []
    def add(tables, target):
        for key, kind in KINDS.items():
            for alias, value in tables.get(key, {}).items():
                spec = {'version': value} if isinstance(value, str) else value
                if 'version' not in spec:
                    raise ValueError(f'unpublishable dependency {alias}: no version')
                name = spec.get('package', alias)
                dependencies.append({
                    'name': alias, 'package': name if name != alias else None,
                    'req': spec['version'], 'kind': kind, 'target': target,
                    'features': spec.get('features', []), 'optional': spec.get('optional', False),
                    'default_features': spec.get('default-features', True),
                    'registry': None if (name, spec['version'].removeprefix('=')) in provided else spec.get('registry-index', CRATES_IO),
                })
    add(manifest, None)
    for target, tables in manifest.get('target', {}).items():
        add(tables, target)
    package = manifest['package']
    return {'name': package['name'], 'vers': package['version'], 'deps': dependencies,
            'cksum': checksum, 'features': {}, 'features2': manifest.get('features', {}),
            'v': 2, 'yanked': False, 'links': package.get('links'),
            'rust_version': package.get('rust-version')}


def extract_crate(data, sources, name, version):
    expected = name + '-' + version
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for entry in archive.getmembers():
            parts = PurePosixPath(entry.name).parts
            if not parts or parts[0] != expected or '..' in parts or entry.name.startswith('/'):
                raise ValueError(f'invalid crate member {entry.name}')
        archive.extractall(sources, filter='data')
    return sources / expected


def dependency_tables(document):
    for key, kind in KINDS.items():
        if key in document:
            yield document[key], kind, None
    for target, tables in document.get('target', {}).items():
        for key, kind in KINDS.items():
            if key in tables:
                yield tables[key], kind, target


def writable_copy(source, target):
    shutil.copytree(source, target)
    for entry in [target, *target.rglob('*')]:
        if not entry.is_symlink():
            entry.chmod(0o755 if entry.is_dir() else entry.stat().st_mode | 0o200)


def write_document(path, document):
    path.write_text(tomlkit.dumps(document))
    checksum = path.parent / '.cargo-checksum.json'
    if checksum.exists():
        record = json.loads(checksum.read_text())
        if record.get('files'):
            record['files']['Cargo.toml'] = hashlib.sha256(path.read_bytes()).hexdigest()
            checksum.write_text(json.dumps(record))


def bind_supplied(document, candidates, selector):
    def bind(table):
        for alias, value in list(table.items()):
            spec = {'version': str(value)} if isinstance(value, str) else value
            if not isinstance(spec, Mapping) or spec.get('workspace') or 'path' in spec:
                continue
            name = spec.get('package', alias)
            choices = candidates.get(name, [])
            if not choices:
                continue
            requirement = str(spec.get('version', '*'))
            exact = requirement.removeprefix('=')
            matches = [entry for entry in choices if entry['version'] == exact]
            if not matches:
                if len(choices) == 1:
                    matches = choices
                else:
                    selected = selector.select(requirement, [entry['sourceVersion'] for entry in choices])
                    matches = [entry for entry in choices if entry['sourceVersion'] == selected]
            if len(matches) != 1:
                raise ValueError(f'ambiguous Nix crate input for {name} {requirement}')
            replacement = tomlkit.inline_table()
            replacement.update({key: item for key, item in spec.items()
                                if key not in {'version', 'git', 'branch', 'tag', 'rev', 'registry', 'registry-index'}})
            replacement['version'] = '=' + matches[0]['version']
            table[alias] = replacement
    for table, _, _ in dependency_tables(document):
        bind(table)
    if 'dependencies' in document.get('workspace', {}):
        bind(document['workspace']['dependencies'])


def target_key(target):
    return re.sub(r'\\s+', '', target or '')


def rewrite_resolved(document, package, edges, versions, paths, directory, workspace_defaults):
    for table, kind, target in dependency_tables(document):
        for alias, value in list(table.items()):
            spec = {'version': str(value)} if isinstance(value, str) else dict(value)
            if spec.pop('workspace', False):
                inherited = workspace_defaults[alias]
                inherited = {'version': str(inherited)} if isinstance(inherited, str) else dict(inherited)
                inherited_features = list(inherited.get('features', []))
                inherited.update(spec)
                inherited['features'] = sorted(set(inherited_features + list(spec.get('features', []))))
                spec = inherited
            matches = {edge['package'] for edge in edges
                       if edge['alias'] == alias.replace('-', '_')
                       and edge['kind'] == kind and target_key(edge['target']) == target_key(target)}
            if not matches:
                matches = {edge['package'] for edge in edges if edge['alias'] == alias.replace('-', '_')}
            if not matches:
                continue
            if len(matches) != 1:
                raise ValueError(f'ambiguous resolved dependency {package["name"]}:{alias}')
            child = next(iter(matches))
            replacement = tomlkit.inline_table()
            replacement.update({key: item for key, item in spec.items()
                                if key not in {'version', 'path', 'git', 'branch', 'tag', 'rev', 'registry', 'registry-index'}})
            replacement['version'] = '=' + versions[child]
            table[alias] = replacement


def project(request, out, catalog_path, cargo):
    if not re.fullmatch('[0-9a-f]{64}', request.get('identity', '')):
        raise ValueError('Cargo projection requires an identity supplied by Nix')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    sources = out / '.sources'
    sources.mkdir()
    packages, records, supplied_paths = {}, {}, {}
    candidates = {}
    for dependency in request.get('dependencies', []):
        repository = Path(dependency['repository'])
        catalog = json.loads(Path(dependency.get('catalog', repository / '.catalog')).read_text())
        if catalog.get('format') != 'cargo':
            raise ValueError(f'not a Cargo repository: {repository}')
        for entry in catalog['packages']:
            name, version = entry['name'], entry['version']
            coordinate = name, version
            expected = name + '-' + version + '.crate'
            if Path(expected).name != expected or entry['archive'] != expected:
                raise ValueError(f'invalid Nix crate archive path for {name}')
            data = (repository / expected).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry['checksum']:
                raise ValueError(f'corrupt Nix crate input {name}')
            manifest = crate_manifest(data)
            if manifest['package']['name'] != name or manifest['package']['version'] != version:
                raise ValueError(f'inconsistent crate input {name}')
            if coordinate in packages:
                if packages[coordinate] != entry:
                    raise ValueError(f'conflicting Nix crate inputs for {name} {version}')
                continue
            (out / expected).write_bytes(data)
            supplied_paths[coordinate] = extract_crate(data, sources, name, version)
            index_rows = [json.loads(line) for line in
                          (repository / 'index' / prefix(name) / name.lower()).read_text().splitlines()]
            records[coordinate] = next(row for row in index_rows if row['vers'] == version)
            packages[coordinate] = entry
            candidates.setdefault(name, []).append(entry)

    with tempfile.TemporaryDirectory(prefix='cargo-project-') as temporary:
        temporary = Path(temporary)
        source = temporary / 'source'
        shutil.copytree(request['src'], source, ignore=shutil.ignore_patterns('.git', 'target', 'result', 'result-*'))
        for entry in [source, *source.rglob('*')]:
            if not entry.is_symlink():
                entry.chmod(0o755 if entry.is_dir() else entry.stat().st_mode | 0o200)
        root = source / request.get('workspace', '.')
        if request.get('cargoLock'):
            shutil.copyfile(request['cargoLock'], root / 'Cargo.lock')
        environment = dict(os.environ, CARGO_PURIFIER='0', CARGO_HOME=str(temporary / 'home'),
                           CARGO_TARGET_DIR=str(temporary / 'target'))
        combined = temporary / 'combined'
        combined.mkdir()
        config = tomlkit.document()
        vendor = request.get('vendor')
        if vendor:
            vendor = Path(vendor)
            config = tomlkit.parse((vendor / '.cargo/config.toml').read_text().replace('@vendor@', str(vendor)))
            for directory in sorted(vendor.iterdir()):
                if (directory / 'Cargo.toml').is_file():
                    package = tomllib.loads((directory / 'Cargo.toml').read_text())['package']
                    if (package['name'], package['version']) not in packages:
                        writable_copy(directory, combined / ('original-' + directory.name))
        for coordinate, path in supplied_paths.items():
            staged = combined / ('input-' + coordinate[0] + '-' + coordinate[1])
            writable_copy(path, staged)
            (staged / '.cargo-checksum.json').write_text(json.dumps({
                'files': {}, 'package': packages[coordinate]['checksum']}))
        source_config = config.setdefault('source', tomlkit.table())
        directory_names = [name for name, table in source_config.items() if 'directory' in table]
        replacement = directory_names[0] if directory_names else 'purifier-projection'
        for name, table in list(source_config.items()):
            if table.get('replace-with') in directory_names:
                table['replace-with'] = replacement
            if name in directory_names and name != replacement:
                del source_config[name]
        source_config.setdefault('crates-io', tomlkit.table())['replace-with'] = replacement
        source_config[replacement] = {'directory': str(combined)}
        config_path = temporary / 'config.toml'
        config_path.write_text(tomlkit.dumps(config))
        selector = VersionSelector(cargo, environment, temporary / 'selectors')
        manifests = [*source.rglob('Cargo.toml')]
        manifests += [directory / 'Cargo.toml' for directory in combined.iterdir()
                      if directory.name.startswith('original-')]
        publication_adjustments = cargo_publication.prepare(manifests, request.get('publicationSourceArchives', []))
        for manifest in manifests:
            document = tomlkit.parse(manifest.read_text())
            cargo_publication.omit(document, manifest, publication_adjustments)
            bind_supplied(document, candidates, selector)
            write_document(manifest, document)
        def native(*arguments, cwd=root):
            return subprocess.check_output([cargo, '--config', str(config_path), *arguments],
                                           cwd=cwd, env=environment, text=True)
        metadata = json.loads(native('metadata', '--offline', '--all-features', '--format-version', '1'))
        infos = {package['id']: package for package in metadata['packages']}
        resolved = {node['id']: node for node in metadata['resolve']['nodes']}
        supplied, nodes, paths = {}, {}, {}
        for key, package in infos.items():
            coordinate = package['name'], package['version']
            paths[key] = Path(package['manifest_path']).parent
            if coordinate in packages:
                supplied[key] = package['version']
                continue
            origin = package.get('source')
            if origin is None:
                relative = Path(package['manifest_path']).relative_to(source).as_posix()
                role = 'path:' + relative
                identity = digest([request.get('sourceIdentity', request['identity']), relative])
            else:
                role = origin + ':' + package['name'] + '@' + package['version']
                checksum_file = paths[key] / '.cargo-checksum.json'
                checksum = json.loads(checksum_file.read_text()).get('package') if checksum_file.exists() else None
                if checksum:
                    identity = 'crate-sha256:' + checksum
                else:
                    matching = [entry['hash'] for entry in request.get('git', [])
                                if entry['source'] == origin]
                    identity = matching[0] if len(matching) == 1 else request['identity']
            edges = []
            for dependency in resolved[key]['deps']:
                for kind in dependency['dep_kinds']:
                    edges.append({'alias': dependency['name'], 'kind': kind['kind'] or 'normal',
                                  'target': kind['target'], 'package': dependency['pkg']})
            nodes[key] = {'identity': identity, 'role': role, 'version': package['version'], 'edges': edges}
        versions = graph_versions(nodes, supplied, request.get('projectorIdentity', request['identity']))
        workspace_document = tomlkit.parse((root / 'Cargo.toml').read_text())
        defaults = workspace_document.get('workspace', {}).get('dependencies', {})
        # Normalize with Cargo before relocating crates: this expands workspace
        # inheritance and brings licenses/readmes outside a member into its crate.
        originals = {}
        original_versions = {key: package['version'] for key, package in infos.items()}
        for key in nodes:
            package = infos[key]
            manifest_path = Path(package['manifest_path'])
            document = tomlkit.parse(manifest_path.read_text())
            rewrite_resolved(document, package, nodes[key]['edges'], original_versions, paths,
                             manifest_path.parent, defaults)
            if package.get('source') is not None:
                document['package'].pop('workspace', None)
                document.setdefault('workspace', tomlkit.table())
            write_document(manifest_path, document)
            for reserved in ('Cargo.toml.orig', '.cargo_vcs_info.json'):
                (manifest_path.parent / reserved).unlink(missing_ok=True)
            native('package', '--offline', '--exclude-lockfile', '--no-verify', '--no-metadata',
                   '--allow-dirty', '--manifest-path', str(manifest_path))
            artifact = temporary / 'target/package' / (package['name'] + '-' + package['version'] + '.crate')
            originals[key] = artifact.read_bytes()
        final = temporary / 'final'
        final.mkdir()
        for key, package in infos.items():
            destination = final / (package['name'] + '-' + versions[key])
            if key in supplied:
                writable_copy(supplied_paths[(package['name'], package['version'])], destination)
            else:
                unpacked = extract_crate(originals[key], temporary / ('unpack-' + digest(key)),
                                         package['name'], package['version'])
                shutil.move(unpacked, destination)
                for reserved in ('Cargo.toml.orig', '.cargo_vcs_info.json', '.cargo-checksum.json'):
                    (destination / reserved).unlink(missing_ok=True)
            paths[key] = destination
        for key in nodes:
            package = infos[key]
            manifest_path = paths[key] / 'Cargo.toml'
            document = tomlkit.parse(manifest_path.read_text())
            document['package']['version'] = versions[key]
            rewrite_resolved(document, package, nodes[key]['edges'], versions, paths, paths[key], {})
            write_document(manifest_path, document)
        # All files now use stable relative locations; no temporary paths enter
        # Cargo.toml.orig or the native archive.
        for key in sorted(nodes):
            package = infos[key]
            manifest_path = paths[key] / 'Cargo.toml'
            native('package', '--offline', '--exclude-lockfile', '--no-verify', '--no-metadata',
                   '--allow-dirty', '--manifest-path', str(manifest_path))
            name, version = package['name'], versions[key]
            archive = name + '-' + version + '.crate'
            data = (temporary / 'target/package' / archive).read_bytes()
            checksum = hashlib.sha256(data).hexdigest()
            (out / archive).write_bytes(data)
            extract_crate(data, sources, name, version)
            packages[(name, version)] = {'name': name, 'version': version, 'sourceVersion': package['version'],
                                         'archive': archive, 'checksum': checksum}
            adjustment = publication_adjustments.get(Path(package['manifest_path']))
            if adjustment is not None:
                packages[(name, version)]['publicationMetadataAdjustment'] = adjustment
            records[(name, version)] = index_record(crate_manifest(data), checksum,
                                                   {(item['name'], versions[identifier]) for identifier, item in infos.items()})
        roots = [{'name': infos[key]['name'], 'version': versions[key]} for key in metadata['workspace_members']]
        (out / 'crates').mkdir()
        for entry in roots:
            archive = entry['name'] + '-' + entry['version'] + '.crate'
            (out / 'crates' / (entry['name'] + '.crate')).symlink_to('../' + archive)
    for name in sorted({coordinate[0] for coordinate in records}):
        path = out / 'index' / prefix(name) / name.lower()
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [records[coordinate] for coordinate in sorted(records) if coordinate[0] == name]
        path.write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))
    catalog = {'format': 'cargo', 'roots': sorted(roots, key=lambda entry: entry['name']),
               'packages': [packages[coordinate] for coordinate in sorted(packages)]}
    data = json.dumps(catalog, sort_keys=True, indent=2) + '\n'
    (out / '.catalog').write_text(data)
    Path(catalog_path).parent.mkdir(parents=True, exist_ok=True)
    Path(catalog_path).write_text(data)


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text())
    project(request, sys.argv[2], sys.argv[3], sys.argv[4])
