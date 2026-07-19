//! Scalar-only records frozen by the native snapshot publication handoff.

use crate::error::{NativeError, NativeResult};
use crate::model::{CanonicalRowId, NativeArena};

pub(crate) type Digest = [u8; 32];

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PositiveIntegerV1(Box<str>);

impl PositiveIntegerV1 {
    pub(crate) fn from_decimal(value: &str) -> NativeResult<Self> {
        if value.len() > 4_096 {
            return Err(NativeError::limit(
                "native publication integer exceeds digit limits",
            ));
        }
        if value.is_empty()
            || value == "0"
            || value.starts_with('0')
            || !value.bytes().all(|byte| byte.is_ascii_digit())
        {
            return Err(NativeError::protocol(
                "native publication integer is not canonical positive decimal",
            ));
        }
        let mut owned = String::new();
        owned
            .try_reserve_exact(value.len())
            .map_err(|_| NativeError::limit("native publication integer allocation failed"))?;
        owned.push_str(value);
        Ok(Self(owned.into_boxed_str()))
    }

    pub(crate) fn from_u64(value: u64) -> NativeResult<Self> {
        if value == 0 {
            return Err(NativeError::protocol(
                "native publication integer must be positive",
            ));
        }
        Self::from_decimal(&value.to_string())
    }

    pub(crate) fn decimal(&self) -> &str {
        &self.0
    }

    pub(crate) fn allows(&self, observed: u64) -> bool {
        let observed = observed.to_string();
        self.0.len() > observed.len()
            || (self.0.len() == observed.len() && self.0.as_bytes() >= observed.as_bytes())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct ApiVersion(pub(crate) u32, pub(crate) u32);

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct OntologyIdV1 {
    pub(crate) ontology_iri: Option<Box<str>>,
    pub(crate) version_iri: Option<Box<str>>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct FingerprintV1 {
    pub(crate) schema: PositiveIntegerV1,
    pub(crate) digest: Digest,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DiagnosticSeverityV1 {
    Info,
    Warning,
    Error,
}

impl DiagnosticSeverityV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Info => "info",
            Self::Warning => "warning",
            Self::Error => "error",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum DiagnosticScalarV1 {
    Text(Box<str>),
    Integer(i64),
    Boolean(bool),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DiagnosticV1 {
    pub(crate) code: Box<str>,
    pub(crate) severity: DiagnosticSeverityV1,
    pub(crate) message: Box<str>,
    pub(crate) document_iri: Option<Box<str>>,
    pub(crate) byte_start: Option<u64>,
    pub(crate) byte_end: Option<u64>,
    pub(crate) line_start: Option<u64>,
    pub(crate) column_start: Option<u64>,
    pub(crate) line_end: Option<u64>,
    pub(crate) column_end: Option<u64>,
    pub(crate) import_chain: Box<[Box<str>]>,
    pub(crate) details: Box<[(Box<str>, DiagnosticScalarV1)]>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DigestKindV1 {
    ExactBytes,
    NormalizedText,
}

impl DigestKindV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::ExactBytes => "exact-bytes",
            Self::NormalizedText => "normalized-text",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DocumentFormatV1 {
    RdfXml,
    Turtle,
    OwlXml,
    Functional,
}

impl DocumentFormatV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::RdfXml => "rdfxml",
            Self::Turtle => "turtle",
            Self::OwlXml => "owlxml",
            Self::Functional => "functional",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DetectionBasisV1 {
    Explicit,
    MediaType,
    Content,
    Extension,
}

impl DetectionBasisV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Explicit => "explicit",
            Self::MediaType => "media-type",
            Self::Content => "content",
            Self::Extension => "extension",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DocumentProvenanceV1 {
    pub(crate) source_sha256: Digest,
    pub(crate) digest_kind: DigestKindV1,
    pub(crate) byte_length: u64,
    pub(crate) decoded_codepoint_length: u64,
    pub(crate) document_iri: Option<Box<str>>,
    pub(crate) acquisition_locator: Option<Box<str>>,
    pub(crate) format: DocumentFormatV1,
    pub(crate) detection_basis: DetectionBasisV1,
    pub(crate) media_type: Option<Box<str>>,
    pub(crate) expected_sha256: Option<Digest>,
    pub(crate) parser: Box<str>,
    pub(crate) backend: Box<str>,
    pub(crate) api_version: ApiVersion,
    pub(crate) model_schema: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ImportDocumentStatusV1 {
    Root,
    Resolved,
}

impl ImportDocumentStatusV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Root => "root",
            Self::Resolved => "resolved",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ImportDocumentV1 {
    pub(crate) document_key: Box<str>,
    pub(crate) ontology_id: OntologyIdV1,
    pub(crate) document_iri: Option<Box<str>>,
    pub(crate) source_sha256: Digest,
    pub(crate) document_fingerprint: FingerprintV1,
    pub(crate) format: DocumentFormatV1,
    pub(crate) status: ImportDocumentStatusV1,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ImportEdgeStatusV1 {
    Resolved,
    Unresolved,
    Ignored,
    Denied,
    Failed,
}

impl ImportEdgeStatusV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Resolved => "resolved",
            Self::Unresolved => "unresolved",
            Self::Ignored => "ignored",
            Self::Denied => "denied",
            Self::Failed => "failed",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ImportEdgeV1 {
    pub(crate) importing_document_key: Box<str>,
    pub(crate) import_iri: Box<str>,
    pub(crate) status: ImportEdgeStatusV1,
    pub(crate) resolved_document_key: Option<Box<str>>,
    pub(crate) resolver_name: Option<Box<str>>,
    pub(crate) sanitized_locator: Option<Box<str>>,
    pub(crate) diagnostic: Option<DiagnosticV1>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ImportPolicyV1 {
    Ignore,
    RecordUnresolved,
    ResolveLocal,
    ResolveStrict,
}

impl ImportPolicyV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Ignore => "ignore",
            Self::RecordUnresolved => "record_unresolved",
            Self::ResolveLocal => "resolve_local",
            Self::ResolveStrict => "resolve_strict",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ImportManifestV1 {
    pub(crate) policy: ImportPolicyV1,
    pub(crate) offline: bool,
    pub(crate) resolver_configuration_fingerprint: Digest,
    pub(crate) documents: Box<[ImportDocumentV1]>,
    pub(crate) edges: Box<[ImportEdgeV1]>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DocumentPublicationV1 {
    pub(crate) document_key: Box<str>,
    pub(crate) ontology_id: OntologyIdV1,
    pub(crate) document_iri: Option<Box<str>>,
    pub(crate) direct_imports: Box<[Box<str>]>,
    pub(crate) provenance: DocumentProvenanceV1,
    pub(crate) document_fingerprint: FingerprintV1,
    pub(crate) diagnostics: Box<[DiagnosticV1]>,
    pub(crate) ontology_annotation_count: u64,
    pub(crate) axiom_count: u64,
    pub(crate) extension_count: u64,
    pub(crate) source_map_entry_count: u64,
    pub(crate) origin_entry_count: u64,
    pub(crate) rdf_mapping_conformant: Option<bool>,
    pub(crate) rdf_mapping_report_sha256: Option<Digest>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum BackendPreferenceV1 {
    Auto,
    Python,
    Native,
}

impl BackendPreferenceV1 {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Python => "python",
            Self::Native => "native",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) enum DeadlineSecondsV1 {
    Integer(PositiveIntegerV1),
    Float(f64),
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct ParseLimitsV1 {
    pub(crate) max_source_bytes: PositiveIntegerV1,
    pub(crate) max_documents: PositiveIntegerV1,
    pub(crate) max_total_source_bytes: PositiveIntegerV1,
    pub(crate) max_axioms: PositiveIntegerV1,
    pub(crate) max_terms: PositiveIntegerV1,
    pub(crate) max_nesting_depth: PositiveIntegerV1,
    pub(crate) max_rdf_list_length: PositiveIntegerV1,
    pub(crate) max_literal_bytes: PositiveIntegerV1,
    pub(crate) max_iri_bytes: PositiveIntegerV1,
    pub(crate) max_prefixes: PositiveIntegerV1,
    pub(crate) max_import_depth: PositiveIntegerV1,
    pub(crate) max_redirects: PositiveIntegerV1,
    pub(crate) max_diagnostics: PositiveIntegerV1,
    pub(crate) max_memory_bytes: Option<PositiveIntegerV1>,
    pub(crate) deadline_seconds: Option<DeadlineSecondsV1>,
    pub(crate) max_triples: PositiveIntegerV1,
    pub(crate) max_strings: PositiveIntegerV1,
    pub(crate) max_annotations: PositiveIntegerV1,
    pub(crate) max_rule_atoms: PositiveIntegerV1,
    pub(crate) max_sequence_arity: PositiveIntegerV1,
    pub(crate) max_catalog_rewrites: PositiveIntegerV1,
    pub(crate) max_resolver_attempts: PositiveIntegerV1,
    pub(crate) max_concurrent_fetches: PositiveIntegerV1,
    pub(crate) max_source_map_entries: PositiveIntegerV1,
    pub(crate) max_origin_entries: PositiveIntegerV1,
    pub(crate) max_overlay_depth: PositiveIntegerV1,
    pub(crate) max_delta_entries: PositiveIntegerV1,
    pub(crate) max_composite_members: PositiveIntegerV1,
    pub(crate) max_index_rows: PositiveIntegerV1,
    pub(crate) max_index_bytes: PositiveIntegerV1,
    pub(crate) max_wire_rows: PositiveIntegerV1,
    pub(crate) max_wire_bytes: PositiveIntegerV1,
    pub(crate) max_temporary_bytes: PositiveIntegerV1,
    pub(crate) max_disk_cache_bytes: PositiveIntegerV1,
    pub(crate) max_decompressed_bytes: PositiveIntegerV1,
    pub(crate) max_canonical_work: PositiveIntegerV1,
    pub(crate) cancellation_check_interval: PositiveIntegerV1,
}

impl Default for ParseLimitsV1 {
    fn default() -> Self {
        let positive =
            |value| PositiveIntegerV1::from_u64(value).expect("positive default publication limit");
        Self {
            max_source_bytes: positive(2 * 1024_u64.pow(3)),
            max_documents: positive(1_000),
            max_total_source_bytes: positive(8 * 1024_u64.pow(3)),
            max_axioms: positive(100_000_000),
            max_terms: positive(500_000_000),
            max_nesting_depth: positive(512),
            max_rdf_list_length: positive(10_000_000),
            max_literal_bytes: positive(64 * 1024_u64.pow(2)),
            max_iri_bytes: positive(1024 * 1024),
            max_prefixes: positive(1_000_000),
            max_import_depth: positive(128),
            max_redirects: positive(5),
            max_diagnostics: positive(10_000),
            max_memory_bytes: None,
            deadline_seconds: None,
            max_triples: positive(100_000_000),
            max_strings: positive(500_000_000),
            max_annotations: positive(100_000_000),
            max_rule_atoms: positive(10_000_000),
            max_sequence_arity: positive(10_000_000),
            max_catalog_rewrites: positive(128),
            max_resolver_attempts: positive(10_000),
            max_concurrent_fetches: positive(8),
            max_source_map_entries: positive(100_000_000),
            max_origin_entries: positive(100_000_000),
            max_overlay_depth: positive(32),
            max_delta_entries: positive(10_000_000),
            max_composite_members: positive(1_024),
            max_index_rows: positive(500_000_000),
            max_index_bytes: positive(16 * 1024_u64.pow(3)),
            max_wire_rows: positive(500_000_000),
            max_wire_bytes: positive(16 * 1024_u64.pow(3)),
            max_temporary_bytes: positive(16 * 1024_u64.pow(3)),
            max_disk_cache_bytes: positive(64 * 1024_u64.pow(3)),
            max_decompressed_bytes: positive(8 * 1024_u64.pow(3)),
            max_canonical_work: positive(1_000_000_000),
            cancellation_check_interval: positive(4_096),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct LoadOptionsV1 {
    pub(crate) format: Option<DocumentFormatV1>,
    pub(crate) imports: ImportPolicyV1,
    pub(crate) backend: BackendPreferenceV1,
    pub(crate) limits: ParseLimitsV1,
    pub(crate) offline: bool,
    pub(crate) preserve_source_map: bool,
    pub(crate) collect_provenance: bool,
    pub(crate) validate_owl2_dl: bool,
    pub(crate) deterministic: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct LoadReportV1 {
    pub(crate) backend: Box<str>,
    pub(crate) api_version: ApiVersion,
    pub(crate) model_schema: u32,
    pub(crate) document_count: u64,
    pub(crate) total_source_bytes: u64,
    pub(crate) effective_axiom_count: u64,
    pub(crate) resolution_attempts: u64,
    pub(crate) acquisition_cache_hits: u64,
    pub(crate) document_cache_hits: u64,
    pub(crate) timings: Box<[(Box<str>, f64)]>,
    pub(crate) structural_fingerprint: FingerprintV1,
    pub(crate) logical_fingerprint: FingerprintV1,
    pub(crate) signature_fingerprint: FingerprintV1,
    pub(crate) owl2_dl_validated: bool,
    pub(crate) owl2_dl_conforms: Option<bool>,
    pub(crate) owl2_dl_report_sha256: Option<Digest>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct NativeSnapshotAttestationV1 {
    pub(crate) version: u8,
    pub(crate) ledger_sha256: Digest,
    pub(crate) root_table_sha256: Digest,
    pub(crate) fingerprint_inputs_sha256: Digest,
    pub(crate) source_manifest_sha256: Digest,
    pub(crate) provenance_manifest_sha256: Digest,
    pub(crate) diagnostics_manifest_sha256: Digest,
    pub(crate) load_options_sha256: Digest,
    pub(crate) report_sha256: Digest,
    pub(crate) document_count: u64,
    pub(crate) import_edge_count: u64,
    pub(crate) diagnostic_count: u64,
    pub(crate) ontology_annotation_count: u64,
    pub(crate) stored_axiom_count: u64,
    pub(crate) effective_axiom_count: u64,
    pub(crate) extension_count: u64,
    pub(crate) total_source_bytes: u64,
    pub(crate) source_map_entry_count: u64,
    pub(crate) origin_entry_count: u64,
    pub(crate) rdf_mapping_report_count: u64,
    pub(crate) capability_bits: u64,
    pub(crate) api_version: ApiVersion,
    pub(crate) model_schema: u32,
    pub(crate) backend: Box<str>,
    pub(crate) root_document_key: Box<str>,
    pub(crate) owl2_dl_validated: bool,
    pub(crate) owl2_dl_conforms: Option<bool>,
    pub(crate) owl2_dl_report_sha256: Option<Digest>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DocumentMembersV1 {
    pub(crate) ontology_annotations: Box<[CanonicalRowId]>,
    pub(crate) axioms: Box<[CanonicalRowId]>,
    pub(crate) extensions: Box<[CanonicalRowId]>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct PublicationCountersV1 {
    pub(crate) retained_metadata_bytes: u64,
    pub(crate) metadata_records: u64,
    pub(crate) arena_rows_copied: u64,
    pub(crate) membership_rows: u64,
}

#[derive(Debug)]
pub(crate) struct PublicationDraftV1 {
    pub(crate) arena: NativeArena,
    pub(crate) documents: Box<[DocumentPublicationV1]>,
    pub(crate) document_members: Box<[DocumentMembersV1]>,
    pub(crate) import_manifest: ImportManifestV1,
    pub(crate) root_document_key: Box<str>,
    pub(crate) load_options: LoadOptionsV1,
    pub(crate) diagnostics: Box<[DiagnosticV1]>,
    pub(crate) report: LoadReportV1,
    pub(crate) capability_bits: u64,
    pub(crate) root_table_sha256: Digest,
    pub(crate) fingerprint_inputs_sha256: Digest,
    pub(crate) source_manifest_sha256: Digest,
    pub(crate) provenance_manifest_sha256: Digest,
}
