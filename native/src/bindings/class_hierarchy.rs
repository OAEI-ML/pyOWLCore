//! Asserted named-class indexes over retained components; no Python graph build.
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyModule, PyTuple};

use crate::cancel::{Cancellation, Guard};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{structural_digest_v2, ComponentFieldRef, ComponentId, NativeComponentArena};
use crate::publication::{NativeSnapshotHandle, PublicationStorageV2};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct Node(bool, usize);

#[pyclass(module = "pyowl_core._native", frozen)]
struct NativeClassHierarchyV1 {
    storage: Arc<PublicationStorageV2>,
    roots: Vec<ComponentId>,
    classes: Vec<Vec<u8>>,
    class_ids: BTreeMap<Vec<u8>, usize>,
    components: Vec<Vec<usize>>,
    membership: BTreeMap<usize, usize>,
    equivalences: Vec<(Vec<usize>, usize)>,
    equivalences_by_class: BTreeMap<usize, Vec<usize>>,
    edges: Vec<(Node, Node, usize)>,
    parents: BTreeMap<Node, BTreeSet<Node>>,
    children: BTreeMap<Node, BTreeSet<Node>>,
    component_mode: bool,
    ignored: usize,
    scanned_roots: usize,
    charged_bytes: usize,
    neighbor_requests: AtomicU64,
    neighbor_rows_visited: AtomicU64,
}

struct Budget<'a> {
    limits: &'a Limits,
    rows: usize,
    bytes: usize,
    base: usize,
}
impl Budget<'_> {
    fn charge(&mut self, rows: usize, bytes: usize) -> NativeResult<()> {
        self.rows = self
            .rows
            .checked_add(rows)
            .ok_or_else(|| NativeError::limit("hierarchy rows overflow"))?;
        self.bytes = self
            .bytes
            .checked_add(bytes)
            .ok_or_else(|| NativeError::limit("hierarchy bytes overflow"))?;
        let memory = self
            .base
            .checked_add(self.bytes.saturating_mul(2))
            .ok_or_else(|| NativeError::limit("hierarchy memory overflow"))?;
        for (key, value) in [
            (LimitKey::MaxIndexRows, self.rows),
            (LimitKey::MaxIndexBytes, self.bytes),
            (LimitKey::MaxMemoryBytes, memory),
        ] {
            let maximum = self.limits.value(key);
            if maximum != 0 && value as u64 > maximum {
                return Err(self.limits.resource_limit(
                    key,
                    value as u64,
                    "native hierarchy exceeds budget",
                ));
            }
        }
        Ok(())
    }
}

fn named(arena: &NativeComponentArena, id: ComponentId) -> NativeResult<bool> {
    let row = arena.record(id)?;
    Ok(row.tag() == 2 && matches!(row.field(0)?, ComponentFieldRef::Enum(b"class")))
}
fn child(arena: &NativeComponentArena, id: ComponentId, field: usize) -> NativeResult<ComponentId> {
    match arena.record(id)?.field(field)? {
        ComponentFieldRef::Node(value) => Ok(value),
        _ => Err(NativeError::protocol(
            "hierarchy constructor expected node field",
        )),
    }
}
fn find(parents: &mut [usize], mut id: usize) -> usize {
    while parents[id] != id {
        parents[id] = parents[parents[id]];
        id = parents[id];
    }
    id
}

impl NativeClassHierarchyV1 {
    fn members(&self, node: Node) -> Vec<usize> {
        if node.0 {
            self.components[node.1].clone()
        } else {
            vec![node.1]
        }
    }
    fn node_to_python(&self, py: Python<'_>, node: Node) -> PyResult<Py<PyTuple>> {
        let members = PyTuple::new(
            py,
            self.members(node)
                .iter()
                .map(|id| PyBytes::new(py, &self.classes[*id])),
        )?;
        PyTuple::new(
            py,
            [
                members.into_any(),
                node.0.into_pyobject(py)?.to_owned().into_any(),
            ],
        )
        .map(Bound::unbind)
    }
    fn selected_node(&self, members: &[Vec<u8>], component: bool) -> Option<Node> {
        let ids = members
            .iter()
            .map(|value| self.class_ids.get(value).copied())
            .collect::<Option<Vec<_>>>()?;
        if component {
            self.components
                .binary_search(&ids)
                .ok()
                .map(|index| Node(true, index))
        } else if ids.len() == 1 {
            Some(Node(false, ids[0]))
        } else {
            None
        }
    }
}

#[pymethods]
impl NativeClassHierarchyV1 {
    fn _report_v1(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let report = PyDict::new(py);
        for (key, value) in [
            ("scanned_roots", self.scanned_roots),
            ("selected_axioms", self.roots.len()),
            ("indexed_edges", self.edges.len()),
            ("named_classes", self.classes.len()),
            ("ignored_complex_endpoint_count", self.ignored),
            ("allocation_charge_bytes", self.charged_bytes),
        ] {
            report.set_item(key, value)?;
        }
        report.set_item("backend", "native")?;
        report.set_item(
            "neighbor_requests",
            self.neighbor_requests.load(Ordering::Relaxed),
        )?;
        report.set_item(
            "neighbor_rows_visited",
            self.neighbor_rows_visited.load(Ordering::Relaxed),
        )?;
        Ok(report.unbind())
    }

    #[pyo3(signature = (kind, members, component, config, cancel=None))]
    #[allow(clippy::too_many_arguments)]
    fn _nodes_v1(
        &self,
        py: Python<'_>,
        kind: &str,
        members: Vec<Vec<u8>>,
        component: bool,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<Py<PyList>> {
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        self.neighbor_requests.fetch_add(1, Ordering::Relaxed);
        let result = crate::run_detached(py, |interrupt| {
            let mut guard = Guard::with_interrupt(
                cancellation,
                limits.deadline,
                limits.cancellation_stride,
                interrupt,
            );
            guard.check(0, true)?;
            let Some(node) = self.selected_node(&members, component) else {
                return Ok(BTreeSet::new());
            };
            let typed = self.storage.typed_structural()?;
            let base = usize::try_from(typed.arena().counters().retained_bytes)
                .map_err(|_| NativeError::limit("hierarchy owner memory exceeds usize"))?
                + typed.external_retained_bytes()
                + self.charged_bytes;
            let mut budget = Budget {
                limits: &limits,
                rows: 0,
                bytes: 0,
                base,
            };
            let mut result = BTreeSet::new();
            let mut insert = |value| -> NativeResult<()> {
                self.neighbor_rows_visited.fetch_add(1, Ordering::Relaxed);
                guard.check(budget.rows as u64, false)?;
                if !result.contains(&value) {
                    let payload = self
                        .members(value)
                        .iter()
                        .map(|id| self.classes[*id].len() + 64)
                        .sum::<usize>();
                    budget.charge(1, payload + 64)?;
                    result.insert(value);
                }
                Ok(())
            };
            match kind {
                "parents" => {
                    for value in self.parents.get(&node).into_iter().flatten() {
                        insert(*value)?;
                    }
                }
                "children" => {
                    for value in self.children.get(&node).into_iter().flatten() {
                        insert(*value)?;
                    }
                }
                "component" => {
                    insert(
                        self.membership
                            .get(&node.1)
                            .filter(|_| self.component_mode)
                            .map_or(node, |group| Node(true, *group)),
                    )?;
                }
                "equivalents" => {
                    if self.component_mode {
                        if let Some(group) = self.membership.get(&node.1) {
                            for id in &self.components[*group] {
                                if *id != node.1 {
                                    insert(Node(false, *id))?;
                                }
                            }
                        }
                    } else {
                        for group in self
                            .equivalences_by_class
                            .get(&node.1)
                            .into_iter()
                            .flatten()
                        {
                            for id in &self.equivalences[*group].0 {
                                if *id != node.1 {
                                    insert(Node(false, *id))?;
                                }
                            }
                        }
                    }
                }
                _ => return Err(NativeError::protocol("unknown hierarchy query")),
            }
            guard.check(budget.rows as u64, true)?;
            Ok(result)
        })?;
        let output = PyList::empty(py);
        for node in result {
            output.append(self.node_to_python(py, node)?)?;
        }
        Ok(output.unbind())
    }

    #[pyo3(signature = (kind, start, max_rows, config, cancel=None))]
    fn _records_v1(
        &self,
        py: Python<'_>,
        kind: &str,
        start: usize,
        max_rows: usize,
        config: &Bound<'_, PyBytes>,
        cancel: Option<PyRef<'_, Cancellation>>,
    ) -> PyResult<Py<PyList>> {
        let limits = crate::limits_from_python(config.as_any())?;
        let cancellation = crate::cancellation_or_default(cancel);
        let total = match kind {
            "edges" => self.edges.len(),
            "equivalences" => self.equivalences.len(),
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "unknown hierarchy records",
                ))
            }
        };
        if max_rows == 0 || start > total {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "invalid hierarchy page",
            ));
        }
        let stop = start.saturating_add(max_rows).min(total);
        let rows = crate::run_detached(py, |interrupt| {
            let typed = self.storage.typed_structural()?;
            let mut retained = self.charged_bytes + typed.external_retained_bytes();
            let mut result = Vec::new();
            for ordinal in start..stop {
                let root = if kind == "edges" {
                    self.edges[ordinal].2
                } else {
                    self.equivalences[ordinal].1
                };
                let bytes = typed.arena().encode(
                    self.roots[root],
                    &limits,
                    cancellation.clone(),
                    Some(interrupt.clone()),
                    retained,
                )?;
                retained = retained
                    .checked_add(bytes.len().saturating_mul(2) + 64)
                    .ok_or_else(|| NativeError::limit("hierarchy page memory overflow"))?;
                result.push(bytes);
            }
            Ok(result)
        })?;
        let output = PyList::empty(py);
        for (offset, bytes) in rows.into_iter().enumerate() {
            let ordinal = start + offset;
            let axiom = PyBytes::new(py, &bytes);
            let digest = PyBytes::new(py, &structural_digest_v2(&bytes));
            let row = if kind == "edges" {
                let (child, parent, _) = self.edges[ordinal];
                PyTuple::new(
                    py,
                    [
                        self.node_to_python(py, child)?.into_any(),
                        self.node_to_python(py, parent)?.into_any(),
                        axiom.into_any().unbind(),
                        digest.into_any().unbind(),
                    ],
                )?
            } else {
                let members = PyTuple::new(
                    py,
                    self.equivalences[ordinal]
                        .0
                        .iter()
                        .map(|id| PyBytes::new(py, &self.classes[*id])),
                )?;
                PyTuple::new(
                    py,
                    [members.into_any(), axiom.into_any(), digest.into_any()],
                )?
            };
            output.append(row)?;
        }
        Ok(output.unbind())
    }
}

#[pyfunction]
#[pyo3(signature = (handle, scope, document_ordinal, handling, include_disjoint_union, config, cancel=None))]
#[allow(clippy::too_many_arguments)]
fn _class_hierarchy_v1(
    py: Python<'_>,
    handle: PyRef<'_, NativeSnapshotHandle>,
    scope: &str,
    document_ordinal: Option<u64>,
    handling: &str,
    include_disjoint_union: bool,
    config: &Bound<'_, PyBytes>,
    cancel: Option<PyRef<'_, Cancellation>>,
) -> PyResult<NativeClassHierarchyV1> {
    if !matches!(handling, "preserve" | "bidirectional" | "component") {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid equivalence handling",
        ));
    }
    let scope = super::views::encoded_selection(scope, document_ordinal)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let storage = handle.encoded_storage_v2(py)?;
    drop(handle);
    let limits = crate::limits_from_python(config.as_any())?;
    let cancellation = crate::cancellation_or_default(cancel);
    crate::run_detached(py, |interrupt| {
        let typed = storage.typed_structural()?;
        let source = typed.selected_axioms(scope, document_ordinal)?;
        let arena = typed.arena();
        let base = usize::try_from(arena.counters().retained_bytes)
            .map_err(|_| NativeError::limit("hierarchy owner memory exceeds usize"))?
            + typed.external_retained_bytes();
        let mut budget = Budget {
            limits: &limits,
            rows: 0,
            bytes: 0,
            base,
        };
        let mut guard = Guard::with_interrupt(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
            interrupt.clone(),
        );
        let mut roots = Vec::new();
        let mut keys = HashMap::new();
        let mut raw_edges = Vec::new();
        let mut raw_groups = Vec::new();
        let mut ignored = 0;
        for (ordinal, root) in source.iter().enumerate() {
            guard.check(ordinal as u64, ordinal == 0)?;
            let record = arena.record(*root)?;
            if !matches!(record.tag(), 61 | 62 | 64) {
                continue;
            }
            budget.charge(1, 64)?;
            let position = roots.len();
            roots.push(*root);
            let mut admit = |id| -> NativeResult<bool> {
                if !named(arena, id)? {
                    ignored += 1;
                    return Ok(false);
                }
                budget.charge(1, 32)?;
                if !keys.contains_key(&id) {
                    let key = arena.encode(
                        id,
                        &limits,
                        cancellation.clone(),
                        Some(interrupt.clone()),
                        budget.bytes + typed.external_retained_bytes(),
                    )?;
                    budget.charge(1, key.len() * 3 + 160)?;
                    keys.insert(id, key);
                }
                Ok(true)
            };
            if record.tag() == 61 {
                let left = child(arena, *root, 0)?;
                let right = child(arena, *root, 1)?;
                let left_named = admit(left)?;
                let right_named = admit(right)?;
                if left_named && right_named {
                    raw_edges.push((left, right, position));
                }
            } else {
                let field = if record.tag() == 62 { 0 } else { 1 };
                let ComponentFieldRef::CanonicalSet(expressions) = record.field(field)? else {
                    return Err(NativeError::protocol(
                        "hierarchy expressions are not a canonical set",
                    ));
                };
                let mut members = Vec::new();
                for index in 0..expressions.len() {
                    let ComponentFieldRef::Node(id) = expressions.item(index)? else {
                        return Err(NativeError::protocol("hierarchy expression is not a node"));
                    };
                    if admit(id)? {
                        members.push(id);
                    }
                }
                if record.tag() == 62 {
                    if members.len() >= 2 {
                        raw_groups.push((members, position));
                    }
                } else if include_disjoint_union {
                    let defined = child(arena, *root, 0)?;
                    admit(defined)?;
                    raw_edges.extend(
                        members
                            .into_iter()
                            .map(|member| (member, defined, position)),
                    );
                }
            }
        }
        let ordered = keys
            .iter()
            .map(|(id, key)| (key.clone(), *id))
            .collect::<BTreeMap<_, _>>();
        let classes = ordered.keys().cloned().collect::<Vec<_>>();
        let class_ids = classes
            .iter()
            .cloned()
            .enumerate()
            .map(|(index, key)| (key, index))
            .collect::<BTreeMap<_, _>>();
        // Different imported arena partitions can retain the same named class.
        // Every physical component maps to its canonical class identity.
        let ids = keys
            .iter()
            .map(|(id, key)| (*id, class_ids[key]))
            .collect::<HashMap<_, _>>();
        let equivalences = raw_groups
            .into_iter()
            .map(|(members, root)| {
                (
                    members.into_iter().map(|id| ids[&id]).collect::<Vec<_>>(),
                    root,
                )
            })
            .collect::<Vec<_>>();
        let mut equivalences_by_class: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        let mut union = (0..classes.len()).collect::<Vec<_>>();
        for (group, (members, _)) in equivalences.iter().enumerate() {
            for member in members {
                guard.check(budget.rows as u64, false)?;
                budget.charge(1, 48)?;
                equivalences_by_class
                    .entry(*member)
                    .or_default()
                    .push(group);
                let left = find(&mut union, members[0]);
                let right = find(&mut union, *member);
                union[left.max(right)] = left.min(right);
            }
        }
        let mut grouped: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for id in equivalences_by_class.keys() {
            grouped.entry(find(&mut union, *id)).or_default().push(*id);
        }
        let mut components = grouped.into_values().collect::<Vec<_>>();
        components.sort();
        let membership = components
            .iter()
            .enumerate()
            .flat_map(|(index, members)| members.iter().map(move |id| (*id, index)))
            .collect::<BTreeMap<_, _>>();
        let node = |id| {
            let index = ids[&id];
            if handling == "component" {
                membership
                    .get(&index)
                    .map_or(Node(false, index), |group| Node(true, *group))
            } else {
                Node(false, index)
            }
        };
        if handling == "bidirectional" {
            for (members, root) in &equivalences {
                for left in members {
                    for right in members {
                        if left != right {
                            guard.check(budget.rows as u64, false)?;
                            budget.charge(1, 96)?;
                            raw_edges.push((
                                ordered[&classes[*left]],
                                ordered[&classes[*right]],
                                *root,
                            ));
                        }
                    }
                }
            }
        }
        let mut edges = BTreeSet::new();
        let mut parents: BTreeMap<Node, BTreeSet<Node>> = BTreeMap::new();
        let mut children: BTreeMap<Node, BTreeSet<Node>> = BTreeMap::new();
        for (left, right, root) in raw_edges {
            guard.check(budget.rows as u64, false)?;
            budget.charge(1, 240)?;
            let left = node(left);
            let right = node(right);
            if left != right || handling != "component" {
                parents.entry(left).or_default().insert(right);
                children.entry(right).or_default().insert(left);
            }
            if left != right {
                edges.insert((left, right, root));
            }
        }
        guard.check(source.len() as u64, true)?;
        Ok(NativeClassHierarchyV1 {
            storage: Arc::clone(&storage),
            roots,
            classes,
            class_ids,
            components,
            membership,
            equivalences,
            equivalences_by_class,
            edges: edges.into_iter().collect(),
            parents,
            children,
            component_mode: handling == "component",
            ignored,
            scanned_roots: source.len(),
            charged_bytes: budget.bytes,
            neighbor_requests: AtomicU64::new(0),
            neighbor_rows_visited: AtomicU64::new(0),
        })
    })
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("NATIVE_CLASS_HIERARCHY_API_VERSION", 1)?;
    module.add_class::<NativeClassHierarchyV1>()?;
    module.add_function(wrap_pyfunction!(_class_hierarchy_v1, module)?)?;
    Ok(())
}
