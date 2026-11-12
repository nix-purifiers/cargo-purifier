# SPDX-License-Identifier: MIT
#
# conformance-offline: after `cargo-purifier prefetch`, the development loop
# and the editor work on the workspace fixture with no network, which the
# sandbox has none of. The sandbox has no Nix daemon either, so a stand-in
# nix-build answers the prefetch with what tool-prefetch.nix builds, and
# refuses every later call: after the prefetch, nothing needs Nix.
{
  pkgs,
  # The cargo-purifier package: the command, the cargo wrapper and
  # rust-analyzer.
  cargo-purifier,
  # What tool-prefetch.nix builds for the workspace fixture.
  prefetched,
}:
pkgs.runCommand "conformance-offline"
  {
    nativeBuildInputs = [
      cargo-purifier
      pkgs.cargo
      pkgs.rustc
      pkgs.stdenv.cc
      pkgs.rust-analyzer
    ];
  }
  ''
    export RUST_SRC_PATH=${pkgs.rustPlatform.rustLibSrc}
    export HOME=$PWD/home CARGO_HOME=$PWD/cargo-home
    cp -r ${./fixtures/workspace} workspace
    chmod -R u+w workspace
    mkdir bin
    export PATH=$PWD/bin:$PATH
    cat >bin/nix-build <<'EOF'
    #!${pkgs.runtimeShell}
    case "$1" in
      */tool-prefetch.nix)
        while [ $# -gt 0 ]; do
          if [ "$1" = --out-link ]; then link=$2; fi
          shift
        done
        ln -sfn ${prefetched} "$link"
        echo ${prefetched}
        ;;
      *)
        echo "error: nix-build was called after the prefetch: $*" >&2
        exit 1
        ;;
    esac
    EOF
    chmod +x bin/nix-build
    cd workspace

    cargo-purifier prefetch
    test "$(readlink target/cargo-purifier/prefetch)" = ${prefetched}

    # The development loop and the editor.
    cargo build --quiet
    test "$(cargo run --quiet --package hello)" = "hello from 2 crates and 1.5"
    cargo test --quiet --workspace
    rust-analyzer analysis-stats --skip-inference . >../analysis 2>&1 || {
      cat ../analysis >&2
      exit 1
    }
    grep -q 'dependency lines of code: [1-9]' ../analysis || {
      echo "rust-analyzer did not load the locked dependencies" >&2
      cat ../analysis >&2
      exit 1
    }

    # A dependency missing from Cargo.lock stops the command, and names the
    # command that fixes it.
    cp hello/Cargo.toml ../hello.toml
    sed -i 's/^\[dependencies\]$/[dependencies]\nsmallvec = "1"/' hello/Cargo.toml
    if cargo build --package hello 2>../stderr; then
      echo "cargo built a dependency Cargo.lock lacks" >&2
      exit 1
    fi
    grep -qF 'Run `cargo-purifier lock` to lock them' ../stderr || {
      echo "missing-dependency rejection did not explain how to relock" >&2
      cat ../stderr >&2
      exit 1
    }
    cp ../hello.toml hello/Cargo.toml

    # So does a Cargo.lock that changed since the lock.
    echo '# edited' >>Cargo.lock
    if cargo build 2>../stderr; then
      echo "cargo built from a Cargo.lock the purifier lockfile was not made from" >&2
      exit 1
    fi
    grep -qF 'Run `cargo-purifier lock` if Cargo.lock changed' ../stderr || {
      echo "changed-lock rejection did not explain how to relock" >&2
      cat ../stderr >&2
      exit 1
    }

    # Commands that read no sources run plain cargo.
    cargo --version | grep -q '^cargo '
    touch $out
  ''
