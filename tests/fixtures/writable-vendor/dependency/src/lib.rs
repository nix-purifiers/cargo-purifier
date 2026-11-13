pub fn generated() -> bool {
    include_str!("../generated/permissions.toml").contains("allow = true")
}
