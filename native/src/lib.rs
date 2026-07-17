//! Private PyO3 boundary for the Java-free pyowl-core native foundation.

#![forbid(unsafe_code)]
#![deny(clippy::all)]

mod cancel;
mod error;
mod limits;
mod model;
mod wire;

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Arc;

use cancel::{interrupt_slot, take_interrupt, Cancellation, Guard, InterruptSlot};
use error::{NativeError, NativeResult};
use limits::Limits;
use model::{scan_canonical, CanonicalRow, ModelArena, ScanBudget};
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyModule, PyTuple};
use wire::WireArena;

const ABI_VERSION: u32 = 1;
const MODEL_SCHEMA_VERSION: u32 = 1;
const WIRE_FORMAT_VERSION: (u16, u16) = (1, 0);
const FEATURES: [&str; 8] = [
    "cancellation",
    "canonical-model-v1",
    "deadlines",
    "gil-release",
    "owned-buffers",
    "panic-containment",
    "safe-rust",
    "wire-v1",
];

create_exception!(_native, _NativeError, PyException);

fn contain<T>(operation: impl FnOnce() -> NativeResult<T>) -> NativeResult<T> {
    catch_unwind(AssertUnwindSafe(operation)).unwrap_or_else(|_| Err(NativeError::panic()))
}

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

fn python_error(error: NativeError) -> PyErr {
    PyErr::new::<_NativeError, _>((error.code, error.message))
}

fn limits_from_python(config: &Bound<'_, PyAny>) -> PyResult<Limits> {
    let bytes = owned_buffer(config.py(), config, None, true)?;
    contain(|| Limits::decode(&bytes)).map_err(python_error)
}

fn cancellation_or_default(value: Option<PyRef<'_, Cancellation>>) -> Cancellation {
    value.map_or_else(|| Cancellation::with_duration(None), |item| item.clone())
}

fn owned_buffer(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    limits: Option<&Limits>,
    configuration: bool,
) -> PyResult<Vec<u8>> {
    let builtins = py.import("builtins")?;
    let view = builtins
        .getattr("memoryview")?
        .call1((value,))
        .map_err(|error| {
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
    let nbytes: usize = view.getattr("nbytes")?.extract()?;
    if configuration && nbytes != 0 && nbytes != limits::CONFIG_BYTES {
        return Err(python_error(NativeError::protocol(
            "invalid native limits framing",
        )));
    }
    if let Some(selected) = limits {
        contain(|| selected.check_source_size(nbytes)).map_err(python_error)?;
    }
    let owned = view.call_method0("tobytes")?;
    let bytes = owned.cast::<PyBytes>()?.as_bytes();
    let mut result = Vec::new();
    result
        .try_reserve_exact(bytes.len())
        .map_err(|_| python_error(NativeError::limit("native owned-buffer allocation failed")))?;
    result.extend_from_slice(bytes);
    Ok(result)
}

#[pyfunction]
fn version() -> (&'static str, u32) {
    (env!("CARGO_PKG_VERSION"), ABI_VERSION)
}

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
        return Err(python_error(NativeError::limit(
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

#[pyfunction]
fn _panic_probe() -> PyResult<()> {
    // The unit gate injects an actual unwind through `contain`.  The installed
    // probe returns the same boundary error without invoking Rust's global
    // panic hook (which would otherwise write process-global stderr).
    Err(python_error(NativeError::panic()))
}

#[pyfunction]
fn parse_document(
    _source: &Bound<'_, PyAny>,
    _config: &[u8],
    _cancel: &Bound<'_, PyAny>,
) -> PyResult<Vec<u8>> {
    Err(python_error(NativeError::new(
        "NATIVE_CAPABILITY_UNAVAILABLE",
        "native document parsing is not implemented by WP07",
    )))
}

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

#[pyfunction]
fn build_index(
    _snapshot_wire: &Bound<'_, PyAny>,
    _request: &[u8],
    _cancel: &Bound<'_, PyAny>,
) -> PyResult<Vec<u8>> {
    Err(python_error(NativeError::new(
        "NATIVE_CAPABILITY_UNAVAILABLE",
        "native index construction is not implemented by WP07",
    )))
}

#[pymodule]
fn _native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("ABI_VERSION", ABI_VERSION)?;
    module.add("MODEL_SCHEMA_VERSION", MODEL_SCHEMA_VERSION)?;
    module.add("WIRE_FORMAT_VERSION", WIRE_FORMAT_VERSION)?;
    module.add("FEATURES", PyTuple::new(py, FEATURES)?)?;
    module.add("_NativeError", py.get_type::<_NativeError>())?;
    module.add_class::<Cancellation>()?;
    module.add_function(wrap_pyfunction!(version, module)?)?;
    module.add_function(wrap_pyfunction!(self_test, module)?)?;
    module.add_function(wrap_pyfunction!(validate_canonical, module)?)?;
    module.add_function(wrap_pyfunction!(validate_wire, module)?)?;
    module.add_function(wrap_pyfunction!(roundtrip_wire, module)?)?;
    module.add_function(wrap_pyfunction!(_work_probe, module)?)?;
    module.add_function(wrap_pyfunction!(_panic_probe, module)?)?;
    module.add_function(wrap_pyfunction!(parse_document, module)?)?;
    module.add_function(wrap_pyfunction!(build_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(build_index, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn panic_probe_is_contained() {
        let error = contain(|| -> NativeResult<()> { panic!("probe") }).unwrap_err();
        assert_eq!(error.code, "NATIVE_PANIC");
    }

    #[test]
    fn feature_ledger_is_sorted_and_foundational() {
        assert!(FEATURES.windows(2).all(|pair| pair[0] < pair[1]));
        assert!(FEATURES.contains(&"wire-v1"));
        assert!(!FEATURES.iter().any(|item| item.contains("parse")));
    }
}
