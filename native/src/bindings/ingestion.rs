//! WP16-owned native ingestion registration seam.
//!
//! WP15 intentionally publishes no successor ingestion capability. WP16 may
//! add functions/classes and feature names in this module without editing the
//! shared module registry.

#[allow(dead_code)]
#[path = "../ingestion/mod.rs"]
mod engine;

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyModule, PyTuple};

#[cfg(feature = "test-hooks")]
use pyo3::buffer::PyBuffer;
#[cfg(feature = "test-hooks")]
use pyo3::types::PyString;

use crate::cancel::Guard;
use crate::error::NativeError;
#[cfg(feature = "test-hooks")]
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
    limits: Limits,
    parser_bytes: u64,
}

type RetainedParseBindingResult = (
    Py<PyBytes>,
    NativeParsedStructuralStorageV2,
    (u64, u64, u64, u64),
);

pub(super) fn register(_py: Python<'_>, _module: &Bound<'_, PyModule>) -> PyResult<()> {
    _module.add_class::<NativeParsedStructuralStorageV2>()?;
    _module.add_function(wrap_pyfunction!(_retain_structural_snapshot_v2, _module)?)?;
    _module.add_function(wrap_pyfunction!(_parse_functional_retained_v2, _module)?)?;
    _module.add_function(wrap_pyfunction!(
        _finalize_parsed_structural_snapshot_v2,
        _module
    )?)?;
    #[cfg(feature = "test-hooks")]
    _module.add_function(wrap_pyfunction!(_ingest_rdfxml_slice_v1, _module)?)?;
    Ok(())
}

/// Parse one Functional document and construct its typed structural arena in
/// the same detached native operation.
///
/// The result framing remains byte-for-byte identical to `parse_document` so
/// the existing complete Python contract decoder can validate parity while
/// WP16 incrementally removes that transitional model reconstruction.  The
/// opaque storage state never advertises a capability and can only be consumed
/// once by the final attested publication boundary below.
#[pyfunction]
#[pyo3(signature = (source, config, cancel=None))]
fn _parse_functional_retained_v2<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<RetainedParseBindingResult> {
    let limits = crate::limits_from_python(config)?;
    let owned = crate::owned_source_request(py, source, &limits)?;
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
    let encoded = PyBytes::new_with(py, outcome.encoded.len(), |buffer| {
        buffer.copy_from_slice(&outcome.encoded);
        Ok(())
    })?
    .unbind();
    Ok((
        encoded,
        NativeParsedStructuralStorageV2 {
            storage: Some(outcome.storage),
            limits,
            parser_bytes,
        },
        phases,
    ))
}

/// Attach immutable metadata/origin attestations to parser-built storage.
#[pyfunction]
#[pyo3(signature = (parsed, origins, attestation, cancel=None))]
fn _finalize_parsed_structural_snapshot_v2<'py>(
    py: Python<'py>,
    mut parsed: PyRefMut<'py, NativeParsedStructuralStorageV2>,
    origins: Option<&Bound<'py, PyAny>>,
    attestation: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<NativeSnapshotHandle> {
    let cancellation = crate::cancellation_or_default(cancel);
    let limits = parsed.limits;
    let parser_bytes = parsed.parser_bytes;
    let storage = parsed.storage.take().ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(
            "native parsed structural storage was already consumed",
        )
    })?;
    let retained_origins = match origins {
        Some(rows) => {
            let mut external_bytes = 0_usize;
            Some(owned_origin_rows(
                py,
                rows,
                &limits,
                &cancellation,
                &mut external_bytes,
            )?)
        }
        None => None,
    };
    crate::publication::typed_structural_handle_v2(
        attestation,
        storage,
        retained_origins,
        parser_bytes,
    )
}

/// Freeze one already-validated document into the real typed V2 owner.
///
/// This private, unadvertised bridge lets the narrowly eligible public
/// forced-native load path retain its structural roots in the typed owner.  It
/// accepts canonical roots plus their attested origin rows, publishes no
/// format capability, and deliberately remains single-document until native
/// import orchestration owns the complete closure.
#[pyfunction]
#[pyo3(signature = (documents, origins, attestation, config, cancel=None))]
fn _retain_structural_snapshot_v2<'py>(
    py: Python<'py>,
    documents: &Bound<'py, PyAny>,
    origins: &Bound<'py, PyAny>,
    attestation: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<NativeSnapshotHandle> {
    let limits = crate::limits_from_python(config)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let (owned_documents, mut external_bytes) =
        owned_structural_documents(py, documents, &limits, &cancellation)?;
    let owned_origins =
        owned_origin_rows(py, origins, &limits, &cancellation, &mut external_bytes)?;
    let (storage, retained_origins) = crate::run_detached(py, move |interrupt| {
        let mut builder = TypedFacadeBuilderV2::new(
            limits,
            cancellation.clone(),
            Some(interrupt),
            external_bytes,
        )?;
        for document in &owned_documents {
            builder.add_document(&document[0], &document[1], &document[2])?;
        }
        Ok((builder.freeze(&[vec![0]], &[0])?, owned_origins))
    })?;
    crate::publication::typed_structural_handle_v2(attestation, storage, Some(retained_origins), 0)
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
    result.try_reserve_exact(size).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native RDF/XML document IRI allocation failed",
        ))
    })?;
    result.push_str(text);
    Ok(Some(result))
}

#[cfg(feature = "test-hooks")]
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

#[cfg(feature = "test-hooks")]
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
