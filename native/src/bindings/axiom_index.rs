//! Compact native constructor/entity postings; Python wraps requested axioms only.
use crate::cancel::{Cancellation, Guard};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{ComponentFieldRef, ComponentId};
use crate::publication::{NativeSnapshotHandle, PublicationStorageV2};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyModule, PyTuple};
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

#[pyclass(module = "pyowl_core._native", frozen)]
struct NativeAxiomIndexV1 {
    storage: Arc<PublicationStorageV2>,
    roots: Vec<ComponentId>,
    constructors: BTreeMap<u16, Vec<usize>>,
    entities: BTreeMap<(u16, Vec<u8>), Vec<usize>>,
    charged_bytes: usize,
    visited_nodes: usize,
    property_punning: bool,
    requested_rows: AtomicU64,
}
fn budget(limits: &Limits, rows: usize, bytes: usize, base: usize) -> NativeResult<()> {
    for (key, value) in [
        (LimitKey::MaxIndexRows, rows),
        (LimitKey::MaxIndexBytes, bytes),
        (LimitKey::MaxMemoryBytes, base + bytes.saturating_mul(2)),
    ] {
        let max = limits.value(key);
        if max != 0 && value as u64 > max {
            return Err(limits.resource_limit(
                key,
                value as u64,
                "native axiom index exceeds budget",
            ));
        }
    }
    Ok(())
}
#[pymethods]
impl NativeAxiomIndexV1 {
    fn _report_v1(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let result = PyDict::new(py);
        result.set_item("backend", "native")?;
        result.set_item("scanned_roots", self.roots.len())?;
        result.set_item("visited_structural_nodes", self.visited_nodes)?;
        result.set_item("constructor_groups", self.constructors.len())?;
        result.set_item("has_object_data_property_punning", self.property_punning)?;
        result.set_item("entity_constructor_groups", self.entities.len())?;
        result.set_item("allocation_charge_bytes", self.charged_bytes)?;
        result.set_item(
            "requested_rows",
            self.requested_rows.load(Ordering::Relaxed),
        )?;
        Ok(result.unbind())
    }
    #[pyo3(signature=(tags,entity,start,max_rows,config,cancel=None))]
    #[allow(clippy::too_many_arguments)]
    fn _page_v1(
        &self,
        py: Python<'_>,
        tags: Vec<u16>,
        entity: Option<Vec<u8>>,
        start: usize,
        max_rows: usize,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<Py<PyTuple>> {
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        let (rows, cursor) = crate::run_detached(py, |interrupt| {
            let typed = self.storage.typed_structural()?;
            let base = typed.arena().counters().retained_bytes as usize
                + typed.external_retained_bytes()
                + self.charged_bytes;
            let mut guard = Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                interrupt.clone(),
            );
            guard.check(0, true)?;
            let groups = if tags.is_empty() && entity.is_none() {
                self.constructors.values().collect::<Vec<_>>()
            } else {
                tags.iter()
                    .filter_map(|tag| match &entity {
                        Some(key) => self.entities.get(&(*tag, key.clone())),
                        None => self.constructors.get(tag),
                    })
                    .collect::<Vec<_>>()
            };
            budget(&limits, groups.len(), groups.len() * 64, base)?;
            let mut heap = BinaryHeap::new();
            for (group, positions) in groups.iter().enumerate() {
                let offset = positions.partition_point(|position| *position < start);
                if offset < positions.len() {
                    heap.push(Reverse((positions[offset], group, offset)));
                }
            }
            let mut rows = Vec::new();
            let mut cursor = start;
            let mut bytes = groups.len() * 64;
            while rows.len() < max_rows {
                let Some(Reverse((position, group, offset))) = heap.pop() else {
                    cursor = self.roots.len();
                    break;
                };
                guard.check(rows.len() as u64, false)?;
                let row = typed.arena().encode(
                    self.roots[position],
                    &limits,
                    cancellation.clone(),
                    Some(interrupt.clone()),
                    base + bytes,
                )?;
                bytes = bytes
                    .checked_add(row.len() * 2 + 32)
                    .ok_or_else(|| NativeError::limit("axiom page overflow"))?;
                budget(&limits, rows.len() + 1, bytes, base)?;
                rows.push(row);
                cursor = position + 1;
                if offset + 1 < groups[group].len() {
                    heap.push(Reverse((groups[group][offset + 1], group, offset + 1)));
                }
            }
            guard.check(rows.len() as u64, true)?;
            Ok((rows, cursor))
        })?;
        self.requested_rows
            .fetch_add(rows.len() as u64, Ordering::Relaxed);
        let result = PyList::empty(py);
        for row in rows {
            result.append(PyBytes::new(py, &row))?;
        }
        PyTuple::new(
            py,
            [result.into_any(), cursor.into_pyobject(py)?.into_any()],
        )
        .map(Bound::unbind)
    }
    #[pyo3(signature=(tag,encoded,config,cancel=None))]
    fn _contains_v1(
        &self,
        py: Python<'_>,
        tag: u16,
        encoded: Vec<u8>,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<bool> {
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        crate::run_detached(py, |interrupt| {
            let mut guard = Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                interrupt.clone(),
            );
            guard.check(0, true)?;
            let Some(positions) = self.constructors.get(&tag) else {
                return Ok(false);
            };
            let typed = self.storage.typed_structural()?;
            let mut lo = 0;
            let mut hi = positions.len();
            while lo < hi {
                let mid = lo + (hi - lo) / 2;
                let value = typed.arena().encode(
                    self.roots[positions[mid]],
                    &limits,
                    cancellation.clone(),
                    Some(interrupt.clone()),
                    self.charged_bytes + encoded.len() + typed.external_retained_bytes(),
                )?;
                match value.cmp(&encoded) {
                    std::cmp::Ordering::Equal => return Ok(true),
                    std::cmp::Ordering::Less => lo = mid + 1,
                    std::cmp::Ordering::Greater => hi = mid,
                }
            }
            Ok(false)
        })
    }
    fn _count_v1(&self, tags: Vec<u16>, entity: Option<Vec<u8>>) -> usize {
        if tags.is_empty() && entity.is_none() {
            return self.roots.len();
        }
        tags.iter()
            .map(|tag| match &entity {
                Some(key) => self.entities.get(&(*tag, key.clone())).map_or(0, Vec::len),
                None => self.constructors.get(tag).map_or(0, Vec::len),
            })
            .sum()
    }
}
#[pyfunction]
#[pyo3(signature=(handle,scope,document_ordinal,config,cancel=None))]
fn _axiom_index_v1(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
    scope: &str,
    document_ordinal: Option<u64>,
    config: &Bound<'_, PyBytes>,
    cancel: Option<PyRef<'_, Cancellation>>,
) -> PyResult<NativeAxiomIndexV1> {
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
        let base = arena.counters().retained_bytes as usize + typed.external_retained_bytes();
        budget(&limits, source.len(), source.len() * 32, base)?;
        let mut constructors: BTreeMap<u16, Vec<usize>> = BTreeMap::new();
        let mut entities: BTreeMap<(u16, Vec<u8>), Vec<usize>> = BTreeMap::new();
        let mut keys: HashMap<ComponentId, Vec<u8>> = HashMap::new();
        let mut charged_bytes = source.len() * 32;
        let mut visited_nodes = 0usize;
        let mut property_kinds: BTreeMap<Vec<u8>, u8> = BTreeMap::new();
        let mut guard = Guard::with_interrupt(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
            interrupt.clone(),
        );
        guard.check(0, true)?;
        for (position, root) in source.iter().enumerate() {
            let tag = arena.tag(*root)?;
            constructors.entry(tag).or_default().push(position);
            let mut pending = vec![ComponentFieldRef::Node(*root)];
            let mut seen = HashSet::new();
            let mut local = BTreeSet::new();
            while let Some(field) = pending.pop() {
                visited_nodes += 1;
                guard.check(visited_nodes as u64, false)?;
                match field {
                    ComponentFieldRef::Node(id) => {
                        if !seen.insert(id) {
                            continue;
                        }
                        budget(
                            &limits,
                            source.len(),
                            charged_bytes + (seen.len() + pending.len()) * 64,
                            base,
                        )?;
                        let record = arena.record(id)?;
                        if record.tag() == 2 {
                            if let std::collections::hash_map::Entry::Vacant(entry) = keys.entry(id)
                            {
                                let key = arena.encode(
                                    id,
                                    &limits,
                                    cancellation.clone(),
                                    Some(interrupt.clone()),
                                    base + charged_bytes,
                                )?;
                                charged_bytes += key.len() + 64;
                                budget(&limits, source.len(), charged_bytes, base)?;
                                entry.insert(key);
                            }
                            if let ComponentFieldRef::Enum(
                                kind @ (b"object_property" | b"data_property"),
                            ) = record.field(0)?
                            {
                                if let ComponentFieldRef::Node(iri) = record.field(1)? {
                                    if let ComponentFieldRef::Text(value) =
                                        arena.record(iri)?.field(0)?
                                    {
                                        if !property_kinds.contains_key(value) {
                                            charged_bytes += value.len() + 64;
                                            budget(&limits, source.len(), charged_bytes, base)?;
                                        }
                                        *property_kinds.entry(value.to_vec()).or_default() |=
                                            if kind == b"object_property" { 1 } else { 2 };
                                    }
                                }
                            }
                            local.insert(keys[&id].clone());
                        }
                        for i in 0..record.field_count() {
                            pending.push(record.field(i)?);
                        }
                    }
                    ComponentFieldRef::CanonicalSet(items)
                    | ComponentFieldRef::OrderedSequence(items) => {
                        budget(
                            &limits,
                            source.len(),
                            charged_bytes + (seen.len() + pending.len() + items.len()) * 64,
                            base,
                        )?;
                        for i in 0..items.len() {
                            pending.push(items.item(i)?);
                        }
                    }
                    _ => {}
                }
            }
            for key in local {
                charged_bytes += key.len() + 96;
                budget(&limits, source.len(), charged_bytes, base)?;
                entities.entry((tag, key)).or_default().push(position);
            }
        }
        guard.check(visited_nodes as u64, true)?;
        Ok(NativeAxiomIndexV1 {
            storage: Arc::clone(&storage),
            roots: source.to_vec(),
            constructors,
            entities,
            charged_bytes,
            visited_nodes,
            property_punning: property_kinds.values().any(|value| *value == 3),
            requested_rows: AtomicU64::new(0),
        })
    })
}
pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("NATIVE_AXIOM_INDEX_API_VERSION", 1)?;
    module.add_class::<NativeAxiomIndexV1>()?;
    module.add_function(wrap_pyfunction!(_axiom_index_v1, module)?)?;
    Ok(())
}
