# SPDX-License-Identifier: MIT
#
# A cargo workspace, built in the sandbox from its checked lock in memory
# (read-lock.nix) and the entries of one system. build-cargo-project.nix,
# the contract's builder, and the command's tool expressions call it.
#
#   packages.<package>   every workspace package that has a binary, or a
#                        library cargo writes as a file to install (a
#                        cdylib, a staticlib or a dylib), built with
#                        `cargo build --package <package>`
#   tests                `cargo test --workspace`, in the sandbox
#   release.<package>    the .crate that `cargo package --package <package>`
#                        makes, which `cargo-purifier publish` uploads
#   fetches              the entries of the lock
#   vendor               the vendored sources every build reads
#
# The lock names every output, so evaluation knows each one's path without
# building anything:
#
#   packages.<package>.bins.<binary>    $out/bin/<binary>
#   packages.<package>.libs."<file>"    $out/lib/lib<name>.so or .a
#   release.<package>.crate             $out/<package>-<version>.crate
{
  lib,
  stdenv,
  runCommand,
  # The Rust toolchain.
  rustPlatform,
  purifier ? null,
  python3 ? null,
  dependencies ? [ ],
  repositoryTools ? ../../tools,
  # The fetchers of registry crates and git checkouts.
  fetchurl,
  fetchgit,
  # The source tree.
  src,
  # The workspace root inside `src`, as readLock takes it.
  workspace ? ".",
  # The Cargo.lock the builds use, which replaces any copy in `src`.
  cargoLock ? (if workspace == "." then src else src + "/${workspace}") + "/Cargo.lock",
  # The lock in memory, without problems.
  lock,
  # The entries of `lock` for this system.
  entries,
  # Runs `cargo test --package <package>` in each package's build too.
  doCheck ? false,
  # Which files of a path `src` the builds see. The default leaves out
  # cargo's target directory and Nix result links.
  srcFilter ? (
    path: type:
    let
      name = baseNameOf path;
    in
    !(
      type == "directory"
      && lib.elem name [
        "target"
        ".git"
      ]
    )
    && !(lib.hasPrefix "result" name)
  ),
}:
let
  inWorkspace = workspace != ".";

  baseVendor = import ./vendor.nix {
    inherit
      lib
      rustPlatform
      runCommand
      fetchurl
      fetchgit
      entries
      cargoLock
      ;
    provided = lock.provided or [ ];
  };

  vendor =
    if dependencies == [ ] then
      baseVendor
    else
      import ./vendor-inputs.nix {
        inherit
          lib
          purifier
          runCommand
          python3
          dependencies
          repositoryTools
          ;
        cargo = rustPlatform.rust.cargo;
        base = baseVendor;
      };
  inputPython =
    if python3 == null then null else python3.withPackages (packages: [ packages.tomlkit ]);

  # Runs cargo in the workspace root, when it is not the root of `src`.
  enterWorkspace = lib.optionalString inWorkspace "cd ${lib.escapeShellArg workspace}\n";

  filteredSrc =
    if builtins.isPath src then
      lib.cleanSourceWith {
        inherit src;
        filter = srcFilter;
      }
    else
      src;

  projections.cargo = import ./cargo-projections.nix {
    inherit
      lib
      purifier
      runCommand
      python3
      workspace
      dependencies
      vendor
      cargoLock
      entries
      repositoryTools
      ;
    src = filteredSrc;
    inherit (rustPlatform.rust) cargo rustc;
  };

  # nixpkgs' cargo hooks find Cargo.lock in `cargoRoot`, and build and test
  # in `buildAndTestSubdir`.
  # Every build works on the Cargo.lock this builder was given, which
  # nixpkgs' cargoSetupHook then compares with the vendored one.
  common = {
    src = filteredSrc;
    cargoDeps = vendor;
    prePatch = ''
      cp ${cargoLock} ${lib.escapeShellArg workspace}/Cargo.lock
      chmod u+w ${lib.escapeShellArg workspace}/Cargo.lock
    ''
    # The vendored crates' paths, such as the file a panic message names,
    # are under the build directory. The Linux sandbox always builds in
    # /build, but darwin builds in a new directory every time, so rustc
    # writes them as under /build there too.
    + lib.optionalString stdenv.hostPlatform.isDarwin ''
      export NIX_RUSTFLAGS="''${NIX_RUSTFLAGS:-} --remap-path-prefix=$NIX_BUILD_TOP=/build"
    '';
    postPatch = ''
      ${lib.optionalString (dependencies != [ ]) ''
        ${lib.getExe inputPython} ${repositoryTools}/cargo_inputs.py materialize \
          ${lib.escapeShellArg workspace} ${vendor} ${lib.getExe rustPlatform.rust.cargo}
        cp ${lib.escapeShellArg workspace}/Cargo.lock "$cargoDepsCopy/Cargo.lock"
      ''}
    '';
  }
  // lib.optionalAttrs inWorkspace {
    cargoRoot = workspace;
    buildAndTestSubdir = workspace;
  };

  # The files cargo writes for a library target and nixpkgs' install hook
  # copies to $out/lib.
  libFiles =
    info:
    let
      kinds = if info.lib == null then [ ] else info.lib.kinds;
      shared = stdenv.hostPlatform.extensions.sharedLibrary;
    in
    lib.optional (lib.elem "cdylib" kinds || lib.elem "dylib" kinds) "lib${info.lib.name}${shared}"
    ++ lib.optional (lib.elem "staticlib" kinds) "lib${info.lib.name}.a";

  hasOutputs = info: info.bins != [ ] || libFiles info != [ ];

  # Named profile CLI support became stable in Cargo 1.57. Current nixpkgs
  # hooks pass --profile release; older native Cargo uses --release instead.
  # https://blog.rust-lang.org/2021/12/02/Rust-1.57.0/
  legacyProfileCli = lib.versionOlder rustPlatform.rust.cargo.version "1.57";
  rustTarget = stdenv.hostPlatform.rust.rustcTarget;

  mkPackage =
    name: info:
    let
      drv =
        if legacyProfileCli then
          cargoRun {
            pname = name;
            inherit (info) version;
            inherit doCheck;
            passthru = { inherit projections; };
            meta = lib.optionalAttrs (lib.length info.bins == 1) { mainProgram = lib.head info.bins; };
            buildPhase = ''
              runHook preBuild
              ${enterWorkspace}cargo build --frozen --offline --release --target ${rustTarget} --package ${lib.escapeShellArg name}
              runHook postBuild
            '';
            checkPhase = ''
              runHook preCheck
              cargo test --frozen --offline --release --target ${rustTarget} --package ${lib.escapeShellArg name}
              runHook postCheck
            '';
            installPhase = ''
              runHook preInstall
              mkdir -p "$out/bin" "$out/lib"
              ${lib.concatMapStrings (bin: ''
                cp target/${rustTarget}/release/${lib.escapeShellArg bin} "$out/bin/"
              '') info.bins}
              ${lib.concatMapStrings (file: ''
                cp target/${rustTarget}/release/${lib.escapeShellArg file} "$out/lib/"
              '') (libFiles info)}
              runHook postInstall
            '';
          }
        else
          rustPlatform.buildRustPackage (
            common
            // {
              pname = name;
              inherit (info) version;
              inherit doCheck;
              passthru = { inherit projections; };
              cargoBuildFlags = [
                "--package"
                name
              ];
              cargoTestFlags = [
                "--package"
                name
              ];
              meta = lib.optionalAttrs (lib.length info.bins == 1) {
                mainProgram = lib.head info.bins;
              };
            }
          );
    in
    drv
    // {
      bins = lib.genAttrs info.bins (bin: "${drv}/bin/${bin}");
      libs = lib.genAttrs (libFiles info) (file: "${drv}/lib/${file}");
    };

  # A derivation that runs cargo in the build's source, with the vendored
  # sources and the toolchain.
  cargoRun =
    attrs:
    stdenv.mkDerivation (
      common
      // {
        nativeBuildInputs = [
          rustPlatform.cargoSetupHook
          rustPlatform.rust.cargo
          rustPlatform.rust.rustc
        ];
      }
      // attrs
    );

  mkRelease =
    name: info:
    let
      inherit (info) version;
      drv =
        if dependencies != [ ] then
          runCommand "${name}-projected-crate"
            {
              passthru = { inherit projections; };
            }
            ''
              mkdir -p "$out"
              cp -L ${projections.cargo}/crates/${name}.crate "$out/${name}-${version}.crate"
            ''
        else
          cargoRun {
            pname = "${name}-crate";
            passthru = { inherit projections; };
            inherit version;
            # `cargo package` also builds the packaged crate, from its own
            # sources and the vendored ones.
            buildPhase = ''
              runHook preBuild
              ${enterWorkspace}${
                if legacyProfileCli then
                  ''
                    manifest=$(cargo metadata --offline --no-deps --format-version 1 | ${lib.getExe python3} -c \
                      'import json,sys; print(next(p["manifest_path"] for p in json.load(sys.stdin)["packages"] if p["name"] == sys.argv[1]))' ${lib.escapeShellArg name})
                    cargo package --frozen --manifest-path "$manifest"
                  ''
                else
                  ''
                    cargo package --frozen --package ${lib.escapeShellArg name}
                  ''
              }
              runHook postBuild
            '';
            installPhase = ''
              runHook preInstall
              install -Dm644 target/package/${name}-${version}.crate $out/${name}-${version}.crate
              runHook postInstall
            '';
          };
    in
    drv // { crate = "${drv}/${name}-${version}.crate"; };
in
{
  packages = lib.mapAttrs mkPackage (lib.filterAttrs (_: hasOutputs) lock.outputs);

  tests = cargoRun {
    name = "cargo-workspace-tests";
    buildPhase = ''
      runHook preBuild
      ${enterWorkspace}cargo test --frozen --workspace
      runHook postBuild
    '';
    installPhase = "touch $out";
  };

  release = lib.mapAttrs mkRelease lock.outputs;

  inherit projections;
  fetches = lock.entries;
  inherit vendor;
}
