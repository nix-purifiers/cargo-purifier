# SPDX-License-Identifier: MIT
#
# lib.cargo-purifier.readLock: the lock in memory of the purifier contract,
# read from a workspace's Cargo.lock and the cargo-purifier.lock.json that
# `cargo-purifier lock` wrote.
#
#   lib.cargo-purifier.readLock { src = ./.; }
#   => { purifier; entries; outputs; problems; }
#
# entries   every download, as purifier library entries: a file entry per
#           registry crate, from the checksum in Cargo.lock and the URL
#           the build fetches it from, and a git entry per checkout. Each
#           carries `cargo = { source, name, version }`, the Cargo.lock
#           package it is for (a git entry: the first of them). Cargo
#           downloads do not depend on the system, so no entry has
#           `systems`.
# outputs   every workspace package, with its binaries, library target and
#           version
# problems  why the lockfiles cannot be used, each naming the command that
#           fixes it
#
# Reading does not fail evaluation, so a tool can report the problems. A
# purifier lockfile that looks like a JSON object but does not parse is the
# exception, since Nix cannot catch a parse error; its error names the file
# and the command that fixes it.
{ lib }:
{
  # The source tree.
  src,
  # The workspace root inside `src`: the directory of the workspace's
  # Cargo.toml and Cargo.lock. A tree that also holds path dependencies
  # outside the workspace names the workspace here.
  workspace ? ".",
  # cargo-purifier.lock.json, which may live outside `src`.
  lockFile ? (if workspace == "." then src else src + "/${workspace}") + "/cargo-purifier.lock.json",
  # Cargo.lock: the copy in the workspace, or one kept outside `src` for a
  # workspace that does not commit its own.
  cargoLock ? (if workspace == "." then src else src + "/${workspace}") + "/Cargo.lock",
}:
let
  registryLib = import ./registry.nix { inherit lib; };
  name = toString lockFile;
  rewrite = "Run `cargo-purifier lock` to write it again.";

  hasCargoLock = builtins.pathExists cargoLock;
  hasLock = builtins.pathExists lockFile;
  text = if hasLock then lib.trim (builtins.readFile lockFile) else "";
  looksLikeJson =
    (lib.hasPrefix "{" text && lib.hasSuffix "}" text)
    || (lib.hasPrefix "[" text && lib.hasSuffix "]" text);

  lock = builtins.addErrorContext "while reading the cargo-purifier lockfile ${name}; run `cargo-purifier lock` to write it again" (
    builtins.fromJSON text
  );

  parsed = if hasCargoLock then builtins.fromTOML (builtins.readFile cargoLock) else { };
  packages = parsed.package or [ ];

  sri =
    hex:
    builtins.convertHash {
      hash = hex;
      hashAlgo = "sha256";
      toHashFormat = "sri";
    };

  fields = [
    "cargoLock"
    "contentHash"
    "git"
    "packages"
  ];

  # The first thing wrong with the files themselves; the lock is usable only
  # without one.
  fileProblem =
    if !hasCargoLock then
      "cargo-purifier: ${toString cargoLock} is missing. Run `cargo-purifier lock`."
    else if !hasLock then
      "cargo-purifier: ${name} is missing. Run `cargo-purifier lock`."
    else if !looksLikeJson then
      "cargo-purifier: ${name} is not valid JSON. ${rewrite}"
    else if !(builtins.isAttrs lock) || !(lock ? version) then
      "cargo-purifier: ${name} is not a cargo-purifier lockfile. ${rewrite}"
    else if lock.version != 1 then
      "cargo-purifier: ${name} has format ${toString lock.version}, and this cargo-purifier reads format 1. Run `cargo-purifier lock` with this cargo-purifier."
    else if !(lib.all (field: lock ? ${field}) fields) then
      "cargo-purifier: ${name} lacks ${
        lib.concatStringsSep ", " (lib.filter (field: !(lock ? ${field})) fields)
      }. ${rewrite}"
    else if
      lock.contentHash
      != sri (builtins.hashString "sha256" (builtins.toJSON (removeAttrs lock [ "contentHash" ])))
    then
      "cargo-purifier: ${name} was changed after `cargo-purifier lock` wrote it. ${rewrite}"
    else if lock.cargoLock != sri (builtins.hashFile "sha256" cargoLock) then
      "cargo-purifier: ${name} was made from a different Cargo.lock. Run `cargo-purifier lock` and commit both files."
    else
      null;

  usable = fileProblem == null;
  checked =
    if usable then
      lock
    else
      {
        git = [ ];
        registries = { };
        packages = { };
      };

  # crates.io, which nixpkgs importCargoLock fetches from its CDN.
  cratesIo = "registry+https://github.com/rust-lang/crates.io-index";
  isRegistry = source: lib.hasPrefix "registry+" source || lib.hasPrefix "sparse+" source;
  isGit = source: lib.hasPrefix "git+" source;

  supplied =
    package:
    lib.any (entry: entry.name == package.name && entry.version == package.version) (
      checked.provided or [ ]
    );
  sourced = lib.filter (package: package ? source && !(supplied package)) packages;
  cratesIoPackages = lib.filter (package: package.source == cratesIo) sourced;
  registryPackages = lib.filter (
    package: isRegistry package.source && package.source != cratesIo
  ) sourced;
  gitPackages = lib.filter (package: isGit package.source) sourced;

  # A git source in Cargo.lock ends with `#<commit>`.
  revOf = package: lib.last (lib.splitString "#" package.source);

  checksumOf =
    package:
    package.checksum
      or parsed.metadata."checksum ${package.name} ${package.version} (${package.source})" or null;

  cargoOf = package: { inherit (package) source name version; };

  # A registry crate's path: `<name>-<version>`, with the source's hash
  # appended when a crates.io crate or a git crate has that name too, as
  # in the vendored sources.
  importedNames = map (package: "${package.name}-${package.version}") (
    cratesIoPackages ++ gitPackages
  );
  registryPath =
    package:
    let
      plain = "${package.name}-${package.version}";
    in
    if lib.elem plain importedNames then
      "${plain}-${builtins.substring 0 8 (builtins.hashString "sha256" package.source)}"
    else
      plain;

  cratesIoEntries = map (package: {
    path = "${package.name}-${package.version}";
    url = "https://static.crates.io/crates/${package.name}/${package.version}/download";
    mirrors = [ ];
    hash = sri (checksumOf package);
    cargo = cargoOf package;
  }) (lib.filter (package: checksumOf package != null) cratesIoPackages);

  registryEntries =
    map
      (package: {
        path = registryPath package;
        url = registryLib.downloadUrl {
          inherit (checked.registries.${package.source}) dl;
          inherit (package) name version;
          checksum = checksumOf package;
        };
        mirrors = [ ];
        hash = sri (checksumOf package);
        cargo = cargoOf package;
      })
      (
        lib.filter (
          package: checked.registries ? ${package.source} && checksumOf package != null
        ) registryPackages
      );

  gitEntries = map (
    entry:
    entry
    // {
      cargo = cargoOf (lib.findFirst (package: revOf package == entry.git.rev) { } gitPackages);
    }
  ) (lib.filter (entry: lib.any (package: revOf package == entry.git.rev) gitPackages) checked.git);

  hashedRevs = map (entry: entry.git.rev) checked.git;

  entryProblems =
    map (
      package:
      "cargo-purifier: ${name} has no hash for the git dependency ${package.name}. Run `cargo-purifier lock`."
    ) (lib.filter (package: !(lib.elem (revOf package) hashedRevs)) gitPackages)
    ++
      map
        (
          source:
          "cargo-purifier: ${name} has no entry for the registry ${source}. Run `cargo-purifier lock`."
        )
        (
          lib.unique (
            map (package: package.source) (
              lib.filter (package: !(checked.registries ? ${package.source})) registryPackages
            )
          )
        )
    ++ map (
      package:
      "cargo-purifier: Cargo.lock has no checksum for ${package.name} ${package.version}. Run `cargo-purifier lock`."
    ) (lib.filter (package: checksumOf package == null) (cratesIoPackages ++ registryPackages));

  # The workspace's own packages, and its path dependencies, have no source.
  versionOf =
    packageName:
    (lib.findFirst (package: package.name == packageName && !(package ? source)) {
      version = null;
    } packages).version;
in
{
  purifier = "cargo-purifier";
  projectInputs = checked.projectInputs or [ ];
  provided = checked.provided or [ ];
  entries = cratesIoEntries ++ registryEntries ++ gitEntries;
  outputs = lib.mapAttrs (
    packageName: info: info // { version = versionOf packageName; }
  ) checked.packages;
  problems = if usable then entryProblems else [ fileProblem ];
}
