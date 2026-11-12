# SPDX-License-Identifier: MIT
#
# What `cargo-purifier publish` builds: the release (the .crate) of one
# workspace package, from the nixpkgs the command was built with.
#
#   nix-build tool-release.nix --arg nixpkgs <path> --argstr system <system> \
#     --arg src <workspace root> --arg lockFile <cargo-purifier.lock.json> \
#     --arg cargoLock <Cargo.lock> --argstr package <package>
{
  nixpkgs,
  system,
  src,
  lockFile,
  cargoLock ? src + "/Cargo.lock",
  package,
  repository ? false,
  sharedSource ? null,
  repositoryTools ? ../../tools,
}:
let
  pkgs = import nixpkgs { inherit system; };
  lock = import ./tool-lock.nix {
    inherit (pkgs) lib;
    inherit src lockFile cargoLock;
  };
  purifier =
    if sharedSource == null then
      null
    else
      import (sharedSource + "/default.nix") { inherit (pkgs) lib; };
  project = import ../build-cargo-project/project.nix {
    inherit purifier repositoryTools;
    inherit (pkgs) python3;
    dependencies =
      if (lock.projectInputs or [ ]) == [ ] then
        [ ]
      else
        purifier.lockedProjectInputs {
          inputs = lock.projectInputs;
          inherit system;
        };
    inherit (pkgs)
      lib
      stdenv
      runCommand
      rustPlatform
      fetchurl
      fetchgit
      ;
    inherit src cargoLock lock;
    inherit (lock) entries;
  };
in
if repository then project.projections.cargo else project.release.${package}
