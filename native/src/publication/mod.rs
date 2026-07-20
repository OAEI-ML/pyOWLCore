//! Versioned retained native snapshot publication and ownership.

mod codec;
mod facade_v2;
#[cfg(any(test, feature = "test-hooks"))]
mod fixture;
mod handle;
mod records;
mod typed_builder_v2;
mod typed_v2;

use std::collections::HashSet;
use std::mem::size_of;
use std::sync::Arc;

use crate::error::{NativeError, NativeResult};
use crate::model::{validate_iri, Category};

pub(crate) use facade_v2::{PublicationStorageV2, AUXILIARY_CODEC_SCHEMA_SHA256_V2};
#[allow(unused_imports)]
pub(crate) use handle::{register_native_handle_types, NativeDocumentHandle, NativeSnapshotHandle};
#[allow(unused_imports)]
pub(crate) use records::{
    ApiVersion, BackendPreferenceV1, DeadlineSecondsV1, DetectionBasisV1, DiagnosticScalarV1,
    DiagnosticSeverityV1, DiagnosticV1, Digest, DigestKindV1, DocumentFormatV1, DocumentMembersV1,
    DocumentProvenanceV1, DocumentPublicationV1, FingerprintV1, ImportDocumentStatusV1,
    ImportDocumentV1, ImportEdgeStatusV1, ImportEdgeV1, ImportManifestV1, ImportPolicyV1,
    LoadOptionsV1, LoadReportV1, NativeSnapshotAttestationV1, OntologyIdV1, ParseLimitsV1,
    PositiveIntegerV1, PublicationCountersV1, PublicationDraftV1,
};
#[allow(unused_imports)]
pub(crate) use typed_builder_v2::TypedFacadeBuilderV2;
#[allow(unused_imports)]
pub(crate) use typed_v2::{
    TypedFacadeCollectionV2, TypedFacadeCoordinateV2, TypedFacadeCounterSnapshotV2,
    TypedFacadePageRequestV2, TypedFacadePageV2, TypedFacadeScopeV2, TypedFacadeSignatureKindV2,
    TypedFacadeStorageV2, TypedFacadeTableV2,
};

pub(crate) const PUBLICATION_VERSION_V1: u8 = 1;
pub(crate) const PUBLICATION_LEDGER_SHA256_V1: [u8; 32] = [
    0x8e, 0x2c, 0xf6, 0x76, 0xd9, 0x3f, 0xb5, 0xec, 0x39, 0x85, 0xea, 0x9b, 0xbe, 0x1a, 0x44, 0x9d,
    0x8a, 0xdf, 0xb8, 0xf2, 0xee, 0xbe, 0x0b, 0x07, 0xca, 0xae, 0xbf, 0x22, 0x4b, 0x1b, 0xb4, 0x6d,
];

#[cfg(feature = "test-hooks")]
pub(crate) fn encoded_fixture_handle_v2() -> NativeResult<NativeSnapshotHandle> {
    Ok(NativeSnapshotHandle::from_storage_v2(
        facade_v2::PublicationStorageV2::encoded_fixture_for_tests()?,
    ))
}

pub(crate) fn typed_structural_handle_v2(
    attestation: &pyo3::Bound<'_, pyo3::types::PyAny>,
    storage: TypedFacadeStorageV2,
    origin_rows: Option<Vec<Vec<u8>>>,
    parser_bytes: u64,
) -> pyo3::PyResult<NativeSnapshotHandle> {
    let attestation = facade_v2::NativeSnapshotAttestationV2::from_python(attestation)?;
    let publication = facade_v2::PublicationStorageV2::from_typed_structural_with_optional_origins(
        attestation,
        storage,
        origin_rows,
        parser_bytes,
    )
    .map_err(crate::python_error)?;
    Ok(NativeSnapshotHandle::from_storage_v2(publication))
}

#[cfg(feature = "test-hooks")]
#[allow(clippy::too_many_arguments)]
pub(crate) fn fixture_handle_v2(
    py: pyo3::Python<'_>,
    attestation: &pyo3::Bound<'_, pyo3::types::PyAny>,
    collections: &pyo3::Bound<'_, pyo3::types::PyAny>,
    documents: &pyo3::Bound<'_, pyo3::types::PyAny>,
    report: &pyo3::Bound<'_, pyo3::types::PyAny>,
    root_document_key: &pyo3::Bound<'_, pyo3::types::PyAny>,
    load_options: &pyo3::Bound<'_, pyo3::types::PyAny>,
    capability_bits: &pyo3::Bound<'_, pyo3::types::PyAny>,
    fingerprint_evidence: &pyo3::Bound<'_, pyo3::types::PyAny>,
    fingerprint_preimages: &pyo3::Bound<'_, pyo3::types::PyAny>,
    facade_cardinality_summary: &pyo3::Bound<'_, pyo3::types::PyAny>,
    owl2_dl_report_summary: Option<&pyo3::Bound<'_, pyo3::types::PyAny>>,
    raw_document_collections: Option<&pyo3::Bound<'_, pyo3::types::PyAny>>,
    max_retained_bytes: u64,
) -> pyo3::PyResult<NativeSnapshotHandle> {
    let storage = facade_v2::PublicationStorageV2::from_validated_python(
        py,
        attestation,
        collections,
        documents,
        report,
        root_document_key,
        load_options,
        capability_bits,
        fingerprint_evidence,
        fingerprint_preimages,
        facade_cardinality_summary,
        owl2_dl_report_summary,
        raw_document_collections,
        max_retained_bytes,
    )?;
    Ok(NativeSnapshotHandle::from_storage_v2(storage))
}

#[cfg(feature = "test-hooks")]
pub(crate) fn unique_axiom_fixture_handle_v2(
    attestation: &pyo3::Bound<'_, pyo3::types::PyAny>,
    row_count: u64,
    max_retained_bytes: u64,
) -> pyo3::PyResult<NativeSnapshotHandle> {
    let storage = facade_v2::PublicationStorageV2::unique_axiom_fixture_for_tests(
        attestation,
        row_count,
        max_retained_bytes,
    )?;
    Ok(NativeSnapshotHandle::from_storage_v2(storage))
}

const MAX_DIAGNOSTIC_DETAILS: usize = 64;
const MAX_DIAGNOSTIC_IMPORT_CHAIN: usize = 128;
const MAX_DIAGNOSTICS_PER_SEQUENCE: usize = 10_000;
const MAX_DIRECT_IMPORTS_PER_DOCUMENT: usize = 10_000_000;
const MAX_DOCUMENT_KEY_BYTES: usize = 256;
const MAX_DOCUMENTS: usize = 1_000_000;
const MAX_IMPORT_EDGES: usize = 100_000_000;
const MAX_IRI_BYTES: usize = 1_048_576;
const MAX_METADATA_STRING_BYTES: usize = 4_096;
const MAX_TIMING_NAME_BYTES: usize = 64;
const MAX_TIMING_ROWS: usize = 64;
const MAX_TOTAL_DIAGNOSTICS: u64 = 1_000_000;
const CAPABILITY_RETAINED_STORAGE: u64 = 1;
const CAPABILITY_LAZY_SCALARS: u64 = 2;
const CAPABILITY_DOCUMENT_SCOPES: u64 = 4;
const CAPABILITY_SOURCE_MAP: u64 = 8;
const CAPABILITY_ORIGIN_INDEX: u64 = 16;
const CAPABILITY_RDF_MAPPING_REPORT: u64 = 32;
const REQUIRED_CAPABILITIES: u64 = 7;
const KNOWN_CAPABILITIES: u64 = 63;

#[cfg(feature = "test-hooks")]
pub(crate) fn fixture_handle_v1() -> NativeResult<NativeSnapshotHandle> {
    Ok(fixture::publication()?.into_handle())
}

#[derive(Debug)]
pub(crate) struct PublicationStorageV1 {
    arena: crate::model::NativeArena,
    documents: Arc<[DocumentPublicationV1]>,
    document_members: Arc<[DocumentMembersV1]>,
    import_manifest: Arc<ImportManifestV1>,
    root_document_key: Arc<str>,
    load_options: Arc<LoadOptionsV1>,
    diagnostics: Arc<[DiagnosticV1]>,
    report: Arc<LoadReportV1>,
    capability_bits: u64,
    root_table_sha256: Digest,
    fingerprint_inputs_sha256: Digest,
    source_manifest_sha256: Digest,
    provenance_manifest_sha256: Digest,
    attestation: NativeSnapshotAttestationV1,
    counters: PublicationCountersV1,
}

impl PublicationStorageV1 {
    pub(crate) const fn attestation(&self) -> &NativeSnapshotAttestationV1 {
        &self.attestation
    }

    pub(crate) const fn counters(&self) -> &PublicationCountersV1 {
        &self.counters
    }

    pub(crate) const fn arena(&self) -> &crate::model::NativeArena {
        &self.arena
    }

    pub(crate) fn document_count(&self) -> usize {
        self.documents.len()
    }
}

#[derive(Debug)]
pub(crate) struct NativeSnapshotPublicationV1 {
    pub(crate) version: u8,
    pub(crate) ledger_sha256: Digest,
    pub(crate) handle: NativeSnapshotHandle,
    pub(crate) documents: Arc<[DocumentPublicationV1]>,
    pub(crate) import_manifest: Arc<ImportManifestV1>,
    pub(crate) root_document_key: Arc<str>,
    pub(crate) load_options: Arc<LoadOptionsV1>,
    pub(crate) diagnostics: Arc<[DiagnosticV1]>,
    pub(crate) report: Arc<LoadReportV1>,
    pub(crate) capability_bits: u64,
    pub(crate) root_table_sha256: Digest,
    pub(crate) fingerprint_inputs_sha256: Digest,
    pub(crate) source_manifest_sha256: Digest,
    pub(crate) provenance_manifest_sha256: Digest,
    storage: Arc<PublicationStorageV1>,
}

impl NativeSnapshotPublicationV1 {
    pub(crate) fn storage(&self) -> &Arc<PublicationStorageV1> {
        &self.storage
    }

    pub(crate) const fn handle(&self) -> &NativeSnapshotHandle {
        &self.handle
    }

    pub(crate) fn into_handle(self) -> NativeSnapshotHandle {
        self.handle
    }
}

impl PublicationDraftV1 {
    pub(crate) fn freeze(self) -> NativeResult<NativeSnapshotPublicationV1> {
        let aggregates = validate_publication(&self)?;
        let diagnostics_manifest_sha256 =
            codec::diagnostics_digest(&self.diagnostics, &self.documents, &self.import_manifest)?;
        let load_options_sha256 = codec::load_options_digest(&self.load_options)?;
        let report_sha256 = codec::report_digest(&self.report)?;
        let attestation = NativeSnapshotAttestationV1 {
            version: PUBLICATION_VERSION_V1,
            ledger_sha256: PUBLICATION_LEDGER_SHA256_V1,
            root_table_sha256: self.root_table_sha256,
            fingerprint_inputs_sha256: self.fingerprint_inputs_sha256,
            source_manifest_sha256: self.source_manifest_sha256,
            provenance_manifest_sha256: self.provenance_manifest_sha256,
            diagnostics_manifest_sha256,
            load_options_sha256,
            report_sha256,
            document_count: usize_u64(self.documents.len())?,
            import_edge_count: usize_u64(self.import_manifest.edges.len())?,
            diagnostic_count: aggregates.diagnostics,
            ontology_annotation_count: aggregates.annotations,
            stored_axiom_count: aggregates.axioms,
            effective_axiom_count: self.report.effective_axiom_count,
            extension_count: aggregates.extensions,
            total_source_bytes: aggregates.source_bytes,
            source_map_entry_count: aggregates.source_map_entries,
            origin_entry_count: aggregates.origin_entries,
            rdf_mapping_report_count: aggregates.rdf_reports,
            capability_bits: self.capability_bits,
            api_version: self.report.api_version,
            model_schema: self.report.model_schema,
            backend: self.report.backend.clone(),
            root_document_key: self.root_document_key.clone(),
            owl2_dl_validated: self.report.owl2_dl_validated,
            owl2_dl_conforms: self.report.owl2_dl_conforms,
            owl2_dl_report_sha256: self.report.owl2_dl_report_sha256,
        };
        // Encoding is checked during freeze so a handle can never own an
        // attestation that fails its exact v1 scalar codec.
        codec::attestation_digest(&attestation)?;
        let counters = publication_counters(&self)?;
        let documents: Arc<[DocumentPublicationV1]> = Arc::from(self.documents);
        let document_members: Arc<[DocumentMembersV1]> = Arc::from(self.document_members);
        let import_manifest = Arc::new(self.import_manifest);
        let root_document_key: Arc<str> = Arc::from(self.root_document_key);
        let load_options = Arc::new(self.load_options);
        let diagnostics: Arc<[DiagnosticV1]> = Arc::from(self.diagnostics);
        let report = Arc::new(self.report);
        let storage = Arc::new(PublicationStorageV1 {
            arena: self.arena,
            documents: Arc::clone(&documents),
            document_members,
            import_manifest: Arc::clone(&import_manifest),
            root_document_key: Arc::clone(&root_document_key),
            load_options: Arc::clone(&load_options),
            diagnostics: Arc::clone(&diagnostics),
            report: Arc::clone(&report),
            capability_bits: self.capability_bits,
            root_table_sha256: self.root_table_sha256,
            fingerprint_inputs_sha256: self.fingerprint_inputs_sha256,
            source_manifest_sha256: self.source_manifest_sha256,
            provenance_manifest_sha256: self.provenance_manifest_sha256,
            attestation,
            counters,
        });
        let handle = NativeSnapshotHandle::from_storage(Arc::clone(&storage));
        Ok(NativeSnapshotPublicationV1 {
            version: PUBLICATION_VERSION_V1,
            ledger_sha256: PUBLICATION_LEDGER_SHA256_V1,
            handle,
            documents,
            import_manifest,
            root_document_key,
            load_options,
            diagnostics,
            report,
            capability_bits: storage.capability_bits,
            root_table_sha256: storage.root_table_sha256,
            fingerprint_inputs_sha256: storage.fingerprint_inputs_sha256,
            source_manifest_sha256: storage.source_manifest_sha256,
            provenance_manifest_sha256: storage.provenance_manifest_sha256,
            storage,
        })
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct Aggregates {
    diagnostics: u64,
    annotations: u64,
    axioms: u64,
    extensions: u64,
    source_bytes: u64,
    source_map_entries: u64,
    origin_entries: u64,
    rdf_reports: u64,
}

fn validate_publication(draft: &PublicationDraftV1) -> NativeResult<Aggregates> {
    if draft.documents.is_empty() {
        return Err(NativeError::protocol(
            "native publication requires at least one document",
        ));
    }
    check_count(
        draft.documents.len(),
        MAX_DOCUMENTS,
        &draft.load_options.limits.max_documents,
        "native publication document count exceeds limits",
    )?;
    check_count(
        draft.import_manifest.edges.len(),
        MAX_IMPORT_EDGES,
        &draft.load_options.limits.max_axioms,
        "native publication import edge count exceeds limits",
    )?;
    if draft.documents.len() != draft.document_members.len()
        || draft.documents.len() != draft.import_manifest.documents.len()
    {
        return Err(NativeError::protocol(
            "native publication document tables are not aligned",
        ));
    }
    if draft.load_options.backend != BackendPreferenceV1::Native {
        return Err(NativeError::protocol(
            "native publication requires forced native backend",
        ));
    }
    validate_load_options(&draft.load_options)?;
    if draft.load_options.imports != draft.import_manifest.policy
        || draft.load_options.offline != draft.import_manifest.offline
    {
        return Err(NativeError::protocol(
            "native publication options and import manifest diverge",
        ));
    }
    validate_document_key(&draft.root_document_key)?;
    validate_report(&draft.report, &draft.load_options)?;
    validate_diagnostics(&draft.diagnostics)?;
    let mut aggregates = Aggregates {
        diagnostics: usize_u64(draft.diagnostics.len())?,
        ..Aggregates::default()
    };
    let mut keys = HashSet::new();
    keys.try_reserve(draft.documents.len())
        .map_err(|_| NativeError::limit("native publication document-key allocation failed"))?;
    let mut previous_key: Option<&str> = None;
    let mut roots = 0_u64;
    let mut edge_index = 0_usize;
    for ((document, members), record) in draft
        .documents
        .iter()
        .zip(&draft.document_members)
        .zip(&draft.import_manifest.documents)
    {
        validate_document(document)?;
        validate_import_document(record)?;
        if previous_key
            .is_some_and(|previous| previous.as_bytes() >= document.document_key.as_bytes())
        {
            return Err(NativeError::protocol(
                "native publication documents are not strictly ordered",
            ));
        }
        previous_key = Some(&document.document_key);
        if !keys.insert(document.document_key.as_ref()) {
            return Err(NativeError::protocol(
                "native publication document keys are not unique",
            ));
        }
        if !document_record_matches(document, record) {
            return Err(NativeError::protocol(
                "native publication document metadata diverges",
            ));
        }
        if record.status == ImportDocumentStatusV1::Root {
            roots = checked_add(roots, 1, "native publication root count overflow")?;
            if record.document_key != draft.root_document_key {
                return Err(NativeError::protocol(
                    "native publication root document diverges",
                ));
            }
        }
        validate_members(&draft.arena, document, members)?;
        aggregates.annotations = checked_add(
            aggregates.annotations,
            document.ontology_annotation_count,
            "native publication annotation count overflow",
        )?;
        aggregates.axioms = checked_add(
            aggregates.axioms,
            document.axiom_count,
            "native publication axiom count overflow",
        )?;
        aggregates.extensions = checked_add(
            aggregates.extensions,
            document.extension_count,
            "native publication extension count overflow",
        )?;
        aggregates.source_bytes = checked_add(
            aggregates.source_bytes,
            document.provenance.byte_length,
            "native publication source byte count overflow",
        )?;
        aggregates.source_map_entries = checked_add(
            aggregates.source_map_entries,
            document.source_map_entry_count,
            "native publication source-map count overflow",
        )?;
        aggregates.origin_entries = checked_add(
            aggregates.origin_entries,
            document.origin_entry_count,
            "native publication origin count overflow",
        )?;
        aggregates.diagnostics = checked_add(
            aggregates.diagnostics,
            usize_u64(document.diagnostics.len())?,
            "native publication diagnostic count overflow",
        )?;
        if document.rdf_mapping_report_sha256.is_some() {
            aggregates.rdf_reports = checked_add(
                aggregates.rdf_reports,
                1,
                "native publication RDF report count overflow",
            )?;
        }
        let mut import_index = 0_usize;
        while draft
            .import_manifest
            .edges
            .get(edge_index)
            .is_some_and(|edge| edge.importing_document_key == document.document_key)
        {
            let edge = &draft.import_manifest.edges[edge_index];
            validate_import_edge(edge)?;
            if document.direct_imports.get(import_index).map(Box::as_ref)
                != Some(edge.import_iri.as_ref())
            {
                return Err(NativeError::protocol(
                    "native publication direct imports and edges diverge",
                ));
            }
            if edge.diagnostic.is_some() {
                aggregates.diagnostics = checked_add(
                    aggregates.diagnostics,
                    1,
                    "native publication diagnostic count overflow",
                )?;
            }
            import_index += 1;
            edge_index += 1;
        }
        if import_index != document.direct_imports.len() {
            return Err(NativeError::protocol(
                "native publication direct imports and edges diverge",
            ));
        }
    }
    if roots != 1 || edge_index != draft.import_manifest.edges.len() {
        return Err(NativeError::protocol(
            "native publication root or import edge alignment is invalid",
        ));
    }
    for edge in &draft.import_manifest.edges {
        if !keys.contains(edge.importing_document_key.as_ref())
            || edge
                .resolved_document_key
                .as_deref()
                .is_some_and(|target| !keys.contains(target))
        {
            return Err(NativeError::protocol(
                "native publication import edge references an unknown document",
            ));
        }
    }
    if !draft
        .load_options
        .limits
        .max_diagnostics
        .allows(aggregates.diagnostics)
        || aggregates.diagnostics > MAX_TOTAL_DIAGNOSTICS
    {
        return Err(NativeError::limit(
            "native publication diagnostic count exceeds limits",
        ));
    }
    validate_report_alignment(draft, aggregates)?;
    validate_capabilities(draft, aggregates)?;
    Ok(aggregates)
}

fn validate_document(document: &DocumentPublicationV1) -> NativeResult<()> {
    validate_document_key(&document.document_key)?;
    validate_ontology_id(&document.ontology_id)?;
    validate_optional_iri(document.document_iri.as_deref())?;
    if document.direct_imports.len() > MAX_DIRECT_IMPORTS_PER_DOCUMENT {
        return Err(NativeError::limit(
            "native publication direct import count exceeds limits",
        ));
    }
    let mut previous: Option<Vec<u8>> = None;
    for iri in &document.direct_imports {
        validate_iri_string(iri)?;
        let current = canonical_iri_bytes(iri)?;
        if previous.as_ref().is_some_and(|value| current <= *value) {
            return Err(NativeError::protocol(
                "native publication direct imports are not strictly ordered",
            ));
        }
        previous = Some(current);
    }
    validate_provenance(&document.provenance)?;
    validate_fingerprint(&document.document_fingerprint)?;
    validate_diagnostics(&document.diagnostics)?;
    if document.rdf_mapping_conformant.is_some() != document.rdf_mapping_report_sha256.is_some() {
        return Err(NativeError::protocol(
            "native publication RDF mapping claims are incomplete",
        ));
    }
    Ok(())
}

fn validate_provenance(value: &DocumentProvenanceV1) -> NativeResult<()> {
    validate_optional_iri(value.document_iri.as_deref())?;
    for text in [
        value.acquisition_locator.as_deref(),
        value.media_type.as_deref(),
    ]
    .into_iter()
    .flatten()
    {
        validate_metadata_string(text, MAX_METADATA_STRING_BYTES)?;
    }
    validate_metadata_string(&value.parser, MAX_METADATA_STRING_BYTES)?;
    validate_metadata_string(&value.backend, MAX_METADATA_STRING_BYTES)
}

fn validate_import_document(value: &ImportDocumentV1) -> NativeResult<()> {
    validate_document_key(&value.document_key)?;
    validate_ontology_id(&value.ontology_id)?;
    validate_optional_iri(value.document_iri.as_deref())?;
    validate_fingerprint(&value.document_fingerprint)
}

fn validate_import_edge(value: &ImportEdgeV1) -> NativeResult<()> {
    validate_document_key(&value.importing_document_key)?;
    validate_iri_string(&value.import_iri)?;
    if value.status == ImportEdgeStatusV1::Resolved {
        if value.resolved_document_key.is_none() {
            return Err(NativeError::protocol(
                "native resolved import edge has no target",
            ));
        }
    } else if value.resolved_document_key.is_some() {
        return Err(NativeError::protocol(
            "native unresolved import edge has a target",
        ));
    }
    if let Some(target) = &value.resolved_document_key {
        validate_document_key(target)?;
    }
    for text in [
        value.resolver_name.as_deref(),
        value.sanitized_locator.as_deref(),
    ]
    .into_iter()
    .flatten()
    {
        validate_metadata_string(text, MAX_METADATA_STRING_BYTES)?;
    }
    if let Some(diagnostic) = &value.diagnostic {
        validate_diagnostic(diagnostic)?;
    }
    Ok(())
}

fn validate_report(report: &LoadReportV1, options: &LoadOptionsV1) -> NativeResult<()> {
    if report.backend.as_ref() != "native" {
        return Err(NativeError::protocol(
            "native publication report backend is not native",
        ));
    }
    if report.timings.len() > MAX_TIMING_ROWS {
        return Err(NativeError::limit(
            "native publication report timing count exceeds limits",
        ));
    }
    let mut previous: Option<&str> = None;
    for (name, seconds) in &report.timings {
        validate_metadata_string(name, MAX_TIMING_NAME_BYTES)?;
        if previous.is_some_and(|value| value.as_bytes() >= name.as_bytes()) {
            return Err(NativeError::protocol(
                "native publication report timings are not strictly ordered",
            ));
        }
        if !seconds.is_finite() || *seconds < 0.0 {
            return Err(NativeError::protocol(
                "native publication report timing is invalid",
            ));
        }
        previous = Some(name);
    }
    for fingerprint in [
        &report.structural_fingerprint,
        &report.logical_fingerprint,
        &report.signature_fingerprint,
    ] {
        validate_fingerprint(fingerprint)?;
    }
    if report.owl2_dl_validated {
        if report.owl2_dl_conforms.is_none() || report.owl2_dl_report_sha256.is_none() {
            return Err(NativeError::protocol(
                "native validated OWL report metadata is incomplete",
            ));
        }
    } else if report.owl2_dl_conforms.is_some() || report.owl2_dl_report_sha256.is_some() {
        return Err(NativeError::protocol(
            "native unvalidated OWL report publishes result metadata",
        ));
    }
    if report.owl2_dl_validated != options.validate_owl2_dl {
        return Err(NativeError::protocol(
            "native OWL validation report and options diverge",
        ));
    }
    Ok(())
}

fn validate_load_options(options: &LoadOptionsV1) -> NativeResult<()> {
    if let Some(DeadlineSecondsV1::Float(value)) = options.limits.deadline_seconds.as_ref() {
        if !value.is_finite() || *value <= 0.0 {
            return Err(NativeError::protocol(
                "native publication deadline must be positive and finite",
            ));
        }
    }
    Ok(())
}

fn validate_report_alignment(
    draft: &PublicationDraftV1,
    aggregates: Aggregates,
) -> NativeResult<()> {
    if draft.report.document_count != usize_u64(draft.documents.len())?
        || draft.report.total_source_bytes != aggregates.source_bytes
        || draft.report.effective_axiom_count > aggregates.axioms
        || !draft
            .load_options
            .limits
            .max_total_source_bytes
            .allows(draft.report.total_source_bytes)
        || !draft
            .load_options
            .limits
            .max_axioms
            .allows(draft.report.effective_axiom_count)
        || draft.report.acquisition_cache_hits > draft.report.resolution_attempts
        || draft.report.document_cache_hits > draft.report.resolution_attempts
        || !draft
            .load_options
            .limits
            .max_resolver_attempts
            .allows(draft.report.resolution_attempts)
    {
        return Err(NativeError::protocol(
            "native publication report claims diverge",
        ));
    }
    Ok(())
}

fn validate_capabilities(draft: &PublicationDraftV1, aggregates: Aggregates) -> NativeResult<()> {
    if draft.capability_bits & !KNOWN_CAPABILITIES != 0
        || draft.capability_bits & REQUIRED_CAPABILITIES != REQUIRED_CAPABILITIES
    {
        return Err(NativeError::protocol(
            "native publication capability bits are invalid",
        ));
    }
    let source = draft.capability_bits & CAPABILITY_SOURCE_MAP != 0;
    let origin = draft.capability_bits & CAPABILITY_ORIGIN_INDEX != 0;
    let rdf = draft.capability_bits & CAPABILITY_RDF_MAPPING_REPORT != 0;
    if source != draft.load_options.preserve_source_map
        || origin != draft.load_options.collect_provenance
        || rdf != (aggregates.rdf_reports != 0)
        || (aggregates.source_map_entries != 0 && !source)
        || (aggregates.origin_entries != 0 && !origin)
        || !draft
            .load_options
            .limits
            .max_source_map_entries
            .allows(aggregates.source_map_entries)
        || !draft
            .load_options
            .limits
            .max_origin_entries
            .allows(aggregates.origin_entries)
    {
        return Err(NativeError::protocol(
            "native publication capability and table claims diverge",
        ));
    }
    debug_assert_eq!(
        REQUIRED_CAPABILITIES,
        CAPABILITY_RETAINED_STORAGE | CAPABILITY_LAZY_SCALARS | CAPABILITY_DOCUMENT_SCOPES
    );
    Ok(())
}

fn validate_members(
    arena: &crate::model::NativeArena,
    document: &DocumentPublicationV1,
    members: &DocumentMembersV1,
) -> NativeResult<()> {
    if usize_u64(members.ontology_annotations.len())? != document.ontology_annotation_count
        || usize_u64(members.axioms.len())? != document.axiom_count
        || usize_u64(members.extensions.len())? != document.extension_count
    {
        return Err(NativeError::protocol(
            "native publication member counts diverge",
        ));
    }
    validate_member_rows(
        arena,
        &members.ontology_annotations,
        Some(Category::Annotation),
    )?;
    validate_member_rows(arena, &members.axioms, Some(Category::Axiom))?;
    validate_member_rows(arena, &members.extensions, None)
}

fn validate_member_rows(
    arena: &crate::model::NativeArena,
    rows: &[crate::model::CanonicalRowId],
    expected: Option<Category>,
) -> NativeResult<()> {
    let mut previous = None;
    for identifier in rows {
        if previous.is_some_and(|value| value >= identifier.raw()) {
            return Err(NativeError::protocol(
                "native publication members are not strictly ordered",
            ));
        }
        let row = arena.canonical_row(*identifier)?;
        if expected.is_some_and(|category| row.category() != category) {
            return Err(NativeError::protocol(
                "native publication member has the wrong model category",
            ));
        }
        previous = Some(identifier.raw());
    }
    Ok(())
}

fn validate_diagnostics(values: &[DiagnosticV1]) -> NativeResult<()> {
    if values.len() > MAX_DIAGNOSTICS_PER_SEQUENCE {
        return Err(NativeError::limit(
            "native publication diagnostic sequence exceeds limits",
        ));
    }
    for value in values {
        validate_diagnostic(value)?;
    }
    Ok(())
}

fn validate_diagnostic(value: &DiagnosticV1) -> NativeResult<()> {
    if value.code.is_empty()
        || !value.code.as_bytes()[0].is_ascii_uppercase()
        || !value
            .code
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
    {
        return Err(NativeError::protocol(
            "native publication diagnostic code is invalid",
        ));
    }
    validate_metadata_string(&value.code, MAX_METADATA_STRING_BYTES)?;
    validate_metadata_string(&value.message, MAX_METADATA_STRING_BYTES)?;
    validate_optional_iri(value.document_iri.as_deref())?;
    if value
        .byte_start
        .zip(value.byte_end)
        .is_some_and(|(start, end)| end < start)
        || value
            .line_start
            .zip(value.line_end)
            .is_some_and(|(start, end)| {
                (end, value.column_end.unwrap_or(1)) < (start, value.column_start.unwrap_or(1))
            })
        || [
            value.line_start,
            value.column_start,
            value.line_end,
            value.column_end,
        ]
        .into_iter()
        .flatten()
        .any(|coordinate| coordinate == 0)
    {
        return Err(NativeError::protocol(
            "native publication diagnostic span is invalid",
        ));
    }
    if value.import_chain.len() > MAX_DIAGNOSTIC_IMPORT_CHAIN
        || value.details.len() > MAX_DIAGNOSTIC_DETAILS
    {
        return Err(NativeError::limit(
            "native publication diagnostic metadata exceeds limits",
        ));
    }
    for iri in &value.import_chain {
        validate_iri_string(iri)?;
    }
    for (index, (key, scalar)) in value.details.iter().enumerate() {
        validate_metadata_string(key, MAX_METADATA_STRING_BYTES)?;
        if value.details[..index]
            .iter()
            .any(|(previous, _)| previous == key)
        {
            return Err(NativeError::protocol(
                "native publication diagnostic detail keys are not unique",
            ));
        }
        if let DiagnosticScalarV1::Text(text) = scalar {
            validate_metadata_string(text, MAX_METADATA_STRING_BYTES)?;
        }
    }
    Ok(())
}

fn validate_ontology_id(value: &OntologyIdV1) -> NativeResult<()> {
    validate_optional_iri(value.ontology_iri.as_deref())?;
    validate_optional_iri(value.version_iri.as_deref())?;
    if value.version_iri.is_some() && value.ontology_iri.is_none() {
        return Err(NativeError::protocol(
            "native publication version IRI has no ontology IRI",
        ));
    }
    Ok(())
}

fn validate_fingerprint(_value: &FingerprintV1) -> NativeResult<()> {
    // Algorithm and digest width are represented by closed Rust types;
    // PositiveIntegerV1 validates schema positivity at construction.
    Ok(())
}

fn validate_optional_iri(value: Option<&str>) -> NativeResult<()> {
    value.map_or(Ok(()), validate_iri_string)
}

fn validate_iri_string(value: &str) -> NativeResult<()> {
    validate_metadata_string(value, MAX_IRI_BYTES)?;
    validate_iri(value)
}

fn validate_document_key(value: &str) -> NativeResult<()> {
    validate_metadata_string(value, MAX_DOCUMENT_KEY_BYTES)
}

fn validate_metadata_string(value: &str, maximum: usize) -> NativeResult<()> {
    if value.is_empty() {
        return Err(NativeError::protocol(
            "native publication metadata string is empty",
        ));
    }
    if value.len() > maximum {
        return Err(NativeError::limit(
            "native publication metadata string exceeds limits",
        ));
    }
    Ok(())
}

fn document_record_matches(document: &DocumentPublicationV1, record: &ImportDocumentV1) -> bool {
    document.document_key == record.document_key
        && document.ontology_id == record.ontology_id
        && document.document_iri == record.document_iri
        && document.provenance.source_sha256 == record.source_sha256
        && document.document_fingerprint == record.document_fingerprint
        && document.provenance.format == record.format
}

fn canonical_iri_bytes(value: &str) -> NativeResult<Vec<u8>> {
    let mut result = Vec::new();
    result
        .try_reserve_exact(value.len().saturating_add(12))
        .map_err(|_| NativeError::limit("native publication IRI key allocation failed"))?;
    result.extend_from_slice(&[1, 2]);
    encode_varint(
        u64::try_from(value.len())
            .map_err(|_| NativeError::limit("native publication IRI length exceeds u64"))?,
        &mut result,
    );
    result.extend_from_slice(value.as_bytes());
    Ok(result)
}

fn encode_varint(mut value: u64, target: &mut Vec<u8>) {
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        target.push(if value == 0 { byte } else { byte | 0x80 });
        if value == 0 {
            return;
        }
    }
}

fn check_count(
    value: usize,
    hard: usize,
    configured: &PositiveIntegerV1,
    message: &'static str,
) -> NativeResult<()> {
    let value_u64 = usize_u64(value)?;
    if value > hard || !configured.allows(value_u64) {
        return Err(NativeError::limit(message));
    }
    Ok(())
}

fn checked_add(left: u64, right: u64, message: &'static str) -> NativeResult<u64> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit(message))
}

fn usize_u64(value: usize) -> NativeResult<u64> {
    u64::try_from(value).map_err(|_| NativeError::limit("native publication count exceeds u64"))
}

fn publication_counters(draft: &PublicationDraftV1) -> NativeResult<PublicationCountersV1> {
    let mut retained = 0_u64;
    let mut records = 2_u64; // envelope + manifest
    let mut membership = 0_u64;
    for (document, members) in draft.documents.iter().zip(&draft.document_members) {
        records = checked_add(records, 2, "native publication record counter overflow")?;
        retained = add_bytes(retained, document.document_key.len())?;
        retained = add_bytes(
            retained,
            document.direct_imports.iter().map(|v| v.len()).sum(),
        )?;
        retained = add_bytes(
            retained,
            document.diagnostics.len() * size_of::<DiagnosticV1>(),
        )?;
        membership = checked_add(
            membership,
            usize_u64(
                members.ontology_annotations.len()
                    + members.axioms.len()
                    + members.extensions.len(),
            )?,
            "native publication membership counter overflow",
        )?;
    }
    records = checked_add(
        records,
        usize_u64(draft.import_manifest.edges.len() + draft.diagnostics.len())?,
        "native publication record counter overflow",
    )?;
    retained = add_bytes(
        retained,
        draft.import_manifest.edges.len() * size_of::<ImportEdgeV1>(),
    )?;
    retained = add_bytes(
        retained,
        draft.diagnostics.len() * size_of::<DiagnosticV1>(),
    )?;
    Ok(PublicationCountersV1 {
        retained_metadata_bytes: retained,
        metadata_records: records,
        arena_rows_copied: 0,
        membership_rows: membership,
    })
}

fn add_bytes(total: u64, value: usize) -> NativeResult<u64> {
    checked_add(
        total,
        usize_u64(value)?,
        "native publication byte counter overflow",
    )
}

#[cfg(test)]
mod tests;
