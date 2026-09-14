//! Bounded annotation publication from retained roots, without Python axiom rows.
use std::collections::BTreeSet;
use std::mem::size_of;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyModule, PyTuple};

use crate::cancel::{Cancellation, Guard};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{structural_digest_v2, ComponentFieldRef, ComponentId};
use crate::publication::{NativeSnapshotHandle, PublicationStorageV2};

#[pyclass(module = "pyowl_core._native", frozen)]
struct NativeAnnotationColumnsV1 {
    storage: Arc<PublicationStorageV2>,
    roots: Vec<ComponentId>,
    retained_bytes: usize,
    scanned_roots: usize,
}

type Row = [Vec<u8>; 5];

fn check_budget(limits: &Limits, key: LimitKey, value: usize) -> NativeResult<()> {
    let maximum = limits.value(key);
    if maximum != 0 && value as u64 > maximum {
        return Err(limits.resource_limit(
            key,
            value as u64,
            "native annotation columns exceed budget",
        ));
    }
    Ok(())
}

#[pymethods]
impl NativeAnnotationColumnsV1 {
    fn _report_v1(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let result = PyDict::new(py);
        result.set_item("backend", "native")?;
        result.set_item("scanned_roots", self.scanned_roots)?;
        result.set_item("annotation_rows", self.roots.len())?;
        result.set_item("retained_bytes", self.retained_bytes)?;
        Ok(result.unbind())
    }

    #[pyo3(signature = (start, subjects, properties, max_rows, max_bytes, config, cancel=None))]
    #[allow(clippy::too_many_arguments)]
    fn _page_v1(
        &self,
        py: Python<'_>,
        start: usize,
        subjects: Option<Vec<Vec<u8>>>,
        properties: Option<Vec<Vec<u8>>>,
        max_rows: usize,
        max_bytes: usize,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<Py<PyTuple>> {
        if max_rows == 0 || max_bytes == 0 || start > self.roots.len() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "invalid native annotation page bounds",
            ));
        }
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        let filter_rows =
            subjects.as_ref().map_or(0, Vec::len) + properties.as_ref().map_or(0, Vec::len);
        let filter_bytes = subjects
            .iter()
            .chain(properties.iter())
            .flat_map(|rows| rows.iter())
            .try_fold(0usize, |total, row| {
                total
                    .checked_add(row.len() + 64)
                    .ok_or_else(|| NativeError::limit("annotation filter size overflow"))
            })
            .map_err(crate::python_error)?;
        check_budget(&limits, LimitKey::MaxIndexRows, filter_rows).map_err(crate::python_error)?;
        check_budget(&limits, LimitKey::MaxIndexBytes, filter_bytes)
            .map_err(crate::python_error)?;
        let subjects = subjects.map(|rows| rows.into_iter().collect::<BTreeSet<_>>());
        let properties = properties.map(|rows| rows.into_iter().collect::<BTreeSet<_>>());
        let (rows, cursor, scanned, payload) = crate::run_detached(py, |interrupt| {
            let typed = self.storage.typed_structural()?;
            let arena = typed.arena();
            let external = typed.external_retained_bytes() + self.retained_bytes + 2 * filter_bytes;
            let base_bytes = usize::try_from(arena.counters().retained_bytes)
                .map_err(|_| NativeError::limit("native retained memory exceeds usize"))?;
            let mut guard = Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                interrupt.clone(),
            );
            guard.check(0, true)?;
            let mut rows: Vec<Row> = Vec::new();
            let mut cursor = start;
            let mut payload = 0usize;
            while cursor < self.roots.len() && rows.len() < max_rows {
                guard.check((cursor - start) as u64, false)?;
                let root = self.roots[cursor];
                let record = arena.record(root)?;
                let component = |index| match record.field(index)? {
                    ComponentFieldRef::Node(node) => Ok(node),
                    _ => Err(NativeError::protocol(
                        "annotation assertion has a non-node field",
                    )),
                };
                let encode = |node| {
                    arena.encode(
                        node,
                        &limits,
                        cancellation.clone(),
                        Some(interrupt.clone()),
                        external + payload,
                    )
                };
                let property = encode(component(0)?)?;
                if properties
                    .as_ref()
                    .is_some_and(|set| !set.contains(&property))
                {
                    cursor += 1;
                    continue;
                }
                let subject = encode(component(1)?)?;
                if subjects.as_ref().is_some_and(|set| !set.contains(&subject)) {
                    cursor += 1;
                    continue;
                }
                // Measure the indivisible row before encoding its potentially large
                // literal/annotations. Never silently drop an oversized assertion.
                let value_id = component(2)?;
                let value_len = arena.encoded_len(
                    value_id,
                    &limits,
                    cancellation.clone(),
                    Some(interrupt.clone()),
                    external + payload,
                )?;
                let assertion_len = arena.encoded_len(
                    root,
                    &limits,
                    cancellation.clone(),
                    Some(interrupt.clone()),
                    external + payload,
                )?;
                let bytes = subject
                    .len()
                    .checked_add(property.len())
                    .and_then(|n| n.checked_add(value_len))
                    .and_then(|n| n.checked_add(assertion_len))
                    .and_then(|n| n.checked_add(32))
                    .ok_or_else(|| NativeError::limit("annotation page size overflow"))?;
                if bytes > max_bytes {
                    return Err(NativeError::resource_limit(
                        "max_bytes",
                        bytes as u64,
                        max_bytes as u64,
                        "one annotation assertion exceeds max_bytes",
                    ));
                }
                if payload.checked_add(bytes).is_none_or(|n| n > max_bytes) {
                    break;
                }
                check_budget(&limits, LimitKey::MaxIndexRows, rows.len() + 1)?;
                let live = base_bytes
                    + external
                    + 2 * (payload + bytes)
                    + (rows.len() + 1) * size_of::<Row>();
                check_budget(&limits, LimitKey::MaxMemoryBytes, live)?;
                rows.try_reserve(1)
                    .map_err(|_| NativeError::limit("annotation page allocation failed"))?;
                let value = encode(value_id)?;
                let assertion = encode(root)?;
                let digest = structural_digest_v2(&assertion).to_vec();
                rows.push([subject, property, value, assertion, digest]);
                payload += bytes;
                cursor += 1;
            }
            guard.check((cursor - start) as u64, true)?;
            Ok((rows, cursor, cursor - start, payload))
        })?;
        let output = PyList::empty(py);
        for row in rows {
            let fields = row.iter().map(|value| PyBytes::new(py, value).into_any());
            output.append(PyTuple::new(py, fields)?)?;
        }
        PyTuple::new(
            py,
            [
                output.into_any(),
                cursor.into_pyobject(py)?.into_any(),
                scanned.into_pyobject(py)?.into_any(),
                payload.into_pyobject(py)?.into_any(),
            ],
        )
        .map(Bound::unbind)
    }
}

#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, config, cancel=None))]
fn _annotation_columns_v1(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
    scope: &str,
    document_ordinal: Option<u64>,
    config: &Bound<'_, PyBytes>,
    cancel: Option<PyRef<'_, Cancellation>>,
) -> PyResult<NativeAnnotationColumnsV1> {
    let selected = super::views::encoded_selection(scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let limits = crate::limits_from_python(config.as_any())?;
    let cancellation = crate::cancellation_or_default(cancel);
    let (roots, retained_bytes, scanned_roots) = crate::run_detached(py, |interrupt| {
        let typed = storage.typed_structural()?;
        let source = typed.selected_axioms(selected, document_ordinal)?;
        let arena = typed.arena();
        let base_bytes = usize::try_from(arena.counters().retained_bytes)
            .map_err(|_| NativeError::limit("native retained memory exceeds usize"))?;
        let mut guard = Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            interrupt,
        );
        let mut roots = Vec::new();
        guard.check(0, true)?;
        for (ordinal, identifier) in source.iter().enumerate() {
            guard.check(ordinal as u64, false)?;
            if arena.tag(*identifier)? != 120 {
                continue;
            }
            check_budget(&limits, LimitKey::MaxIndexRows, roots.len() + 1)?;
            if roots.len() == roots.capacity() {
                let capacity = roots
                    .capacity()
                    .max(512)
                    .saturating_mul(2)
                    .min(source.len());
                let bytes = capacity
                    .checked_mul(size_of::<ComponentId>())
                    .ok_or_else(|| NativeError::limit("annotation index size overflow"))?;
                check_budget(&limits, LimitKey::MaxIndexBytes, bytes)?;
                check_budget(
                    &limits,
                    LimitKey::MaxMemoryBytes,
                    base_bytes + typed.external_retained_bytes() + bytes,
                )?;
                roots
                    .try_reserve_exact(capacity - roots.len())
                    .map_err(|_| NativeError::limit("annotation index allocation failed"))?;
            }
            roots.push(*identifier);
        }
        guard.check(source.len() as u64, true)?;
        let retained_bytes = roots.capacity() * size_of::<ComponentId>();
        Ok((roots, retained_bytes, source.len()))
    })?;
    Ok(NativeAnnotationColumnsV1 {
        storage,
        roots,
        retained_bytes,
        scanned_roots,
    })
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("NATIVE_ANNOTATION_COLUMNS_API_VERSION", 1)?;
    module.add_class::<NativeAnnotationColumnsV1>()?;
    module.add_function(wrap_pyfunction!(_annotation_columns_v1, module)?)?;
    Ok(())
}
