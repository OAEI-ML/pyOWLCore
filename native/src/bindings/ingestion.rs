//! WP16-owned native ingestion registration seam.
//!
//! WP15 intentionally publishes no successor ingestion capability. WP16 may
//! add functions/classes and feature names in this module without editing the
//! shared module registry.

#[allow(dead_code)]
#[path = "../ingestion/mod.rs"]
pub(crate) mod engine;

use pyo3::buffer::PyBuffer;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyModule, PyString, PyTuple};
use std::time::Instant;

use crate::cancel::Guard;
use crate::error::NativeError;
use crate::error::NativeResult;
use crate::limits::{LimitKey, Limits};
use crate::publication::{NativeSnapshotHandle, TypedFacadeBuilderV2, TypedFacadeStorageV2};
use crate::session::Session;

pub(super) const FEATURES: &[&str] = &[];

#[pyclass(
    module = "pyowl_core._native",
    name = "_NativeParsedStructuralStorageV2",
    skip_from_py_object
)]
struct NativeParsedStructuralStorageV2 {
    storage: Option<TypedFacadeStorageV2>,
    metadata: Option<crate::parse::RetainedParseMetadataV2>,
    prepared: Option<crate::parse::PreparedRetainedPublicationV2>,
    prepared_summary: Option<Vec<u8>>,
    limits: Limits,
    parser_bytes: u64,
}

type RetainedParseBindingResult = (
    Py<PyBytes>,
    NativeParsedStructuralStorageV2,
    (u64, u64, u64, u64),
);

type RetainedRdfXmlParseBindingResult = (
    Py<PyBytes>,
    NativeParsedStructuralStorageV2,
    (u64, u64, u64, u64, u64),
);

pub(super) fn register(_py: Python<'_>, _module: &Bound<'_, PyModule>) -> PyResult<()> {
    _module.add_class::<NativeParsedStructuralStorageV2>()?;
    _module.add_function(wrap_pyfunction!(_retain_structural_snapshot_v2, _module)?)?;
    _module.add_function(wrap_pyfunction!(_parse_functional_retained_v2, _module)?)?;
    _module.add_function(wrap_pyfunction!(_parse_rdfxml_retained_v2, _module)?)?;
    #[cfg(feature = "test-hooks")]
    _module.add_function(wrap_pyfunction!(
        _rdfxml_retained_bridge_allocation_probe_v2,
        _module
    )?)?;
    #[cfg(feature = "test-hooks")]
    _module.add_function(wrap_pyfunction!(
        _functional_retained_bridge_allocation_probe_v2,
        _module
    )?)?;
    _module.add_function(wrap_pyfunction!(
        _prepare_parsed_structural_snapshot_v2,
        _module
    )?)?;
    #[cfg(feature = "test-hooks")]
    _module.add_function(wrap_pyfunction!(
        _prepare_parsed_structural_bridge_allocation_probe_v2,
        _module
    )?)?;
    _module.add_function(wrap_pyfunction!(
        _finalize_parsed_structural_snapshot_v2,
        _module
    )?)?;
    #[cfg(feature = "test-hooks")]
    _module.add_function(wrap_pyfunction!(_ingest_rdfxml_slice_v1, _module)?)?;
    Ok(())
}

/// Parse one complete RDF/XML document and retain its canonical structural
/// roots in the typed V2 owner. This production seam stays private and absent
/// from the capability ledger until the full forced-native format matrix is
/// complete.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    source,
    document_iri,
    config,
    collect_provenance,
    allow_partial_rdf_mapping,
    require_empty_imports,
    cancel=None
))]
fn _parse_rdfxml_retained_v2<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    document_iri: Option<&Bound<'py, PyAny>>,
    config: &Bound<'py, PyAny>,
    collect_provenance: bool,
    allow_partial_rdf_mapping: bool,
    require_empty_imports: bool,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<RetainedRdfXmlParseBindingResult> {
    let mut allocations = crate::BridgeAllocationProbe::disabled();
    parse_rdfxml_retained_v2_with_allocations(
        py,
        source,
        document_iri,
        config,
        collect_provenance,
        allow_partial_rdf_mapping,
        require_empty_imports,
        cancel,
        &mut allocations,
    )
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    source,
    document_iri,
    config,
    collect_provenance,
    allow_partial_rdf_mapping,
    require_empty_imports,
    fail_after=None
))]
fn _rdfxml_retained_bridge_allocation_probe_v2<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    document_iri: Option<&Bound<'py, PyAny>>,
    config: &Bound<'py, PyAny>,
    collect_provenance: bool,
    allow_partial_rdf_mapping: bool,
    require_empty_imports: bool,
    fail_after: Option<u64>,
) -> PyResult<(Py<PyBytes>, u64)> {
    let mut allocations = crate::BridgeAllocationProbe::configured(
        fail_after,
        "injected native RDF/XML retained bridge allocation failure",
    );
    let (encoded, _storage, _phases) = parse_rdfxml_retained_v2_with_allocations(
        py,
        source,
        document_iri,
        config,
        collect_provenance,
        allow_partial_rdf_mapping,
        require_empty_imports,
        None,
        &mut allocations,
    )?;
    Ok((encoded, allocations.count()))
}

#[allow(clippy::too_many_arguments)]
fn parse_rdfxml_retained_v2_with_allocations<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    document_iri: Option<&Bound<'py, PyAny>>,
    config: &Bound<'py, PyAny>,
    collect_provenance: bool,
    allow_partial_rdf_mapping: bool,
    require_empty_imports: bool,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
    allocations: &mut crate::BridgeAllocationProbe,
) -> PyResult<RetainedRdfXmlParseBindingResult> {
    if allow_partial_rdf_mapping {
        return Err(crate::python_error(NativeError::new(
            "NATIVE_RDFXML_RETAINED_UNSUPPORTED",
            "native retained RDF/XML publication does not support partial mapping",
        )));
    }
    let limits = crate::limits_from_python_with_allocations(config, allocations)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let document_iri = owned_document_iri_with_allocations(py, document_iri, &limits, allocations)?;
    let document_iri_size = document_iri.as_ref().map_or(0, String::len);
    let owned = owned_source_with_allocations(
        py,
        source,
        document_iri_size,
        &limits,
        &cancellation,
        allocations,
    )?;
    let input_size = owned.len();
    let accounted_input =
        accounted_input_bytes(input_size, document_iri_size).map_err(crate::python_error)?;
    let (outcome, parser_bytes) = crate::run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
            interrupt.clone(),
        );
        let mut session = Session::new(&mut guard, &limits, accounted_input)?;
        let outcome = engine::parse_rdfxml_retained_v2(
            &owned,
            document_iri.as_deref(),
            &mut session,
            limits,
            cancellation,
            Some(interrupt),
            accounted_input,
            collect_provenance,
            require_empty_imports,
        )?;
        let parser_bytes = u64::try_from(input_size)
            .map_err(|_| NativeError::limit("native RDF/XML source exceeds u64"))?;
        Ok((outcome, parser_bytes))
    })?;
    crate::contain(|| limits.check_output_size(input_size, outcome.encoded.len()))
        .map_err(crate::python_error)?;
    let phases = (
        outcome.phases.syntax_parse_ns,
        outcome.mapping_ns,
        outcome.phases.result_encode_ns,
        outcome.phases.arena_construction_ns,
        outcome.phases.freeze_ns,
    );
    allocations.checkpoint()?;
    let encoded = PyBytes::new_with(py, outcome.encoded.len(), |buffer| {
        buffer.copy_from_slice(&outcome.encoded);
        Ok(())
    })?
    .unbind();
    Ok((
        encoded,
        NativeParsedStructuralStorageV2 {
            storage: Some(outcome.storage),
            metadata: Some(outcome.metadata),
            prepared: None,
            prepared_summary: None,
            limits,
            parser_bytes,
        },
        phases,
    ))
}

/// Parse one Functional document and construct its typed structural arena in
/// the same detached native operation.
///
/// Eligible documents, including single-document anonymous-node scopes,
/// return only bounded metadata and fingerprint evidence; canonical ontology
/// rows remain solely in the native component owner. Explicit diagnostic and
/// resolver-backed closure fallbacks retain the complete framing.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    source,
    config,
    collect_provenance,
    preserve_source_map,
    record_unresolved,
    require_empty_imports,
    cancel=None
))]
fn _parse_functional_retained_v2<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    collect_provenance: bool,
    preserve_source_map: bool,
    record_unresolved: bool,
    require_empty_imports: bool,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<RetainedParseBindingResult> {
    let mut allocations = crate::BridgeAllocationProbe::disabled();
    parse_functional_retained_v2_with_allocations(
        py,
        source,
        config,
        collect_provenance,
        preserve_source_map,
        record_unresolved,
        require_empty_imports,
        cancel,
        &mut allocations,
    )
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    source,
    config,
    collect_provenance,
    preserve_source_map,
    record_unresolved,
    require_empty_imports,
    fail_after=None
))]
fn _functional_retained_bridge_allocation_probe_v2<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    collect_provenance: bool,
    preserve_source_map: bool,
    record_unresolved: bool,
    require_empty_imports: bool,
    fail_after: Option<u64>,
) -> PyResult<(Py<PyBytes>, u64)> {
    let mut allocations = crate::BridgeAllocationProbe::configured(
        fail_after,
        "injected native Functional retained bridge allocation failure",
    );
    let (encoded, _storage, _phases) = parse_functional_retained_v2_with_allocations(
        py,
        source,
        config,
        collect_provenance,
        preserve_source_map,
        record_unresolved,
        require_empty_imports,
        None,
        &mut allocations,
    )?;
    Ok((encoded, allocations.count()))
}

#[allow(clippy::too_many_arguments)]
fn parse_functional_retained_v2_with_allocations<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    collect_provenance: bool,
    preserve_source_map: bool,
    record_unresolved: bool,
    require_empty_imports: bool,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
    allocations: &mut crate::BridgeAllocationProbe,
) -> PyResult<RetainedParseBindingResult> {
    let limits = crate::limits_from_python_with_allocations(config, allocations)?;
    let owned = crate::owned_source_request_with_allocations(py, source, &limits, allocations)?;
    let input_size = owned.len();
    let cancellation = crate::cancellation_or_default(cancel);
    let (outcome, parser_bytes) = crate::run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
            interrupt.clone(),
        );
        let request = crate::source::SourceRequest::decode(&owned, &limits)?;
        let parser_bytes = u64::try_from(request.source.len())
            .map_err(|_| NativeError::limit("native parser source exceeds u64"))?;
        let mut session = Session::new(&mut guard, &limits, input_size)?;
        let outcome = crate::parse::parse_retained(
            request,
            &mut session,
            limits,
            cancellation,
            Some(interrupt),
            input_size,
            collect_provenance,
            preserve_source_map,
            record_unresolved,
            require_empty_imports,
        )?;
        Ok((outcome, parser_bytes))
    })?;
    crate::contain(|| limits.check_output_size(input_size, outcome.encoded.len()))
        .map_err(crate::python_error)?;
    let phases = (
        outcome.phases.syntax_parse_ns,
        outcome.phases.result_encode_ns,
        outcome.phases.arena_construction_ns,
        outcome.phases.freeze_ns,
    );
    allocations.checkpoint()?;
    let encoded = PyBytes::new_with(py, outcome.encoded.len(), |buffer| {
        buffer.copy_from_slice(&outcome.encoded);
        Ok(())
    })?
    .unbind();
    Ok((
        encoded,
        NativeParsedStructuralStorageV2 {
            storage: Some(outcome.storage),
            metadata: outcome.metadata,
            prepared: None,
            prepared_summary: None,
            limits,
            parser_bytes,
        },
        phases,
    ))
}

/// Prepare exact fingerprints, content manifests, and native-owned provenance
/// without exporting canonical rows or preimages to Python.
#[pyfunction]
#[pyo3(signature = (
    parsed,
    manifest,
    document_key,
    collect_provenance,
    preserve_source_map,
    cancel=None
))]
fn _prepare_parsed_structural_snapshot_v2<'py>(
    py: Python<'py>,
    parsed: PyRefMut<'py, NativeParsedStructuralStorageV2>,
    manifest: &Bound<'py, PyBytes>,
    document_key: String,
    collect_provenance: bool,
    preserve_source_map: bool,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<Py<PyBytes>> {
    let mut allocations = crate::BridgeAllocationProbe::disabled();
    prepare_parsed_structural_snapshot_v2_with_allocations(
        py,
        parsed,
        manifest,
        document_key,
        collect_provenance,
        preserve_source_map,
        cancel,
        &mut allocations,
    )
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    parsed,
    manifest,
    document_key,
    collect_provenance,
    preserve_source_map,
    fail_after=None
))]
fn _prepare_parsed_structural_bridge_allocation_probe_v2<'py>(
    py: Python<'py>,
    parsed: PyRefMut<'py, NativeParsedStructuralStorageV2>,
    manifest: &Bound<'py, PyBytes>,
    document_key: String,
    collect_provenance: bool,
    preserve_source_map: bool,
    fail_after: Option<u64>,
) -> PyResult<(Py<PyBytes>, u64)> {
    let mut allocations = crate::BridgeAllocationProbe::configured(
        fail_after,
        "injected native retained preparation bridge allocation failure",
    );
    let encoded = prepare_parsed_structural_snapshot_v2_with_allocations(
        py,
        parsed,
        manifest,
        document_key,
        collect_provenance,
        preserve_source_map,
        None,
        &mut allocations,
    )?;
    Ok((encoded, allocations.count()))
}

#[allow(clippy::too_many_arguments)]
fn prepare_parsed_structural_snapshot_v2_with_allocations<'py>(
    py: Python<'py>,
    mut parsed: PyRefMut<'py, NativeParsedStructuralStorageV2>,
    manifest: &Bound<'py, PyBytes>,
    document_key: String,
    collect_provenance: bool,
    preserve_source_map: bool,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
    allocations: &mut crate::BridgeAllocationProbe,
) -> PyResult<Py<PyBytes>> {
    if parsed.prepared.is_some() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            "native parsed structural storage was already prepared",
        ));
    }
    let mut owned_manifest = Vec::new();
    allocations.checkpoint()?;
    owned_manifest
        .try_reserve_exact(manifest.as_bytes().len())
        .map_err(|_| {
            crate::python_error(NativeError::limit(
                "native retained manifest allocation failed",
            ))
        })?;
    owned_manifest.extend_from_slice(manifest.as_bytes());
    let storage = parsed.storage.take().ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(
            "native parsed structural storage was already consumed",
        )
    })?;
    let metadata = parsed.metadata.take().ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(
            "native parsed structural storage has no compact publication metadata",
        )
    })?;
    let limits = parsed.limits;
    let cancellation = crate::cancellation_or_default(cancel);
    let (storage, prepared, summary) = crate::run_detached(py, move |interrupt| {
        let started = Instant::now();
        let prepared = crate::parse::prepare_retained_publication_v2(
            &storage,
            &metadata,
            &owned_manifest,
            &document_key,
            collect_provenance,
            preserve_source_map,
            &limits,
            cancellation,
            Some(interrupt),
        )?;
        let prepare_ns = u64::try_from(started.elapsed().as_nanos())
            .map_err(|_| NativeError::limit("native publication preparation time exceeds u64"))?;
        let summary = prepared.encode_summary(prepare_ns)?;
        Ok((storage, prepared, summary))
    })?;
    allocations.checkpoint()?;
    let encoded = PyBytes::new_with(py, summary.len(), |buffer| {
        buffer.copy_from_slice(&summary);
        Ok(())
    })?
    .unbind();
    parsed.storage = Some(storage);
    parsed.prepared = Some(prepared);
    parsed.prepared_summary = Some(summary);
    Ok(encoded)
}

/// Attach immutable metadata attestations to already prepared parser storage.
#[pyfunction]
#[pyo3(signature = (parsed, prepared_summary, attestation, cancel=None))]
fn _finalize_parsed_structural_snapshot_v2<'py>(
    py: Python<'py>,
    mut parsed: PyRefMut<'py, NativeParsedStructuralStorageV2>,
    prepared_summary: &Bound<'py, PyBytes>,
    attestation: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<NativeSnapshotHandle> {
    let _cancellation = crate::cancellation_or_default(cancel);
    if parsed.prepared_summary.as_deref() != Some(prepared_summary.as_bytes()) {
        return Err(crate::python_error(NativeError::protocol(
            "native retained summary diverges before final publication",
        )));
    }
    let parser_bytes = parsed.parser_bytes;
    let storage = parsed.storage.take().ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(
            "native parsed structural storage was already consumed",
        )
    })?;
    let prepared = parsed.prepared.take().ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(
            "native parsed structural storage was not prepared",
        )
    })?;
    parsed.prepared_summary = None;
    validate_prepared_attestation(py, &prepared, attestation)?;
    let source_map = prepared.source_map;
    let rdf_report = prepared.rdf_report.map(|report| report.rows);
    crate::publication::typed_structural_handle_v2(
        attestation,
        storage,
        prepared.origin_rows,
        prepared.raw_origin_rows,
        source_map,
        rdf_report,
        parser_bytes,
    )
}

fn validate_prepared_attestation(
    py: Python<'_>,
    prepared: &crate::parse::PreparedRetainedPublicationV2,
    attestation: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let expected_digests = [
        ("root_table_sha256", prepared.content.root_table_sha256),
        (
            "effective_root_table_sha256",
            prepared.content.effective_root_table_sha256,
        ),
        (
            "fingerprint_inputs_sha256",
            prepared.content.fingerprint_inputs_sha256,
        ),
        (
            "source_manifest_sha256",
            prepared.content.source_manifest_sha256,
        ),
        (
            "provenance_manifest_sha256",
            prepared.content.provenance_manifest_sha256,
        ),
        (
            "effective_origin_manifest_sha256",
            prepared.content.effective_origin_manifest_sha256,
        ),
    ];
    for (name, expected) in expected_digests {
        let value = attestation.getattr(name)?;
        if !value.get_type().is(py.get_type::<PyBytes>())
            || value.cast::<PyBytes>()?.as_bytes() != expected
        {
            return Err(crate::python_error(NativeError::protocol(
                "native retained content diverges from its attestation",
            )));
        }
    }
    let origins = prepared
        .raw_origin_rows
        .as_ref()
        .or(prepared.origin_rows.as_ref())
        .map_or(Ok(0_u64), |rows| {
            u64::try_from(rows.len()).map_err(|_| {
                crate::python_error(NativeError::limit(
                    "native retained origin count exceeds u64",
                ))
            })
        })?;
    let source_entries = prepared.source_map.as_ref().map_or(Ok(0_u64), |source| {
        u64::try_from(source.entries.len()).map_err(|_| {
            crate::python_error(NativeError::limit(
                "native retained source-map count exceeds u64",
            ))
        })
    })?;
    let capability_bits = 7_u64
        | if prepared.source_map.is_some() { 8 } else { 0 }
        | if prepared.origin_rows.is_some() {
            16
        } else {
            0
        }
        | if prepared.rdf_report.is_some() { 32 } else { 0 };
    if attestation
        .getattr("root_document_key")?
        .extract::<String>()?
        != prepared.document_key.as_ref()
        || attestation.getattr("document_count")?.extract::<u64>()? != 1
        || attestation
            .getattr("source_map_entry_count")?
            .extract::<u64>()?
            != source_entries
        || attestation
            .getattr("origin_entry_count")?
            .extract::<u64>()?
            != origins
        || attestation
            .getattr("rdf_mapping_report_count")?
            .extract::<u64>()?
            != u64::from(prepared.rdf_report.is_some())
        || attestation.getattr("capability_bits")?.extract::<u64>()? != capability_bits
        || attestation
            .getattr("max_facade_row_bytes")?
            .extract::<u64>()?
            != prepared.max_facade_row_bytes
    {
        return Err(crate::python_error(NativeError::protocol(
            "native retained publication metadata diverges from its attestation",
        )));
    }
    Ok(())
}

/// Freeze one already-validated document into the real typed V2 owner.
///
/// This private, unadvertised bridge lets the narrowly eligible public
/// forced-native load path retain its structural roots in the typed owner.  It
/// accepts canonical roots plus their attested origin rows, publishes no
/// format capability, and deliberately remains single-document until native
/// import orchestration owns the complete closure.
#[pyfunction]
#[pyo3(signature = (
    documents,
    origins,
    attestation,
    config,
    cancel=None,
    *,
    effective_documents=None,
    effective_origins=None
))]
#[allow(clippy::too_many_arguments)]
fn _retain_structural_snapshot_v2<'py>(
    py: Python<'py>,
    documents: &Bound<'py, PyAny>,
    origins: &Bound<'py, PyAny>,
    attestation: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
    effective_documents: Option<&Bound<'py, PyAny>>,
    effective_origins: Option<&Bound<'py, PyAny>>,
) -> PyResult<NativeSnapshotHandle> {
    let limits = crate::limits_from_python(config)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let (owned_documents, mut external_bytes) =
        owned_structural_documents(py, documents, &limits, &cancellation)?;
    let owned_effective_documents = effective_documents
        .map(|value| owned_structural_documents(py, value, &limits, &cancellation))
        .transpose()?;
    if let Some((_, effective_bytes)) = &owned_effective_documents {
        external_bytes = external_bytes
            .checked_add(*effective_bytes)
            .ok_or_else(|| {
                crate::python_error(NativeError::limit(
                    "native scoped structural boundary size overflow",
                ))
            })?;
        enforce_retained_boundary(external_bytes, &limits)?;
    }
    let owned_origins =
        owned_origin_rows(py, origins, &limits, &cancellation, &mut external_bytes)?;
    let owned_effective_origins = effective_origins
        .map(|value| owned_origin_rows(py, value, &limits, &cancellation, &mut external_bytes))
        .transpose()?;
    let owned_effective_documents = owned_effective_documents.map(|(rows, _bytes)| rows);
    let (storage, retained_origins, raw_origins) = crate::run_detached(py, move |interrupt| {
        let mut builder = TypedFacadeBuilderV2::new(
            limits,
            cancellation.clone(),
            Some(interrupt),
            external_bytes,
        )?;
        if let Some(effective_documents) = owned_effective_documents {
            if effective_documents.len() != owned_documents.len() {
                return Err(NativeError::protocol(
                    "native raw and effective document counts diverge",
                ));
            }
            for (document, effective) in owned_documents.iter().zip(&effective_documents) {
                builder.add_scoped_document(
                    &document[0],
                    &document[1],
                    &document[2],
                    &effective[0],
                    &effective[1],
                    &effective[2],
                )?;
            }
        } else {
            for document in &owned_documents {
                builder.add_document(&document[0], &document[1], &document[2])?;
            }
        }
        let (effective, raw) = match owned_effective_origins {
            Some(effective) => (effective, Some(owned_origins)),
            None => (owned_origins, None),
        };
        Ok((builder.freeze(&[vec![0]], &[0])?, effective, raw))
    })?;
    crate::publication::typed_structural_handle_v2(
        attestation,
        storage,
        Some(retained_origins),
        raw_origins,
        None,
        None,
        0,
    )
}

fn enforce_retained_boundary(total_bytes: usize, limits: &Limits) -> PyResult<()> {
    let total = u64::try_from(total_bytes).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native retained boundary size exceeds u64",
        ))
    })?;
    if total > limits.value(LimitKey::MaxTemporaryBytes)
        || limits
            .max_memory_bytes
            .is_some_and(|maximum| total > maximum)
    {
        return Err(crate::python_error(NativeError::limit(
            "native retained boundary exceeds configured memory limits",
        )));
    }
    Ok(())
}

type OwnedStructuralDocument = [Vec<Vec<u8>>; 3];

fn owned_structural_documents(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: &Limits,
    cancellation: &crate::cancel::Cancellation,
) -> PyResult<(Vec<OwnedStructuralDocument>, usize)> {
    if !value.get_type().is(py.get_type::<PyTuple>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "native structural documents must be an exact tuple",
        ));
    }
    let documents = value.cast::<PyTuple>()?;
    if documents.len() != 1 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "native structural retention currently requires exactly one document",
        ));
    }
    let mut owned = Vec::new();
    owned.try_reserve_exact(1).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native structural document allocation failed",
        ))
    })?;
    let mut total_bytes = 0_usize;
    for document in documents {
        cancellation.checkpoint().map_err(crate::python_error)?;
        if !document.get_type().is(py.get_type::<PyTuple>()) {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "native structural document must be an exact tuple",
            ));
        }
        let document = document.cast::<PyTuple>()?;
        if document.len() != 3 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "native structural document must contain three root collections",
            ));
        }
        let annotations = owned_structural_rows(
            py,
            &document.get_item(0)?,
            limits.max_annotations,
            limits,
            cancellation,
            &mut total_bytes,
        )?;
        let axioms = owned_structural_rows(
            py,
            &document.get_item(1)?,
            limits.max_axioms,
            limits,
            cancellation,
            &mut total_bytes,
        )?;
        let extensions = owned_structural_rows(
            py,
            &document.get_item(2)?,
            limits.max_axioms,
            limits,
            cancellation,
            &mut total_bytes,
        )?;
        owned.push([annotations, axioms, extensions]);
    }
    Ok((owned, total_bytes))
}

fn owned_structural_rows(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    maximum_rows: u64,
    limits: &Limits,
    cancellation: &crate::cancel::Cancellation,
    total_bytes: &mut usize,
) -> PyResult<Vec<Vec<u8>>> {
    if !value.get_type().is(py.get_type::<PyTuple>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "native structural roots must be exact tuples",
        ));
    }
    let rows = value.cast::<PyTuple>()?;
    if u64::try_from(rows.len()).map_or(true, |length| length > maximum_rows) {
        return Err(crate::python_error(NativeError::limit(
            "native structural root count exceeds configured limits",
        )));
    }
    let mut owned = Vec::new();
    owned.try_reserve_exact(rows.len()).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native structural root allocation failed",
        ))
    })?;
    for (ordinal, row) in rows.iter().enumerate() {
        if ordinal
            % usize::try_from(limits.cancellation_stride)
                .unwrap_or(1)
                .max(1)
            == 0
        {
            cancellation.checkpoint().map_err(crate::python_error)?;
            py.check_signals()?;
        }
        if !row.get_type().is(py.get_type::<PyBytes>()) {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "native structural roots must contain exact bytes",
            ));
        }
        let bytes = row.cast::<PyBytes>()?.as_bytes();
        if bytes.is_empty() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "native structural roots must be nonempty",
            ));
        }
        *total_bytes = total_bytes.checked_add(bytes.len()).ok_or_else(|| {
            crate::python_error(NativeError::limit(
                "native structural boundary size overflow",
            ))
        })?;
        let total = u64::try_from(*total_bytes).map_err(|_| {
            crate::python_error(NativeError::limit(
                "native structural boundary size exceeds u64",
            ))
        })?;
        if total > limits.value(LimitKey::MaxTemporaryBytes)
            || limits
                .max_memory_bytes
                .is_some_and(|maximum| total > maximum)
        {
            return Err(crate::python_error(NativeError::limit(
                "native structural boundary exceeds configured memory limits",
            )));
        }
        let mut copied = Vec::new();
        copied.try_reserve_exact(bytes.len()).map_err(|_| {
            crate::python_error(NativeError::limit(
                "native structural row allocation failed",
            ))
        })?;
        copied.extend_from_slice(bytes);
        owned.push(copied);
    }
    Ok(owned)
}

fn owned_origin_rows(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: &Limits,
    cancellation: &crate::cancel::Cancellation,
    total_bytes: &mut usize,
) -> PyResult<Vec<Vec<u8>>> {
    if !value.get_type().is(py.get_type::<PyTuple>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "native origin rows must be an exact tuple",
        ));
    }
    let rows = value.cast::<PyTuple>()?;
    if u64::try_from(rows.len()).map_or(true, |length| length > limits.max_origin_entries) {
        return Err(crate::python_error(NativeError::limit(
            "native origin row count exceeds configured limits",
        )));
    }
    let mut owned = Vec::new();
    owned.try_reserve_exact(rows.len()).map_err(|_| {
        crate::python_error(NativeError::limit("native origin row allocation failed"))
    })?;
    for (ordinal, row) in rows.iter().enumerate() {
        if ordinal
            % usize::try_from(limits.cancellation_stride)
                .unwrap_or(1)
                .max(1)
            == 0
        {
            cancellation.checkpoint().map_err(crate::python_error)?;
            py.check_signals()?;
        }
        if !row.get_type().is(py.get_type::<PyBytes>()) {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "native origin rows must contain exact bytes",
            ));
        }
        let bytes = row.cast::<PyBytes>()?.as_bytes();
        if bytes.is_empty() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "native origin rows must be nonempty",
            ));
        }
        *total_bytes = total_bytes.checked_add(bytes.len()).ok_or_else(|| {
            crate::python_error(NativeError::limit("native retained boundary size overflow"))
        })?;
        let total = u64::try_from(*total_bytes).map_err(|_| {
            crate::python_error(NativeError::limit(
                "native retained boundary size exceeds u64",
            ))
        })?;
        if total > limits.value(LimitKey::MaxTemporaryBytes)
            || limits
                .max_memory_bytes
                .is_some_and(|maximum| total > maximum)
        {
            return Err(crate::python_error(NativeError::limit(
                "native retained boundary exceeds configured memory limits",
            )));
        }
        let mut copied = Vec::new();
        copied.try_reserve_exact(bytes.len()).map_err(|_| {
            crate::python_error(NativeError::limit("native origin row allocation failed"))
        })?;
        copied.extend_from_slice(bytes);
        owned.push(copied);
    }
    Ok(owned)
}

/// Bounded observability hook for the unadvertised first WP16 slice.
///
/// The returned bytes are a test-only ledger; the first tuple item is the real
/// frozen V1 owner.  No production capability is advertised until the complete
/// RDF/XML grammar and OWL RDF mapping pass the installed-path matrix.
#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (source, document_iri, config, cancel=None))]
fn _ingest_rdfxml_slice_v1<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    document_iri: Option<&Bound<'py, PyAny>>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<(NativeSnapshotHandle, Py<PyBytes>)> {
    let limits = crate::limits_from_python(config)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let document_iri = owned_document_iri(py, document_iri, &limits)?;
    let document_iri_size = document_iri.as_ref().map_or(0, String::len);
    let owned = owned_source(py, source, document_iri_size, &limits, &cancellation)?;
    let input_size = owned.len();
    let accounted_input =
        accounted_input_bytes(input_size, document_iri_size).map_err(crate::python_error)?;
    let outcome = crate::run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let mut session = Session::new(&mut guard, &limits, accounted_input)?;
        let outcome =
            engine::ingest_rdfxml_v1_test_adapter(&owned, document_iri.as_deref(), &mut session)?;
        session.finish()?;
        Ok(outcome)
    })?;
    let observation = PyBytes::new(py, &outcome.observation).unbind();
    Ok((outcome.publication.into_handle(), observation))
}

#[cfg(feature = "test-hooks")]
fn owned_source(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    document_iri_bytes: usize,
    limits: &crate::limits::Limits,
    cancellation: &crate::cancel::Cancellation,
) -> PyResult<Vec<u8>> {
    let mut allocations = crate::BridgeAllocationProbe::disabled();
    owned_source_with_allocations(
        py,
        value,
        document_iri_bytes,
        limits,
        cancellation,
        &mut allocations,
    )
}

fn owned_source_with_allocations(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    document_iri_bytes: usize,
    limits: &crate::limits::Limits,
    cancellation: &crate::cancel::Cancellation,
    allocations: &mut crate::BridgeAllocationProbe,
) -> PyResult<Vec<u8>> {
    cancellation.checkpoint().map_err(crate::python_error)?;
    let view = PyBuffer::<u8>::get(value).map_err(|error| {
        if error.is_instance_of::<pyo3::exceptions::PyBufferError>(py)
            || error.is_instance_of::<pyo3::exceptions::PyTypeError>(py)
        {
            pyo3::exceptions::PyTypeError::new_err(
                "native RDF/XML source must expose a contiguous byte buffer",
            )
        } else {
            error
        }
    })?;
    let size = view.len_bytes();
    contain_source_size(size, document_iri_bytes, limits).map_err(crate::python_error)?;
    let cells = view.as_slice(py).ok_or_else(|| {
        pyo3::exceptions::PyTypeError::new_err(
            "native RDF/XML source must expose a contiguous byte buffer",
        )
    })?;
    let mut result = Vec::new();
    allocations.checkpoint()?;
    result.try_reserve_exact(size).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native RDF/XML owned-source allocation failed",
        ))
    })?;
    for chunk in cells.chunks(64 * 1024) {
        cancellation.checkpoint().map_err(crate::python_error)?;
        py.check_signals()?;
        result.extend(chunk.iter().map(pyo3::buffer::ReadOnlyCell::get));
    }
    cancellation.checkpoint().map_err(crate::python_error)?;
    Ok(result)
}

#[cfg(feature = "test-hooks")]
fn owned_document_iri(
    py: Python<'_>,
    value: Option<&Bound<'_, PyAny>>,
    limits: &crate::limits::Limits,
) -> PyResult<Option<String>> {
    let mut allocations = crate::BridgeAllocationProbe::disabled();
    owned_document_iri_with_allocations(py, value, limits, &mut allocations)
}

fn owned_document_iri_with_allocations(
    py: Python<'_>,
    value: Option<&Bound<'_, PyAny>>,
    limits: &crate::limits::Limits,
    allocations: &mut crate::BridgeAllocationProbe,
) -> PyResult<Option<String>> {
    let Some(value) = value else {
        return Ok(None);
    };
    if !value.get_type().is(py.get_type::<PyString>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "native RDF/XML document_iri must be an exact str or None",
        ));
    }
    let value = value.cast::<PyString>()?;
    let maximum = limits.value(LimitKey::MaxIriBytes);
    if u64::try_from(value.len()?).map_or(true, |size| size > maximum) {
        return Err(crate::python_error(NativeError::limit(
            "native RDF/XML document IRI exceeds max_iri_bytes",
        )));
    }
    let text = value.to_str()?;
    let size = text.len();
    let size_u64 = u64::try_from(size).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native RDF/XML document IRI length exceeds u64",
        ))
    })?;
    let boundary_bytes = size_u64.checked_mul(2).ok_or_else(|| {
        crate::python_error(NativeError::limit(
            "native RDF/XML document IRI accounting overflow",
        ))
    })?;
    if size_u64 > maximum
        || boundary_bytes > limits.value(LimitKey::MaxTemporaryBytes)
        || limits
            .max_memory_bytes
            .is_some_and(|maximum| boundary_bytes > maximum)
    {
        return Err(crate::python_error(NativeError::limit(
            "native RDF/XML document IRI exceeds configured resource limits",
        )));
    }
    let mut result = String::new();
    allocations.checkpoint()?;
    result.try_reserve_exact(size).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native RDF/XML document IRI allocation failed",
        ))
    })?;
    result.push_str(text);
    Ok(Some(result))
}

fn accounted_input_bytes(source: usize, document_iri: usize) -> NativeResult<usize> {
    source
        .checked_mul(2)
        .and_then(|source| {
            document_iri
                .checked_mul(2)
                .and_then(|document_iri| source.checked_add(document_iri))
        })
        .ok_or_else(|| NativeError::limit("native RDF/XML boundary memory accounting overflow"))
}

fn contain_source_size(
    size: usize,
    document_iri_bytes: usize,
    limits: &crate::limits::Limits,
) -> NativeResult<()> {
    let size = u64::try_from(size)
        .map_err(|_| NativeError::limit("native RDF/XML source length exceeds u64"))?;
    let document_iri_bytes = u64::try_from(document_iri_bytes)
        .map_err(|_| NativeError::limit("native RDF/XML document IRI length exceeds u64"))?;
    let source_transient = size
        .checked_mul(3)
        .ok_or_else(|| NativeError::limit("native RDF/XML transient size overflow"))?;
    let iri_transient = document_iri_bytes
        .checked_mul(2)
        .ok_or_else(|| NativeError::limit("native RDF/XML document IRI accounting overflow"))?;
    let transient = source_transient
        .checked_add(iri_transient)
        .ok_or_else(|| NativeError::limit("native RDF/XML transient size overflow"))?;
    if size > limits.value(LimitKey::MaxSourceBytes)
        || size > limits.value(LimitKey::MaxTotalSourceBytes)
        || transient > limits.value(LimitKey::MaxTemporaryBytes)
        || limits
            .max_memory_bytes
            .is_some_and(|maximum| transient > maximum)
    {
        return Err(NativeError::limit(
            "native RDF/XML source exceeds configured resource limits",
        ));
    }
    Ok(())
}
