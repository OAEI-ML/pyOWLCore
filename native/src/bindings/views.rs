//! WP17-owned native view/index/wire registration seam.
//!
//! WP15 intentionally publishes no successor view capability. WP17 may add
//! functions/classes and feature names here without editing the shared module
//! registry or the ingestion module.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict, PyMemoryView, PyModule, PySlice, PyString, PyTuple};

use crate::error::NativeError;
#[cfg(any(test, feature = "test-hooks"))]
use crate::error::NativeResult;
use crate::publication::{
    NativeDocumentHandle, NativeSnapshotHandle, PublicationStorageV2, TypedFacadeScopeV2,
};

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

#[derive(Debug, Default)]
struct EncodedBridgeAllocationProbe {
    #[cfg(feature = "test-hooks")]
    fail_after: Option<u64>,
    #[cfg(feature = "test-hooks")]
    allocations: u64,
}

impl EncodedBridgeAllocationProbe {
    const fn disabled() -> Self {
        Self {
            #[cfg(feature = "test-hooks")]
            fail_after: None,
            #[cfg(feature = "test-hooks")]
            allocations: 0,
        }
    }

    #[cfg(feature = "test-hooks")]
    const fn configured(fail_after: Option<u64>) -> Self {
        Self {
            fail_after,
            allocations: 0,
        }
    }

    fn checkpoint(&mut self) -> PyResult<()> {
        #[cfg(feature = "test-hooks")]
        {
            if self
                .fail_after
                .is_some_and(|maximum| self.allocations >= maximum)
            {
                return Err(pyo3::exceptions::PyMemoryError::new_err(
                    "injected native encoded-view bridge allocation failure",
                ));
            }
            self.allocations = self.allocations.checked_add(1).ok_or_else(|| {
                pyo3::exceptions::PyMemoryError::new_err(
                    "native encoded-view bridge allocation counter overflow",
                )
            })?;
        }
        Ok(())
    }

    #[cfg(feature = "test-hooks")]
    const fn count(&self) -> u64 {
        self.allocations
    }
}

pub(super) const FEATURES: &[&str] = &[];

pub(super) fn register(_py: Python<'_>, _module: &Bound<'_, PyModule>) -> PyResult<()> {
    _module.add_class::<NativeRetainedAxiomTypeIndexV1>()?;
    _module.add_class::<NativeRetainedSignatureIndexV1>()?;
    _module.add_class::<NativeRetainedOntologyIdentityIndexV1>()?;
    _module.add_function(wrap_pyfunction!(_encoded_structural_columns_v1, _module)?)?;
    _module.add_function(wrap_pyfunction!(
        _encoded_structural_document_columns_v1,
        _module
    )?)?;
    _module.add_function(wrap_pyfunction!(_retained_axiom_type_index_v1, _module)?)?;
    _module.add_function(wrap_pyfunction!(_retained_signature_index_v1, _module)?)?;
    _module.add_function(wrap_pyfunction!(
        _retained_ontology_identity_index_v1,
        _module
    )?)?;
    #[cfg(feature = "test-hooks")]
    {
        _module.add_function(wrap_pyfunction!(_encoded_view_schema_v1, _module)?)?;
        _module.add_function(wrap_pyfunction!(_encoded_structural_fixture_v1, _module)?)?;
        _module.add_function(wrap_pyfunction!(
            _encoded_structural_bridge_allocation_probe_v1,
            _module
        )?)?;
        _module.add_function(wrap_pyfunction!(
            _encoded_structural_workspace_allocation_probe_v1,
            _module
        )?)?;
    }
    Ok(())
}

/// Private exact signature counts built over retained root identifiers. The
/// count rows follow the existing canonical signature facade table.
#[pyclass(
    module = "pyowl_core._native",
    frozen,
    name = "_NativeRetainedSignatureIndexV1",
    skip_from_py_object
)]
struct NativeRetainedSignatureIndexV1 {
    storage: Arc<crate::publication::PublicationStorageV2>,
    index: crate::index::RetainedSignatureIndexV1,
}

type PyRetainedSignatureLayoutV1 = (
    Py<PyBytes>,
    Py<PyBytes>,
    Py<PyTuple>,
    Py<PyTuple>,
    Py<PyTuple>,
    Py<PyDict>,
);

#[pymethods]
impl NativeRetainedSignatureIndexV1 {
    fn _layout_v1<'py>(&self, py: Python<'py>) -> PyResult<PyRetainedSignatureLayoutV1> {
        let (root_table_sha256, effective_root_table_sha256) =
            self.storage.retained_signature_binding_v1();
        let referenced = PyTuple::new(py, self.index.referenced_counts().iter().copied())?.unbind();
        let nonannotation =
            PyTuple::new(py, self.index.nonannotation_counts().iter().copied())?.unbind();
        let declarations =
            PyTuple::new(py, self.index.declaration_counts().iter().copied())?.unbind();
        let counters = self.index.counters();
        let observed = PyDict::new(py);
        for (name, value) in [
            ("structural_root_rows", counters.structural_root_rows),
            ("entity_rows", counters.entity_rows),
            ("referenced_links", counters.referenced_links),
            ("nonannotation_links", counters.nonannotation_links),
            ("declaration_links", counters.declaration_links),
            ("retained_buffer_bytes", counters.retained_buffer_bytes),
            ("peak_owned_bytes", counters.peak_owned_bytes),
            ("canonical_work", counters.canonical_work),
            (
                "complete_root_encode_calls",
                counters.complete_root_encode_calls,
            ),
        ] {
            observed.set_item(name, value)?;
        }
        Ok((
            PyBytes::new(py, root_table_sha256).unbind(),
            PyBytes::new(py, effective_root_table_sha256).unbind(),
            referenced,
            nonannotation,
            declarations,
            observed.unbind(),
        ))
    }
}

/// Count retained signature contributions without encoding complete roots or
/// crossing the scalar ontology iterators.
#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, config, cancel=None))]
fn _retained_signature_index_v1<'py>(
    py: Python<'py>,
    handle: PyRef<'py, NativeSnapshotHandle>,
    scope: &Bound<'py, PyAny>,
    document_ordinal: Option<u64>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<NativeRetainedSignatureIndexV1> {
    if !scope.get_type().is(py.get_type::<PyString>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "retained signature scope must be an exact str",
        ));
    }
    let scope: String = scope.extract()?;
    let selected_scope = encoded_selection(&scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let limits = crate::limits_from_python(config)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let owner = Arc::clone(&storage);
    let index = crate::run_detached(py, move |interrupt| {
        storage.retained_signature_index(
            selected_scope,
            document_ordinal,
            &limits,
            cancellation,
            Some(interrupt),
        )
    })?;
    Ok(NativeRetainedSignatureIndexV1 {
        storage: owner,
        index,
    })
}

/// Private O(1) owner for the identity/import/diagnostic readiness metadata
/// attested by the exact retained publication. No successor capability is
/// advertised until the installed-path matrix closes.
#[pyclass(
    module = "pyowl_core._native",
    frozen,
    name = "_NativeRetainedOntologyIdentityIndexV1",
    skip_from_py_object
)]
struct NativeRetainedOntologyIdentityIndexV1 {
    storage: Arc<crate::publication::PublicationStorageV2>,
}

type PyRetainedOntologyIdentityLayoutV1 = (
    Py<PyString>,
    Py<PyBytes>,
    Py<PyBytes>,
    Py<PyBytes>,
    Py<PyDict>,
);

#[pymethods]
impl NativeRetainedOntologyIdentityIndexV1 {
    fn _layout_v1<'py>(&self, py: Python<'py>) -> PyResult<PyRetainedOntologyIdentityLayoutV1> {
        let contract = self.storage.retained_ontology_identity_contract_v1();
        let counters = PyDict::new(py);
        for (name, value) in [
            ("document_count", contract.document_count),
            ("import_edge_count", contract.import_edge_count),
            ("diagnostic_count", contract.diagnostic_count),
            ("retained_owner_bytes", contract.retained_owner_bytes),
            ("complete_root_encode_calls", 0),
        ] {
            counters.set_item(name, value)?;
        }
        Ok((
            PyString::new(py, contract.root_document_key).unbind(),
            PyBytes::new(py, contract.metadata_manifest_sha256).unbind(),
            PyBytes::new(py, contract.diagnostics_manifest_sha256).unbind(),
            PyBytes::new(py, contract.report_sha256).unbind(),
            counters.unbind(),
        ))
    }
}

/// Retain the exact publication storage used by the public ontology identity
/// index without traversing or encoding structural roots.
#[pyfunction]
fn _retained_ontology_identity_index_v1(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
) -> PyResult<NativeRetainedOntologyIdentityIndexV1> {
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    Ok(NativeRetainedOntologyIdentityIndexV1 { storage })
}

/// Private owner for constructor postings built directly over retained arena
/// root identifiers. The class and operation remain absent from VIEW_FEATURES
/// until the complete installed consumer matrix closes.
#[pyclass(
    module = "pyowl_core._native",
    frozen,
    name = "_NativeRetainedAxiomTypeIndexV1",
    skip_from_py_object
)]
struct NativeRetainedAxiomTypeIndexV1 {
    storage: Arc<crate::publication::PublicationStorageV2>,
    index: Arc<crate::index::RetainedAxiomTypeIndexV1>,
}

type PyRetainedAxiomTypeLayoutV1 = (
    Py<PyTuple>,
    Py<PyTuple>,
    Py<PyTuple>,
    Py<PyTuple>,
    Py<PyTuple>,
    Py<PyDict>,
);

#[pymethods]
impl NativeRetainedAxiomTypeIndexV1 {
    fn _binding_v1<'py>(&self, py: Python<'py>) -> (Py<PyBytes>, Py<PyBytes>) {
        let (root_table_sha256, effective_root_table_sha256) =
            self.storage.retained_axiom_type_binding_v1();
        (
            PyBytes::new(py, root_table_sha256).unbind(),
            PyBytes::new(py, effective_root_table_sha256).unbind(),
        )
    }

    fn _canonical_sizes_v1<'py>(&self, py: Python<'py>) -> PyResult<Py<PyTuple>> {
        Ok(PyTuple::new(py, self.index.canonical_sizes().iter().copied())?.unbind())
    }

    fn _layout_v1<'py>(&self, py: Python<'py>) -> PyResult<PyRetainedAxiomTypeLayoutV1> {
        let tags = PyTuple::new(py, self.index.tags().iter().copied())?.unbind();
        let offsets = PyTuple::new(py, self.index.offsets().iter().copied())?.unbind();
        let category_codes =
            PyTuple::new(py, self.index.category_codes().iter().copied())?.unbind();
        let category_offsets =
            PyTuple::new(py, self.index.category_offsets().iter().copied())?.unbind();
        let postings = PyTuple::new(py, self.index.postings().iter().copied())?.unbind();
        let counters = self.index.counters();
        let observed = PyDict::new(py);
        for (name, value) in [
            ("axiom_rows", counters.axiom_rows),
            ("constructor_groups", counters.constructor_groups),
            ("category_groups", counters.category_groups),
            ("retained_buffer_bytes", counters.retained_buffer_bytes),
            ("peak_owned_bytes", counters.peak_owned_bytes),
            ("canonical_work", counters.canonical_work),
            (
                "complete_root_encode_calls",
                self.index.complete_root_encode_calls(),
            ),
        ] {
            observed.set_item(name, value)?;
        }
        Ok((
            tags,
            offsets,
            category_codes,
            category_offsets,
            postings,
            observed.unbind(),
        ))
    }

    #[pyo3(signature = (tag, start, max_rows, max_bytes, config, cancel=None))]
    #[allow(clippy::too_many_arguments)]
    fn _page_v1<'py>(
        &self,
        py: Python<'py>,
        tag: u16,
        start: u64,
        max_rows: u32,
        max_bytes: u64,
        config: &Bound<'py, PyAny>,
        cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
    ) -> PyResult<(Py<PyTuple>, u64, Option<u64>)> {
        let limits = crate::limits_from_python(config)?;
        let cancellation = crate::cancellation_or_default(cancel);
        let index = Arc::clone(&self.index);
        let page = crate::run_detached(py, move |interrupt| {
            index.constructor_page(
                tag,
                start,
                max_rows,
                max_bytes,
                &limits,
                cancellation,
                Some(interrupt),
            )
        })?;
        let rows = PyTuple::new(
            py,
            page.rows.iter().map(|row| PyBytes::new(py, row).unbind()),
        )?
        .unbind();
        Ok((rows, page.total_count, page.next_cursor))
    }
}

/// Build constructor/category postings without encoding retained roots or
/// crossing the scalar Python ontology iterator before native construction.
#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, config, cancel=None))]
fn _retained_axiom_type_index_v1<'py>(
    py: Python<'py>,
    handle: PyRef<'py, NativeSnapshotHandle>,
    scope: &Bound<'py, PyAny>,
    document_ordinal: Option<u64>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, crate::cancel::Cancellation>>,
) -> PyResult<NativeRetainedAxiomTypeIndexV1> {
    if !scope.get_type().is(py.get_type::<PyString>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "retained axiom-type scope must be an exact str",
        ));
    }
    let scope: String = scope.extract()?;
    let selected_scope = encoded_selection(&scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let limits = crate::limits_from_python(config)?;
    let cancellation = crate::cancellation_or_default(cancel);
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let owner = Arc::clone(&storage);
    let index = crate::run_detached(py, move |interrupt| {
        storage.retained_axiom_type_index(
            selected_scope,
            document_ordinal,
            false,
            &limits,
            cancellation,
            Some(interrupt),
        )
    })?;
    Ok(NativeRetainedAxiomTypeIndexV1 {
        storage: owner,
        index: Arc::new(index),
    })
}

/// Exercise raw document-owner selection without relaxing the snapshot
/// operation's effective-scope semantics.
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
    encoded_columns_to_python(
        py,
        storage.as_ref(),
        TypedFacadeScopeV2::Document,
        Some(document_ordinal),
        true,
        &limits,
        cancellation,
    )
}

/// Exercise the direct retained-column path through an open V2 snapshot owner.
/// The private operation remains unadvertised until the installed-wheel
/// lifetime/copy matrix permits exposing the frozen view capability.
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
    encoded_columns_to_python(
        py,
        storage.as_ref(),
        selected_scope,
        document_ordinal,
        false,
        &limits,
        cancellation,
    )
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, config, fail_after=None))]
fn _encoded_structural_bridge_allocation_probe_v1<'py>(
    py: Python<'py>,
    handle: PyRef<'py, NativeSnapshotHandle>,
    scope: &Bound<'py, PyAny>,
    document_ordinal: Option<u64>,
    config: &Bound<'py, PyAny>,
    fail_after: Option<u64>,
) -> PyResult<(Py<PyDict>, Py<PyDict>, u64)> {
    if !scope.get_type().is(py.get_type::<PyString>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "encoded structural scope must be an exact str",
        ));
    }
    let scope: String = scope.extract()?;
    let selected_scope = encoded_selection(&scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let limits = crate::limits_from_python(config)?;
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let mut allocations = EncodedBridgeAllocationProbe::configured(fail_after);
    let (buffers, counters) = encoded_columns_to_python_with_allocations(
        py,
        storage.as_ref(),
        selected_scope,
        document_ordinal,
        false,
        &limits,
        crate::cancel::Cancellation::with_duration(None),
        &mut allocations,
    )?;
    Ok((buffers, counters, allocations.count()))
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, config, fail_after=None))]
fn _encoded_structural_workspace_allocation_probe_v1<'py>(
    py: Python<'py>,
    handle: PyRef<'py, NativeSnapshotHandle>,
    scope: &Bound<'py, PyAny>,
    document_ordinal: Option<u64>,
    config: &Bound<'py, PyAny>,
    fail_after: Option<u64>,
) -> PyResult<(Py<PyDict>, Py<PyDict>, u64)> {
    if !scope.get_type().is(py.get_type::<PyString>()) {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "encoded structural scope must be an exact str",
        ));
    }
    let scope: String = scope.extract()?;
    let selected_scope = encoded_selection(&scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let limits = crate::limits_from_python(config)?;
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let worker_storage = storage.as_ref();
    let prepared = crate::run_detached(py, move |interrupt| {
        worker_storage.prepare_encoded_structural_columns_with_allocation_probe(
            selected_scope,
            document_ordinal,
            false,
            &limits,
            crate::cancel::Cancellation::with_duration(None),
            Some(interrupt),
            fail_after,
        )
    })?;
    let allocation_count = prepared.workspace_allocation_count();
    let mut bridge_allocations = EncodedBridgeAllocationProbe::disabled();
    let (buffers, counters) = encoded_prepared_columns_to_python(
        py,
        storage.as_ref(),
        prepared,
        &mut bridge_allocations,
    )?;
    Ok((buffers, counters, allocation_count))
}

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

fn encoded_columns_to_python(
    py: Python<'_>,
    storage: &PublicationStorageV2,
    scope: TypedFacadeScopeV2,
    document_ordinal: Option<u64>,
    raw_document_owner: bool,
    limits: &crate::limits::Limits,
    cancellation: crate::cancel::Cancellation,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    let mut allocations = EncodedBridgeAllocationProbe::disabled();
    encoded_columns_to_python_with_allocations(
        py,
        storage,
        scope,
        document_ordinal,
        raw_document_owner,
        limits,
        cancellation,
        &mut allocations,
    )
}

#[allow(clippy::too_many_arguments)]
fn encoded_columns_to_python_with_allocations(
    py: Python<'_>,
    storage: &PublicationStorageV2,
    scope: TypedFacadeScopeV2,
    document_ordinal: Option<u64>,
    raw_document_owner: bool,
    limits: &crate::limits::Limits,
    cancellation: crate::cancel::Cancellation,
    allocations: &mut EncodedBridgeAllocationProbe,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    let prepared = crate::run_detached(py, move |interrupt| {
        storage.prepare_encoded_structural_columns(
            scope,
            document_ordinal,
            raw_document_owner,
            limits,
            cancellation,
            Some(interrupt),
        )
    })?;
    encoded_prepared_columns_to_python(py, storage, prepared, allocations)
}

fn encoded_prepared_columns_to_python(
    py: Python<'_>,
    storage: &PublicationStorageV2,
    prepared: crate::model::PreparedEncodedStructuralColumnsV1<'_>,
    allocations: &mut EncodedBridgeAllocationProbe,
) -> PyResult<(Py<PyDict>, Py<PyDict>)> {
    let layout = prepared.layout();
    let total_bytes = layout.total_bytes();
    isize::try_from(total_bytes).map_err(|_| {
        crate::python_error(NativeError::limit(
            "native encoded-column Python owner exceeds Py_ssize_t",
        ))
    })?;
    allocations.checkpoint()?;
    let backing = PyBytes::new_with(py, total_bytes, |output| {
        py.detach(|| crate::contain(|| prepared.write_into(output)))
            .map_err(crate::python_error)
    })?;
    allocations.checkpoint()?;
    let owner_view = PyMemoryView::from(backing.as_any())?;
    allocations.checkpoint()?;
    let buffers = PyDict::new(py);
    for (name, start, length) in layout.named_ranges() {
        let stop = start.checked_add(length).ok_or_else(|| {
            crate::python_error(NativeError::limit(
                "native encoded-column Python range overflow",
            ))
        })?;
        let start = isize::try_from(start).map_err(|_| {
            crate::python_error(NativeError::limit(
                "native encoded-column Python range exceeds Py_ssize_t",
            ))
        })?;
        let stop = isize::try_from(stop).map_err(|_| {
            crate::python_error(NativeError::limit(
                "native encoded-column Python range exceeds Py_ssize_t",
            ))
        })?;
        allocations.checkpoint()?;
        let slice = PySlice::new(py, start, stop, 1);
        allocations.checkpoint()?;
        let view = owner_view.get_item(slice)?;
        allocations.checkpoint()?;
        buffers.set_item(name, view)?;
    }
    let counters = prepared.counters();
    allocations.checkpoint()?;
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
        // All eleven observations are read-only memoryview slices over the
        // one Python bytes allocation filled directly by Rust.
        ("python_bridge_copy_bytes", 0),
    ] {
        allocations.checkpoint()?;
        observed.set_item(name, value)?;
    }
    storage
        .record_encoded_view_success()
        .map_err(crate::python_error)?;
    Ok((buffers.unbind(), observed.unbind()))
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
fn _encoded_structural_fixture_v1() -> PyResult<NativeSnapshotHandle> {
    crate::publication::encoded_fixture_handle_v2().map_err(crate::python_error)
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
