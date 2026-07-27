#![no_main]
#![allow(unexpected_cfgs)]

use libfuzzer_sys::fuzz_target;

// The production wire module reads this crate-level contract constant when it
// builds validation receipts. Keep the fuzz crate's root equivalent to the
// extension crate root; test_manifest.py guards the value against drift.
const ABI_VERSION: u32 = 3;

#[path = "../../../../native/src/cancel.rs"]
mod cancel;
#[path = "../../../../native/src/error.rs"]
mod error;
#[path = "../../../../native/src/hash.rs"]
mod hash;
#[path = "../../../../native/src/limits.rs"]
mod limits;
#[path = "../../../../native/src/model/mod.rs"]
mod model;
#[path = "../../../../native/src/wire/mod.rs"]
mod wire;

use cancel::{Cancellation, Guard};
use limits::Limits;
use wire::WireArena;

fuzz_target!(|data: &[u8]| {
    if data.len() > 4 * 1024 * 1024 {
        return;
    }
    let limits = Limits::default();
    let cancellation = Cancellation::with_duration(None);
    let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
    let _ = WireArena::decode(data.to_vec(), &limits, &mut guard);
});
