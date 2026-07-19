//! Owned, private canonical model arena primitives.

#[allow(dead_code)]
mod arena;
#[allow(dead_code)]
#[path = "../builder.rs"]
mod builder;
mod canonical;
#[allow(dead_code)]
mod components;
#[allow(dead_code)]
mod ids;
#[allow(dead_code)]
mod tables;

#[allow(unused_imports)]
pub(crate) use arena::{CanonicalRow, ModelArena, NativeArena};
#[allow(unused_imports)]
pub(crate) use builder::NativeArenaBuilder;
pub(crate) use canonical::{scan_canonical, validate_iri, Category, ScanBudget};
#[allow(unused_imports)]
pub(crate) use components::{
    ComponentCounters, ComponentId, ComponentIdRemap, ComponentTables, FrozenComponentBuild,
    NativeComponentArena, NativeComponentBuilder, PendingComponentId,
};
#[allow(unused_imports)]
pub(crate) use ids::{AnonymousId, CanonicalRowId, DocumentId, SequenceId};
#[allow(unused_imports)]
pub(crate) use tables::{ArenaCounters, SequenceKind};
