//! WP17-owned native view/index/wire registration seam.
//!
//! WP15 intentionally publishes no successor view capability. WP17 may add
//! functions/classes and feature names here without editing the shared module
//! registry or the ingestion module.

use pyo3::prelude::*;
use pyo3::types::PyModule;

#[cfg(feature = "test-hooks")]
use pyo3::types::PyBytes;

#[cfg(any(test, feature = "test-hooks"))]
use crate::error::{NativeError, NativeResult};

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
    _module.add_function(wrap_pyfunction!(_encoded_view_schema_v1, _module)?)?;
    Ok(())
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
}
