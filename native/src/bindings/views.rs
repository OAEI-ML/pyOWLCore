//! WP17-owned native view/index/wire registration seam.
//!
//! WP15 intentionally publishes no successor view capability. WP17 may add
//! functions/classes and feature names here without editing the shared module
//! registry or the ingestion module.

use pyo3::prelude::*;
use pyo3::types::PyModule;

pub(super) const FEATURES: &[&str] = &[];

pub(super) fn register(_py: Python<'_>, _module: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
