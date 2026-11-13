fn main() {
    // Like cbindgen, build scripts query the complete dependency graph through
    // Cargo's executable. An empty registry cache must remain sufficient.
    let input = std::path::PathBuf::from(std::env::var_os("PURIFIER_CONFIG_INPUT").unwrap());
    assert_eq!(std::fs::read_to_string(input.join("sentinel")).unwrap(), "original\n");
    let output = std::process::Command::new(std::env::var_os("CARGO").unwrap())
        .args(["metadata", "--offline", "--locked", "--format-version", "1"])
        .output()
        .unwrap();
    assert!(output.status.success(), "child Cargo metadata failed: {}",
        String::from_utf8_lossy(&output.stderr));
}
