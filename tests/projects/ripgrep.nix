# SPDX-License-Identifier: MIT
#
# ripgrep 14.1.1, a real workspace of ten packages and 61 locked crates,
# with the purifier lockfile `cargo-purifier lock` wrote for it, kept
# outside ripgrep's source (projects/ripgrep/cargo-purifier.lock.json).
# ripgrep's own Cargo.lock is the one in its source.
{
  pkgs,
  buildCargoProject,
  # The cargo-purifier package: the command, the cargo wrapper and
  # rust-analyzer.
  cargo-purifier,
  # ripgrep's source at 4649aa9700619f94cf9c66876e9549d83420e16c.
  src,
}:
let
  inherit (pkgs) lib;

  lockFile = ./ripgrep/cargo-purifier.lock.json;

  project = buildCargoProject { inherit src lockFile; };

  vendor = import ../../pkgs/cargo-purifier/tool-vendor.nix {
    nixpkgs = pkgs.path;
    inherit (pkgs.stdenv.hostPlatform) system;
    inherit src;
    inherit lockFile;
  };
in
{
  # The release build of rg, from the sandbox.
  ripgrep = pkgs.runCommand "ripgrep" { } ''
    ${project.packages.ripgrep.bins.rg} --version | grep -q '^ripgrep 14.1.1'
    printf 'one\nneedle\nthree\n' >haystack
    test "$(${lib.getExe project.packages.ripgrep} --count needle haystack)" = 1
    touch $out
  '';

  # A stdenv build, not runCommand with stdenv.cc: on darwin, stdenv also
  # brings the SDK and nixpkgs' libiconv, which the libc crate links and
  # nixpkgs' SDK leaves out.
  composition-real-project =
    pkgs.runCommandCC "ripgrep-projected-native-consumer"
      {
        nativeBuildInputs = [
          pkgs.cargo
          pkgs.rustc
          pkgs.stdenv.cc
          pkgs.jq
        ];
        passthru.repository = project.projections.cargo;
      }
      ''
        export HOME="$TMPDIR/home" CARGO_HOME="$TMPDIR/cargo-home"
        mkdir -p "$CARGO_HOME"
        printf '[source.crates-io]\nreplace-with="nix"\n[source.nix]\nlocal-registry="%s"\n' \
          ${project.projections.cargo} > "$CARGO_HOME/config.toml"
        version=$(jq -r '.roots[] | select(.name == "ripgrep") | .version' \
          ${project.projections.cargo.catalog})
        cargo install --offline --quiet --root "$out" --version "=$version" ripgrep
        "$out/bin/rg" --version | grep '^ripgrep 14.1.1'
        printf 'one\nneedle\nthree\n' > haystack
        test "$("$out/bin/rg" --count needle haystack)" = 1
      '';

  # A developer's loop on ripgrep through the cargo wrapper, offline: build,
  # run, test, rust-analyzer, and a new dependency that Cargo.lock lacks. The
  # lockfile stays outside ripgrep's tree, named by CARGO_PURIFIER_LOCKFILE.
  # The sandbox has no Nix daemon, so a stand-in nix-build answers the
  # wrapper with the vendored sources its own expression builds. The build
  # has a C compiler, as a developer's machine does: on darwin, stdenv also
  # brings the SDK and nixpkgs' libiconv, which the libc crate links and
  # nixpkgs' SDK leaves out.
  ripgrep-develop =
    pkgs.runCommandCC "ripgrep-develop"
      {
        nativeBuildInputs = [
          cargo-purifier
          pkgs.cargo
          pkgs.rustc
          pkgs.rust-analyzer
        ];
      }
      ''
        export RUST_SRC_PATH=${pkgs.rustPlatform.rustLibSrc}
        export HOME=$PWD/home CARGO_HOME=$PWD/cargo-home
        export CARGO_PURIFIER_LOCKFILE=${lockFile}
        cp -r ${src} ripgrep
        chmod -R u+w ripgrep
        mkdir bin
        export PATH=$PWD/bin:$PATH
        cat >bin/nix-build <<'EOF'
        #!${pkgs.runtimeShell}
        while [ $# -gt 0 ]; do
          if [ "$1" = --out-link ]; then link=$2; fi
          shift
        done
        ln -sfn ${vendor} "$link"
        echo ${vendor}
        EOF
        chmod +x bin/nix-build
        cd ripgrep

        cargo build --quiet
        cargo run --quiet -- --version | grep -q '^ripgrep 14.1.1'
        cargo test --quiet --package grep-cli

        # rust-analyzer runs cargo metadata and cargo check through the
        # wrapper, so it loads the workspace from the vendored sources.
        rust-analyzer analysis-stats --skip-inference . >../analysis 2>&1 || {
          cat ../analysis >&2
          exit 1
        }
        # The dependencies' code is loaded only when cargo metadata resolved
        # them from the vendored sources.
        grep -q '^Database loaded:' ../analysis && grep -q 'dependency lines of code: [1-9]' ../analysis || {
          cat ../analysis >&2
          exit 1
        }

        # A dependency added to Cargo.toml but not yet locked.
        sed -i 's/^\[dependencies\]$/[dependencies]\nsmallvec = "1"/' Cargo.toml
        if cargo build --quiet 2>../stderr; then
          echo "cargo built offline with a dependency Cargo.lock lacks" >&2
          exit 1
        fi
        grep -qF 'Run `cargo-purifier lock` to lock them' ../stderr || {
          cat ../stderr >&2
          exit 1
        }
        touch $out
      '';
}
