#![no_main]
#![allow(unexpected_cfgs)]

use libfuzzer_sys::fuzz_target;

#[path = "../../../../native/src/cancel.rs"]
mod cancel;
#[path = "../../../../native/src/canonical.rs"]
mod canonical;
#[path = "../../../../native/src/error.rs"]
mod error;
#[path = "../../../../native/src/hash.rs"]
mod hash;
#[cfg(not(fuzzing))]
#[path = "../../../../native/src/index/mod.rs"]
mod index;
#[path = "../../../../native/src/limits.rs"]
mod limits;
#[path = "../../../../native/src/model/mod.rs"]
mod model;
#[path = "../../../../native/src/parse/mod.rs"]
mod parse;
#[cfg(not(fuzzing))]
#[path = "../../../../native/src/publication/mod.rs"]
mod publication;
#[path = "../../../../native/src/session.rs"]
mod session;
#[path = "../../../../native/src/source.rs"]
mod source;

#[cfg(not(fuzzing))]
fn python_error(error: error::NativeError) -> pyo3::PyErr {
    pyo3::exceptions::PyRuntimeError::new_err((error.code, error.message))
}

use cancel::{Cancellation, Guard};
use limits::Limits;
use session::Session;
use source::SourceRequest;

fuzz_target!(|data: &[u8]| {
    if data.len() > 1024 * 1024 {
        return;
    }
    let limits = Limits::default();
    let cancellation = Cancellation::with_duration(None);
    let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
    if let Ok(mut session) = Session::new(&mut guard, &limits, data.len()) {
        let request = SourceRequest {
            source: data,
            allow_swrl: true,
        };
        let _ = parse::parse(request, &mut session);
    }
});
