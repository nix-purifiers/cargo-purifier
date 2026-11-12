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
  # This existing opt-in profile deliberately supplies a complete SDK.
  tools = pkgs.symlinkJoin {
    name = "cargo-purifier-current-stable";
    paths = [
      scope.cargo-purifier
      toolchain
    ];
    nativeBuildInputs = [ pkgs.makeWrapper ];
    postBuild = ''
      wrapProgram $out/bin/cargo \
        --run ": 'cargo-purifier native-cargo wrapper'" \
        --prefix PATH : ${
          pkgs.lib.makeBinPath [
            toolchain
            pkgs.stdenv.cc
          ]
        }
      wrapProgram $out/bin/cargo-purifier \
        --prefix PATH : ${
          pkgs.lib.makeBinPath [
            toolchain
            pkgs.stdenv.cc
          ]
        }
    '';
    meta.mainProgram = "cargo-purifier";
  };
}
