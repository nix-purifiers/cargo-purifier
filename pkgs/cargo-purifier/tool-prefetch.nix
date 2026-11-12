# SPDX-License-Identifier: MIT
#
# What `cargo-purifier prefetch` builds: everything the development loop and
# the editor need, in one store path that its GC root keeps alive.
#
#   vendor    the vendored sources of the workspace
#   tools     the local cargo-purifier command and Cargo wrapper. The project's
#             installed SDK and editor tools remain selected by the caller.
#
#   nix-build tool-prefetch.nix --arg nixpkgs <path> --argstr system <system> \
#     --arg src <workspace root> --arg lockFile <cargo-purifier.lock.json> \
#     --arg cargoLock <Cargo.lock> --arg tools 'builtins.storePath "<the cargo-purifier package>"'
{
  nixpkgs,
  system,
  src,
  lockFile,
  cargoLock ? src + "/Cargo.lock",
  stdlibLock ? null,
  # The cargo-purifier package that runs the prefetch, or null when the
  # command runs outside it.
  tools,
  sharedSource ? null,
  repositoryTools ? ../../tools,
}:
let
  pkgs = import nixpkgs { inherit system; };
  vendor = import ./tool-vendor.nix {
    inherit
      nixpkgs
      system
      src
      lockFile
      cargoLock
      stdlibLock
      sharedSource
      repositoryTools
      ;
  };
in
pkgs.linkFarm "cargo-purifier-prefetch" (
  {
    inherit vendor;
  }
  // pkgs.lib.optionalAttrs (tools != null) { inherit tools; }
)
