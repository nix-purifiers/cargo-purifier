# A registry dependency whose build script writes next to its manifest, like
# Tauri's permission generator. The vendored input really lives in the store.
{ pkgs, cargo-purifier }:
let
  vendor = pkgs.runCommand "writable-vendor-input" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    mkdir -p "$out/.cargo"
    cp -r ${./fixtures/writable-vendor/dependency} "$out/source-generator-1.0.0"
    chmod -R u+w "$out"
    export VENDOR_ROOT="$out"
    python3 - <<'PY'
    import hashlib, json, os
    from pathlib import Path
    root = Path(os.environ["VENDOR_ROOT"]) / "source-generator-1.0.0"
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    (root / ".cargo-checksum.json").write_text(json.dumps({"files": files, "package": "0" * 64}))
    PY
    cat >"$out/.cargo/config.toml" <<'EOF_CONFIG'
    [source.crates-io]
    replace-with = "nix"
    [source.nix]
    directory = "cargo-vendor-dir"
    EOF_CONFIG
  '';
in
pkgs.runCommand "cargo-writable-vendor"
  {
    nativeBuildInputs = [
      cargo-purifier
      pkgs.cargo
      pkgs.rustc
      pkgs.stdenv.cc
    ];
  }
  ''
    cp -r ${./fixtures/writable-vendor/consumer} project
    chmod -R u+w project
    cd project
    export CARGO_HOME="$TMPDIR/caller/.cargo"
    mkdir -p "$CARGO_HOME"
    mkdir -p "$TMPDIR/caller/config-input"
    echo original > "$TMPDIR/caller/config-input/sentinel"
    cat > "$CARGO_HOME/config" <<'EOF_HOME'
    [env]
    PURIFIER_CONFIG_INPUT = { value = "config-input", relative = true }
    EOF_HOME
    cp "$CARGO_HOME/config" "$TMPDIR/original-config"
    mkdir -p .cargo
    cat > .cargo/config.toml <<'EOF_PROJECT'
    [source.crates-io]
    replace-with = "project-original"
    [source.project-original]
    registry = "sparse+https://example.invalid/index/"
    EOF_PROJECT
    CARGO_PURIFIER=0 cargo --config 'source.crates-io.replace-with="nix"' \
      --config 'source.nix.directory="${vendor}"' generate-lockfile --offline
    printf '{}\n' > cargo-purifier.lock.json
    mkdir -p target/cargo-purifier
    ln -s ${vendor} target/cargo-purifier/vendor
    cargo-purifier _vendor-stamp Cargo.lock cargo-purifier.lock.json > target/cargo-purifier/stamp
    cargo test --offline
    cmp "$TMPDIR/original-config" "$CARGO_HOME/config"
    test ! -e ${vendor}/source-generator-1.0.0/generated
    find target/cargo-purifier/vendor-worktrees -name permissions.toml | grep .
    # A second command reuses the local source copy and its generated files.
    cargo test --offline
    test ! -e ${vendor}/source-generator-1.0.0/generated
    project=$PWD
    mkdir -p "$TMPDIR/unrelated/src"
    cat > "$TMPDIR/unrelated/Cargo.toml" <<'EOF_OTHER'
    [package]
    name = "unrelated-project"
    version = "0.1.0"
    edition = "2021"
    [dependencies]
    not-in-the-requested-lock = "99"
    EOF_OTHER
    touch "$TMPDIR/unrelated/src/lib.rs"
    ln -s "$project" "$TMPDIR/manifest project"
    cd "$TMPDIR/unrelated"
    cargo metadata --manifest-path "$TMPDIR/manifest project/Cargo.toml" \
      --offline --locked --format-version 1 > "$TMPDIR/spaced-manifest.json"
    cargo metadata --manifest-path="$project/Cargo.toml" \
      --offline --locked --format-version 1 > "$TMPDIR/equals-manifest.json"
    cd "$project"
    cargo run --offline --locked --example manifest-option -- \
      --manifest-path application-input
    touch "$out"
  ''
