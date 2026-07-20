//! WP16-owned native ingestion registration seam.
//!
//! WP15 intentionally publishes no successor ingestion capability. WP16 may
//! add functions/classes and feature names in this module without editing the
//! shared module registry.

#[allow(dead_code)]
#[path = "../ingestion/mod.rs"]
mod engine;

use pyo3::prelude::*;
use pyo3::types::PyModule;

#[cfg(feature = "test-hooks")]
use pyo3::buffer::PyBuffer;
#[cfg(feature = "test-hooks")]
use pyo3::types::{PyAny, PyBytes, PyString};

#[cfg(feature = "test-hooks")]
use crate::cancel::Guard;
#[cfg(feature = "test-hooks")]
use crate::error::{NativeError, NativeResult};
#[cfg(feature = "test-hooks")]
use crate::limits::LimitKey;
#[cfg(feature = "test-hooks")]
use crate::publication::NativeSnapshotHandle;
#[cfg(feature = "test-hooks")]
use crate::session::Session;

pub(super) const FEATURES: &[&str] = &[];

pub(super) fn register(_py: Python<'_>, _module: &Bound<'_, PyModule>) -> PyResult<()> {
    #[cfg(feature = "test-hooks")]
    _module.add_function(wrap_pyfunction!(_ingest_rdfxml_slice_v1, _module)?)?;
    Ok(())
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
