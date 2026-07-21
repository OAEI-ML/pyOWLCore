//! Miri-compatible ownership and canonicalization slice of the native crate.

#![forbid(unsafe_code)]
#![allow(dead_code)]

#[path = "../../../../native/src/canonical.rs"]
mod canonical;
#[path = "../../../../native/src/error.rs"]
mod error;
#[path = "../../../../native/src/hash.rs"]
mod hash;
#[path = "../../../../native/src/limits.rs"]
mod limits;
mod model;
