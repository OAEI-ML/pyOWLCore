//! Exact sealed Python owners for retained document and snapshot storage.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict, PyInt, PyModule, PyTuple, PyType};

use super::facade_v2::PublicationStorageV2;
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

    fn close(&self) -> bool {
        !self.closed.swap(true, Ordering::AcqRel)
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
    storage_v1: Option<Arc<PublicationStorageV1>>,
    storage_v2: Option<Arc<PublicationStorageV2>>,
    document_ordinal: u64,
    lifecycle: Arc<HandleLifecycle>,
}

impl NativeDocumentHandle {
    pub(super) fn from_storage(storage: Arc<PublicationStorageV1>, document_ordinal: u32) -> Self {
        Self {
            storage_v1: Some(storage),
            storage_v2: None,
            document_ordinal: u64::from(document_ordinal),
            lifecycle: Arc::new(HandleLifecycle::open()),
        }
    }

    fn from_storage_v2(storage: Arc<PublicationStorageV2>, document_ordinal: u64) -> Self {
        Self {
            storage_v1: None,
            storage_v2: Some(storage),
            document_ordinal,
            lifecycle: Arc::new(HandleLifecycle::open()),
        }
    }

    pub(crate) const fn document_ordinal(&self) -> u64 {
        self.document_ordinal
    }

    pub(crate) fn shares_storage_with(&self, snapshot: &NativeSnapshotHandle) -> bool {
        self.storage_v1
            .as_ref()
            .zip(snapshot.storage_v1.as_ref())
            .is_some_and(|(document, owner)| Arc::ptr_eq(document, owner))
            || self
                .storage_v2
                .as_ref()
                .zip(snapshot.storage_v2.as_ref())
                .is_some_and(|(document, owner)| Arc::ptr_eq(document, owner))
    }

    pub(crate) fn closed(&self) -> bool {
        self.lifecycle.closed()
    }

    pub(crate) fn close(&self) -> bool {
        self.lifecycle.close()
    }

    pub(crate) fn encoded_storage_v2(&self, py: Python<'_>) -> PyResult<Arc<PublicationStorageV2>> {
        self.require_open_v2(py, "native V2 document handle is closed")?;
        self.storage_v2.as_ref().cloned().ok_or_else(|| {
            PyRuntimeError::new_err("native document owner has no typed V2 publication")
        })
    }

    pub(crate) fn counters_to_python_with_allocations(
        &self,
        py: Python<'_>,
        allocations: &mut crate::BridgeAllocationProbe,
    ) -> PyResult<Py<PyAny>> {
        self.require_v2()?
            .counters_to_python_with_allocations(py, allocations)
    }

    pub(crate) fn attestation_to_python_with_allocations(
        &self,
        py: Python<'_>,
        allocations: &mut crate::BridgeAllocationProbe,
    ) -> PyResult<Py<PyAny>> {
        self.require_v2()?
            .attestation_to_python_with_allocations(py, allocations)
    }
}

#[pymethods]
impl NativeDocumentHandle {
    fn _publication_closed_v1(&self) -> PyResult<bool> {
        self.require_v1()?;
        Ok(self.closed())
    }

    fn _publication_close_v1(&self) -> PyResult<()> {
        self.require_v1()?;
        self.close();
        Ok(())
    }

    fn _publication_attestation_v2(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let mut allocations = crate::BridgeAllocationProbe::disabled();
        self.attestation_to_python_with_allocations(py, &mut allocations)
    }

    fn _publication_closed_v2(&self) -> PyResult<bool> {
        self.require_v2()?;
        Ok(self.closed())
    }

    fn _publication_close_v2(&self) -> PyResult<()> {
        let storage = self.require_v2()?;
        storage.bump_close(self.close())
    }

    fn _publication_page_v2(
        &self,
        py: Python<'_>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let storage = self.require_v2()?;
        self.require_open_v2(py, "native V2 document handle is closed")?;
        storage.page_to_python(py, request, true, Some(self.document_ordinal))
    }

    fn _publication_contains_v2(
        &self,
        py: Python<'_>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let storage = self.require_v2()?;
        self.require_open_v2(py, "native V2 document handle is closed")?;
        storage.contains(py, request, true, Some(self.document_ordinal))
    }

    fn _publication_counters_v2(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let mut allocations = crate::BridgeAllocationProbe::disabled();
        self.counters_to_python_with_allocations(py, &mut allocations)
    }
}

impl NativeDocumentHandle {
    fn require_v1(&self) -> PyResult<&PublicationStorageV1> {
        self.storage_v1.as_deref().ok_or_else(|| {
            PyRuntimeError::new_err("native document owner does not contain a V1 publication")
        })
    }

    fn require_v2(&self) -> PyResult<&PublicationStorageV2> {
        self.storage_v2.as_deref().ok_or_else(|| {
            PyRuntimeError::new_err("native document owner does not contain a V2 publication")
        })
    }

    fn require_open_v2(&self, py: Python<'_>, message: &str) -> PyResult<()> {
        if self.closed() {
            Err(closed_snapshot_error(py, message)?)
        } else {
            Ok(())
        }
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
    storage_v1: Option<Arc<PublicationStorageV1>>,
    storage_v2: Option<Arc<PublicationStorageV2>>,
    lifecycle: Arc<HandleLifecycle>,
}

impl NativeSnapshotHandle {
    pub(super) fn from_storage(storage: Arc<PublicationStorageV1>) -> Self {
        Self {
            storage_v1: Some(storage),
            storage_v2: None,
            lifecycle: Arc::new(HandleLifecycle::open()),
        }
    }

    pub(super) fn from_storage_v2(storage: Arc<PublicationStorageV2>) -> Self {
        Self {
            storage_v1: None,
            storage_v2: Some(storage),
            lifecycle: Arc::new(HandleLifecycle::open()),
        }
    }

    pub(crate) fn document(&self, ordinal: u32) -> Option<NativeDocumentHandle> {
        let storage = self.storage_v1.as_ref()?;
        (usize::try_from(ordinal).ok()? < storage.document_count())
            .then(|| NativeDocumentHandle::from_storage(Arc::clone(storage), ordinal))
    }

    pub(crate) fn closed(&self) -> bool {
        self.lifecycle.closed()
    }

    pub(crate) fn close(&self) -> bool {
        self.lifecycle.close()
    }

    pub(crate) fn fork(&self) -> Self {
        Self {
            storage_v1: self.storage_v1.clone(),
            storage_v2: self.storage_v2.clone(),
            lifecycle: Arc::new(HandleLifecycle::open()),
        }
    }

    pub(crate) fn storage(&self) -> Option<&PublicationStorageV1> {
        self.storage_v1.as_deref()
    }

    pub(crate) fn encoded_storage_v2(&self, py: Python<'_>) -> PyResult<Arc<PublicationStorageV2>> {
        self.require_open_v2(py, "native V2 snapshot handle is closed")?;
        self.storage_v2.as_ref().cloned().ok_or_else(|| {
            PyRuntimeError::new_err("native snapshot owner has no typed V2 publication")
        })
    }

    pub(crate) fn counters_to_python_with_allocations(
        &self,
        py: Python<'_>,
        allocations: &mut crate::BridgeAllocationProbe,
    ) -> PyResult<Py<PyAny>> {
        self.require_v2()?
            .counters_to_python_with_allocations(py, allocations)
    }

    pub(crate) fn attestation_to_python_with_allocations(
        &self,
        py: Python<'_>,
        allocations: &mut crate::BridgeAllocationProbe,
    ) -> PyResult<Py<PyAny>> {
        self.require_v2()?
            .attestation_to_python_with_allocations(py, allocations)
    }
}

#[pymethods]
impl NativeSnapshotHandle {
    fn _publication_attestation_v1(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        attestation_to_python(py, self.require_v1()?.attestation())
    }

    fn _publication_closed_v1(&self) -> PyResult<bool> {
        self.require_v1()?;
        Ok(self.closed())
    }

    fn _publication_close_v1(&self) -> PyResult<()> {
        self.require_v1()?;
        self.close();
        Ok(())
    }

    fn _publication_attestation_v2(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let mut allocations = crate::BridgeAllocationProbe::disabled();
        self.attestation_to_python_with_allocations(py, &mut allocations)
    }

    fn _publication_closed_v2(&self) -> PyResult<bool> {
        self.require_v2()?;
        Ok(self.closed())
    }

    fn _publication_close_v2(&self) -> PyResult<()> {
        let storage = self.require_v2()?;
        storage.bump_close(self.close())
    }

    fn _publication_page_v2(
        &self,
        py: Python<'_>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let storage = self.require_v2()?;
        self.require_open_v2(py, "native V2 snapshot handle is closed")?;
        storage.page_to_python(py, request, false, None)
    }

    fn _publication_contains_v2(
        &self,
        py: Python<'_>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let storage = self.require_v2()?;
        self.require_open_v2(py, "native V2 snapshot handle is closed")?;
        storage.contains(py, request, false, None)
    }

    fn _publication_counters_v2(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let mut allocations = crate::BridgeAllocationProbe::disabled();
        self.counters_to_python_with_allocations(py, &mut allocations)
    }

    fn _publication_document_v2(
        &self,
        py: Python<'_>,
        document_ordinal: &Bound<'_, PyAny>,
    ) -> PyResult<NativeDocumentHandle> {
        if !document_ordinal
            .get_type()
            .is(document_ordinal.py().get_type::<PyInt>())
        {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "native V2 document ordinal must be an exact int",
            ));
        }
        let document_ordinal: u64 = document_ordinal.extract()?;
        self.document_v2(py, document_ordinal)
    }
}

impl NativeSnapshotHandle {
    fn document_v2(&self, py: Python<'_>, document_ordinal: u64) -> PyResult<NativeDocumentHandle> {
        let storage = self.require_v2()?;
        self.require_open_v2(py, "native V2 snapshot handle is closed")?;
        if document_ordinal >= storage.document_count() {
            return Err(PyValueError::new_err(
                "native V2 document ordinal is out of bounds",
            ));
        }
        Ok(NativeDocumentHandle::from_storage_v2(
            Arc::clone(
                self.storage_v2
                    .as_ref()
                    .ok_or_else(|| PyRuntimeError::new_err("missing native V2 storage"))?,
            ),
            document_ordinal,
        ))
    }

    fn require_v1(&self) -> PyResult<&PublicationStorageV1> {
        self.storage_v1.as_deref().ok_or_else(|| {
            PyRuntimeError::new_err("native snapshot owner does not contain a V1 publication")
        })
    }

    fn require_v2(&self) -> PyResult<&PublicationStorageV2> {
        self.storage_v2.as_deref().ok_or_else(|| {
            PyRuntimeError::new_err("native snapshot owner does not contain a V2 publication")
        })
    }

    fn require_open_v2(&self, py: Python<'_>, message: &str) -> PyResult<()> {
        if self.closed() {
            Err(closed_snapshot_error(py, message)?)
        } else {
            Ok(())
        }
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
    let handoff_v2 = py.import("pyowl_core.backends.native_handoff_v2")?;
    handoff_v2.call_method1(
        "_register_rust_native_snapshot_handle_v2",
        (module.getattr("_NativeSnapshotHandle")?,),
    )?;
    handoff_v2.call_method1(
        "_register_rust_native_document_handle_v2",
        (module.getattr("_NativeDocumentHandle")?,),
    )?;
    Ok(())
}

fn closed_snapshot_error(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    let exception = py
        .import("pyowl_core.exceptions")?
        .getattr("ClosedSnapshotError")?
        .cast_into::<PyType>()?;
    Ok(PyErr::from_type(exception, message.to_owned()))
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
    use std::sync::Barrier;
    use std::thread;

    use super::*;

    fn assert_send_sync<T: Send + Sync>() {}

    #[test]
    fn owner_types_are_send_sync_for_concurrent_reads() {
        assert_send_sync::<NativeSnapshotHandle>();
        assert_send_sync::<NativeDocumentHandle>();
    }

    #[test]
    fn v2_snapshot_and_document_owners_share_storage_but_not_lifecycle() {
        let storage = PublicationStorageV2::fixture_for_tests();
        let snapshot = NativeSnapshotHandle::from_storage_v2(Arc::clone(&storage));
        let document = NativeDocumentHandle::from_storage_v2(storage, 0);
        assert!(document.shares_storage_with(&snapshot));
        assert!(!snapshot.closed());
        assert!(!document.closed());
        assert!(document.close());
        assert!(!document.close());
        assert!(document.closed());
        assert!(!snapshot.closed());
    }

    #[test]
    fn v2_document_owner_preserves_ordinals_above_u32_without_allocating_documents() {
        let ordinal = u64::from(u32::MAX) + 1;
        let storage = PublicationStorageV2::fixture_for_tests_with_document_count(ordinal + 1);
        let snapshot = NativeSnapshotHandle::from_storage_v2(storage);
        Python::initialize();
        Python::attach(|py| {
            let document = snapshot
                .document_v2(py, ordinal)
                .expect("u64 document ordinal");
            assert_eq!(document.document_ordinal(), ordinal);
            assert!(document.shares_storage_with(&snapshot));
        });
    }

    #[test]
    fn concurrent_storage_reads_survive_an_independent_document_close() {
        let storage = PublicationStorageV2::fixture_for_tests();
        let snapshot = Arc::new(NativeSnapshotHandle::from_storage_v2(Arc::clone(&storage)));
        let closing_document = Arc::new(NativeDocumentHandle::from_storage_v2(
            Arc::clone(&storage),
            0,
        ));
        let surviving_document = Arc::new(NativeDocumentHandle::from_storage_v2(storage, 0));
        let start = Arc::new(Barrier::new(9));
        let readers: Vec<_> = (0..8)
            .map(|_| {
                let snapshot = Arc::clone(&snapshot);
                let document = Arc::clone(&surviving_document);
                let start = Arc::clone(&start);
                thread::spawn(move || {
                    start.wait();
                    for _ in 0..10_000 {
                        assert_eq!(
                            snapshot
                                .require_v2()
                                .expect("snapshot storage")
                                .document_count(),
                            1
                        );
                        assert_eq!(
                            document
                                .require_v2()
                                .expect("document storage")
                                .document_count(),
                            1
                        );
                        assert!(document.shares_storage_with(&snapshot));
                        assert!(!snapshot.closed());
                        assert!(!document.closed());
                    }
                })
            })
            .collect();
        let closer = {
            let document = Arc::clone(&closing_document);
            let start = Arc::clone(&start);
            thread::spawn(move || {
                start.wait();
                assert!(document.close());
                for _ in 0..10_000 {
                    assert!(!document.close());
                    assert!(document.closed());
                }
            })
        };

        for reader in readers {
            reader.join().expect("reader");
        }
        closer.join().expect("closer");
        assert!(closing_document.closed());
        assert!(!surviving_document.closed());
        assert!(!snapshot.closed());
        assert!(snapshot.close());
        assert!(snapshot.closed());
        assert!(!surviving_document.closed());
        assert_eq!(
            surviving_document
                .require_v2()
                .expect("surviving storage")
                .document_count(),
            1
        );
    }

    #[test]
    fn v1_and_v2_owner_instances_fail_closed_across_protocol_versions() {
        let publication = super::super::fixture::publication().expect("V1 publication");
        let v1 = publication.handle();
        let v2 = NativeSnapshotHandle::from_storage_v2(PublicationStorageV2::fixture_for_tests());
        assert!(v1.require_v1().is_ok());
        assert!(v1.require_v2().is_err());
        assert!(v2.require_v1().is_err());
        assert!(v2.require_v2().is_ok());
    }
}
