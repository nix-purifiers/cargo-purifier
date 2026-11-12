fn main() {
    let root = std::path::PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let generated = root.join("generated");
    std::fs::create_dir_all(&generated).unwrap();
    std::fs::write(generated.join("permissions.toml"), "allow = true\n").unwrap();
}
