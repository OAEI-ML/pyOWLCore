//! Native document and snapshot scoping for parser-produced anonymous values.
//!
//! Syntax parsers initially use lexical blank labels under the provisional
//! parser scope. This module reproduces the Python model's structural alpha
//! canonicalization before those rows enter retained storage, then derives the
//! distinct one-document snapshot scope used by effective facade owners.

use std::mem::size_of;

use crate::cancel::Cancellation;
#[cfg(test)]
use crate::canonical::iri;
use crate::canonical::{Node, LEXICAL_KEY, PROVISIONAL_SCOPE};
use crate::error::{NativeError, NativeLimitDetails, NativeResult};
use crate::hash::{sha256, Sha256};
use crate::limits::{LimitKey, Limits};
use crate::model::{canonical_field_count, scan_canonical, ScanBudget};
use crate::session::Session;

use super::retained::{functional_document_fingerprint, rdfxml_document_fingerprint};

const DOCUMENT_SCOPE_DOMAIN: &[u8] = b"pyowl-core:document-scope:v2\0";
const SNAPSHOT_SCOPE_DOMAIN: &[u8] = b"pyowl-core:snapshot-document-scope:v2\0";
const ANONYMOUS_KEY_DOMAIN: &[u8] = b"pyowl-core:anonymous-key:v2\0";
const BLANK_GRAPH_DOMAIN: &[u8] = b"pyowl-core:blank-graph:v2\0";
const BLANK_COLOR_DOMAIN: &[u8] = b"pyowl-core:blank-color:v2\0";
const COMPONENT_CLASS_DOMAIN: &[u8] = b"pyowl-core:blank-component-class:v2\0";
const COMPONENT_MANIFEST_DOMAIN: &[u8] = b"pyowl-core:blank-component-manifest:v2\0";

#[derive(Debug)]
pub(crate) struct ScopedAnonymousRowsV2 {
    pub(crate) raw: [Vec<Vec<u8>>; 3],
    pub(crate) effective: [Vec<Vec<u8>>; 3],
    /// Effective digests in canonical raw collection/row order. RDF/XML has
    /// no lexical occurrence table, so its retained provenance follows these
    /// canonical roots.
    pub(crate) effective_occurrence_digests: Vec<[u8; 32]>,
    /// Raw/effective digest pairs in parser occurrence order. Functional
    /// source maps and provenance retain this order even when canonical root
    /// sets sort or deduplicate the corresponding values.
    pub(crate) source_occurrence_digests: Vec<([u8; 32], [u8; 32])>,
    /// Bounded schema-2 component/work/allocation-accounting counters retained for incident
    /// benchmarks and structured limit diagnostics. No source labels or
    /// ontology-sized payloads enter this summary.
    pub(crate) anonymous_metrics: AnonymousCanonicalMetricsV2,
}

/// Bounded component/work telemetry plus the exact monotonic `Session` byte
/// charge added while the anonymous scoping phase runs. The byte charge is not
/// allocator-live memory or a peak; those remain RSS/external-allocator evidence.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct AnonymousCanonicalMetricsV2 {
    pub(crate) component_count: u64,
    pub(crate) total_labels: u64,
    pub(crate) total_arcs: u64,
    pub(crate) largest_component_labels: u64,
    pub(crate) largest_component_arcs: u64,
    pub(crate) largest_component_roots: u64,
    pub(crate) maximum_root_interval_span: u64,
    pub(crate) maximum_open_root_intervals: u64,
    pub(crate) total_setup_work: u64,
    pub(crate) total_refinement_work: u64,
    pub(crate) total_candidate_order_work: u64,
    pub(crate) total_canonical_work: u64,
    pub(crate) largest_component_work: u64,
    pub(crate) maximum_refinement_rounds: u64,
    pub(crate) total_permutations_examined: u64,
    pub(crate) accounted_bytes: u64,
}

#[derive(Clone, Copy, Debug)]
struct Identity<'a> {
    scope: &'a [u8],
    key: &'a [u8],
}

#[derive(Clone, Debug)]
struct OwnedIdentity {
    scope: [u8; 32],
    key: [u8; 32],
}

#[derive(Debug)]
struct CanonicalNode<'a> {
    tag: u64,
    original: &'a [u8],
    fields: Vec<CanonicalField<'a>>,
    contains_anonymous: bool,
}

#[derive(Debug)]
enum CanonicalField<'a> {
    None,
    /// Exactly one child, stored in a fallibly reserved vector so allocation
    /// failure can propagate instead of crossing `Box::new`'s aborting path.
    Node(Vec<CanonicalNode<'a>>),
    Scalar(u8, &'a [u8]),
    Set(Vec<CanonicalNode<'a>>),
    Sequence(Vec<CanonicalNode<'a>>),
}

#[derive(Clone, Debug)]
struct BlankArc {
    source: usize,
    role: String,
    target: Option<usize>,
    payload: usize,
}

#[derive(Debug)]
struct BlankComponent {
    global_labels: Vec<usize>,
    arcs: Vec<BlankArc>,
    alpha: AlphaResult,
}

/// Freeze provisional RDF/XML blank labels into exact raw document identities
/// and exact effective snapshot identities without constructing Python model
/// objects or exporting canonical rows across the language boundary.
pub(crate) fn scope_rdfxml_anonymous_rows_v2(
    ontology_iri: Option<&str>,
    version_iri: Option<&str>,
    imports: &[String],
    rows: [&[Vec<u8>]; 3],
    session: &mut Session<'_>,
    cancellation: &Cancellation,
) -> NativeResult<ScopedAnonymousRowsV2> {
    let ontology_key = ontology_key_text(ontology_iri, version_iri, session)?;
    scope_anonymous_rows_v2(rows, ontology_key, session, cancellation, |rows| {
        rdfxml_document_fingerprint(ontology_iri, version_iri, imports, rows)
    })
}

/// Freeze Functional Syntax provisional blank labels while retaining digest
/// pairs in the parser's lexical occurrence order.
pub(crate) fn scope_functional_anonymous_rows_v2(
    ontology_iri: &Option<Node>,
    version_iri: &Option<Node>,
    imports: &[Node],
    rows: [&[Vec<u8>]; 3],
    session: &mut Session<'_>,
    cancellation: &Cancellation,
) -> NativeResult<ScopedAnonymousRowsV2> {
    let ontology_key = ontology_key_nodes(ontology_iri, version_iri, session)?;
    scope_anonymous_rows_v2(rows, ontology_key, session, cancellation, |rows| {
        functional_document_fingerprint(ontology_iri, version_iri, imports, rows)
    })
}

/// Re-scope canonical raw document rows into a caller-selected snapshot scope.
///
/// Parser owners initially retain the ordinal-zero effective rows. Closure
/// composition uses this path only for a later member of an equal-fingerprint
/// group, and deliberately starts from the raw document roots so the derived
/// local keys match Python's single re-scope operation exactly.
pub(crate) fn rescope_anonymous_rows_v2(
    rows: [&[Vec<u8>]; 3],
    snapshot_scope: [u8; 32],
    session: &mut Session<'_>,
    cancellation: &Cancellation,
) -> NativeResult<[Vec<Vec<u8>>; 3]> {
    let limits = *session.limits();
    let mut parsed = [Vec::new(), Vec::new(), Vec::new()];
    for (target, source) in parsed.iter_mut().zip(rows) {
        reserve_items::<CanonicalNode<'_>>(session, source.len())?;
        target
            .try_reserve_exact(source.len())
            .map_err(|_| NativeError::limit("native anonymous root allocation failed"))?;
        for row in source {
            cancellation.checkpoint()?;
            let mut scan = ScanBudget::from_limits(&limits);
            scan_canonical(row, &mut scan)?;
            target.push(parse_root(row, &limits, cancellation, session)?);
        }
    }
    let mut effective = encode_collections(
        &parsed,
        |identity| Ok(Some(rescope_identity(identity, snapshot_scope))),
        session,
    )?;
    canonicalize_collections(&mut effective);
    cancellation.checkpoint()?;
    Ok(effective)
}

fn scope_anonymous_rows_v2<F>(
    rows: [&[Vec<u8>]; 3],
    ontology_key: Vec<u8>,
    session: &mut Session<'_>,
    cancellation: &Cancellation,
    document_fingerprint: F,
) -> NativeResult<ScopedAnonymousRowsV2>
where
    F: FnOnce([&[Vec<u8>]; 3]) -> NativeResult<super::retained::FingerprintEvidenceV2>,
{
    let accounted_before = session.accounted_bytes();
    let limits = *session.limits();
    let mut parsed = [Vec::new(), Vec::new(), Vec::new()];
    for (target, source) in parsed.iter_mut().zip(rows) {
        reserve_items::<CanonicalNode<'_>>(session, source.len())?;
        target
            .try_reserve_exact(source.len())
            .map_err(|_| NativeError::limit("native anonymous root allocation failed"))?;
        for row in source {
            cancellation.checkpoint()?;
            let mut scan = ScanBudget::from_limits(&limits);
            scan_canonical(row, &mut scan)?;
            target.push(parse_root(row, &limits, cancellation, session)?);
        }
    }

    let mut labels = Vec::new();
    for node in parsed.iter().flatten() {
        collect_labels(node, &mut labels, session)?;
    }
    labels.sort_unstable();
    labels.dedup();
    if labels.is_empty() {
        return Err(NativeError::protocol(
            "native anonymous scoping received no provisional blank labels",
        ));
    }
    let (arcs, payloads) = blank_arcs(&parsed, &labels, &limits, cancellation, session)?;
    let total_terms = checked_add_u64(
        labels.len(),
        arcs.len(),
        "native blank document term count overflow",
    )?;
    if total_terms > limits.max_terms {
        return Err(NativeError::resource_limit(
            "max_terms",
            total_terms,
            limits.max_terms,
            "native anonymous canonicalization exceeds max_terms",
        ));
    }
    let components = canonical_components(
        labels.len(),
        &arcs,
        &payloads,
        &limits,
        cancellation,
        session,
    )?;
    let mut anonymous_metrics = anonymous_metrics(&components, labels.len(), arcs.len(), session)?;
    let manifest = component_manifest(&components, session)?;
    let document_scope = framed_digest(DOCUMENT_SCOPE_DOMAIN, &ontology_key, &manifest, session)?;
    let raw_identities =
        identities_for_components(labels.len(), &components, document_scope, session)?;

    let mut raw = encode_collections(
        &parsed,
        |identity| {
            let label = provisional_label(identity)?;
            Ok(label.and_then(|value| {
                labels
                    .binary_search(&value)
                    .ok()
                    .and_then(|index| raw_identities.get(index))
                    .and_then(Option::as_ref)
                    .cloned()
            }))
        },
        session,
    )?;
    let occurrence_count = raw.iter().try_fold(0_usize, |total, values| {
        total
            .checked_add(values.len())
            .ok_or_else(|| NativeError::limit("native anonymous occurrence count overflow"))
    })?;
    reserve_items::<[u8; 32]>(session, occurrence_count)?;
    let mut raw_source_digests = Vec::new();
    raw_source_digests
        .try_reserve_exact(occurrence_count)
        .map_err(|_| NativeError::limit("native anonymous digest allocation failed"))?;
    raw_source_digests.extend(raw.iter().flatten().map(|row| structural_digest(row)));
    let canonical_occurrence_order = canonical_occurrence_order(&raw, session)?;
    reserve_items::<([u8; 32], [u8; 32])>(session, occurrence_count)?;
    let mut source_occurrence_digests = Vec::new();
    source_occurrence_digests
        .try_reserve_exact(occurrence_count)
        .map_err(|_| NativeError::limit("native anonymous digest allocation failed"))?;

    canonicalize_collections(&mut raw);
    let raw_slices = [raw[0].as_slice(), raw[1].as_slice(), raw[2].as_slice()];
    let document = document_fingerprint(raw_slices)?;
    let snapshot_scope = snapshot_scope(document.digest);
    let mut effective = encode_collections(
        &parsed,
        |identity| {
            let raw = if let Some(label) = provisional_label(identity)? {
                labels
                    .binary_search(&label)
                    .ok()
                    .and_then(|index| raw_identities.get(index))
                    .and_then(Option::as_ref)
                    .map(|value| Identity {
                        scope: &value.scope,
                        key: &value.key,
                    })
            } else {
                Some(identity)
            };
            Ok(raw.map(|value| rescope_identity(value, snapshot_scope)))
        },
        session,
    )?;
    for (raw_digest, effective_row) in raw_source_digests
        .into_iter()
        .zip(effective.iter().flatten())
    {
        source_occurrence_digests.push((raw_digest, structural_digest(effective_row)));
    }
    if source_occurrence_digests.len() != occurrence_count {
        return Err(NativeError::protocol(
            "native anonymous occurrence digest count diverged",
        ));
    }
    reserve_items::<[u8; 32]>(session, canonical_occurrence_order.len())?;
    let mut effective_occurrence_digests = Vec::new();
    effective_occurrence_digests
        .try_reserve_exact(canonical_occurrence_order.len())
        .map_err(|_| NativeError::limit("native anonymous digest allocation failed"))?;
    for index in canonical_occurrence_order {
        effective_occurrence_digests.push(
            source_occurrence_digests
                .get(index)
                .ok_or_else(|| {
                    NativeError::protocol("native anonymous root order is out of bounds")
                })?
                .1,
        );
    }
    canonicalize_collections(&mut effective);
    for (raw_values, effective_values) in raw.iter().zip(&effective) {
        if raw_values.len() != effective_values.len() {
            return Err(NativeError::protocol(
                "native anonymous scoping changed structural root cardinality",
            ));
        }
    }
    anonymous_metrics.accounted_bytes = session
        .accounted_bytes()
        .checked_sub(accounted_before)
        .ok_or_else(|| NativeError::protocol("native anonymous memory accounting regressed"))?;
    Ok(ScopedAnonymousRowsV2 {
        raw,
        effective,
        effective_occurrence_digests,
        source_occurrence_digests,
        anonymous_metrics,
    })
}

fn canonical_components(
    label_count: usize,
    arcs: &[BlankArc],
    payloads: &[Vec<u8>],
    limits: &Limits,
    cancellation: &Cancellation,
    session: &mut Session<'_>,
) -> NativeResult<Vec<BlankComponent>> {
    let label_index_slots = label_count
        .checked_mul(5)
        .ok_or_else(|| NativeError::limit("native blank component index count overflow"))?;
    reserve_items::<usize>(session, label_index_slots)?;
    let mut parents = Vec::new();
    parents
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    parents.extend(0..label_count);
    let mut sizes = Vec::new();
    sizes
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    sizes.resize(label_count, 1_usize);
    for arc in arcs {
        cancellation.checkpoint()?;
        if let Some(target) = arc.target {
            union_component_labels(&mut parents, &mut sizes, arc.source, target)?;
        }
    }

    let mut root_components = Vec::new();
    root_components
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    root_components.resize(label_count, usize::MAX);
    let mut label_components = Vec::new();
    label_components
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    label_components.resize(label_count, usize::MAX);
    let mut local_indexes = Vec::new();
    local_indexes
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    local_indexes.resize(label_count, usize::MAX);
    reserve_items::<Vec<usize>>(session, label_count)?;
    let mut component_labels: Vec<Vec<usize>> = Vec::new();
    component_labels
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    for label in 0..label_count {
        let root = find_component_root(&mut parents, label);
        let component = if root_components[root] == usize::MAX {
            let index = component_labels.len();
            root_components[root] = index;
            component_labels.push(Vec::new());
            index
        } else {
            root_components[root]
        };
        let selected = component_labels
            .get_mut(component)
            .ok_or_else(|| NativeError::protocol("native blank component index is invalid"))?;
        reserve_items::<usize>(session, 1)?;
        selected
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
        local_indexes[label] = selected.len();
        label_components[label] = component;
        selected.push(label);
    }

    reserve_items::<Vec<BlankArc>>(session, component_labels.len())?;
    let mut component_arcs: Vec<Vec<BlankArc>> = Vec::new();
    component_arcs
        .try_reserve_exact(component_labels.len())
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    component_arcs.resize_with(component_labels.len(), Vec::new);
    for arc in arcs {
        cancellation.checkpoint()?;
        let component = *label_components
            .get(arc.source)
            .ok_or_else(|| NativeError::protocol("native blank arc source is invalid"))?;
        let target =
            match arc.target {
                None => None,
                Some(target) => {
                    if label_components.get(target).copied() != Some(component) {
                        return Err(NativeError::protocol(
                            "native blank arc crosses canonical components",
                        ));
                    }
                    Some(*local_indexes.get(target).ok_or_else(|| {
                        NativeError::protocol("native blank arc target is invalid")
                    })?)
                }
            };
        let role = owned_text(&arc.role, session)?;
        let localized = BlankArc {
            source: *local_indexes
                .get(arc.source)
                .ok_or_else(|| NativeError::protocol("native blank arc source is invalid"))?,
            role,
            target,
            payload: arc.payload,
        };
        let selected = component_arcs
            .get_mut(component)
            .ok_or_else(|| NativeError::protocol("native blank component is invalid"))?;
        reserve_items::<BlankArc>(session, 1)?;
        selected
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native blank component arc allocation failed"))?;
        selected.push(localized);
    }

    let limit_details = NativeLimitDetails {
        component_count: Some(usize_u64(
            component_labels.len(),
            "native blank component count exceeds u64",
        )?),
        largest_component_labels: Some(
            component_labels
                .iter()
                .map(Vec::len)
                .max()
                .map_or(Ok(0), |value| {
                    usize_u64(value, "native blank component label count exceeds u64")
                })?,
        ),
        largest_component_arcs: Some(
            component_arcs
                .iter()
                .map(Vec::len)
                .max()
                .map_or(Ok(0), |value| {
                    usize_u64(value, "native blank component arc count exceeds u64")
                })?,
        ),
        refinement_rounds: None,
        work_term: None,
    };
    reserve_items::<BlankComponent>(session, component_labels.len())?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(component_labels.len())
        .map_err(|_| NativeError::limit("native blank component allocation failed"))?;
    for (global_labels, local_arcs) in component_labels.into_iter().zip(component_arcs) {
        cancellation.checkpoint()?;
        let alpha = alpha_order(
            global_labels.len(),
            &local_arcs,
            payloads,
            limits,
            limit_details,
            cancellation,
            session,
        )?;
        result.push(BlankComponent {
            global_labels,
            arcs: local_arcs,
            alpha,
        });
    }
    Ok(result)
}

fn anonymous_metrics(
    components: &[BlankComponent],
    total_labels: usize,
    total_arcs: usize,
    session: &mut Session<'_>,
) -> NativeResult<AnonymousCanonicalMetricsV2> {
    let mut metrics = AnonymousCanonicalMetricsV2 {
        component_count: usize_u64(components.len(), "native blank component count exceeds u64")?,
        total_labels: usize_u64(total_labels, "native blank label count exceeds u64")?,
        total_arcs: usize_u64(total_arcs, "native blank arc count exceeds u64")?,
        ..AnonymousCanonicalMetricsV2::default()
    };
    let event_count = components
        .len()
        .checked_mul(2)
        .ok_or_else(|| NativeError::limit("native blank interval count overflow"))?;
    reserve_items::<(usize, i8)>(session, event_count)?;
    let mut events = Vec::new();
    events
        .try_reserve_exact(event_count)
        .map_err(|_| NativeError::limit("native blank interval allocation failed"))?;
    for component in components {
        let labels = usize_u64(
            component.global_labels.len(),
            "native blank component label count exceeds u64",
        )?;
        let arcs = usize_u64(
            component.arcs.len(),
            "native blank component arc count exceeds u64",
        )?;
        metrics.largest_component_labels = metrics.largest_component_labels.max(labels);
        metrics.largest_component_arcs = metrics.largest_component_arcs.max(arcs);
        metrics.total_setup_work = metrics
            .total_setup_work
            .checked_add(component.alpha.setup_work)
            .ok_or_else(|| NativeError::limit("native blank setup work overflow"))?;
        metrics.total_refinement_work = metrics
            .total_refinement_work
            .checked_add(component.alpha.refinement_work)
            .ok_or_else(|| NativeError::limit("native blank refinement work overflow"))?;
        metrics.total_candidate_order_work = metrics
            .total_candidate_order_work
            .checked_add(component.alpha.candidate_order_work)
            .ok_or_else(|| NativeError::limit("native blank candidate work overflow"))?;
        let component_work = component
            .alpha
            .setup_work
            .checked_add(component.alpha.refinement_work)
            .and_then(|value| value.checked_add(component.alpha.candidate_order_work))
            .ok_or_else(|| NativeError::limit("native blank component work overflow"))?;
        metrics.largest_component_work = metrics.largest_component_work.max(component_work);
        metrics.maximum_refinement_rounds = metrics
            .maximum_refinement_rounds
            .max(component.alpha.refinement_rounds);
        metrics.total_permutations_examined = metrics
            .total_permutations_examined
            .checked_add(component.alpha.permutations_examined)
            .ok_or_else(|| NativeError::limit("native blank permutation count overflow"))?;

        let mut first_root = None;
        let mut previous_root = None;
        let mut root_count = 0_u64;
        for arc in &component.arcs {
            if previous_root.is_some_and(|previous| arc.payload < previous) {
                return Err(NativeError::protocol(
                    "native blank component roots are not in document order",
                ));
            }
            if previous_root != Some(arc.payload) {
                root_count = root_count
                    .checked_add(1)
                    .ok_or_else(|| NativeError::limit("native blank root count overflow"))?;
                first_root.get_or_insert(arc.payload);
                previous_root = Some(arc.payload);
            }
        }
        let first_root = first_root.ok_or_else(|| {
            NativeError::protocol("native blank component contains no structural root")
        })?;
        let last_root = previous_root.expect("a blank component has one structural root");
        let span = last_root
            .checked_sub(first_root)
            .and_then(|value| value.checked_add(1))
            .ok_or_else(|| NativeError::limit("native blank root interval overflow"))?;
        metrics.largest_component_roots = metrics.largest_component_roots.max(root_count);
        metrics.maximum_root_interval_span = metrics
            .maximum_root_interval_span
            .max(usize_u64(span, "native blank root interval exceeds u64")?);
        events.push((first_root, 1));
        events.push((
            last_root
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native blank root interval overflow"))?,
            -1,
        ));
    }
    metrics.total_canonical_work = metrics
        .total_setup_work
        .checked_add(metrics.total_refinement_work)
        .and_then(|value| value.checked_add(metrics.total_candidate_order_work))
        .ok_or_else(|| NativeError::limit("native blank total canonical work overflow"))?;
    events.sort_unstable_by_key(|event| event.0);
    let mut active = 0_i64;
    let mut maximum_active = 0_i64;
    let mut offset = 0_usize;
    while offset < events.len() {
        let position = events[offset].0;
        let mut delta = 0_i64;
        while offset < events.len() && events[offset].0 == position {
            delta = delta
                .checked_add(i64::from(events[offset].1))
                .ok_or_else(|| NativeError::limit("native blank interval activity overflow"))?;
            offset += 1;
        }
        active = active
            .checked_add(delta)
            .ok_or_else(|| NativeError::limit("native blank interval activity overflow"))?;
        if active < 0 {
            return Err(NativeError::protocol(
                "native blank interval activity became negative",
            ));
        }
        maximum_active = maximum_active.max(active);
    }
    if active != 0 {
        return Err(NativeError::protocol(
            "native blank interval activity ledger is incomplete",
        ));
    }
    metrics.maximum_open_root_intervals = u64::try_from(maximum_active)
        .map_err(|_| NativeError::limit("native blank interval activity exceeds u64"))?;
    Ok(metrics)
}

fn find_component_root(parents: &mut [usize], mut value: usize) -> usize {
    let mut root = value;
    while parents[root] != root {
        root = parents[root];
    }
    while parents[value] != value {
        let next = parents[value];
        parents[value] = root;
        value = next;
    }
    root
}

fn union_component_labels(
    parents: &mut [usize],
    sizes: &mut [usize],
    left: usize,
    right: usize,
) -> NativeResult<()> {
    let mut left_root = find_component_root(parents, left);
    let mut right_root = find_component_root(parents, right);
    if left_root == right_root {
        return Ok(());
    }
    if sizes[left_root] < sizes[right_root] {
        std::mem::swap(&mut left_root, &mut right_root);
    }
    parents[right_root] = left_root;
    sizes[left_root] = sizes[left_root]
        .checked_add(sizes[right_root])
        .ok_or_else(|| NativeError::limit("native blank component size overflow"))?;
    Ok(())
}

fn sorted_component_indexes(
    components: &[BlankComponent],
    session: &mut Session<'_>,
) -> NativeResult<Vec<usize>> {
    reserve_items::<usize>(session, components.len())?;
    let mut indexes = Vec::new();
    indexes
        .try_reserve_exact(components.len())
        .map_err(|_| NativeError::limit("native blank component order allocation failed"))?;
    indexes.extend(0..components.len());
    indexes.sort_unstable_by(|left, right| {
        components[*left]
            .alpha
            .graph
            .cmp(&components[*right].alpha.graph)
            .then_with(|| {
                components[*left]
                    .global_labels
                    .cmp(&components[*right].global_labels)
            })
    });
    Ok(indexes)
}

fn component_manifest(
    components: &[BlankComponent],
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let indexes = sorted_component_indexes(components, session)?;
    let distinct = indexes
        .iter()
        .enumerate()
        .filter(|(offset, index)| {
            *offset == 0
                || components[**index].alpha.graph != components[indexes[*offset - 1]].alpha.graph
        })
        .count();
    let mut manifest = Vec::new();
    append_bytes(&mut manifest, COMPONENT_MANIFEST_DOMAIN, session)?;
    append_varint(
        &mut manifest,
        usize_u64(distinct, "native blank component count exceeds u64")?,
        session,
    )?;
    let mut start = 0_usize;
    while start < indexes.len() {
        let graph = &components[indexes[start]].alpha.graph;
        let mut end = start + 1;
        while end < indexes.len() && components[indexes[end]].alpha.graph == *graph {
            end += 1;
        }
        append_frame(&mut manifest, graph, session)?;
        append_varint(
            &mut manifest,
            usize_u64(
                end - start,
                "native blank component multiplicity exceeds u64",
            )?,
            session,
        )?;
        start = end;
    }
    Ok(manifest)
}

#[derive(Debug)]
struct AlphaResult {
    order: Vec<usize>,
    graph: Vec<u8>,
    setup_work: u64,
    refinement_work: u64,
    candidate_order_work: u64,
    refinement_rounds: u64,
    permutations_examined: u64,
}

fn alpha_order(
    label_count: usize,
    arcs: &[BlankArc],
    payloads: &[Vec<u8>],
    limits: &Limits,
    limit_details: NativeLimitDetails,
    cancellation: &Cancellation,
    session: &mut Session<'_>,
) -> NativeResult<AlphaResult> {
    let terms = checked_add_u64(label_count, arcs.len(), "native blank term count overflow")?;
    if terms > limits.max_terms {
        return Err(NativeError::resource_limit(
            "max_terms",
            terms,
            limits.max_terms,
            "native anonymous canonicalization exceeds max_terms",
        ));
    }
    let label_count_u64 = usize_u64(label_count, "native blank label count exceeds u64")?;
    let arc_count = usize_u64(arcs.len(), "native blank arc count exceeds u64")?;
    let setup_work = label_count_u64
        .checked_add(
            arc_count
                .checked_mul(2)
                .ok_or_else(|| NativeError::limit("native blank work overflow"))?,
        )
        .ok_or_else(|| NativeError::limit("native blank work overflow"))?;
    let mut work = setup_work;
    enforce_work(
        work,
        limits,
        NativeLimitDetails {
            refinement_rounds: Some(0),
            work_term: Some("setup"),
            ..limit_details
        },
    )?;
    let mut colors = colors_from_signatures(
        neighborhoods(label_count, arcs, payloads, None, cancellation, session)?,
        session,
    )?;
    let mut rounds = 0_usize;
    loop {
        cancellation.checkpoint()?;
        rounds = rounds
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native blank refinement overflow"))?;
        let neighborhoods = neighborhoods(
            label_count,
            arcs,
            payloads,
            Some(&colors),
            cancellation,
            session,
        )?;
        let mut signatures = Vec::new();
        reserve_items::<Vec<Vec<u8>>>(session, label_count)?;
        signatures
            .try_reserve_exact(label_count)
            .map_err(|_| NativeError::limit("native blank signature allocation failed"))?;
        for (color, neighborhood) in colors.iter().zip(neighborhoods) {
            let mut signature = Vec::new();
            reserve_items::<Vec<u8>>(session, neighborhood.len().saturating_add(1))?;
            signature
                .try_reserve_exact(neighborhood.len().saturating_add(1))
                .map_err(|_| NativeError::limit("native blank signature allocation failed"))?;
            signature.push(copy_bytes(color, session)?);
            signature.extend(neighborhood);
            signatures.push(signature);
        }
        let refined = colors_from_signatures(signatures, session)?;
        work = work
            .checked_add(
                label_count_u64
                    .checked_mul(2)
                    .and_then(|value| value.checked_add(arc_count.checked_mul(2)?))
                    .ok_or_else(|| NativeError::limit("native blank work overflow"))?,
            )
            .ok_or_else(|| NativeError::limit("native blank work overflow"))?;
        enforce_work(
            work,
            limits,
            NativeLimitDetails {
                refinement_rounds: Some(usize_u64(
                    rounds,
                    "native blank refinement rounds exceed u64",
                )?),
                work_term: Some("refinement"),
                ..limit_details
            },
        )?;
        if same_partition(&colors, &refined, session)? {
            colors = refined;
            break;
        }
        colors = refined;
        if rounds > label_count.saturating_add(1) {
            return Err(NativeError::protocol(
                "native blank-node partition refinement did not converge",
            ));
        }
    }

    let partitions = partitions(&colors, session)?;
    let candidate_details = NativeLimitDetails {
        refinement_rounds: Some(usize_u64(
            rounds,
            "native blank refinement rounds exceed u64",
        )?),
        work_term: Some("candidate_orders"),
        ..limit_details
    };
    let candidates = permutation_count(
        &partitions,
        limits.max_canonical_work,
        work,
        candidate_details,
    )?;
    let unit = label_count_u64
        .checked_add(arc_count)
        .ok_or_else(|| NativeError::limit("native blank work overflow"))?
        .max(1);
    let candidate_order_work = candidates
        .checked_mul(unit)
        .ok_or_else(|| NativeError::limit("native blank work overflow"))?;
    let final_work = work
        .checked_add(candidate_order_work)
        .ok_or_else(|| NativeError::limit("native blank work overflow"))?;
    enforce_work(final_work, limits, candidate_details)?;

    let mut choices = clone_partitions(&partitions, session)?;
    let mut best_graph: Option<Vec<u8>> = None;
    let mut best_order: Option<Vec<usize>> = None;
    loop {
        cancellation.checkpoint()?;
        reserve_items::<usize>(session, label_count)?;
        let mut order = Vec::new();
        order
            .try_reserve_exact(label_count)
            .map_err(|_| NativeError::limit("native blank order allocation failed"))?;
        order.extend(choices.iter().flatten().copied());
        let graph = serialize_graph(&order, arcs, payloads, session)?;
        if best_graph.as_ref().is_none_or(|best| graph < *best) {
            best_graph = Some(graph);
            best_order = Some(order);
        }
        if !advance_partition_product(&mut choices) {
            break;
        }
    }
    Ok(AlphaResult {
        order: best_order.ok_or_else(|| {
            NativeError::protocol("native blank canonicalization produced no candidate")
        })?,
        graph: best_graph.ok_or_else(|| {
            NativeError::protocol("native blank canonicalization produced no graph")
        })?,
        setup_work,
        refinement_work: work
            .checked_sub(setup_work)
            .ok_or_else(|| NativeError::protocol("native blank work accounting regressed"))?,
        candidate_order_work,
        refinement_rounds: usize_u64(rounds, "native blank refinement rounds exceed u64")?,
        permutations_examined: candidates,
    })
}

fn neighborhoods(
    label_count: usize,
    arcs: &[BlankArc],
    payloads: &[Vec<u8>],
    colors: Option<&[[u8; 32]]>,
    cancellation: &Cancellation,
    session: &mut Session<'_>,
) -> NativeResult<Vec<Vec<Vec<u8>>>> {
    reserve_items::<Vec<Vec<u8>>>(session, label_count)?;
    let mut gathered = Vec::new();
    gathered
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank neighborhood allocation failed"))?;
    gathered.resize_with(label_count, Vec::new);
    for arc in arcs {
        cancellation.checkpoint()?;
        reserve_items::<Vec<u8>>(session, 1)?;
        gathered[arc.source]
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native blank neighborhood allocation failed"))?;
        gathered[arc.source].push(arc_signature(arc.source, arc, payloads, colors, session)?);
        if let Some(target) = arc.target.filter(|target| *target != arc.source) {
            reserve_items::<Vec<u8>>(session, 1)?;
            gathered[target]
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native blank neighborhood allocation failed"))?;
            gathered[target].push(arc_signature(target, arc, payloads, colors, session)?);
        }
    }
    for values in &mut gathered {
        values.sort_unstable();
    }
    Ok(gathered)
}

fn arc_signature(
    label: usize,
    arc: &BlankArc,
    payloads: &[Vec<u8>],
    colors: Option<&[[u8; 32]]>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let (direction, neighbor): (u8, Vec<u8>) = if arc.source == label {
        (
            b'S',
            match arc.target {
                None => one_byte(b'N', session)?,
                Some(target) if target == label => one_byte(b'L', session)?,
                Some(target) => neighbor_color(target, colors, session)?,
            },
        )
    } else if arc.target == Some(label) {
        (b'T', neighbor_color(arc.source, colors, session)?)
    } else {
        return Err(NativeError::protocol(
            "native blank arc does not contain its requested label",
        ));
    };
    let payload = payloads
        .get(arc.payload)
        .ok_or_else(|| NativeError::protocol("native blank arc payload is missing"))?;
    let mut result = Vec::new();
    reserve_bytes(session, 1)?;
    result.push(direction);
    append_frame(&mut result, arc.role.as_bytes(), session)?;
    append_bytes(&mut result, &neighbor, session)?;
    append_frame(&mut result, payload, session)?;
    Ok(result)
}

fn neighbor_color(
    target: usize,
    colors: Option<&[[u8; 32]]>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let Some(colors) = colors else {
        return one_byte(b'B', session);
    };
    let color = colors
        .get(target)
        .ok_or_else(|| NativeError::protocol("native blank neighbor color is missing"))?;
    reserve_bytes(session, 33)?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(33)
        .map_err(|_| NativeError::limit("native blank color allocation failed"))?;
    result.push(b'C');
    result.extend_from_slice(color);
    Ok(result)
}

fn colors_from_signatures(
    signatures: Vec<Vec<Vec<u8>>>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<[u8; 32]>> {
    reserve_items::<[u8; 32]>(session, signatures.len())?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(signatures.len())
        .map_err(|_| NativeError::limit("native blank color allocation failed"))?;
    for signature in signatures {
        let mut hasher = Sha256::new();
        hasher.update(BLANK_COLOR_DOMAIN);
        for item in signature {
            let mut framed = Vec::new();
            append_frame(&mut framed, &item, session)?;
            hasher.update(&framed);
        }
        result.push(hasher.finish());
    }
    Ok(result)
}

fn same_partition(
    first: &[[u8; 32]],
    second: &[[u8; 32]],
    session: &mut Session<'_>,
) -> NativeResult<bool> {
    if first.len() != second.len() {
        return Ok(false);
    }
    reserve_items::<([u8; 32], [u8; 32])>(session, first.len())?;
    let mut pairs = Vec::new();
    pairs
        .try_reserve_exact(first.len())
        .map_err(|_| NativeError::limit("native blank partition allocation failed"))?;
    pairs.extend(first.iter().copied().zip(second.iter().copied()));
    pairs.sort_unstable();
    if pairs
        .windows(2)
        .any(|values| values[0].0 == values[1].0 && values[0].1 != values[1].1)
    {
        return Ok(false);
    }
    pairs.sort_unstable_by_key(|value| (value.1, value.0));
    Ok(!pairs
        .windows(2)
        .any(|values| values[0].1 == values[1].1 && values[0].0 != values[1].0))
}

fn partitions(colors: &[[u8; 32]], session: &mut Session<'_>) -> NativeResult<Vec<Vec<usize>>> {
    reserve_items::<usize>(session, colors.len())?;
    let mut indexes = Vec::new();
    indexes
        .try_reserve_exact(colors.len())
        .map_err(|_| NativeError::limit("native blank partition allocation failed"))?;
    indexes.extend(0..colors.len());
    indexes.sort_unstable_by_key(|index| (colors[*index], *index));
    reserve_items::<Vec<usize>>(session, colors.len())?;
    let mut result: Vec<Vec<usize>> = Vec::new();
    result
        .try_reserve_exact(colors.len())
        .map_err(|_| NativeError::limit("native blank partition allocation failed"))?;
    for index in indexes {
        if result
            .last()
            .and_then(|values| values.first())
            .is_none_or(|first| colors[*first] != colors[index])
        {
            result.push(Vec::new());
        }
        reserve_items::<usize>(session, 1)?;
        let selected = result.last_mut().expect("partition was inserted");
        selected
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native blank partition allocation failed"))?;
        selected.push(index);
    }
    Ok(result)
}

fn clone_partitions(
    partitions: &[Vec<usize>],
    session: &mut Session<'_>,
) -> NativeResult<Vec<Vec<usize>>> {
    reserve_items::<Vec<usize>>(session, partitions.len())?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(partitions.len())
        .map_err(|_| NativeError::limit("native blank partition clone allocation failed"))?;
    for partition in partitions {
        reserve_items::<usize>(session, partition.len())?;
        let mut selected = Vec::new();
        selected
            .try_reserve_exact(partition.len())
            .map_err(|_| NativeError::limit("native blank partition clone allocation failed"))?;
        selected.extend_from_slice(partition);
        result.push(selected);
    }
    Ok(result)
}

fn permutation_count(
    partitions: &[Vec<usize>],
    maximum: u64,
    consumed: u64,
    details: NativeLimitDetails,
) -> NativeResult<u64> {
    let remaining = maximum.saturating_sub(consumed);
    let mut count = 1_u64;
    for partition in partitions {
        for factor in 2..=partition.len() {
            let factor = usize_u64(factor, "native blank permutation factor exceeds u64")?;
            if count > remaining / factor {
                return Err(NativeError::resource_limit_with_details(
                    "max_canonical_work",
                    maximum.saturating_add(1),
                    maximum,
                    details,
                    "native anonymous canonicalization exceeds max_canonical_work",
                ));
            }
            count *= factor;
        }
    }
    Ok(count)
}

fn advance_partition_product(partitions: &mut [Vec<usize>]) -> bool {
    for partition in partitions.iter_mut().rev() {
        if next_permutation(partition) {
            return true;
        }
        partition.sort_unstable();
    }
    false
}

fn next_permutation(values: &mut [usize]) -> bool {
    let Some(pivot) = (1..values.len())
        .rev()
        .find(|index| values[index - 1] < values[*index])
    else {
        return false;
    };
    let pivot = pivot - 1;
    let swap = (pivot + 1..values.len())
        .rev()
        .find(|index| values[pivot] < values[*index])
        .expect("a lexicographic permutation pivot has a successor");
    values.swap(pivot, swap);
    values[pivot + 1..].reverse();
    true
}

fn serialize_graph(
    order: &[usize],
    arcs: &[BlankArc],
    payloads: &[Vec<u8>],
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    reserve_items::<usize>(session, order.len())?;
    let mut indexes = Vec::new();
    indexes
        .try_reserve_exact(order.len())
        .map_err(|_| NativeError::limit("native blank index allocation failed"))?;
    indexes.resize(order.len(), 0);
    for (index, label) in order.iter().copied().enumerate() {
        *indexes
            .get_mut(label)
            .ok_or_else(|| NativeError::protocol("native blank order is invalid"))? = index;
    }
    reserve_items::<Vec<u8>>(session, arcs.len())?;
    let mut members = Vec::new();
    members
        .try_reserve_exact(arcs.len())
        .map_err(|_| NativeError::limit("native blank graph allocation failed"))?;
    for arc in arcs {
        let payload = payloads
            .get(arc.payload)
            .ok_or_else(|| NativeError::protocol("native blank graph payload is missing"))?;
        let mut member = Vec::new();
        append_varint(
            &mut member,
            usize_u64(indexes[arc.source], "blank index exceeds u64")?,
            session,
        )?;
        append_frame(&mut member, arc.role.as_bytes(), session)?;
        match arc.target {
            None => {
                reserve_bytes(session, 1)?;
                member.push(0);
            }
            Some(target) => {
                reserve_bytes(session, 1)?;
                member.push(1);
                append_varint(
                    &mut member,
                    usize_u64(indexes[target], "blank target index exceeds u64")?,
                    session,
                )?;
            }
        }
        append_frame(&mut member, payload, session)?;
        members.push(member);
    }
    members.sort_unstable();
    members.dedup();
    let mut graph = Vec::new();
    append_bytes(&mut graph, BLANK_GRAPH_DOMAIN, session)?;
    append_varint(
        &mut graph,
        usize_u64(order.len(), "blank order exceeds u64")?,
        session,
    )?;
    append_varint(
        &mut graph,
        usize_u64(members.len(), "blank graph exceeds u64")?,
        session,
    )?;
    for member in members {
        append_frame(&mut graph, &member, session)?;
    }
    Ok(graph)
}

fn blank_arcs(
    roots: &[Vec<CanonicalNode<'_>>; 3],
    labels: &[&str],
    limits: &Limits,
    cancellation: &Cancellation,
    session: &mut Session<'_>,
) -> NativeResult<(Vec<BlankArc>, Vec<Vec<u8>>)> {
    let mut arcs = Vec::new();
    let mut payloads = Vec::new();
    for root in roots.iter().flatten() {
        cancellation.checkpoint()?;
        let skeleton = skeleton_node(root, session)?;
        let skeleton_work = usize_u64(skeleton.len(), "blank skeleton exceeds u64")?;
        if skeleton_work > limits.max_canonical_work {
            return Err(NativeError::resource_limit_with_details(
                "max_canonical_work",
                skeleton_work,
                limits.max_canonical_work,
                NativeLimitDetails {
                    work_term: Some("setup"),
                    ..NativeLimitDetails::default()
                },
                "native blank skeleton exceeds max_canonical_work",
            ));
        }
        let payload = payloads.len();
        reserve_items::<Vec<u8>>(session, 1)?;
        payloads
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native blank payload allocation failed"))?;
        payloads.push(skeleton);
        let (name, _) = constructor_ledger(root.tag)?;
        let mut occurrences = Vec::new();
        blank_occurrences(
            root,
            owned_text(name, session)?,
            labels,
            &mut occurrences,
            session,
        )?;
        let following = arcs
            .len()
            .checked_add(occurrences.len())
            .and_then(|value| {
                occurrences
                    .len()
                    .checked_mul(occurrences.len().saturating_sub(1))
                    .and_then(|pairs| value.checked_add(pairs / 2))
            })
            .ok_or_else(|| NativeError::limit("native blank arc count overflow"))?;
        let following_terms = checked_add_u64(
            labels.len(),
            following,
            "native blank document term count overflow",
        )?;
        if following_terms > limits.max_terms {
            return Err(NativeError::resource_limit(
                "max_terms",
                following_terms,
                limits.max_terms,
                "native anonymous canonicalization exceeds max_terms",
            ));
        }
        reserve_items::<BlankArc>(session, following.saturating_sub(arcs.len()))?;
        arcs.try_reserve(following.saturating_sub(arcs.len()))
            .map_err(|_| NativeError::limit("native blank arc allocation failed"))?;
        for (label, path) in &occurrences {
            arcs.push(BlankArc {
                source: *label,
                role: owned_text(path, session)?,
                target: None,
                payload,
            });
        }
        for (index, (source, source_path)) in occurrences.iter().enumerate() {
            for (target, target_path) in &occurrences[index + 1..] {
                let mut role = String::new();
                reserve_bytes(
                    session,
                    source_path
                        .len()
                        .saturating_add(target_path.len())
                        .saturating_add(2),
                )?;
                role.try_reserve(
                    source_path
                        .len()
                        .saturating_add(target_path.len())
                        .saturating_add(2),
                )
                .map_err(|_| NativeError::limit("native blank role allocation failed"))?;
                role.push_str(source_path);
                role.push_str("->");
                role.push_str(target_path);
                arcs.push(BlankArc {
                    source: *source,
                    role,
                    target: Some(*target),
                    payload,
                });
            }
        }
    }
    Ok((arcs, payloads))
}

fn blank_occurrences(
    node: &CanonicalNode<'_>,
    path: String,
    labels: &[&str],
    output: &mut Vec<(usize, String)>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    if node.tag == 3 {
        if let Some(label) = provisional_label(anonymous_identity(node)?)? {
            let index = labels
                .binary_search(&label)
                .map_err(|_| NativeError::protocol("native blank occurrence label is missing"))?;
            reserve_items::<(usize, String)>(session, 1)?;
            output
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native blank occurrence allocation failed"))?;
            output.push((index, path));
        }
        return Ok(());
    }
    let (_, names) = constructor_ledger(node.tag)?;
    if names.len() != node.fields.len() {
        return Err(NativeError::protocol(
            "native anonymous constructor field ledger diverges",
        ));
    }
    for (field, name) in node.fields.iter().zip(names.iter()) {
        let field_path = joined_path(&path, name, session)?;
        match field {
            CanonicalField::Node(children) => {
                blank_occurrences(only_child(children)?, field_path, labels, output, session)?
            }
            CanonicalField::Set(values) => {
                reserve_items::<(Vec<u8>, &CanonicalNode<'_>)>(session, values.len())?;
                let mut grouped = Vec::new();
                grouped.try_reserve_exact(values.len()).map_err(|_| {
                    NativeError::limit("native blank set ordering allocation failed")
                })?;
                for value in values {
                    grouped.push((skeleton_node(value, session)?, value));
                }
                // Canonical sets arrive in canonical-byte order. Use that as
                // the explicit tie-breaker so this allocation-free unstable
                // sort has the same result as Python's stable skeleton sort.
                grouped.sort_unstable_by(|left, right| {
                    left.0
                        .cmp(&right.0)
                        .then_with(|| left.1.original.cmp(right.1.original))
                });
                for (skeleton, value) in grouped {
                    let marker = full_hex(sha256(&skeleton), session)?;
                    blank_occurrences(
                        value,
                        joined_path(&field_path, &set_marker(&marker, session)?, session)?,
                        labels,
                        output,
                        session,
                    )?;
                }
            }
            CanonicalField::Sequence(values) => {
                for (index, value) in values.iter().enumerate() {
                    blank_occurrences(
                        value,
                        joined_path(&field_path, &decimal_index(index, session)?, session)?,
                        labels,
                        output,
                        session,
                    )?;
                }
            }
            CanonicalField::None | CanonicalField::Scalar(_, _) => {}
        }
    }
    Ok(())
}

fn skeleton_node(node: &CanonicalNode<'_>, session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
    if node.tag == 3 {
        return one_byte(b'B', session);
    }
    if !node.contains_anonymous {
        let mut result = Vec::new();
        reserve_bytes(session, 1)?;
        result.push(b'C');
        append_frame(&mut result, node.original, session)?;
        return Ok(result);
    }
    let mut result = Vec::new();
    reserve_bytes(session, 1)?;
    result.push(b'N');
    append_varint(&mut result, node.tag, session)?;
    for field in &node.fields {
        let member = skeleton_field(field, session)?;
        append_frame(&mut result, &member, session)?;
    }
    Ok(result)
}

fn skeleton_field(field: &CanonicalField<'_>, session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
    match field {
        CanonicalField::None => one_byte(b'0', session),
        CanonicalField::Node(values) => skeleton_node(only_child(values)?, session),
        CanonicalField::Scalar(4, value) => {
            let mut result = Vec::new();
            reserve_bytes(session, 1)?;
            result.push(b'I');
            append_bytes(&mut result, value, session)?;
            Ok(result)
        }
        CanonicalField::Scalar(2 | 5, value) => {
            let mut result = Vec::new();
            reserve_bytes(session, 1)?;
            result.push(b'T');
            append_frame(&mut result, value, session)?;
            Ok(result)
        }
        CanonicalField::Scalar(_, _) => Err(NativeError::protocol(
            "native blank skeleton contains an unsupported scalar field",
        )),
        CanonicalField::Set(values) => {
            reserve_items::<Vec<u8>>(session, values.len())?;
            let mut members = Vec::new();
            members
                .try_reserve_exact(values.len())
                .map_err(|_| NativeError::limit("native blank skeleton allocation failed"))?;
            for value in values {
                members.push(skeleton_node(value, session)?);
            }
            members.sort_unstable();
            let mut result = Vec::new();
            reserve_bytes(session, 1)?;
            result.push(b'S');
            append_varint(
                &mut result,
                usize_u64(members.len(), "set size exceeds u64")?,
                session,
            )?;
            for member in members {
                append_frame(&mut result, &member, session)?;
            }
            Ok(result)
        }
        CanonicalField::Sequence(values) => {
            let mut result = Vec::new();
            reserve_bytes(session, 1)?;
            result.push(b'Q');
            append_varint(
                &mut result,
                usize_u64(values.len(), "sequence size exceeds u64")?,
                session,
            )?;
            for value in values {
                append_frame(&mut result, &skeleton_node(value, session)?, session)?;
            }
            Ok(result)
        }
    }
}

fn collect_labels<'a>(
    node: &CanonicalNode<'a>,
    labels: &mut Vec<&'a str>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    if node.tag == 3 {
        if let Some(label) = provisional_label(anonymous_identity(node)?)? {
            reserve_items::<&str>(session, 1)?;
            labels
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native blank label allocation failed"))?;
            labels.push(label);
        }
        return Ok(());
    }
    for field in &node.fields {
        match field {
            CanonicalField::Node(values) => collect_labels(only_child(values)?, labels, session)?,
            CanonicalField::Set(values) | CanonicalField::Sequence(values) => {
                for value in values {
                    collect_labels(value, labels, session)?;
                }
            }
            CanonicalField::None | CanonicalField::Scalar(_, _) => {}
        }
    }
    Ok(())
}

fn encode_collections<F>(
    parsed: &[Vec<CanonicalNode<'_>>; 3],
    replacement: F,
    session: &mut Session<'_>,
) -> NativeResult<[Vec<Vec<u8>>; 3]>
where
    F: Fn(Identity<'_>) -> NativeResult<Option<OwnedIdentity>>,
{
    let mut result = [Vec::new(), Vec::new(), Vec::new()];
    for (target, source) in result.iter_mut().zip(parsed) {
        reserve_items::<Vec<u8>>(session, source.len())?;
        target
            .try_reserve_exact(source.len())
            .map_err(|_| NativeError::limit("native scoped row allocation failed"))?;
        for node in source {
            target.push(encode_replaced(node, &replacement, session)?);
        }
    }
    Ok(result)
}

fn encode_replaced<F>(
    node: &CanonicalNode<'_>,
    replacement: &F,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>>
where
    F: Fn(Identity<'_>) -> NativeResult<Option<OwnedIdentity>>,
{
    if node.tag == 3 {
        let identity = anonymous_identity(node)?;
        return match replacement(identity)? {
            Some(value) => encode_anonymous(&value.scope, &value.key, session),
            None => copy_bytes(node.original, session),
        };
    }
    if !node.contains_anonymous {
        return copy_bytes(node.original, session);
    }
    let mut output = Vec::new();
    append_varint(&mut output, node.tag, session)?;
    for field in &node.fields {
        match field {
            CanonicalField::None => {
                reserve_bytes(session, 1)?;
                output.push(0);
            }
            CanonicalField::Node(values) => {
                reserve_bytes(session, 1)?;
                output.push(1);
                let encoded = encode_replaced(only_child(values)?, replacement, session)?;
                append_frame(&mut output, &encoded, session)?;
            }
            CanonicalField::Scalar(marker, value) => {
                reserve_bytes(session, 1)?;
                output.push(*marker);
                if *marker == 4 {
                    append_bytes(&mut output, value, session)?;
                } else {
                    append_frame(&mut output, value, session)?;
                }
            }
            CanonicalField::Set(values) => {
                reserve_bytes(session, 1)?;
                output.push(6);
                reserve_items::<Vec<u8>>(session, values.len())?;
                let mut members = Vec::new();
                members
                    .try_reserve_exact(values.len())
                    .map_err(|_| NativeError::limit("native scoped set allocation failed"))?;
                for value in values {
                    members.push(encode_replaced(value, replacement, session)?);
                }
                members.sort_unstable();
                members.dedup();
                append_varint(
                    &mut output,
                    usize_u64(members.len(), "set size exceeds u64")?,
                    session,
                )?;
                for member in members {
                    append_frame(&mut output, &member, session)?;
                }
            }
            CanonicalField::Sequence(values) => {
                reserve_bytes(session, 1)?;
                output.push(7);
                append_varint(
                    &mut output,
                    usize_u64(values.len(), "sequence size exceeds u64")?,
                    session,
                )?;
                for value in values {
                    reserve_bytes(session, 1)?;
                    output.push(1);
                    append_frame(
                        &mut output,
                        &encode_replaced(value, replacement, session)?,
                        session,
                    )?;
                }
            }
        }
    }
    Ok(output)
}

fn parse_root<'a>(
    row: &'a [u8],
    limits: &Limits,
    cancellation: &Cancellation,
    session: &mut Session<'_>,
) -> NativeResult<CanonicalNode<'a>> {
    let mut terms = 0_u64;
    let (node, consumed) = parse_node(
        row,
        0,
        row.len(),
        0,
        &mut terms,
        limits,
        cancellation,
        session,
    )?;
    if consumed != row.len() {
        return Err(NativeError::protocol(
            "native anonymous canonical row has trailing bytes",
        ));
    }
    Ok(node)
}

#[allow(clippy::too_many_arguments)]
fn parse_node<'a>(
    data: &'a [u8],
    start: usize,
    bound: usize,
    depth: u32,
    terms: &mut u64,
    limits: &Limits,
    cancellation: &Cancellation,
    session: &mut Session<'_>,
) -> NativeResult<(CanonicalNode<'a>, usize)> {
    cancellation.checkpoint()?;
    *terms = terms
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("native anonymous term count overflow"))?;
    if *terms > limits.max_terms {
        return Err(limits.resource_limit(
            LimitKey::MaxTerms,
            *terms,
            "native anonymous canonical scan exceeds max_terms",
        ));
    }
    let maximum_depth = limits.max_nesting_depth.min(1024);
    if depth > maximum_depth {
        return Err(NativeError::resource_limit(
            "max_nesting_depth",
            u64::from(depth),
            u64::from(maximum_depth),
            "native anonymous canonical scan exceeds max_nesting_depth",
        ));
    }
    let (tag, mut offset) = read_varint(data, start, bound)?;
    let field_count = canonical_field_count(
        u16::try_from(tag).map_err(|_| NativeError::protocol("canonical tag exceeds u16"))?,
    )
    .ok_or_else(|| NativeError::protocol("native anonymous canonical tag is unknown"))?;
    let mut fields = Vec::new();
    reserve_items::<CanonicalField<'_>>(session, usize::from(field_count))?;
    fields
        .try_reserve_exact(usize::from(field_count))
        .map_err(|_| NativeError::limit("native anonymous field allocation failed"))?;
    let mut contains_anonymous = tag == 3;
    for _ in 0..field_count {
        let marker = *data
            .get(offset)
            .filter(|_| offset < bound)
            .ok_or_else(|| NativeError::protocol("native anonymous field is truncated"))?;
        offset += 1;
        let field = match marker {
            0 => CanonicalField::None,
            1 => {
                let (frame_start, frame_end) = read_frame(data, offset, bound)?;
                let (child, consumed) = parse_node(
                    data,
                    frame_start,
                    frame_end,
                    depth.saturating_add(1),
                    terms,
                    limits,
                    cancellation,
                    session,
                )?;
                if consumed != frame_end {
                    return Err(NativeError::protocol(
                        "native anonymous child frame is invalid",
                    ));
                }
                contains_anonymous |= child.contains_anonymous;
                offset = frame_end;
                reserve_items::<CanonicalNode<'_>>(session, 1)?;
                let mut children = Vec::new();
                children
                    .try_reserve_exact(1)
                    .map_err(|_| NativeError::limit("native anonymous child allocation failed"))?;
                children.push(child);
                CanonicalField::Node(children)
            }
            2 | 3 | 5 => {
                let (frame_start, frame_end) = read_frame(data, offset, bound)?;
                offset = frame_end;
                CanonicalField::Scalar(marker, &data[frame_start..frame_end])
            }
            4 => {
                let value_start = offset;
                let after = read_any_varint(data, offset, bound)?;
                offset = after;
                CanonicalField::Scalar(marker, &data[value_start..after])
            }
            6 => {
                let (count, after) = read_varint(data, offset, bound)?;
                offset = after;
                let count = usize::try_from(count)
                    .map_err(|_| NativeError::limit("native anonymous set exceeds usize"))?;
                let mut values = Vec::new();
                reserve_items::<CanonicalNode<'_>>(session, count)?;
                values
                    .try_reserve_exact(count)
                    .map_err(|_| NativeError::limit("native anonymous set allocation failed"))?;
                for _ in 0..count {
                    let (frame_start, frame_end) = read_frame(data, offset, bound)?;
                    let (child, consumed) = parse_node(
                        data,
                        frame_start,
                        frame_end,
                        depth.saturating_add(1),
                        terms,
                        limits,
                        cancellation,
                        session,
                    )?;
                    if consumed != frame_end {
                        return Err(NativeError::protocol(
                            "native anonymous set frame is invalid",
                        ));
                    }
                    contains_anonymous |= child.contains_anonymous;
                    values.push(child);
                    offset = frame_end;
                }
                CanonicalField::Set(values)
            }
            7 => {
                let (count, after) = read_varint(data, offset, bound)?;
                offset = after;
                let count = usize::try_from(count)
                    .map_err(|_| NativeError::limit("native anonymous sequence exceeds usize"))?;
                let mut values = Vec::new();
                reserve_items::<CanonicalNode<'_>>(session, count)?;
                values.try_reserve_exact(count).map_err(|_| {
                    NativeError::limit("native anonymous sequence allocation failed")
                })?;
                for _ in 0..count {
                    if data.get(offset) != Some(&1) {
                        return Err(NativeError::protocol(
                            "native anonymous sequence item marker is invalid",
                        ));
                    }
                    offset += 1;
                    let (frame_start, frame_end) = read_frame(data, offset, bound)?;
                    let (child, consumed) = parse_node(
                        data,
                        frame_start,
                        frame_end,
                        depth.saturating_add(1),
                        terms,
                        limits,
                        cancellation,
                        session,
                    )?;
                    if consumed != frame_end {
                        return Err(NativeError::protocol(
                            "native anonymous sequence frame is invalid",
                        ));
                    }
                    contains_anonymous |= child.contains_anonymous;
                    values.push(child);
                    offset = frame_end;
                }
                CanonicalField::Sequence(values)
            }
            _ => {
                return Err(NativeError::protocol(
                    "native anonymous field marker is invalid",
                ))
            }
        };
        fields.push(field);
    }
    Ok((
        CanonicalNode {
            tag,
            original: data
                .get(start..offset)
                .ok_or_else(|| NativeError::protocol("native anonymous node is truncated"))?,
            fields,
            contains_anonymous,
        },
        offset,
    ))
}

fn anonymous_identity<'a>(node: &CanonicalNode<'a>) -> NativeResult<Identity<'a>> {
    match node.fields.as_slice() {
        [CanonicalField::Scalar(3, scope), CanonicalField::Scalar(3, key)]
            if scope.len() == 32 && !key.is_empty() =>
        {
            Ok(Identity { scope, key })
        }
        _ => Err(NativeError::protocol(
            "native anonymous identity has invalid canonical fields",
        )),
    }
}

fn only_child<'a, 'row>(
    values: &'a [CanonicalNode<'row>],
) -> NativeResult<&'a CanonicalNode<'row>> {
    match values {
        [value] => Ok(value),
        _ => Err(NativeError::protocol(
            "native anonymous child field has invalid cardinality",
        )),
    }
}

fn provisional_label(identity: Identity<'_>) -> NativeResult<Option<&str>> {
    if identity.scope != PROVISIONAL_SCOPE || !identity.key.starts_with(LEXICAL_KEY) {
        return Ok(None);
    }
    let payload = &identity.key[LEXICAL_KEY.len()..];
    let (length, offset) = read_varint(payload, 0, payload.len())?;
    let length = usize::try_from(length)
        .map_err(|_| NativeError::protocol("native blank label length exceeds usize"))?;
    let end = offset
        .checked_add(length)
        .filter(|end| *end == payload.len())
        .ok_or_else(|| NativeError::protocol("native blank label frame is invalid"))?;
    std::str::from_utf8(&payload[offset..end])
        .map(Some)
        .map_err(|_| NativeError::protocol("native blank label is not UTF-8"))
}

fn identities_for_components(
    label_count: usize,
    components: &[BlankComponent],
    scope: [u8; 32],
    session: &mut Session<'_>,
) -> NativeResult<Vec<Option<OwnedIdentity>>> {
    reserve_items::<Option<OwnedIdentity>>(session, label_count)?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(label_count)
        .map_err(|_| NativeError::limit("native blank identity allocation failed"))?;
    result.resize_with(label_count, || None);
    let indexes = sorted_component_indexes(components, session)?;
    let mut start = 0_usize;
    while start < indexes.len() {
        let graph = &components[indexes[start]].alpha.graph;
        let component_class = component_class(graph, session)?;
        let mut end = start + 1;
        while end < indexes.len() && components[indexes[end]].alpha.graph == *graph {
            end += 1;
        }
        for (ordinal, component_index) in indexes[start..end].iter().copied().enumerate() {
            let component = &components[component_index];
            let local = identities_for_order(
                component.global_labels.len(),
                &component.alpha.order,
                scope,
                component_class,
                ordinal,
                session,
            )?;
            for (local_index, global_label) in component.global_labels.iter().copied().enumerate() {
                let identity = local.get(local_index).cloned().ok_or_else(|| {
                    NativeError::protocol("native blank component identity is missing")
                })?;
                let selected = result.get_mut(global_label).ok_or_else(|| {
                    NativeError::protocol("native blank component label is invalid")
                })?;
                if selected.replace(identity).is_some() {
                    return Err(NativeError::protocol(
                        "native blank component identity is duplicated",
                    ));
                }
            }
        }
        start = end;
    }
    if result.iter().any(Option::is_none) {
        return Err(NativeError::protocol(
            "native blank component identity ledger is incomplete",
        ));
    }
    Ok(result)
}

fn component_class(graph: &[u8], session: &mut Session<'_>) -> NativeResult<[u8; 32]> {
    let mut frame = Vec::new();
    append_frame(&mut frame, graph, session)?;
    let mut hasher = Sha256::new();
    hasher.update(COMPONENT_CLASS_DOMAIN);
    hasher.update(&frame);
    Ok(hasher.finish())
}

fn identities_for_order(
    count: usize,
    order: &[usize],
    scope: [u8; 32],
    component_class: [u8; 32],
    occurrence_ordinal: usize,
    session: &mut Session<'_>,
) -> NativeResult<Vec<OwnedIdentity>> {
    reserve_items::<usize>(session, count)?;
    let mut indexes = Vec::new();
    indexes
        .try_reserve_exact(count)
        .map_err(|_| NativeError::limit("native blank binding allocation failed"))?;
    indexes.resize(count, 0);
    for (index, label) in order.iter().copied().enumerate() {
        *indexes
            .get_mut(label)
            .ok_or_else(|| NativeError::protocol("native blank binding order is invalid"))? = index;
    }
    reserve_items::<OwnedIdentity>(session, count)?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(count)
        .map_err(|_| NativeError::limit("native blank identity allocation failed"))?;
    for index in indexes {
        let mut hasher = Sha256::new();
        hasher.update(ANONYMOUS_KEY_DOMAIN);
        hasher.update(&scope);
        hasher.update(&component_class);
        let mut encoded = Vec::new();
        append_varint(
            &mut encoded,
            usize_u64(
                occurrence_ordinal,
                "native blank component ordinal exceeds u64",
            )?,
            session,
        )?;
        append_varint(
            &mut encoded,
            usize_u64(index, "native blank canonical index exceeds u64")?,
            session,
        )?;
        hasher.update(&encoded);
        result.push(OwnedIdentity {
            scope,
            key: hasher.finish(),
        });
    }
    Ok(result)
}

fn rescope_identity(identity: Identity<'_>, scope: [u8; 32]) -> OwnedIdentity {
    let mut hasher = Sha256::new();
    hasher.update(ANONYMOUS_KEY_DOMAIN);
    hasher.update(&scope);
    hasher.update(identity.scope);
    hasher.update(identity.key);
    OwnedIdentity {
        scope,
        key: hasher.finish(),
    }
}

fn ontology_key_text(
    ontology_iri: Option<&str>,
    version_iri: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let Some(ontology_iri) = ontology_iri else {
        if version_iri.is_some() {
            return Err(NativeError::protocol(
                "native anonymous scope has a version IRI without an ontology IRI",
            ));
        }
        return copy_bytes(b"anonymous-ontology", session);
    };
    let mut result = encode_iri_bytes(ontology_iri, session)?;
    if let Some(version_iri) = version_iri {
        let version = encode_iri_bytes(version_iri, session)?;
        append_bytes(&mut result, &version, session)?;
    }
    Ok(result)
}

fn ontology_key_nodes(
    ontology_iri: &Option<Node>,
    version_iri: &Option<Node>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let Some(ontology_iri) = ontology_iri else {
        if version_iri.is_some() {
            return Err(NativeError::protocol(
                "native anonymous scope has a version IRI without an ontology IRI",
            ));
        }
        return copy_bytes(b"anonymous-ontology", session);
    };
    let mut result = copy_bytes(ontology_iri.as_bytes(), session)?;
    if let Some(version_iri) = version_iri {
        append_bytes(&mut result, version_iri.as_bytes(), session)?;
    }
    Ok(result)
}

fn encode_iri_bytes(value: &str, session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
    let mut result = Vec::new();
    append_varint(&mut result, 1, session)?;
    reserve_bytes(session, 1)?;
    result.push(2);
    append_frame(&mut result, value.as_bytes(), session)?;
    Ok(result)
}

fn framed_digest(
    domain: &[u8],
    first: &[u8],
    second: &[u8],
    session: &mut Session<'_>,
) -> NativeResult<[u8; 32]> {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    for value in [first, second] {
        let mut frame = Vec::new();
        append_frame(&mut frame, value, session)?;
        hasher.update(&frame);
    }
    Ok(hasher.finish())
}

fn snapshot_scope(document_fingerprint: [u8; 32]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(SNAPSHOT_SCOPE_DOMAIN);
    hasher.update(&document_fingerprint);
    hasher.update(&[0]); // encode_varint(ordinal=0)
    hasher.finish()
}

fn structural_digest(row: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"pyowl-core:structural-value:v1\0");
    hasher.update(&[2]); // encode_varint(model schema=2)
    hasher.update(row);
    hasher.finish()
}

fn encode_anonymous(
    scope: &[u8; 32],
    key: &[u8],
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let mut result = Vec::new();
    append_varint(&mut result, 3, session)?;
    reserve_bytes(session, 1)?;
    result.push(3);
    append_frame(&mut result, scope, session)?;
    reserve_bytes(session, 1)?;
    result.push(3);
    append_frame(&mut result, key, session)?;
    Ok(result)
}

fn canonicalize_collections(rows: &mut [Vec<Vec<u8>>; 3]) {
    for values in rows {
        values.sort_unstable();
        values.dedup();
    }
}

fn canonical_occurrence_order(
    rows: &[Vec<Vec<u8>>; 3],
    session: &mut Session<'_>,
) -> NativeResult<Vec<usize>> {
    let occurrence_count = rows.iter().try_fold(0_usize, |total, values| {
        total
            .checked_add(values.len())
            .ok_or_else(|| NativeError::limit("native anonymous occurrence count overflow"))
    })?;
    reserve_items::<usize>(session, occurrence_count)?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(occurrence_count)
        .map_err(|_| NativeError::limit("native anonymous root order allocation failed"))?;
    let mut base = 0_usize;
    for values in rows {
        reserve_items::<usize>(session, values.len())?;
        let mut indices = Vec::new();
        indices
            .try_reserve_exact(values.len())
            .map_err(|_| NativeError::limit("native anonymous root order allocation failed"))?;
        indices.extend(0..values.len());
        indices.sort_unstable_by(|left, right| values[*left].cmp(&values[*right]));
        let mut previous = None;
        for index in indices {
            if previous.is_some_and(|prior| values[prior] == values[index]) {
                continue;
            }
            result.push(
                base.checked_add(index)
                    .ok_or_else(|| NativeError::limit("native anonymous root order overflow"))?,
            );
            previous = Some(index);
        }
        base = base
            .checked_add(values.len())
            .ok_or_else(|| NativeError::limit("native anonymous root order overflow"))?;
    }
    Ok(result)
}

fn constructor_ledger(tag: u64) -> NativeResult<(&'static str, &'static [&'static str])> {
    let value = match tag {
        1 => ("IRI", &["value"][..]),
        2 => ("Entity", &["kind", "iri"][..]),
        3 => ("AnonymousIndividual", &["document_scope", "local_key"][..]),
        4 => ("Literal", &["lexical_form", "datatype", "language"][..]),
        5 => ("Annotation", &["property", "value", "annotations"][..]),
        10 => ("ObjectInverseOf", &["property"][..]),
        11 => ("ObjectPropertyChain", &["properties"][..]),
        20 => ("FacetRestriction", &["facet", "value"][..]),
        21 => ("DataIntersectionOf", &["operands"][..]),
        22 => ("DataUnionOf", &["operands"][..]),
        23 => ("DataComplementOf", &["operand"][..]),
        24 => ("DataOneOf", &["values"][..]),
        25 => ("DatatypeRestriction", &["datatype", "restrictions"][..]),
        30 => ("ObjectIntersectionOf", &["operands"][..]),
        31 => ("ObjectUnionOf", &["operands"][..]),
        32 => ("ObjectComplementOf", &["operand"][..]),
        33 => ("ObjectOneOf", &["individuals"][..]),
        34 => ("ObjectSomeValuesFrom", &["property", "filler"][..]),
        35 => ("ObjectAllValuesFrom", &["property", "filler"][..]),
        36 => ("ObjectHasValue", &["property", "value"][..]),
        37 => ("ObjectHasSelf", &["property"][..]),
        38 => (
            "ObjectMinCardinality",
            &["cardinality", "property", "filler"][..],
        ),
        39 => (
            "ObjectMaxCardinality",
            &["cardinality", "property", "filler"][..],
        ),
        40 => (
            "ObjectExactCardinality",
            &["cardinality", "property", "filler"][..],
        ),
        41 => ("DataSomeValuesFrom", &["properties", "filler"][..]),
        42 => ("DataAllValuesFrom", &["properties", "filler"][..]),
        43 => ("DataHasValue", &["property", "value"][..]),
        44 => (
            "DataMinCardinality",
            &["cardinality", "property", "filler"][..],
        ),
        45 => (
            "DataMaxCardinality",
            &["cardinality", "property", "filler"][..],
        ),
        46 => (
            "DataExactCardinality",
            &["cardinality", "property", "filler"][..],
        ),
        60 => ("Declaration", &["entity", "annotations"][..]),
        61 => (
            "SubClassOf",
            &["sub_class", "super_class", "annotations"][..],
        ),
        62 => ("EquivalentClasses", &["expressions", "annotations"][..]),
        63 => ("DisjointClasses", &["expressions", "annotations"][..]),
        64 => (
            "DisjointUnion",
            &["defined_class", "expressions", "annotations"][..],
        ),
        70 => (
            "SubObjectPropertyOf",
            &["sub_property", "super_property", "annotations"][..],
        ),
        71 => (
            "EquivalentObjectProperties",
            &["properties", "annotations"][..],
        ),
        72 => (
            "DisjointObjectProperties",
            &["properties", "annotations"][..],
        ),
        73 => (
            "InverseObjectProperties",
            &["first", "second", "annotations"][..],
        ),
        74 => (
            "ObjectPropertyDomain",
            &["property", "domain", "annotations"][..],
        ),
        75 => (
            "ObjectPropertyRange",
            &["property", "range", "annotations"][..],
        ),
        76 => ("FunctionalObjectProperty", &["property", "annotations"][..]),
        77 => (
            "InverseFunctionalObjectProperty",
            &["property", "annotations"][..],
        ),
        78 => ("ReflexiveObjectProperty", &["property", "annotations"][..]),
        79 => (
            "IrreflexiveObjectProperty",
            &["property", "annotations"][..],
        ),
        80 => ("SymmetricObjectProperty", &["property", "annotations"][..]),
        81 => ("AsymmetricObjectProperty", &["property", "annotations"][..]),
        82 => ("TransitiveObjectProperty", &["property", "annotations"][..]),
        90 => (
            "SubDataPropertyOf",
            &["sub_property", "super_property", "annotations"][..],
        ),
        91 => (
            "EquivalentDataProperties",
            &["properties", "annotations"][..],
        ),
        92 => ("DisjointDataProperties", &["properties", "annotations"][..]),
        93 => (
            "DataPropertyDomain",
            &["property", "domain", "annotations"][..],
        ),
        94 => (
            "DataPropertyRange",
            &["property", "range", "annotations"][..],
        ),
        95 => ("FunctionalDataProperty", &["property", "annotations"][..]),
        100 => (
            "DatatypeDefinition",
            &["datatype", "data_range", "annotations"][..],
        ),
        101 => (
            "HasKey",
            &[
                "class_expression",
                "object_properties",
                "data_properties",
                "annotations",
            ][..],
        ),
        110 => ("SameIndividual", &["individuals", "annotations"][..]),
        111 => ("DifferentIndividuals", &["individuals", "annotations"][..]),
        112 => (
            "ClassAssertion",
            &["class_expression", "individual", "annotations"][..],
        ),
        113 => (
            "ObjectPropertyAssertion",
            &["property", "source", "target", "annotations"][..],
        ),
        114 => (
            "NegativeObjectPropertyAssertion",
            &["property", "source", "target", "annotations"][..],
        ),
        115 => (
            "DataPropertyAssertion",
            &["property", "source", "value", "annotations"][..],
        ),
        116 => (
            "NegativeDataPropertyAssertion",
            &["property", "source", "value", "annotations"][..],
        ),
        120 => (
            "AnnotationAssertion",
            &["property", "subject", "value", "annotations"][..],
        ),
        121 => (
            "SubAnnotationPropertyOf",
            &["sub_property", "super_property", "annotations"][..],
        ),
        122 => (
            "AnnotationPropertyDomain",
            &["property", "domain", "annotations"][..],
        ),
        123 => (
            "AnnotationPropertyRange",
            &["property", "range", "annotations"][..],
        ),
        140 => ("Variable", &["iri"][..]),
        141 => ("ClassAtom", &["predicate", "argument"][..]),
        142 => ("DataRangeAtom", &["predicate", "argument"][..]),
        143 => ("ObjectPropertyAtom", &["predicate", "source", "target"][..]),
        144 => ("DataPropertyAtom", &["predicate", "source", "target"][..]),
        145 => ("BuiltInAtom", &["predicate", "arguments"][..]),
        146 => ("SameIndividualAtom", &["first", "second"][..]),
        147 => ("DifferentIndividualsAtom", &["first", "second"][..]),
        148 => ("SWRLRule", &["body", "head", "annotations"][..]),
        _ => {
            return Err(NativeError::protocol(
                "native anonymous constructor ledger is incomplete",
            ))
        }
    };
    Ok(value)
}

fn joined_path(prefix: &str, suffix: &str, session: &mut Session<'_>) -> NativeResult<String> {
    let mut result = String::new();
    reserve_bytes(
        session,
        prefix.len().saturating_add(suffix.len()).saturating_add(1),
    )?;
    result
        .try_reserve(prefix.len().saturating_add(suffix.len()).saturating_add(1))
        .map_err(|_| NativeError::limit("native blank path allocation failed"))?;
    result.push_str(prefix);
    result.push('/');
    result.push_str(suffix);
    Ok(result)
}

fn full_hex(digest: [u8; 32], session: &mut Session<'_>) -> NativeResult<String> {
    use std::fmt::Write;
    reserve_bytes(session, 64)?;
    let mut output = String::new();
    output
        .try_reserve_exact(64)
        .map_err(|_| NativeError::limit("native blank marker allocation failed"))?;
    for byte in digest {
        write!(output, "{byte:02x}").expect("writing to String cannot fail");
    }
    Ok(output)
}

fn read_frame(data: &[u8], offset: usize, bound: usize) -> NativeResult<(usize, usize)> {
    let (length, start) = read_varint(data, offset, bound)?;
    let length = usize::try_from(length)
        .map_err(|_| NativeError::protocol("native anonymous frame exceeds usize"))?;
    let end = start
        .checked_add(length)
        .filter(|end| *end <= bound && *end <= data.len())
        .ok_or_else(|| NativeError::protocol("native anonymous frame is truncated"))?;
    Ok((start, end))
}

fn read_varint(data: &[u8], offset: usize, bound: usize) -> NativeResult<(u64, usize)> {
    let start = offset;
    let mut cursor = offset;
    let mut value = 0_u64;
    let mut shift = 0_u32;
    while cursor < bound {
        let byte = data[cursor];
        cursor += 1;
        let payload = byte & 0x7f;
        if cursor - start > 10 || (shift == 63 && payload > 1) {
            return Err(NativeError::protocol(
                "native anonymous varint is too large",
            ));
        }
        value |= u64::from(payload) << shift;
        if byte & 0x80 == 0 {
            if cursor - start > 1 && byte == 0 {
                return Err(NativeError::protocol(
                    "native anonymous varint is nonminimal",
                ));
            }
            return Ok((value, cursor));
        }
        shift += 7;
    }
    Err(NativeError::protocol(
        "native anonymous varint is truncated",
    ))
}

fn read_any_varint(data: &[u8], offset: usize, bound: usize) -> NativeResult<usize> {
    let start = offset;
    let mut cursor = offset;
    while cursor < bound {
        let byte = data[cursor];
        cursor += 1;
        if byte & 0x80 == 0 {
            if cursor - start > 1 && byte == 0 {
                return Err(NativeError::protocol(
                    "native anonymous integer is nonminimal",
                ));
            }
            return Ok(cursor);
        }
        if cursor - start >= 142_858 {
            return Err(NativeError::protocol(
                "native anonymous integer is unreasonably long",
            ));
        }
    }
    Err(NativeError::protocol(
        "native anonymous integer is truncated",
    ))
}

fn append_frame(output: &mut Vec<u8>, value: &[u8], session: &mut Session<'_>) -> NativeResult<()> {
    append_varint(
        output,
        usize_u64(value.len(), "native frame length exceeds u64")?,
        session,
    )?;
    append_bytes(output, value, session)
}

fn append_varint(
    output: &mut Vec<u8>,
    mut value: u64,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let mut bytes = [0_u8; 10];
    let mut length = 0_usize;
    loop {
        let mut byte = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            byte |= 0x80;
        }
        bytes[length] = byte;
        length += 1;
        if value == 0 {
            return append_bytes(output, &bytes[..length], session);
        }
    }
}

fn append_bytes(output: &mut Vec<u8>, value: &[u8], session: &mut Session<'_>) -> NativeResult<()> {
    reserve_bytes(session, value.len())?;
    output
        .try_reserve(value.len())
        .map_err(|_| NativeError::limit("native anonymous byte allocation failed"))?;
    output.extend_from_slice(value);
    Ok(())
}

fn reserve_items<T>(session: &mut Session<'_>, count: usize) -> NativeResult<()> {
    let bytes = count
        .checked_mul(size_of::<T>())
        .ok_or_else(|| NativeError::limit("native anonymous allocation accounting overflow"))?;
    reserve_bytes(session, bytes)
}

fn reserve_bytes(session: &mut Session<'_>, bytes: usize) -> NativeResult<()> {
    session.reserve_temporary_bytes(bytes)
}

fn copy_bytes(value: &[u8], session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
    reserve_bytes(session, value.len())?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native anonymous byte allocation failed"))?;
    output.extend_from_slice(value);
    Ok(output)
}

fn owned_text(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    reserve_bytes(session, value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native anonymous text allocation failed"))?;
    output.push_str(value);
    Ok(output)
}

fn one_byte(value: u8, session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
    reserve_bytes(session, 1)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(1)
        .map_err(|_| NativeError::limit("native anonymous byte allocation failed"))?;
    output.push(value);
    Ok(output)
}

fn set_marker(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    let mut output = String::new();
    reserve_bytes(session, value.len().saturating_add(4))?;
    output
        .try_reserve_exact(value.len().saturating_add(4))
        .map_err(|_| NativeError::limit("native blank set marker allocation failed"))?;
    output.push_str("set:");
    output.push_str(value);
    Ok(output)
}

fn decimal_index(value: usize, session: &mut Session<'_>) -> NativeResult<String> {
    use std::fmt::Write;
    let capacity = usize::BITS as usize;
    reserve_bytes(session, capacity)?;
    let mut output = String::new();
    output
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native blank index allocation failed"))?;
    write!(output, "{value}").expect("writing to String cannot fail");
    Ok(output)
}

fn enforce_work(work: u64, limits: &Limits, details: NativeLimitDetails) -> NativeResult<()> {
    if work > limits.max_canonical_work {
        return Err(NativeError::resource_limit_with_details(
            "max_canonical_work",
            work,
            limits.max_canonical_work,
            details,
            "native anonymous canonicalization exceeds max_canonical_work",
        ));
    }
    Ok(())
}

fn usize_u64(value: usize, message: &'static str) -> NativeResult<u64> {
    u64::try_from(value).map_err(|_| NativeError::limit(message))
}

fn checked_add_u64(left: usize, right: usize, message: &'static str) -> NativeResult<u64> {
    usize_u64(left, message)?
        .checked_add(usize_u64(right, message)?)
        .ok_or_else(|| NativeError::limit(message))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::Guard;
    use crate::canonical::{anonymous, entity, Field, Node};
    use crate::error::{NativeErrorPayload, NativeNumber};

    fn hex(value: &[u8]) -> String {
        use std::fmt::Write;

        let mut output = String::with_capacity(value.len().saturating_mul(2));
        for byte in value {
            write!(output, "{byte:02x}").expect("writing to String cannot fail");
        }
        output
    }

    fn scope(
        ontology_iri: Option<&str>,
        rows: [&[Vec<u8>]; 3],
    ) -> NativeResult<ScopedAnonymousRowsV2> {
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let input_bytes = rows
            .iter()
            .flat_map(|values| values.iter())
            .map(Vec::len)
            .sum();
        let mut session = Session::new(&mut guard, &limits, input_bytes)?;
        scope_rdfxml_anonymous_rows_v2(ontology_iri, None, &[], rows, &mut session, &cancellation)
    }

    fn scope_with_accounting(
        rows: [&[Vec<u8>]; 3],
    ) -> NativeResult<(AnonymousCanonicalMetricsV2, u64)> {
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let input_bytes = rows
            .iter()
            .flat_map(|values| values.iter())
            .map(Vec::len)
            .sum();
        let mut session = Session::new(&mut guard, &limits, input_bytes)?;
        let before = session.accounted_bytes();
        let result = scope_anonymous_rows_v2(
            rows,
            b"ontology-key".to_vec(),
            &mut session,
            &cancellation,
            |_| {
                Ok(super::super::retained::FingerprintEvidenceV2 {
                    preimage_bytes: 0,
                    digest: sha256(b"document"),
                })
            },
        )?;
        let delta = session
            .accounted_bytes()
            .checked_sub(before)
            .expect("session accounting is monotonic");
        Ok((result.anonymous_metrics, delta))
    }

    #[test]
    fn scopes_one_blank_into_distinct_raw_and_effective_rows() {
        let axiom = Node::build(
            112,
            vec![
                Field::Node(entity("class", iri("urn:C".to_owned()).unwrap()).unwrap()),
                Field::Node(anonymous("person").unwrap()),
                Field::Set(Vec::new()),
            ],
        )
        .unwrap()
        .into_bytes();
        let result = scope(Some("urn:o"), [&[], &[axiom], &[]]).unwrap();
        assert_eq!(result.raw[1].len(), 1);
        assert_eq!(result.effective[1].len(), 1);
        assert_ne!(result.raw[1], result.effective[1]);
        assert_eq!(result.effective_occurrence_digests.len(), 1);
        assert_eq!(result.source_occurrence_digests.len(), 1);
        assert_eq!(
            result.source_occurrence_digests[0].0,
            structural_digest(&result.raw[1][0]),
        );
        assert_eq!(
            result.source_occurrence_digests[0].1,
            structural_digest(&result.effective[1][0]),
        );
    }

    #[test]
    fn anonymous_accounting_is_the_exact_deterministic_session_delta() {
        let axiom = Node::build(
            112,
            vec![
                Field::Node(entity("class", iri("urn:C".to_owned()).unwrap()).unwrap()),
                Field::Node(anonymous("person").unwrap()),
                Field::Set(Vec::new()),
            ],
        )
        .unwrap()
        .into_bytes();

        let (first, first_delta) =
            scope_with_accounting([&[], &[axiom.clone()], &[]]).expect("first scope");
        let (second, second_delta) =
            scope_with_accounting([&[], &[axiom], &[]]).expect("second scope");

        assert!(first.accounted_bytes > 0);
        assert_eq!(first.accounted_bytes, first_delta);
        assert_eq!(second.accounted_bytes, second_delta);
        assert_eq!(first.accounted_bytes, second.accounted_bytes);
    }

    #[test]
    fn duplicate_occurrences_retain_digests_while_root_tables_deduplicate() {
        let axiom = Node::build(
            112,
            vec![
                Field::Node(entity("class", iri("urn:C".to_owned()).unwrap()).unwrap()),
                Field::Node(anonymous("person").unwrap()),
                Field::Set(Vec::new()),
            ],
        )
        .unwrap()
        .into_bytes();
        let result = scope(Some("urn:o"), [&[], &[axiom.clone(), axiom], &[]]).unwrap();
        assert_eq!(result.raw[1].len(), 1);
        assert_eq!(result.effective[1].len(), 1);
        assert_eq!(result.effective_occurrence_digests.len(), 1);
        assert_eq!(result.source_occurrence_digests.len(), 2);
        assert_eq!(
            result.source_occurrence_digests[0],
            result.source_occurrence_digests[1],
        );
    }

    #[test]
    fn constructor_ledger_matches_the_canonical_field_ledger() {
        for tag in [
            1_u16, 2, 3, 4, 5, 10, 11, 20, 21, 22, 23, 24, 25, 30, 31, 32, 33, 34, 35, 36, 37, 38,
            39, 40, 41, 42, 43, 44, 45, 46, 60, 61, 62, 63, 64, 70, 71, 72, 73, 74, 75, 76, 77, 78,
            79, 80, 81, 82, 90, 91, 92, 93, 94, 95, 100, 101, 110, 111, 112, 113, 114, 115, 116,
            120, 121, 122, 123, 140, 141, 142, 143, 144, 145, 146, 147, 148,
        ] {
            assert_eq!(
                constructor_ledger(u64::from(tag)).unwrap().1.len(),
                usize::from(canonical_field_count(tag).unwrap()),
                "tag {tag}",
            );
        }
    }

    #[test]
    fn scoping_preserves_arbitrarily_wide_integer_fields() {
        let enumeration =
            Node::build(33, vec![Field::Set(vec![anonymous("member").unwrap()])]).unwrap();
        let property = entity(
            "object_property",
            iri("urn:p".to_owned()).expect("property IRI"),
        )
        .expect("property");
        let restriction = Node::build(
            38,
            vec![
                Field::Integer("1180591620717411303424".to_owned()),
                Field::Node(property),
                Field::Node(enumeration),
            ],
        )
        .unwrap();
        let axiom = Node::build(
            112,
            vec![
                Field::Node(restriction),
                Field::Node(anonymous("subject").unwrap()),
                Field::Set(Vec::new()),
            ],
        )
        .unwrap()
        .into_bytes();
        let result = scope(None, [&[], &[axiom], &[]]).expect("wide integer anonymous scope");
        let mut budget = ScanBudget::from_limits(&Limits::default());
        assert_eq!(
            scan_canonical(&result.raw[1][0], &mut budget).expect("scoped row"),
            crate::model::Category::Axiom,
        );
    }

    #[test]
    fn component_manifest_and_scope_match_the_schema_two_ledger() {
        assert_eq!(
            sha256(b"pyowl-core:provisional-document-scope:v2\0"),
            PROVISIONAL_SCOPE,
        );
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let components = [
            BlankComponent {
                global_labels: vec![0],
                arcs: Vec::new(),
                alpha: AlphaResult {
                    order: vec![0],
                    graph: b"beta".to_vec(),
                    setup_work: 0,
                    refinement_work: 0,
                    candidate_order_work: 0,
                    refinement_rounds: 0,
                    permutations_examined: 1,
                },
            },
            BlankComponent {
                global_labels: vec![1],
                arcs: Vec::new(),
                alpha: AlphaResult {
                    order: vec![0],
                    graph: b"alpha".to_vec(),
                    setup_work: 0,
                    refinement_work: 0,
                    candidate_order_work: 0,
                    refinement_rounds: 0,
                    permutations_examined: 1,
                },
            },
            BlankComponent {
                global_labels: vec![2],
                arcs: Vec::new(),
                alpha: AlphaResult {
                    order: vec![0],
                    graph: b"alpha".to_vec(),
                    setup_work: 0,
                    refinement_work: 0,
                    candidate_order_work: 0,
                    refinement_rounds: 0,
                    permutations_examined: 1,
                },
            },
        ];
        let manifest = component_manifest(&components, &mut session).expect("component manifest");
        assert_eq!(
            hex(&manifest),
            "70796f776c2d636f72653a626c616e6b2d636f6d706f6e656e742d6d616e69666573743a7632000205616c70686102046265746101",
        );
        let scope = framed_digest(
            DOCUMENT_SCOPE_DOMAIN,
            b"ontology-key",
            &manifest,
            &mut session,
        )
        .expect("document scope");
        assert_eq!(
            hex(&scope),
            "f07f564a417f771c6ca7a28eca28677a6769cd5e6de976dba0493523c7ea5a5c",
        );
    }

    #[test]
    fn repeated_component_keys_and_work_match_the_schema_two_ledger() {
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let payloads = vec![b"payload".to_vec(), b"payload".to_vec()];
        let first_arc = BlankArc {
            source: 0,
            role: "edge".to_owned(),
            target: Some(1),
            payload: 0,
        };
        let second_arc = BlankArc {
            payload: 1,
            ..first_arc.clone()
        };
        let first = alpha_order(
            2,
            std::slice::from_ref(&first_arc),
            &payloads,
            &limits,
            NativeLimitDetails {
                component_count: Some(2),
                largest_component_labels: Some(2),
                largest_component_arcs: Some(1),
                refinement_rounds: None,
                work_term: None,
            },
            &cancellation,
            &mut session,
        )
        .expect("first component");
        assert_eq!(
            hex(&first.graph),
            "70796f776c2d636f72653a626c616e6b2d67726170683a7632000201100004656467650101077061796c6f6164",
        );
        assert_eq!(
            (
                first.setup_work,
                first.refinement_work,
                first.candidate_order_work,
                first.setup_work + first.refinement_work + first.candidate_order_work,
                first.refinement_rounds,
                first.permutations_examined,
            ),
            (4, 6, 3, 13, 1, 1),
        );
        let second = alpha_order(
            2,
            std::slice::from_ref(&second_arc),
            &payloads,
            &limits,
            NativeLimitDetails {
                component_count: Some(2),
                largest_component_labels: Some(2),
                largest_component_arcs: Some(1),
                refinement_rounds: None,
                work_term: None,
            },
            &cancellation,
            &mut session,
        )
        .expect("second component");
        let components = [
            BlankComponent {
                global_labels: vec![0, 1],
                arcs: vec![first_arc],
                alpha: first,
            },
            BlankComponent {
                global_labels: vec![2, 3],
                arcs: vec![second_arc],
                alpha: second,
            },
        ];
        let manifest = component_manifest(&components, &mut session).expect("component manifest");
        let scope = framed_digest(
            DOCUMENT_SCOPE_DOMAIN,
            b"ontology-key",
            &manifest,
            &mut session,
        )
        .expect("document scope");
        assert_eq!(
            hex(&scope),
            "8d1c3f2c1068788506082f3c597048d2d934dfad530d6c864d708bb5b8c0c0a9",
        );
        let identities = identities_for_components(4, &components, scope, &mut session)
            .expect("component identities");
        let keys: Vec<String> = identities
            .iter()
            .map(|identity| hex(&identity.as_ref().expect("bound identity").key))
            .collect();
        assert_eq!(
            keys,
            [
                "b22b8089f9ef61ea37d482f355e714bae2c0cf3dd3e6738bfb70c7d3c69c8c73",
                "193b7a5768e93a443b8fca99d386985997d3a1dd0b83f84d76bd1725a4f5236b",
                "71d226d29151745d047295d1573b190764da7c9ae38af5678fae7e05c2855a3e",
                "bdac8436947fc21f5b4aa132835300787003bba6914114624f69578a80751373",
            ],
        );
        let metrics = anonymous_metrics(&components, 4, 2, &mut session).expect("metrics");
        assert_eq!(metrics.component_count, 2);
        assert_eq!(metrics.largest_component_labels, 2);
        assert_eq!(metrics.largest_component_arcs, 1);
        assert_eq!(metrics.largest_component_roots, 1);
        assert_eq!(metrics.maximum_root_interval_span, 1);
        assert_eq!(metrics.maximum_open_root_intervals, 1);
        assert_eq!(metrics.total_setup_work, 8);
        assert_eq!(metrics.total_refinement_work, 12);
        assert_eq!(metrics.total_candidate_order_work, 6);
        assert_eq!(metrics.total_canonical_work, 26);
    }

    #[test]
    fn component_work_failure_carries_typed_global_context() {
        let mut limits = Limits::default();
        limits.max_canonical_work = 3;
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let arc = BlankArc {
            source: 0,
            role: "edge".to_owned(),
            target: Some(1),
            payload: 0,
        };
        let error = alpha_order(
            2,
            &[arc],
            &[b"payload".to_vec()],
            &limits,
            NativeLimitDetails {
                component_count: Some(17),
                largest_component_labels: Some(2),
                largest_component_arcs: Some(1),
                refinement_rounds: None,
                work_term: None,
            },
            &cancellation,
            &mut session,
        )
        .expect_err("setup work exceeds the configured component budget");
        let NativeErrorPayload::ResourceLimit(payload) = error.payload else {
            panic!("configured work failure has no typed payload");
        };
        assert_eq!(payload.limit, "max_canonical_work");
        assert_eq!(payload.observed, NativeNumber::Integer(4));
        assert_eq!(payload.allowed, NativeNumber::Integer(3));
        assert_eq!(payload.details.component_count, Some(17));
        assert_eq!(payload.details.largest_component_labels, Some(2));
        assert_eq!(payload.details.largest_component_arcs, Some(1));
        assert_eq!(payload.details.refinement_rounds, Some(0));
        assert_eq!(payload.details.work_term, Some("setup"));
    }

    #[test]
    fn anonymous_and_retry_enforcement_share_typed_limit_fields() {
        let mut limits = Limits::default();
        limits.max_canonical_work = 10;
        let anonymous = enforce_work(
            11,
            &limits,
            NativeLimitDetails {
                component_count: Some(1),
                largest_component_labels: Some(2),
                largest_component_arcs: Some(1),
                refinement_rounds: Some(0),
                work_term: Some("setup"),
            },
        )
        .expect_err("anonymous enforcement must reject excess work");

        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session = Session::new(&mut guard, &limits, 0).expect("retry session");
        let retry = session
            .step(11)
            .expect_err("generic retry enforcement must reject excess work");

        assert_ne!(anonymous.message, retry.message);
        let NativeErrorPayload::ResourceLimit(anonymous_payload) = anonymous.payload else {
            panic!("anonymous enforcement omitted its typed payload");
        };
        let NativeErrorPayload::ResourceLimit(retry_payload) = retry.payload else {
            panic!("retry enforcement omitted its typed payload");
        };
        assert_eq!(anonymous_payload.limit, retry_payload.limit);
        assert_eq!(anonymous_payload.observed, retry_payload.observed);
        assert_eq!(anonymous_payload.allowed, retry_payload.allowed);
    }
}
