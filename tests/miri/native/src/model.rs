//! Pure retained-arena modules exercised under Miri without the CPython FFI.

#[path = "../../../../native/src/model/arena.rs"]
mod arena;
#[path = "../../../../native/src/model/canonical.rs"]
mod canonical;
#[path = "../../../../native/src/model/ids.rs"]
mod ids;
#[path = "../../../../native/src/model/tables.rs"]
mod tables;
