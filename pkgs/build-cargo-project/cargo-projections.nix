# SPDX-License-Identifier: MIT
{
  lib,
  purifier,
  runCommand,
  python3,
  cargo,
  rustc,
  src,
  workspace,
  dependencies,
  vendor,
  cargoLock,
  entries,
  repositoryTools ? ../../tools,
  publicationSourceArchives ? [ ],
  name ? "cargo-repository",
}:
let
  selected = purifier.projectInputs {
    inherit dependencies;
    formats = [ "cargo" ];
  };
  python = python3.withPackages (packages: [ packages.tomlkit ]);
  projector = "${repositoryTools}/cargo_repository.py";
  toolchain = runCommand "cargo-projector-toolchain" {
    inherit
      projector
      python
      cargo
      rustc
      ;
  } "touch $out";
  identity = runCommand "cargo-repository-identity" {
    source = src;
    publicationSources = builtins.toJSON publicationSourceArchives;
    inherit vendor;
    inherit
      projector
      python
      cargo
      rustc
      ;
    repositories = map (input: input.repository) selected;
  } "touch $out";
  projection =
    runCommand name
      {
        outputs = [
          "out"
          "metadata"
        ];
        nativeBuildInputs = [
          cargo
          rustc
        ];
        request = builtins.toJSON {
          inherit
            src
            workspace
            vendor
            cargoLock
            publicationSourceArchives
            ;
          sourceIdentity = (purifier.inputIdentity src).hash;
          projectorIdentity = (purifier.inputIdentity toolchain).hash;
          git = map (entry: {
            source = entry.cargo.source;
            inherit (entry) hash;
          }) (builtins.filter (entry: entry ? git) entries);
          identity = (purifier.inputIdentity identity).hash;
          dependencies = map (input: {
            repository = toString input.repository;
            catalog = input.repository.catalog;
          }) selected;
        };
        passAsFile = [ "request" ];
        passthru.catalog = "${projection.metadata}/catalog.json";
      }
      ''
        export HOME="$TMPDIR/home"
        ${lib.getExe python} ${projector} "$requestPath" "$out" \
          "$metadata/catalog.json" ${lib.getExe cargo}
        mkdir -p "$out/nix-support"
        printf '%s\n' ${
          lib.escapeShellArgs (
            [ (toString src) ]
            ++ lib.concatMap (input: [
              (toString input.repository)
              (toString input.producer)
            ]) selected
          )
        } > "$out/nix-support/inputs"
      '';
in
projection
