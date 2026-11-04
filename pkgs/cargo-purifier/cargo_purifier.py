# SPDX-License-Identifier: MIT
"""cargo-purifier: lock a cargo workspace for Nix, prefetch it, and publish
what Nix packaged.

`cargo-purifier lock` writes cargo-purifier.lock.json at the workspace root.
Cargo.lock already pins every registry download by name, version and
checksum, so the purifier lockfile records only what Cargo.lock leaves out:
the NAR hash of each git dependency's checkout, the download URL template
of each registry other than crates.io, and the workspace's packages with
their binaries and libraries. It also records the hash of the Cargo.lock it
read, so a Nix build can tell when the two files no longer belong
together, and the hash of its own content, so a hand edit shows.

`cargo-purifier prefetch` builds the vendored sources and the tools of the
development loop and the editor in the Nix store, and keeps them alive with a
garbage-collector root at target/cargo-purifier/prefetch.

`cargo-purifier publish` uploads the .crate that Nix packages for a
workspace package in the sandbox, with the registry's publish API.

cargo runs this command as `cargo purifier <command>` too.
"""

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

LOCK_NAME = "cargo-purifier.lock.json"
FORMAT = 1
CRATES_IO_INDEX = "https://github.com/rust-lang/crates.io-index"
CRATES_IO_SOURCE = "registry+" + CRATES_IO_INDEX
CRATES_IO_API = "https://crates.io"
# A git source in Cargo.lock: git+<url>[?rev=…|?tag=…|?branch=…]#<commit>
GIT_SOURCE = re.compile(r"git\+([^?#]+)(?:\?[^#]*)?#([0-9a-f]+)")
LIB_KINDS = {"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"}


class Failure(Exception):
    pass


def run(command, capture=True, cwd=None):
    environment = dict(os.environ, CARGO_PURIFIER="0")
    if command[0] == "cargo" and environment.get("CARGO_PURIFIER_CAPTURE_CONFIG"):
        command = [command[0], "--config", environment["CARGO_PURIFIER_CAPTURE_CONFIG"], *command[1:]]
    result = subprocess.run(command, check=True, text=True, capture_output=capture, cwd=cwd, env=environment)
    return result.stdout if capture else None


def sri_sha256(data):
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def content_hash(lock):
    """The hash of a lockfile's content without its own `contentHash`: the
    SHA-256 of the JSON that Nix's builtins.toJSON writes for the same
    value, with sorted keys and no whitespace."""
    rest = {key: value for key, value in lock.items() if key != "contentHash"}
    return sri_sha256(json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def lock_path_of(options, root):
    """The purifier lockfile: `--lockfile` or CARGO_PURIFIER_LOCKFILE, or the
    one at the workspace root."""
    value = options.lockfile or os.environ.get("CARGO_PURIFIER_LOCKFILE")
    return Path(value).resolve() if value else root / LOCK_NAME


def cargo_lock_of(options, root):
    """Cargo.lock: `--tool-lockfile` or CARGO_PURIFIER_TOOL_LOCKFILE, for a
    copy kept outside the tree, or the one at the workspace root."""
    value = options.tool_lockfile or os.environ.get("CARGO_PURIFIER_TOOL_LOCKFILE")
    return Path(value).resolve() if value else root / "Cargo.lock"


@contextlib.contextmanager
def cargo_lock_in_tree(root, cargo_lock):
    """Runs the cargo commands inside it on `cargo_lock`, which cargo reads
    only from the workspace root. A Cargo.lock outside the tree goes into
    the workspace root first, when it exists, and otherwise cargo starts
    from the tree's copy. Afterwards the result goes to `cargo_lock`, and
    the tree's own copy is put back as it was."""
    tree = root / "Cargo.lock"
    if cargo_lock == tree:
        yield
        return
    kept = tree.read_bytes() if tree.exists() else None
    if cargo_lock.exists():
        tree.write_bytes(cargo_lock.read_bytes())
    try:
        yield
        cargo_lock.parent.mkdir(parents=True, exist_ok=True)
        cargo_lock.write_bytes(tree.read_bytes())
    finally:
        if kept is None:
            tree.unlink(missing_ok=True)
        else:
            tree.write_bytes(kept)


def checked_lock(cargo_lock, path, mutation=False):
    """The purifier lockfile at `path`, refused when it is missing,
    malformed, edited by hand, or made from another Cargo.lock than
    `cargo_lock`."""
    fix = "Run `cargo-purifier lock` to write it again."
    if not path.exists():
        raise Failure(f"{path} is missing. Run `cargo-purifier lock` first.")
    try:
        lock = json.loads(path.read_text())
    except ValueError as error:
        raise Failure(f"{path} is not valid JSON ({error}). {fix}") from error
    if not isinstance(lock, dict) or lock.get("version") != FORMAT:
        raise Failure(f"{path} is not a cargo-purifier lockfile of format {FORMAT}. {fix}")
    if lock.get("contentHash") != content_hash(lock):
        raise Failure(f"{path} was changed after `cargo-purifier lock` wrote it. {fix}")
    if not cargo_lock.exists():
        raise Failure(f"{cargo_lock} is missing. Run `cargo-purifier lock` first.")
    if not mutation and lock.get("cargoLock") != sri_sha256(cargo_lock.read_bytes()):
        raise Failure(f"{path} was made from a different Cargo.lock. Run `cargo-purifier lock` and commit both files.")
    return lock


def cargo_home():
    return Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")


def env_name(name):
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


class CargoConfig:
    """The registries and tokens cargo would read for a workspace: from
    .cargo/config.toml in the workspace root and its ancestors, from
    $CARGO_HOME/config.toml, from $CARGO_HOME/credentials.toml and from
    CARGO_REGISTRIES_<NAME>_* and CARGO_REGISTRY_TOKEN."""

    def __init__(self, root):
        self.indexes = {}
        self.tokens = {}
        files = []
        for directory in [root, *root.parents]:
            for name in ("config.toml", "config"):
                path = directory / ".cargo" / name
                if path.is_file():
                    files.append(path)
                    break
        for name in ("config.toml", "config"):
            path = cargo_home() / name
            if path.is_file() and path not in files:
                files.append(path)
                break
        # Files nearer the workspace take precedence.
        for path in reversed(files):
            data = tomllib.loads(path.read_text())
            for name, registry in data.get("registries", {}).items():
                if "index" in registry:
                    self.indexes[name] = registry["index"]
                if "token" in registry:
                    self.tokens[name] = registry["token"]
            if "token" in data.get("registry", {}):
                self.tokens["crates-io"] = data["registry"]["token"]
        for name in ("credentials.toml", "credentials"):
            path = cargo_home() / name
            if path.is_file():
                data = tomllib.loads(path.read_text())
                for registry_name, registry in data.get("registries", {}).items():
                    if "token" in registry:
                        self.tokens[registry_name] = registry["token"]
                if "token" in data.get("registry", {}):
                    self.tokens["crates-io"] = data["registry"]["token"]
                break
        prefix = "CARGO_REGISTRIES_"
        for key, value in os.environ.items():
            if key.startswith(prefix) and key.endswith("_INDEX"):
                wanted = key[len(prefix):-len("_INDEX")]
                for name in list(self.indexes) or []:
                    if env_name(name) == wanted:
                        self.indexes[name] = value
        if os.environ.get("CARGO_REGISTRY_TOKEN"):
            self.tokens["crates-io"] = os.environ["CARGO_REGISTRY_TOKEN"]
        for name in self.indexes:
            token = os.environ.get(f"CARGO_REGISTRIES_{env_name(name)}_TOKEN")
            if token:
                self.tokens[name] = token

    def name_of_source(self, source):
        """The configured name of the registry a Cargo.lock source names."""
        for name, index in self.indexes.items():
            if source in (index, "registry+" + index):
                return name
        return None

    def token_for_source(self, source):
        name = self.name_of_source(source)
        return self.tokens.get(name) if name else None


def http_get(url, token=None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", token)
    with urllib.request.urlopen(request) as response:
        return response.read()


def index_config(source, config):
    """The index configuration (config.json) of the registry a Cargo.lock
    source names: over HTTP for a sparse index, from a shallow clone for a
    git index."""
    if source.startswith("sparse+"):
        url = source[len("sparse+"):]
        if not url.endswith("/"):
            url += "/"
        try:
            return json.loads(http_get(url + "config.json"))
        except urllib.error.HTTPError as error:
            token = config.token_for_source(source)
            if error.code in (401, 403) and token:
                return json.loads(http_get(url + "config.json", token))
            raise Failure(f"cannot read {url}config.json: {error}") from error
    if source.startswith("registry+"):
        url = source[len("registry+"):]
        with tempfile.TemporaryDirectory() as directory:
            run(["git", "clone", "--quiet", "--depth", "1", url, directory])
            return json.loads((Path(directory) / "config.json").read_text())
    raise Failure(f"{source} is not a registry")


def fetch_url(url):
    """The URL a Nix build fetches a git dependency from. The sandbox holds
    no SSH keys, so an ssh:// dependency is fetched over HTTPS from the same
    host and path."""
    if not url.startswith("ssh://"):
        return url
    authority, _, path = url[len("ssh://"):].partition("/")
    host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    return f"https://{host}/{path}"


def git_hash(url, rev):
    """The NAR hash of a checkout with its submodules, as nixpkgs fetchgit
    computes it."""
    out = run([
        "nix-prefetch-git", "--url", url, "--rev", rev,
        "--fetch-submodules", "--no-add-path", "--quiet",
    ])
    return json.loads(out)["hash"]


def workspace_root(manifest_path):
    """The workspace root and its Cargo.toml. cargo reads its configuration
    from the current directory and its ancestors, so cargo runs from the
    manifest's directory here and from the workspace root afterwards."""
    if manifest_path:
        manifest = Path(manifest_path).resolve()
        found = run(["cargo", "metadata", "--no-deps", "--offline", "--format-version", "1",
                     "--manifest-path", str(manifest)], cwd=manifest.parent)
    else:
        found = run(["cargo", "metadata", "--no-deps", "--offline", "--format-version", "1"])
    root_manifest = str(Path(json.loads(found)["workspace_root"]) / "Cargo.toml")
    return Path(root_manifest).parent, root_manifest


def input_adapter():
    directory = os.environ.get("CARGO_PURIFIER_TOOLS_DIR")
    if directory is None:
        directory = str(Path(__file__).resolve().parents[2] / "tools")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    import cargo_cli_inputs
    return cargo_cli_inputs


def previous_lock(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def input_state(options, root, previous):
    if (not previous.get("projectInputs") and not getattr(options, "input", None)
            and not getattr(options, "no_inputs", False)):
        return None
    saved = getattr(options, "_input_state", None)
    return saved if saved is not None else input_adapter().resolve(options, root, previous)


def lock(options):
    root, root_manifest = workspace_root(options.manifest_path)
    workspace = ["--manifest-path", root_manifest]
    cargo_lock_path = cargo_lock_of(options, root)
    lock_path = lock_path_of(options, root)
    previous = previous_lock(lock_path)
    state = input_state(options, root, previous)
    configuration = CargoConfig(root)
    cargo = os.environ.get("CARGO_PURIFIER_REAL_CARGO", "cargo")
    original_home = cargo_home()
    context = (input_adapter().native_capture(root, state, configuration, cargo, original_home)
               if state is not None else contextlib.nullcontext(None))

    with cargo_lock_in_tree(root, cargo_lock_path), context as capture_home:
        # Brings Cargo.lock up to date with the manifests, changing no more
        # than it must, and downloads what it names. Its output goes to the
        # terminal.
        if state is None:
            run(["cargo", "fetch", *workspace], capture=False, cwd=root)
        else:
            input_adapter().fetch([cargo, "fetch", *workspace], root, capture_home, state, cargo)
        metadata = json.loads(run(["cargo", "metadata", "--format-version", "1", "--no-deps", "--locked",
                                   *workspace], cwd=root))
        if state is not None and state['inputs']:
            from cargo_inputs import portable_lock
            native_lock = root / 'Cargo.lock'
            native_lock.write_bytes(portable_lock(native_lock.read_bytes(), state['entries']))
    members = set(metadata["workspace_members"])
    packages = {}
    for package in metadata["packages"]:
        if package["id"] not in members:
            continue
        targets = package["targets"]
        libs = [t for t in targets if LIB_KINDS & set(t["kind"])]
        packages[package["name"]] = {
            "bins": sorted(t["name"] for t in targets if "bin" in t["kind"]),
            # The library target, whose name the files cargo writes for it
            # carry, with '-' as '_'.
            "lib": {
                "name": libs[0]["name"].replace("-", "_"),
                "kinds": sorted(libs[0]["crate_types"]),
            } if libs else None,
        }

    cargo_lock_bytes = cargo_lock_path.read_bytes()
    cargo_lock = tomllib.loads(cargo_lock_bytes.decode())

    lock_path = lock_path_of(options, root)
    previous_git = {}
    previous_registries = {}
    if lock_path.exists():
        try:
            previous = json.loads(lock_path.read_text())
            for entry in previous.get("git", []):
                previous_git[(entry["git"]["url"], entry["git"]["rev"])] = entry["hash"]
            previous_registries = previous.get("registries", {})
        except (ValueError, KeyError, TypeError):
            previous_git, previous_registries = {}, {}

    checkouts = set()
    registry_sources = set()
    for package in cargo_lock.get("package", []):
        source = package.get("source", "")
        if source.startswith("git+"):
            match = GIT_SOURCE.fullmatch(source)
            if not match:
                raise Failure(f"cannot read the git source {source!r} of {package['name']}")
            checkouts.add((fetch_url(match.group(1)), match.group(2)))
        elif source.startswith(("registry+", "sparse+")) and source != CRATES_IO_SOURCE:
            registry_sources.add(source)

    git = []
    for url, rev in sorted(checkouts):
        digest = previous_git.get((url, rev))
        if digest is None:
            print(f"cargo-purifier: hashing {url} at {rev}", file=sys.stderr)
            digest = git_hash(url, rev)
        git.append({
            "path": f"git/{rev}",
            # cargo reads a checkout's files and never its .git directory.
            "git": {"url": url, "rev": rev, "submodules": True, "leaveDotGit": False, "commitObject": None},
            "hash": digest,
        })

    config = CargoConfig(root)
    registries = {}
    for source in sorted(registry_sources):
        print(f"cargo-purifier: reading the index configuration of {source}", file=sys.stderr)
        try:
            dl = index_config(source, config)["dl"]
        except (Failure, OSError, subprocess.CalledProcessError, KeyError, ValueError):
            if source not in previous_registries:
                raise
            print(f"cargo-purifier: keeping the recorded configuration of {source}", file=sys.stderr)
            dl = previous_registries[source]["dl"]
        registries[source] = {"dl": dl}

    result = {
        "version": FORMAT,
        "cargoLock": sri_sha256(cargo_lock_bytes),
        "git": git,
        "registries": registries,
        "packages": packages,
    }
    if state is not None and state['inputs']:
        result['projectInputs'] = state['inputs']
    result["contentHash"] = content_hash(result)
    lock_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if state is not None:
        input_adapter().prune_roots(state)
    print(f"cargo-purifier: wrote {lock_path}", file=sys.stderr)


def update(options):
    """`cargo update` of every package, or of the named ones, then the lock
    of the result."""
    root, root_manifest = workspace_root(options.manifest_path)
    named = [argument for package in options.packages for argument in ("--package", package)]
    options.refresh_inputs = True
    state = input_state(options, root, previous_lock(lock_path_of(options, root)))
    options._input_state = state
    cargo = os.environ.get("CARGO_PURIFIER_REAL_CARGO", "cargo")
    context = (input_adapter().native_capture(root, state, CargoConfig(root), cargo, cargo_home())
               if state is not None else contextlib.nullcontext(None))
    with cargo_lock_in_tree(root, cargo_lock_of(options, root)), context:
        run(["cargo", "update", "--manifest-path", root_manifest, *named], capture=False, cwd=root)
    lock(options)


def version_req(requirement):
    """A requirement as cargo sends it: a bare version means a caret
    requirement."""
    parts = []
    for part in requirement.split(","):
        part = part.strip()
        parts.append("^" + part if part[:1].isdigit() else part)
    return ", ".join(parts)


def member_text(archive, member):
    """A regular file of a tar archive, as text; KeyError when it is
    missing or not a regular file."""
    file = archive.extractfile(member)
    if file is None:
        raise KeyError(member)
    return file.read().decode()


def crate_contents(crate_bytes):
    with tarfile.open(fileobj=io.BytesIO(crate_bytes), mode="r:gz") as archive:
        manifests = [m for m in archive.getmembers() if m.name.count("/") == 1 and m.name.endswith("/Cargo.toml")]
        if len(manifests) != 1:
            raise Failure("the crate has no Cargo.toml at its top level")
        top = manifests[0].name.split("/")[0]
        manifest = tomllib.loads(member_text(archive, manifests[0]))
        readme = manifest.get("package", {}).get("readme")
        readme_text = None
        if isinstance(readme, str):
            try:
                readme_text = member_text(archive, f"{top}/{readme}")
            except KeyError:
                readme_text = None
        return manifest, readme_text


def publish_metadata(manifest, readme_text, target_index, projected=()):
    """The JSON part of a publish request, as cargo builds it from the
    crate's normalized Cargo.toml."""
    package = manifest["package"]
    deps = []

    def add(table, kind, target):
        for key, spec in (table or {}).items():
            if isinstance(spec, str):
                spec = {"version": spec}
            if "version" not in spec:
                continue
            index = spec.get("registry-index", CRATES_IO_INDEX)
            deps.append({
                "optional": spec.get("optional", False),
                "default_features": spec.get("default-features", True),
                "name": spec.get("package", key),
                "features": spec.get("features", []),
                "version_req": version_req(spec["version"]),
                "target": target,
                "kind": kind,
                "registry": None if index == target_index or
                    (spec.get("package", key), spec["version"].removeprefix('=')) in projected else index,
                "explicit_name_in_toml": key if "package" in spec else None,
            })

    for kind, key in (("normal", "dependencies"), ("dev", "dev-dependencies"), ("build", "build-dependencies")):
        add(manifest.get(key), kind, None)
        for target, tables in manifest.get("target", {}).items():
            add(tables.get(key), kind, target)

    readme = package.get("readme")
    return {
        "name": package["name"],
        "vers": package["version"],
        "deps": deps,
        "features": manifest.get("features", {}),
        "authors": package.get("authors", []),
        "description": package.get("description"),
        "documentation": package.get("documentation"),
        "homepage": package.get("homepage"),
        "readme": readme_text,
        "readme_file": readme if isinstance(readme, str) else None,
        "keywords": package.get("keywords", []),
        "categories": package.get("categories", []),
        "license": package.get("license"),
        "license_file": package.get("license-file"),
        "repository": package.get("repository"),
        "badges": {},
        "links": package.get("links"),
        "rust_version": package.get("rust-version"),
    }


def publish_body(metadata, crate_bytes):
    encoded = json.dumps(metadata).encode()
    return (
        len(encoded).to_bytes(4, "little") + encoded
        + len(crate_bytes).to_bytes(4, "little") + crate_bytes
    )


def nix_build(expression, *args):
    """Realize the tool expression with explicit, packaged integration paths."""
    extra = []
    if os.environ.get("CARGO_PURIFIER_SHARED_SOURCE"):
        extra += ["--arg", "sharedSource", path_arg(Path(os.environ["CARGO_PURIFIER_SHARED_SOURCE"]))]
    if os.environ.get("CARGO_PURIFIER_TOOLS_DIR"):
        extra += ["--arg", "repositoryTools", path_arg(Path(os.environ["CARGO_PURIFIER_TOOLS_DIR"]))]
    return run([
        "nix-build", os.path.join(os.environ["CARGO_PURIFIER_NIX_LIB"], expression),
        "--arg", "nixpkgs", os.environ["CARGO_PURIFIER_NIXPKGS"],
        "--argstr", "system", os.environ["CARGO_PURIFIER_SYSTEM"],
        *extra, *args,
    ]).strip()


def path_arg(path):
    """A Nix expression for an absolute path, for nix-build --arg."""
    return "/. + " + json.dumps(str(path))


def tools_arg():
    """A Nix expression for the cargo-purifier package this command runs
    in, which the prefetch keeps alive, or null outside it."""
    tools = os.environ.get("CARGO_PURIFIER_TOOLS")
    return f"builtins.storePath {json.dumps(tools)}" if tools else "null"


def vendor_stamp(cargo_lock, lock_path):
    """What the cargo wrapper records for the lockfiles it built the
    vendored sources from: the output of `cat <lockfiles> | sha256sum`."""
    data = lock_path.read_bytes() + cargo_lock.read_bytes()
    return hashlib.sha256(data).hexdigest() + "  -"


def prefetch(options):
    root, _ = workspace_root(options.manifest_path)
    lock_path = lock_path_of(options, root)
    cargo_lock = cargo_lock_of(options, root)
    checked_lock(cargo_lock, lock_path)
    state = root / "target" / "cargo-purifier"
    state.mkdir(parents=True, exist_ok=True)
    print("cargo-purifier: building the vendored sources and the tools in the Nix store", file=sys.stderr)
    out = Path(nix_build(
        "tool-prefetch.nix",
        "--arg", "src", path_arg(root),
        "--arg", "lockFile", path_arg(lock_path),
        "--arg", "cargoLock", path_arg(cargo_lock),
        "--arg", "tools", tools_arg(),
        "--out-link", str(state / "prefetch"),
    ))
    # The cargo wrapper reads the vendored sources from these two files
    # without calling Nix.
    vendor = state / "vendor"
    if vendor.is_symlink() or vendor.exists():
        vendor.unlink()
    vendor.symlink_to(os.path.realpath(out / "vendor"))
    (state / "stamp").write_text(vendor_stamp(cargo_lock, lock_path) + "\n")
    print(f"cargo-purifier: prefetched; {state / 'prefetch'} keeps everything alive, "
          "and the development loop and the editor now work offline", file=sys.stderr)


def nix_release(root, lock_path, cargo_lock, package):
    """The .crate file Nix packages for a workspace package."""
    out = nix_build(
        "tool-release.nix", "--no-out-link",
        "--arg", "src", path_arg(root),
        "--arg", "lockFile", path_arg(lock_path),
        "--arg", "cargoLock", path_arg(cargo_lock),
        "--argstr", "package", package,
    )
    crates = list(Path(out).glob("*.crate"))
    if len(crates) != 1:
        raise Failure(f"{out} holds no single .crate file")
    return crates[0]


def publish(options):
    root, _ = workspace_root(options.manifest_path)
    config = CargoConfig(root)

    repository = None
    projected = getattr(options, '_projected', ())
    if options.crate:
        crate_path = Path(options.crate)
    else:
        lock_path = lock_path_of(options, root)
        cargo_lock = cargo_lock_of(options, root)
        lock_data = checked_lock(cargo_lock, lock_path)
        package = options.package
        if package is None:
            names = sorted(lock_data["packages"])
            if len(names) != 1:
                raise Failure(f"the workspace has several packages ({', '.join(names)}); choose one with --package")
            package = names[0]
        print(f"cargo-purifier: packaging {package} with Nix", file=sys.stderr)
        crate_path = nix_release(root, lock_path, cargo_lock, package)
        if lock_data.get('projectInputs'):
            repository = Path(nix_build(
                'tool-release.nix', '--no-out-link', '--arg', 'src', path_arg(root),
                '--arg', 'lockFile', path_arg(lock_path), '--arg', 'cargoLock', path_arg(cargo_lock),
                '--argstr', 'package', package, '--arg', 'repository', 'true'))
            catalog = json.loads((repository / '.catalog').read_text())
            projected = {(entry['name'], entry['version']) for entry in catalog['packages']}
    crate_bytes = crate_path.read_bytes()

    if options.registry:
        if options.registry not in config.indexes:
            raise Failure(f"no registry named {options.registry} is configured")
        index_url = config.indexes[options.registry]
        source = index_url if index_url.startswith("sparse+") else "registry+" + index_url
        api = index_config(source, config).get("api")
        if not api:
            raise Failure(f"the registry {options.registry} does not accept publishing")
        token = config.tokens.get(options.registry)
        target_index = index_url
    else:
        api = CRATES_IO_API
        token = config.tokens.get("crates-io")
        target_index = CRATES_IO_INDEX

    manifest, readme_text = crate_contents(crate_bytes)
    metadata = publish_metadata(manifest, readme_text, target_index, projected)
    print(f"cargo-purifier: {crate_path} is {metadata['name']} {metadata['vers']}", file=sys.stderr)
    if options.dry_run:
        print(json.dumps(metadata, indent=2, sort_keys=True))
        return
    if not token:
        raise Failure("no token for the registry; run `cargo login`, or set CARGO_REGISTRY_TOKEN "
                      "or CARGO_REGISTRIES_<NAME>_TOKEN")

    if repository is not None:
        from types import SimpleNamespace
        archives = {(entry['name'], entry['version']): repository / entry['archive']
                    for entry in catalog['packages']}
        visited = set()
        ordered = []
        def visit(coordinate):
            if coordinate in visited:
                return
            visited.add(coordinate)
            data = archives[coordinate].read_bytes()
            document, readme = crate_contents(data)
            native = publish_metadata(document, readme, target_index, projected)
            for dependency in native['deps']:
                child = dependency['name'], dependency['version_req'].removeprefix('=')
                if child in archives:
                    visit(child)
            ordered.append(archives[coordinate])
        visit((metadata['name'], metadata['vers']))
        for archive in ordered:
            publish(SimpleNamespace(**{**vars(options), 'crate': str(archive),
                                       '_projected': projected, '_reuse': True}))
        return

    if getattr(options, '_reuse', False):
        input_adapter()
        from cargo_capture import Registry
        from cargo_repository import prefix
        with tempfile.TemporaryDirectory(prefix='cargo-publication-') as temporary:
            registry = Registry(target_index, token, temporary)
            try:
                rows = [json.loads(line) for line in registry.read(prefix(metadata['name']) + '/' +
                                                                  metadata['name'].lower()).splitlines() if line]
            except (FileNotFoundError, urllib.error.HTTPError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code != 404:
                    raise
                rows = []
            matches = [row for row in rows if row['vers'] == metadata['vers']]
            if matches:
                record = matches[0]
                registry.records[(metadata['name'], metadata['vers'])] = record
                if record['cksum'] != hashlib.sha256(crate_bytes).hexdigest() or \
                        registry.download(metadata['name'], metadata['vers']) != crate_bytes:
                    raise Failure(f"the registry already contains different bytes for {metadata['name']} {metadata['vers']}")
                print(f"cargo-purifier: reusing {metadata['name']} {metadata['vers']}", file=sys.stderr)
                return

    request = urllib.request.Request(
        api.rstrip("/") + "/api/v1/crates/new",
        data=publish_body(metadata, crate_bytes),
        method="PUT",
        headers={"Authorization": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            answer = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        try:
            details = "; ".join(e.get("detail", "") for e in json.loads(body).get("errors", []))
        except ValueError:
            details = body
        raise Failure(f"the registry refused the upload ({error.code}): {details}") from error
    for kind, items in (answer.get("warnings") or {}).items():
        for item in items:
            print(f"cargo-purifier: warning from the registry ({kind}): {item}", file=sys.stderr)
    print(f"cargo-purifier: published {metadata['name']} {metadata['vers']}", file=sys.stderr)



def native_command(options):
    root = Path(options.root).resolve()
    arguments = options.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    lock_path = lock_path_of(options, root)
    cargo_lock = cargo_lock_of(options, root)
    edits = options.subcommand in {"add", "remove", "rm", "update", "generate-lockfile"}
    data = checked_lock(cargo_lock, lock_path, mutation=edits)
    adapter = input_adapter()
    state = adapter.resolve(options, root, data)
    cargo = options.native_cargo
    subcommand = options.subcommand
    edits = subcommand in {"add", "remove", "rm", "update", "generate-lockfile"}
    reads = subcommand in {"build", "b", "check", "c", "run", "r", "test", "t", "bench",
                          "doc", "d", "clippy", "rustc", "rustdoc", "tree", "metadata", "package", "fix"}
    if not reads and not edits:
        return subprocess.call([cargo, *arguments], env=dict(os.environ, CARGO_PURIFIER="0"))
    with cargo_lock_in_tree(root, cargo_lock):
        if edits:
            if subcommand == "add":
                candidates = {}
                for entry in state['entries']:
                    candidates.setdefault(entry['name'], []).append(entry)
                rewritten = []
                skip_value = False
                for argument in arguments:
                    if skip_value:
                        rewritten.append(argument)
                        skip_value = False
                        continue
                    if argument in {"--package", "-p", "--features", "-F", "--rename", "--target",
                                    "--manifest-path", "--registry", "--path", "--git", "--branch", "--tag", "--rev"}:
                        skip_value = True
                    name, separator, requirement = argument.partition('@')
                    choices = candidates.get(name, [])
                    if len(choices) == 1:
                        argument = name + '@=' + choices[0]['version']
                    elif choices:
                        from cargo_graph import VersionSelector
                        with tempfile.TemporaryDirectory(prefix="cargo-add-selector-") as temporary:
                            selector = VersionSelector(cargo, dict(os.environ, CARGO_PURIFIER="0",
                                                       CARGO_HOME=temporary + "/home"), Path(temporary) / "index")
                            selected = selector.select(requirement if separator else "*",
                                                       [entry['sourceVersion'] for entry in choices])
                        matches = [entry for entry in choices if entry['sourceVersion'] == selected]
                        if len(matches) != 1:
                            raise Failure(f"ambiguous Nix-provided source for {name} {selected}")
                        argument = name + '@=' + matches[0]['version']
                    rewritten.append(argument)
                arguments = rewritten
            with adapter.native_capture(root, state, CargoConfig(root), cargo, cargo_home(), mutation=True):
                command = [cargo, "--config", os.environ["CARGO_PURIFIER_CAPTURE_CONFIG"], *arguments]
                subprocess.run(command, env=dict(os.environ, CARGO_PURIFIER="0"), check=True)
            from cargo_inputs import portable_lock
            native_lock = root / "Cargo.lock"
            native_lock.write_bytes(portable_lock(native_lock.read_bytes(), state["entries"]))
            return 0
        directory = root / "target/cargo-purifier"
        directory.mkdir(parents=True, exist_ok=True)
        vendor = Path(nix_build(
            "tool-vendor.nix", "--arg", "src", path_arg(root),
            "--arg", "lockFile", path_arg(lock_path),
            "--arg", "cargoLock", path_arg(cargo_lock),
            "--out-link", str(directory / "vendor")))
        configuration = directory / "config.toml"
        configuration.write_text((vendor / ".cargo/config.toml").read_text()
                                 .replace('"cargo-vendor-dir"', json.dumps(str(vendor)))
                                 .replace('@vendor@', str(vendor)))
        from cargo_inputs import borrowed_manifests, hydrate_lock
        saved = (root / "Cargo.lock").read_bytes()
        try:
            with borrowed_manifests(root, state['entries'], cargo):
                hydrate_lock(root, vendor, cargo, configuration, state["entries"])
                command = [cargo, "--config", str(configuration)]
                validation = subprocess.run([*command, "metadata", "--offline", "--locked", "--format-version", "1"],
                                            cwd=root, env=dict(os.environ, CARGO_PURIFIER="0"),
                                            text=True, capture_output=True)
                if validation.returncode:
                    sys.stderr.write(validation.stderr)
                    raise Failure("the locked Cargo graph changed; run cargo-purifier lock")
                return subprocess.call([*command, "--offline", *arguments],
                                       env=dict(os.environ, CARGO_PURIFIER="0"))
        finally:
            (root / "Cargo.lock").write_bytes(saved)

def main(argv):
    args = argv[1:]
    if args and args[0] == "_workspace-root":
        _, manifest = workspace_root(args[1] if len(args) > 1 else None)
        print(manifest)
        return 0
    # cargo runs an external subcommand with the subcommand's name first.
    if args[:1] == ["purifier"]:
        args = args[1:]
    parser = argparse.ArgumentParser(prog="cargo-purifier", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    locker = commands.add_parser("lock", help="write cargo-purifier.lock.json, keeping cargo's resolution")
    updater = commands.add_parser("update", help="`cargo update` every package or the named ones, then lock")
    updater.add_argument("packages", nargs="*", help="the packages to update; all of them by default")
    fetcher = commands.add_parser("prefetch", help="build what the development loop and the editor need, for offline work")
    publisher = commands.add_parser("publish", help="upload the .crate Nix packages for a workspace package")
    for command in (locker, updater, fetcher, publisher):
        command.add_argument("--manifest-path", help="a Cargo.toml of the workspace")
        command.add_argument("--lockfile", help="the purifier lockfile, when it lives outside the workspace")
        command.add_argument("--tool-lockfile", help="Cargo.lock, when a copy outside the workspace takes its place")
    for command in (locker, updater):
        command.add_argument("--input", action="append", help="a Nix project exporting Cargo crates")
        command.add_argument("--no-inputs", action="store_true", help="remove Nix project input pins")
        command.add_argument("--refresh-inputs", action="store_true", help="advance the Nix source pins")
    publisher.add_argument("-p", "--package", help="the workspace package to publish")
    publisher.add_argument("--registry", help="a registry configured in cargo's configuration; crates.io by default")
    publisher.add_argument("--crate", help="a .crate file to upload instead of the one Nix packages")
    publisher.add_argument("--dry-run", action="store_true", help="print the publish metadata and upload nothing")
    native = commands.add_parser("_cargo", help=argparse.SUPPRESS)
    native.add_argument("--root", required=True)
    native.add_argument("--native-cargo", required=True)
    native.add_argument("--subcommand", required=True)
    native.add_argument("--lockfile")
    native.add_argument("--tool-lockfile")
    native.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(args)
    if options.command == "_cargo":
        raise SystemExit(native_command(options))

    {"lock": lock, "update": update, "prefetch": prefetch, "publish": publish}[options.command](options)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except subprocess.CalledProcessError as error:
        if error.stderr:
            sys.stderr.write(error.stderr)
        sys.exit(f"cargo-purifier: {' '.join(error.cmd)} failed")
    except Failure as error:
        sys.exit(f"cargo-purifier: {error}")
