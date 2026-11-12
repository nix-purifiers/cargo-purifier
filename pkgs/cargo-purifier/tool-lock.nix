# SPDX-License-Identifier: MIT
#
# The lock in memory for the command's tool expressions, which run outside
# the flake: readLock, and its problems as the error when there are any.
{
  lib,
  # The workspace root.
  src,
  # cargo-purifier.lock.json.
  lockFile,
  # Cargo.lock.
  cargoLock ? src + "/Cargo.lock",
}:
let
  lock = import ../../lib-overlays/default/read-lock.nix { inherit lib; } {
    inherit src lockFile cargoLock;
  };
in
if lock.problems != [ ] then throw (lib.concatStringsSep "\n" lock.problems) else lock
