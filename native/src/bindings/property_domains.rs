//! Native asserted domain/range postings over the original retained typed selection.
use std::collections::BTreeMap;
use std::ops::Bound::{Excluded, Unbounded};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyModule, PyTuple};

use crate::cancel::{Cancellation, Guard};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{structural_digest_v2, ComponentFieldRef, ComponentId, NativeComponentArena};
use crate::publication::{NativeSnapshotHandle, PublicationStorageV2};

#[derive(Clone, Copy)]
struct Row {
    root: ComponentId,
    range: bool,
    named: bool,
}

#[pyclass(module = "pyowl_core._native", frozen)]
struct NativePropertyDomainsV1 {
    storage: Arc<PublicationStorageV2>,
    rows: Vec<Row>,
    properties: BTreeMap<Vec<u8>, Vec<usize>>,
    charged_bytes: usize,
    scanned_roots: usize,
    query_rows: AtomicU64,
    published_rows: AtomicU64,
}

fn budget(limits: &Limits, rows: usize, bytes: usize, base: usize) -> NativeResult<()> {
    let memory = base
        .checked_add(bytes.saturating_mul(2))
        .ok_or_else(|| NativeError::limit("domain/range memory overflow"))?;
    for (key, value) in [
        (LimitKey::MaxIndexRows, rows),
        (LimitKey::MaxIndexBytes, bytes),
        (LimitKey::MaxMemoryBytes, memory),
    ] {
        let max = limits.value(key);
        if max != 0 && value as u64 > max {
            return Err(limits.resource_limit(
                key,
                value as u64,
                "native domain/range index exceeds budget",
            ));
        }
    }
    Ok(())
}
fn child(arena: &NativeComponentArena, id: ComponentId, field: usize) -> NativeResult<ComponentId> {
    match arena.record(id)?.field(field)? {
        ComponentFieldRef::Node(value) => Ok(value),
        _ => Err(NativeError::protocol(
            "domain/range constructor requires node fields",
        )),
    }
}
fn named(arena: &NativeComponentArena, id: ComponentId, annotation: bool) -> NativeResult<bool> {
    let value = arena.record(id)?;
    Ok(if annotation {
        value.tag() == 1
    } else {
        value.tag() == 2
            && matches!(
                value.field(0)?,
                ComponentFieldRef::Enum(b"class" | b"datatype")
            )
    })
}

#[pymethods]
impl NativePropertyDomainsV1 {
    fn _report_v1(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let result = PyDict::new(py);
        result.set_item("backend", "native")?;
        result.set_item("scanned_roots", self.scanned_roots)?;
        result.set_item("selected_axioms", self.rows.len())?;
        result.set_item("properties", self.properties.len())?;
        result.set_item("allocation_charge_bytes", self.charged_bytes)?;
        result.set_item(
            "query_rows_visited",
            self.query_rows.load(Ordering::Relaxed),
        )?;
        result.set_item(
            "published_axioms",
            self.published_rows.load(Ordering::Relaxed),
        )?;
        Ok(result.unbind())
    }
    #[pyo3(signature=(property,kind,config,cancel=None))]
    fn _counts_v1(
        &self,
        py: Python<'_>,
        property: Vec<u8>,
        kind: Option<bool>,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<(usize, usize)> {
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        crate::run_detached(py, |interrupt| {
            let mut guard = Guard::with_interrupt(
                cancellation,
                limits.deadline,
                limits.cancellation_stride,
                interrupt,
            );
            guard.check(0, true)?;
            let (mut total, mut complex) = (0, 0);
            for (position, &id) in self
                .properties
                .get(&property)
                .into_iter()
                .flatten()
                .enumerate()
            {
                guard.check(position as u64, false)?;
                self.query_rows.fetch_add(1, Ordering::Relaxed);
                let row = self.rows[id];
                if kind.is_none_or(|kind| kind == row.range) {
                    total += 1;
                    complex += usize::from(!row.named);
                }
            }
            guard.check(total as u64, true)?;
            Ok((total, complex))
        })
    }
    #[pyo3(signature=(property,kind,named_only,start,max_rows,config,cancel=None))]
    #[allow(clippy::too_many_arguments)]
    fn _records_v1(
        &self,
        py: Python<'_>,
        property: Option<Vec<u8>>,
        kind: Option<bool>,
        named_only: bool,
        start: usize,
        max_rows: usize,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<(usize, bool, Py<PyList>)> {
        if max_rows == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "page size must be positive",
            ));
        }
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        let (next, done, rows) = crate::run_detached(py, |interrupt| {
            let typed = self.storage.typed_structural()?;
            let arena = typed.arena();
            let external = typed.external_retained_bytes() + self.charged_bytes;
            let base = usize::try_from(arena.counters().retained_bytes)
                .map_err(|_| NativeError::limit("domain owner memory exceeds usize"))?
                + external;
            let posting = property
                .as_ref()
                .map(|key| self.properties.get(key).map_or(&[][..], Vec::as_slice));
            let total = posting.map_or(self.rows.len(), <[usize]>::len);
            if start > total {
                return Err(NativeError::protocol("invalid domain/range page cursor"));
            }
            let mut guard = Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                interrupt.clone(),
            );
            guard.check(0, true)?;
            let (mut cursor, mut bytes, mut result) = (start, 0usize, Vec::new());
            while cursor < total && result.len() < max_rows {
                guard.check((cursor - start) as u64, false)?;
                let id = posting.map_or(cursor, |ids| ids[cursor]);
                cursor += 1;
                self.query_rows.fetch_add(1, Ordering::Relaxed);
                let row = self.rows[id];
                if kind.is_some_and(|kind| kind != row.range) || (named_only && !row.named) {
                    continue;
                }
                budget(&limits, result.len() + 1, bytes, base)?;
                let encoded = arena.encode(
                    row.root,
                    &limits,
                    cancellation.clone(),
                    Some(interrupt.clone()),
                    external + bytes,
                )?;
                bytes = bytes
                    .checked_add(encoded.len().saturating_mul(2) + 96)
                    .ok_or_else(|| NativeError::limit("domain/range page memory overflow"))?;
                budget(&limits, result.len() + 1, bytes, base)?;
                result
                    .try_reserve(1)
                    .map_err(|_| NativeError::limit("domain/range page allocation failed"))?;
                result.push(encoded);
            }
            guard.check((cursor - start) as u64, true)?;
            Ok((cursor, cursor == total, result))
        })?;
        let output = PyList::empty(py);
        for bytes in &rows {
            output.append(PyTuple::new(
                py,
                [
                    PyBytes::new(py, bytes),
                    PyBytes::new(py, &structural_digest_v2(bytes)),
                ],
            )?)?;
        }
        self.published_rows
            .fetch_add(rows.len() as u64, Ordering::Relaxed);
        Ok((next, done, output.unbind()))
    }
    #[pyo3(signature=(after,max_rows,config,cancel=None))]
    fn _properties_v1(
        &self,
        py: Python<'_>,
        after: Option<Vec<u8>>,
        max_rows: usize,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<Py<PyList>> {
        if max_rows == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "page size must be positive",
            ));
        }
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        let rows = crate::run_detached(py, |interrupt| {
            let typed = self.storage.typed_structural()?;
            let base = usize::try_from(typed.arena().counters().retained_bytes)
                .map_err(|_| NativeError::limit("domain owner memory exceeds usize"))?
                + typed.external_retained_bytes()
                + self.charged_bytes;
            let mut guard = Guard::with_interrupt(
                cancellation,
                limits.deadline,
                limits.cancellation_stride,
                interrupt,
            );
            guard.check(0, true)?;
            let mut result = Vec::new();
            let mut bytes = 0usize;
            let lower = after.as_ref().map_or(Unbounded, Excluded);
            for (key, _) in self
                .properties
                .range::<Vec<u8>, _>((lower, Unbounded))
                .take(max_rows)
            {
                guard.check(result.len() as u64, false)?;
                bytes = bytes
                    .checked_add(key.len().saturating_mul(2) + 32)
                    .ok_or_else(|| NativeError::limit("domain property page overflow"))?;
                budget(&limits, result.len() + 1, bytes, base)?;
                result
                    .try_reserve(1)
                    .map_err(|_| NativeError::limit("domain property page allocation failed"))?;
                result.push(key.clone());
            }
            guard.check(result.len() as u64, true)?;
            Ok(result)
        })?;
        let output = PyList::empty(py);
        for bytes in rows {
            output.append(PyBytes::new(py, &bytes))?;
        }
        Ok(output.unbind())
    }
}

#[pyfunction]
#[pyo3(signature=(handle,scope,document_ordinal,config,cancel=None))]
fn _property_domains_v1(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
    scope: &str,
    document_ordinal: Option<u64>,
    config: &Bound<'_, PyBytes>,
    cancel: Option<PyRef<'_, Cancellation>>,
) -> PyResult<NativePropertyDomainsV1> {
    let scope = super::views::encoded_selection(scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let limits = crate::limits_from_python(config.as_any())?;
    let cancellation = crate::cancellation_or_default(cancel);
    crate::run_detached(py, |interrupt| {
        let typed = storage.typed_structural()?;
        let arena = typed.arena();
        let source = typed.selected_axioms(scope, document_ordinal)?;
        let base = usize::try_from(arena.counters().retained_bytes)
            .map_err(|_| NativeError::limit("domain owner memory exceeds usize"))?
            + typed.external_retained_bytes();
        let mut guard = Guard::with_interrupt(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
            interrupt.clone(),
        );
        guard.check(0, true)?;
        let (mut rows, mut properties, mut bytes) =
            (Vec::new(), BTreeMap::<Vec<u8>, Vec<usize>>::new(), 0usize);
        for (position, &root) in source.iter().enumerate() {
            guard.check(position as u64, false)?;
            let record = arena.record(root)?;
            let tag = record.tag();
            if !matches!(tag, 74 | 75 | 93 | 94 | 122 | 123) {
                continue;
            }
            let property = child(arena, root, 0)?;
            let value = child(arena, root, 1)?;
            let key = arena.encode(
                property,
                &limits,
                cancellation.clone(),
                Some(interrupt.clone()),
                typed.external_retained_bytes() + bytes,
            )?;
            let new = !properties.contains_key(&key);
            bytes = bytes
                .checked_add(
                    64 + if new {
                        key.len().saturating_mul(2) + 128
                    } else {
                        0
                    },
                )
                .ok_or_else(|| NativeError::limit("domain index memory overflow"))?;
            budget(&limits, rows.len() + 1, bytes, base)?;
            let posting = properties.entry(key).or_default();
            posting
                .try_reserve(1)
                .map_err(|_| NativeError::limit("domain posting allocation failed"))?;
            posting.push(rows.len());
            rows.try_reserve(1)
                .map_err(|_| NativeError::limit("domain row allocation failed"))?;
            rows.push(Row {
                root,
                range: matches!(tag, 75 | 94 | 123),
                named: named(arena, value, matches!(tag, 122 | 123))?,
            });
        }
        guard.check(source.len() as u64, true)?;
        let scanned_roots = source.len();
        Ok(NativePropertyDomainsV1 {
            storage: storage.clone(),
            rows,
            properties,
            charged_bytes: bytes,
            scanned_roots,
            query_rows: AtomicU64::new(0),
            published_rows: AtomicU64::new(0),
        })
    })
}
pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("NATIVE_PROPERTY_DOMAINS_API_VERSION", 1)?;
    module.add_class::<NativePropertyDomainsV1>()?;
    module.add_function(wrap_pyfunction!(_property_domains_v1, module)?)?;
    Ok(())
}
