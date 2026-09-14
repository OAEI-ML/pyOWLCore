//! Named structural features: retained IDs, query-local reduction, no OWL reasoning.
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyModule};

use crate::cancel::{Cancellation, Guard};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{Category, ComponentFieldRef, ComponentId, NativeComponentArena};
use crate::publication::{NativeSnapshotHandle, PublicationStorageV2};

const BOUNDS: [&str; 2] = [
    "http://www.w3.org/2002/07/owl#Thing",
    "http://www.w3.org/2002/07/owl#Nothing",
];

struct Budget<'a> {
    limits: &'a Limits,
    base: usize,
    bytes: usize,
    peak: usize,
}
impl Budget<'_> {
    fn charge(&mut self, bytes: usize) -> NativeResult<()> {
        let next = self
            .bytes
            .checked_add(bytes)
            .ok_or_else(|| NativeError::limit("class feature allocation overflow"))?;
        let live = next
            .checked_mul(2)
            .and_then(|n| n.checked_add(self.base))
            .ok_or_else(|| NativeError::limit("class feature memory overflow"))?;
        for (key, value) in [
            (LimitKey::MaxIndexBytes, next),
            (LimitKey::MaxMemoryBytes, live),
        ] {
            if self.limits.value(key) != 0 && value as u64 > self.limits.value(key) {
                return Err(self
                    .limits
                    .resource_limit(key, value as u64, "class feature budget"));
            }
        }
        self.bytes = next;
        self.peak = self.peak.max(next);
        Ok(())
    }
    fn release(&mut self, bytes: usize) {
        self.bytes -= bytes;
    }
}

fn node(arena: &NativeComponentArena, id: ComponentId, field: usize) -> NativeResult<ComponentId> {
    match arena.record(id)?.field(field)? {
        ComponentFieldRef::Node(value) => Ok(value),
        _ => Err(NativeError::protocol("class feature expected node field")),
    }
}
fn named(arena: &NativeComponentArena, id: ComponentId) -> NativeResult<Option<&str>> {
    let value = arena.record(id)?;
    if value.tag() != 2 || !matches!(value.field(0)?, ComponentFieldRef::Enum(b"class")) {
        return Ok(None);
    }
    match arena.record(node(arena, id, 1)?)?.field(0)? {
        ComponentFieldRef::Text(iri) => std::str::from_utf8(iri)
            .map(Some)
            .map_err(|_| NativeError::protocol("invalid native class IRI")),
        _ => Err(NativeError::protocol("class feature expected IRI")),
    }
}
fn expressions(arena: &NativeComponentArena, id: ComponentId) -> NativeResult<Vec<ComponentId>> {
    let ComponentFieldRef::CanonicalSet(values) = arena.record(id)?.field(0)? else {
        return Err(NativeError::protocol(
            "class feature expected expression set",
        ));
    };
    (0..values.len())
        .map(|i| match values.item(i)? {
            ComponentFieldRef::Node(value) => Ok(value),
            _ => Err(NativeError::protocol("class feature expected expression")),
        })
        .collect()
}
fn find(parents: &mut [usize], mut id: usize) -> usize {
    while parents[id] != id {
        parents[id] = parents[parents[id]];
        id = parents[id];
    }
    id
}
fn admit(
    iri: &str,
    names: &mut Vec<String>,
    ids: &mut BTreeMap<String, usize>,
    budget: &mut Budget<'_>,
) -> NativeResult<usize> {
    if let Some(id) = ids.get(iri) {
        return Ok(*id);
    }
    budget.charge(
        iri.len()
            .checked_mul(4)
            .and_then(|n| n.checked_add(256))
            .ok_or_else(|| NativeError::limit("class feature key overflow"))?,
    )?;
    let id = names.len();
    names.push(iri.to_owned());
    ids.insert(iri.to_owned(), id);
    Ok(id)
}

#[pyclass(module = "pyowl_core._native", frozen)]
struct NativeClassFeaturesV1 {
    storage: Arc<PublicationStorageV2>,
    names: Vec<String>,
    ids: BTreeMap<String, usize>,
    membership: Vec<usize>,
    components: BTreeMap<usize, Vec<usize>>,
    parents: BTreeMap<usize, BTreeSet<usize>>,
    children: BTreeMap<usize, BTreeSet<usize>>,
    restrictions: BTreeMap<usize, Vec<ComponentId>>,
    include_builtins: bool,
    scanned_roots: usize,
    selected_axioms: usize,
    charged_bytes: usize,
    queries: AtomicU64,
    visits: AtomicU64,
    published: AtomicU64,
}
struct Query<'a> {
    index: &'a NativeClassFeaturesV1,
    budget: Budget<'a>,
    guard: Guard,
    visits: u64,
}
impl Query<'_> {
    fn visit(&mut self) -> NativeResult<()> {
        self.visits += 1;
        self.guard.check(self.visits, false)
    }
    fn insert(&mut self, set: &mut BTreeSet<usize>, id: usize) -> NativeResult<()> {
        self.visit()?;
        if !set.contains(&id) {
            self.budget.charge(64)?;
            set.insert(id);
        }
        Ok(())
    }
    fn neighbors(&mut self, id: usize, up: bool) -> NativeResult<BTreeSet<usize>> {
        let mut result = BTreeSet::new();
        let table = if up {
            &self.index.parents
        } else {
            &self.index.children
        };
        for value in table.get(&id).into_iter().flatten() {
            self.insert(&mut result, *value)?;
        }
        Ok(result)
    }
    fn reachable(&mut self, from: usize, target: usize) -> NativeResult<bool> {
        let mut pending = self.neighbors(from, true)?;
        let mut seen = BTreeSet::new();
        let mut found = false;
        while let Some(id) = pending.pop_first() {
            self.budget.release(64);
            if id == target {
                found = true;
                break;
            }
            if seen.contains(&id) {
                continue;
            }
            self.insert(&mut seen, id)?;
            for parent in self.index.parents.get(&id).into_iter().flatten() {
                if !seen.contains(parent) {
                    self.insert(&mut pending, *parent)?;
                }
            }
        }
        self.budget.release((seen.len() + pending.len()) * 64);
        Ok(found)
    }
    fn direct_parents(&mut self, id: usize) -> NativeResult<BTreeSet<usize>> {
        let candidates = self.neighbors(id, true)?;
        let mut result = BTreeSet::new();
        for parent in &candidates {
            let mut redundant = false;
            for other in &candidates {
                if parent != other && self.reachable(*other, *parent)? {
                    redundant = true;
                    break;
                }
            }
            if !redundant {
                self.insert(&mut result, *parent)?;
            }
        }
        self.budget.release(candidates.len() * 64);
        Ok(result)
    }
    fn direct_children(&mut self, id: usize) -> NativeResult<BTreeSet<usize>> {
        let candidates = self.neighbors(id, false)?;
        let mut result = BTreeSet::new();
        for child in &candidates {
            let parents = self.direct_parents(*child)?;
            if parents.contains(&id) {
                self.insert(&mut result, *child)?;
            }
            self.budget.release(parents.len() * 64);
        }
        self.budget.release(candidates.len() * 64);
        Ok(result)
    }
    fn visible(&self, component: usize) -> bool {
        self.index.include_builtins
            || self.index.components[&component]
                .iter()
                .any(|id| !BOUNDS.contains(&self.index.names[*id].as_str()))
    }
    fn related(&mut self, id: usize, up: bool, transitive: bool) -> NativeResult<BTreeSet<usize>> {
        let mut pending = if up {
            self.direct_parents(id)?
        } else {
            self.direct_children(id)?
        };
        if !transitive {
            return Ok(pending);
        }
        let mut seen = BTreeSet::new();
        while let Some(current) = pending.pop_first() {
            self.budget.release(64);
            if !self.visible(current) || seen.contains(&current) {
                continue;
            }
            self.insert(&mut seen, current)?;
            let next = if up {
                self.direct_parents(current)?
            } else {
                self.direct_children(current)?
            };
            for value in &next {
                if !seen.contains(value) {
                    self.insert(&mut pending, *value)?;
                }
            }
            self.budget.release(next.len() * 64);
        }
        Ok(seen)
    }
}

#[pymethods]
impl NativeClassFeaturesV1 {
    fn _report_v1(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let result = PyDict::new(py);
        result.set_item("backend", "native")?;
        for (name, value) in [
            ("scanned_roots", self.scanned_roots),
            ("selected_axioms", self.selected_axioms),
            ("named_classes", self.names.len()),
            ("allocation_charge_bytes", self.charged_bytes),
        ] {
            result.set_item(name, value)?;
        }
        result.set_item("queries", self.queries.load(Ordering::Relaxed))?;
        result.set_item("neighbor_visits", self.visits.load(Ordering::Relaxed))?;
        result.set_item("published_rows", self.published.load(Ordering::Relaxed))?;
        Ok(result.unbind())
    }

    #[pyo3(signature = (kind, iri, config, cancel=None))]
    fn _query_v1(
        &self,
        py: Python<'_>,
        kind: &str,
        iri: &str,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<Py<PyList>> {
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        self.queries.fetch_add(1, Ordering::Relaxed);
        let result = crate::run_detached(py, |interrupt| {
            let typed = self.storage.typed_structural()?;
            let base = usize::try_from(typed.arena().counters().retained_bytes)
                .ok()
                .and_then(|n| n.checked_add(typed.external_retained_bytes()))
                .and_then(|n| n.checked_add(self.charged_bytes))
                .ok_or_else(|| NativeError::limit("class feature owner memory overflow"))?;
            let mut query = Query {
                index: self,
                budget: Budget {
                    limits: &limits,
                    base,
                    bytes: 0,
                    peak: 0,
                },
                guard: Guard::with_interrupt(
                    cancellation.clone(),
                    limits.deadline,
                    limits.cancellation_stride,
                    interrupt.clone(),
                ),
                visits: 0,
            };
            query.guard.check(0, true)?;
            query.budget.charge(0)?;
            let Some(id) = self.ids.get(iri).copied() else {
                return Ok(Vec::new());
            };
            let mut output = Vec::new();
            if kind == "restrictions" {
                let mut seen = BTreeSet::new();
                for expression in self.restrictions.get(&id).into_iter().flatten() {
                    query.visit()?;
                    let bytes = typed.arena().encode(
                        *expression,
                        &limits,
                        cancellation.clone(),
                        Some(interrupt.clone()),
                        typed.external_retained_bytes() + self.charged_bytes + query.budget.bytes,
                    )?;
                    if !seen.contains(&bytes) {
                        query.budget.charge(
                            bytes
                                .len()
                                .checked_mul(4)
                                .and_then(|n| n.checked_add(128))
                                .ok_or_else(|| {
                                    NativeError::limit("class feature result overflow")
                                })?,
                        )?;
                        seen.insert(bytes.clone());
                        output.push(bytes);
                    }
                }
            } else {
                let component = self.membership[id];
                let groups = match kind {
                    "parents" => query.related(component, true, false)?,
                    "children" => query.related(component, false, false)?,
                    "ancestors" => query.related(component, true, true)?,
                    "descendants" => query.related(component, false, true)?,
                    "component" => {
                        let mut result = BTreeSet::new();
                        query.insert(&mut result, component)?;
                        result
                    }
                    _ => return Err(NativeError::protocol("unknown class feature query")),
                };
                for group in groups {
                    for (ordinal, member) in self.components[&group].iter().enumerate() {
                        query.guard.check(ordinal as u64, false)?;
                        let iri = &self.names[*member];
                        if kind == "component"
                            || self.include_builtins
                            || !BOUNDS.contains(&iri.as_str())
                        {
                            query.budget.charge(iri.len() * 2 + 64)?;
                            output.push(iri.as_bytes().to_vec());
                        }
                    }
                }
                output.sort_unstable();
            }
            self.visits.fetch_add(query.visits, Ordering::Relaxed);
            self.published
                .fetch_add(output.len() as u64, Ordering::Relaxed);
            Ok(output)
        })?;
        let output = PyList::empty(py);
        for value in result {
            output.append(PyBytes::new(py, &value))?;
        }
        Ok(output.unbind())
    }
}

#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, equivalent_operands, include_builtins, config, cancel=None))]
#[allow(clippy::too_many_arguments)]
fn _class_features_v1(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
    scope: &str,
    document_ordinal: Option<u64>,
    equivalent_operands: bool,
    include_builtins: bool,
    config: &Bound<'_, PyBytes>,
    cancel: Option<PyRef<'_, Cancellation>>,
) -> PyResult<NativeClassFeaturesV1> {
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
            .ok()
            .and_then(|n| n.checked_add(typed.external_retained_bytes()))
            .ok_or_else(|| NativeError::limit("class feature owner memory overflow"))?;
        let mut budget = Budget {
            limits: &limits,
            base,
            bytes: 0,
            peak: 0,
        };
        budget.charge(512)?;
        let mut guard = Guard::with_interrupt(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
            interrupt.clone(),
        );
        guard.check(0, true)?;
        let mut roots = Vec::new();
        for (i, root) in source.iter().enumerate() {
            guard.check(i as u64, i == 0)?;
            if matches!(arena.record(*root)?.tag(), 61 | 62) {
                let rows = roots
                    .len()
                    .checked_add(1)
                    .ok_or_else(|| NativeError::limit("class feature row count overflow"))?;
                let maximum = limits.value(LimitKey::MaxIndexRows);
                if maximum != 0 && rows as u64 > maximum {
                    return Err(limits.resource_limit(
                        LimitKey::MaxIndexRows,
                        rows as u64,
                        "class feature rows",
                    ));
                }
                budget.charge(64)?;
                roots.push(*root);
            }
        }
        arena.sort_deduplicate_ids(
            &mut roots,
            Category::Axiom,
            &limits,
            cancellation.clone(),
            Some(interrupt.clone()),
            typed.external_retained_bytes() + budget.bytes,
        )?;
        let mut names = Vec::new();
        let mut ids = BTreeMap::new();
        let mut raw_edges = Vec::new();
        let mut groups = Vec::new();
        let mut restrictions: BTreeMap<usize, Vec<ComponentId>> = BTreeMap::new();
        // Constructor partition order is the established feature order: subclass,
        // then equivalence axioms, canonical within each partition.
        for tag in [61, 62] {
            for (ordinal, root) in roots.iter().enumerate() {
                guard.check(ordinal as u64, false)?;
                if arena.record(*root)?.tag() != tag {
                    continue;
                }
                if tag == 61 {
                    let left = node(arena, *root, 0)?;
                    let right = node(arena, *root, 1)?;
                    if let Some(iri) = named(arena, left)? {
                        let from = admit(iri, &mut names, &mut ids, &mut budget)?;
                        budget.charge(64)?;
                        restrictions.entry(from).or_default().push(right);
                        if let Some(iri) = named(arena, right)? {
                            let to = admit(iri, &mut names, &mut ids, &mut budget)?;
                            budget.charge(32)?;
                            raw_edges.push((from, to));
                        }
                    }
                } else {
                    // Charge the temporary operand vector before allocation.
                    let ComponentFieldRef::CanonicalSet(values) = arena.record(*root)?.field(0)?
                    else {
                        return Err(NativeError::protocol(
                            "class feature expected equivalence operands",
                        ));
                    };
                    budget.charge(
                        values
                            .len()
                            .checked_mul(128)
                            .ok_or_else(|| NativeError::limit("class feature operands overflow"))?,
                    )?;
                    let values = expressions(arena, *root)?;
                    let mut anchors = Vec::new();
                    for (index, expression) in values.iter().enumerate() {
                        guard.check(index as u64, false)?;
                        if let Some(iri) = named(arena, *expression)? {
                            anchors.push(admit(iri, &mut names, &mut ids, &mut budget)?);
                        }
                    }
                    budget.charge(64)?;
                    groups.push(anchors.clone());
                    for anchor in anchors {
                        for expression in &values {
                            if named(arena, *expression)?.is_some() {
                                continue;
                            }
                            budget.charge(64)?;
                            restrictions.entry(anchor).or_default().push(*expression);
                            let shape = arena.record(*expression)?.tag();
                            if equivalent_operands && matches!(shape, 30 | 31) {
                                let ComponentFieldRef::CanonicalSet(operands) =
                                    arena.record(*expression)?.field(0)?
                                else {
                                    return Err(NativeError::protocol(
                                        "class feature expected named operands",
                                    ));
                                };
                                for i in 0..operands.len() {
                                    guard.check(i as u64, false)?;
                                    let ComponentFieldRef::Node(operand) = operands.item(i)? else {
                                        return Err(NativeError::protocol(
                                            "class feature expected operand",
                                        ));
                                    };
                                    if let Some(iri) = named(arena, operand)? {
                                        let id = admit(iri, &mut names, &mut ids, &mut budget)?;
                                        budget.charge(32)?;
                                        raw_edges.push(if shape == 30 {
                                            (anchor, id)
                                        } else {
                                            (id, anchor)
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        budget.charge(
            names
                .len()
                .checked_mul(192)
                .ok_or_else(|| NativeError::limit("class feature components overflow"))?,
        )?;
        let mut membership = (0..names.len()).collect::<Vec<_>>();
        for group in groups {
            if let Some(first) = group.first() {
                for member in &group[1..] {
                    let a = find(&mut membership, *first);
                    let b = find(&mut membership, *member);
                    membership[a.max(b)] = a.min(b);
                }
            }
        }
        let mut components: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for id in 0..names.len() {
            guard.check(id as u64, false)?;
            let group = find(&mut membership, id);
            components.entry(group).or_default().push(id);
        }
        let mut parents: BTreeMap<usize, BTreeSet<usize>> = BTreeMap::new();
        let mut children: BTreeMap<usize, BTreeSet<usize>> = BTreeMap::new();
        for (ordinal, (left, right)) in raw_edges.into_iter().enumerate() {
            guard.check(ordinal as u64, false)?;
            let left = membership[left];
            let right = membership[right];
            if left != right && !parents.get(&left).is_some_and(|rows| rows.contains(&right)) {
                budget.charge(256)?;
                parents.entry(left).or_default().insert(right);
                children.entry(right).or_default().insert(left);
            }
        }
        Ok(NativeClassFeaturesV1 {
            storage: Arc::clone(&storage),
            names,
            ids,
            membership,
            components,
            parents,
            children,
            restrictions,
            include_builtins,
            scanned_roots: source.len(),
            selected_axioms: roots.len(),
            charged_bytes: budget.peak,
            queries: AtomicU64::new(0),
            visits: AtomicU64::new(0),
            published: AtomicU64::new(0),
        })
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeClassFeaturesV1>()?;
    module.add_function(wrap_pyfunction!(_class_features_v1, module)?)?;
    module.add("NATIVE_CLASS_FEATURES_API_VERSION", 1)?;
    Ok(())
}
