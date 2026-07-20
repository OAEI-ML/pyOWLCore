//! Exact bounded V2 scalar facade over immutable Rust-owned publication rows.
//!
//! The V2 envelope and its digest codecs remain owned by the Python handoff
//! module.  This module owns the long-lived data and implements the exact
//! attestation, paging, membership, counter, and lifecycle-adjacent operations
//! used by the registered PyO3 handle classes.

use std::alloc::Layout;
use std::borrow::Borrow;
use std::collections::HashMap;
use std::mem::size_of;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;

use pyo3::exceptions::{PyMemoryError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyBytes, PyDict, PyInt, PyModule, PyTuple, PyType};

use crate::cancel::{Cancellation, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::index::RetainedAxiomTypeIndexV1;
use crate::limits::Limits;
use crate::model::{EncodedStructuralColumnsV1, PreparedEncodedStructuralColumnsV1};

use super::records::Digest;
use super::{
    TypedFacadeCollectionV2, TypedFacadeCoordinateV2, TypedFacadePageRequestV2, TypedFacadeScopeV2,
    TypedFacadeSignatureKindV2, TypedFacadeStorageV2,
};

pub(super) const PUBLICATION_VERSION_V2: u32 = 2;
pub(super) const PUBLICATION_LEDGER_SHA256_V2: Digest = [
    0x58, 0x87, 0x39, 0x15, 0x98, 0x80, 0xf3, 0xb0, 0x39, 0x1f, 0xb3, 0xe7, 0x0e, 0x80, 0x09, 0xa0,
    0xbd, 0x86, 0x44, 0x4d, 0x35, 0x97, 0xec, 0x6e, 0xff, 0x44, 0xc3, 0x13, 0x68, 0x94, 0x27, 0x33,
];
const FACADE_ACCESS_SCHEMA_SHA256_V2: Digest = [
    0xc4, 0x5b, 0x65, 0x8c, 0x43, 0x57, 0x13, 0x1b, 0x09, 0xcc, 0x75, 0x09, 0x33, 0x0a, 0x70, 0xf2,
    0x46, 0xa7, 0x1a, 0x2c, 0x37, 0x21, 0xb8, 0x9c, 0x34, 0x0c, 0xcf, 0x70, 0xad, 0x91, 0xd7, 0x0b,
];
pub(crate) const AUXILIARY_CODEC_SCHEMA_SHA256_V2: Digest = [
    0x60, 0x72, 0x8e, 0xf2, 0x00, 0x6e, 0x0b, 0x9c, 0x46, 0x7e, 0x4e, 0x7d, 0xd1, 0xb4, 0x38, 0xb9,
    0x13, 0x34, 0x48, 0xfd, 0x3d, 0x2b, 0x6b, 0xe6, 0x7d, 0x7e, 0xd4, 0x01, 0x93, 0x7e, 0x8a, 0xab,
];

const HANDOFF_MODULE: &str = "pyowl_core.backends.native_handoff_v2";
const MAX_FIXTURE_TABLES: usize = 100_000;
const MAX_FACADE_PAGE_ROWS_V2: u32 = 64;
const MAX_FACADE_PAGE_BYTES_V2: u64 = 8 * 1024 * 1024;

const COUNTER_NAMES: [&str; 89] = [
    "component_node_requests",
    "component_node_hits",
    "string_requests",
    "string_hits",
    "byte_string_requests",
    "byte_string_hits",
    "integer_requests",
    "integer_hits",
    "component_sequence_requests",
    "component_sequence_hits",
    "canonical_input_rows",
    "canonical_input_bytes",
    "unique_component_nodes",
    "unique_strings",
    "unique_byte_strings",
    "unique_integers",
    "unique_component_sequences",
    "retained_document_tables",
    "retained_annotation_rows",
    "retained_axiom_rows",
    "retained_extension_rows",
    "retained_source_map_rows",
    "retained_source_prefix_rows",
    "retained_origin_rows",
    "retained_rdf_header_rows",
    "retained_rdf_triple_rows",
    "retained_rdf_rule_rows",
    "retained_rdf_diagnostic_rows",
    "retained_owl2_dl_structural_issue_rows",
    "retained_owl2_dl_issue_rows",
    "retained_owl2_dl_role_property_rows",
    "retained_owl2_dl_role_hierarchy_rows",
    "retained_owl2_dl_role_composite_rows",
    "retained_owl2_dl_role_non_simple_rows",
    "retained_component_bytes",
    "retained_root_bytes",
    "retained_source_bytes",
    "retained_origin_bytes",
    "retained_rdf_bytes",
    "retained_owl2_dl_bytes",
    "retained_index_bytes",
    "retained_metadata_bytes",
    "retained_owner_bytes",
    "peak_builder_live_bytes",
    "peak_freeze_live_bytes",
    "peak_facade_cache_bytes",
    "publication_metadata_records_emitted",
    "publication_structural_rows_copied",
    "publication_structural_bytes_copied",
    "page_requests",
    "pages_returned",
    "rows_emitted",
    "payload_bytes_copied",
    "canonical_payload_bytes_copied",
    "auxiliary_payload_bytes_copied",
    "contains_requests",
    "contains_hits",
    "ontology_annotation_rows_emitted",
    "axiom_rows_emitted",
    "extension_rows_emitted",
    "signature_rows_emitted",
    "source_map_rows_emitted",
    "source_prefix_rows_emitted",
    "origin_rows_emitted",
    "rdf_header_rows_emitted",
    "rdf_triple_rows_emitted",
    "rdf_rule_rows_emitted",
    "rdf_diagnostic_rows_emitted",
    "owl2_dl_structural_issue_rows_emitted",
    "owl2_dl_issue_rows_emitted",
    "owl2_dl_role_property_rows_emitted",
    "owl2_dl_role_hierarchy_rows_emitted",
    "owl2_dl_role_composite_rows_emitted",
    "owl2_dl_role_non_simple_rows_emitted",
    "canonical_encode_requests",
    "canonical_encode_cache_hits",
    "facade_cache_hits",
    "facade_cache_misses",
    "facade_cache_evictions",
    "close_requests",
    "close_transitions",
    "fork_reinitializations",
    "facade_cache_current_entries",
    "facade_cache_current_bytes",
    "parser_bytes",
    "encoded_view_requests",
    "wire_encode_requests",
    "wire_decode_requests",
    "base_flatten_requests",
];

const RETAINED_DOCUMENT_TABLES: usize = 17;
const RETAINED_ROW_FIRST: usize = 18;
const RETAINED_COMPONENT_BYTES: usize = 34;
const RETAINED_ROOT_BYTES: usize = 35;
const RETAINED_SOURCE_BYTES: usize = 36;
const RETAINED_ORIGIN_BYTES: usize = 37;
const RETAINED_RDF_BYTES: usize = 38;
const RETAINED_OWL2_DL_BYTES: usize = 39;
const RETAINED_INDEX_BYTES: usize = 40;
const RETAINED_METADATA_BYTES: usize = 41;
const RETAINED_OWNER_BYTES: usize = 42;
const PEAK_BUILDER_BYTES: usize = 43;
const PEAK_FREEZE_BYTES: usize = 44;
const PEAK_FACADE_CACHE_BYTES: usize = 45;
const PUBLICATION_METADATA_RECORDS: usize = 46;
const PAGE_REQUESTS: usize = 49;
const PAGES_RETURNED: usize = 50;
const ROWS_EMITTED: usize = 51;
const PAYLOAD_BYTES_COPIED: usize = 52;
const CANONICAL_PAYLOAD_BYTES_COPIED: usize = 53;
const AUXILIARY_PAYLOAD_BYTES_COPIED: usize = 54;
const CONTAINS_REQUESTS: usize = 55;
const CONTAINS_HITS: usize = 56;
const EMITTED_ROW_FIRST: usize = 57;
const CLOSE_REQUESTS: usize = 79;
const CLOSE_TRANSITIONS: usize = 80;
const FORK_REINITIALIZATIONS: usize = 81;
const FACADE_CACHE_CURRENT_BYTES: usize = 83;
const PARSER_BYTES: usize = 84;
const ENCODED_VIEW_REQUESTS: usize = 85;

#[cfg(feature = "test-hooks")]
type SignatureEntitiesV2 = HashMap<Vec<u8>, (SignatureKindV2, bool)>;
#[cfg(feature = "test-hooks")]
type SignatureEntitiesByScopeV2 = HashMap<(ScopeV2, Option<u64>), SignatureEntitiesV2>;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum CollectionV2 {
    OntologyAnnotations,
    Axioms,
    Extensions,
    Signature,
    SourceMapEntries,
    SourceMapPrefixes,
    OriginEntries,
    RdfReportHeader,
    RdfUnconsumedTriples,
    RdfRuleIds,
    RdfDiagnostics,
    Owl2DlStructuralIssues,
    Owl2DlIssues,
    Owl2DlRoleProperties,
    Owl2DlRoleHierarchy,
    Owl2DlRoleComposite,
    Owl2DlRoleNonSimple,
}

impl CollectionV2 {
    fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        require_exact_handoff_type(value, "NativeFacadeCollectionV2")?;
        let selected: String = value.getattr("value")?.extract()?;
        match selected.as_str() {
            "ontology-annotations" => Ok(Self::OntologyAnnotations),
            "axioms" => Ok(Self::Axioms),
            "extensions" => Ok(Self::Extensions),
            "signature" => Ok(Self::Signature),
            "source-map-entries" => Ok(Self::SourceMapEntries),
            "source-map-prefixes" => Ok(Self::SourceMapPrefixes),
            "origin-entries" => Ok(Self::OriginEntries),
            "rdf-report-header" => Ok(Self::RdfReportHeader),
            "rdf-unconsumed-triples" => Ok(Self::RdfUnconsumedTriples),
            "rdf-rule-ids" => Ok(Self::RdfRuleIds),
            "rdf-diagnostics" => Ok(Self::RdfDiagnostics),
            "owl2-dl-structural-issues" => Ok(Self::Owl2DlStructuralIssues),
            "owl2-dl-issues" => Ok(Self::Owl2DlIssues),
            "owl2-dl-role-properties" => Ok(Self::Owl2DlRoleProperties),
            "owl2-dl-role-hierarchy" => Ok(Self::Owl2DlRoleHierarchy),
            "owl2-dl-role-composite" => Ok(Self::Owl2DlRoleComposite),
            "owl2-dl-role-non-simple" => Ok(Self::Owl2DlRoleNonSimple),
            _ => Err(PyValueError::new_err("unknown V2 facade collection")),
        }
    }

    const fn retained_row_counter(self) -> Option<usize> {
        match self {
            Self::OntologyAnnotations => Some(RETAINED_ROW_FIRST),
            Self::Axioms => Some(RETAINED_ROW_FIRST + 1),
            Self::Extensions => Some(RETAINED_ROW_FIRST + 2),
            Self::Signature => None,
            Self::SourceMapEntries => Some(RETAINED_ROW_FIRST + 3),
            Self::SourceMapPrefixes => Some(RETAINED_ROW_FIRST + 4),
            Self::OriginEntries => Some(RETAINED_ROW_FIRST + 5),
            Self::RdfReportHeader => Some(RETAINED_ROW_FIRST + 6),
            Self::RdfUnconsumedTriples => Some(RETAINED_ROW_FIRST + 7),
            Self::RdfRuleIds => Some(RETAINED_ROW_FIRST + 8),
            Self::RdfDiagnostics => Some(RETAINED_ROW_FIRST + 9),
            Self::Owl2DlStructuralIssues => Some(RETAINED_ROW_FIRST + 10),
            Self::Owl2DlIssues => Some(RETAINED_ROW_FIRST + 11),
            Self::Owl2DlRoleProperties => Some(RETAINED_ROW_FIRST + 12),
            Self::Owl2DlRoleHierarchy => Some(RETAINED_ROW_FIRST + 13),
            Self::Owl2DlRoleComposite => Some(RETAINED_ROW_FIRST + 14),
            Self::Owl2DlRoleNonSimple => Some(RETAINED_ROW_FIRST + 15),
        }
    }

    const fn emitted_row_counter(self) -> usize {
        EMITTED_ROW_FIRST + self as usize
    }

    const fn is_structural(self) -> bool {
        matches!(
            self,
            Self::OntologyAnnotations
                | Self::Axioms
                | Self::Extensions
                | Self::Signature
                | Self::Owl2DlRoleProperties
                | Self::Owl2DlRoleComposite
                | Self::Owl2DlRoleNonSimple
        )
    }

    const fn digest_filter_supported(self) -> bool {
        matches!(self, Self::SourceMapEntries | Self::OriginEntries)
    }

    const fn raw_for_document_owner(self) -> bool {
        matches!(
            self,
            Self::OntologyAnnotations | Self::Axioms | Self::Extensions | Self::OriginEntries
        )
    }

    const fn always_raw_document(self) -> bool {
        matches!(self, Self::SourceMapEntries | Self::SourceMapPrefixes)
    }

    const fn document_only(self) -> bool {
        matches!(
            self,
            Self::SourceMapEntries
                | Self::SourceMapPrefixes
                | Self::RdfReportHeader
                | Self::RdfUnconsumedTriples
                | Self::RdfRuleIds
                | Self::RdfDiagnostics
        )
    }

    const fn owl2_dl(self) -> bool {
        matches!(
            self,
            Self::Owl2DlStructuralIssues
                | Self::Owl2DlIssues
                | Self::Owl2DlRoleProperties
                | Self::Owl2DlRoleHierarchy
                | Self::Owl2DlRoleComposite
                | Self::Owl2DlRoleNonSimple
        )
    }

    const fn required_capability(self) -> Option<u64> {
        match self {
            Self::SourceMapEntries | Self::SourceMapPrefixes => Some(8),
            Self::OriginEntries => Some(16),
            Self::RdfReportHeader
            | Self::RdfUnconsumedTriples
            | Self::RdfRuleIds
            | Self::RdfDiagnostics => Some(32),
            _ => None,
        }
    }

    const fn memory_counter(self) -> usize {
        match self {
            Self::OntologyAnnotations | Self::Axioms | Self::Extensions => RETAINED_ROOT_BYTES,
            Self::Signature => RETAINED_INDEX_BYTES,
            Self::SourceMapEntries | Self::SourceMapPrefixes => RETAINED_SOURCE_BYTES,
            Self::OriginEntries => RETAINED_ORIGIN_BYTES,
            Self::RdfReportHeader
            | Self::RdfUnconsumedTriples
            | Self::RdfRuleIds
            | Self::RdfDiagnostics => RETAINED_RDF_BYTES,
            Self::Owl2DlStructuralIssues
            | Self::Owl2DlIssues
            | Self::Owl2DlRoleProperties
            | Self::Owl2DlRoleHierarchy
            | Self::Owl2DlRoleComposite
            | Self::Owl2DlRoleNonSimple => RETAINED_OWL2_DL_BYTES,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum ScopeV2 {
    Document,
    Closure,
}

impl ScopeV2 {
    fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        require_exact_handoff_type(value, "NativeFacadeScopeV2")?;
        match value.getattr("value")?.extract::<String>()?.as_str() {
            "document" => Ok(Self::Document),
            "closure" => Ok(Self::Closure),
            _ => Err(PyValueError::new_err("unknown V2 facade scope")),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum SignatureKindV2 {
    All,
    Class,
    Datatype,
    ObjectProperty,
    DataProperty,
    AnnotationProperty,
    NamedIndividual,
}

impl SignatureKindV2 {
    const ALL_VALUES: [Self; 7] = [
        Self::All,
        Self::Class,
        Self::Datatype,
        Self::ObjectProperty,
        Self::DataProperty,
        Self::AnnotationProperty,
        Self::NamedIndividual,
    ];

    fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        require_exact_handoff_type(value, "NativeSignatureKindV2")?;
        Self::from_value(&value.getattr("value")?.extract::<String>()?)
    }

    fn from_value(value: &str) -> PyResult<Self> {
        match value {
            "all" => Ok(Self::All),
            "class" => Ok(Self::Class),
            "datatype" => Ok(Self::Datatype),
            "object_property" => Ok(Self::ObjectProperty),
            "data_property" => Ok(Self::DataProperty),
            "annotation_property" => Ok(Self::AnnotationProperty),
            "named_individual" => Ok(Self::NamedIndividual),
            _ => Err(PyValueError::new_err("unknown V2 signature kind")),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
struct CoordinateV2 {
    collection: CollectionV2,
    scope: ScopeV2,
    document_ordinal: Option<u64>,
    signature_kind: SignatureKindV2,
    include_builtins: bool,
}

impl CoordinateV2 {
    fn typed(self) -> Option<TypedFacadeCoordinateV2> {
        let collection = match self.collection {
            CollectionV2::OntologyAnnotations => TypedFacadeCollectionV2::OntologyAnnotations,
            CollectionV2::Axioms => TypedFacadeCollectionV2::Axioms,
            CollectionV2::Extensions => TypedFacadeCollectionV2::Extensions,
            CollectionV2::Signature => TypedFacadeCollectionV2::Signature,
            _ => return None,
        };
        let scope = match self.scope {
            ScopeV2::Document => TypedFacadeScopeV2::Document,
            ScopeV2::Closure => TypedFacadeScopeV2::Closure,
        };
        let signature_kind = match self.signature_kind {
            SignatureKindV2::All => TypedFacadeSignatureKindV2::All,
            SignatureKindV2::Class => TypedFacadeSignatureKindV2::Class,
            SignatureKindV2::Datatype => TypedFacadeSignatureKindV2::Datatype,
            SignatureKindV2::ObjectProperty => TypedFacadeSignatureKindV2::ObjectProperty,
            SignatureKindV2::DataProperty => TypedFacadeSignatureKindV2::DataProperty,
            SignatureKindV2::AnnotationProperty => TypedFacadeSignatureKindV2::AnnotationProperty,
            SignatureKindV2::NamedIndividual => TypedFacadeSignatureKindV2::NamedIndividual,
        };
        Some(TypedFacadeCoordinateV2 {
            collection,
            scope,
            document_ordinal: self.document_ordinal,
            signature_kind,
            include_builtins: self.include_builtins,
        })
    }
}

#[derive(Clone, Debug)]
struct FacadeTableV2 {
    coordinate: CoordinateV2,
    rows: Vec<Arc<Vec<u8>>>,
    source_identity: usize,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct SharedRowV2(Arc<Vec<u8>>);

impl Borrow<[u8]> for SharedRowV2 {
    fn borrow(&self) -> &[u8] {
        self.0.as_slice()
    }
}

#[derive(Clone, Debug)]
struct Owl2DlSummaryV2 {
    structural_values_checked: u64,
    structural_complete: bool,
    report_complete: bool,
    structural_issue_count: u64,
    issue_count: u64,
    role_property_count: u64,
    role_hierarchy_count: u64,
    role_composite_count: u64,
    role_non_simple_count: u64,
}

#[derive(Clone, Debug)]
pub(super) struct NativeSnapshotAttestationV2 {
    version: u32,
    ledger_sha256: Digest,
    metadata_manifest_sha256: Digest,
    facade_access_schema_sha256: Digest,
    auxiliary_codec_schema_sha256: Digest,
    root_table_sha256: Digest,
    effective_root_table_sha256: Digest,
    fingerprint_inputs_sha256: Digest,
    source_manifest_sha256: Digest,
    provenance_manifest_sha256: Digest,
    effective_origin_manifest_sha256: Digest,
    diagnostics_manifest_sha256: Digest,
    diagnostic_reference_kinds_sha256: Digest,
    facade_cardinality_summary_sha256: Digest,
    load_options_sha256: Digest,
    report_sha256: Digest,
    max_facade_row_bytes: u64,
    document_count: u64,
    import_edge_count: u64,
    diagnostic_count: u64,
    ontology_annotation_count: u64,
    stored_axiom_count: u64,
    effective_axiom_count: u64,
    extension_count: u64,
    total_source_bytes: u64,
    source_map_entry_count: u64,
    origin_entry_count: u64,
    rdf_mapping_report_count: u64,
    capability_bits: u64,
    api_version: (u32, u32),
    model_schema: u32,
    backend: Box<str>,
    root_document_key: Box<str>,
    owl2_dl_report_summary: Option<Owl2DlSummaryV2>,
    owl2_dl_validated: bool,
    owl2_dl_conforms: Option<bool>,
    owl2_dl_report_sha256: Option<Digest>,
}

#[derive(Debug)]
struct AllocationBudget {
    retained: u64,
    temporary: u64,
    peak: u64,
    maximum: u64,
}

#[cfg(feature = "test-hooks")]
#[derive(Debug, Default)]
struct TemporaryBudget {
    used: u64,
}

#[cfg(feature = "test-hooks")]
impl TemporaryBudget {
    fn claim(&mut self, retained: &mut AllocationBudget, amount: usize) -> PyResult<()> {
        let amount = u64::try_from(amount)
            .map_err(|_| PyMemoryError::new_err("native V2 temporary work exceeds u64"))?;
        retained
            .claim_temporary_u64(amount)
            .map_err(native_error_to_python)?;
        self.used = self
            .used
            .checked_add(amount)
            .ok_or_else(|| PyMemoryError::new_err("native V2 temporary work overflow"))?;
        Ok(())
    }

    fn release(self, retained: &mut AllocationBudget) -> PyResult<()> {
        retained
            .release_temporary(self.used)
            .map_err(native_error_to_python)
    }
}

impl AllocationBudget {
    fn new(maximum: u64) -> NativeResult<Self> {
        if maximum == 0 {
            return Err(NativeError::limit(
                "native V2 retained allocation budget must be positive",
            ));
        }
        Ok(Self {
            retained: 0,
            temporary: 0,
            peak: 0,
            maximum,
        })
    }

    fn claim(&mut self, amount: usize) -> NativeResult<()> {
        let amount = u64::try_from(amount)
            .map_err(|_| NativeError::limit("native V2 retained allocation exceeds u64"))?;
        let following_retained = self
            .retained
            .checked_add(amount)
            .ok_or_else(|| NativeError::limit("native V2 retained allocation overflow"))?;
        let following_live = following_retained
            .checked_add(self.temporary)
            .ok_or_else(|| NativeError::limit("native V2 live allocation overflow"))?;
        if following_live > self.maximum {
            return Err(NativeError::limit(
                "native V2 retained allocation exceeds its explicit budget",
            ));
        }
        self.retained = following_retained;
        self.peak = self.peak.max(following_live);
        Ok(())
    }

    fn claim_temporary(&mut self, amount: usize) -> NativeResult<()> {
        let amount = u64::try_from(amount)
            .map_err(|_| NativeError::limit("native V2 temporary allocation exceeds u64"))?;
        self.claim_temporary_u64(amount)
    }

    fn claim_temporary_u64(&mut self, amount: u64) -> NativeResult<()> {
        let following_temporary = self
            .temporary
            .checked_add(amount)
            .ok_or_else(|| NativeError::limit("native V2 temporary allocation overflow"))?;
        let following_live = self
            .retained
            .checked_add(following_temporary)
            .ok_or_else(|| NativeError::limit("native V2 live allocation overflow"))?;
        if following_live > self.maximum {
            return Err(NativeError::limit(
                "native V2 temporary allocation exceeds its explicit budget",
            ));
        }
        self.temporary = following_temporary;
        self.peak = self.peak.max(following_live);
        Ok(())
    }

    fn release_temporary(&mut self, amount: u64) -> NativeResult<()> {
        self.temporary = self.temporary.checked_sub(amount).ok_or_else(|| {
            NativeError::protocol("native V2 temporary allocation accounting underflow")
        })?;
        Ok(())
    }

    const fn retained(&self) -> u64 {
        self.retained
    }
}

fn arc_sized_allocation_bytes<T>() -> NativeResult<usize> {
    Layout::new::<[AtomicUsize; 2]>()
        .extend(Layout::new::<T>())
        .map(|(layout, _offset)| layout.pad_to_align().size())
        .map_err(|_| NativeError::limit("native V2 owner allocation layout overflow"))
}

fn arc_vec_allocation_bytes(capacity: usize) -> NativeResult<usize> {
    arc_sized_allocation_bytes::<Vec<u8>>()?
        .checked_add(capacity)
        .ok_or_else(|| NativeError::limit("native V2 row allocation layout overflow"))
}

#[derive(Debug)]
struct CounterStateV2 {
    pid: AtomicU32,
    gate: AtomicBool,
    values: [AtomicU64; COUNTER_NAMES.len()],
}

struct CounterGuard<'a>(&'a AtomicBool);

impl Drop for CounterGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

impl CounterStateV2 {
    fn new(initial: [u64; COUNTER_NAMES.len()]) -> Self {
        Self {
            pid: AtomicU32::new(std::process::id()),
            gate: AtomicBool::new(false),
            values: std::array::from_fn(|index| AtomicU64::new(initial[index])),
        }
    }

    fn prepare_process(&self) {
        self.prepare_process_id(std::process::id());
    }

    fn prepare_process_id(&self, current: u32) {
        loop {
            let observed = self.pid.load(Ordering::Acquire);
            if observed == current {
                return;
            }
            if observed == 0 {
                std::hint::spin_loop();
                continue;
            }
            if self
                .pid
                .compare_exchange(observed, 0, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
            {
                continue;
            }
            // A child may inherit the gate while another parent thread held
            // it.  No such thread survives in the child, so clearing it is
            // the required process-local lock reset.
            self.gate.store(false, Ordering::Release);
            let _guard = self.lock();
            self.values[PEAK_FACADE_CACHE_BYTES].store(0, Ordering::Relaxed);
            for index in PAGE_REQUESTS..COUNTER_NAMES.len() {
                self.values[index].store(0, Ordering::Relaxed);
            }
            self.values[FORK_REINITIALIZATIONS].store(1, Ordering::Relaxed);
            self.pid.store(current, Ordering::Release);
            return;
        }
    }

    fn lock(&self) -> CounterGuard<'_> {
        while self
            .gate
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            std::hint::spin_loop();
            std::thread::yield_now();
        }
        CounterGuard(&self.gate)
    }

    fn snapshot(&self) -> [u64; COUNTER_NAMES.len()] {
        self.prepare_process();
        let _guard = self.lock();
        std::array::from_fn(|index| self.values[index].load(Ordering::Relaxed))
    }

    #[cfg(test)]
    fn snapshot_for_process(&self, pid: u32) -> [u64; COUNTER_NAMES.len()] {
        self.prepare_process_id(pid);
        let _guard = self.lock();
        std::array::from_fn(|index| self.values[index].load(Ordering::Relaxed))
    }

    fn add_pairs(&self, pairs: &[(usize, u64)]) -> NativeResult<()> {
        self.prepare_process();
        let _guard = self.lock();
        for (index, amount) in pairs {
            self.values[*index]
                .load(Ordering::Relaxed)
                .checked_add(*amount)
                .ok_or_else(|| NativeError::limit("native V2 facade counter overflow"))?;
        }
        for (index, amount) in pairs {
            self.values[*index].fetch_add(*amount, Ordering::Relaxed);
        }
        Ok(())
    }

    fn page(&self, collection: CollectionV2, rows: u64, bytes: u64) -> NativeResult<()> {
        let byte_counter = if collection.is_structural() {
            CANONICAL_PAYLOAD_BYTES_COPIED
        } else {
            AUXILIARY_PAYLOAD_BYTES_COPIED
        };
        self.add_pairs(&[
            (PAGE_REQUESTS, 1),
            (PAGES_RETURNED, 1),
            (ROWS_EMITTED, rows),
            (PAYLOAD_BYTES_COPIED, bytes),
            (byte_counter, bytes),
            (collection.emitted_row_counter(), rows),
        ])
    }

    fn contains(&self, found: bool) -> NativeResult<()> {
        self.add_pairs(&[(CONTAINS_REQUESTS, 1), (CONTAINS_HITS, u64::from(found))])
    }

    fn close(&self, transitioned: bool) -> NativeResult<()> {
        self.add_pairs(&[
            (CLOSE_REQUESTS, 1),
            (CLOSE_TRANSITIONS, u64::from(transitioned)),
        ])
    }
}

#[derive(Debug)]
pub(crate) struct PublicationStorageV2 {
    attestation: NativeSnapshotAttestationV2,
    effective_tables: Vec<FacadeTableV2>,
    raw_document_tables: Option<Vec<FacadeTableV2>>,
    typed_structural: Option<Arc<TypedFacadeStorageV2>>,
    counters: CounterStateV2,
}

impl PublicationStorageV2 {
    pub(super) const fn attestation(&self) -> &NativeSnapshotAttestationV2 {
        &self.attestation
    }

    pub(super) const fn document_count(&self) -> u64 {
        self.attestation.document_count
    }

    pub(super) fn bump_close(&self, transitioned: bool) -> PyResult<()> {
        self.counters
            .close(transitioned)
            .map_err(native_error_to_python)
    }

    pub(super) fn attestation_to_python(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.attestation.to_python(py)
    }

    pub(super) fn page_to_python(
        &self,
        py: Python<'_>,
        request: &Bound<'_, PyAny>,
        raw_document_owner: bool,
        fixed_document_ordinal: Option<u64>,
    ) -> PyResult<Py<PyAny>> {
        let handoff = py.import(HANDOFF_MODULE)?;
        require_exact_type(&handoff, "NativeFacadePageRequestV2", request)?;
        let selected = PageRequestV2::from_python(request)?;
        self.validate_request(
            selected.coordinate,
            selected.max_row_bytes,
            fixed_document_ordinal,
        )?;
        if selected.digest_filter.is_some()
            && !selected.coordinate.collection.digest_filter_supported()
        {
            return Err(PyValueError::new_err(
                "V2 digest filters are supported only for source-map and origin rows",
            ));
        }
        if let (Some(storage), Some(coordinate)) = (
            self.typed_structural.as_deref(),
            selected.coordinate.typed(),
        ) {
            let page = storage
                .page(
                    TypedFacadePageRequestV2::new(
                        coordinate,
                        raw_document_owner,
                        selected.start,
                        selected.max_rows,
                        selected.max_bytes,
                    ),
                    Cancellation::with_duration(None),
                    None,
                )
                .map_err(native_error_to_python)?;
            let emitted_count = u64::try_from(page.rows.len())
                .map_err(|_| PyValueError::new_err("V2 page row count exceeds u64"))?;
            let terminal = page.next_cursor.is_none();
            let py_rows = PyTuple::new(
                py,
                page.rows.iter().map(|row| PyBytes::new(py, row.as_slice())),
            )?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("total_count", page.total_count)?;
            kwargs.set_item("next_cursor", page.next_cursor)?;
            kwargs.set_item("terminal", terminal)?;
            kwargs.set_item("rows", py_rows)?;
            let result = handoff
                .getattr("_unchecked_owner_page_v2")?
                .call((request,), Some(&kwargs))?;
            self.counters
                .page(
                    selected.coordinate.collection,
                    emitted_count,
                    page.page_bytes,
                )
                .map_err(native_error_to_python)?;
            return Ok(result.unbind());
        }
        let rows = self.rows(selected.coordinate, raw_document_owner);
        let (lower, upper) = digest_range(rows, selected.digest_filter.as_ref());
        let total = upper - lower;
        let start = usize::try_from(selected.start)
            .map_err(|_| PyValueError::new_err("V2 page start exceeds usize"))?;
        if start > total {
            return Err(PyValueError::new_err(
                "V2 page start exceeds the selected collection total",
            ));
        }
        let absolute_start = lower
            .checked_add(start)
            .ok_or_else(|| PyValueError::new_err("V2 page start overflow"))?;
        let requested_stop =
            absolute_start.saturating_add(usize::try_from(selected.max_rows).unwrap_or(usize::MAX));
        let absolute_stop = upper.min(requested_stop);
        let (emitted_end, page_bytes) =
            bounded_page_end(rows, absolute_start, absolute_stop, selected.max_bytes)
                .map_err(native_error_to_python)?;
        let mut emitted = Vec::new();
        emitted
            .try_reserve_exact(emitted_end.saturating_sub(absolute_start))
            .map_err(|_| PyMemoryError::new_err("native V2 page allocation failed"))?;
        emitted.extend(rows[absolute_start..emitted_end].iter().cloned());
        let emitted_count = u64::try_from(emitted.len())
            .map_err(|_| PyValueError::new_err("V2 page row count exceeds u64"))?;
        let end = selected
            .start
            .checked_add(emitted_count)
            .ok_or_else(|| PyValueError::new_err("V2 page cursor overflow"))?;
        let total_u64 =
            u64::try_from(total).map_err(|_| PyValueError::new_err("V2 page total exceeds u64"))?;
        let terminal = end == total_u64;
        let py_rows = PyTuple::new(
            py,
            emitted.iter().map(|row| PyBytes::new(py, row.as_slice())),
        )?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("total_count", total_u64)?;
        kwargs.set_item("next_cursor", if terminal { None } else { Some(end) })?;
        kwargs.set_item("terminal", terminal)?;
        kwargs.set_item("rows", py_rows)?;
        let page = handoff
            .getattr("_unchecked_owner_page_v2")?
            .call((request,), Some(&kwargs))?;
        self.counters
            .page(selected.coordinate.collection, emitted_count, page_bytes)
            .map_err(native_error_to_python)?;
        Ok(page.unbind())
    }

    pub(super) fn contains(
        &self,
        py: Python<'_>,
        request: &Bound<'_, PyAny>,
        raw_document_owner: bool,
        fixed_document_ordinal: Option<u64>,
    ) -> PyResult<bool> {
        let handoff = py.import(HANDOFF_MODULE)?;
        require_exact_type(&handoff, "NativeFacadeContainsRequestV2", request)?;
        let selected =
            ContainsRequestV2::from_python(request, self.attestation.max_facade_row_bytes)?;
        self.validate_request(
            selected.coordinate,
            selected.max_row_bytes,
            fixed_document_ordinal,
        )?;
        if selected.coordinate.collection != CollectionV2::Axioms {
            return Err(PyValueError::new_err("V2 contains is axioms-only"));
        }
        if let (Some(storage), Some(coordinate)) = (
            self.typed_structural.as_deref(),
            selected.coordinate.typed(),
        ) {
            let found = storage
                .contains_axiom(
                    coordinate,
                    raw_document_owner,
                    &selected.canonical,
                    Cancellation::with_duration(None),
                    None,
                )
                .map_err(native_error_to_python)?;
            self.counters
                .contains(found)
                .map_err(native_error_to_python)?;
            return Ok(found);
        }
        let rows = self.rows(selected.coordinate, raw_document_owner);
        let found = rows
            .binary_search_by(|row| row.as_slice().cmp(selected.canonical.as_slice()))
            .is_ok();
        self.counters
            .contains(found)
            .map_err(native_error_to_python)?;
        Ok(found)
    }

    pub(super) fn counters_to_python(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let values = self.counters.snapshot();
        let kwargs = PyDict::new(py);
        for (name, value) in COUNTER_NAMES.iter().zip(values) {
            kwargs.set_item(*name, value)?;
        }
        let handoff = py.import(HANDOFF_MODULE)?;
        Ok(handoff
            .getattr("NativeFacadeCountersV2")?
            .call((), Some(&kwargs))?
            .unbind())
    }

    pub(crate) fn encoded_structural_columns(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<EncodedStructuralColumnsV1> {
        let columns = self
            .prepare_encoded_structural_columns(
                scope,
                document_ordinal,
                raw_document_owner,
                limits,
                cancellation,
                interrupt,
            )?
            .into_columns()?;
        self.record_encoded_view_success()?;
        Ok(columns)
    }

    pub(crate) fn prepare_encoded_structural_columns(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<PreparedEncodedStructuralColumnsV1<'_>> {
        let typed = self.typed_structural.as_deref().ok_or_else(|| {
            NativeError::protocol("native V2 publication has no typed structural owner")
        })?;
        typed.prepare_encoded_structural_columns(
            scope,
            document_ordinal,
            raw_document_owner,
            limits,
            cancellation,
            interrupt,
        )
    }

    pub(crate) fn record_encoded_view_success(&self) -> NativeResult<()> {
        self.counters.add_pairs(&[(ENCODED_VIEW_REQUESTS, 1)])?;
        Ok(())
    }

    pub(crate) fn retained_axiom_type_index(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<RetainedAxiomTypeIndexV1> {
        let typed = self.typed_structural.as_deref().ok_or_else(|| {
            NativeError::protocol("native V2 publication has no typed structural owner")
        })?;
        typed.axiom_type_index(
            scope,
            document_ordinal,
            raw_document_owner,
            limits,
            cancellation,
            interrupt,
        )
    }

    /// Attach the production typed structural owner without retaining a
    /// second canonical row store. The origin-aware variant adds one shared
    /// auxiliary row table without changing the typed arena.
    #[allow(dead_code)]
    pub(super) fn from_typed_structural(
        attestation: NativeSnapshotAttestationV2,
        typed_structural: TypedFacadeStorageV2,
    ) -> NativeResult<Arc<Self>> {
        Self::from_typed_structural_parts(attestation, typed_structural, None, 0)
    }

    pub(super) fn from_typed_structural_with_origins(
        attestation: NativeSnapshotAttestationV2,
        typed_structural: TypedFacadeStorageV2,
        origin_rows: Vec<Vec<u8>>,
        parser_bytes: u64,
    ) -> NativeResult<Arc<Self>> {
        Self::from_typed_structural_with_optional_origins(
            attestation,
            typed_structural,
            Some(origin_rows),
            parser_bytes,
        )
    }

    pub(super) fn from_typed_structural_with_optional_origins(
        attestation: NativeSnapshotAttestationV2,
        typed_structural: TypedFacadeStorageV2,
        origin_rows: Option<Vec<Vec<u8>>>,
        parser_bytes: u64,
    ) -> NativeResult<Arc<Self>> {
        Self::from_typed_structural_parts(attestation, typed_structural, origin_rows, parser_bytes)
    }

    fn from_typed_structural_parts(
        attestation: NativeSnapshotAttestationV2,
        typed_structural: TypedFacadeStorageV2,
        origin_rows: Option<Vec<Vec<u8>>>,
        parser_bytes: u64,
    ) -> NativeResult<Arc<Self>> {
        if attestation.version != PUBLICATION_VERSION_V2
            || attestation.ledger_sha256 != PUBLICATION_LEDGER_SHA256_V2
            || attestation.facade_access_schema_sha256 != FACADE_ACCESS_SCHEMA_SHA256_V2
            || attestation.auxiliary_codec_schema_sha256 != AUXILIARY_CODEC_SCHEMA_SHA256_V2
            || attestation.model_schema != 1
        {
            return Err(NativeError::protocol(
                "typed V2 publication attestation schema differs",
            ));
        }
        let retains_origins = origin_rows.is_some();
        let origins = origin_rows.unwrap_or_default();
        let origin_count = u64::try_from(origins.len())
            .map_err(|_| NativeError::limit("typed V2 origin count exceeds u64"))?;
        let origin_maximum = origins.iter().try_fold(1_u64, |maximum, row| {
            if row.len() < 32 {
                return Err(NativeError::protocol(
                    "typed V2 retained origin row is truncated",
                ));
            }
            Ok(maximum.max(
                u64::try_from(row.len())
                    .map_err(|_| NativeError::limit("typed V2 origin row exceeds u64"))?,
            ))
        })?;
        let structural_counts = typed_structural.structural_counts()?;
        if attestation.document_count != typed_structural.document_count()
            || attestation.max_facade_row_bytes
                != typed_structural.maximum_row_bytes().max(origin_maximum)
            || attestation.ontology_annotation_count != structural_counts.ontology_annotations
            || attestation.stored_axiom_count != structural_counts.stored_axioms
            || attestation.effective_axiom_count != structural_counts.effective_axioms
            || attestation.extension_count != structural_counts.extensions
        {
            return Err(NativeError::protocol(
                "typed V2 structural owner diverges from its attestation",
            ));
        }
        let expected_capability_bits = if retains_origins { 7 | 16 } else { 7 };
        if attestation.capability_bits != expected_capability_bits
            || attestation.source_map_entry_count != 0
            || attestation.origin_entry_count != origin_count
            || attestation.rdf_mapping_report_count != 0
            || attestation.owl2_dl_report_summary.is_some()
            || attestation.owl2_dl_validated
            || attestation.owl2_dl_conforms.is_some()
            || attestation.owl2_dl_report_sha256.is_some()
        {
            return Err(NativeError::protocol(
                "typed V2 owner attests unsupported auxiliary collections",
            ));
        }
        if retains_origins && attestation.document_count != 1 {
            return Err(NativeError::protocol(
                "typed V2 origin attachment currently requires one document",
            ));
        }
        if parser_bytes != 0 && parser_bytes != attestation.total_source_bytes {
            return Err(NativeError::protocol(
                "typed V2 parser byte count diverges from its attestation",
            ));
        }
        let typed_structural = Arc::new(typed_structural);
        let mut initial = typed_initial_counters(&typed_structural, &attestation)?;
        initial[PARSER_BYTES] = parser_bytes;
        let retained_origins = retain_origin_tables_v2(origins)?;
        if retains_origins {
            initial[RETAINED_ROW_FIRST + 5] = origin_count
                .checked_mul(2)
                .ok_or_else(|| NativeError::limit("typed V2 origin row count overflow"))?;
            initial[RETAINED_ORIGIN_BYTES] = retained_origins.payload_bytes;
            initial[RETAINED_METADATA_BYTES] = checked_add(
                initial[RETAINED_METADATA_BYTES],
                retained_origins.metadata_bytes,
            )?;
            let retained_delta = checked_add(
                retained_origins.payload_bytes,
                retained_origins.metadata_bytes,
            )?;
            initial[RETAINED_OWNER_BYTES] =
                checked_add(initial[RETAINED_OWNER_BYTES], retained_delta)?;
            initial[PEAK_BUILDER_BYTES] =
                initial[PEAK_BUILDER_BYTES].max(initial[RETAINED_OWNER_BYTES]);
            initial[PEAK_FREEZE_BYTES] =
                initial[PEAK_FREEZE_BYTES].max(initial[RETAINED_OWNER_BYTES]);
            if typed_structural
                .max_memory_bytes()
                .is_some_and(|maximum| initial[RETAINED_OWNER_BYTES] > maximum)
            {
                return Err(NativeError::limit(
                    "typed V2 origin attachment exceeds max_memory_bytes",
                ));
            }
            validate_retained_total(&initial)?;
        }
        Ok(Arc::new(Self {
            attestation,
            effective_tables: retained_origins.effective_tables,
            raw_document_tables: None,
            typed_structural: Some(typed_structural),
            counters: CounterStateV2::new(initial),
        }))
    }

    #[cfg(feature = "test-hooks")]
    pub(crate) fn encoded_fixture_for_tests() -> NativeResult<Arc<Self>> {
        use crate::canonical::{entity, iri, Field, Node};

        let limits = Limits::default();
        let declaration = Node::build(
            60,
            vec![
                Field::Node(entity("class", iri("urn:encoded-view:fixture".into())?)?),
                Field::Set(Vec::new()),
            ],
        )?;
        let mut builder =
            super::TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)?;
        builder.add_document(&[], &[declaration.as_bytes().to_vec()], &[])?;
        let typed = builder.freeze(&[vec![0]], &[0])?;
        let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        attestation.max_facade_row_bytes = typed.maximum_row_bytes();
        Self::from_typed_structural(attestation, typed)
    }

    #[cfg(test)]
    pub(super) fn fixture_for_tests() -> Arc<Self> {
        Self::fixture_for_tests_with_document_count(1)
    }

    #[cfg(test)]
    pub(super) fn fixture_for_tests_with_document_count(document_count: u64) -> Arc<Self> {
        let coordinate = CoordinateV2 {
            collection: CollectionV2::Axioms,
            scope: ScopeV2::Document,
            document_ordinal: Some(0),
            signature_kind: SignatureKindV2::All,
            include_builtins: true,
        };
        let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        attestation.document_count = document_count;
        Arc::new(Self {
            attestation,
            effective_tables: vec![FacadeTableV2 {
                coordinate,
                rows: vec![Arc::new(b"axiom".to_vec())],
                source_identity: 1,
            }],
            raw_document_tables: None,
            typed_structural: None,
            counters: CounterStateV2::new([0; COUNTER_NAMES.len()]),
        })
    }

    fn validate_request(
        &self,
        coordinate: CoordinateV2,
        max_row_bytes: u64,
        fixed_document_ordinal: Option<u64>,
    ) -> PyResult<()> {
        if max_row_bytes != self.attestation.max_facade_row_bytes {
            return Err(PyValueError::new_err(
                "V2 request row bound does not match the publication",
            ));
        }
        match (coordinate.scope, coordinate.document_ordinal) {
            (ScopeV2::Document, None) => {
                return Err(PyValueError::new_err(
                    "V2 document-scope request requires a document ordinal",
                ));
            }
            (ScopeV2::Closure, Some(_)) => {
                return Err(PyValueError::new_err(
                    "V2 closure request requires document_ordinal=None",
                ));
            }
            _ => {}
        }
        if coordinate
            .document_ordinal
            .is_some_and(|ordinal| ordinal >= self.attestation.document_count)
        {
            return Err(PyValueError::new_err(
                "V2 request document ordinal is out of bounds",
            ));
        }
        if fixed_document_ordinal.is_some_and(|ordinal| {
            coordinate.scope != ScopeV2::Document || coordinate.document_ordinal != Some(ordinal)
        }) {
            return Err(PyValueError::new_err(
                "V2 document owner request escaped its fixed ordinal",
            ));
        }
        if coordinate.collection.document_only() && coordinate.scope != ScopeV2::Document {
            return Err(PyValueError::new_err(
                "V2 collection supports document scope only",
            ));
        }
        if coordinate.collection.owl2_dl() && coordinate.scope != ScopeV2::Closure {
            return Err(PyValueError::new_err(
                "V2 OWL2 DL collection supports closure scope only",
            ));
        }
        if coordinate.collection != CollectionV2::Signature
            && (coordinate.signature_kind != SignatureKindV2::All || !coordinate.include_builtins)
        {
            return Err(PyValueError::new_err(
                "V2 non-signature request requires kind=all and builtins included",
            ));
        }
        if coordinate.collection.owl2_dl() && self.attestation.owl2_dl_report_summary.is_none() {
            return Err(PyValueError::new_err(
                "V2 OWL2 DL collection has no retained validated report",
            ));
        }
        if coordinate
            .collection
            .required_capability()
            .is_some_and(|required| self.attestation.capability_bits & required == 0)
        {
            return Err(PyValueError::new_err(
                "V2 collection is absent from the retained publication capabilities",
            ));
        }
        Ok(())
    }

    fn rows(&self, coordinate: CoordinateV2, raw_document_owner: bool) -> &[Arc<Vec<u8>>] {
        let use_raw = coordinate.scope == ScopeV2::Document
            && (coordinate.collection.always_raw_document()
                || (raw_document_owner && coordinate.collection.raw_for_document_owner()));
        if use_raw {
            if let Some(rows) = self.raw_document_tables.as_deref().and_then(|tables| {
                tables
                    .binary_search_by_key(&coordinate, |table| table.coordinate)
                    .ok()
                    .map(|index| tables[index].rows.as_ref())
            }) {
                return rows;
            }
        }
        self.effective_tables
            .binary_search_by_key(&coordinate, |table| table.coordinate)
            .ok()
            .map_or(&[], |index| self.effective_tables[index].rows.as_ref())
    }

    #[cfg(feature = "test-hooks")]
    #[allow(clippy::too_many_arguments)]
    pub(super) fn from_validated_python(
        py: Python<'_>,
        attestation: &Bound<'_, PyAny>,
        collections: &Bound<'_, PyAny>,
        documents: &Bound<'_, PyAny>,
        report: &Bound<'_, PyAny>,
        root_document_key: &Bound<'_, PyAny>,
        load_options: &Bound<'_, PyAny>,
        capability_bits: &Bound<'_, PyAny>,
        fingerprint_evidence: &Bound<'_, PyAny>,
        fingerprint_preimages: &Bound<'_, PyAny>,
        facade_cardinality_summary: &Bound<'_, PyAny>,
        owl2_dl_report_summary: Option<&Bound<'_, PyAny>>,
        raw_document_collections: Option<&Bound<'_, PyAny>>,
        max_retained_bytes: u64,
    ) -> PyResult<Arc<Self>> {
        let handoff = py.import(HANDOFF_MODULE)?;
        require_exact_type(&handoff, "NativeSnapshotAttestationV2", attestation)?;
        let validated_attestation = reconstruct_attestation(&handoff, attestation)?;
        let attestation_value = NativeSnapshotAttestationV2::from_python(&validated_attestation)?;
        handoff
            .getattr("_validate_exact_load_options_v2")?
            .call1((load_options,))?;
        let limits = load_options.getattr("limits")?;
        let mut builder =
            StorageBuilderV2::new(max_retained_bytes).map_err(native_error_to_python)?;
        let (effective, effective_largest) = validate_fixture_mapping(
            &handoff,
            &validated_attestation,
            collections,
            &limits,
            false,
        )?;
        builder
            .add_python_tables(&effective, false, attestation_value.document_count)
            .map_err(native_error_to_python)?;
        let derived_largest = derive_and_add_signature_tables(
            py,
            &effective,
            &limits,
            attestation_value.document_count,
            &mut builder,
        )?;
        let (raw_binding, raw_largest) = if let Some(raw) = raw_document_collections {
            let (selected, largest) =
                validate_fixture_mapping(&handoff, &validated_attestation, raw, &limits, true)?;
            builder
                .add_python_tables(&selected, true, attestation_value.document_count)
                .map_err(native_error_to_python)?;
            builder.raw_supplied = true;
            (selected, largest)
        } else {
            validate_fixture_mapping(&handoff, &validated_attestation, collections, &limits, true)?
        };
        if effective_largest
            .max(raw_largest)
            .max(derived_largest)
            .max(1)
            != attestation_value.max_facade_row_bytes
        {
            return Err(PyValueError::new_err(
                "native V2 retained rows do not match the attested maximum row size",
            ));
        }
        validate_content_binding(
            &handoff,
            &validated_attestation,
            &effective,
            &raw_binding,
            documents,
            report,
            root_document_key,
            load_options,
            capability_bits,
            fingerprint_evidence,
            fingerprint_preimages,
            facade_cardinality_summary,
            owl2_dl_report_summary,
        )?;
        builder
            .finish(attestation_value)
            .map_err(native_error_to_python)
    }
}

#[derive(Debug, Default)]
struct RetainedOriginTablesV2 {
    effective_tables: Vec<FacadeTableV2>,
    payload_bytes: u64,
    metadata_bytes: u64,
}

fn retain_origin_tables_v2(rows: Vec<Vec<u8>>) -> NativeResult<RetainedOriginTablesV2> {
    if rows.is_empty() {
        return Ok(RetainedOriginTablesV2::default());
    }
    if rows
        .windows(2)
        .any(|pair| pair[0].get(..32) > pair[1].get(..32))
    {
        return Err(NativeError::protocol(
            "typed V2 retained origin digest groups are not ordered",
        ));
    }

    let mut document_rows = Vec::new();
    document_rows
        .try_reserve_exact(rows.len())
        .map_err(|_| NativeError::limit("typed V2 origin row-reference allocation failed"))?;
    let mut payload_bytes = 0_u64;
    let mut metadata_bytes = u64::try_from(
        document_rows
            .capacity()
            .checked_mul(size_of::<Arc<Vec<u8>>>())
            .ok_or_else(|| NativeError::limit("typed V2 origin row-reference size overflow"))?,
    )
    .map_err(|_| NativeError::limit("typed V2 origin row-reference size exceeds u64"))?;
    for row in rows {
        let payload = u64::try_from(row.len())
            .map_err(|_| NativeError::limit("typed V2 origin payload exceeds u64"))?;
        let allocation = arc_vec_allocation_bytes(row.capacity())?;
        let allocation = u64::try_from(allocation)
            .map_err(|_| NativeError::limit("typed V2 origin allocation exceeds u64"))?;
        payload_bytes = checked_add(payload_bytes, payload)?;
        metadata_bytes = checked_add(
            metadata_bytes,
            allocation.checked_sub(payload).ok_or_else(|| {
                NativeError::protocol("typed V2 origin allocation accounting underflow")
            })?,
        )?;
        document_rows.push(Arc::new(row));
    }

    let mut closure_rows = Vec::new();
    closure_rows
        .try_reserve_exact(document_rows.len())
        .map_err(|_| NativeError::limit("typed V2 origin closure allocation failed"))?;
    metadata_bytes = checked_add(
        metadata_bytes,
        u64::try_from(
            closure_rows
                .capacity()
                .checked_mul(size_of::<Arc<Vec<u8>>>())
                .ok_or_else(|| NativeError::limit("typed V2 origin closure size overflow"))?,
        )
        .map_err(|_| NativeError::limit("typed V2 origin closure size exceeds u64"))?,
    )?;
    closure_rows.extend(document_rows.iter().cloned());

    let mut effective_tables = Vec::new();
    effective_tables
        .try_reserve_exact(2)
        .map_err(|_| NativeError::limit("typed V2 origin table allocation failed"))?;
    metadata_bytes = checked_add(
        metadata_bytes,
        u64::try_from(
            effective_tables
                .capacity()
                .checked_mul(size_of::<FacadeTableV2>())
                .ok_or_else(|| NativeError::limit("typed V2 origin table size overflow"))?,
        )
        .map_err(|_| NativeError::limit("typed V2 origin table size exceeds u64"))?,
    )?;
    effective_tables.push(FacadeTableV2 {
        coordinate: CoordinateV2 {
            collection: CollectionV2::OriginEntries,
            scope: ScopeV2::Document,
            document_ordinal: Some(0),
            signature_kind: SignatureKindV2::All,
            include_builtins: true,
        },
        rows: document_rows,
        source_identity: 0,
    });
    effective_tables.push(FacadeTableV2 {
        coordinate: CoordinateV2 {
            collection: CollectionV2::OriginEntries,
            scope: ScopeV2::Closure,
            document_ordinal: None,
            signature_kind: SignatureKindV2::All,
            include_builtins: true,
        },
        rows: closure_rows,
        source_identity: 0,
    });
    effective_tables.sort_unstable_by_key(|table| table.coordinate);
    reject_duplicate_coordinates(&effective_tables)?;
    Ok(RetainedOriginTablesV2 {
        effective_tables,
        payload_bytes,
        metadata_bytes,
    })
}

fn typed_initial_counters(
    storage: &TypedFacadeStorageV2,
    attestation: &NativeSnapshotAttestationV2,
) -> NativeResult<[u64; COUNTER_NAMES.len()]> {
    let typed = storage.counters()?;
    let component = typed.component;
    let typed_arc_overhead = arc_sized_allocation_bytes::<TypedFacadeStorageV2>()?
        .checked_sub(size_of::<TypedFacadeStorageV2>())
        .ok_or_else(|| NativeError::protocol("typed V2 Arc layout accounting underflow"))?;
    let publication_bytes = arc_sized_allocation_bytes::<PublicationStorageV2>()?;
    let dynamic_attestation_bytes = attestation
        .backend
        .len()
        .checked_add(attestation.root_document_key.len())
        .ok_or_else(|| NativeError::limit("typed V2 attestation size overflow"))?;
    let additional_metadata = typed_arc_overhead
        .checked_add(publication_bytes)
        .and_then(|value| value.checked_add(dynamic_attestation_bytes))
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| NativeError::limit("typed V2 owner metadata size overflow"))?;
    let retained_metadata = typed
        .retained_metadata_bytes
        .checked_add(additional_metadata)
        .ok_or_else(|| NativeError::limit("typed V2 metadata counter overflow"))?;
    let retained_owner = typed
        .retained_owner_bytes
        .checked_add(additional_metadata)
        .ok_or_else(|| NativeError::limit("typed V2 owner counter overflow"))?;
    if storage
        .max_memory_bytes()
        .is_some_and(|maximum| retained_owner > maximum)
    {
        return Err(NativeError::limit(
            "typed V2 publication envelope exceeds max_memory_bytes",
        ));
    }

    let mut initial = [0_u64; COUNTER_NAMES.len()];
    initial[0] = component.node_requests;
    initial[1] = component.node_hits;
    initial[2] = component.string_requests;
    initial[3] = component.string_hits;
    initial[4] = component.bytes_requests;
    initial[5] = component.bytes_hits;
    initial[6] = component.integer_requests;
    initial[7] = component.integer_hits;
    initial[8] = component.sequence_requests;
    initial[9] = component.sequence_hits;
    initial[10] = typed.canonical_input_rows;
    initial[11] = typed.canonical_input_bytes;
    initial[12] = component.unique_nodes;
    initial[13] = component.unique_strings;
    initial[14] = component.unique_bytes;
    initial[15] = component.unique_integers;
    initial[16] = component.unique_sequences;
    initial[RETAINED_DOCUMENT_TABLES] = typed.retained_document_tables;
    initial[RETAINED_ROW_FIRST] =
        storage.retained_rows(TypedFacadeCollectionV2::OntologyAnnotations)?;
    initial[RETAINED_ROW_FIRST + 1] = storage.retained_rows(TypedFacadeCollectionV2::Axioms)?;
    initial[RETAINED_ROW_FIRST + 2] = storage.retained_rows(TypedFacadeCollectionV2::Extensions)?;
    initial[RETAINED_COMPONENT_BYTES] = typed.retained_component_bytes;
    initial[RETAINED_ROOT_BYTES] = typed.retained_root_bytes;
    initial[RETAINED_INDEX_BYTES] = typed.retained_index_bytes;
    initial[RETAINED_METADATA_BYTES] = retained_metadata;
    initial[RETAINED_OWNER_BYTES] = retained_owner;
    initial[PEAK_BUILDER_BYTES] = typed.peak_builder_live_bytes.max(retained_owner);
    initial[PEAK_FREEZE_BYTES] = typed.peak_freeze_live_bytes.max(retained_owner);
    initial[47] = typed.publication_structural_rows_copied;
    initial[48] = typed.publication_structural_bytes_copied;
    initial[PUBLICATION_METADATA_RECORDS] = 2_u64
        .checked_add(attestation.document_count)
        .and_then(|value| value.checked_add(attestation.import_edge_count))
        .ok_or_else(|| NativeError::limit("typed V2 publication record count overflow"))?;
    validate_retained_total(&initial)?;
    Ok(initial)
}

#[derive(Debug)]
struct PageRequestV2 {
    coordinate: CoordinateV2,
    start: u64,
    max_rows: u32,
    max_bytes: u64,
    max_row_bytes: u64,
    digest_filter: Option<Digest>,
}

impl PageRequestV2 {
    fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let selected = Self {
            coordinate: coordinate_from_request(value)?,
            start: exact_u64(value.getattr("start")?, "page start")?,
            max_rows: exact_u32(value.getattr("max_rows")?, "page max_rows")?,
            max_bytes: exact_u64(value.getattr("max_bytes")?, "page max_bytes")?,
            max_row_bytes: exact_u64(value.getattr("max_row_bytes")?, "page max_row_bytes")?,
            digest_filter: optional_digest(value.getattr("digest_filter")?)?,
        };
        validate_page_bounds(
            selected.max_rows,
            selected.max_bytes,
            selected.max_row_bytes,
        )?;
        Ok(selected)
    }
}

fn validate_page_bounds(max_rows: u32, max_bytes: u64, max_row_bytes: u64) -> PyResult<()> {
    if !(1..=MAX_FACADE_PAGE_ROWS_V2).contains(&max_rows) {
        return Err(PyValueError::new_err(
            "V2 page max_rows is zero or exceeds the frozen bound",
        ));
    }
    if !(1..=MAX_FACADE_PAGE_BYTES_V2).contains(&max_bytes) {
        return Err(PyValueError::new_err(
            "V2 page max_bytes is zero or exceeds the frozen bound",
        ));
    }
    if max_row_bytes == 0 {
        return Err(PyValueError::new_err(
            "V2 page max_row_bytes must be positive",
        ));
    }
    Ok(())
}

#[derive(Debug)]
struct ContainsRequestV2 {
    coordinate: CoordinateV2,
    canonical: Vec<u8>,
    max_row_bytes: u64,
}

impl ContainsRequestV2 {
    fn from_python(value: &Bound<'_, PyAny>, attested_max_row_bytes: u64) -> PyResult<Self> {
        // The Python dataclass is frozen only by convention: object.__setattr__
        // can still corrupt an exact instance. Read and bind the scalar limit
        // before inspecting or allocating attacker-controlled canonical data.
        let max_row_bytes = exact_u64(value.getattr("max_row_bytes")?, "contains max_row_bytes")?;
        if max_row_bytes != attested_max_row_bytes {
            return Err(PyValueError::new_err(
                "V2 request row bound does not match the publication",
            ));
        }
        let canonical = value.getattr("canonical")?;
        if !canonical
            .get_type()
            .is(canonical.py().get_type::<PyBytes>())
        {
            return Err(PyTypeError::new_err(
                "V2 contains canonical row must be exact bytes",
            ));
        }
        let bytes = canonical.cast::<PyBytes>()?.as_bytes();
        let byte_count = u64::try_from(bytes.len())
            .map_err(|_| PyValueError::new_err("V2 contains row length exceeds u64"))?;
        if bytes.is_empty() || byte_count > attested_max_row_bytes {
            return Err(PyValueError::new_err(
                "V2 contains canonical row is empty or exceeds the attested row bound",
            ));
        }
        // The public facade decoded this exact request once and retained the
        // immutable bytes bound to that validation. Compare the private bytes
        // directly so a mutated request cannot trigger a second decode or an
        // attacker-sized canonical re-encoding at this lower boundary.
        let validated = value.call_method0("_validated_canonical_v2")?;
        if !validated
            .get_type()
            .is(validated.py().get_type::<PyBytes>())
        {
            return Err(PyTypeError::new_err(
                "V2 contains validated canonical row must be exact bytes",
            ));
        }
        let validated = validated.cast::<PyBytes>()?.as_bytes();
        if u64::try_from(validated.len()).map_or(true, |length| {
            length > attested_max_row_bytes || validated != bytes
        }) {
            return Err(PyValueError::new_err(
                "V2 contains canonical row diverges from its validated bytes",
            ));
        }
        let coordinate = CoordinateV2 {
            collection: CollectionV2::from_python(&value.getattr("collection")?)?,
            scope: ScopeV2::from_python(&value.getattr("scope")?)?,
            document_ordinal: optional_u64(value.getattr("document_ordinal")?)?,
            signature_kind: SignatureKindV2::All,
            include_builtins: true,
        };
        let mut owned = Vec::new();
        owned
            .try_reserve_exact(bytes.len())
            .map_err(|_| PyMemoryError::new_err("native V2 contains allocation failed"))?;
        owned.extend_from_slice(bytes);
        Ok(Self {
            coordinate,
            canonical: owned,
            max_row_bytes,
        })
    }
}

fn coordinate_from_request(value: &Bound<'_, PyAny>) -> PyResult<CoordinateV2> {
    Ok(CoordinateV2 {
        collection: CollectionV2::from_python(&value.getattr("collection")?)?,
        scope: ScopeV2::from_python(&value.getattr("scope")?)?,
        document_ordinal: optional_u64(value.getattr("document_ordinal")?)?,
        signature_kind: SignatureKindV2::from_python(&value.getattr("signature_kind")?)?,
        include_builtins: exact_bool(value.getattr("include_builtins")?, "page include_builtins")?,
    })
}

fn optional_u64(value: Bound<'_, PyAny>) -> PyResult<Option<u64>> {
    if value.is_none() {
        Ok(None)
    } else {
        exact_u64(value, "document_ordinal").map(Some)
    }
}

fn optional_digest(value: Bound<'_, PyAny>) -> PyResult<Option<Digest>> {
    if value.is_none() {
        return Ok(None);
    }
    digest_from_python(&value).map(Some)
}

fn digest_range(rows: &[Arc<Vec<u8>>], digest: Option<&Digest>) -> (usize, usize) {
    let Some(selected) = digest else {
        return (0, rows.len());
    };
    let start = rows.partition_point(|row| {
        row.get(..32)
            .is_some_and(|prefix| prefix < selected.as_slice())
    });
    let end = rows.partition_point(|row| {
        row.get(..32)
            .is_some_and(|prefix| prefix <= selected.as_slice())
    });
    (start, end)
}

fn bounded_page_end(
    rows: &[Arc<Vec<u8>>],
    start: usize,
    stop: usize,
    max_bytes: u64,
) -> NativeResult<(usize, u64)> {
    if start > stop || stop > rows.len() || max_bytes == 0 {
        return Err(NativeError::protocol(
            "native V2 bounded page coordinates are invalid",
        ));
    }
    let mut end = start;
    let mut used = 0_u64;
    for row in &rows[start..stop] {
        let row_bytes = u64::try_from(row.len())
            .map_err(|_| NativeError::limit("native V2 row length exceeds u64"))?;
        let following = used
            .checked_add(row_bytes)
            .ok_or_else(|| NativeError::limit("native V2 page byte count overflow"))?;
        if end > start && following > max_bytes {
            break;
        }
        end = end
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native V2 page cursor overflow"))?;
        used = following;
        if used > max_bytes {
            break;
        }
    }
    Ok((end, used))
}

fn require_exact_type(
    module: &Bound<'_, PyModule>,
    name: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let expected = module.getattr(name)?.cast_into::<PyType>()?;
    if value.get_type().is(&expected) {
        Ok(())
    } else {
        Err(PyTypeError::new_err(format!(
            "native V2 owner requires an exact {name}",
        )))
    }
}

fn require_exact_handoff_type(value: &Bound<'_, PyAny>, name: &str) -> PyResult<()> {
    let module = value.py().import(HANDOFF_MODULE)?;
    require_exact_type(&module, name, value)
}

fn exact_u64(value: Bound<'_, PyAny>, name: &str) -> PyResult<u64> {
    if !value.get_type().is(value.py().get_type::<PyInt>()) {
        return Err(PyTypeError::new_err(format!(
            "native V2 {name} must be an exact int",
        )));
    }
    value.extract()
}

fn exact_u32(value: Bound<'_, PyAny>, name: &str) -> PyResult<u32> {
    if !value.get_type().is(value.py().get_type::<PyInt>()) {
        return Err(PyTypeError::new_err(format!(
            "native V2 {name} must be an exact int",
        )));
    }
    value.extract()
}

fn exact_bool(value: Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
    if !value.get_type().is(value.py().get_type::<PyBool>()) {
        return Err(PyTypeError::new_err(format!(
            "native V2 {name} must be an exact bool",
        )));
    }
    value.extract()
}

fn exact_bytes<'py>(value: &'py Bound<'py, PyAny>, name: &str) -> PyResult<&'py [u8]> {
    if !value.get_type().is(value.py().get_type::<PyBytes>()) {
        return Err(PyTypeError::new_err(format!(
            "native V2 {name} must be exact bytes",
        )));
    }
    Ok(value.cast::<PyBytes>()?.as_bytes())
}

fn native_error_to_python(error: NativeError) -> PyErr {
    if error.code == "NATIVE_WIRE_LIMIT" {
        PyMemoryError::new_err(error.message)
    } else {
        PyRuntimeError::new_err(error.message)
    }
}

#[derive(Debug)]
struct StorageBuilderV2 {
    budget: AllocationBudget,
    interner: HashMap<SharedRowV2, CollectionV2>,
    interner_temporary_bytes: u64,
    effective_tables: Vec<FacadeTableV2>,
    raw_document_tables: Vec<FacadeTableV2>,
    raw_supplied: bool,
}

impl StorageBuilderV2 {
    fn new(maximum: u64) -> NativeResult<Self> {
        Ok(Self {
            budget: AllocationBudget::new(maximum)?,
            interner: HashMap::new(),
            interner_temporary_bytes: 0,
            effective_tables: Vec::new(),
            raw_document_tables: Vec::new(),
            raw_supplied: false,
        })
    }

    #[cfg(feature = "test-hooks")]
    fn add_python_tables(
        &mut self,
        mapping: &Bound<'_, PyDict>,
        raw: bool,
        document_count: u64,
    ) -> NativeResult<()> {
        if !raw {
            let following_count = self
                .effective_tables
                .len()
                .checked_add(self.raw_document_tables.len())
                .and_then(|value| value.checked_add(mapping.len()))
                .ok_or_else(|| NativeError::limit("native V2 facade table count overflow"))?;
            if following_count > MAX_FIXTURE_TABLES {
                return Err(NativeError::limit(
                    "native V2 facade fixture has too many coordinate tables",
                ));
            }
            self.reserve_table_capacity(false, mapping.len())?;
        }
        for (key, rows) in mapping {
            let coordinate = coordinate_from_key(&key).map_err(|_error| {
                NativeError::protocol("native V2 coordinate extraction failed")
            })?;
            if coordinate
                .document_ordinal
                .is_some_and(|ordinal| ordinal >= document_count)
            {
                return Err(NativeError::protocol(
                    "native V2 facade coordinate document ordinal is out of bounds",
                ));
            }
            if !rows.get_type().is(rows.py().get_type::<PyTuple>()) {
                return Err(NativeError::protocol(
                    "native V2 row table must be an exact tuple",
                ));
            }
            let rows = rows
                .cast::<PyTuple>()
                .map_err(|_error| NativeError::protocol("native V2 row tuple extraction failed"))?;
            if raw && coordinate.collection == CollectionV2::Signature {
                self.validate_raw_signature_table(coordinate, rows)?;
            }
            if raw
                && self.effective_tables.iter().any(|table| {
                    table.coordinate == coordinate
                        && table.source_identity == rows.as_ptr() as usize
                })
            {
                continue;
            }
            let following_count = self
                .effective_tables
                .len()
                .checked_add(self.raw_document_tables.len())
                .and_then(|value| value.checked_add(1))
                .ok_or_else(|| NativeError::limit("native V2 facade table count overflow"))?;
            if following_count > MAX_FIXTURE_TABLES {
                return Err(NativeError::limit(
                    "native V2 facade fixture has too many coordinate tables",
                ));
            }
            if raw {
                self.reserve_table_capacity(true, 1)?;
            }
            let table = self.build_table(coordinate, rows)?;
            if raw {
                self.raw_document_tables.push(table);
            } else {
                self.effective_tables.push(table);
            }
        }
        Ok(())
    }

    #[cfg(any(test, feature = "test-hooks"))]
    fn reserve_table_capacity(&mut self, raw: bool, additional: usize) -> NativeResult<()> {
        let tables = if raw {
            &mut self.raw_document_tables
        } else {
            &mut self.effective_tables
        };
        let old_capacity = tables.capacity();
        tables
            .try_reserve_exact(additional)
            .map_err(|_| NativeError::limit("native V2 facade table allocation failed"))?;
        let capacity_bytes = tables
            .capacity()
            .saturating_sub(old_capacity)
            .checked_mul(size_of::<FacadeTableV2>())
            .ok_or_else(|| NativeError::limit("native V2 facade table capacity overflow"))?;
        self.budget.claim(capacity_bytes)?;
        Ok(())
    }

    #[cfg(feature = "test-hooks")]
    fn validate_raw_signature_table(
        &self,
        coordinate: CoordinateV2,
        rows: &Bound<'_, PyTuple>,
    ) -> NativeResult<()> {
        let expected = self
            .effective_tables
            .iter()
            .find(|table| table.coordinate == coordinate)
            .ok_or_else(|| {
                NativeError::protocol(
                    "native V2 raw signature projection has no derived effective coordinate",
                )
            })?;
        if expected.rows.len() != rows.len() {
            return Err(NativeError::protocol(
                "native V2 raw signature projection diverges from retained roots",
            ));
        }
        for (observed, expected) in rows.iter().zip(&expected.rows) {
            let observed = exact_bytes(&observed, "raw signature row").map_err(|_error| {
                NativeError::protocol("native V2 raw signature row extraction failed")
            })?;
            if observed != expected.as_slice() {
                return Err(NativeError::protocol(
                    "native V2 raw signature projection diverges from retained roots",
                ));
            }
        }
        Ok(())
    }

    #[cfg(feature = "test-hooks")]
    fn build_table(
        &mut self,
        coordinate: CoordinateV2,
        rows: &Bound<'_, PyTuple>,
    ) -> NativeResult<FacadeTableV2> {
        let mut retained = Vec::new();
        retained
            .try_reserve_exact(rows.len())
            .map_err(|_| NativeError::limit("native V2 row-reference allocation failed"))?;
        self.claim_row_capacity(retained.capacity())?;
        for value in rows {
            let bytes = exact_bytes(&value, "facade row")
                .map_err(|_error| NativeError::protocol("native V2 row extraction failed"))?;
            if bytes.is_empty() {
                return Err(NativeError::protocol(
                    "native V2 retained rows must be nonempty",
                ));
            }
            let selected = self.intern(bytes, coordinate.collection)?;
            retained.push(selected);
        }
        Ok(FacadeTableV2 {
            coordinate,
            rows: retained,
            source_identity: rows.as_ptr() as usize,
        })
    }

    #[cfg(any(test, feature = "test-hooks"))]
    fn claim_row_capacity(&mut self, capacity: usize) -> NativeResult<()> {
        let retained = capacity
            .checked_mul(size_of::<Arc<Vec<u8>>>())
            .ok_or_else(|| NativeError::limit("native V2 row capacity overflow"))?;
        self.budget.claim(retained)
    }

    #[cfg(any(test, feature = "test-hooks"))]
    fn add_derived_table(&mut self, coordinate: CoordinateV2, rows: &[&[u8]]) -> NativeResult<()> {
        if let Some(index) = self
            .effective_tables
            .iter()
            .position(|table| table.coordinate == coordinate)
        {
            let observed = &self.effective_tables[index].rows;
            if observed.len() != rows.len()
                || observed
                    .iter()
                    .zip(rows)
                    .any(|(left, right)| left.as_slice() != *right)
            {
                return Err(NativeError::protocol(
                    "native V2 signature projection diverges from retained roots",
                ));
            }
            return Ok(());
        }
        let following = self
            .effective_tables
            .len()
            .checked_add(self.raw_document_tables.len())
            .and_then(|value| value.checked_add(1))
            .ok_or_else(|| NativeError::limit("native V2 facade table count overflow"))?;
        if following > MAX_FIXTURE_TABLES {
            return Err(NativeError::limit(
                "native V2 facade fixture has too many derived tables",
            ));
        }
        self.reserve_table_capacity(false, 1)?;
        let mut retained = Vec::new();
        retained
            .try_reserve_exact(rows.len())
            .map_err(|_| NativeError::limit("native V2 row-reference allocation failed"))?;
        self.claim_row_capacity(retained.capacity())?;
        for row in rows {
            retained.push(self.intern(row, coordinate.collection)?);
        }
        self.effective_tables.push(FacadeTableV2 {
            coordinate,
            rows: retained,
            source_identity: 0,
        });
        Ok(())
    }

    #[cfg(any(test, feature = "test-hooks"))]
    fn intern(&mut self, bytes: &[u8], collection: CollectionV2) -> NativeResult<Arc<Vec<u8>>> {
        if bytes.is_empty() {
            return Err(NativeError::protocol(
                "native V2 retained rows must be nonempty",
            ));
        }
        if let Some(existing_collection) = self.interner.get_mut(bytes) {
            *existing_collection = (*existing_collection).min(collection);
            let existing = self
                .interner
                .get_key_value(bytes)
                .map(|(shared, _category)| &shared.0)
                .ok_or_else(|| NativeError::protocol("native V2 row interner lost an entry"))?;
            return Ok(Arc::clone(existing));
        }
        self.budget.claim(arc_vec_allocation_bytes(bytes.len())?)?;
        let mut owned = Vec::new();
        owned
            .try_reserve_exact(bytes.len())
            .map_err(|_| NativeError::limit("native V2 row allocation failed"))?;
        let capacity_excess = owned.capacity().saturating_sub(bytes.len());
        self.budget.claim(capacity_excess)?;
        owned.extend_from_slice(bytes);
        let shared = Arc::new(owned);
        let old_capacity = self.interner.capacity();
        self.interner
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native V2 row interner allocation failed"))?;
        let added_capacity = self.interner.capacity().saturating_sub(old_capacity);
        // HashMap's allocator layout is deliberately private. Two full entry
        // widths per reported slot is a conservative live-builder bound that
        // covers its entry and control storage without entering retained
        // publication counters.
        let temporary = added_capacity
            .checked_mul(
                size_of::<(SharedRowV2, CollectionV2)>()
                    .checked_mul(2)
                    .ok_or_else(|| NativeError::limit("native V2 interner size overflow"))?,
            )
            .ok_or_else(|| NativeError::limit("native V2 interner size overflow"))?;
        self.budget.claim_temporary(temporary)?;
        self.interner_temporary_bytes = self
            .interner_temporary_bytes
            .checked_add(
                u64::try_from(temporary)
                    .map_err(|_| NativeError::limit("native V2 interner allocation exceeds u64"))?,
            )
            .ok_or_else(|| NativeError::limit("native V2 interner accounting overflow"))?;
        self.interner
            .insert(SharedRowV2(Arc::clone(&shared)), collection);
        Ok(shared)
    }

    #[cfg(test)]
    fn add_test_table(
        &mut self,
        coordinate: CoordinateV2,
        rows: &[&[u8]],
        raw: bool,
        source_identity: usize,
    ) -> NativeResult<()> {
        if raw {
            self.raw_supplied = true;
            if self.effective_tables.iter().any(|table| {
                table.coordinate == coordinate && table.source_identity == source_identity
            }) {
                return Ok(());
            }
        }
        let following = self
            .effective_tables
            .len()
            .checked_add(self.raw_document_tables.len())
            .and_then(|value| value.checked_add(1))
            .ok_or_else(|| NativeError::limit("native V2 facade table count overflow"))?;
        if following > MAX_FIXTURE_TABLES {
            return Err(NativeError::limit(
                "native V2 facade fixture has too many coordinate tables",
            ));
        }
        self.reserve_table_capacity(raw, 1)?;
        let mut retained = Vec::new();
        retained
            .try_reserve_exact(rows.len())
            .map_err(|_| NativeError::limit("native V2 test row allocation failed"))?;
        self.claim_row_capacity(retained.capacity())?;
        for row in rows {
            retained.push(self.intern(row, coordinate.collection)?);
        }
        let table = FacadeTableV2 {
            coordinate,
            rows: retained,
            source_identity,
        };
        if raw {
            self.raw_document_tables.push(table);
        } else {
            self.effective_tables.push(table);
        }
        Ok(())
    }

    fn finish(
        mut self,
        attestation: NativeSnapshotAttestationV2,
    ) -> NativeResult<Arc<PublicationStorageV2>> {
        let attestation_bytes = attestation
            .backend
            .len()
            .checked_add(attestation.root_document_key.len())
            .ok_or_else(|| NativeError::limit("native V2 attestation size overflow"))?;
        self.budget.claim(attestation_bytes)?;
        self.budget
            .claim(arc_sized_allocation_bytes::<PublicationStorageV2>()?)?;
        self.effective_tables
            .sort_unstable_by_key(|table| table.coordinate);
        self.raw_document_tables
            .sort_unstable_by_key(|table| table.coordinate);
        reject_duplicate_coordinates(&self.effective_tables)?;
        reject_duplicate_coordinates(&self.raw_document_tables)?;
        let mut initial = [0_u64; COUNTER_NAMES.len()];
        initial[RETAINED_DOCUMENT_TABLES] = attestation.document_count;
        for table in &self.effective_tables {
            add_retained_row_occurrences(&mut initial, table)?;
        }
        for table in &self.raw_document_tables {
            add_retained_row_occurrences(&mut initial, table)?;
        }
        for effective in self
            .effective_tables
            .iter()
            .filter(|table| is_canonical_input_table(table))
        {
            let selected = if self.raw_supplied {
                self.raw_document_tables
                    .binary_search_by_key(&effective.coordinate, |table| table.coordinate)
                    .ok()
                    .map_or(effective, |index| &self.raw_document_tables[index])
            } else {
                effective
            };
            add_canonical_input(&mut initial, selected)?;
        }
        if self.raw_supplied {
            for raw in self
                .raw_document_tables
                .iter()
                .filter(|table| is_canonical_input_table(table))
                .filter(|table| {
                    self.effective_tables
                        .binary_search_by_key(&table.coordinate, |item| item.coordinate)
                        .is_err()
                })
            {
                add_canonical_input(&mut initial, raw)?;
            }
        }
        let mut payload_total = 0_u64;
        for (row, collection) in &self.interner {
            let bytes = u64::try_from(row.0.len())
                .map_err(|_| NativeError::limit("native V2 retained row exceeds u64"))?;
            payload_total = checked_add(payload_total, bytes)?;
            let memory_counter = collection.memory_counter();
            initial[memory_counter] = checked_add(initial[memory_counter], bytes)?;
        }
        let interner = std::mem::take(&mut self.interner);
        drop(interner);
        self.budget
            .release_temporary(self.interner_temporary_bytes)?;
        let effective_tables = std::mem::take(&mut self.effective_tables);
        let raw_document_tables = self
            .raw_supplied
            .then(|| std::mem::take(&mut self.raw_document_tables));
        if self.budget.temporary != 0 {
            return Err(NativeError::protocol(
                "native V2 builder retained temporary allocation accounting",
            ));
        }
        initial[RETAINED_COMPONENT_BYTES] = 0;
        initial[RETAINED_METADATA_BYTES] = self
            .budget
            .retained()
            .checked_sub(payload_total)
            .ok_or_else(|| NativeError::protocol("native V2 retained byte accounting underflow"))?;
        initial[RETAINED_OWNER_BYTES] = self.budget.retained();
        initial[PEAK_BUILDER_BYTES] = self.budget.peak;
        initial[PEAK_FREEZE_BYTES] = self.budget.peak;
        initial[PUBLICATION_METADATA_RECORDS] = 2_u64
            .checked_add(attestation.document_count)
            .and_then(|value| value.checked_add(attestation.import_edge_count))
            .ok_or_else(|| NativeError::limit("native V2 publication record count overflow"))?;
        validate_retained_total(&initial)?;
        Ok(Arc::new(PublicationStorageV2 {
            attestation,
            effective_tables,
            raw_document_tables,
            typed_structural: None,
            counters: CounterStateV2::new(initial),
        }))
    }
}

fn is_canonical_input_table(table: &FacadeTableV2) -> bool {
    table.coordinate.scope == ScopeV2::Document
        && table.coordinate.signature_kind == SignatureKindV2::All
        && table.coordinate.include_builtins
        && matches!(
            table.coordinate.collection,
            CollectionV2::OntologyAnnotations | CollectionV2::Axioms | CollectionV2::Extensions
        )
}

fn add_canonical_input(
    counters: &mut [u64; COUNTER_NAMES.len()],
    table: &FacadeTableV2,
) -> NativeResult<()> {
    counters[10] = checked_add(
        counters[10],
        u64::try_from(table.rows.len())
            .map_err(|_| NativeError::limit("native V2 canonical input row count exceeds u64"))?,
    )?;
    for row in &table.rows {
        counters[11] = checked_add(
            counters[11],
            u64::try_from(row.len())
                .map_err(|_| NativeError::limit("native V2 canonical input bytes exceed u64"))?,
        )?;
    }
    Ok(())
}

fn checked_add(left: u64, right: u64) -> NativeResult<u64> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("native V2 retained counter overflow"))
}

fn add_retained_row_occurrences(
    counters: &mut [u64; COUNTER_NAMES.len()],
    table: &FacadeTableV2,
) -> NativeResult<()> {
    let Some(counter) = table.coordinate.collection.retained_row_counter() else {
        return Ok(());
    };
    counters[counter] = checked_add(
        counters[counter],
        u64::try_from(table.rows.len())
            .map_err(|_| NativeError::limit("native V2 retained row count exceeds u64"))?,
    )?;
    Ok(())
}

fn validate_retained_total(values: &[u64; COUNTER_NAMES.len()]) -> NativeResult<()> {
    let total = (RETAINED_COMPONENT_BYTES..=RETAINED_METADATA_BYTES)
        .try_fold(0_u64, |current, index| checked_add(current, values[index]))?;
    if total != values[RETAINED_OWNER_BYTES] {
        return Err(NativeError::protocol(
            "native V2 disjoint retained byte counters diverge",
        ));
    }
    Ok(())
}

fn reject_duplicate_coordinates(tables: &[FacadeTableV2]) -> NativeResult<()> {
    if tables
        .windows(2)
        .any(|pair| pair[0].coordinate == pair[1].coordinate)
    {
        return Err(NativeError::protocol(
            "native V2 facade tables contain duplicate coordinates",
        ));
    }
    Ok(())
}

#[cfg(feature = "test-hooks")]
fn validate_fixture_mapping<'py>(
    handoff: &Bound<'py, PyModule>,
    attestation: &Bound<'py, PyAny>,
    collections: &Bound<'py, PyAny>,
    limits: &Bound<'py, PyAny>,
    raw_document_owner: bool,
) -> PyResult<(Bound<'py, PyDict>, u64)> {
    let kwargs = PyDict::new(handoff.py());
    kwargs.set_item("raw_document_owner", raw_document_owner)?;
    let frozen = handoff
        .getattr("_freeze_fixture_collections_v2")?
        .call((collections, attestation, limits), Some(&kwargs))?;
    let result = frozen.cast::<PyTuple>()?;
    Ok((
        result.get_item(0)?.cast_into::<PyDict>()?,
        result.get_item(1)?.extract()?,
    ))
}

#[cfg(feature = "test-hooks")]
fn reconstruct_attestation<'py>(
    handoff: &Bound<'py, PyModule>,
    attestation: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(handoff.py());
    let fields = handoff
        .getattr("NATIVE_SNAPSHOT_ATTESTATION_FIELDS_V2")?
        .cast_into::<PyTuple>()?;
    for field in &fields {
        let row = field.cast::<PyTuple>()?;
        let name: String = row.get_item(1)?.extract()?;
        let selected = attestation.getattr(name.as_str())?;
        if name == "owl2_dl_report_summary" && !selected.is_none() {
            kwargs.set_item(&name, reconstruct_owl2_summary(handoff, &selected)?)?;
        } else {
            kwargs.set_item(&name, selected)?;
        }
    }
    handoff
        .getattr("NativeSnapshotAttestationV2")?
        .call((), Some(&kwargs))
}

#[cfg(feature = "test-hooks")]
fn reconstruct_owl2_summary<'py>(
    handoff: &Bound<'py, PyModule>,
    summary: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    require_exact_type(handoff, "NativeOWL2DLReportSummaryV2", summary)?;
    let kwargs = PyDict::new(handoff.py());
    let fields = handoff
        .getattr("NATIVE_OWL2_DL_REPORT_SUMMARY_FIELDS_V2")?
        .cast_into::<PyTuple>()?;
    for field in &fields {
        let name: String = field.cast::<PyTuple>()?.get_item(1)?.extract()?;
        kwargs.set_item(&name, summary.getattr(name.as_str())?)?;
    }
    handoff
        .getattr("NativeOWL2DLReportSummaryV2")?
        .call((), Some(&kwargs))
}

#[cfg(feature = "test-hooks")]
#[allow(clippy::too_many_arguments)]
fn validate_content_binding(
    handoff: &Bound<'_, PyModule>,
    attestation: &Bound<'_, PyAny>,
    collections: &Bound<'_, PyDict>,
    raw_document_collections: &Bound<'_, PyDict>,
    documents: &Bound<'_, PyAny>,
    report: &Bound<'_, PyAny>,
    root_document_key: &Bound<'_, PyAny>,
    load_options: &Bound<'_, PyAny>,
    capability_bits: &Bound<'_, PyAny>,
    fingerprint_evidence: &Bound<'_, PyAny>,
    fingerprint_preimages: &Bound<'_, PyAny>,
    facade_cardinality_summary: &Bound<'_, PyAny>,
    owl2_dl_report_summary: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    let kwargs = PyDict::new(handoff.py());
    kwargs.set_item("documents", documents)?;
    kwargs.set_item("report", report)?;
    kwargs.set_item("root_document_key", root_document_key)?;
    kwargs.set_item("load_options", load_options)?;
    kwargs.set_item("capability_bits", capability_bits)?;
    kwargs.set_item("collections", collections)?;
    kwargs.set_item("fingerprint_evidence", fingerprint_evidence)?;
    kwargs.set_item("fingerprint_preimages", fingerprint_preimages)?;
    kwargs.set_item(
        "owl2_dl_report_summary",
        owl2_dl_report_summary.map_or_else(|| handoff.py().None(), |value| value.clone().unbind()),
    )?;
    kwargs.set_item("facade_cardinality_summary", facade_cardinality_summary)?;
    kwargs.set_item("raw_document_collections", raw_document_collections)?;
    let observed = handoff
        .getattr("native_snapshot_content_digests_v2")?
        .call((), Some(&kwargs))?;
    for name in [
        "root_table_sha256",
        "effective_root_table_sha256",
        "fingerprint_inputs_sha256",
        "source_manifest_sha256",
        "provenance_manifest_sha256",
        "effective_origin_manifest_sha256",
    ] {
        if digest_attr(&observed, name)? != digest_attr(attestation, name)? {
            return Err(PyValueError::new_err(
                "native V2 retained content diverges from its owner attestation",
            ));
        }
    }
    Ok(())
}

#[cfg(feature = "test-hooks")]
fn derive_and_add_signature_tables(
    py: Python<'_>,
    collections: &Bound<'_, PyDict>,
    limits: &Bound<'_, PyAny>,
    document_count: u64,
    builder: &mut StorageBuilderV2,
) -> PyResult<u64> {
    let coordinate_count = document_count
        .checked_add(1)
        .and_then(|value| value.checked_mul(14))
        .ok_or_else(|| PyMemoryError::new_err("native V2 signature table count overflow"))?;
    if coordinate_count > MAX_FIXTURE_TABLES as u64 {
        return Err(PyMemoryError::new_err(
            "native V2 signature projections exceed the fixture table bound",
        ));
    }
    let model = py.import("pyowl_core.model")?;
    let decode = model.getattr("decode_canonical")?;
    let canonical = model.getattr("canonical_bytes")?;
    let signature = py
        .import("pyowl_core.model.visitor")?
        .getattr("signature")?;
    let is_builtin = py
        .import("pyowl_core.index.signature")?
        .getattr("_is_builtin")?;
    let codec_kwargs = PyDict::new(py);
    codec_kwargs.set_item("limits", limits)?;
    let mut work = TemporaryBudget::default();
    let mut entities = SignatureEntitiesByScopeV2::new();
    for (key, values) in collections {
        let coordinate = coordinate_from_key(&key)?;
        if coordinate.signature_kind != SignatureKindV2::All
            || !coordinate.include_builtins
            || !matches!(
                coordinate.collection,
                CollectionV2::OntologyAnnotations | CollectionV2::Axioms | CollectionV2::Extensions
            )
        {
            continue;
        }
        if !entities.contains_key(&(coordinate.scope, coordinate.document_ordinal)) {
            work.claim(
                &mut builder.budget,
                size_of::<(
                    (ScopeV2, Option<u64>),
                    HashMap<Vec<u8>, (SignatureKindV2, bool)>,
                )>(),
            )?;
            entities.try_reserve(1).map_err(|_| {
                PyMemoryError::new_err("native V2 signature coordinate allocation failed")
            })?;
        }
        let rows = values.cast::<PyTuple>()?;
        let selected = entities
            .entry((coordinate.scope, coordinate.document_ordinal))
            .or_default();
        for row in rows {
            let decoded = decode.call((row,), Some(&codec_kwargs))?;
            let referenced = signature.call1((decoded,))?.cast_into::<PyTuple>()?;
            work.claim(
                &mut builder.budget,
                referenced
                    .len()
                    .checked_mul(size_of::<(Vec<u8>, (SignatureKindV2, bool))>())
                    .ok_or_else(|| {
                        PyMemoryError::new_err("native V2 signature entity size overflow")
                    })?,
            )?;
            selected.try_reserve(referenced.len()).map_err(|_| {
                PyMemoryError::new_err("native V2 signature entity allocation failed")
            })?;
            for entity in referenced {
                let encoded = canonical.call((&entity,), Some(&codec_kwargs))?;
                let bytes = encoded.cast::<PyBytes>()?.as_bytes();
                work.claim(&mut builder.budget, bytes.len())?;
                let mut owned = Vec::new();
                owned.try_reserve_exact(bytes.len()).map_err(|_| {
                    PyMemoryError::new_err("native V2 signature derivation allocation failed")
                })?;
                owned.extend_from_slice(bytes);
                let kind = SignatureKindV2::from_value(
                    &entity
                        .getattr("kind")?
                        .getattr("value")?
                        .extract::<String>()?,
                )?;
                let builtin: bool = is_builtin.call1((&entity,))?.extract()?;
                selected.insert(owned, (kind, builtin));
            }
        }
    }
    let mut largest = 0_u64;
    for ordinal in 0..=document_count {
        let (scope, document_ordinal) = if ordinal == document_count {
            (ScopeV2::Closure, None)
        } else {
            (ScopeV2::Document, Some(ordinal))
        };
        let selected = entities
            .remove(&(scope, document_ordinal))
            .unwrap_or_default();
        work.claim(
            &mut builder.budget,
            selected
                .len()
                .checked_mul(size_of::<(Vec<u8>, (SignatureKindV2, bool))>())
                .ok_or_else(|| {
                    PyMemoryError::new_err("native V2 sorted signature size overflow")
                })?,
        )?;
        let mut sorted = Vec::new();
        sorted
            .try_reserve_exact(selected.len())
            .map_err(|_| PyMemoryError::new_err("native V2 sorted signature allocation failed"))?;
        sorted.extend(selected);
        sorted.sort_unstable_by(|left, right| left.0.cmp(&right.0));
        work.claim(
            &mut builder.budget,
            sorted
                .len()
                .checked_mul(size_of::<&[u8]>())
                .ok_or_else(|| {
                    PyMemoryError::new_err("native V2 signature projection size overflow")
                })?,
        )?;
        for signature_kind in SignatureKindV2::ALL_VALUES {
            for include_builtins in [true, false] {
                let mut rows = Vec::new();
                rows.try_reserve_exact(sorted.len()).map_err(|_| {
                    PyMemoryError::new_err("native V2 signature projection allocation failed")
                })?;
                rows.extend(sorted.iter().filter_map(|(bytes, (kind, builtin))| {
                    ((signature_kind == SignatureKindV2::All || signature_kind == *kind)
                        && (include_builtins || !builtin))
                        .then_some(bytes.as_slice())
                }));
                for row in &rows {
                    largest = largest.max(u64::try_from(row.len()).map_err(|_| {
                        PyMemoryError::new_err("native V2 signature row length exceeds u64")
                    })?);
                }
                builder
                    .add_derived_table(
                        CoordinateV2 {
                            collection: CollectionV2::Signature,
                            scope,
                            document_ordinal,
                            signature_kind,
                            include_builtins,
                        },
                        &rows,
                    )
                    .map_err(native_error_to_python)?;
            }
        }
    }
    if !entities.is_empty() {
        return Err(PyValueError::new_err(
            "native V2 roots contain an out-of-bounds signature coordinate",
        ));
    }
    work.release(&mut builder.budget)?;
    Ok(largest)
}

#[cfg(feature = "test-hooks")]
fn coordinate_from_key(value: &Bound<'_, PyAny>) -> PyResult<CoordinateV2> {
    if !value.get_type().is(value.py().get_type::<PyTuple>()) {
        return Err(PyTypeError::new_err(
            "native V2 fixture coordinate must be an exact tuple",
        ));
    }
    let key = value.cast::<PyTuple>()?;
    if key.len() != 5 {
        return Err(PyTypeError::new_err(
            "native V2 fixture coordinate must have five fields",
        ));
    }
    Ok(CoordinateV2 {
        collection: CollectionV2::from_python(&key.get_item(0)?)?,
        scope: ScopeV2::from_python(&key.get_item(1)?)?,
        document_ordinal: optional_u64(key.get_item(2)?)?,
        signature_kind: SignatureKindV2::from_python(&key.get_item(3)?)?,
        include_builtins: exact_bool(key.get_item(4)?, "fixture include_builtins")?,
    })
}

impl NativeSnapshotAttestationV2 {
    #[cfg(any(test, feature = "test-hooks"))]
    fn fixture_for_tests() -> Self {
        Self {
            version: PUBLICATION_VERSION_V2,
            ledger_sha256: PUBLICATION_LEDGER_SHA256_V2,
            metadata_manifest_sha256: [1; 32],
            facade_access_schema_sha256: FACADE_ACCESS_SCHEMA_SHA256_V2,
            auxiliary_codec_schema_sha256: AUXILIARY_CODEC_SCHEMA_SHA256_V2,
            root_table_sha256: [2; 32],
            effective_root_table_sha256: [3; 32],
            fingerprint_inputs_sha256: [4; 32],
            source_manifest_sha256: [5; 32],
            provenance_manifest_sha256: [6; 32],
            effective_origin_manifest_sha256: [7; 32],
            diagnostics_manifest_sha256: [8; 32],
            diagnostic_reference_kinds_sha256: [9; 32],
            facade_cardinality_summary_sha256: [10; 32],
            load_options_sha256: [11; 32],
            report_sha256: [12; 32],
            max_facade_row_bytes: 1024,
            document_count: 1,
            import_edge_count: 0,
            diagnostic_count: 0,
            ontology_annotation_count: 0,
            stored_axiom_count: 1,
            effective_axiom_count: 1,
            extension_count: 0,
            total_source_bytes: 0,
            source_map_entry_count: 0,
            origin_entry_count: 0,
            rdf_mapping_report_count: 0,
            capability_bits: 7,
            api_version: (0, 1),
            model_schema: 1,
            backend: Box::from("native"),
            root_document_key: Box::from("d1:test"),
            owl2_dl_report_summary: None,
            owl2_dl_validated: false,
            owl2_dl_conforms: None,
            owl2_dl_report_sha256: None,
        }
    }

    pub(super) fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let selected = Self {
            version: value.getattr("version")?.extract()?,
            ledger_sha256: digest_attr(value, "ledger_sha256")?,
            metadata_manifest_sha256: digest_attr(value, "metadata_manifest_sha256")?,
            facade_access_schema_sha256: digest_attr(value, "facade_access_schema_sha256")?,
            auxiliary_codec_schema_sha256: digest_attr(value, "auxiliary_codec_schema_sha256")?,
            root_table_sha256: digest_attr(value, "root_table_sha256")?,
            effective_root_table_sha256: digest_attr(value, "effective_root_table_sha256")?,
            fingerprint_inputs_sha256: digest_attr(value, "fingerprint_inputs_sha256")?,
            source_manifest_sha256: digest_attr(value, "source_manifest_sha256")?,
            provenance_manifest_sha256: digest_attr(value, "provenance_manifest_sha256")?,
            effective_origin_manifest_sha256: digest_attr(
                value,
                "effective_origin_manifest_sha256",
            )?,
            diagnostics_manifest_sha256: digest_attr(value, "diagnostics_manifest_sha256")?,
            diagnostic_reference_kinds_sha256: digest_attr(
                value,
                "diagnostic_reference_kinds_sha256",
            )?,
            facade_cardinality_summary_sha256: digest_attr(
                value,
                "facade_cardinality_summary_sha256",
            )?,
            load_options_sha256: digest_attr(value, "load_options_sha256")?,
            report_sha256: digest_attr(value, "report_sha256")?,
            max_facade_row_bytes: value.getattr("max_facade_row_bytes")?.extract()?,
            document_count: value.getattr("document_count")?.extract()?,
            import_edge_count: value.getattr("import_edge_count")?.extract()?,
            diagnostic_count: value.getattr("diagnostic_count")?.extract()?,
            ontology_annotation_count: value.getattr("ontology_annotation_count")?.extract()?,
            stored_axiom_count: value.getattr("stored_axiom_count")?.extract()?,
            effective_axiom_count: value.getattr("effective_axiom_count")?.extract()?,
            extension_count: value.getattr("extension_count")?.extract()?,
            total_source_bytes: value.getattr("total_source_bytes")?.extract()?,
            source_map_entry_count: value.getattr("source_map_entry_count")?.extract()?,
            origin_entry_count: value.getattr("origin_entry_count")?.extract()?,
            rdf_mapping_report_count: value.getattr("rdf_mapping_report_count")?.extract()?,
            capability_bits: value.getattr("capability_bits")?.extract()?,
            api_version: value.getattr("api_version")?.extract()?,
            model_schema: value.getattr("model_schema")?.extract()?,
            backend: value
                .getattr("backend")?
                .extract::<String>()?
                .into_boxed_str(),
            root_document_key: value
                .getattr("root_document_key")?
                .extract::<String>()?
                .into_boxed_str(),
            owl2_dl_report_summary: optional_summary(value.getattr("owl2_dl_report_summary")?)?,
            owl2_dl_validated: value.getattr("owl2_dl_validated")?.extract()?,
            owl2_dl_conforms: optional_bool(value.getattr("owl2_dl_conforms")?)?,
            owl2_dl_report_sha256: optional_digest(value.getattr("owl2_dl_report_sha256")?)?,
        };
        selected.validate_schema()?;
        Ok(selected)
    }

    fn validate_schema(&self) -> PyResult<()> {
        if self.version != PUBLICATION_VERSION_V2
            || self.ledger_sha256 != PUBLICATION_LEDGER_SHA256_V2
            || self.facade_access_schema_sha256 != FACADE_ACCESS_SCHEMA_SHA256_V2
            || self.auxiliary_codec_schema_sha256 != AUXILIARY_CODEC_SCHEMA_SHA256_V2
        {
            return Err(PyValueError::new_err(
                "native V2 attestation schema constants diverge from Rust",
            ));
        }
        if self.max_facade_row_bytes == 0 || self.backend.as_ref() != "native" {
            return Err(PyValueError::new_err(
                "native V2 attestation has invalid owner invariants",
            ));
        }
        Ok(())
    }

    fn to_python(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let handoff = py.import(HANDOFF_MODULE)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("version", self.version)?;
        set_digest(&kwargs, "ledger_sha256", &self.ledger_sha256)?;
        set_digest(
            &kwargs,
            "metadata_manifest_sha256",
            &self.metadata_manifest_sha256,
        )?;
        set_digest(
            &kwargs,
            "facade_access_schema_sha256",
            &self.facade_access_schema_sha256,
        )?;
        set_digest(
            &kwargs,
            "auxiliary_codec_schema_sha256",
            &self.auxiliary_codec_schema_sha256,
        )?;
        set_digest(&kwargs, "root_table_sha256", &self.root_table_sha256)?;
        set_digest(
            &kwargs,
            "effective_root_table_sha256",
            &self.effective_root_table_sha256,
        )?;
        set_digest(
            &kwargs,
            "fingerprint_inputs_sha256",
            &self.fingerprint_inputs_sha256,
        )?;
        set_digest(
            &kwargs,
            "source_manifest_sha256",
            &self.source_manifest_sha256,
        )?;
        set_digest(
            &kwargs,
            "provenance_manifest_sha256",
            &self.provenance_manifest_sha256,
        )?;
        set_digest(
            &kwargs,
            "effective_origin_manifest_sha256",
            &self.effective_origin_manifest_sha256,
        )?;
        set_digest(
            &kwargs,
            "diagnostics_manifest_sha256",
            &self.diagnostics_manifest_sha256,
        )?;
        set_digest(
            &kwargs,
            "diagnostic_reference_kinds_sha256",
            &self.diagnostic_reference_kinds_sha256,
        )?;
        set_digest(
            &kwargs,
            "facade_cardinality_summary_sha256",
            &self.facade_cardinality_summary_sha256,
        )?;
        set_digest(&kwargs, "load_options_sha256", &self.load_options_sha256)?;
        set_digest(&kwargs, "report_sha256", &self.report_sha256)?;
        kwargs.set_item("max_facade_row_bytes", self.max_facade_row_bytes)?;
        kwargs.set_item("document_count", self.document_count)?;
        kwargs.set_item("import_edge_count", self.import_edge_count)?;
        kwargs.set_item("diagnostic_count", self.diagnostic_count)?;
        kwargs.set_item("ontology_annotation_count", self.ontology_annotation_count)?;
        kwargs.set_item("stored_axiom_count", self.stored_axiom_count)?;
        kwargs.set_item("effective_axiom_count", self.effective_axiom_count)?;
        kwargs.set_item("extension_count", self.extension_count)?;
        kwargs.set_item("total_source_bytes", self.total_source_bytes)?;
        kwargs.set_item("source_map_entry_count", self.source_map_entry_count)?;
        kwargs.set_item("origin_entry_count", self.origin_entry_count)?;
        kwargs.set_item("rdf_mapping_report_count", self.rdf_mapping_report_count)?;
        kwargs.set_item("capability_bits", self.capability_bits)?;
        kwargs.set_item(
            "api_version",
            PyTuple::new(py, [self.api_version.0, self.api_version.1])?,
        )?;
        kwargs.set_item("model_schema", self.model_schema)?;
        kwargs.set_item("backend", &*self.backend)?;
        kwargs.set_item("root_document_key", &*self.root_document_key)?;
        match &self.owl2_dl_report_summary {
            Some(summary) => kwargs.set_item("owl2_dl_report_summary", summary.to_python(py)?)?,
            None => kwargs.set_item("owl2_dl_report_summary", py.None())?,
        }
        kwargs.set_item("owl2_dl_validated", self.owl2_dl_validated)?;
        kwargs.set_item("owl2_dl_conforms", self.owl2_dl_conforms)?;
        match &self.owl2_dl_report_sha256 {
            Some(digest) => set_digest(&kwargs, "owl2_dl_report_sha256", digest)?,
            None => kwargs.set_item("owl2_dl_report_sha256", py.None())?,
        }
        Ok(handoff
            .getattr("NativeSnapshotAttestationV2")?
            .call((), Some(&kwargs))?
            .unbind())
    }
}

impl Owl2DlSummaryV2 {
    fn to_python(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let kwargs = PyDict::new(py);
        kwargs.set_item("structural_values_checked", self.structural_values_checked)?;
        kwargs.set_item("structural_complete", self.structural_complete)?;
        kwargs.set_item("report_complete", self.report_complete)?;
        kwargs.set_item("structural_issue_count", self.structural_issue_count)?;
        kwargs.set_item("issue_count", self.issue_count)?;
        kwargs.set_item("role_property_count", self.role_property_count)?;
        kwargs.set_item("role_hierarchy_count", self.role_hierarchy_count)?;
        kwargs.set_item("role_composite_count", self.role_composite_count)?;
        kwargs.set_item("role_non_simple_count", self.role_non_simple_count)?;
        Ok(py
            .import(HANDOFF_MODULE)?
            .getattr("NativeOWL2DLReportSummaryV2")?
            .call((), Some(&kwargs))?
            .unbind())
    }
}

fn optional_summary(value: Bound<'_, PyAny>) -> PyResult<Option<Owl2DlSummaryV2>> {
    if value.is_none() {
        return Ok(None);
    }
    Ok(Some(Owl2DlSummaryV2 {
        structural_values_checked: value.getattr("structural_values_checked")?.extract()?,
        structural_complete: value.getattr("structural_complete")?.extract()?,
        report_complete: value.getattr("report_complete")?.extract()?,
        structural_issue_count: value.getattr("structural_issue_count")?.extract()?,
        issue_count: value.getattr("issue_count")?.extract()?,
        role_property_count: value.getattr("role_property_count")?.extract()?,
        role_hierarchy_count: value.getattr("role_hierarchy_count")?.extract()?,
        role_composite_count: value.getattr("role_composite_count")?.extract()?,
        role_non_simple_count: value.getattr("role_non_simple_count")?.extract()?,
    }))
}

fn optional_bool(value: Bound<'_, PyAny>) -> PyResult<Option<bool>> {
    if value.is_none() {
        Ok(None)
    } else {
        value.extract().map(Some)
    }
}

fn digest_attr(value: &Bound<'_, PyAny>, name: &str) -> PyResult<Digest> {
    digest_from_python(&value.getattr(name)?)
}

fn digest_from_python(value: &Bound<'_, PyAny>) -> PyResult<Digest> {
    exact_bytes(value, "digest")?
        .try_into()
        .map_err(|_| PyValueError::new_err("native V2 digest must contain exactly 32 bytes"))
}

fn set_digest(mapping: &Bound<'_, PyDict>, name: &str, digest: &Digest) -> PyResult<()> {
    mapping.set_item(name, PyBytes::new(mapping.py(), digest))
}

#[cfg(test)]
mod tests {
    #[cfg(feature = "test-hooks")]
    use std::ffi::CString;
    use std::sync::Barrier;
    use std::thread;

    use super::*;
    use crate::canonical::{entity, iri, Field, Node};
    use crate::limits::Limits;
    use crate::model::NativeComponentBuilder;
    use crate::publication::{TypedFacadeBuilderV2, TypedFacadeTableV2};

    fn typed_frame(value: &[u8]) -> Vec<u8> {
        let mut result = typed_varint(value.len());
        result.extend_from_slice(value);
        result
    }

    fn typed_varint(mut value: usize) -> Vec<u8> {
        let mut result = Vec::new();
        loop {
            let byte = (value & 0x7f) as u8;
            value >>= 7;
            result.push(if value == 0 { byte } else { byte | 0x80 });
            if value == 0 {
                return result;
            }
        }
    }

    fn typed_declaration(value: &str) -> Vec<u8> {
        let mut iri = vec![1, 2];
        iri.extend(typed_frame(value.as_bytes()));
        let mut entity = vec![2, 5];
        entity.extend(typed_frame(b"class"));
        entity.push(1);
        entity.extend(typed_frame(&iri));
        let mut declaration = vec![60, 1];
        declaration.extend(typed_frame(&entity));
        declaration.extend([6, 0]);
        declaration
    }

    fn typed_annotation(value: &str) -> Vec<u8> {
        Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:typed:annotation-property".into()).expect("annotation IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri(value.into()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("annotation")
        .as_bytes()
        .to_vec()
    }

    fn typed_extension() -> Vec<u8> {
        Node::build(
            148,
            vec![
                Field::Set(Vec::new()),
                Field::Set(Vec::new()),
                Field::Set(Vec::new()),
            ],
        )
        .expect("SWRL rule")
        .as_bytes()
        .to_vec()
    }

    fn typed_structural_owner() -> (TypedFacadeStorageV2, Vec<u8>) {
        let canonical = typed_declaration("urn:typed:facade");
        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&limits).expect("component builder");
        let pending = builder
            .intern_canonical(&canonical)
            .expect("canonical declaration");
        let frozen = builder.freeze().expect("component freeze");
        let root = frozen.resolve(pending).expect("declaration root");
        let arena = frozen.into_arena();
        let document = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let closure = TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms);
        let storage = TypedFacadeStorageV2::freeze(
            arena,
            vec![
                TypedFacadeTableV2::new(document, vec![root]),
                TypedFacadeTableV2::new(closure, vec![root]),
            ],
            vec![TypedFacadeTableV2::new(document, vec![root])],
            1,
            limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("typed structural owner");
        (storage, canonical)
    }

    fn coordinate(
        collection: CollectionV2,
        scope: ScopeV2,
        document_ordinal: Option<u64>,
    ) -> CoordinateV2 {
        CoordinateV2 {
            collection,
            scope,
            document_ordinal,
            signature_kind: SignatureKindV2::All,
            include_builtins: true,
        }
    }

    #[test]
    fn typed_coordinate_mapping_is_structural_only_and_preserves_selectors() {
        let kinds = [
            (SignatureKindV2::All, TypedFacadeSignatureKindV2::All),
            (SignatureKindV2::Class, TypedFacadeSignatureKindV2::Class),
            (
                SignatureKindV2::Datatype,
                TypedFacadeSignatureKindV2::Datatype,
            ),
            (
                SignatureKindV2::ObjectProperty,
                TypedFacadeSignatureKindV2::ObjectProperty,
            ),
            (
                SignatureKindV2::DataProperty,
                TypedFacadeSignatureKindV2::DataProperty,
            ),
            (
                SignatureKindV2::AnnotationProperty,
                TypedFacadeSignatureKindV2::AnnotationProperty,
            ),
            (
                SignatureKindV2::NamedIndividual,
                TypedFacadeSignatureKindV2::NamedIndividual,
            ),
        ];
        for (source, expected) in kinds {
            let mapped = CoordinateV2 {
                collection: CollectionV2::Signature,
                scope: ScopeV2::Closure,
                document_ordinal: None,
                signature_kind: source,
                include_builtins: false,
            }
            .typed()
            .expect("structural coordinate");
            assert_eq!(mapped.collection, TypedFacadeCollectionV2::Signature);
            assert_eq!(mapped.scope, TypedFacadeScopeV2::Closure);
            assert_eq!(mapped.signature_kind, expected);
            assert!(!mapped.include_builtins);
        }
        assert!(CoordinateV2 {
            collection: CollectionV2::SourceMapEntries,
            scope: ScopeV2::Document,
            document_ordinal: Some(0),
            signature_kind: SignatureKindV2::All,
            include_builtins: true,
        }
        .typed()
        .is_none());
    }

    #[test]
    fn typed_structural_owner_attaches_without_a_canonical_row_copy() {
        let (typed, canonical) = typed_structural_owner();
        let arena_witness = typed.arena().clone();
        let typed_counters = typed.counters().expect("typed counters");
        let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        attestation.max_facade_row_bytes = canonical.len() as u64;
        let storage = PublicationStorageV2::from_typed_structural(attestation, typed)
            .expect("typed publication");

        assert!(storage.effective_tables.is_empty());
        assert!(storage.raw_document_tables.is_none());
        let attached = storage
            .typed_structural
            .as_ref()
            .expect("typed structural backend");
        assert!(attached.arena().shares_storage_with(&arena_witness));
        let counters = storage.counters.snapshot();
        assert_eq!(counters[RETAINED_DOCUMENT_TABLES], 1);
        assert_eq!(counters[RETAINED_ROW_FIRST + 1], 3);
        assert_eq!(
            counters[RETAINED_COMPONENT_BYTES],
            typed_counters.retained_component_bytes
        );
        assert_eq!(counters[47], 0);
        assert_eq!(counters[48], 0);
        assert_eq!(counters[PUBLICATION_METADATA_RECORDS], 3);
        assert!(counters[RETAINED_OWNER_BYTES] > typed_counters.retained_owner_bytes);
        validate_retained_total(&counters).expect("disjoint retained owner counters");
    }

    #[test]
    fn typed_structural_owner_retains_one_shared_origin_table() {
        let (typed, canonical) = typed_structural_owner();
        let origin = vec![0x42; 96];
        let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        attestation.capability_bits |= 16;
        attestation.origin_entry_count = 1;
        attestation.max_facade_row_bytes = canonical.len().max(origin.len()) as u64;
        let storage = PublicationStorageV2::from_typed_structural_with_origins(
            attestation,
            typed,
            vec![origin.clone()],
            0,
        )
        .expect("typed publication with origins");

        let document = storage.rows(
            coordinate(CollectionV2::OriginEntries, ScopeV2::Document, Some(0)),
            false,
        );
        let closure = storage.rows(
            coordinate(CollectionV2::OriginEntries, ScopeV2::Closure, None),
            false,
        );
        assert_eq!(document.len(), 1);
        assert_eq!(document[0].as_slice(), origin);
        assert!(Arc::ptr_eq(&document[0], &closure[0]));
        let counters = storage.counters.snapshot();
        assert_eq!(counters[RETAINED_ROW_FIRST + 5], 2);
        assert_eq!(counters[RETAINED_ORIGIN_BYTES], origin.len() as u64);
        assert!(counters[RETAINED_METADATA_BYTES] > 0);
        validate_retained_total(&counters).expect("origin owner counters");
    }

    #[test]
    fn typed_structural_owner_rejects_attestation_drift_and_auxiliary_claims() {
        let (typed, canonical) = typed_structural_owner();
        let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        attestation.max_facade_row_bytes = canonical.len() as u64 + 1;
        assert!(PublicationStorageV2::from_typed_structural(attestation, typed).is_err());

        let (typed, canonical) = typed_structural_owner();
        let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        attestation.max_facade_row_bytes = canonical.len() as u64;
        attestation.capability_bits |= 8;
        attestation.source_map_entry_count = 1;
        assert!(PublicationStorageV2::from_typed_structural(attestation, typed).is_err());

        for selected in 0..4 {
            let (typed, canonical) = typed_structural_owner();
            let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
            attestation.max_facade_row_bytes = canonical.len() as u64;
            match selected {
                0 => attestation.ontology_annotation_count = 1,
                1 => attestation.stored_axiom_count = 2,
                2 => attestation.effective_axiom_count = 2,
                3 => attestation.extension_count = 1,
                _ => unreachable!(),
            }
            assert!(PublicationStorageV2::from_typed_structural(attestation, typed).is_err());
        }
    }

    #[test]
    fn typed_structural_counts_follow_raw_owners_and_effective_import_closure() {
        let annotation_a = typed_annotation("urn:typed:annotation-a");
        let annotation_b = typed_annotation("urn:typed:annotation-b");
        let axiom_a = typed_declaration("urn:typed:axiom-a");
        let axiom_b = typed_declaration("urn:typed:axiom-b");
        let extension = typed_extension();
        let limits = Limits::default();
        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("typed builder");
        assert_eq!(
            builder
                .add_document(
                    std::slice::from_ref(&annotation_a),
                    std::slice::from_ref(&axiom_a),
                    &[],
                )
                .expect("root document"),
            0,
        );
        assert_eq!(
            builder
                .add_document(
                    std::slice::from_ref(&annotation_b),
                    std::slice::from_ref(&axiom_b),
                    std::slice::from_ref(&extension),
                )
                .expect("imported document"),
            1,
        );
        let typed = builder
            .freeze(&[vec![0, 1], vec![1]], &[0, 1])
            .expect("import closure");
        let before = typed.counters().expect("pre-count counters");
        let counts = typed.structural_counts().expect("structural counts");
        let after = typed.counters().expect("post-count counters");
        assert_eq!(counts.ontology_annotations, 2);
        assert_eq!(counts.stored_axioms, 2);
        assert_eq!(counts.effective_axioms, 2);
        assert_eq!(counts.extensions, 1);
        assert_eq!(
            after.canonical_encode_requests,
            before.canonical_encode_requests
        );
        assert_eq!(after.publication_structural_rows_copied, 0);
        assert_eq!(after.publication_structural_bytes_copied, 0);

        let mut attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        attestation.document_count = 2;
        attestation.import_edge_count = 1;
        attestation.max_facade_row_bytes = typed.maximum_row_bytes();
        attestation.ontology_annotation_count = 2;
        attestation.stored_axiom_count = 2;
        attestation.effective_axiom_count = 2;
        attestation.extension_count = 1;
        let storage = PublicationStorageV2::from_typed_structural(attestation, typed)
            .expect("count-aligned typed publication");
        let before = storage.counters.snapshot();
        assert_eq!(before[47], 0);
        assert_eq!(before[48], 0);
        assert_eq!(before[ENCODED_VIEW_REQUESTS], 0);
        let columns = storage
            .encoded_structural_columns(
                TypedFacadeScopeV2::Closure,
                None,
                false,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("direct closure columns");
        assert_eq!(columns.counters().root_rows, 5);
        let index = storage
            .retained_axiom_type_index(
                TypedFacadeScopeV2::Closure,
                None,
                false,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("direct closure axiom-type index");
        assert_eq!(index.tags(), [60]);
        assert_eq!(index.category_codes(), [1]);
        assert_eq!(index.postings(), [0, 1]);
        assert_eq!(index.counters().complete_root_encode_calls, 0);
        let after = storage.counters.snapshot();
        assert_eq!(after[ENCODED_VIEW_REQUESTS], 1);
        assert_eq!(&after[84..89], &[0, 1, 0, 0, 0]);
        assert_eq!(after[47], 0);
        assert_eq!(after[48], 0);
    }

    #[test]
    fn retained_counter_mapping_is_exhaustive_and_does_not_shift_at_signature() {
        let expected = [
            (CollectionV2::OntologyAnnotations, Some(18)),
            (CollectionV2::Axioms, Some(19)),
            (CollectionV2::Extensions, Some(20)),
            (CollectionV2::Signature, None),
            (CollectionV2::SourceMapEntries, Some(21)),
            (CollectionV2::SourceMapPrefixes, Some(22)),
            (CollectionV2::OriginEntries, Some(23)),
            (CollectionV2::RdfReportHeader, Some(24)),
            (CollectionV2::RdfUnconsumedTriples, Some(25)),
            (CollectionV2::RdfRuleIds, Some(26)),
            (CollectionV2::RdfDiagnostics, Some(27)),
            (CollectionV2::Owl2DlStructuralIssues, Some(28)),
            (CollectionV2::Owl2DlIssues, Some(29)),
            (CollectionV2::Owl2DlRoleProperties, Some(30)),
            (CollectionV2::Owl2DlRoleHierarchy, Some(31)),
            (CollectionV2::Owl2DlRoleComposite, Some(32)),
            (CollectionV2::Owl2DlRoleNonSimple, Some(33)),
        ];
        for (collection, counter) in expected {
            assert_eq!(collection.retained_row_counter(), counter);
        }
        assert_eq!(COUNTER_NAMES[33], "retained_owl2_dl_role_non_simple_rows");
        assert_eq!(COUNTER_NAMES[34], "retained_component_bytes");
    }

    #[test]
    fn occurrence_counters_match_document_and_closure_tables_while_bytes_are_interned() {
        let mut builder = StorageBuilderV2::new(1 << 20).expect("builder");
        let axiom = b"retained-axiom".as_slice();
        builder
            .add_test_table(
                coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0)),
                &[axiom],
                false,
                1,
            )
            .expect("document");
        builder
            .add_test_table(
                coordinate(CollectionV2::Axioms, ScopeV2::Closure, None),
                &[axiom],
                false,
                2,
            )
            .expect("closure");
        let storage = builder
            .finish(NativeSnapshotAttestationV2::fixture_for_tests())
            .expect("storage");
        let counters = storage.counters.snapshot();
        assert_eq!(counters[19], 2);
        assert_eq!(counters[10], 1);
        assert_eq!(counters[11], axiom.len() as u64);
        assert_eq!(counters[RETAINED_ROOT_BYTES], axiom.len() as u64);
        validate_retained_total(&counters).expect("disjoint retained bytes");
    }

    #[test]
    fn retained_counters_equal_the_owned_layout_after_builder_state_is_dropped() {
        let attestation = NativeSnapshotAttestationV2::fixture_for_tests();
        let dynamic_attestation_bytes =
            attestation.backend.len() + attestation.root_document_key.len();
        let row = b"one-shared-row".as_slice();
        let mut builder = StorageBuilderV2::new(1 << 20).expect("builder");
        for (scope, ordinal, identity) in
            [(ScopeV2::Document, Some(0), 1), (ScopeV2::Closure, None, 2)]
        {
            builder
                .add_test_table(
                    coordinate(CollectionV2::Axioms, scope, ordinal),
                    &[row],
                    false,
                    identity,
                )
                .expect("shared table");
        }
        let storage = builder.finish(attestation).expect("storage");
        let counters = storage.counters.snapshot();
        let expected_owner = arc_sized_allocation_bytes::<PublicationStorageV2>()
            .expect("storage layout")
            + dynamic_attestation_bytes
            + storage.effective_tables.capacity() * size_of::<FacadeTableV2>()
            + storage
                .effective_tables
                .iter()
                .map(|table| table.rows.capacity() * size_of::<Arc<Vec<u8>>>())
                .sum::<usize>()
            + arc_vec_allocation_bytes(row.len()).expect("row layout");

        assert_eq!(counters[RETAINED_OWNER_BYTES], expected_owner as u64);
        assert_eq!(counters[RETAINED_ROOT_BYTES], row.len() as u64);
        assert_eq!(
            counters[RETAINED_METADATA_BYTES],
            (expected_owner - row.len()) as u64
        );
        assert_eq!(Arc::strong_count(&storage.effective_tables[0].rows[0]), 2);
        assert!(Arc::ptr_eq(
            &storage.effective_tables[0].rows[0],
            &storage.effective_tables[1].rows[0]
        ));
        assert!(counters[PEAK_BUILDER_BYTES] >= counters[RETAINED_OWNER_BYTES]);
        validate_retained_total(&counters).expect("exact retained layout model");
    }

    #[test]
    fn explicit_raw_tables_count_only_distinct_python_table_ownership() {
        fn storage(raw_identity: usize) -> Arc<PublicationStorageV2> {
            let mut builder = StorageBuilderV2::new(1 << 20).expect("builder");
            let row = b"axiom".as_slice();
            builder
                .add_test_table(
                    coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0)),
                    &[row],
                    false,
                    11,
                )
                .expect("effective document");
            builder
                .add_test_table(
                    coordinate(CollectionV2::Axioms, ScopeV2::Closure, None),
                    &[row],
                    false,
                    12,
                )
                .expect("effective closure");
            builder
                .add_test_table(
                    coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0)),
                    &[row],
                    true,
                    raw_identity,
                )
                .expect("raw document");
            builder
                .finish(NativeSnapshotAttestationV2::fixture_for_tests())
                .expect("storage")
        }

        let coordinate = coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0));
        let fallback = storage(11);
        let override_storage = storage(13);
        let fallback_counters = fallback.counters.snapshot();
        let override_counters = override_storage.counters.snapshot();

        assert_eq!(
            fallback.raw_document_tables.as_deref().map(<[_]>::len),
            Some(0)
        );
        assert_eq!(
            override_storage
                .raw_document_tables
                .as_deref()
                .map(<[_]>::len),
            Some(1)
        );
        assert_eq!(
            fallback.rows(coordinate, true),
            fallback.rows(coordinate, false)
        );
        assert_eq!(fallback_counters[19], 2);
        assert_eq!(override_counters[19], 3);
        assert_eq!(fallback_counters[10], 1);
        assert_eq!(override_counters[10], 1);
        assert!(fallback_counters[RETAINED_OWNER_BYTES] < override_counters[RETAINED_OWNER_BYTES]);
    }

    #[test]
    fn identical_raw_fallback_does_not_consume_the_coordinate_table_limit() {
        let coordinate = coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0));
        let table = FacadeTableV2 {
            coordinate,
            rows: Vec::new(),
            source_identity: 7,
        };
        let mut builder = StorageBuilderV2::new(u64::MAX).expect("builder");
        builder.effective_tables = vec![table; MAX_FIXTURE_TABLES];

        builder
            .add_test_table(coordinate, &[], true, 7)
            .expect("fallback does not retain an override");

        assert_eq!(builder.effective_tables.len(), MAX_FIXTURE_TABLES);
        assert!(builder.raw_document_tables.is_empty());
        assert!(builder.raw_supplied);
    }

    #[test]
    fn million_root_occurrences_share_one_payload_without_facade_materialization() {
        const ROOT_COUNT: usize = 1_000_000;
        const RETAINED_LIMIT: u64 = 64 * 1024 * 1024;

        // Repeating one already-validated canonical root isolates the native
        // row-reference scaling and interning contract. Canonical uniqueness
        // itself is enforced before the fixture reaches this Rust builder.
        let row = b"root-row".as_slice();
        let roots = vec![row; ROOT_COUNT];
        let mut builder = StorageBuilderV2::new(RETAINED_LIMIT).expect("bounded builder");
        builder
            .add_test_table(
                coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0)),
                &roots,
                false,
                1,
            )
            .expect("million document roots");
        builder
            .add_test_table(
                coordinate(CollectionV2::Axioms, ScopeV2::Closure, None),
                &roots,
                false,
                2,
            )
            .expect("million closure roots");
        let storage = builder
            .finish(NativeSnapshotAttestationV2::fixture_for_tests())
            .expect("bounded million-root storage");
        let counters = storage.counters.snapshot();

        assert_eq!(storage.effective_tables.len(), 2);
        assert_eq!(storage.effective_tables[0].rows.len(), ROOT_COUNT);
        assert_eq!(storage.effective_tables[1].rows.len(), ROOT_COUNT);
        assert!(Arc::ptr_eq(
            &storage.effective_tables[0].rows[0],
            &storage.effective_tables[1].rows[ROOT_COUNT - 1],
        ));
        assert_eq!(counters[10], ROOT_COUNT as u64);
        assert_eq!(counters[11], (ROOT_COUNT * row.len()) as u64);
        assert_eq!(counters[19], (ROOT_COUNT * 2) as u64);
        assert_eq!(counters[RETAINED_ROOT_BYTES], row.len() as u64);
        assert!(counters[RETAINED_OWNER_BYTES] < 40 * 1024 * 1024);
        assert!(counters[PEAK_BUILDER_BYTES] >= counters[RETAINED_OWNER_BYTES]);
        assert!(counters[PEAK_BUILDER_BYTES] <= RETAINED_LIMIT);
        assert_eq!(counters[PEAK_FREEZE_BYTES], counters[PEAK_BUILDER_BYTES]);
        assert_eq!(counters[PAGE_REQUESTS], 0);
        assert_eq!(counters[ROWS_EMITTED], 0);
        assert_eq!(counters[PAYLOAD_BYTES_COPIED], 0);
        validate_retained_total(&counters).expect("million-root byte accounting");
    }

    #[test]
    fn bounded_pages_make_progress_and_allow_only_one_oversized_first_row() {
        let rows: Vec<Arc<Vec<u8>>> = [b"123456".as_slice(), b"abcdef", b"x"]
            .into_iter()
            .map(|row| Arc::new(row.to_vec()))
            .collect();
        assert_eq!(bounded_page_end(&rows, 0, 3, 4).unwrap(), (1, 6));
        assert_eq!(bounded_page_end(&rows, 0, 3, 10).unwrap(), (1, 6));
        assert_eq!(bounded_page_end(&rows, 1, 3, 10).unwrap(), (3, 7));
        assert!(bounded_page_end(&rows, 3, 2, 10).is_err());
        assert!(bounded_page_end(&rows, 0, 3, 0).is_err());
    }

    #[test]
    fn frozen_page_bounds_are_rechecked_below_the_python_dataclass() {
        assert!(validate_page_bounds(1, 1, 1).is_ok());
        assert!(validate_page_bounds(MAX_FACADE_PAGE_ROWS_V2, MAX_FACADE_PAGE_BYTES_V2, 1).is_ok());
        assert!(validate_page_bounds(0, 1, 1).is_err());
        assert!(validate_page_bounds(MAX_FACADE_PAGE_ROWS_V2 + 1, 1, 1).is_err());
        assert!(validate_page_bounds(1, 0, 1).is_err());
        assert!(validate_page_bounds(1, MAX_FACADE_PAGE_BYTES_V2 + 1, 1).is_err());
        assert!(validate_page_bounds(1, 1, 0).is_err());
    }

    #[test]
    fn request_coordinate_and_capability_invariants_are_rechecked_natively() {
        let storage = PublicationStorageV2::fixture_for_tests();
        let valid_document = coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0));
        let valid_closure = coordinate(CollectionV2::Axioms, ScopeV2::Closure, None);
        assert!(storage.validate_request(valid_document, 1024, None).is_ok());
        assert!(storage.validate_request(valid_closure, 1024, None).is_ok());

        let mut selected = valid_document;
        selected.document_ordinal = None;
        assert!(storage.validate_request(selected, 1024, None).is_err());
        selected = valid_closure;
        selected.document_ordinal = Some(0);
        assert!(storage.validate_request(selected, 1024, None).is_err());
        selected = coordinate(CollectionV2::SourceMapEntries, ScopeV2::Closure, None);
        assert!(storage.validate_request(selected, 1024, None).is_err());
        selected = coordinate(
            CollectionV2::Owl2DlStructuralIssues,
            ScopeV2::Document,
            Some(0),
        );
        assert!(storage.validate_request(selected, 1024, None).is_err());
        selected = valid_closure;
        selected.signature_kind = SignatureKindV2::Class;
        assert!(storage.validate_request(selected, 1024, None).is_err());
        selected = valid_closure;
        selected.include_builtins = false;
        assert!(storage.validate_request(selected, 1024, None).is_err());
        selected = coordinate(CollectionV2::SourceMapEntries, ScopeV2::Document, Some(0));
        assert!(storage.validate_request(selected, 1024, None).is_err());
        selected = coordinate(CollectionV2::Owl2DlStructuralIssues, ScopeV2::Closure, None);
        assert!(storage.validate_request(selected, 1024, None).is_err());
    }

    #[test]
    fn digest_ranges_are_exact_group_relative_indexes() {
        let first = [1_u8; 32];
        let second = [2_u8; 32];
        let rows: Vec<Arc<Vec<u8>>> = [
            [&first[..], b"a"].concat(),
            [&first[..], b"b"].concat(),
            [&second[..], b"a"].concat(),
        ]
        .into_iter()
        .map(|row| Arc::new(row.to_vec()))
        .collect();
        assert_eq!(digest_range(&rows, None), (0, 3));
        assert_eq!(digest_range(&rows, Some(&first)), (0, 2));
        assert_eq!(digest_range(&rows, Some(&second)), (2, 3));
        assert_eq!(digest_range(&rows, Some(&[3; 32])), (3, 3));
    }

    #[test]
    fn concurrent_reads_observe_coherent_counter_transactions() {
        let counters = Arc::new(CounterStateV2::new([0; COUNTER_NAMES.len()]));
        let start = Arc::new(Barrier::new(9));
        let finished = Arc::new(AtomicBool::new(false));
        let observer = {
            let selected = Arc::clone(&counters);
            let start = Arc::clone(&start);
            let finished = Arc::clone(&finished);
            thread::spawn(move || {
                start.wait();
                while !finished.load(Ordering::Acquire) {
                    let values = selected.snapshot();
                    assert_eq!(values[PAGE_REQUESTS], values[PAGES_RETURNED]);
                    assert_eq!(values[PAGE_REQUESTS], values[ROWS_EMITTED]);
                    assert_eq!(values[PAYLOAD_BYTES_COPIED], values[PAGE_REQUESTS] * 7);
                    assert_eq!(
                        values[CANONICAL_PAYLOAD_BYTES_COPIED],
                        values[PAYLOAD_BYTES_COPIED]
                    );
                    assert_eq!(values[EMITTED_ROW_FIRST + 1], values[PAGE_REQUESTS]);
                    thread::yield_now();
                }
            })
        };
        let workers: Vec<_> = (0..8)
            .map(|worker| {
                let selected = Arc::clone(&counters);
                let start = Arc::clone(&start);
                thread::spawn(move || {
                    start.wait();
                    for iteration in 0..1_000 {
                        selected
                            .contains((worker + iteration) % 2 == 0)
                            .expect("counter update");
                        selected
                            .page(CollectionV2::Axioms, 1, 7)
                            .expect("coherent page counters");
                    }
                })
            })
            .collect();
        for worker in workers {
            worker.join().expect("worker");
        }
        finished.store(true, Ordering::Release);
        observer.join().expect("observer");
        let values = counters.snapshot();
        assert_eq!(values[CONTAINS_REQUESTS], 8_000);
        assert_eq!(values[CONTAINS_HITS], 4_000);
        assert_eq!(values[PAGE_REQUESTS], 8_000);
        assert_eq!(values[PAGES_RETURNED], 8_000);
        assert_eq!(values[ROWS_EMITTED], 8_000);
        assert_eq!(values[PAYLOAD_BYTES_COPIED], 56_000);
        assert_eq!(values[CANONICAL_PAYLOAD_BYTES_COPIED], 56_000);
        assert_eq!(values[EMITTED_ROW_FIRST + 1], 8_000);
    }

    #[test]
    fn process_epoch_reset_preserves_frozen_gauges_and_clears_runtime_events() {
        let mut initial = [0; COUNTER_NAMES.len()];
        initial[RETAINED_DOCUMENT_TABLES] = 3;
        for value in &mut initial[FACADE_CACHE_CURRENT_BYTES + 1..] {
            *value = 9;
        }
        let counters = CounterStateV2::new(initial);
        counters.contains(true).expect("contains");
        counters.close(true).expect("close");
        let current = std::process::id();
        let child = current
            .checked_add(1)
            .filter(|value| *value != 0)
            .unwrap_or(1);
        let values = counters.snapshot_for_process(child);
        assert_eq!(values[RETAINED_DOCUMENT_TABLES], 3);
        assert_eq!(values[CONTAINS_REQUESTS], 0);
        assert_eq!(values[CLOSE_REQUESTS], 0);
        assert!(values[FACADE_CACHE_CURRENT_BYTES + 1..]
            .iter()
            .all(|value| *value == 0));
        assert_eq!(values[FORK_REINITIALIZATIONS], 1);
    }

    #[test]
    fn fork_epoch_reset_recovers_an_inherited_locked_counter_gate() {
        let counters = CounterStateV2::new([0; COUNTER_NAMES.len()]);
        counters.contains(true).expect("parent event");
        counters.gate.store(true, Ordering::Release);
        let current = std::process::id();
        let child = current
            .checked_add(1)
            .filter(|value| *value != 0)
            .unwrap_or(1);

        let values = counters.snapshot_for_process(child);

        assert_eq!(values[CONTAINS_REQUESTS], 0);
        assert_eq!(values[CONTAINS_HITS], 0);
        assert_eq!(values[FORK_REINITIALIZATIONS], 1);
        assert!(!counters.gate.load(Ordering::Acquire));
    }

    #[test]
    fn overflowing_counter_transaction_publishes_no_partial_updates() {
        let mut initial = [0; COUNTER_NAMES.len()];
        initial[PAGE_REQUESTS] = u64::MAX;
        let counters = CounterStateV2::new(initial);

        assert!(counters.page(CollectionV2::Axioms, 1, 7).is_err());

        let values = counters.snapshot();
        assert_eq!(values[PAGE_REQUESTS], u64::MAX);
        assert_eq!(values[PAGES_RETURNED], 0);
        assert_eq!(values[ROWS_EMITTED], 0);
        assert_eq!(values[PAYLOAD_BYTES_COPIED], 0);
        assert_eq!(values[CANONICAL_PAYLOAD_BYTES_COPIED], 0);
        assert_eq!(values[EMITTED_ROW_FIRST + 1], 0);
    }

    #[test]
    fn allocation_budget_rejects_growth_before_state_mutation() {
        let mut budget = AllocationBudget::new(8).expect("budget");
        budget.claim(8).expect("within budget");
        assert_eq!(budget.retained(), 8);
        assert_eq!(budget.temporary, 0);
        assert!(budget.claim(1).is_err());
        assert_eq!(budget.retained(), 8);
        assert_eq!(budget.temporary, 0);
        assert!(AllocationBudget::new(0).is_err());
    }

    #[test]
    fn table_budget_failure_publishes_no_partial_coordinate() {
        let maximum = u64::try_from(size_of::<FacadeTableV2>()).expect("table size");
        let mut builder = StorageBuilderV2::new(maximum).expect("builder");

        let result = builder.add_test_table(
            coordinate(CollectionV2::Axioms, ScopeV2::Document, Some(0)),
            &[b"row"],
            false,
            1,
        );

        assert!(result.is_err());
        assert!(builder.effective_tables.is_empty());
        assert!(builder.raw_document_tables.is_empty());
        assert!(builder.interner.is_empty());
    }

    #[cfg(feature = "test-hooks")]
    #[test]
    fn temporary_work_and_retained_growth_share_one_live_memory_limit() {
        let mut retained = AllocationBudget::new(100).expect("retained budget");
        retained.claim(40).expect("initial retained bytes");
        let mut temporary = TemporaryBudget::default();

        temporary
            .claim(&mut retained, 30)
            .expect("temporary work within limit");
        assert_eq!(temporary.used, 30);
        assert_eq!(retained.temporary, 30);
        assert_eq!(retained.maximum, 100);
        retained.claim(30).expect("combined live bytes at limit");
        assert_eq!(retained.retained(), 70);
        assert!(temporary.claim(&mut retained, 1).is_err());
        assert_eq!(temporary.used, 30);
        assert_eq!(retained.temporary, 30);
        assert_eq!(retained.maximum, 100);

        temporary
            .release(&mut retained)
            .expect("temporary accounting release");
        assert_eq!(retained.temporary, 0);
        assert_eq!(retained.maximum, 100);
        assert_eq!(retained.peak, 100);
    }

    #[cfg(feature = "test-hooks")]
    #[test]
    fn contains_request_reuses_one_validated_decode_and_rejects_post_init_mutation() {
        Python::initialize();
        Python::attach(|py| -> PyResult<()> {
            let source_root = concat!(env!("CARGO_MANIFEST_DIR"), "/../src");
            py.import("sys")?
                .getattr("path")?
                .call_method1("insert", (0, source_root))?;
            let globals = PyDict::new(py);
            let setup = CString::new(
                r#"
import pyowl_core.backends.native_handoff_v2 as handoff
from pyowl_core.model import IRI, Class, Declaration, canonical_bytes
original_decode = handoff.decode_canonical
decode_calls = 0
def counted_decode(*args, **kwargs):
    global decode_calls
    decode_calls += 1
    return original_decode(*args, **kwargs)
handoff.decode_canonical = counted_decode
canonical = canonical_bytes(Declaration(Class(IRI("urn:contains:bounded"))))
request = handoff.NativeFacadeContainsRequestV2(
    collection=handoff.NativeFacadeCollectionV2.AXIOMS,
    scope=handoff.NativeFacadeScopeV2.CLOSURE,
    document_ordinal=None,
    canonical=canonical,
    max_row_bytes=len(canonical),
)
"#,
            )
            .expect("static Python setup");
            py.run(setup.as_c_str(), Some(&globals), None)?;
            let request = globals
                .get_item("request")?
                .ok_or_else(|| PyRuntimeError::new_err("missing contains request"))?;
            let row_bound: u64 = globals
                .get_item("canonical")?
                .ok_or_else(|| PyRuntimeError::new_err("missing canonical row"))?
                .len()?
                .try_into()
                .map_err(|_| PyRuntimeError::new_err("canonical row length exceeds u64"))?;

            let validated = ContainsRequestV2::from_python(&request, row_bound)?;
            assert_eq!(validated.canonical.len() as u64, row_bound);
            assert_eq!(
                globals
                    .get_item("decode_calls")?
                    .unwrap()
                    .extract::<u64>()?,
                1
            );

            let mutate = CString::new(
                r#"
object.__setattr__(request, "canonical", canonical[:-1] + bytes([canonical[-1] ^ 1]))
"#,
            )
            .expect("static Python mutation");
            py.run(mutate.as_c_str(), Some(&globals), None)?;
            let error = ContainsRequestV2::from_python(&request, row_bound)
                .expect_err("mutated exact request must fail closed");
            assert!(error
                .to_string()
                .contains("diverges from its validated bytes"));
            assert_eq!(
                globals
                    .get_item("decode_calls")?
                    .unwrap()
                    .extract::<u64>()?,
                1
            );

            let wrong_bound = ContainsRequestV2::from_python(&request, row_bound + 1)
                .expect_err("row-bound mismatch must precede canonical work");
            assert!(wrong_bound.to_string().contains("row bound"));
            globals.get_item("handoff")?.unwrap().setattr(
                "decode_canonical",
                globals.get_item("original_decode")?.unwrap(),
            )?;
            Ok(())
        })
        .expect("contains trust-boundary evidence");
    }

    #[test]
    fn native_limit_errors_map_to_python_memory_errors() {
        Python::initialize();
        Python::attach(|py| {
            let error = native_error_to_python(NativeError::limit("bounded allocation"));
            assert!(error.is_instance_of::<PyMemoryError>(py));
        });
    }

    #[test]
    fn embedded_schema_hashes_match_the_frozen_v2_vectors() {
        assert_eq!(PUBLICATION_VERSION_V2, 2);
        assert_eq!(
            PUBLICATION_LEDGER_SHA256_V2,
            super::PUBLICATION_LEDGER_SHA256_V2
        );
        assert_ne!(FACADE_ACCESS_SCHEMA_SHA256_V2, [0; 32]);
        assert_ne!(AUXILIARY_CODEC_SCHEMA_SHA256_V2, [0; 32]);
        NativeSnapshotAttestationV2::fixture_for_tests()
            .validate_schema()
            .expect("schema");
    }
}
