# SPDX-License-Identifier: MIT
# Required current stable release coverage, independently of the default SDK pin.
{
  pkgs,
  overlay,
  readLock,
  ripgrep,
  historicalRustSource,
  rustOverlaySource,
}:
let
  lib = pkgs.lib;
  stable = import ../pkgs/cargo-purifier/current-stable-scope.nix {
    inherit pkgs overlay rustOverlaySource;
  };
  inherit (stable) selected scope toolchain;
  checks = import ./default.nix {
    pkgs = selected;
    inherit readLock ripgrep historicalRustSource;
    inherit (scope) buildCargoProject cargo-purifier;
  };
in
{
  current-stable-tools = scope.cargo-purifier;
  current-stable = pkgs.runCommand "cargo-current-stable-1.99.1" { } ''
    test "$(${toolchain}/bin/rustc --version | cut -d' ' -f2)" = 1.99.1
    test "$(${scope.cargo-purifier}/bin/cargo --version | cut -d' ' -f2)" = 1.99.1
    test "$(${scope.cargo-purifier}/bin/rust-analyzer --version | cut -d' ' -f2)" = 1.99.1
    ${lib.concatMapStringsSep "\n" (check: "test -e ${check}") (builtins.attrValues checks)}
    printf '%s\n' 'Rust/Cargo 1.99.1: full fixture and ripgrep checks passed' > "$out"
  '';
}
