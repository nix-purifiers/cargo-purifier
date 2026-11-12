fn main() {
    let arguments: Vec<_> = std::env::args().skip(1).collect();
    assert_eq!(arguments, ["--manifest-path", "application-input"]);
}
