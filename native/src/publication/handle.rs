//! Exact sealed Python owners for retained document and snapshot storage.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyModule, PyTuple};

use super::records::{Digest, NativeSnapshotAttestationV1};
use super::PublicationStorageV1;

#[derive(Debug)]
struct HandleLifecycle {
    closed: AtomicBool,
}

impl HandleLifecycle {
    fn open() -> Self {
        Self {
            closed: AtomicBool::new(false),
        }
    }

    fn closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    fn close(&self) {
        self.closed.store(true, Ordering::Release);
    }
}

#[pyclass(
    module = "pyowl_core._native",
    frozen,
    name = "_NativeDocumentHandle",
    skip_from_py_object
)]
#[derive(Debug)]
pub(crate) struct NativeDocumentHandle {
    storage: Arc<PublicationStorageV1>,
    document_ordinal: u32,
    lifecycle: Arc<HandleLifecycle>,
}

impl NativeDocumentHandle {
    pub(super) fn from_storage(storage: Arc<PublicationStorageV1>, document_ordinal: u32) -> Self {
        Self {
            storage,
            document_ordinal,
            lifecycle: Arc::new(HandleLifecycle::open()),
        }
    }

    pub(crate) const fn document_ordinal(&self) -> u32 {
        self.document_ordinal
    }

    pub(crate) fn shares_storage_with(&self, snapshot: &NativeSnapshotHandle) -> bool {
        Arc::ptr_eq(&self.storage, &snapshot.storage)
    }

    pub(crate) fn closed(&self) -> bool {
        self.lifecycle.closed()
    }

    pub(crate) fn close(&self) {
        self.lifecycle.close();
    }
}

#[pymethods]
impl NativeDocumentHandle {
    fn _publication_closed_v1(&self) -> bool {
        self.closed()
    }

    fn _publication_close_v1(&self) {
        self.close();
    }
}

#[pyclass(
    module = "pyowl_core._native",
    frozen,
    name = "_NativeSnapshotHandle",
    skip_from_py_object
)]
#[derive(Debug)]
pub(crate) struct NativeSnapshotHandle {
    pub(super) storage: Arc<PublicationStorageV1>,
    lifecycle: Arc<HandleLifecycle>,
}

impl NativeSnapshotHandle {
    pub(super) fn from_storage(storage: Arc<PublicationStorageV1>) -> Self {
        Self {
            storage,
            lifecycle: Arc::new(HandleLifecycle::open()),
        }
    }

    pub(crate) fn document(&self, ordinal: u32) -> Option<NativeDocumentHandle> {
        (usize::try_from(ordinal).ok()? < self.storage.document_count())
            .then(|| NativeDocumentHandle::from_storage(Arc::clone(&self.storage), ordinal))
    }

    pub(crate) fn closed(&self) -> bool {
        self.lifecycle.closed()
    }

    pub(crate) fn close(&self) {
        self.lifecycle.close();
    }

    pub(crate) fn fork(&self) -> Self {
        Self::from_storage(Arc::clone(&self.storage))
    }

    pub(crate) fn storage(&self) -> &PublicationStorageV1 {
        &self.storage
    }
}

#[pymethods]
impl NativeSnapshotHandle {
    fn _publication_attestation_v1(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        attestation_to_python(py, self.storage.attestation())
    }

    fn _publication_closed_v1(&self) -> bool {
        self.closed()
    }

    fn _publication_close_v1(&self) {
        self.close();
    }
}

pub(crate) fn register_native_handle_types(
    py: Python<'_>,
    module: &Bound<'_, PyModule>,
) -> PyResult<()> {
    module.add_class::<NativeDocumentHandle>()?;
    module.add_class::<NativeSnapshotHandle>()?;
    let handoff = py.import("pyowl_core.backends.native_handoff")?;
    handoff.call_method1(
        "_register_rust_native_snapshot_handle_v1",
        (module.getattr("_NativeSnapshotHandle")?,),
    )?;
    Ok(())
}

fn attestation_to_python(
    py: Python<'_>,
    value: &NativeSnapshotAttestationV1,
) -> PyResult<Py<PyAny>> {
    let handoff = py.import("pyowl_core.backends.native_handoff")?;
    let record = handoff.getattr("NativeSnapshotAttestationV1")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("version", value.version)?;
    set_digest(&kwargs, "ledger_sha256", &value.ledger_sha256)?;
    set_digest(&kwargs, "root_table_sha256", &value.root_table_sha256)?;
    set_digest(
        &kwargs,
        "fingerprint_inputs_sha256",
        &value.fingerprint_inputs_sha256,
    )?;
    set_digest(
        &kwargs,
        "source_manifest_sha256",
        &value.source_manifest_sha256,
    )?;
    set_digest(
        &kwargs,
        "provenance_manifest_sha256",
        &value.provenance_manifest_sha256,
    )?;
    set_digest(
        &kwargs,
        "diagnostics_manifest_sha256",
        &value.diagnostics_manifest_sha256,
    )?;
    set_digest(&kwargs, "load_options_sha256", &value.load_options_sha256)?;
    set_digest(&kwargs, "report_sha256", &value.report_sha256)?;
    kwargs.set_item("document_count", value.document_count)?;
    kwargs.set_item("import_edge_count", value.import_edge_count)?;
    kwargs.set_item("diagnostic_count", value.diagnostic_count)?;
    kwargs.set_item("ontology_annotation_count", value.ontology_annotation_count)?;
    kwargs.set_item("stored_axiom_count", value.stored_axiom_count)?;
    kwargs.set_item("effective_axiom_count", value.effective_axiom_count)?;
    kwargs.set_item("extension_count", value.extension_count)?;
    kwargs.set_item("total_source_bytes", value.total_source_bytes)?;
    kwargs.set_item("source_map_entry_count", value.source_map_entry_count)?;
    kwargs.set_item("origin_entry_count", value.origin_entry_count)?;
    kwargs.set_item("rdf_mapping_report_count", value.rdf_mapping_report_count)?;
    kwargs.set_item("capability_bits", value.capability_bits)?;
    kwargs.set_item(
        "api_version",
        PyTuple::new(py, [value.api_version.0, value.api_version.1])?,
    )?;
    kwargs.set_item("model_schema", value.model_schema)?;
    kwargs.set_item("backend", &*value.backend)?;
    kwargs.set_item("root_document_key", &*value.root_document_key)?;
    kwargs.set_item("owl2_dl_validated", value.owl2_dl_validated)?;
    kwargs.set_item("owl2_dl_conforms", value.owl2_dl_conforms)?;
    match &value.owl2_dl_report_sha256 {
        Some(digest) => set_digest(&kwargs, "owl2_dl_report_sha256", digest)?,
        None => kwargs.set_item("owl2_dl_report_sha256", py.None())?,
    }
    Ok(record.call((), Some(&kwargs))?.unbind())
}

fn set_digest(mapping: &Bound<'_, PyDict>, name: &str, digest: &Digest) -> PyResult<()> {
    mapping.set_item(name, PyBytes::new(mapping.py(), digest))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_send_sync<T: Send + Sync>() {}

    #[test]
    fn owner_types_are_send_sync_for_concurrent_reads() {
        assert_send_sync::<NativeSnapshotHandle>();
        assert_send_sync::<NativeDocumentHandle>();
    }
}
