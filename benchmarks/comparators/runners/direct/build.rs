use std::env;

fn main() {
    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        // Scope LC_UUID removal to the delivered executable. A global linker
        // flag also strips proc-macro dylibs, which macOS then refuses to load.
        println!("cargo:rustc-link-arg-bin=pyowl-core-direct-comparator=-Wl,-no_uuid");
    }
}
