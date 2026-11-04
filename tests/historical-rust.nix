# SPDX-License-Identifier: MIT
# One actual compiler/target ABI point, separate from native CLI coverage.
{
  pkgs,
  historicalRustSource,
  buildCargoProject,
}:
let
  inherit (pkgs.stdenv.hostPlatform) system;
  old = import historicalRustSource { inherit system; };
  rustPlatform = pkgs.makeRustPlatform { inherit (old) cargo rustc; };
  project = (buildCargoProject.override { inherit rustPlatform; }) {
    src = ./fixtures/historical-target;
    doCheck = true;
  };
  package = project.packages.historical-target;
in
pkgs.runCommand "rust-141-target-and-c-abi"
  {
    nativeBuildInputs = [
      old.cargo
      old.rustc
      pkgs.stdenv.cc
      pkgs.gnutar
    ];
    passthru = { inherit project; };
  }
  ''
    export HOME="$TMPDIR/home"
    export CARGO_HOME="$TMPDIR/cargo-home"
    mkdir -p "$HOME" "$CARGO_HOME"
    test "$(${package}/bin/historical-target)" = '42 compiled by rustc 1.41.0'
    test -e ${project.tests}
    cat > consumer.c <<'C'
    #include <stdint.h>
    extern uint32_t answer(void);
    int main(void) { return answer() == 42 ? 0 : 1; }
    C
    cc consumer.c -L${package}/lib -lhistorical_target -Wl,-rpath,${package}/lib -o consumer
    ./consumer
    mkdir ordinary
    cd ordinary
    tar -xf ${project.release.historical-target.crate}
    cd historical-target-0.1.0
    cargo build --offline --locked
    test "$(target/debug/historical-target)" = '42 compiled by rustc 1.41.0'
    touch "$out"
  ''
