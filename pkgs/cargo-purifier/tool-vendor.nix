# SPDX-License-Identifier: MIT
#
# What the cargo wrapper builds: the vendored sources of a workspace, from
# the nixpkgs the wrapper was built with.
#
#   nix-build tool-vendor.nix --arg nixpkgs <path> --argstr system <system> \
#     --arg src <workspace root> --arg lockFile <cargo-purifier.lock.json> \
#     --arg cargoLock <Cargo.lock>
{
  nixpkgs,
  system,
  src,
  lockFile,
  sharedSource ? null,
  repositoryTools ? ../../tools,
  cargoLock ? src + "/Cargo.lock",
  stdlibLock ? null,
}:
let
  pkgs = import nixpkgs { inherit system; };
  lock = import ./tool-lock.nix {
    inherit (pkgs) lib;
    inherit src lockFile cargoLock;
  };
  base = import ../build-cargo-project/vendor.nix {
    inherit (pkgs)
      lib
      rustPlatform
      fetchurl
      fetchgit
      runCommand
      ;
    inherit cargoLock;
    inherit (lock) entries;
    provided = lock.provided or [ ];
  };
  purifier =
    if sharedSource == null then
      null
    else
      import (sharedSource + "/default.nix") { inherit (pkgs) lib; };
  dependencies =
    if (lock.projectInputs or [ ]) == [ ] then
      [ ]
    else
      purifier.lockedProjectInputs {
        inputs = lock.projectInputs;
        inherit system;
      };
  projectVendor =
    if dependencies == [ ] then
      base
    else
      import ../build-cargo-project/vendor-inputs.nix {
        inherit (pkgs)
          lib
          runCommand
          python3
          cargo
          ;
        inherit
          purifier
          base
          dependencies
          repositoryTools
          ;
      };
  stdlib = pkgs.rustPlatform.importCargoLock { lockFile = stdlibLock; };
in
if stdlibLock == null then
  projectVendor
else
  pkgs.runCommand "cargo-vendor-with-stdlib" { nativeBuildInputs = [ pkgs.python3 ]; } ''
      python3 - ${projectVendor} ${stdlib} "$out" <<'PY'
    import json, pathlib, shutil, sys
    project, stdlib, output = map(pathlib.Path, sys.argv[1:])
    shutil.copytree(project, output, symlinks=True)
    output.chmod(0o755)
    for crate in stdlib.iterdir():
        checksum = crate / '.cargo-checksum.json'
        if not checksum.is_file():
            continue
        destination = output / crate.name
        if destination.exists():
            old = json.loads((destination / '.cargo-checksum.json').read_text())
            new = json.loads(checksum.read_text())
            if not old.get('package') or old['package'] != new.get('package'):
                raise SystemExit('conflicting project/SDK crate: ' + crate.name)
        else:
            destination.symlink_to(crate)
    PY
  ''
