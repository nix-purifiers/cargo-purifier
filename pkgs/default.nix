# SPDX-License-Identifier: MIT
#
# The `cargo-purifier` scope that the default package overlay adds to the
# package set. `lib` here is the library this flake composes, which holds
# the purifier library and readLock.
{ callPackage, lib, ... }:
let
  command = callPackage ./cargo-purifier { };
  cargo = callPackage ./cargo { inherit command; };
  rust-analyzer = callPackage ./rust-analyzer { inherit cargo; };
in
{
  # The builder of the purifier contract (pkgs/build-cargo-project/default.nix).
  buildCargoProject = callPackage ./build-cargo-project/default.nix {
    inherit (lib) purifier;
    inherit (lib.cargo-purifier) readLock;
  };
  # cargo with the locked sources: the development loop.
  inherit cargo;
  # rust-analyzer on the locked sources: the editor.
  inherit rust-analyzer;
  # The `cargo-purifier` command, with the cargo wrapper and rust-analyzer
  # beside it. `cargo-purifier prefetch` keeps this package alive in the
  # project's GC root, through the store path it records here.
  cargo-purifier = callPackage (
    { symlinkJoin, makeWrapper }:
    symlinkJoin {
      name = "cargo-purifier";
      paths = [
        command
        cargo
        rust-analyzer
      ];
      nativeBuildInputs = [ makeWrapper ];
      postBuild = ''
        wrapProgram $out/bin/cargo-purifier --set CARGO_PURIFIER_TOOLS $out
      '';
      meta.mainProgram = "cargo-purifier";
    }
  ) { };
}
