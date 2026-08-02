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
pub(crate) use canonical::{
    canonical_contains_tag, canonical_field_count, scan_canonical, validate_iri, Category,
    ScanBudget,
};
#[allow(unused_imports)]
pub(crate) use component_index::{
    structural_digest_v1, structural_digest_v2, NativeComponentDigestIndex, StructuralDigest,
};
#[allow(unused_imports)]
pub(crate) use components::{
    ComponentCounters, ComponentFieldRef, ComponentId, ComponentIdRemap, ComponentRecordRef,
    ComponentSequenceKind, ComponentSequenceRef, ComponentTables, FrozenComponentBuild,
    NativeComponentArena, NativeComponentBuilder, PendingComponentId,
};
#[cfg(feature = "test-hooks")]
pub(crate) use encoded_columns::prepare_encoded_structural_columns_from_tables_with_allocation_probe_v2;
#[allow(unused_imports)]
pub(crate) use encoded_columns::{
    build_encoded_structural_columns_from_tables_v2, build_encoded_structural_columns_v2,
    prepare_encoded_structural_columns_from_tables_v2, EncodedColumnCountersV2, EncodedRootKindV2,
    EncodedRootTableV2, EncodedRootV2, EncodedStructuralBufferLayoutV2, EncodedStructuralBuffersV2,
    EncodedStructuralColumnsV2, PreparedEncodedStructuralColumnsV2,
    ENCODED_STRUCTURAL_MODEL_SCHEMA_V2, ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
    ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
};
#[allow(unused_imports)]
pub(crate) use ids::{AnonymousId, CanonicalRowId, DocumentId, SequenceId};
#[allow(unused_imports)]
pub(crate) use tables::{ArenaCounters, SequenceKind};
