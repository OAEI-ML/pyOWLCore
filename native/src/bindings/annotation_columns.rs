//! Bounded annotation publication from retained roots, without Python axiom rows.
use std::collections::{BTreeMap, BTreeSet};
use std::mem::size_of;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyModule, PyTuple};

use crate::cancel::{Cancellation, Guard};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{
    structural_digest_v2, ComponentFieldRef, ComponentId, NativeComponentArena,
    NativeComponentBuilder,
};
use crate::publication::{NativeSnapshotHandle, PublicationStorageV2};

#[pyclass(module = "pyowl_core._native", frozen)]
struct NativeAnnotationColumnsV1 {
    storage: Arc<PublicationStorageV2>,
    roots: Vec<(usize, ComponentId)>,
    arenas: Vec<NativeComponentArena>,
    retained_bytes: usize,
    scanned_roots: usize,
    subjects: BTreeMap<Vec<u8>, Vec<usize>>,
    properties: BTreeMap<Vec<u8>, Vec<usize>>,
}

type Row = [Vec<u8>; 5];

type Postings = BTreeMap<Vec<u8>, Vec<usize>>;

fn annotation_postings(
    arenas: &[NativeComponentArena],
    roots: &[(usize, ComponentId)],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: crate::cancel::InterruptSlot,
    external: usize,
) -> NativeResult<(Postings, Postings, usize)> {
    let mut subjects = BTreeMap::new();
    let mut properties = BTreeMap::new();
    let mut bytes = roots.len() * size_of::<(usize, ComponentId)>();
    let mut guard = Guard::with_interrupt(
        cancellation.clone(),
        limits.deadline,
        limits.cancellation_stride,
        interrupt.clone(),
    );
    guard.check(0, true)?;
    for (position, &(arena_index, root)) in roots.iter().enumerate() {
        guard.check(position as u64, false)?;
        let arena = &arenas[arena_index];
        let record = arena.record(root)?;
        for (field, postings) in [(0, &mut properties), (1, &mut subjects)] {
            let node = match record.field(field)? {
                ComponentFieldRef::Node(id) => id,
                _ => {
                    return Err(NativeError::protocol(
                        "annotation index field is not a node",
                    ))
                }
            };
            let key = arena.encode(
                node,
                limits,
                cancellation.clone(),
                Some(interrupt.clone()),
                external + bytes,
            )?;
            let new = !postings.contains_key(&key);
            bytes = bytes
                .checked_add(16 + if new { key.len() + 96 } else { 0 })
                .ok_or_else(|| NativeError::limit("annotation posting size overflow"))?;
            check_budget(limits, LimitKey::MaxIndexBytes, bytes)?;
            check_budget(limits, LimitKey::MaxMemoryBytes, external + 2 * bytes)?;
            let values: &mut Vec<usize> = postings.entry(key).or_default();
            values
                .try_reserve(1)
                .map_err(|_| NativeError::limit("annotation posting allocation failed"))?;
            values.push(position);
        }
    }
    guard.check(roots.len() as u64, true)?;
    Ok((subjects, properties, bytes))
}

fn posting_count(filters: &BTreeSet<Vec<u8>>, postings: &Postings) -> usize {
    filters
        .iter()
        .filter_map(|key| postings.get(key))
        .map(Vec::len)
        .sum()
}

fn selected_positions(
    filters: &BTreeSet<Vec<u8>>,
    postings: &Postings,
    limits: &Limits,
    guard: &mut Guard,
    external: usize,
) -> NativeResult<BTreeSet<usize>> {
    let mut selected = BTreeSet::new();
    for row in filters.iter().filter_map(|key| postings.get(key)).flatten() {
        guard.check(selected.len() as u64, false)?;
        check_budget(limits, LimitKey::MaxIndexRows, selected.len() + 1)?;
        check_budget(limits, LimitKey::MaxIndexBytes, (selected.len() + 1) * 64)?;
        check_budget(
            limits,
            LimitKey::MaxMemoryBytes,
            external + (selected.len() + 1) * 64,
        )?;
        selected.insert(*row);
    }
    Ok(selected)
}

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

    /// Patch only explicit annotation deltas. Base root IDs and immutable arenas
    /// remain retained; canonical lookup costs O(delta * log(annotation roots)).
    #[pyo3(signature = (additions, removals, config, cancel=None))]
    fn _patch_v1(
        &self,
        py: Python<'_>,
        additions: Vec<Vec<u8>>,
        removals: Vec<Vec<u8>>,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<Self> {
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        crate::run_detached(py, |interrupt| {
            let input_bytes =
                additions
                    .iter()
                    .chain(removals.iter())
                    .try_fold(0usize, |n, row| {
                        n.checked_add(row.len() + 32)
                            .ok_or_else(|| NativeError::limit("annotation delta size overflow"))
                    })?;
            let capacity = self
                .roots
                .len()
                .checked_add(additions.len())
                .ok_or_else(|| NativeError::limit("annotation delta row overflow"))?;
            let bytes = capacity
                .checked_mul(size_of::<(usize, ComponentId)>())
                .ok_or_else(|| NativeError::limit("annotation delta index overflow"))?;
            check_budget(&limits, LimitKey::MaxIndexRows, capacity)?;
            check_budget(&limits, LimitKey::MaxIndexBytes, bytes + input_bytes)?;
            let base_bytes = self.arenas.iter().try_fold(0usize, |n, arena| {
                let bytes = usize::try_from(arena.counters().retained_bytes)
                    .map_err(|_| NativeError::limit("native retained memory exceeds usize"))?;
                n.checked_add(bytes)
                    .ok_or_else(|| NativeError::limit("native retained memory overflow"))
            })?;
            let external = base_bytes
                + self.storage.typed_structural()?.external_retained_bytes()
                + self.retained_bytes
                + bytes
                + input_bytes;
            check_budget(&limits, LimitKey::MaxMemoryBytes, external)?;
            let mut guard = Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                interrupt.clone(),
            );
            guard.check(0, true)?;
            let locate = |key: &[u8]| -> NativeResult<Result<usize, usize>> {
                let mut lo = 0;
                let mut hi = self.roots.len();
                while lo < hi {
                    let mid = lo + (hi - lo) / 2;
                    let (arena_index, root) = self.roots[mid];
                    let encoded = self.arenas[arena_index].encode(
                        root,
                        &limits,
                        cancellation.clone(),
                        Some(interrupt.clone()),
                        external,
                    )?;
                    match encoded.as_slice().cmp(key) {
                        std::cmp::Ordering::Less => lo = mid + 1,
                        std::cmp::Ordering::Greater => hi = mid,
                        std::cmp::Ordering::Equal => return Ok(Ok(mid)),
                    }
                }
                Ok(Err(lo))
            };
            let mut removed = BTreeSet::new();
            for row in &removals {
                if let Ok(position) = locate(row)? {
                    removed.insert(position);
                }
            }
            let mut additions = additions;
            additions.sort();
            additions.dedup();
            let mut builder = NativeComponentBuilder::with_control(
                &limits,
                cancellation.clone(),
                Some(interrupt.clone()),
                external,
            )?;
            let mut pending = Vec::new();
            for row in &additions {
                guard.check(pending.len() as u64, false)?;
                match locate(row)? {
                    Ok(position) => {
                        removed.remove(&position);
                    }
                    Err(position) => pending.push((position, builder.intern_canonical(row)?)),
                }
            }
            let frozen = builder.freeze()?;
            let mut inserted: BTreeMap<usize, Vec<ComponentId>> = BTreeMap::new();
            for (position, identifier) in pending {
                let id = frozen.resolve(identifier)?;
                if frozen.arena().tag(id)? != 120 {
                    return Err(NativeError::protocol("non-annotation delta row"));
                }
                inserted.entry(position).or_default().push(id);
            }
            let arena_index = self.arenas.len();
            let mut arenas = self.arenas.clone();
            arenas.push(frozen.into_arena());
            let mut roots = Vec::new();
            roots
                .try_reserve_exact(capacity)
                .map_err(|_| NativeError::limit("annotation delta index allocation failed"))?;
            for position in 0..=self.roots.len() {
                guard.check(position as u64, false)?;
                if let Some(ids) = inserted.remove(&position) {
                    roots.extend(ids.into_iter().map(|id| (arena_index, id)));
                }
                if position < self.roots.len() && !removed.contains(&position) {
                    roots.push(self.roots[position]);
                }
            }
            guard.check(self.roots.len() as u64, true)?;
            let (subjects, properties, retained_bytes) =
                annotation_postings(&arenas, &roots, &limits, cancellation, interrupt, external)?;
            Ok(Self {
                storage: Arc::clone(&self.storage),
                roots,
                arenas,
                retained_bytes,
                scanned_roots: self.scanned_roots,
                subjects,
                properties,
            })
        })
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
            let external = typed.external_retained_bytes() + self.retained_bytes + 2 * filter_bytes;
            let base_bytes = self.arenas.iter().try_fold(0usize, |n, arena| {
                let bytes = usize::try_from(arena.counters().retained_bytes)
                    .map_err(|_| NativeError::limit("native retained memory exceeds usize"))?;
                n.checked_add(bytes)
                    .ok_or_else(|| NativeError::limit("native retained memory overflow"))
            })?;
            let mut guard = Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                interrupt.clone(),
            );
            guard.check(0, true)?;
            let mut rows: Vec<Row> = Vec::new();
            // Visit the smaller requested posting; the other filter is checked
            // against its scalar identity during row publication below.
            let selection = match (&subjects, &properties) {
                (Some(a), Some(b))
                    if posting_count(a, &self.subjects) <= posting_count(b, &self.properties) =>
                {
                    Some((a, &self.subjects))
                }
                (Some(_), Some(b)) | (None, Some(b)) => Some((b, &self.properties)),
                (Some(a), None) => Some((a, &self.subjects)),
                (None, None) => None,
            };
            let selected = selection
                .map(|(keys, postings)| {
                    selected_positions(keys, postings, &limits, &mut guard, base_bytes + external)
                })
                .transpose()?;
            let external = external + selected.as_ref().map_or(0, |rows| rows.len() * 64);
            let mut positions: Box<dyn Iterator<Item = usize>> = match &selected {
                Some(values) => Box::new(values.range(start..).copied()),
                None => Box::new(start..self.roots.len()),
            };
            let mut cursor = start;
            let mut scanned = 0usize;
            let mut payload = 0usize;
            while rows.len() < max_rows {
                let Some(position) = positions.next() else {
                    cursor = self.roots.len();
                    break;
                };
                cursor = position;
                guard.check(scanned as u64, false)?;
                scanned += 1;
                let (arena_index, root) = self.roots[cursor];
                let arena = &self.arenas[arena_index];
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
            guard.check(scanned as u64, true)?;
            Ok((rows, cursor, scanned, payload))
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
            cancellation.clone(),
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
                    .checked_mul(size_of::<(usize, ComponentId)>())
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
            roots.push((0, *identifier));
        }
        guard.check(source.len() as u64, true)?;
        let retained_bytes = roots.capacity() * size_of::<(usize, ComponentId)>();
        Ok((roots, retained_bytes, source.len()))
    })?;
    let arenas = vec![storage
        .typed_structural()
        .map_err(crate::python_error)?
        .arena()
        .clone()];
    let (subjects, properties, retained_bytes) = crate::run_detached(py, |interrupt| {
        let typed = storage.typed_structural()?;
        let base = usize::try_from(typed.arena().counters().retained_bytes)
            .map_err(|_| NativeError::limit("native retained memory exceeds usize"))?;
        annotation_postings(
            &arenas,
            &roots,
            &limits,
            cancellation.clone(),
            interrupt,
            base + typed.external_retained_bytes() + retained_bytes,
        )
    })?;
    Ok(NativeAnnotationColumnsV1 {
        arenas,
        storage,
        roots,
        retained_bytes,
        scanned_roots,
        subjects,
        properties,
    })
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("NATIVE_ANNOTATION_COLUMNS_API_VERSION", 1)?;
    module.add_class::<NativeAnnotationColumnsV1>()?;
    module.add_function(wrap_pyfunction!(_annotation_columns_v1, module)?)?;
    Ok(())
}
