# cargo-purifier

cargo-purifier keeps Cargo's development workflow on locked,
Nix-resolved dependencies. Use the normal build and test commands in your
working tree; building the project itself in the Nix sandbox is an
optional next step.

cargo keeps planning and compiling the build. `Cargo.lock` already pins
every registry download by name, version and checksum, so cargo-purifier
reuses it, and writes `cargo-purifier.lock.json` beside it only for what
`Cargo.lock` cannot record.

## Quickstart

From the project root:

```zsh
nix shell \
  'github:nix-purifiers/cargo-purifier#cargo' \
  'github:nix-purifiers/cargo-purifier#cargo-purifier'
cargo-purifier lock
cargo-purifier prefetch
cargo build
cargo test
```

Commit the lockfiles that the lock command writes, including the build
tool's own lockfile when it has one. Keep using the project's usual build,
test and editor commands with this setup. Nix provides the locked
dependencies; the development commands above run outside the Nix sandbox.
No project Nix files are needed for this workflow.

Sandboxed builds of the whole project are an optional
[next step](#building-the-project-with-nix).

The shell provides the `cargo` wrapper. Use it for the locked
development workflow. With no purifier lockfile, it warns on standard
error and runs the ordinary tool with impure resolution.

For persistent use, add these packages through your usual Nix configuration.


## Development loop

The `cargo-purifier` package holds the command and a `cargo` wrapper. Local
capture, prefetch and development use your installed Cargo and Rust tools.
The wrapper discovers Cargo on `PATH`, skipping purifier wrappers, or uses
`CARGO_PURIFIER_REAL_CARGO` when explicitly provided. `cargo purifier` preserves
the invoking Cargo. Rustup proxies, project toolchain selection, and explicit
`RUSTC` and `RUSTDOC` settings remain in effect. A missing runtime produces an
error explaining how to provide one. There is no runtime-version allowlist;
the selected tools enforce their actual manifest, lock-format and compiler
requirements.

In a workspace with a `cargo-purifier.lock.json`,
the wrapper's `cargo build`, `check`, `run`, `test`, `bench`, `doc`,
`clippy`, `rustc`, `rustdoc`, `tree`, `metadata`, `package` and `fix` read
every registry and git source from a vendored copy that Nix builds from
the two lockfiles, and run with `--offline`. Incremental compilation works
as usual. In projects with Nix inputs, `add`, `remove` and `update` also
resolve against those inputs. Run `cargo-purifier lock` after changing dependencies.

Installed Cargo plugins, such as `cargo nextest run`, also receive the prepared
sources. Your Cargo still selects and invokes the plugin, with its original
arguments, SDK and search path. Native Cargo calls made by the plugin inherit a
private Cargo home with the locked source configuration; the caller's Cargo
home and installed plugins remain available. No plugin or SDK is installed by
this delegation.

Plugin edits to workspace manifests and `Cargo.lock`, and the native exit
status, are preserved. The purifier does not silently refresh its capture after
an edit: run `cargo-purifier lock` before the next locked operation. Commands
that manage Cargo itself, such as `fmt`, `clean`, `install` and `publish`, retain
their native behavior. Plugins that manage Cargo-global configuration or SDKs
should likewise be run explicitly with plain Cargo, rather than treating the
private prepared home as the caller's global configuration.

Ordinary Cargo aliases are classified through the selected Cargo and expanded
by Cargo itself. Shell aliases, alias cycles and aliases that change invocation
context are refused before their bodies run; invoke the explicit command from
the intended workspace instead. Cargo's own command-line configuration behavior
is retained, including which settings its external subcommands inherit.

Delegation currently refuses workspaces that actually require temporary
synthetic bindings for Nix-provided crates. An unused Nix catalog alone does not
prevent delegation. The provided-input check examines the full feature graph;
if Cargo cannot classify that graph, delegation reports the native diagnostic
without executing the plugin. External tool lockfiles are also outside this
delegation protocol. Built-in Nix-input operations described above are unchanged.

To add a dependency, run `cargo add`, then `cargo-purifier lock`. To
upgrade, run `cargo-purifier update`, which runs `cargo update` for every
package, or for the packages it names, and then locks the result. Commit
both lockfiles either way. `cargo-purifier lock` keeps cargo's resolution:
it runs `cargo fetch`, which brings `Cargo.lock` up to date with the
manifests and changes no more than it must. It then hashes each git
checkout with `nix-prefetch-git`, submodules included, and reads the index
configuration of each registry other than crates.io. A relock of an
unchanged workspace reproduces the lockfile byte for byte, and hashes only
the git checkouts that changed. A command that needs a download the
lockfiles do not record stops, and names `cargo-purifier lock`.
`CARGO_PURIFIER=0` runs plain cargo, which downloads what it needs.

### The Nix build

`cargo-purifier.buildCargoProject { src, lockFile ? …, cargoLock ? …, fetchers ? { }, ... }`
returns:

| Attribute | What it is |
|---|---|
| `packages.<package>` | every workspace package with a binary or an installable library, built with `cargo build --package <package>` |
| `packages.<package>.bins.<binary>` | `$out/bin/<binary>` |
| `packages.<package>.libs."lib<name>.so"`, `."lib<name>.a"` | the libraries of `cdylib`, `dylib` and `staticlib` targets |
| `tests` | `cargo test --workspace`, in the sandbox |
| `release.<package>` | the .crate of `cargo package --package <package>`; `release.<package>.crate` is its path |
| `fetches` | every download: the entries of the lock in memory |
| `vendor` | the vendored sources every build reads |

Evaluation knows every attribute and path from the lockfiles, without
building anything or importing generated Nix code. A plain Rust library
(`rlib`) is only usable compiled into something, so it has a release and
no package.

### The lock in memory

`lib.cargo-purifier.readLock { src, workspace ? ".", lockFile ? …, cargoLock ? … }` reads
both lockfiles into the shape every purifier shares: `purifier`,
`entries`, `outputs` and `problems`. `entries` are download entries of the
[purifier library](https://github.com/nix-purifiers/purifier): a file
entry per registry crate, from the checksum in `Cargo.lock`, and a git
entry per checkout. Each also carries `cargo = { source, name, version }`.
Cargo downloads are the same on every system, so no entry has `systems`.
`outputs` names every workspace package, with its binaries, library and
version, and `problems` holds the reasons the lockfiles cannot be used.
Reading never fails, except on a purifier lockfile that looks like a JSON
object and does not parse, which Nix cannot catch; that error still names
the file and the fix. The builder passes the lock to the library's
`checkLock`, and builds the entries `entriesFor` gives for the host
system.

`fetchers.fetchurl` and `fetchers.fetchgit` replace nixpkgs' fetchers for
every download, crates.io included, for a mirror or credentials.
`pkgs/build-cargo-project/default.nix` documents the other arguments: `doCheck`
also runs each package's tests in its build, and `srcFilter` chooses the
files of a path `src` the builds see. The builder takes the packages it
builds with by name from the scope, so
`cargo-purifier.overrideScope (final: prev: { rustPlatform = …; })` builds
with another Rust toolchain, and `buildCargoProject.override` replaces a
package for the builder alone.

### Lockfiles outside the source

An external build is an option for a workspace whose repository does not
hold the lockfiles: a packaging flake that builds a workspace its author
does not change, or a library that does not commit `Cargo.lock`. The
packaging flake keeps the lockfiles and passes them to the builder.
`lockFile` names the purifier lockfile, and `cargoLock` names a
`Cargo.lock` that the builds use in place of any copy in `src`.

Every command takes `--lockfile <path>` and `--tool-lockfile <path>`, or
reads the same paths from `CARGO_PURIFIER_LOCKFILE` and
`CARGO_PURIFIER_TOOL_LOCKFILE`. A development shell of the packaging flake
can set both variables to the paths it passes to the builder. With an
outside `Cargo.lock`, `cargo-purifier lock` and `update` run cargo on it,
write the result there, and leave the workspace's own `Cargo.lock` as it
was. When the outside file does not exist yet, they start from the
workspace's copy. cargo reads `Cargo.lock` only from the workspace root, so
the cargo wrapper copies the outside `Cargo.lock` there before it runs
cargo.

### Dependencies from another Nix project

```zsh
cargo-purifier lock --input github:example/library
cargo run
```

The input exports `projections.cargo` from a package built with
`buildCargoProject`. Its Nix source pin covers the supplied crates; their
package records stay out of `Cargo.lock`, and their download hashes stay out
of the purifier lock. Cargo receives a temporary native lock reconstructed
from the current Nix inputs and captured ordinary dependencies. Existing dependency requirements
are bound automatically, and the working manifest keeps its original versions.
Use `--refresh-inputs` to advance source pins or `--no-inputs` to remove them.

In a Nix build, pass producer packages with `dependencies = [ producer ];`.
When the consumer lock already records the inputs, the builder loads those pins
automatically. Each package and release exposes `projections.cargo`, a native
local registry with the complete crate closure. Nix-derived versions and exact
dependency requirements keep ordinary downstream resolution on that closure.

### Publish

```zsh
cargo-purifier publish --package greet
```

`cargo-purifier publish` has Nix build `release.<package>` from the
workspace, and uploads that .crate file to crates.io, or to the registry
`--registry <name>` names in cargo's configuration. The upload is a request
to the registry's publish API, of the kind `cargo publish` sends, with the
metadata read from the normalized `Cargo.toml` inside the crate. The token
comes from where cargo keeps it: `CARGO_REGISTRY_TOKEN` or
`CARGO_REGISTRIES_<NAME>_TOKEN`, or `credentials.toml` as `cargo login`
writes it. `--dry-run` prints the metadata and uploads nothing.

For a project with Nix inputs, publication includes the reachable projected
dependencies. Existing coordinates are reused only when their checksums and
downloaded bytes match. Ordinary Cargo projects can consume the resulting
registry without the purifier. Cargo's `install --registry` path treats the
installed root's unqualified manifest dependencies as crates.io dependencies;
installing those binaries from an alternative registry requires a crates.io
source replacement pointing at that registry.

### Decisions

- Each package builds with its own `cargo build --package`, so a package's
  build does not depend on its siblings. They share the vendored sources,
  not compiled crates.
- `doCheck` is off by default, so a test suite that needs the network does
  not break a package's build; `tests` runs the whole suite.
- `cargo-purifier publish` uploads through the registry API itself,
  because `cargo publish` always packages the crate again before
  uploading.
- The vendoring of crates.io and git sources is nixpkgs'
  `importCargoLock`, which places each crate of a checkout by name and
  resolves `workspace = true` inheritance in vendored manifests.

## Building the project with Nix

Once the native development workflow is working, you can also build and
test the whole project in the Nix sandbox. For this step, keep a flake or
another Nix build definition in the project's repository, or maintain an
external packaging repository that points at its sources and lockfiles.
The builder consumes the same lockfiles as the development workflow.

A flake can package the workspace by calling the builder directly. It can
live in the repository of the workspace or in another one:

```nix
{
  inputs.cargo-purifier.url =
    "github:nix-purifiers/cargo-purifier";
  inputs.nixpkgs.follows = "cargo-purifier/nixpkgs";

  outputs =
    { nixpkgs, cargo-purifier, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ cargo-purifier.overlays.default ];
      };
      project = pkgs.cargo-purifier.buildCargoProject {
        src = ./.;
      };
    in
    {
      packages.${system}.hello = project.packages.hello;
      checks.${system}.tests = project.tests;
    };
}
```

For the development loop and the editor, put the `cargo-purifier` package
of the scope on the `PATH`, for example in a development shell as
`pkgs.cargo-purifier.cargo-purifier`, and prefetch once:

```zsh
cargo-purifier prefetch
```

When the selected compiler has `rust-src` installed, prefetch also prepares
the dependencies in that component's standard-library `Cargo.lock`. Builds
using `-Zbuild-std` can then resolve those crates offline alongside the
project's dependencies. The SDK lock stays separate from the project's locks.

SDK discovery follows `RUSTC`, then native Cargo's `build.rustc` configuration
(including `CARGO_BUILD_RUSTC` and CLI config overrides), then `rustc` on PATH.
Prefetch with the compiler selection intended for the build. Changing the SDK
lock invalidates the prepared sources; SDK installation remains with the
caller's toolchain manager.
For Cargo without configuration-inspection support, set `RUSTC` explicitly
when preparing standard-library dependencies; ordinary Cargo use keeps its
native compiler selection.

### One-off builds without a project Nix file

For an ad hoc build, the builder can also be evaluated directly from the
project root. Replace the example package name with one from your project:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/cargo-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.cargo-purifier.buildCargoProject { src = ./.; })
.packages.hello'
```

This uses the purifier's pinned nixpkgs, matching the lock command above.
To build with ambient `<nixpkgs>` from your Nix search path instead,
replace `import purifier.inputs.nixpkgs` with `import <nixpkgs>`.
Both forms need no Nix files in your project. Select a toolchain that can read
the project's manifests and lock format, using the builder's `rustPlatform`
override when needed. The purifier's default toolchain is not a requirement
that projects use those same versions.

### Toolchain compatibility evidence

Required CI runs the full checks, native composition and lock/update/rebuild
workflows on x86_64 Linux, aarch64 Linux and aarch64 macOS. The current stable
profile additionally runs native composition, the full fixture suite and a
pinned ripgrep 14.1.1 build with Rust/Cargo 1.99.1, including its grep-cli
tests and the offline wrapper and rust-analyzer loop.
The default nixpkgs toolchain remains separate from this selected profile.
To use the current-stable wrappers, select the package in your normal Nix
configuration, or start a shell:

```sh
nix shell github:nix-purifiers/cargo-purifier#current-stable
```

Rust supports its latest stable release. Keep the pinned current-stable profile
up to date as releases change; beta and nightly channels are not credited by
this profile. See [Rust's support policy](https://rust-lang.org/policies/security/).


Native semver selection and workspace discovery are checked with Cargo
1.41.0 on x86_64 Linux and with the current pinned Cargo on both Linux
architectures. The private version-selection probe uses Cargo's default
2015 edition and longstanding project configuration file; it does not impose
edition 2021 or the newer global `--config` argument on the selected resolver.
Workspace discovery asks native Cargo for dependency-free offline metadata,
without resolving dependencies or generating a lock.

The resolver checks do not establish project compilation or publication by
themselves. A separate x86_64 Linux fixture selects both Cargo and rustc 1.41.0
through `makeRustPlatform` and the builder's `rustPlatform` override. It builds
and runs a binary, runs unit tests, calls a compiled `cdylib` from C, verifies
the native `.crate`, and rebuilds and runs the extracted crate with that same
historical compiler as an ordinary project.

For Cargo before stable named profiles, the builder uses native `--release`
build/test commands instead of the newer Nix build hooks' `--profile release`.
Native metadata selects a package manifest for archive creation, because
Cargo 1.41's package command has no `--package` option. This historical build
path uses the selected tools directly and does not add current cargo-auditable
instrumentation.

This is one compiler/target-ABI point, not a historical MSRV interval. It does
not establish the older main wrapper's `--config` path, namespaced projected
registry consumption, CLI publication, cross compilation, other architectures
or earlier compiler APIs. Those remain explicit adapter and test work. Earlier
releases and usable prereleases remain eligible; tested points are neither a
minimum supported version nor a version whitelist.

## Working offline

`cargo-purifier prefetch` builds the locked vendored sources and keeps the
local purifier tools in the Nix store. It keeps them alive with
a garbage-collector root at `target/cargo-purifier/prefetch`, and records
the vendored sources for the wrapper, which then needs no Nix call. After a
prefetch, `cargo build`, `run` and `test` replay those dependencies offline
using your installed runtime. Prepare any SDK components, native libraries
and editor tools your project needs before disconnecting.

`cargo clean` removes the root with the rest of `target`, so prefetch
again before the next stretch of offline work.

Local prefetch does not substitute a Nix SDK for your runtime or keep a
mutable host SDK alive through its garbage-collector root. The explicit
`current-stable` profile is a convenience that supplies a packaged SDK.
Fully sandboxed project builds separately use the builder's immutable
`rustPlatform`, which can be replaced through the builder or scope override.
Project SDK selection and implementation dependencies are separate: packaging
the purifier's Python or Git helpers can require their own build tools.

Build scripts use writable dependency copies in
`$XDG_CACHE_HOME/cargo-purifier` (by default `~/.cache/cargo-purifier`). The
copies are separate for each project and locked vendor output. Keeping them
outside the workspace by default, with Cargo's registry cache path shape,
lets diagnostic consumers recognize dependency source locations. The caller's
Cargo home is left untouched. `cargo clean` keeps these source copies, as it
does native Cargo's source cache; remove the purifier cache to discard files
generated beside dependency sources.
If the default home cache is not writable, the wrapper uses a private cache
under the caller's temporary directory. An explicit `XDG_CACHE_HOME` is kept
authoritative, including errors when it cannot be written.

## Editor

Use your installed `rust-analyzer` with the purifier's Cargo wrapper on its
`PATH`, and select standard-library sources matching your SDK through your
usual editor settings or `RUST_SRC_PATH`. The optional Nix scope package
`pkgs.cargo-purifier.rust-analyzer` supplies an editor wrapper and the package
set's Rust sources; choose that helper explicitly when those sources match
your selected SDK.

## Troubleshooting

| Mistake | What happens |
|---|---|
| `Cargo.lock` changed since the lock (an upgrade, or a hand edit) | "… was made from a different Cargo.lock. Run `cargo-purifier lock` and commit both files." The wrapper stops with the same reason. |
| A dependency added to `Cargo.toml` but not locked | The wrapper stops: "Cargo.toml needs dependencies that Cargo.lock lacks. Run `cargo-purifier lock` to lock them." |
| The purifier lockfile edited by hand | "… was changed after `cargo-purifier lock` wrote it. Run `cargo-purifier lock` to write it again." |
| A git dependency missing from the purifier lockfile | "… has no hash for the git dependency <name>. Run `cargo-purifier lock`." |
| A registry missing from the purifier lockfile | "… has no entry for the registry <source>. Run `cargo-purifier lock`." |
| A malformed or incomplete purifier lockfile | The error names the file, with "run `cargo-purifier lock` to write it again". |
| Offline, with sources never prefetched | The wrapper stops with Nix's reason, and says to run `cargo-purifier prefetch` while online. |
| A hash that does not match the download | Nix reports a hash mismatch for the crate or checkout. The upstream changed what it serves under that version or commit; `cargo-purifier lock` records what it serves now. |

## Supported language repositories

| Source in Cargo.lock | How the build gets it |
|---|---|
| crates.io | nixpkgs `importCargoLock`, by the checksum in `Cargo.lock` |
| Another registry, git or sparse index | the URL its `dl` template gives, with all of cargo's markers, by the checksum in `Cargo.lock` |
| git, over HTTPS or any URL Nix can fetch | `fetchgit` with submodules, by the hash in the purifier lockfile |
| git over `ssh://` | the same repository over HTTPS from the same host, since the sandbox holds no SSH keys; a private repository needs a credentialed `fetchers.fetchgit` |
| path | the build's `src` |

A path dependency outside the workspace root builds when `src` is a
directory that holds both, and `workspace` names the workspace inside it,
as in `{ src = ./.; workspace = "app"; }`. The builds use the package set's
Rust toolchain, and do not read `rust-toolchain.toml`. cargo-purifier runs
on x86_64-linux, aarch64-linux and aarch64-darwin.

### What the purifier lockfile holds

Each field of `cargo-purifier.lock.json` is something Nix cannot get from
`Cargo.lock` or `Cargo.toml` at evaluation time. crates.io crates appear
only in `Cargo.lock`.

| Field | Why `Cargo.lock` cannot supply it |
|---|---|
| `version` | the purifier lockfile's own format |
| `contentHash` | the hash of the file's own content, which shows a hand edit |
| `cargoLock` | the hash of the `Cargo.lock` it was made from; a `Cargo.lock` cannot vouch that it is unchanged |
| `git[]` | `Cargo.lock` records a git dependency's commit, but a fixed-output fetch needs the checkout's NAR hash, with submodules |
| `registries.<source>.dl` | a registry's download URL template is in its index's `config.json`, which evaluation cannot fetch without import-from-derivation |
| `packages.<package>.bins`, `.lib` | the outputs: cargo finds binaries in `src/main.rs` and `src/bin/` without manifest entries, members may be globs, and the library's name and crate types decide its file names |

## Examples

For day-to-day work on these projects, use the native-tool setup and
commands in [Quickstart](#quickstart) and [Development loop](#development-loop).
The Nix expressions below show optional sandboxed packaging of those
projects.

### ripgrep in its own checkout

[ripgrep](https://github.com/BurntSushi/ripgrep) is a cargo workspace whose
`rg` binary comes from its root package. To work on it with
cargo-purifier, lock it once at the root of a checkout, and build it from
there:

```zsh
nix shell \
  'github:nix-purifiers/cargo-purifier#cargo' \
  'github:nix-purifiers/cargo-purifier#cargo-purifier'
git clone https://github.com/BurntSushi/ripgrep
cd ripgrep
cargo-purifier lock
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/cargo-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.cargo-purifier.buildCargoProject { src = ./.; })
.packages.ripgrep'
```

`nix build` leaves `rg` at `result/bin/rg`. For the development loop,
prefetch once with the `cargo-purifier` package on the `PATH`, and run
`cargo build`, `cargo test` and rust-analyzer as usual, online or offline.

### ripgrep packaged from outside

A packaging flake can build ripgrep without changing ripgrep's repository.
Keep the purifier lockfile in the packaging flake, and write it from a
checkout of the release you package:

```zsh
cargo-purifier lock --manifest-path ~/src/ripgrep/Cargo.toml \
  --lockfile ./ripgrep.lock.json
```

```nix
{
  inputs.cargo-purifier.url =
    "github:nix-purifiers/cargo-purifier";
  inputs.nixpkgs.follows = "cargo-purifier/nixpkgs";
  inputs.ripgrep = {
    url = "github:BurntSushi/ripgrep/14.1.1";
    flake = false;
  };

  outputs =
    {
      nixpkgs,
      cargo-purifier,
      ripgrep,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ cargo-purifier.overlays.default ];
      };
      project = pkgs.cargo-purifier.buildCargoProject {
        src = ripgrep;
        lockFile = ./ripgrep.lock.json;
      };
    in
    {
      packages.${system}.ripgrep =
        project.packages.ripgrep;
    };
}
```

When ripgrep releases a new version, move the `ripgrep` input to it, run the
same `cargo-purifier lock` on a checkout of that version, and commit both
files.

## Captured publication sources

The owning Cargo projection constructor, `pkgs/build-cargo-project/cargo-projections.nix`, accepts optional `publicationSourceArchives = [ { archive = capturedCrate; checksum = "<native registry SHA-256>"; } ];`. This is for a published registry archive that declares an optional `package.readme` file absent from that original archive. Modern Cargo can build some such archives but rejects republishing them.

The projector verifies the archive checksum against the captured registry checksum, its original normalized manifest, and every captured source file. It rejects changed or added source files, inconsistent manifests, and a README lost only during vendoring. With that explicit source evidence, it omits only the absent optional readme declaration in its temporary export manifest; Cargo then authors the normalized publication, which can encode `readme = false`. Existing README files, original archives, captured vendor files, code, dependencies, features, licenses and build inputs remain intact. Missing required source, license and build files retain their native failures.

The output catalog records the original archive and manifest hashes, source file hashes, omitted field and path, and export manifest hashes. The projected manifest and archive metadata change; the original artifact remains the provenance input. Ordinary exports need no additional argument. This constructor option does not acquire or resolve dependencies and is separate from uploading a crate to a public registry.

## Native library projections

Packages with an explicit Cargo `cdylib` target expose `projections.native`,
`python-ffi`, and `java-ffi` alongside their existing Cargo source publication.
The library path comes from the locked target name and selected host platform;
the catalog records the Rust compiler version and crate type. Static libraries,
ordinary Rust `dylib` targets, `rlib`s and binaries do not acquire foreign ABI
projections. The crate must deliberately export a C ABI; symbol signatures,
Rust object layouts and runtime dependencies remain its responsibility.

The native views expose the shared catalog, Python ctypes loader and Java
library paths. The check builds the workspace's real `ffi` cdylib and calls its
`ffi_answer` symbol through ordinary Python ctypes and Java FFM. Java 21 needs
its preview flags; this does not synthesize JNI bindings or Python wheels.

## Credits

The purifier idea was inspired by nixcloud's
[cargo (libnix)](https://github.com/nixcloud/cargo).

nixpkgs `rustPlatform.importCargoLock` vendors the crates.io and git
dependencies. [crane](https://github.com/ipetkov/crane) and
[naersk](https://github.com/nix-community/naersk) also build Cargo projects
from `Cargo.lock`.
