# SPDX-License-Identifier: MIT
#
# The lock run and the publish step, in the sandbox and without the
# network. The check sets up a cargo registry of its own (a git index, and
# an HTTP server on 127.0.0.1 for downloads and publishing) and a git
# repository. It then runs `cargo-purifier lock` on a workspace that takes a crate
# from each, and `cargo-purifier publish` of a crate Nix packaged to the
# registry.
{
  pkgs,
  # The cargo-purifier package.
  cargo-purifier,
  # A .crate file buildCargoProject packaged: greet 0.1.0.
  crate,
}:
pkgs.runCommand "local-registry"
  {
    nativeBuildInputs = [
      cargo-purifier
      pkgs.cargo
      pkgs.rustc
      pkgs.git
      pkgs.python3
      # nix-prefetch-git hashes with the nix-hash on the PATH.
      pkgs.nix
    ];
  }
  ''
    export HOME=$PWD/home CARGO_HOME=$PWD/cargo-home
    mkdir -p $HOME
    git config --global user.email check@example.invalid
    git config --global user.name check
    git config --global init.defaultBranch main

    python3 ${./registry-server.py} port www &
    # Darwin builds have no PID namespace, so a server left running
    # would keep the build's log open after the builder exits.
    server=$!
    trap 'kill $server' EXIT
    mkdir -p www
    while [ ! -s port ]; do sleep 0.1; done
    port=$(cat port)

    # The crate the registry serves.
    mkdir -p tiny/src
    cat >tiny/Cargo.toml <<'EOF'
    [package]
    name = "tiny"
    version = "0.1.0"
    edition = "2021"
    description = "A crate of the check's registry"
    license = "MIT"
    EOF
    echo 'pub fn answer() -> u32 { 42 }' >tiny/src/lib.rs
    (cd tiny && cargo package --offline --quiet)
    mkdir -p www/dl/ti/ny
    cp tiny/target/package/tiny-0.1.0.crate www/dl/ti/ny/
    cksum=$(sha256sum tiny/target/package/tiny-0.1.0.crate | cut -d ' ' -f 1)

    # The registry's index, with a download template that uses {prefix}.
    dl="http://127.0.0.1:$port/dl/{prefix}/{crate}-{version}.crate"
    mkdir -p index/ti/ny
    printf '{"dl":"%s","api":"http://127.0.0.1:%s"}\n' "$dl" "$port" >index/config.json
    printf '{"name":"tiny","vers":"0.1.0","deps":[],"cksum":"%s","features":{},"yanked":false}\n' "$cksum" \
      >index/ti/ny/tiny
    git -C index init --quiet
    git -C index add -A
    git -C index commit --quiet -m index

    # A git dependency.
    mkdir -p gitdep/src
    cat >gitdep/Cargo.toml <<'EOF'
    [package]
    name = "gitdep"
    version = "0.1.0"
    edition = "2021"
    EOF
    echo 'pub fn name() -> &'"'"'static str { "gitdep" }' >gitdep/src/lib.rs
    git -C gitdep init --quiet
    git -C gitdep add -A
    git -C gitdep commit --quiet -m gitdep

    # The workspace.
    mkdir -p app/src app/.cargo
    cat >app/.cargo/config.toml <<EOF
    [registries.local]
    index = "file://$PWD/index"
    EOF
    cat >app/Cargo.toml <<EOF
    [package]
    name = "app"
    version = "0.1.0"
    edition = "2021"

    [dependencies]
    tiny = { version = "0.1", registry = "local" }
    gitdep = { git = "file://$PWD/gitdep" }
    EOF
    echo 'fn main() { println!("{} {}", gitdep::name(), tiny::answer()); }' >app/src/main.rs

    cargo-purifier lock --manifest-path app/Cargo.toml

    # A relock from scratch reproduces the lockfile byte for byte.
    mv app/cargo-purifier.lock.json first.lock.json
    cargo-purifier lock --manifest-path app/Cargo.toml
    cmp first.lock.json app/cargo-purifier.lock.json

    CARGO_REGISTRIES_LOCAL_TOKEN=local-registry-token \
      cargo-purifier publish --manifest-path app/Cargo.toml --registry local \
      --crate ${crate}/greet-0.1.0.crate

    python3 ${./check-local-registry.py} app/cargo-purifier.lock.json "file://$PWD/index" "$dl" \
      "file://$PWD/gitdep" upload.bin upload-auth ${crate}/greet-0.1.0.crate
    touch $out
  ''
