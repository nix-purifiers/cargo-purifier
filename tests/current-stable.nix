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
  binaries = (pkgs.extend (import rustOverlaySource)).rust-bin;
  toolchain = binaries.stable."1.99.1".default.override {
    extensions = [
      "rust-src"
      "rust-analyzer"
    ];
  };
  executable =
    name:
    toolchain
    // {
      meta = toolchain.meta // {
        mainProgram = name;
      };
    };
  selected = pkgs // {
    cargo = executable "cargo";
    rustc = executable "rustc";
    rust-analyzer = executable "rust-analyzer";
    rustPlatform =
      (pkgs.makeRustPlatform {
        cargo = executable "cargo";
        rustc = executable "rustc";
      })
      // {
        rustLibSrc = "${toolchain}/lib/rustlib/src/rust/library";
      };
  };
  scope = (pkgs.extend overlay).cargo-purifier.overrideScope (
    _: _: {
      pkgs = selected;
      rustPlatform = selected.rustPlatform;
    }
  );
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
