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
    packageCargoLocks = lock.packageCargoLocks or { };
    python3 = pkgs.python3;
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
  stdlib = pkgs.rustPlatform.importCargoLock {
    lockFile = if builtins.isPath stdlibLock then stdlibLock else /. + stdlibLock;
  };
in
if stdlibLock == null then
  projectVendor
else
  pkgs.runCommand "cargo-vendor-with-stdlib"
    {
      nativeBuildInputs = [ (pkgs.python3.withPackages (p: [ p.tomlkit ])) ];
    }
    ''
      python3 ${../build-cargo-project/package_vendor.py} --stdlib ${projectVendor} ${stdlib} "$out"
    ''
