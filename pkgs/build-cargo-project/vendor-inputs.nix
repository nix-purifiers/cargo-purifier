# SPDX-License-Identifier: MIT
{
  lib,
  purifier,
  runCommand,
  python3,
  cargo,
  base,
  dependencies,
  repositoryTools ? ../../tools,
}:
let
  selected = purifier.projectInputs {
    inherit dependencies;
    formats = [ "cargo" ];
  };
  python = python3.withPackages (packages: [ packages.tomlkit ]);
in
runCommand "cargo-vendor-with-inputs"
  {
    request = builtins.toJSON {
      inherit base;
      dependencies = map (input: {
        repository = toString input.repository;
        catalog = input.repository.catalog;
      }) selected;
    };
    passAsFile = [ "request" ];
  }
  ''
    ${lib.getExe python} ${repositoryTools}/cargo_inputs.py vendor \
      "$requestPath" "$out" ${lib.getExe cargo}
    mkdir -p "$out/nix-support"
    printf '%s\n' ${
      lib.escapeShellArgs (
        lib.concatMap (input: [
          (toString input.repository)
          (toString input.producer)
        ]) selected
      )
    } > "$out/nix-support/inputs"
  ''
