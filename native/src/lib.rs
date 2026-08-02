//! Private PyO3 boundary for the Java-free pyowl-core native foundation.

#![forbid(unsafe_code)]
#![deny(clippy::all)]
#![cfg_attr(
    all(feature = "comparator", not(feature = "extension-module")),
    allow(dead_code, unused_imports)
)]

#[cfg(feature = "extension-module")]
mod bindings;
mod cancel;
mod canonical;
#[cfg(feature = "comparator")]
pub mod comparator;
mod error;
mod hash;
mod index;
#[allow(dead_code)]
mod ingestion;
mod limits;
mod model;
mod parse;
#[cfg(feature = "process-allocator-test")]
#[doc(hidden)]
pub mod process_allocator_test;
#[allow(dead_code)]
mod publication;
mod session;
mod source;
mod wire;

#[cfg(feature = "extension-module")]
use std::panic::{catch_unwind, AssertUnwindSafe};
#[cfg(feature = "extension-module")]
use std::sync::Arc;

#[cfg(feature = "extension-module")]
use cancel::{interrupt_slot, take_interrupt, Cancellation, Guard, InterruptSlot};
#[cfg(feature = "extension-module")]
use error::{NativeError, NativeErrorPayload, NativeNumber, NativeResult};
#[cfg(feature = "extension-module")]
use limits::Limits;
#[cfg(all(feature = "extension-module", feature = "test-hooks"))]
use model::NativeComponentBuilder;
#[cfg(feature = "extension-module")]
use model::{scan_canonical, CanonicalRow, ModelArena, ScanBudget};
#[cfg(feature = "extension-module")]
use pyo3::create_exception;
#[cfg(feature = "extension-module")]
use pyo3::exceptions::{PyException, PyTypeError};
#[cfg(all(feature = "extension-module", feature = "test-hooks"))]
use pyo3::exceptions::{PyMemoryError, PyValueError};
#[cfg(feature = "extension-module")]
use pyo3::prelude::*;
#[cfg(feature = "extension-module")]
use pyo3::types::{PyAny, PyBytes, PyDict, PyModule, PyTuple};
#[cfg(feature = "extension-module")]
use wire::WireArena;

pub(crate) const ABI_VERSION: u32 = 3;
#[cfg(feature = "extension-module")]
const MODEL_SCHEMA_VERSION: u32 = 2;
#[cfg(feature = "extension-module")]
const WIRE_FORMAT_VERSION: (u16, u16) = (1, 2);
#[cfg(feature = "extension-module")]
const FOUNDATION_FEATURES: [&str; 9] = [
    "cancellation",
    "canonical-model-v2",
    "deadlines",
    "gil-release",
    "index-axiom-types-v1",
    "owned-buffers",
    "panic-containment",
    "safe-rust",
    "wire-v1",
];

#[cfg(feature = "extension-module")]
create_exception!(_native, _NativeError, PyException);

#[cfg(feature = "extension-module")]
#[derive(Debug)]
struct BridgeAllocationProbe {
    #[cfg(feature = "test-hooks")]
    fail_after: Option<u64>,
    #[cfg(feature = "test-hooks")]
    allocations: u64,
    #[cfg(feature = "test-hooks")]
    failure_message: &'static str,
}

#[cfg(feature = "extension-module")]
impl BridgeAllocationProbe {
    const fn disabled() -> Self {
        Self {
            #[cfg(feature = "test-hooks")]
            fail_after: None,
            #[cfg(feature = "test-hooks")]
            allocations: 0,
            #[cfg(feature = "test-hooks")]
            failure_message: "",
        }
    }

    #[cfg(feature = "test-hooks")]
    const fn configured(fail_after: Option<u64>, failure_message: &'static str) -> Self {
        Self {
            fail_after,
            allocations: 0,
            failure_message,
        }
    }

    fn checkpoint(&mut self) -> PyResult<()> {
        #[cfg(feature = "test-hooks")]
        {
            if self
                .fail_after
                .is_some_and(|maximum| self.allocations >= maximum)
            {
                return Err(PyMemoryError::new_err(self.failure_message));
            }
            self.allocations = self.allocations.checked_add(1).ok_or_else(|| {
                PyMemoryError::new_err("native bridge allocation counter overflow")
            })?;
        }
        Ok(())
    }

    #[cfg(feature = "test-hooks")]
    const fn count(&self) -> u64 {
        self.allocations
    }
}

#[cfg(feature = "extension-module")]
fn contain<T>(operation: impl FnOnce() -> NativeResult<T>) -> NativeResult<T> {
    catch_unwind(AssertUnwindSafe(operation)).unwrap_or_else(|_| Err(NativeError::panic()))
}

#[cfg(feature = "extension-module")]
fn run_detached<T: Send>(
    py: Python<'_>,
    operation: impl FnOnce(InterruptSlot) -> NativeResult<T> + Send,
) -> PyResult<T> {
    let interrupt = interrupt_slot();
    let worker_interrupt = Arc::clone(&interrupt);
    let result = py.detach(move || contain(|| operation(worker_interrupt)));
    if let Some(error) = take_interrupt(&interrupt).map_err(python_error)? {
        return Err(error);
    }
    result.map_err(python_error)
}

#[cfg(feature = "extension-module")]
fn python_error(error: NativeError) -> PyErr {
    let NativeError {
        code,
        message,
        payload,
    } = error;
    match payload {
        NativeErrorPayload::None => PyErr::new::<_NativeError, _>((code, message)),
        NativeErrorPayload::ResourceLimit(resource) => Python::attach(|py| {
            let payload = PyDict::new(py);
            if let Err(error) = payload.set_item("kind", "resource_limit") {
                return error;
            }
            if let Err(error) = payload.set_item("limit", resource.limit) {
                return error;
            }
            let observed = match resource.observed {
                NativeNumber::Integer(value) => payload.set_item("observed", value),
                NativeNumber::FloatBits(bits) => payload.set_item("observed", f64::from_bits(bits)),
            };
            if let Err(error) = observed {
                return error;
            }
            let allowed = match resource.allowed {
                NativeNumber::Integer(value) => payload.set_item("allowed", value),
                NativeNumber::FloatBits(bits) => payload.set_item("allowed", f64::from_bits(bits)),
            };
            if let Err(error) = allowed {
                return error;
            }
            let details = PyDict::new(py);
            let observations = resource.details;
            for (key, value) in [
                ("component_count", observations.component_count),
                (
                    "largest_component_labels",
                    observations.largest_component_labels,
                ),
                (
                    "largest_component_arcs",
                    observations.largest_component_arcs,
                ),
                ("refinement_rounds", observations.refinement_rounds),
            ] {
                if let Some(value) = value {
                    if let Err(error) = details.set_item(key, value) {
                        return error;
                    }
                }
            }
            if let Some(value) = observations.work_term {
                if let Err(error) = details.set_item("work_term", value) {
                    return error;
                }
            }
            if let Err(error) = payload.set_item("details", details) {
                return error;
            }
            PyErr::new::<_NativeError, _>((code, message, payload.unbind()))
        }),
        NativeErrorPayload::Opaque { kind, data } => Python::attach(|py| {
            const MAX_OPAQUE_ERROR_BYTES: usize = 4 * 1024 * 1024;
            if data.len() > MAX_OPAQUE_ERROR_BYTES {
                return PyErr::new::<_NativeError, _>((
                    "NATIVE_PROTOCOL",
                    "native error payload exceeds the boundary limit",
                ));
            }
            let payload = PyDict::new(py);
            if let Err(error) = payload.set_item("kind", kind) {
                return error;
            }
            if let Err(error) = payload.set_item("data", PyBytes::new(py, &data)) {
                return error;
            }
            PyErr::new::<_NativeError, _>((code, message, payload.unbind()))
        }),
    }
}

#[cfg(feature = "extension-module")]
fn limits_from_python(config: &Bound<'_, PyAny>) -> PyResult<Limits> {
    let mut allocations = BridgeAllocationProbe::disabled();
    limits_from_python_with_allocations(config, &mut allocations)
}

#[cfg(feature = "extension-module")]
fn limits_from_python_with_allocations(
    config: &Bound<'_, PyAny>,
    allocations: &mut BridgeAllocationProbe,
) -> PyResult<Limits> {
    let bytes = owned_buffer_with_allocations(config.py(), config, None, true, allocations)?;
    contain(|| Limits::decode(&bytes)).map_err(python_error)
}

#[cfg(feature = "extension-module")]
fn cancellation_or_default(value: Option<PyRef<'_, Cancellation>>) -> Cancellation {
    value.map_or_else(|| Cancellation::with_duration(None), |item| item.clone())
}

#[cfg(feature = "extension-module")]
fn owned_buffer(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: Option<&Limits>,
    configuration: bool,
) -> PyResult<Vec<u8>> {
    let mut allocations = BridgeAllocationProbe::disabled();
    owned_buffer_with_allocations(py, value, limits, configuration, &mut allocations)
}

#[cfg(feature = "extension-module")]
fn owned_buffer_with_allocations(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: Option<&Limits>,
    configuration: bool,
    allocations: &mut BridgeAllocationProbe,
) -> PyResult<Vec<u8>> {
    allocations.checkpoint()?;
    let builtins = py.import("builtins")?;
    allocations.checkpoint()?;
    let memoryview = builtins.getattr("memoryview")?;
    allocations.checkpoint()?;
    let view = memoryview.call1((value,)).map_err(|error| {
        if error.is_instance_of::<PyTypeError>(py) {
            PyTypeError::new_err(if configuration {
                "native config must expose a byte buffer"
            } else {
                "native input must expose a byte buffer"
            })
        } else {
            error
        }
    })?;
    allocations.checkpoint()?;
    let nbytes: usize = view.getattr("nbytes")?.extract()?;
    if configuration && nbytes != 0 && nbytes != limits::CONFIG_BYTES {
        return Err(python_error(NativeError::protocol(
            "invalid native limits framing",
        )));
    }
    if let Some(selected) = limits {
        contain(|| selected.check_source_size(nbytes)).map_err(python_error)?;
    }
    allocations.checkpoint()?;
    let owned = view.call_method0("tobytes")?;
    let bytes = owned.cast::<PyBytes>()?.as_bytes();
    let mut result = Vec::new();
    if !bytes.is_empty() {
        allocations.checkpoint()?;
    }
    result
        .try_reserve_exact(bytes.len())
        .map_err(|_| python_error(NativeError::limit("native owned-buffer allocation failed")))?;
    result.extend_from_slice(bytes);
    Ok(result)
}

#[cfg(feature = "extension-module")]
fn owned_source_request(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: &Limits,
) -> PyResult<Vec<u8>> {
    let mut allocations = BridgeAllocationProbe::disabled();
    owned_source_request_with_allocations(py, value, limits, &mut allocations)
}

#[cfg(feature = "extension-module")]
fn owned_source_request_with_allocations(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: &Limits,
    allocations: &mut BridgeAllocationProbe,
) -> PyResult<Vec<u8>> {
    allocations.checkpoint()?;
    let builtins = py.import("builtins")?;
    allocations.checkpoint()?;
    let memoryview = builtins.getattr("memoryview")?;
    allocations.checkpoint()?;
    let view = memoryview.call1((value,)).map_err(|error| {
        if error.is_instance_of::<PyTypeError>(py) {
            PyTypeError::new_err("native parser request must expose a byte buffer")
        } else {
            error
        }
    })?;
    allocations.checkpoint()?;
    let nbytes: usize = view.getattr("nbytes")?.extract()?;
    let maximum = usize::try_from(limits.max_source_bytes)
        .unwrap_or(usize::MAX)
        .saturating_add(source::HEADER_BYTES);
    if nbytes > maximum {
        let observed = u64::try_from(nbytes.saturating_sub(source::HEADER_BYTES))
            .map_err(|_| python_error(NativeError::limit("native source size exceeds u64")))?;
        return Err(python_error(limits.resource_limit(
            limits::LimitKey::MaxSourceBytes,
            observed,
            "native parser request exceeds max_source_bytes",
        )));
    }
    let observed = u64::try_from(nbytes)
        .map_err(|_| python_error(NativeError::limit("native request size exceeds u64")))?;
    if let Some(maximum) = limits
        .max_memory_bytes
        .filter(|maximum| observed > *maximum)
    {
        return Err(python_error(NativeError::resource_limit(
            "max_memory_bytes",
            observed,
            maximum,
            "native parser request exceeds max_memory_bytes",
        )));
    }
    allocations.checkpoint()?;
    let owned = view.call_method0("tobytes")?;
    let bytes = owned.cast::<PyBytes>()?.as_bytes();
    let mut result = Vec::new();
    if !bytes.is_empty() {
        allocations.checkpoint()?;
    }
    result
        .try_reserve_exact(bytes.len())
        .map_err(|_| python_error(NativeError::limit("native owned-buffer allocation failed")))?;
    result.extend_from_slice(bytes);
    Ok(result)
}

#[cfg(feature = "extension-module")]
fn owned_index_request(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let mut allocations = BridgeAllocationProbe::disabled();
    owned_index_request_with_allocations(py, value, &mut allocations)
}

#[cfg(feature = "extension-module")]
fn owned_index_request_with_allocations(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    allocations: &mut BridgeAllocationProbe,
) -> PyResult<Vec<u8>> {
    allocations.checkpoint()?;
    let builtins = py.import("builtins")?;
    allocations.checkpoint()?;
    let memoryview = builtins.getattr("memoryview")?;
    allocations.checkpoint()?;
    let view = memoryview.call1((value,)).map_err(|error| {
        if error.is_instance_of::<PyTypeError>(py) {
            PyTypeError::new_err("native index request must expose a byte buffer")
        } else {
            error
        }
    })?;
    allocations.checkpoint()?;
    let nbytes: usize = view.getattr("nbytes")?.extract()?;
    if nbytes != 8 + limits::CONFIG_BYTES {
        return Err(python_error(NativeError::protocol(
            "invalid native index request framing",
        )));
    }
    allocations.checkpoint()?;
    let owned = view.call_method0("tobytes")?;
    let bytes = owned.cast::<PyBytes>()?.as_bytes();
    let mut result = Vec::new();
    if !bytes.is_empty() {
        allocations.checkpoint()?;
    }
    result
        .try_reserve_exact(bytes.len())
        .map_err(|_| python_error(NativeError::limit("native owned-buffer allocation failed")))?;
    result.extend_from_slice(bytes);
    Ok(result)
}

#[cfg(feature = "extension-module")]
fn owned_index_source(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: &Limits,
) -> PyResult<Vec<u8>> {
    let mut allocations = BridgeAllocationProbe::disabled();
    owned_index_source_with_allocations(py, value, limits, &mut allocations)
}

#[cfg(feature = "extension-module")]
fn owned_index_source_with_allocations(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: &Limits,
    allocations: &mut BridgeAllocationProbe,
) -> PyResult<Vec<u8>> {
    allocations.checkpoint()?;
    let builtins = py.import("builtins")?;
    allocations.checkpoint()?;
    let memoryview = builtins.getattr("memoryview")?;
    allocations.checkpoint()?;
    let view = memoryview.call1((value,)).map_err(|error| {
        if error.is_instance_of::<PyTypeError>(py) {
            PyTypeError::new_err("native index source must expose a byte buffer")
        } else {
            error
        }
    })?;
    allocations.checkpoint()?;
    let nbytes: usize = view.getattr("nbytes")?.extract()?;
    let observed = u64::try_from(nbytes)
        .map_err(|_| python_error(NativeError::limit("native index size exceeds u64")))?;
    if observed > limits.value(limits::LimitKey::MaxIndexBytes) {
        return Err(python_error(limits.resource_limit(
            limits::LimitKey::MaxIndexBytes,
            observed,
            "native index source exceeds max_index_bytes",
        )));
    }
    if let Some(maximum) = limits
        .max_memory_bytes
        .filter(|maximum| observed > *maximum)
    {
        return Err(python_error(NativeError::resource_limit(
            "max_memory_bytes",
            observed,
            maximum,
            "native index source exceeds max_memory_bytes",
        )));
    }
    allocations.checkpoint()?;
    let owned = view.call_method0("tobytes")?;
    let bytes = owned.cast::<PyBytes>()?.as_bytes();
    let mut result = Vec::new();
    if !bytes.is_empty() {
        allocations.checkpoint()?;
    }
    result
        .try_reserve_exact(bytes.len())
        .map_err(|_| python_error(NativeError::limit("native owned-buffer allocation failed")))?;
    result.extend_from_slice(bytes);
    Ok(result)
}

#[cfg(feature = "extension-module")]
#[pyfunction]
fn version() -> (&'static str, u32) {
    (env!("CARGO_PKG_VERSION"), ABI_VERSION)
}

#[cfg(feature = "extension-module")]
#[pyfunction]
fn self_test() -> PyResult<()> {
    contain(|| {
        let limits = Limits::decode(&[])?;
        let mut budget = ScanBudget::from_limits(&limits);
        let iri = vec![1, 2, 5, b'u', b'r', b'n', b':', b'x'];
        let category = scan_canonical(&iri, &mut budget)?;
        if !matches!(category, model::Category::Iri) {
            return Err(NativeError::protocol("native canonical self-test failed"));
        }
        let mut arena = ModelArena::default();
        arena.try_push(CanonicalRow {
            category,
            bytes: iri,
        })?;
        let receipt = wire::Validation {
            minor: 0,
            feature_flags: 0,
            total_length: 0,
            file_digest: [0; 32],
            section_count: 0,
            total_rows: 0,
        }
        .receipt()?;
        if receipt.len() != wire::RECEIPT_BYTES {
            return Err(NativeError::protocol("native wire self-test failed"));
        }
        Ok(())
    })
    .map_err(python_error)
}

#[cfg(feature = "extension-module")]
#[pyfunction]
#[pyo3(signature = (canonical, config, cancel=None))]
fn validate_canonical<'py>(
    py: Python<'py>,
    canonical: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, Cancellation>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let limits = limits_from_python(config)?;
    let owned = owned_buffer(py, canonical, Some(&limits), false)?;
    contain(|| limits.check_output_size(owned.len(), owned.len())).map_err(python_error)?;
    let cancellation = cancellation_or_default(cancel);
    let output = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        guard.check(0, true)?;
        let mut budget = ScanBudget::from_limits(&limits);
        scan_canonical(&owned, &mut budget)?;
        guard.check(1, true)?;
        Ok(owned)
    })?;
    PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })
}

#[cfg(feature = "extension-module")]
#[pyfunction]
#[pyo3(signature = (snapshot_wire, config, cancel=None))]
fn validate_wire<'py>(
    py: Python<'py>,
    snapshot_wire: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, Cancellation>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let limits = limits_from_python(config)?;
    let owned = owned_buffer(py, snapshot_wire, Some(&limits), false)?;
    let input_size = owned.len();
    contain(|| limits.check_output_size(input_size, wire::RECEIPT_BYTES)).map_err(python_error)?;
    let cancellation = cancellation_or_default(cancel);
    let receipt = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        WireArena::decode(owned, &limits, &mut guard)?
            .validation
            .receipt()
    })?;
    PyBytes::new_with(py, receipt.len(), |buffer| {
        buffer.copy_from_slice(&receipt);
        Ok(())
    })
}

#[cfg(feature = "extension-module")]
#[pyfunction]
#[pyo3(signature = (snapshot_wire, config, cancel=None))]
fn roundtrip_wire<'py>(
    py: Python<'py>,
    snapshot_wire: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, Cancellation>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let limits = limits_from_python(config)?;
    let owned = owned_buffer(py, snapshot_wire, Some(&limits), false)?;
    let input_size = owned.len();
    contain(|| limits.check_output_size(input_size, input_size)).map_err(python_error)?;
    let cancellation = cancellation_or_default(cancel);
    let output = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        Ok(WireArena::decode(owned, &limits, &mut guard)?.encode())
    })?;
    PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })
}

#[cfg(feature = "extension-module")]
#[pyfunction]
#[pyo3(signature = (iterations, config, cancel=None))]
fn _work_probe(
    py: Python<'_>,
    iterations: u64,
    config: &Bound<'_, PyAny>,
    cancel: Option<PyRef<'_, Cancellation>>,
) -> PyResult<u64> {
    let limits = limits_from_python(config)?;
    if iterations > limits.max_terms {
        return Err(python_error(limits.resource_limit(
            limits::LimitKey::MaxTerms,
            iterations,
            "native work probe exceeds max_terms",
        )));
    }
    let cancellation = cancellation_or_default(cancel);
    run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let mut value = 0_u64;
        for work in 0..iterations {
            value = value.wrapping_add(work.rotate_left((work & 31) as u32));
            guard.check(work, false)?;
        }
        guard.check(iterations, true)?;
        Ok(value)
    })
}

#[cfg(feature = "extension-module")]
#[pyfunction]
fn _panic_probe() -> PyResult<()> {
    // The unit gate injects an actual unwind through `contain`.  The installed
    // probe returns the same boundary error without invoking Rust's global
    // panic hook (which would otherwise write process-global stderr).
    Err(python_error(NativeError::panic()))
}

#[cfg(feature = "test-hooks")]
#[cfg(feature = "extension-module")]
#[pyfunction]
fn _publication_fixture_v1() -> PyResult<publication::NativeSnapshotHandle> {
    contain(publication::fixture_handle_v1).map_err(python_error)
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    attestation,
    collections,
    *,
    documents,
    report,
    root_document_key,
    load_options,
    capability_bits,
    fingerprint_evidence,
    fingerprint_preimages,
    facade_cardinality_summary,
    owl2_dl_report_summary=None,
    raw_document_collections=None,
    max_retained_bytes=None
))]
fn _publication_fixture_v2(
    py: Python<'_>,
    attestation: &Bound<'_, PyAny>,
    collections: &Bound<'_, PyAny>,
    documents: &Bound<'_, PyAny>,
    report: &Bound<'_, PyAny>,
    root_document_key: &Bound<'_, PyAny>,
    load_options: &Bound<'_, PyAny>,
    capability_bits: &Bound<'_, PyAny>,
    fingerprint_evidence: &Bound<'_, PyAny>,
    fingerprint_preimages: &Bound<'_, PyAny>,
    facade_cardinality_summary: &Bound<'_, PyAny>,
    owl2_dl_report_summary: Option<&Bound<'_, PyAny>>,
    raw_document_collections: Option<&Bound<'_, PyAny>>,
    max_retained_bytes: Option<&Bound<'_, PyAny>>,
) -> PyResult<publication::NativeSnapshotHandle> {
    let max_retained_bytes = if let Some(value) = max_retained_bytes {
        if !value
            .get_type()
            .is(value.py().get_type::<pyo3::types::PyInt>())
        {
            return Err(PyTypeError::new_err(
                "max_retained_bytes must be an exact int",
            ));
        }
        value.extract()?
    } else {
        67_108_864
    };
    publication::fixture_handle_v2(
        py,
        attestation,
        collections,
        documents,
        report,
        root_document_key,
        load_options,
        capability_bits,
        fingerprint_evidence,
        fingerprint_preimages,
        facade_cardinality_summary,
        owl2_dl_report_summary,
        raw_document_collections,
        max_retained_bytes,
    )
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    attestation,
    collections,
    *,
    documents,
    report,
    root_document_key,
    load_options,
    capability_bits,
    fingerprint_evidence,
    fingerprint_preimages,
    facade_cardinality_summary,
    owl2_dl_report_summary=None,
    raw_document_collections=None,
    max_retained_bytes=None,
    fail_after=None
))]
fn _publication_allocation_probe_v2(
    py: Python<'_>,
    attestation: &Bound<'_, PyAny>,
    collections: &Bound<'_, PyAny>,
    documents: &Bound<'_, PyAny>,
    report: &Bound<'_, PyAny>,
    root_document_key: &Bound<'_, PyAny>,
    load_options: &Bound<'_, PyAny>,
    capability_bits: &Bound<'_, PyAny>,
    fingerprint_evidence: &Bound<'_, PyAny>,
    fingerprint_preimages: &Bound<'_, PyAny>,
    facade_cardinality_summary: &Bound<'_, PyAny>,
    owl2_dl_report_summary: Option<&Bound<'_, PyAny>>,
    raw_document_collections: Option<&Bound<'_, PyAny>>,
    max_retained_bytes: Option<&Bound<'_, PyAny>>,
    fail_after: Option<u64>,
) -> PyResult<(publication::NativeSnapshotHandle, u64)> {
    let max_retained_bytes = if let Some(value) = max_retained_bytes {
        if !value
            .get_type()
            .is(value.py().get_type::<pyo3::types::PyInt>())
        {
            return Err(PyTypeError::new_err(
                "max_retained_bytes must be an exact int",
            ));
        }
        value.extract()?
    } else {
        67_108_864
    };
    publication::fixture_allocation_probe_v2(
        py,
        attestation,
        collections,
        documents,
        report,
        root_document_key,
        load_options,
        capability_bits,
        fingerprint_evidence,
        fingerprint_preimages,
        facade_cardinality_summary,
        owl2_dl_report_summary,
        raw_document_collections,
        max_retained_bytes,
        fail_after,
    )
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (attestation, row_count, *, max_retained_bytes=None))]
fn _unique_axiom_publication_fixture_v2(
    attestation: &Bound<'_, PyAny>,
    row_count: &Bound<'_, PyAny>,
    max_retained_bytes: Option<&Bound<'_, PyAny>>,
) -> PyResult<publication::NativeSnapshotHandle> {
    if !row_count
        .get_type()
        .is(row_count.py().get_type::<pyo3::types::PyInt>())
    {
        return Err(PyTypeError::new_err("row_count must be an exact int"));
    }
    let row_count: u64 = row_count.extract()?;
    let max_retained_bytes = if let Some(value) = max_retained_bytes {
        if !value
            .get_type()
            .is(value.py().get_type::<pyo3::types::PyInt>())
        {
            return Err(PyTypeError::new_err(
                "max_retained_bytes must be an exact int",
            ));
        }
        value.extract()?
    } else {
        1_073_741_824
    };
    publication::unique_axiom_fixture_handle_v2(attestation, row_count, max_retained_bytes)
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (canonical, config, cancel=None))]
fn _component_roundtrip_v1<'py>(
    py: Python<'py>,
    canonical: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, Cancellation>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let limits = limits_from_python(config)?;
    let cancellation = cancellation_or_default(cancel);
    let owned = owned_buffer(py, canonical, Some(&limits), false)?;
    let input_size = owned.len();
    contain(|| limits.check_output_size(input_size, input_size)).map_err(python_error)?;
    let output = run_detached(py, move |interrupt| {
        let mut builder = NativeComponentBuilder::with_control(
            &limits,
            cancellation,
            Some(interrupt),
            input_size,
        )?;
        let pending = builder.intern_canonical(&owned)?;
        let mut frozen = builder.freeze()?;
        let identifier = frozen.resolve(pending)?;
        frozen.encode(identifier)
    })?;
    PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })
}

#[cfg(feature = "test-hooks")]
#[derive(Clone, Copy)]
enum ComponentAllocationPhase {
    Build,
    Freeze,
    Encode,
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (canonical, config, phase, fail_after=None))]
fn _component_allocation_probe_v1<'py>(
    py: Python<'py>,
    canonical: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    phase: &str,
    fail_after: Option<u64>,
) -> PyResult<(Bound<'py, PyBytes>, u64)> {
    let phase = match phase {
        "build" => ComponentAllocationPhase::Build,
        "freeze" => ComponentAllocationPhase::Freeze,
        "encode" => ComponentAllocationPhase::Encode,
        _ => {
            return Err(PyValueError::new_err(
                "component allocation phase must be build, freeze, or encode",
            ));
        }
    };
    let limits = limits_from_python(config)?;
    let owned = owned_buffer(py, canonical, Some(&limits), false)?;
    let input_size = owned.len();
    contain(|| limits.check_output_size(input_size, input_size)).map_err(python_error)?;
    let (output, allocations) = run_detached(py, move |interrupt| {
        let mut builder = NativeComponentBuilder::with_control(
            &limits,
            Cancellation::with_duration(None),
            Some(interrupt),
            input_size,
        )?;

        let (pending, build_allocations) = if matches!(phase, ComponentAllocationPhase::Build) {
            builder.configure_allocation_failure(fail_after)?;
            match builder.intern_canonical(&owned) {
                Ok(identifier) => {
                    let allocations = builder.allocation_count()?;
                    builder.configure_allocation_failure(None)?;
                    (identifier, allocations)
                }
                Err(injected) => {
                    builder.configure_allocation_failure(None)?;
                    let following = match builder.intern_canonical(&owned) {
                        Err(error) => error,
                        Ok(_) => {
                            return Err(NativeError::protocol(
                                "component allocation failure allowed later mutation",
                            ));
                        }
                    };
                    if following.code != "NATIVE_PROTOCOL" {
                        return Err(NativeError::protocol(
                            "component allocation failure did not poison later mutation",
                        ));
                    }
                    let freeze = match builder.freeze() {
                        Err(error) => error,
                        Ok(_) => {
                            return Err(NativeError::protocol(
                                "component allocation failure allowed freeze",
                            ));
                        }
                    };
                    if freeze.code != "NATIVE_PROTOCOL" {
                        return Err(NativeError::protocol(
                            "component allocation failure did not poison freeze",
                        ));
                    }
                    return Err(injected);
                }
            }
        } else {
            (builder.intern_canonical(&owned)?, 0)
        };

        let (mut frozen, freeze_allocations) = if matches!(phase, ComponentAllocationPhase::Freeze)
        {
            builder.configure_allocation_failure(fail_after)?;
            let mut frozen = builder.freeze()?;
            let allocations = frozen.allocation_count();
            frozen.configure_allocation_failure(None);
            (frozen, allocations)
        } else {
            (builder.freeze()?, 0)
        };
        let identifier = frozen.resolve(pending)?;

        let (output, encode_allocations) = if matches!(phase, ComponentAllocationPhase::Encode) {
            frozen.configure_allocation_failure(fail_after);
            match frozen.encode(identifier) {
                Ok(output) => {
                    let allocations = frozen.allocation_count();
                    frozen.configure_allocation_failure(None);
                    (output, allocations)
                }
                Err(injected) => {
                    frozen.configure_allocation_failure(None);
                    let recovered = frozen.encode(identifier)?;
                    if recovered != owned {
                        return Err(NativeError::protocol(
                            "component allocation failure changed the retained arena",
                        ));
                    }
                    return Err(injected);
                }
            }
        } else {
            (frozen.encode(identifier)?, 0)
        };

        let allocations = match phase {
            ComponentAllocationPhase::Build => build_allocations,
            ComponentAllocationPhase::Freeze => freeze_allocations,
            ComponentAllocationPhase::Encode => encode_allocations,
        };
        Ok((output, allocations))
    })?;
    let output = PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })?;
    Ok((output, allocations))
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (snapshot_wire, config, fail_after=None))]
fn _wire_allocation_probe_v1<'py>(
    py: Python<'py>,
    snapshot_wire: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    fail_after: Option<u64>,
) -> PyResult<(Bound<'py, PyBytes>, u64)> {
    let limits = limits_from_python(config)?;
    let owned = owned_buffer(py, snapshot_wire, Some(&limits), false)?;
    let input_size = owned.len();
    contain(|| limits.check_output_size(input_size, wire::RECEIPT_BYTES)).map_err(python_error)?;
    let (receipt, allocations) = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        wire::allocation_probe(&owned, &limits, &mut guard, fail_after)
    })?;
    let receipt = PyBytes::new_with(py, receipt.len(), |buffer| {
        buffer.copy_from_slice(&receipt);
        Ok(())
    })?;
    Ok((receipt, allocations))
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (source, config, fail_after=None))]
fn _parser_allocation_probe_v1<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    fail_after: Option<u64>,
) -> PyResult<(Bound<'py, PyBytes>, u64)> {
    let limits = limits_from_python(config)?;
    let owned = owned_source_request(py, source, &limits)?;
    let input_size = owned.len();
    let (output, allocations) = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let request = source::SourceRequest::decode(&owned, &limits)?;
        let mut session =
            session::Session::with_allocation_failure(&mut guard, &limits, input_size, fail_after)?;
        let output = parse::parse(request, &mut session)?;
        let allocations = session.allocation_count();
        Ok((output, allocations))
    })?;
    contain(|| limits.check_output_size(input_size, output.len())).map_err(python_error)?;
    let output = PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })?;
    Ok((output, allocations))
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (source, config, fail_after=None))]
fn _parser_bridge_allocation_probe_v1<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    fail_after: Option<u64>,
) -> PyResult<(Bound<'py, PyBytes>, u64)> {
    let mut allocations = BridgeAllocationProbe::configured(
        fail_after,
        "injected native parser bridge allocation failure",
    );
    let limits = limits_from_python_with_allocations(config, &mut allocations)?;
    let owned = owned_source_request_with_allocations(py, source, &limits, &mut allocations)?;
    let input_size = owned.len();
    let output = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let request = source::SourceRequest::decode(&owned, &limits)?;
        let mut session = session::Session::new(&mut guard, &limits, input_size)?;
        parse::parse(request, &mut session)
    })?;
    contain(|| limits.check_output_size(input_size, output.len())).map_err(python_error)?;
    allocations.checkpoint()?;
    let output = PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })?;
    Ok((output, allocations.count()))
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (snapshot_wire, request, fail_after=None))]
fn _index_bridge_allocation_probe_v1<'py>(
    py: Python<'py>,
    snapshot_wire: &Bound<'py, PyAny>,
    request: &Bound<'py, PyAny>,
    fail_after: Option<u64>,
) -> PyResult<(Bound<'py, PyBytes>, u64)> {
    let mut allocations = BridgeAllocationProbe::configured(
        fail_after,
        "injected native index bridge allocation failure",
    );
    let owned_request = owned_index_request_with_allocations(py, request, &mut allocations)?;
    let limits = contain(|| index::decode_limits(&owned_request)).map_err(python_error)?;
    let owned_source =
        owned_index_source_with_allocations(py, snapshot_wire, &limits, &mut allocations)?;
    let input_size = owned_source.len();
    let output = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let mut session = session::Session::new(&mut guard, &limits, input_size)?;
        index::build(&owned_source, &mut session)
    })?;
    allocations.checkpoint()?;
    let output = PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })?;
    Ok((output, allocations.count()))
}

#[cfg(feature = "test-hooks")]
#[derive(Clone, Copy)]
enum FoundationBridgeOperation {
    ValidateCanonical,
    ValidateWire,
    RoundtripWire,
}

#[cfg(feature = "test-hooks")]
#[pyfunction]
#[pyo3(signature = (operation, source, config, fail_after=None))]
fn _foundation_bridge_allocation_probe_v1<'py>(
    py: Python<'py>,
    operation: &str,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    fail_after: Option<u64>,
) -> PyResult<(Bound<'py, PyBytes>, u64)> {
    let operation = match operation {
        "validate-canonical" => FoundationBridgeOperation::ValidateCanonical,
        "validate-wire" => FoundationBridgeOperation::ValidateWire,
        "roundtrip-wire" => FoundationBridgeOperation::RoundtripWire,
        _ => {
            return Err(PyValueError::new_err(
                "foundation bridge operation must be validate-canonical, validate-wire, or roundtrip-wire",
            ));
        }
    };
    let mut allocations = BridgeAllocationProbe::configured(
        fail_after,
        "injected native foundation bridge allocation failure",
    );
    let limits = limits_from_python_with_allocations(config, &mut allocations)?;
    let owned = owned_buffer_with_allocations(py, source, Some(&limits), false, &mut allocations)?;
    let input_size = owned.len();
    let output = match operation {
        FoundationBridgeOperation::ValidateCanonical => {
            contain(|| limits.check_output_size(input_size, input_size)).map_err(python_error)?;
            run_detached(py, move |interrupt| {
                let mut guard = Guard::with_interrupt(
                    Cancellation::with_duration(None),
                    limits.deadline,
                    limits.cancellation_stride,
                    interrupt,
                );
                guard.check(0, true)?;
                let mut budget = ScanBudget::from_limits(&limits);
                scan_canonical(&owned, &mut budget)?;
                guard.check(1, true)?;
                Ok(owned)
            })?
        }
        FoundationBridgeOperation::ValidateWire => {
            contain(|| limits.check_output_size(input_size, wire::RECEIPT_BYTES))
                .map_err(python_error)?;
            run_detached(py, move |interrupt| {
                let mut guard = Guard::with_interrupt(
                    Cancellation::with_duration(None),
                    limits.deadline,
                    limits.cancellation_stride,
                    interrupt,
                );
                WireArena::decode(owned, &limits, &mut guard)?
                    .validation
                    .receipt()
            })?
        }
        FoundationBridgeOperation::RoundtripWire => {
            contain(|| limits.check_output_size(input_size, input_size)).map_err(python_error)?;
            run_detached(py, move |interrupt| {
                let mut guard = Guard::with_interrupt(
                    Cancellation::with_duration(None),
                    limits.deadline,
                    limits.cancellation_stride,
                    interrupt,
                );
                Ok(WireArena::decode(owned, &limits, &mut guard)?.encode())
            })?
        }
    };
    allocations.checkpoint()?;
    let output = PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })?;
    Ok((output, allocations.count()))
}

#[cfg(feature = "extension-module")]
#[pyfunction]
#[pyo3(signature = (source, config, cancel=None))]
fn parse_document<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    config: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, Cancellation>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let limits = limits_from_python(config)?;
    let owned = owned_source_request(py, source, &limits)?;
    let input_size = owned.len();
    let cancellation = cancellation_or_default(cancel);
    let output = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let request = source::SourceRequest::decode(&owned, &limits)?;
        let mut session = session::Session::new(&mut guard, &limits, input_size)?;
        parse::parse(request, &mut session)
    })?;
    contain(|| limits.check_output_size(input_size, output.len())).map_err(python_error)?;
    PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })
}

#[cfg(feature = "extension-module")]
#[pyfunction]
fn build_snapshot(
    _documents: &Bound<'_, PyAny>,
    _config: &[u8],
    _cancel: &Bound<'_, PyAny>,
) -> PyResult<Vec<u8>> {
    Err(python_error(NativeError::new(
        "NATIVE_CAPABILITY_UNAVAILABLE",
        "native snapshot construction is not implemented by WP07",
    )))
}

#[cfg(feature = "extension-module")]
#[pyfunction]
#[pyo3(signature = (snapshot_wire, request, cancel=None))]
fn build_index<'py>(
    py: Python<'py>,
    snapshot_wire: &Bound<'py, PyAny>,
    request: &Bound<'py, PyAny>,
    cancel: Option<PyRef<'py, Cancellation>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let owned_request = owned_index_request(py, request)?;
    let limits = contain(|| index::decode_limits(&owned_request)).map_err(python_error)?;
    let owned_source = owned_index_source(py, snapshot_wire, &limits)?;
    let input_size = owned_source.len();
    let cancellation = cancellation_or_default(cancel);
    let output = run_detached(py, move |interrupt| {
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let mut session = session::Session::new(&mut guard, &limits, input_size)?;
        index::build(&owned_source, &mut session)
    })?;
    PyBytes::new_with(py, output.len(), |buffer| {
        buffer.copy_from_slice(&output);
        Ok(())
    })
}

#[cfg(feature = "extension-module")]
#[pymodule]
fn _native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    let binding_features = bindings::register(py, module)?;
    let mut features = Vec::from(FOUNDATION_FEATURES);
    features.extend(binding_features.combined()?);
    features.sort_unstable();
    if features.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            "native foundation and binding feature ledgers overlap",
        ));
    }
    module.add("ABI_VERSION", ABI_VERSION)?;
    module.add("MODEL_SCHEMA_VERSION", MODEL_SCHEMA_VERSION)?;
    module.add("WIRE_FORMAT_VERSION", WIRE_FORMAT_VERSION)?;
    module.add("FEATURES", PyTuple::new(py, &features)?)?;
    module.add(
        "INGESTION_FEATURES",
        PyTuple::new(py, binding_features.ingestion)?,
    )?;
    module.add("VIEW_FEATURES", PyTuple::new(py, binding_features.views)?)?;
    module.add("_NativeError", py.get_type::<_NativeError>())?;
    module.add_class::<Cancellation>()?;
    publication::register_native_handle_types(py, module)?;
    module.add_function(wrap_pyfunction!(version, module)?)?;
    module.add_function(wrap_pyfunction!(self_test, module)?)?;
    module.add_function(wrap_pyfunction!(validate_canonical, module)?)?;
    module.add_function(wrap_pyfunction!(validate_wire, module)?)?;
    module.add_function(wrap_pyfunction!(roundtrip_wire, module)?)?;
    module.add_function(wrap_pyfunction!(_work_probe, module)?)?;
    module.add_function(wrap_pyfunction!(_panic_probe, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_publication_fixture_v1, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_publication_fixture_v2, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_publication_allocation_probe_v2, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(
        _unique_axiom_publication_fixture_v2,
        module
    )?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_component_roundtrip_v1, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_component_allocation_probe_v1, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_wire_allocation_probe_v1, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_parser_allocation_probe_v1, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(
        _parser_bridge_allocation_probe_v1,
        module
    )?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(_index_bridge_allocation_probe_v1, module)?)?;
    #[cfg(feature = "test-hooks")]
    module.add_function(wrap_pyfunction!(
        _foundation_bridge_allocation_probe_v1,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(parse_document, module)?)?;
    module.add_function(wrap_pyfunction!(build_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(build_index, module)?)?;
    Ok(())
}

#[cfg(all(test, feature = "extension-module"))]
mod tests {
    use super::*;

    #[test]
    fn panic_probe_is_contained() {
        let error = contain(|| -> NativeResult<()> { panic!("probe") }).unwrap_err();
        assert_eq!(error.code, "NATIVE_PANIC");
    }

    #[test]
    fn feature_ledger_is_sorted_and_foundational() {
        assert!(FOUNDATION_FEATURES.windows(2).all(|pair| pair[0] < pair[1]));
        assert!(FOUNDATION_FEATURES.contains(&"wire-v1"));
        assert!(!FOUNDATION_FEATURES.contains(&"parse-functional-v1"));
        assert!(FOUNDATION_FEATURES.contains(&"index-axiom-types-v1"));
    }
}
