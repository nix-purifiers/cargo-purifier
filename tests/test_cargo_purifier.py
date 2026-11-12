# SPDX-License-Identifier: MIT
"""Unit tests of cargo_purify: the parsing of Cargo.lock sources, the URLs
git dependencies are fetched from, cargo's configuration, and the publish
request."""

import contextlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cargo_purifier as cargo_purify


class NativeCargo(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {'PATH': str(self.root)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def executable(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n' + text)
        path.chmod(0o755)
        return path

    def test_skip_multiple_purifier_wrappers_and_preserve_host_path(self):
        first = self.executable(self.root / 'one/cargo', '# cargo-purifier native-cargo wrapper\nexit 99\n')
        second = self.executable(self.root / 'two/cargo', '# old CARGO_PURIFIER_LOCKFILE wrapper\nexit 98\n')
        host = self.executable(self.root / 'host/cargo', 'echo installed-cargo\n')
        os.environ['PATH'] = os.pathsep.join(str(p.parent) for p in (first, second, host))
        os.environ['CARGO'] = str(first)
        self.assertEqual(cargo_purify.native_cargo(), str(host))
        self.assertEqual(cargo_purify.run(['cargo', '--version']), 'installed-cargo\n')

    def test_missing_runtime_does_not_choose_a_packaged_sdk(self):
        with self.assertRaisesRegex(cargo_purify.Failure, 'no installed Cargo'):
            cargo_purify.native_cargo()

    def test_explicit_runtime_wins_and_invalid_override_does_not_fall_back(self):
        self.executable(self.root / 'cargo', 'echo path-cargo\n')
        supplied = self.executable(self.root / 'selected', 'echo supplied-cargo\n')
        os.environ['CARGO_PURIFIER_REAL_CARGO'] = str(supplied)
        self.assertEqual(cargo_purify.run(['cargo', '--version']), 'supplied-cargo\n')
        supplied.unlink()
        with self.assertRaisesRegex(cargo_purify.Failure, 'not an executable'):
            cargo_purify.native_cargo()

    def test_explicit_wrapper_is_rejected_without_recursing(self):
        wrapper = self.executable(self.root / 'cargo', '# cargo-purifier native-cargo wrapper\nexit 99\n')
        os.environ['CARGO_PURIFIER_REAL_CARGO'] = str(wrapper)
        with self.assertRaisesRegex(cargo_purify.Failure, 'purifier wrapper'):
            cargo_purify.native_cargo()

    def test_external_subcommand_uses_its_invoking_cargo(self):
        self.executable(self.root / 'cargo', 'echo path-cargo\n')
        supplied = self.executable(self.root / 'invoking/cargo', 'echo invoking-cargo\n')
        os.environ['CARGO'] = str(supplied)
        self.assertEqual(cargo_purify.run(['cargo', '--version']), 'invoking-cargo\n')

    def test_rustup_proxy_keeps_argv0_and_original_project_selection(self):
        rustup = self.executable(self.root / 'rustup',
            'case "$0" in */rustup) printf "%s-sdk (directory override)\\n" "${PWD##*/}";; '
            '*) printf "%s:%s:%s\\n" "$0" "$RUSTUP_TOOLCHAIN" "$RUSTC";; esac\n')
        proxy = self.root / 'cargo'
        proxy.symlink_to(rustup)
        os.environ['RUSTC'] = '/user/compiler-wrapper'
        original = self.root / 'original'
        copied = self.root / 'copied'
        original.mkdir()
        copied.mkdir()
        with contextlib.chdir(original):
            self.assertEqual(cargo_purify.native_cargo(), str(proxy))
        self.assertEqual(cargo_purify.run(['cargo', '--version'], cwd=copied),
                         f'{proxy}:original-sdk:/user/compiler-wrapper\n')

    def test_explicit_rustup_toolchain_is_preserved(self):
        rustup = self.executable(self.root / 'rustup', 'exit 99\n')
        (self.root / 'cargo').symlink_to(rustup)
        os.environ['RUSTUP_TOOLCHAIN'] = 'user-pinned-sdk'
        self.assertEqual(cargo_purify.native_cargo(), str(self.root / 'cargo'))
        self.assertEqual(os.environ['RUSTUP_TOOLCHAIN'], 'user-pinned-sdk')

    def test_rustup_proxy_outside_its_install_directory(self):
        rustup = self.executable(self.root / 'installation/rustup',
                                 'printf "external-sdk (directory override)\\n"\n')
        proxy = self.root / 'cargo'
        proxy.symlink_to(rustup)
        self.assertEqual(cargo_purify.native_cargo(), str(proxy))
        self.assertEqual(os.environ['RUSTUP_TOOLCHAIN'], 'external-sdk')

    def test_explicit_empty_toolchain_environment_is_not_replaced(self):
        rustup = self.executable(self.root / 'rustup', 'exit 99\n')
        (self.root / 'cargo').symlink_to(rustup)
        os.environ['RUSTUP_TOOLCHAIN'] = ''
        self.assertEqual(cargo_purify.native_cargo(), str(self.root / 'cargo'))
        self.assertEqual(os.environ['RUSTUP_TOOLCHAIN'], '')


class RuntimeMetadata(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sdk = self.root / 'sdk'
        self.manifest = self.sdk / 'lib/rustlib/src/rust/library/Cargo.toml'
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('[workspace]\n')
        environment = mock.patch.dict(os.environ, {
            'RUSTUP_TOOLCHAIN': str(self.sdk),
            'CARGO_RESOLVER_LOCKFILE_PATH': str(self.root / 'temporary/Cargo.lock'),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_redirected_sdk_metadata_is_separate_from_project_lock(self):
        self.assertTrue(cargo_purify.runtime_metadata(
            'metadata', ['metadata', '--manifest-path', str(self.manifest)]))
        self.assertTrue(cargo_purify.runtime_metadata(
            'metadata', ['metadata', '--manifest-path=' + str(self.manifest)]))
        with contextlib.chdir(self.manifest.parent):
            self.assertTrue(cargo_purify.runtime_metadata('metadata', ['metadata']))

    def test_project_metadata_keeps_project_lock(self):
        self.assertFalse(cargo_purify.runtime_metadata(
            'metadata', ['metadata', '--manifest-path', str(self.root / 'project/Cargo.toml')]))

    def test_user_supplied_sdk_sources_need_not_live_under_sysroot(self):
        sources = self.root / 'custom-sources'
        os.environ['RUST_SRC_PATH'] = str(sources)
        args = ['--manifest-path', str(sources / 'Cargo.toml')]
        self.assertTrue(cargo_purify.runtime_metadata('metadata', args))
        self.assertFalse(cargo_purify.runtime_metadata('build', args))

    def test_builds_and_unredirected_sdk_queries_keep_lock_guards(self):
        args = ['--manifest-path', str(self.manifest)]
        self.assertFalse(cargo_purify.runtime_metadata('build', args))
        del os.environ['CARGO_RESOLVER_LOCKFILE_PATH']
        self.assertFalse(cargo_purify.runtime_metadata('metadata', args))

    def test_named_toolchain_is_not_an_sdk_source_path(self):
        os.environ['RUSTUP_TOOLCHAIN'] = 'custom-toolchain'
        self.assertFalse(cargo_purify.runtime_metadata(
            'metadata', ['--manifest-path', str(self.manifest)]))



class GitSource(unittest.TestCase):
    def parse(self, source):
        match = cargo_purify.GIT_SOURCE.fullmatch(source)
        return match and (match.group(1), match.group(2))

    def test_reference_kinds(self):
        rev = "12746aa88cc51a6f1fc97f92b8d36d991c2d966e"
        for query in ("", "?tag=1.0.18", "?branch=main", "?rev=12746aa"):
            self.assertEqual(
                self.parse(f"git+https://github.com/dtolnay/ryu{query}#{rev}"),
                ("https://github.com/dtolnay/ryu", rev),
            )

    def test_not_git(self):
        self.assertIsNone(self.parse("registry+https://github.com/rust-lang/crates.io-index"))
        self.assertIsNone(self.parse("git+https://example.com/repo"))


class FetchUrl(unittest.TestCase):
    def test_https_unchanged(self):
        self.assertEqual(cargo_purify.fetch_url("https://github.com/a/b"), "https://github.com/a/b")
        self.assertEqual(cargo_purify.fetch_url("file:///tmp/repo"), "file:///tmp/repo")

    def test_ssh_becomes_https(self):
        self.assertEqual(cargo_purify.fetch_url("ssh://git@github.com/a/b"), "https://github.com/a/b")
        self.assertEqual(cargo_purify.fetch_url("ssh://git@example.com:2222/a/b.git"), "https://example.com/a/b.git")
        self.assertEqual(cargo_purify.fetch_url("ssh://example.com/a"), "https://example.com/a")


class VersionReq(unittest.TestCase):
    def test_bare_version_is_caret(self):
        self.assertEqual(cargo_purify.version_req("1"), "^1")
        self.assertEqual(cargo_purify.version_req("1.2.3"), "^1.2.3")

    def test_operators_kept(self):
        self.assertEqual(cargo_purify.version_req("=1.0.0"), "=1.0.0")
        self.assertEqual(cargo_purify.version_req(">=1, <2"), ">=1, <2")
        self.assertEqual(cargo_purify.version_req("~1.2"), "~1.2")
        self.assertEqual(cargo_purify.version_req("*"), "*")


class SriSha256(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(cargo_purify.sri_sha256(b""), "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=")


def make_crate(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, text in files.items():
            encoded = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
    return data.getvalue()


MANIFEST = """
[package]
name = "greet"
version = "0.1.0"
description = "Greets"
license = "MIT"
readme = "README.md"
keywords = ["hello"]

[dependencies.itoa]
version = "1"

[dependencies.renamed]
version = "0.2"
package = "real-name"
optional = true
default-features = false
features = ["x"]

[dependencies.private]
version = "3"
registry-index = "sparse+https://registry.example/index/"

[dev-dependencies.tester]
version = "=2.0.0"

[target.'cfg(unix)'.dependencies.libc]
version = "0.2"

[features]
default = []
extra = ["renamed"]
"""


class PublishMetadata(unittest.TestCase):
    def setUp(self):
        crate = make_crate({"greet-0.1.0/Cargo.toml": MANIFEST, "greet-0.1.0/README.md": "# greet\n"})
        self.manifest, self.readme = cargo_purify.crate_contents(crate)

    def metadata(self, index=cargo_purify.CRATES_IO_INDEX):
        return cargo_purify.publish_metadata(self.manifest, self.readme, index)

    def dep(self, metadata, name):
        [dep] = [d for d in metadata["deps"] if d["name"] == name]
        return dep

    def test_package_fields(self):
        metadata = self.metadata()
        self.assertEqual((metadata["name"], metadata["vers"]), ("greet", "0.1.0"))
        self.assertEqual(metadata["readme"], "# greet\n")
        self.assertEqual(metadata["readme_file"], "README.md")
        self.assertEqual(metadata["keywords"], ["hello"])
        self.assertEqual(metadata["features"], {"default": [], "extra": ["renamed"]})

    def test_dependencies(self):
        metadata = self.metadata()
        itoa = self.dep(metadata, "itoa")
        self.assertEqual((itoa["version_req"], itoa["kind"], itoa["registry"]), ("^1", "normal", None))
        renamed = self.dep(metadata, "real-name")
        self.assertEqual(renamed["explicit_name_in_toml"], "renamed")
        self.assertTrue(renamed["optional"])
        self.assertFalse(renamed["default_features"])
        self.assertEqual(renamed["features"], ["x"])
        self.assertEqual(self.dep(metadata, "tester")["kind"], "dev")
        self.assertEqual(self.dep(metadata, "libc")["target"], "cfg(unix)")
        self.assertEqual(self.dep(metadata, "private")["registry"], "sparse+https://registry.example/index/")

    def test_registry_of_dependencies_for_another_registry(self):
        metadata = self.metadata("sparse+https://registry.example/index/")
        self.assertEqual(self.dep(metadata, "itoa")["registry"], cargo_purify.CRATES_IO_INDEX)
        self.assertIsNone(self.dep(metadata, "private")["registry"])

    def test_body(self):
        metadata = self.metadata()
        body = cargo_purify.publish_body(metadata, b"crate bytes")
        length = int.from_bytes(body[:4], "little")
        self.assertEqual(json.loads(body[4:4 + length]), metadata)
        self.assertEqual(int.from_bytes(body[4 + length:8 + length], "little"), 11)
        self.assertEqual(body[8 + length:], b"crate bytes")

    def test_crate_without_manifest(self):
        with self.assertRaises(cargo_purify.Failure):
            cargo_purify.crate_contents(make_crate({"greet-0.1.0/src/lib.rs": ""}))


class Config(unittest.TestCase):
    def test_registries_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            home = Path(directory) / "cargo-home"
            (root / ".cargo").mkdir(parents=True)
            home.mkdir()
            (root / ".cargo" / "config.toml").write_text(
                '[registries.local]\nindex = "sparse+https://local.example/index/"\n'
            )
            (home / "config.toml").write_text(
                '[registries.local]\nindex = "https://overridden.example"\n'
                '[registries.git-index]\nindex = "https://git.example/index"\n'
            )
            (home / "credentials.toml").write_text('[registries.git-index]\ntoken = "from-file"\n')
            environment = {"CARGO_HOME": str(home), "CARGO_REGISTRIES_LOCAL_TOKEN": "from-env"}
            with mock.patch.dict(os.environ, environment, clear=True):
                config = cargo_purify.CargoConfig(root)
        # The workspace configuration takes precedence over the one in
        # CARGO_HOME.
        self.assertEqual(config.indexes["local"], "sparse+https://local.example/index/")
        self.assertEqual(config.name_of_source("sparse+https://local.example/index/"), "local")
        self.assertEqual(config.name_of_source("registry+https://git.example/index"), "git-index")
        self.assertEqual(config.token_for_source("sparse+https://local.example/index/"), "from-env")
        self.assertEqual(config.tokens["git-index"], "from-file")
        self.assertIsNone(config.name_of_source("sparse+https://unknown.example/"))


class Lockfile(unittest.TestCase):
    def write(self, directory, lock, cargo_lock=b"version = 4\n"):
        root = Path(directory)
        (root / "Cargo.lock").write_bytes(cargo_lock)
        path = root / cargo_purify.LOCK_NAME
        path.write_text(lock if isinstance(lock, str) else json.dumps(lock))
        return root, path

    def lock(self):
        lock = {
            "version": 1,
            "cargoLock": cargo_purify.sri_sha256(b"version = 4\n"),
            "git": [],
            "registries": {},
            "packages": {"hello": {"bins": ["hello"], "lib": None}},
        }
        lock["contentHash"] = cargo_purify.content_hash(lock)
        return lock

    def test_content_hash_ignores_itself_and_key_order(self):
        lock = self.lock()
        reordered = dict(reversed(list(lock.items())))
        self.assertEqual(cargo_purify.content_hash(reordered), lock["contentHash"])
        self.assertNotEqual(cargo_purify.content_hash(lock | {"git": [{"path": "x"}]}), lock["contentHash"])

    def test_accepts_a_written_lockfile(self):
        with tempfile.TemporaryDirectory() as directory:
            root, path = self.write(directory, self.lock())
            self.assertEqual(cargo_purify.checked_lock(root / "Cargo.lock", path)["packages"]["hello"]["bins"], ["hello"])

    def refusal(self, lock, cargo_lock=b"version = 4\n"):
        with tempfile.TemporaryDirectory() as directory:
            root, path = self.write(directory, lock, cargo_lock)
            with self.assertRaises(cargo_purify.Failure) as raised:
                cargo_purify.checked_lock(root / "Cargo.lock", path)
            return str(raised.exception)

    def test_refusals_name_the_command(self):
        tampered = self.lock() | {"git": [{"path": "git/x"}]}
        cases = {
            "malformed": ('{ "version": 1, ', b"version = 4\n", "is not valid JSON"),
            "tampered": (tampered, b"version = 4\n", "was changed after `cargo-purifier lock` wrote it"),
            "stale": (self.lock(), b"version = 4\n# edited\n", "was made from a different Cargo.lock"),
            "other format": (self.lock() | {"version": 2}, b"version = 4\n", "is not a cargo-purifier lockfile"),
        }
        for name, (lock, cargo_lock, expected) in cases.items():
            with self.subTest(name):
                message = self.refusal(lock, cargo_lock)
                self.assertIn(expected, message)
                self.assertIn("`cargo-purifier lock`", message)



class OutsideCargoLock(unittest.TestCase):
    def test_writes_outside_and_keeps_the_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            (root / "Cargo.lock").write_bytes(b"tree\n")
            outside = Path(directory) / "outside" / "Cargo.lock"
            seen = []
            for result in (b"first\n", b"second\n"):
                with cargo_purify.cargo_lock_in_tree(root, outside):
                    seen.append((root / "Cargo.lock").read_bytes())
                    (root / "Cargo.lock").write_bytes(result)
                self.assertEqual(outside.read_bytes(), result)
                self.assertEqual((root / "Cargo.lock").read_bytes(), b"tree\n")
            # The first run starts from the tree's copy, and the second from
            # the outside copy the first one wrote.
            self.assertEqual(seen, [b"tree\n", b"first\n"])

    def test_a_tree_without_cargo_lock_stays_without(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "elsewhere.lock"
            with cargo_purify.cargo_lock_in_tree(root, outside):
                (root / "Cargo.lock").write_bytes(b"made\n")
            self.assertEqual(outside.read_bytes(), b"made\n")
            self.assertFalse((root / "Cargo.lock").exists())

    def test_a_failed_command_writes_nothing_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.lock").write_bytes(b"tree\n")
            outside = root / "elsewhere.lock"
            with self.assertRaises(RuntimeError):
                with cargo_purify.cargo_lock_in_tree(root, outside):
                    (root / "Cargo.lock").write_bytes(b"half\n")
                    raise RuntimeError
            self.assertFalse(outside.exists())
            self.assertEqual((root / "Cargo.lock").read_bytes(), b"tree\n")


class WritableVendor(unittest.TestCase):
    def test_generated_files_do_not_modify_store_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "store-source"
            source.mkdir()
            (source / "build.rs").write_text("// original\n")
            (source / "build.rs").chmod(0o444)
            vendor = root / "vendor-output"
            vendor.mkdir()
            (vendor / "plugin").symlink_to(source, target_is_directory=True)
            state = root / "state"
            copied = cargo_purify.writable_vendor(state, vendor)
            self.assertFalse((copied / "plugin").is_symlink())
            (copied / "plugin" / "permissions").mkdir()
            (copied / "plugin" / "permissions" / "generated.toml").write_text("allow = true\n")
            self.assertFalse((source / "permissions").exists())
            self.assertEqual((source / "build.rs").read_text(), "// original\n")
            self.assertEqual((source / "build.rs").stat().st_mode & 0o222, 0)
            self.assertEqual(cargo_purify.writable_vendor(state, vendor), copied)
            self.assertTrue((copied / "plugin" / "permissions" / "generated.toml").exists())

    def test_retained_repository_links_preserve_parent_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "git-store"
            (repository / "nested" / "crate").mkdir(parents=True)
            (repository / "README.md").write_text("repository documentation")
            vendor = root / "vendor-output"
            (vendor / ".git-sources").mkdir(parents=True)
            (vendor / ".git-sources" / "checkout").symlink_to(repository, target_is_directory=True)
            (vendor / "dependency-1.0.0").symlink_to(".git-sources/checkout/nested/crate", target_is_directory=True)
            copied = cargo_purify.writable_vendor(root / "state", vendor)
            alias = copied / "dependency-1.0.0"
            self.assertTrue(alias.is_symlink())
            self.assertFalse((copied / ".git-sources/checkout").is_symlink())
            self.assertEqual((alias / "../../README.md").read_text(), "repository documentation")
            (alias / "../../README.md").write_text("generated documentation")
            self.assertEqual((repository / "README.md").read_text(), "repository documentation")

    def test_vendoring_upgrade_invalidates_cached_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native, lock = root / "Cargo.lock", root / "purifier.json"
            native.write_text("unchanged native lock")
            lock.write_text("unchanged purifier lock")
            with mock.patch.dict(os.environ, CARGO_PURIFIER_NIX_LIB="/nix/store/old-lib"):
                old = cargo_purify.vendor_stamp(native, lock)
            with mock.patch.dict(os.environ, CARGO_PURIFIER_NIX_LIB="/nix/store/new-lib"):
                new = cargo_purify.vendor_stamp(native, lock)
            self.assertNotEqual(old, new)

    def test_changed_vendor_output_has_a_new_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "lib.rs").write_text("first")
            (second / "lib.rs").write_text("second")
            state = root / "state"
            one = cargo_purify.writable_vendor(state, first)
            two = cargo_purify.writable_vendor(state, second)
            self.assertNotEqual(one, two)
            self.assertEqual((two / "lib.rs").read_text(), "second")


class InheritedCargoHome(unittest.TestCase):
    def test_project_source_identity_survives_an_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'project'
            (project / '.cargo').mkdir(parents=True)
            (project / '.cargo/config.toml').write_text(
                '[source.crates-io]\nreplace-with="local"\n'
                '[source.local]\ndirectory="sources"\n')
            configuration = root / 'vendor.toml'
            configuration.write_text('[source.crates-io]\nreplace-with="frozen"\n'
                                     '[source.frozen]\ndirectory="/immutable/vendor"\n')
            with mock.patch.dict(os.environ, CARGO_HOME=str(root / 'caller-home')):
                home = cargo_purify.inherited_cargo_home(project / 'target/cargo-purifier', configuration)
            data = cargo_purify.tomllib.loads((home / 'config.toml').read_text())
            self.assertEqual(data['source']['local'], {
                'directory': str(project / 'sources'), 'replace-with': 'frozen'})

    def test_legacy_config_paths_cache_links_and_repeated_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'caller-home'
            home.mkdir()
            (home / 'registry').mkdir()
            (home / 'registry' / 'sentinel').write_text('cached')
            original = ('[env]\nRELATIVE_INPUT = {value="inputs", relative=true}\n'
                        '[build]\nrustc-wrapper="tools/wrapper"\ntarget-dir="output"\n'
                        '[target.x86_64-unknown-linux-gnu]\nrunner=["tools/runner", "--flag"]\n')
            (home / 'config').write_text(original)
            (home / 'config.toml').write_text('[env]\nWRONG="ignored"\n')
            vendor = root / 'vendor-config.toml'
            vendor.write_text('[source.crates-io]\nreplace-with="frozen"\n'
                              '[source.frozen]\ndirectory="/immutable/vendor"\n')
            with mock.patch.dict(os.environ, CARGO_HOME=str(home)):
                overlay = cargo_purify.inherited_cargo_home(root / 'state', vendor)
            data = cargo_purify.tomllib.loads((overlay / 'config.toml').read_text())
            self.assertEqual(data['env']['RELATIVE_INPUT']['value'], str(root / 'inputs'))
            self.assertNotIn('WRONG', data['env'])
            self.assertEqual(data['build']['rustc-wrapper'], str(root / 'tools/wrapper'))
            self.assertEqual(data['build']['target-dir'], str(root / 'output'))
            self.assertEqual(data['target']['x86_64-unknown-linux-gnu']['runner'],
                             [str(root / 'tools/runner'), '--flag'])
            self.assertEqual(data['source']['frozen']['directory'], '/immutable/vendor')
            self.assertTrue((overlay / 'registry').is_symlink())
            self.assertEqual((overlay / 'registry/sentinel').read_text(), 'cached')
            self.assertEqual((home / 'config').read_text(), original)
            self.assertTrue(data['net']['offline'])
            with mock.patch.dict(os.environ, CARGO_HOME=str(overlay)):
                self.assertEqual(cargo_purify.inherited_cargo_home(root / 'state', vendor), overlay)

    def test_preserve_bare_commands_and_rebase_existing_includes(self):
        path = Path('/original/.cargo/config.toml')
        data = {'build': {'rustc': 'rustc', 'rustdoc': './bin/rustdoc'},
                'target': {'custom': {'runner': 'tools/run --argument'}},
                'include': ['shared.toml', {'path': '../optional.toml', 'optional': True}],
                'env': {'ABSOLUTE': {'value': '/fixed', 'relative': True}}}
        changed = cargo_purify.rebase_home_config(data, path)
        self.assertEqual(changed['build']['rustc'], 'rustc')
        self.assertEqual(changed['build']['rustdoc'], '/original/bin/rustdoc')
        self.assertEqual(changed['target']['custom']['runner'], '/original/tools/run --argument')
        self.assertEqual(changed['include'][0], '/original/.cargo/shared.toml')
        self.assertEqual(changed['include'][1]['path'], '/original/.cargo/../optional.toml')
        self.assertEqual(changed['env']['ABSOLUTE']['value'], '/fixed')


class StandardLibraryVendor(unittest.TestCase):
    def test_sdk_lock_path_is_data_at_the_nix_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / 'SDK "quoted" ${not_a_variable}' / 'Cargo.lock'
            lock.parent.mkdir()
            lock.write_text('literal SDK lock contents\n')
            with mock.patch.object(cargo_purify, 'stdlib_lock', return_value=lock):
                arguments = cargo_purify.stdlib_vendor_args()
            result = subprocess.run(
                ['nix-instantiate', '--store', 'dummy://', '--eval', '--strict', '--json',
                 '--expr', '{ stdlibLock }: builtins.readFile stdlibLock', *arguments],
                text=True, capture_output=True,
                env=dict(os.environ, XDG_CACHE_HOME=str(root / 'cache')))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), lock.read_text())

    def test_invalid_explicit_rustc_does_not_fall_back_to_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = str(Path(temporary) / 'missing-rustc')
            with mock.patch.dict(os.environ, RUSTC=missing):
                with self.assertRaisesRegex(cargo_purify.Failure, 'cannot inspect the selected rustc'):
                    cargo_purify.stdlib_lock()

    def test_sdk_queries_cannot_select_an_arbitrary_foreign_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / 'selected-sdk/library/Cargo.lock'
            sdk.parent.mkdir(parents=True)
            sdk.write_text('version = 4\n')
            manifest = sdk.parent / 'Cargo.toml'
            foreign = root / 'other-sdk/library/Cargo.toml'
            with mock.patch.object(cargo_purify, 'stdlib_lock', return_value=sdk):
                self.assertTrue(cargo_purify.stdlib_metadata('metadata',
                                ['metadata', '--manifest-path', str(manifest)]))
                self.assertTrue(cargo_purify.stdlib_metadata('metadata',
                                ['metadata', '--manifest-path=' + str(manifest)]))
                self.assertFalse(cargo_purify.stdlib_metadata('metadata',
                                 ['metadata', '--manifest-path', str(foreign)]))
                self.assertFalse(cargo_purify.stdlib_metadata('run',
                                 ['run', '--', '--manifest-path', str(manifest)]))

    def test_sdk_switch_and_dependency_change_invalidate_prepared_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native, lock = root / 'Cargo.lock', root / 'purifier.json'
            native.write_text('project-native-lock')
            lock.write_text('project-purifier-lock')
            first, second = root / 'sdk-one.lock', root / 'sdk-two.lock'
            first.write_text('sdk-lock')
            second.write_text('sdk-lock')
            with mock.patch.object(cargo_purify, 'stdlib_lock', return_value=None):
                ordinary = cargo_purify.vendor_stamp(native, lock)
            with mock.patch.object(cargo_purify, 'stdlib_lock', return_value=first):
                prepared = cargo_purify.vendor_stamp(native, lock)
                first.write_text('updated-sdk-lock')
                changed = cargo_purify.vendor_stamp(native, lock)
            with mock.patch.object(cargo_purify, 'stdlib_lock', return_value=second):
                switched = cargo_purify.vendor_stamp(native, lock)
            self.assertEqual(len({ordinary, prepared, changed, switched}), 4)
            self.assertEqual(native.read_text(), 'project-native-lock')
            self.assertEqual(lock.read_text(), 'project-purifier-lock')


if __name__ == "__main__":
    unittest.main()
