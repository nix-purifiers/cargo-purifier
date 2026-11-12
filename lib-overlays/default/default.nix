# SPDX-License-Identifier: MIT
#
# The `cargo-purifier` library namespace, which holds the reader of the
# purifier contract:
#
#   lib.cargo-purifier.readLock { src, lockFile ? ... }
#
# lib-overlays/default/read-lock.nix documents it. The builder, buildCargoProject, is a
# package in the scope `pkgs.cargo-purifier`, which the default package
# overlay adds.
{ closure-inputs, ... }:
{

  overlay = final: prev: {
    cargo-purifier = {
      sharedLibrarySource = "${closure-inputs.purifier}";
      readLock = import ./read-lock.nix { lib = final; };
    };
  };

}
