#[no_mangle]
pub extern "C" fn answer() -> u32 { 42 }
#[test]
fn answer_is_42() { assert_eq!(answer(), 42); }
