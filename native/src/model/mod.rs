//! Owned, private canonical model arena primitives.

#[allow(dead_code)]
mod arena;
#[allow(dead_code)]
#[path = "../builder.rs"]
mod builder;
mod canonical;
#[allow(dead_code)]
mod component_index;
#[allow(dead_code)]
mod components;
#[allow(dead_code)]
mod encoded_columns;
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
pub(crate) use component_index::{
    structural_digest_v1, NativeComponentDigestIndex, StructuralDigest,
};
#[allow(unused_imports)]
pub(crate) use components::{
    ComponentCounters, ComponentFieldRef, ComponentId, ComponentIdRemap, ComponentRecordRef,
    ComponentSequenceKind, ComponentSequenceRef, ComponentTables, FrozenComponentBuild,
    NativeComponentArena, NativeComponentBuilder, PendingComponentId,
};
#[allow(unused_imports)]
pub(crate) use encoded_columns::{
    build_encoded_structural_columns_from_tables_v1, build_encoded_structural_columns_v1,
    EncodedColumnCountersV1, EncodedRootKindV1, EncodedRootTableV1, EncodedRootV1,
    EncodedStructuralBuffersV1, EncodedStructuralColumnsV1, ENCODED_STRUCTURAL_MODEL_SCHEMA_V1,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V1, ENCODED_STRUCTURAL_SCHEMA_VERSION_V1,
};
#[allow(unused_imports)]
pub(crate) use ids::{AnonymousId, CanonicalRowId, DocumentId, SequenceId};
#[allow(unused_imports)]
pub(crate) use tables::{ArenaCounters, SequenceKind};
