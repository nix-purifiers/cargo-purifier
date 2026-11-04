# SPDX-License-Identifier: MIT
"""Native Cargo version selection and stable identities for resolved graphs."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def namespace_version(version, identity):
    # Keep numeric version fields: macro/build-script pairs use these for ABI
    # names (for example serde's private module), independently of resolution.
    base, separator, metadata = version.partition('+')
    base += ('.nix.h' if '-' in base else '-nix.h') + identity
    return base + (separator + metadata if separator else '')


class VersionSelector:
    """Use Cargo's actual resolver; candidate indexes contain no crate bodies."""
    def __init__(self, cargo, environment, directory):
        self.cargo, self.environment, self.directory = cargo, environment, Path(directory)
        self.cache = {}
        self.directory.mkdir(parents=True, exist_ok=True)

    def select(self, requirement, versions):
        versions = tuple(sorted(set(versions)))
        key = requirement, versions
        if key in self.cache:
            return self.cache[key]
        if not versions:
            raise ValueError('no candidate versions')
        with tempfile.TemporaryDirectory(prefix='select-', dir=self.directory) as temporary:
            root = Path(temporary)
            (root / 'src').mkdir()
            (root / 'src/lib.rs').write_text('')
            (root / 'Cargo.toml').write_text('[package]\nname="purifier-selector"\nversion="0.0.0"\nauthors=[]\n[workspace]\n[dependencies]\npurifier-candidate=' + json.dumps(requirement) + '\n')
            index = root / 'registry/index/pu/ri'
            index.mkdir(parents=True)
            records = [{'name': 'purifier-candidate', 'vers': version, 'deps': [],
                        'features': {}, 'yanked': False, 'cksum': '0' * 64} for version in versions]
            (index / 'purifier-candidate').write_text(''.join(json.dumps(record) + '\n' for record in records))
            # The probe has no pre-existing project configuration. Use Cargo's
            # longstanding project config file instead of the newer --config
            # CLI, and the default 2015 edition rather than imposing 2021.
            config = root / '.cargo/config'
            config.parent.mkdir()
            config.write_text('[source.crates-io]\nreplace-with="candidates"\n[source.candidates]\nlocal-registry=' + json.dumps(str(root / 'registry')) + '\n')
            result = subprocess.run([self.cargo, 'generate-lockfile', '--offline', '--quiet'],
                                    cwd=root, env=self.environment, text=True, capture_output=True)
            if result.returncode:
                raise ValueError(f'Cargo cannot select {requirement!r} from {versions}: {result.stderr.strip()}')
            lock = tomllib.loads((root / 'Cargo.lock').read_text())
            selected = next(package['version'] for package in lock['package'] if package['name'] == 'purifier-candidate')
        self.cache[key] = selected
        return selected


def graph_versions(nodes, supplied, toolchain):
    """Hash each node's source and reachable graph, including dev cycles.

    Source keys/roles are canonical origins, never temporary extraction paths.
    Independent producers of a shared registry/git dependency get the same ID.
    """
    indices, low, stack, active, components = {}, {}, [], set(), []
    def visit(key):
        indices[key] = low[key] = len(indices)
        stack.append(key)
        active.add(key)
        for edge in nodes[key]['edges']:
            child = edge['package']
            if child in supplied:
                continue
            if child not in indices:
                visit(child)
                low[key] = min(low[key], low[child])
            elif child in active:
                low[key] = min(low[key], indices[child])
        if low[key] == indices[key]:
            component = []
            while True:
                child = stack.pop()
                active.remove(child)
                component.append(child)
                if child == key:
                    break
            components.append(component)
    for key in sorted(nodes):
        if key not in indices:
            visit(key)
    versions = dict(supplied)
    for component in components:
        members = set(component)
        records = []
        for key in component:
            node = nodes[key]
            edges = []
            for edge in node['edges']:
                child = edge['package']
                label = {field: value for field, value in edge.items() if field != 'package'}
                label['dependency'] = nodes[child]['role'] if child in members else versions[child]
                edges.append(label)
            records.append({'source': node['identity'], 'role': node['role'],
                            'edges': sorted(edges, key=lambda edge: json.dumps(edge, sort_keys=True))})
        component_hash = digest({'toolchain': toolchain, 'nodes': sorted(records, key=lambda node: node['role'])})
        for key in component:
            versions[key] = namespace_version(nodes[key]['version'], digest([component_hash, nodes[key]['role']]))
    return versions
