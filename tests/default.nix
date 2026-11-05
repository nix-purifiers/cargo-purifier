# SPDX-License-Identifier: MIT
#
# The flake's checks. Every one builds in the sandbox; the only network
# access is the fixed-output fetches of locked crates and git checkouts.
# conformance-relock and conformance-rebuild need the network or a Nix
# daemon, so they are CI jobs of those names (.github/workflows/check.yml).
{
  pkgs,
  # buildCargoProject from the package set, and lib.cargo-purifier.readLock.
  buildCargoProject,
  readLock,
  # The cargo-purifier package: the command, the cargo wrapper and
  # rust-analyzer.
  cargo-purifier,
  # ripgrep's source, pinned in tests/dependencies.
  ripgrep,
  historicalRustSource,
}:
let
  inherit (pkgs) lib;
  inherit (pkgs.stdenv.hostPlatform) system;

  build = buildCargoProject;

  registryUrls = import ../nix/lib/registry.nix { inherit lib; };

  fixturePrefetch = import ../nix/lib/tool-prefetch.nix {
    nixpkgs = pkgs.path;
    inherit system;
    src = ./fixtures/workspace;
    lockFile = ./fixtures/workspace/cargo-purifier.lock.json;
    tools = cargo-purifier;
  };

  refuses = import ./refuses.nix { inherit pkgs; };

  # A workspace with a library from crates.io, a binary with a git
  # dependency, and a C library, locked by tests/relock-fixtures.sh.
  workspace = build { src = ./fixtures/workspace; };

  # The same workspace with `cargo test` in its package builds.
  checked = build {
    src = ./fixtures/workspace;
    doCheck = true;
  };

  # A binary with a crate from a registry other than crates.io, whose
  # download template has markers. Its lockfiles are written by hand: the
  # registry's index does not exist, and the build never reads it.
  registry = build { src = ./fixtures/registry; };

  # A binary with an ssh:// git dependency. Its lockfiles are written by
  # hand, with the dependency recorded under its HTTPS URL as
  # `cargo-purifier lock` records it.
  ssh = build { src = ./fixtures/ssh; };

  # A workspace in fixtures/outside/app with a path dependency in
  # fixtures/outside/shared, outside the workspace root.
  outside = build {
    src = ./fixtures/outside;
    workspace = "app";
  };

  # The workspace fixture without the purifier lockfile in its source, built
  # from the one beside it.
  external = build {
    src = builtins.path {
      name = "workspace-without-purifier-lockfile";
      path = ./fixtures/workspace;
      filter = path: _: baseNameOf path != "cargo-purifier.lock.json";
    };
    lockFile = ./fixtures/workspace/cargo-purifier.lock.json;
  };

  # The workspace fixture with a Cargo.lock in its source that cargo cannot
  # read, built from both lockfiles beside it: the Cargo.lock passed to the
  # builder replaces the one in the source.
  externalCargoLock = build {
    src = pkgs.runCommand "workspace-with-another-cargo-lock" { } ''
      cp -r ${./fixtures/workspace} $out
      chmod -R u+w $out
      rm $out/cargo-purifier.lock.json
      echo 'not a Cargo.lock' >$out/Cargo.lock
    '';
    lockFile = ./fixtures/workspace/cargo-purifier.lock.json;
    cargoLock = ./fixtures/workspace/Cargo.lock;
  };

  workspaceLock = lib.importJSON ./fixtures/workspace/cargo-purifier.lock.json;

  sri =
    hex:
    builtins.convertHash {
      hash = hex;
      hashAlgo = "sha256";
      toHashFormat = "sri";
    };

  # A lockfile changed by hand, with the content hash `cargo-purifier lock`
  # would have written for it, so that only the change itself is wrong.
  rehashed =
    lock:
    lock
    // {
      contentHash = sri (
        builtins.hashString "sha256" (builtins.toJSON (removeAttrs lock [ "contentHash" ]))
      );
    };

  lockText = value: builtins.toFile "cargo-purifier.lock.json" (builtins.toJSON value);

  workspaceRefuses =
    name: lockFile: expected:
    refuses name {
      inherit lockFile expected;
      src = ./fixtures/workspace;
    };

  pass = name: pkgs.runCommand name { } "touch $out";

  realWorld = import ./projects/ripgrep.nix {
    inherit pkgs cargo-purifier;
    buildCargoProject = buildCargoProject;
    src = ripgrep;
  };
in
realWorld
// {
  native-projections = import ./native-consumer.nix {
    inherit pkgs;
    projection = workspace.packages.ffi.projections;
    library = "ffi";
    symbol = "ffi_answer";
  };
}
// lib.optionalAttrs (system == "x86_64-linux") {
  historical-target-abi = import ./historical-rust.nix {
    inherit pkgs historicalRustSource buildCargoProject;
  };
  historical-native-selection =
    let
      old = import historicalRustSource { inherit system; };
    in
    pkgs.runCommand "cargo-1.41-native-selection"
      {
        nativeBuildInputs = [
          pkgs.python3
          old.cargo
          old.rustc
        ];
      }
      ''
        export HOME="$TMPDIR/home"
        export CARGO_COMPAT_NATIVE=${old.cargo}/bin/cargo
        export CARGO_COMPAT_EXPECTED_VERSION=${old.cargo.version}
        export CARGO_COMPAT_TOOLS=${../tools}
        export CARGO_COMPAT_COMMAND=${../pkgs/cargo-purifier/cargo_purifier.py}
        python3 ${./test_historical_cargo.py} -v
        touch "$out"
      '';
}
// {
  current-native-selection =
    pkgs.runCommand "cargo-current-native-selection"
      {
        nativeBuildInputs = [
          pkgs.python3
          pkgs.cargo
          pkgs.rustc
        ];
      }
      ''
        export HOME="$TMPDIR/home"
        export CARGO_COMPAT_NATIVE=${pkgs.cargo}/bin/cargo
        export CARGO_COMPAT_EXPECTED_VERSION=${pkgs.cargo.version}
        export CARGO_COMPAT_TOOLS=${../tools}
        export CARGO_COMPAT_COMMAND=${../pkgs/cargo-purifier/cargo_purifier.py}
        python3 ${./test_historical_cargo.py} -v
        touch "$out"
      '';
  composition-repository =
    pkgs.runCommand "cargo-repository-native-tests"
      {
        nativeBuildInputs = [
          pkgs.cargo
          pkgs.rustc
          pkgs.stdenv.cc
          pkgs.git
        ];
      }
      ''
        export HOME="$TMPDIR/home"
        export CARGO_REPOSITORY_TEST_CARGO=${pkgs.cargo}/bin/cargo
        export CARGO_PURIFIER_TEST_TOOLS=${../tools}
        ${(pkgs.python3.withPackages (python: [ python.tomlkit ]))}/bin/python3 \
          ${./test_cargo_repository.py}
        touch "$out"
      '';

  composition-native-export = import ./native-cargo-export.nix {
    inherit pkgs;
    project = workspace;
  };
  # The purifier contract.

  # The workspace lockfile beside a Cargo.lock it was not made from
  # (fixtures/stale holds only that Cargo.lock).
  conformance-stale = refuses "conformance-stale" {
    src = ./fixtures/stale;
    lockFile = ./fixtures/workspace/cargo-purifier.lock.json;
    expected = "was made from a different Cargo.lock. Run `cargo-purifier lock`";
  };

  conformance-tampered = workspaceRefuses "conformance-tampered" (lockText (
    workspaceLock
    // {
      git = map (
        entry: entry // { hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; }
      ) workspaceLock.git;
    }
  )) "was changed after `cargo-purifier lock` wrote it";

  conformance-missing = workspaceRefuses "conformance-missing" (lockText (
    rehashed (workspaceLock // { git = [ ]; })
  )) "has no hash for the git dependency ryu. Run `cargo-purifier lock`";

  conformance-malformed =
    workspaceRefuses "conformance-malformed"
      (builtins.toFile "cargo-purifier.lock.json" ''{ "version": 1, '')
      "is not valid JSON. Run `cargo-purifier lock` to write it again";

  # A file that looks like a JSON object but does not parse fails reading
  # itself, which Nix cannot turn into a problem; its error still names the
  # file and the fix.
  refuses-unparsable =
    workspaceRefuses "refuses-unparsable"
      (builtins.toFile "cargo-purifier.lock.json" ''{ "version": 1, }'')
      "run `cargo-purifier lock` to write it again";

  conformance-offline = import ./conformance-offline.nix {
    inherit pkgs cargo-purifier;
    prefetched = fixturePrefetch;
  };

  conformance-concurrent-config =
    pkgs.runCommand "conformance-concurrent-config"
      {
        nativeBuildInputs = [
          pkgs.python3
          pkgs.rustc
        ];
      }
      ''
        python3 ${./concurrent-config.py} \
          --tools ${cargo-purifier} \
          --cargo ${lib.getExe pkgs.cargo} \
          --fixture ${./fixtures/workspace} \
          --vendor ${fixturePrefetch}/vendor
        touch $out
      '';

  conformance-external = pkgs.runCommand "conformance-external" { } ''
    test "$(${lib.getExe external.packages.hello})" = "hello from 2 crates and 1.5"
    test "$(${lib.getExe externalCargoLock.packages.hello})" = "hello from 2 crates and 1.5"
    touch $out
  '';

  # More lockfiles evaluation refuses, each with the command that fixes it.

  refuses-missing-field = workspaceRefuses "refuses-missing-field" (lockText (
    rehashed (removeAttrs workspaceLock [ "packages" ])
  )) "lacks packages. Run `cargo-purifier lock`";

  refuses-other-format = workspaceRefuses "refuses-other-format" (lockText (
    rehashed (workspaceLock // { version = 2; })
  )) "reads format 1. Run `cargo-purifier lock`";

  refuses-not-a-lockfile = workspaceRefuses "refuses-not-a-lockfile" (lockText [
  ]) "is not a cargo-purifier lockfile. Run `cargo-purifier lock`";

  refuses-missing-registry = refuses "refuses-missing-registry" {
    lockFile = lockText (
      rehashed (lib.importJSON ./fixtures/registry/cargo-purifier.lock.json // { registries = { }; })
    );
    src = ./fixtures/registry;
    expected = "has no entry for the registry sparse+https://registry.invalid/index/. Run `cargo-purifier lock`";
  };

  # The fixtures.

  # hello runs, with its crates.io and git dependencies.
  workspace = pkgs.runCommand "workspace" { } ''
    test "$(${lib.getExe workspace.packages.hello})" = "hello from 2 crates and 1.5"
    touch $out
  '';

  # hello with `cargo test` in its build, which conformance-rebuild also
  # builds twice.
  workspace-hello = checked.packages.hello;

  # The workspace test run in the sandbox.
  workspace-tests = workspace.tests;

  # counter runs, with itoa from the fixture's own registry.
  registry = pkgs.runCommand "registry" { } ''
    test "$(${lib.getExe registry.packages.counter})" = "counted 42"
    touch $out
  '';

  # app runs, with a path dependency outside its workspace, from a source
  # tree that holds both.
  outside = pkgs.runCommand "outside" { } ''
    test "$(${lib.getExe outside.packages.app})" = "shared from outside the workspace"
    touch $out
  '';

  # tell runs, with ryu from an ssh:// git dependency fetched over HTTPS.
  ssh = pkgs.runCommand "ssh" { } ''
    test "$(${lib.getExe ssh.packages.tell})" = "told 2.5"
    touch $out
  '';

  # Download URLs follow cargo's rules for `dl` templates.
  registry-urls =
    let
      url =
        dl: name:
        registryUrls.downloadUrl {
          inherit dl name;
          version = "1.0.0";
          checksum = "abc";
        };
      cases = {
        "https://dl.example/crates/serde/1.0.0/download" = url "https://dl.example/crates" "serde";
        "https://dl.example/se/rd/serde/1.0.0" =
          url "https://dl.example/{prefix}/{crate}/{version}" "serde";
        "https://dl.example/1/a.crate" = url "https://dl.example/{prefix}/{crate}.crate" "a";
        "https://dl.example/2/ab" = url "https://dl.example/{prefix}/{crate}" "ab";
        "https://dl.example/3/a/abc" = url "https://dl.example/{lowerprefix}/{crate}" "abc";
        "https://dl.example/se/rd/Serde?abc" =
          url "https://dl.example/{lowerprefix}/{crate}?{sha256-checksum}" "Serde";
      };
      failures = lib.filterAttrs (expected: got: expected != got) cases;
    in
    if failures == { } then
      pass "registry-urls"
    else
      throw "registry-urls: ${builtins.toJSON failures}";

  # The workspace packages with a binary, or with a library cargo writes as
  # a file to install, have a package; greet has neither.
  packages =
    if
      lib.attrNames workspace.packages == [
        "ffi"
        "hello"
      ]
    then
      pass "packages"
    else
      throw "packages: ${toString (lib.attrNames workspace.packages)}";

  # Every output the lockfile names, at the path evaluation gives it. hello
  # has a release attribute too, but `cargo package` refuses its git
  # dependency, which has no version.
  outputs =
    let
      inherit (workspace.packages) ffi hello;
      named = {
        "hello.bins.hello" = hello.bins.hello;
        "ffi.libs.libffi.so" = ffi.libs."libffi${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";
        "ffi.libs.libffi.a" = ffi.libs."libffi.a";
        "release.ffi.crate" = workspace.release.ffi.crate;
        "release.greet.crate" = workspace.release.greet.crate;
      };
    in
    assert lib.attrNames hello.bins == [ "hello" ];
    assert lib.attrNames ffi.bins == [ ];
    assert lib.length (lib.attrNames ffi.libs) == 2;
    assert
      lib.attrNames workspace.release == [
        "ffi"
        "greet"
        "hello"
      ];
    pkgs.runCommand "outputs" { } ''
      ${lib.concatStrings (
        lib.mapAttrsToList (name: file: ''
          test -f ${file} || { echo "${name} is missing: ${file}" >&2; exit 1; }
        '') named
      )}
      test -x ${hello.bins.hello}
      tar tzf ${workspace.release.greet.crate} | grep -qx 'greet-0.1.0/src/lib.rs'
      touch $out
    '';

  # `fetches` lists every download as a purifier library entry: the
  # crates.io crate itoa as a file entry, and the ryu checkout as a git
  # entry.
  fetches =
    let
      inherit (workspace) fetches;
      files = lib.filter (entry: entry ? url) fetches;
      gits = lib.filter (entry: entry ? git) fetches;
    in
    assert map (entry: entry.path) files == [ "itoa-1.0.18" ];
    assert lib.all (entry: entry.mirrors == [ ] && lib.hasPrefix "sha256-" entry.hash) files;
    assert map (entry: entry.git.url) gits == [ "https://github.com/dtolnay/ryu" ];
    assert lib.length fetches == 2;
    pass "fetches";

  # The lock in memory of the workspace fixture has the contract's shape:
  # the crates.io crate and the git checkout as entries, every package as an
  # output, and no problems.
  read-lock =
    let
      lock = readLock { src = ./fixtures/workspace; };
    in
    assert lock.purifier == "cargo-purifier";
    assert lock.problems == [ ];
    assert
      lib.attrNames lock.outputs == [
        "ffi"
        "greet"
        "hello"
      ];
    assert lock.outputs.hello.bins == [ "hello" ] && lock.outputs.hello.version == "0.2.0";
    assert
      map (entry: entry.cargo.name) lock.entries == [
        "itoa"
        "ryu"
      ];
    assert lib.all (entry: !(entry ? systems)) lock.entries;
    pass "read-lock";

  # The release that `cargo-purifier publish` has Nix build.
  publish-release = import ../nix/lib/tool-release.nix {
    nixpkgs = pkgs.path;
    inherit system;
    src = ./fixtures/workspace;
    lockFile = ./fixtures/workspace/cargo-purifier.lock.json;
    package = "greet";
  };

  # The lock run and the publish step against a registry of the check's own.
  local-registry = import ./local-registry.nix {
    inherit pkgs cargo-purifier;
    crate = workspace.release.greet;
  };

  # The parsing, URL, lockfile and publish logic of `cargo-purifier`.
  unit = pkgs.runCommand "unit" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    cp ${./test_cargo_purifier.py} test_cargo_purifier.py
    PYTHONPATH=${../pkgs/cargo-purifier} python3 -m unittest -v test_cargo_purifier
    touch $out
  '';

  inherit cargo-purifier;
}
