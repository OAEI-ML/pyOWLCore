//! WP17-owned native view/index/wire registration seam.
//!
//! WP15 intentionally publishes no successor view capability. WP17 may add
//! functions/classes and feature names here without editing the shared module
//! registry or the ingestion module.

use pyo3::prelude::*;
use pyo3::types::PyModule;

#[cfg(feature = "test-hooks")]
use pyo3::types::{PyAny, PyBytes, PyDict, PyString};

#[cfg(any(test, feature = "test-hooks"))]
use crate::error::{NativeError, NativeResult};
#[cfg(any(test, feature = "test-hooks"))]
use crate::publication::TypedFacadeScopeV2;
#[cfg(feature = "test-hooks")]
use crate::publication::{NativeDocumentHandle, NativeSnapshotHandle};

#[cfg(any(test, feature = "test-hooks"))]
mod generated {
    include!(concat!(env!("OUT_DIR"), "/encoded_view_v1.rs"));
}

#[cfg(any(test, feature = "test-hooks"))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct EncodedViewSchema {
    name: &'static str,
    version: u32,
    model_schema: u32,
    status: &'static str,
    capability_advertised: bool,
    descriptor: &'static [u8],
    descriptor_sha256: [u8; 32],
}

#[cfg(any(test, feature = "test-hooks"))]
const ENCODED_VIEW_SCHEMA_V1: EncodedViewSchema = EncodedViewSchema {
    name: generated::NAME,
    version: generated::VERSION,
    model_schema: generated::MODEL_SCHEMA,
    status: generated::STATUS,
    capability_advertised: generated::CAPABILITY_ADVERTISED,
    descriptor: generated::DESCRIPTOR,
    descriptor_sha256: generated::DESCRIPTOR_SHA256,
};

#[cfg(feature = "test-hooks")]
type PyEncodedViewSchemaV1 = (String, u32, u32, Py<PyBytes>, Py<PyBytes>, String, bool);

pub(super) const FEATURES: &[&str] = &[];

pub(super) fn register(_py: Python<'_>, _module: &Bound<'_, PyModule>) -> PyResult<()> {
    #[cfg(feature = "test-hooks")]
    {
        _module.add_function(wrap_pyfunction!(_encoded_view_schema_v1, _module)?)?;
        _module.add_function(wrap_pyfunction!(_encoded_structural_columns_v1, _module)?)?;
        _module.add_function(wrap_pyfunction!(
            _encoded_structural_document_columns_v1,
            _module
        )?)?;
    }
    Ok(())
}

/// Exercise raw document-owner selection without relaxing the snapshot hook's
/// effective-scope semantics.
#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (handle, config, cancel=None))]
fn _encoded_structural_document_columns_v1<'py>(
    py: Python<'py>,
    handle: PyRef<'py, NativeDocumentHandle>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    let limits = crate::limits_from_python(config)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let document_ordinal = handle.document_ordinal();
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let columns = crate::run_detached(py, move |interrupt| {
        storage.encoded_structural_columns(
            TypedFacadeScopeV2::Document,
            Some(document_ordinal),
            true,
            &limits,
            cancellation,
            Some(interrupt),
        )
    })?;
    encoded_columns_to_python(py, &columns)
}

/// Exercise the direct retained-column path through an open V2 snapshot
/// owner. This remains a test hook until the installed-wheel lifetime/copy
/// matrix permits advertising the frozen view capability.
#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, config, cancel=None))]
fn _encoded_structural_columns_v1<'py>(
    py: Python<'py>,
    handle: PyRef<'py, NativeSnapshotHandle>,
    scope: &Bound<'py, PyAny>,
    document_ordinal: Option<u64>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    if !scope.get_type().is(py.get_type::<PyString>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "encoded structural scope must be an exact str",
        ));
    }
    let scope: String = scope.extract()?;
    let selected_scope = encoded_selection(&scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let limits = crate::limits_from_python(config)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let columns = crate::run_detached(py, move |interrupt| {
        storage.encoded_structural_columns(
            selected_scope,
            document_ordinal,
            false,
            &limits,
            cancellation,
            Some(interrupt),
        )
    })?;
    encoded_columns_to_python(py, &columns)
}

#[cfg(any(test, feature = "test-hooks"))]
fn encoded_selection(
    scope: &str,
    document_ordinal: Option<u64>,
) -> Result<TypedFacadeScopeV2, &'static str> {
    match scope {
        "closure" if document_ordinal.is_none() => Ok(TypedFacadeScopeV2::Closure),
        "document" if document_ordinal.is_some() => Ok(TypedFacadeScopeV2::Document),
        "closure" | "document" => Err("encoded structural scope and document ordinal disagree"),
        _ => Err("encoded structural scope must be closure or document"),
    }
}

#[cfg(feature = "test-hooks")]
fn encoded_columns_to_python(
    py: Python<'_>,
    columns: &crate::model::EncodedStructuralColumnsV1,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    let buffers = PyDict::new(py);
    for (name, payload) in columns.buffers().named() {
        buffers.set_item(name, PyBytes::new(py, payload))?;
    }
    let counters = columns.counters();
    let observed = PyDict::new(py);
    for (name, value) in [
        ("root_rows", counters.root_rows),
        ("node_rows", counters.node_rows),
        ("field_rows", counters.field_rows),
        ("item_rows", counters.item_rows),
        ("scalar_bytes", counters.scalar_bytes),
        ("retained_buffer_bytes", counters.retained_buffer_bytes),
        ("retained_metadata_bytes", counters.retained_metadata_bytes),
        ("peak_owned_bytes", counters.peak_owned_bytes),
        ("peak_workspace_bytes", counters.peak_workspace_bytes),
        ("scalar_copy_bytes", counters.scalar_copy_bytes),
        ("canonical_work", counters.canonical_work),
        (
            "canonical_comparison_bytes",
            counters.canonical_comparison_bytes,
        ),
        (
            "complete_root_encode_calls",
            counters.complete_root_encode_calls,
        ),
        // The pre-advertisement PyO3 observation bridge freezes each Rust
        // buffer into Python bytes. Keep that copy visible and distinct from
        // construction counters until the owning buffer surface replaces it.
        ("python_bridge_copy_bytes", counters.retained_buffer_bytes),
    ] {
        observed.set_item(name, value)?;
    }
    Ok((buffers.unbind(), observed.unbind()))
}

#[cfg(any(test, feature = "test-hooks"))]
fn registered_schema(
    name: &str,
    version: u32,
    model_schema: u32,
    descriptor_sha256: &[u8],
) -> NativeResult<&'static EncodedViewSchema> {
    let schema = &ENCODED_VIEW_SCHEMA_V1;
    if name != schema.name
        || version != schema.version
        || model_schema != schema.model_schema
        || descriptor_sha256 != schema.descriptor_sha256
        || schema.capability_advertised
    {
        return Err(NativeError::protocol(
            "native encoded-view schema registration mismatch",
        ));
    }
    Ok(schema)
}

/// Validate and observe the frozen descriptor without advertising a capability.
#[cfg(feature = "test-hooks")]
#[pyfunction]
fn _encoded_view_schema_v1(
    py: Python<'_>,
    schema_name: &str,
    schema_version: u32,
    model_schema: u32,
    descriptor_sha256: &Bound<'_, PyBytes>,
) -> PyResult<PyEncodedViewSchemaV1> {
    let schema = registered_schema(
        schema_name,
        schema_version,
        model_schema,
        descriptor_sha256.as_bytes(),
    )
    .map_err(crate::python_error)?;
    Ok((
        schema.name.to_owned(),
        schema.version,
        schema.model_schema,
        PyBytes::new(py, schema.descriptor).unbind(),
        PyBytes::new(py, &schema.descriptor_sha256).unbind(),
        schema.status.to_owned(),
        schema.capability_advertised,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_schema_matches_the_embedded_descriptor() {
        let schema = registered_schema(
            generated::NAME,
            generated::VERSION,
            generated::MODEL_SCHEMA,
            &generated::DESCRIPTOR_SHA256,
        )
        .unwrap();
        assert_eq!(
            crate::hash::sha256(schema.descriptor),
            schema.descriptor_sha256
        );
        assert!(schema.descriptor.is_ascii());
        assert!(!schema.capability_advertised);
        assert!(FEATURES.is_empty());
    }

    #[test]
    fn registration_mismatches_fail_closed() {
        let schema = ENCODED_VIEW_SCHEMA_V1;
        let mut wrong_digest = schema.descriptor_sha256;
        wrong_digest[0] ^= 0xff;
        assert!(registered_schema(
            "pyowl-core/not-the-frozen-schema",
            schema.version,
            schema.model_schema,
            &schema.descriptor_sha256,
        )
        .is_err());
        assert!(registered_schema(
            schema.name,
            schema.version + 1,
            schema.model_schema,
            &schema.descriptor_sha256,
        )
        .is_err());
        assert!(registered_schema(
            schema.name,
            schema.version,
            schema.model_schema + 1,
            &schema.descriptor_sha256,
        )
        .is_err());
        assert!(registered_schema(
            schema.name,
            schema.version,
            schema.model_schema,
            &wrong_digest,
        )
        .is_err());
        assert!(registered_schema(schema.name, schema.version, schema.model_schema, &[]).is_err());
    }

    #[test]
    fn direct_column_selection_requires_exact_scope_coordinates() {
        assert_eq!(
            encoded_selection("closure", None),
            Ok(TypedFacadeScopeV2::Closure)
        );
        assert_eq!(
            encoded_selection("document", Some(0)),
            Ok(TypedFacadeScopeV2::Document)
        );
        assert!(encoded_selection("closure", Some(0)).is_err());
        assert!(encoded_selection("document", None).is_err());
        assert!(encoded_selection("root", None).is_err());
    }
}
