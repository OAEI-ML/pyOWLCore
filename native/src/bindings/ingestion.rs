//! WP16-owned native ingestion registration seam.
//!
//! WP15 intentionally publishes no successor ingestion capability. WP16 may
//! add functions/classes and feature names in this module without editing the
//! shared module registry.

use pyo3::prelude::*;
use pyo3::types::PyModule;

pub(super) const FEATURES: &[&str] = &[];

pub(super) fn register(_py: Python<'_>, _module: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
