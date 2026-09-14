//! Receipts for checked native construction, never caller-supplied column tables.
//! The retained arena is validated at ingestion; the native column builder checks
//! selection, constructor/depth/size limits, canonical order and unique/reachable
//! nodes, then its checked direct writer fills exact immutable schema buffers.

use crate::publication::{NativeSnapshotHandle, PublicationStorageV2};
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict, PyMemoryView, PyModule};
use std::sync::Arc;

#[pyclass(module = "pyowl_core._native", frozen)]
struct NativeColumnValidationReceiptV1 {
    owner: Py<PyAny>,
    storage: Arc<PublicationStorageV2>,
    scope: String,
    ordinal: Option<u64>,
    config: Py<PyBytes>,
    buffers: Vec<(String, Py<PyAny>)>,
    fingerprint: Py<PyBytes>,
    canonical_work: u64,
    node_rows: u64,
    root_rows: u64,
}

#[pymethods]
impl NativeColumnValidationReceiptV1 {
    // Snapshot caches retain their views; expose the receipt's strong owner edge
    // so Python's cyclic collector can reclaim snapshot -> view -> receipt cycles.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.owner)?;
        visit.call(&self.config)?;
        visit.call(&self.fingerprint)?;
        for (_, buffer) in &self.buffers {
            visit.call(buffer)?;
        }
        Ok(())
    }

    fn _matches_v1(
        &self,
        py: Python<'_>,
        owner: &Bound<'_, PyAny>,
        scope: &str,
        ordinal: Option<u64>,
        buffers: &Bound<'_, PyDict>,
    ) -> PyResult<bool> {
        if !self.owner.bind(py).is(owner)
            || self.scope != scope
            || self.ordinal != ordinal
            || buffers.len() != self.buffers.len()
        {
            return Ok(false);
        }
        let raw_owner = owner
            .getattr("_native_snapshot_state")?
            .getattr("owner")?
            .getattr("handle")?
            .getattr("_owner_v2")?;
        let handle = raw_owner.extract::<PyRef<'_, NativeSnapshotHandle>>()?;
        if !Arc::ptr_eq(&self.storage, &handle.encoded_storage_v2(py)?) {
            return Ok(false);
        }
        for (name, expected) in &self.buffers {
            let Some(value) = buffers.get_item(name)? else {
                return Ok(false);
            };
            if !value.is(expected.bind(py)) || !value.is_instance_of::<PyMemoryView>() {
                return Ok(false);
            }
            // A released memoryview must not retain admission through its old identity.
            if !value.getattr("readonly")?.extract::<bool>()?
                || !value.getattr("obj")?.is_instance_of::<PyBytes>()
            {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn _same_limits_v1(&self, py: Python<'_>, config: &Bound<'_, PyBytes>) -> PyResult<bool> {
        let original = crate::limits_from_python(self.config.bind(py).as_any())?;
        let requested = crate::limits_from_python(config.as_any())?;
        Ok(original.same_structural_budget(&requested))
    }

    fn _fingerprint_v1(&self, py: Python<'_>) -> Py<PyBytes> {
        self.fingerprint.clone_ref(py)
    }

    fn _report_v1(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let report = PyDict::new(py);
        report.set_item("backend", "native")?;
        report.set_item("validation", "checked-retained-construction-v1")?;
        report.set_item("native_validation_passes", 1)?;
        report.set_item("canonical_work", self.canonical_work)?;
        report.set_item("node_rows", self.node_rows)?;
        report.set_item("root_rows", self.root_rows)?;
        Ok(report.unbind())
    }
}

#[pyfunction]
#[pyo3(signature = (handle, owner, scope, document_ordinal, config, cancel=None))]
fn _validated_encoded_structural_columns_v2(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
    owner: &Bound<'_, PyAny>,
    scope: &str,
    document_ordinal: Option<u64>,
    config: &Bound<'_, PyBytes>,
    cancel: Option<PyRef<'_, crate::cancel::Cancellation>>,
) -> PyResult<(Py<PyDict>, Py<PyDict>, NativeColumnValidationReceiptV1)> {
    let selected = super::views::encoded_selection(scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let limits = crate::limits_from_python(config.as_any())?;
    let storage = handle.encoded_storage_v2(py)?;
    let raw_owner = owner
        .getattr("_native_snapshot_state")?
        .getattr("owner")?
        .getattr("handle")?
        .getattr("_owner_v2")?;
    let owner_handle = raw_owner.extract::<PyRef<'_, NativeSnapshotHandle>>()?;
    if !Arc::ptr_eq(&storage, &owner_handle.encoded_storage_v2(py)?) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "foreign native receipt owner",
        ));
    }
    drop(owner_handle);
    drop(handle);
    let (buffers, counters) = super::views::encoded_columns_to_python(
        py,
        storage.as_ref(),
        selected,
        document_ordinal,
        false,
        &limits,
        crate::cancellation_or_default(cancel),
    )?;
    let read_count = |name| -> PyResult<u64> {
        counters
            .bind(py)
            .get_item(name)?
            .ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("native construction counter missing")
            })?
            .extract()
    };
    let receipt = NativeColumnValidationReceiptV1 {
        owner: owner.clone().unbind(),
        storage,
        scope: scope.to_owned(),
        ordinal: document_ordinal,
        config: config.clone().unbind(),
        buffers: buffers
            .bind(py)
            .iter()
            .map(|(name, value)| Ok((name.extract::<String>()?, value.unbind())))
            .collect::<PyResult<Vec<_>>>()?,
        fingerprint: direct_fingerprint(py, buffers.bind(py))?,
        canonical_work: read_count("canonical_work")?,
        node_rows: read_count("node_rows")?,
        root_rows: read_count("root_rows")?,
    };
    Ok((buffers, counters, receipt))
}

// hashlib performs each whole-buffer update in C; the bridge visits only the
// fixed schema header and eleven buffers. Store the digest once in the native
// receipt so downstream admission cannot trigger another ontology-sized hash.
fn frame_length(mut value: usize) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(10);
    loop {
        let low = (value & 0x7f) as u8;
        value >>= 7;
        bytes.push(if value == 0 { low } else { low | 0x80 });
        if value == 0 {
            return bytes;
        }
    }
}

fn direct_fingerprint(py: Python<'_>, buffers: &Bound<'_, PyDict>) -> PyResult<Py<PyBytes>> {
    let model = py.import("pyowl_core.backends.native_views")?;
    let hasher = py.import("hashlib")?.getattr("sha256")?.call0()?;
    let update = hasher.getattr("update")?;
    update.call1((PyBytes::new(py, b"pyowl-core:encoded-structural-view:v2\0"),))?;
    let descriptor = model.getattr("ENCODED_STRUCTURAL_DESCRIPTOR_V2")?;
    update.call1((PyBytes::new(py, &frame_length(descriptor.len()?)),))?;
    update.call1((&descriptor,))?;
    for name in [
        "root_kinds",
        "root_ids",
        "node_tags",
        "node_field_offsets",
        "field_kinds",
        "field_values",
        "field_lengths",
        "item_kinds",
        "item_values",
        "item_lengths",
        "scalar_bytes",
    ] {
        update.call1((PyBytes::new(py, &frame_length(name.len())),))?;
        update.call1((PyBytes::new(py, name.as_bytes()),))?;
        let buffer = buffers
            .get_item(name)?
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("missing native column"))?;
        update.call1((PyBytes::new(py, &(buffer.len()? as u64).to_le_bytes()),))?;
        update.call1((&buffer,))?;
    }
    update.call1((PyBytes::new(py, &1u64.to_le_bytes()),))?;
    update.call1((PyBytes::new(py, &[1, 0, 0, 0]),))?;
    update.call1((PyBytes::new(py, &[0; 16]),))?;
    Ok(hasher
        .call_method0("digest")?
        .cast_into::<PyBytes>()?
        .unbind())
}

#[pyfunction]
#[pyo3(signature = (handle, left_scope, left_ordinal, right_scope, right_ordinal))]
fn _encoded_scopes_equivalent_v1(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
    left_scope: &str,
    left_ordinal: Option<u64>,
    right_scope: &str,
    right_ordinal: Option<u64>,
) -> PyResult<bool> {
    let left = super::views::encoded_selection(left_scope, left_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let right = super::views::encoded_selection(right_scope, right_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    crate::run_detached(py, move |_interrupt| {
        storage.encoded_scopes_equivalent(left, left_ordinal, right, right_ordinal)
    })
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("NATIVE_VALIDATED_COLUMNS_API_VERSION", 1)?;
    module.add_class::<NativeColumnValidationReceiptV1>()?;
    module.add_function(wrap_pyfunction!(
        _validated_encoded_structural_columns_v2,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(_encoded_scopes_equivalent_v1, module)?)?;
    Ok(())
}
