# SPDX-License-Identifier: MIT
# Installable current stable tools without changing the default nixpkgs pin.
{
  pkgs,
  overlay,
  rustOverlaySource,
}:
let
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
in
{
  inherit selected scope toolchain;
}
