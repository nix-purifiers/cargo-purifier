# Exercise two versions in the same replacement source and SDK metadata from
# the project directory, using native Cargo for both graphs.
{ pkgs, cargo-purifier }:
let
  sdkSource = pkgs.runCommand "stdlib-source-fixture" { } ''
    mkdir -p "$out/lib/rustlib/src/rust/library/std/src"
    cd "$out/lib/rustlib/src/rust/library"
    printf '[workspace]\nmembers=["std"]\nresolver="2"\n' > Cargo.toml
    printf '[package]\nname="std-fixture"\nversion="0.0.0"\nedition="2021"\n[dependencies]\nitoa="=1.0.0"\n' > std/Cargo.toml
    touch std/src/lib.rs
    cat > Cargo.lock <<'EOF'
    version = 4
    [[package]]
    name = "std-fixture"
    version = "0.0.0"
    dependencies = ["itoa"]
    [[package]]
    name = "itoa"
    version = "1.0.0"
    source = "registry+https://github.com/rust-lang/crates.io-index"
    checksum = "d2f3e61cf687687b30c9e6ddf0fc36cf15f035e66d491e6da968fa49ffa9a378"
    EOF
  '';
  rustc = pkgs.writeShellScript "fixture-rustc" ''
    if [ "$#" = 2 ] && [ "$1" = --print ] && [ "$2" = sysroot ]; then
      echo ${sdkSource}
    else
      exec ${pkgs.rustc}/bin/rustc "$@"
    fi
  '';
  vendor = import ../pkgs/cargo-purifier/tool-vendor.nix {
    nixpkgs = pkgs.path;
    system = pkgs.stdenv.hostPlatform.system;
    src = ./fixtures/workspace;
    lockFile = ./fixtures/workspace/cargo-purifier.lock.json;
    stdlibLock = "${sdkSource}/lib/rustlib/src/rust/library/Cargo.lock";
  };
in
pkgs.runCommand "cargo-stdlib-vendor"
  {
    nativeBuildInputs = [
      cargo-purifier
      pkgs.cargo
      pkgs.rustc
      pkgs.python3
    ];
  }
  ''
    cp -r ${./fixtures/workspace} project
    chmod -R u+w project
    cd project
    export RUSTC=${rustc}
    export CARGO_HOME="$TMPDIR/empty-cargo-home"
    mkdir -p target/cargo-purifier
    ln -s ${vendor} target/cargo-purifier/vendor
    cargo-purifier _vendor-stamp Cargo.lock cargo-purifier.lock.json > target/cargo-purifier/stamp
    cp Cargo.lock "$TMPDIR/project.lock"
    cargo metadata --offline --locked --format-version 1 > "$TMPDIR/project.json"
    cargo metadata --manifest-path ${sdkSource}/lib/rustlib/src/rust/library/Cargo.toml \
      --offline --locked --format-version 1 > "$TMPDIR/sdk.json"
    unset RUSTC
    mkdir -p .cargo
    printf '[build]\nrustc="%s"\n' ${rustc} > .cargo/config.toml
    cargo-purifier _vendor-stamp Cargo.lock cargo-purifier.lock.json > "$TMPDIR/config-stamp"
    cmp target/cargo-purifier/stamp "$TMPDIR/config-stamp"
    cargo metadata --manifest-path ${sdkSource}/lib/rustlib/src/rust/library/Cargo.toml \
      --offline --locked --format-version 1 > "$TMPDIR/config-sdk.json"
    CARGO_BUILD_RUSTC=${rustc} cargo metadata \
      --manifest-path ${sdkSource}/lib/rustlib/src/rust/library/Cargo.toml \
      --offline --locked --format-version 1 > "$TMPDIR/env-sdk.json"
    cargo --config 'build.rustc="${rustc}"' metadata \
      --manifest-path ${sdkSource}/lib/rustlib/src/rust/library/Cargo.toml \
      --offline --locked --format-version 1 > "$TMPDIR/cli-sdk.json"
    python3 - "$TMPDIR" <<'PY'
    import json, pathlib, sys
    root = pathlib.Path(sys.argv[1])
    project = json.loads((root / 'project.json').read_text())
    sdk = json.loads((root / 'sdk.json').read_text())
    assert {p['version'] for p in project['packages'] if p['name'] == 'itoa'} == {'1.0.18'}
    assert {p['version'] for p in sdk['packages'] if p['name'] == 'itoa'} == {'1.0.0'}
    assert all('/vendor-worktrees/' in p['manifest_path'] for p in sdk['packages'] if p['source'])
    for name in ('config-sdk', 'env-sdk', 'cli-sdk'):
        assert json.loads((root / (name + '.json')).read_text()) == sdk
    PY
    cmp Cargo.lock "$TMPDIR/project.lock"
    test ! -e "$CARGO_HOME/registry/src"
    touch "$out"
  ''
