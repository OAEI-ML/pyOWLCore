//! Native document and snapshot scoping for parser-produced anonymous values.
//!
//! RDF/XML mapping initially uses lexical blank labels under the provisional
//! parser scope. This module reproduces the Python model's structural alpha
//! canonicalization before those rows enter retained storage, then derives the
//! distinct one-document snapshot scope used by effective facade owners.

use std::collections::{BTreeMap, BTreeSet};

use crate::cancel::Cancellation;
use crate::canonical::{iri, LEXICAL_KEY, PROVISIONAL_SCOPE};
use crate::error::{NativeError, NativeResult};
use crate::hash::{sha256, Sha256};
use crate::limits::Limits;
use crate::model::{canonical_field_count, scan_canonical, ScanBudget};

use super::retained::rdfxml_document_fingerprint;

const DOCUMENT_SCOPE_DOMAIN: &[u8] = b"pyowl-core:document-scope:v1\0";
const SNAPSHOT_SCOPE_DOMAIN: &[u8] = b"pyowl-core:snapshot-document-scope:v1\0";
const ANONYMOUS_KEY_DOMAIN: &[u8] = b"pyowl-core:anonymous-key:v1\0";
const BLANK_GRAPH_DOMAIN: &[u8] = b"pyowl-core:blank-graph:v1\0";
const BLANK_COLOR_DOMAIN: &[u8] = b"pyowl-core:blank-color:v1\0";

#[derive(Debug)]
pub(crate) struct ScopedAnonymousRowsV2 {
    pub(crate) raw: [Vec<Vec<u8>>; 3],
    pub(crate) effective: [Vec<Vec<u8>>; 3],
    /// Effective digests in raw collection/row order, matching retained
    /// parser occurrence metadata rather than effective canonical ordering.
    pub(crate) effective_occurrence_digests: Vec<[u8; 32]>,
}

#[derive(Clone, Copy, Debug)]
struct Identity<'a> {
    scope: &'a [u8],
    key: &'a [u8],
}

#[derive(Clone, Debug)]
struct OwnedIdentity {
    scope: [u8; 32],
    key: Vec<u8>,
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
    Node(Box<CanonicalNode<'a>>),
    Scalar(u8, &'a [u8]),
    Set(Vec<CanonicalNode<'a>>),
    Sequence(Vec<CanonicalNode<'a>>),
}

#[derive(Debug)]
struct BlankArc {
    source: usize,
    role: String,
    target: Option<usize>,
    payload: usize,
}

/// Freeze provisional RDF/XML blank labels into exact raw document identities
/// and exact effective snapshot identities without constructing Python model
/// objects or exporting canonical rows across the language boundary.
pub(crate) fn scope_rdfxml_anonymous_rows_v2(
    ontology_iri: Option<&str>,
    version_iri: Option<&str>,
    imports: &[String],
    rows: [&[Vec<u8>]; 3],
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<ScopedAnonymousRowsV2> {
    let mut parsed = [Vec::new(), Vec::new(), Vec::new()];
    for (target, source) in parsed.iter_mut().zip(rows) {
        target
            .try_reserve_exact(source.len())
            .map_err(|_| NativeError::limit("native anonymous root allocation failed"))?;
        for row in source {
            cancellation.checkpoint()?;
            let mut scan = ScanBudget::from_limits(limits);
            scan_canonical(row, &mut scan)?;
            target.push(parse_root(row, limits, cancellation)?);
        }
    }

    let mut labels = BTreeSet::new();
    for node in parsed.iter().flatten() {
        collect_labels(node, &mut labels)?;
    }
    if labels.is_empty() {
        return Err(NativeError::protocol(
            "native anonymous scoping received no provisional blank labels",
        ));
    }
    let labels: Vec<String> = labels.into_iter().collect();
    let label_indexes: BTreeMap<&str, usize> = labels
        .iter()
        .enumerate()
        .map(|(index, label)| (label.as_str(), index))
        .collect();
    let (arcs, payloads) = blank_arcs(&parsed, &label_indexes, limits, cancellation)?;
    let alpha = alpha_order(&labels, &arcs, &payloads, limits, cancellation)?;

    let ontology_key = ontology_key(ontology_iri, version_iri)?;
    let document_scope = framed_digest(DOCUMENT_SCOPE_DOMAIN, &ontology_key, &alpha.graph)?;
    let graph_digest = sha256(&alpha.graph);
    let raw_identities =
        identities_for_order(labels.len(), &alpha.order, document_scope, graph_digest)?;

    let mut raw = encode_collections(&parsed, |identity| {
        let label = provisional_label(identity)?;
        Ok(label.and_then(|value| {
            label_indexes
                .get(value)
                .and_then(|index| raw_identities.get(*index))
                .cloned()
        }))
    })?;
    canonicalize_collections(&mut raw);

    let raw_slices = [raw[0].as_slice(), raw[1].as_slice(), raw[2].as_slice()];
    let document = rdfxml_document_fingerprint(ontology_iri, version_iri, imports, raw_slices)?;
    let snapshot_scope = snapshot_scope(document.digest);
    let mut effective_occurrence_digests = Vec::new();
    let occurrence_count = raw.iter().try_fold(0_usize, |total, values| {
        total
            .checked_add(values.len())
            .ok_or_else(|| NativeError::limit("native anonymous occurrence count overflow"))
    })?;
    effective_occurrence_digests
        .try_reserve_exact(occurrence_count)
        .map_err(|_| NativeError::limit("native anonymous digest allocation failed"))?;

    let mut effective = [Vec::new(), Vec::new(), Vec::new()];
    for (target, source) in effective.iter_mut().zip(&raw) {
        target
            .try_reserve_exact(source.len())
            .map_err(|_| NativeError::limit("native effective root allocation failed"))?;
        for row in source {
            cancellation.checkpoint()?;
            let node = parse_root(row, limits, cancellation)?;
            let encoded = encode_replaced(&node, &|identity| {
                Ok(Some(rescope_identity(identity, snapshot_scope)))
            })?;
            effective_occurrence_digests.push(structural_digest(&encoded));
            target.push(encoded);
        }
    }
    canonicalize_collections(&mut effective);
    for (raw_values, effective_values) in raw.iter().zip(&effective) {
        if raw_values.len() != effective_values.len() {
            return Err(NativeError::protocol(
                "native anonymous scoping changed structural root cardinality",
            ));
        }
    }
    Ok(ScopedAnonymousRowsV2 {
        raw,
        effective,
        effective_occurrence_digests,
    })
}

#[derive(Debug)]
struct AlphaResult {
    order: Vec<usize>,
    graph: Vec<u8>,
}

fn alpha_order(
    labels: &[String],
    arcs: &[BlankArc],
    payloads: &[Vec<u8>],
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<AlphaResult> {
    let terms = checked_add_u64(labels.len(), arcs.len(), "native blank term count overflow")?;
    if terms > limits.max_terms {
        return Err(NativeError::limit(
            "native anonymous canonicalization exceeds max_terms",
        ));
    }
    let label_count = usize_u64(labels.len(), "native blank label count exceeds u64")?;
    let arc_count = usize_u64(arcs.len(), "native blank arc count exceeds u64")?;
    let mut work = label_count
        .checked_add(
            arc_count
                .checked_mul(2)
                .ok_or_else(|| NativeError::limit("native blank work overflow"))?,
        )
        .ok_or_else(|| NativeError::limit("native blank work overflow"))?;
    enforce_work(work, limits)?;
    let mut colors = colors_from_signatures(neighborhoods(
        labels.len(),
        arcs,
        payloads,
        None,
        cancellation,
    )?)?;
    let mut rounds = 0_usize;
    loop {
        cancellation.checkpoint()?;
        rounds = rounds
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native blank refinement overflow"))?;
        let neighborhoods =
            neighborhoods(labels.len(), arcs, payloads, Some(&colors), cancellation)?;
        let mut signatures = Vec::new();
        signatures
            .try_reserve_exact(labels.len())
            .map_err(|_| NativeError::limit("native blank signature allocation failed"))?;
        for (color, neighborhood) in colors.iter().zip(neighborhoods) {
            let mut signature = Vec::new();
            signature
                .try_reserve_exact(neighborhood.len().saturating_add(1))
                .map_err(|_| NativeError::limit("native blank signature allocation failed"))?;
            signature.push(color.to_vec());
            signature.extend(neighborhood);
            signatures.push(signature);
        }
        let refined = colors_from_signatures(signatures)?;
        work = work
            .checked_add(
                label_count
                    .checked_mul(2)
                    .and_then(|value| value.checked_add(arc_count.checked_mul(2)?))
                    .ok_or_else(|| NativeError::limit("native blank work overflow"))?,
            )
            .ok_or_else(|| NativeError::limit("native blank work overflow"))?;
        enforce_work(work, limits)?;
        if same_partition(&colors, &refined) {
            colors = refined;
            break;
        }
        colors = refined;
        if rounds > labels.len().saturating_add(1) {
            return Err(NativeError::protocol(
                "native blank-node partition refinement did not converge",
            ));
        }
    }

    let partitions = partitions(&colors);
    let candidates = permutation_count(&partitions, limits.max_canonical_work, work)?;
    let unit = label_count.saturating_add(arc_count).max(1);
    enforce_work(work.saturating_add(candidates.saturating_mul(unit)), limits)?;

    let mut choices = partitions.clone();
    let mut best_graph: Option<Vec<u8>> = None;
    let mut best_order: Option<Vec<usize>> = None;
    loop {
        cancellation.checkpoint()?;
        let order: Vec<usize> = choices.iter().flatten().copied().collect();
        let graph = serialize_graph(&order, arcs, payloads)?;
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
    })
}

fn neighborhoods(
    label_count: usize,
    arcs: &[BlankArc],
    payloads: &[Vec<u8>],
    colors: Option<&[[u8; 32]]>,
    cancellation: &Cancellation,
) -> NativeResult<Vec<Vec<Vec<u8>>>> {
    let mut gathered = vec![Vec::new(); label_count];
    for arc in arcs {
        cancellation.checkpoint()?;
        gathered[arc.source].push(arc_signature(arc.source, arc, payloads, colors)?);
        if let Some(target) = arc.target.filter(|target| *target != arc.source) {
            gathered[target].push(arc_signature(target, arc, payloads, colors)?);
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
) -> NativeResult<Vec<u8>> {
    let (direction, neighbor): (u8, Vec<u8>) = if arc.source == label {
        (
            b'S',
            match arc.target {
                None => vec![b'N'],
                Some(target) if target == label => vec![b'L'],
                Some(target) => neighbor_color(target, colors)?,
            },
        )
    } else if arc.target == Some(label) {
        (b'T', neighbor_color(arc.source, colors)?)
    } else {
        return Err(NativeError::protocol(
            "native blank arc does not contain its requested label",
        ));
    };
    let payload = payloads
        .get(arc.payload)
        .ok_or_else(|| NativeError::protocol("native blank arc payload is missing"))?;
    let mut result = Vec::new();
    result.push(direction);
    append_frame(&mut result, arc.role.as_bytes())?;
    append_bytes(&mut result, &neighbor)?;
    append_frame(&mut result, payload)?;
    Ok(result)
}

fn neighbor_color(target: usize, colors: Option<&[[u8; 32]]>) -> NativeResult<Vec<u8>> {
    let Some(colors) = colors else {
        return Ok(vec![b'B']);
    };
    let color = colors
        .get(target)
        .ok_or_else(|| NativeError::protocol("native blank neighbor color is missing"))?;
    let mut result = Vec::with_capacity(33);
    result.push(b'C');
    result.extend_from_slice(color);
    Ok(result)
}

fn colors_from_signatures(signatures: Vec<Vec<Vec<u8>>>) -> NativeResult<Vec<[u8; 32]>> {
    signatures
        .into_iter()
        .map(|signature| {
            let mut hasher = Sha256::new();
            hasher.update(BLANK_COLOR_DOMAIN);
            for item in signature {
                let mut framed = Vec::new();
                append_frame(&mut framed, &item)?;
                hasher.update(&framed);
            }
            Ok(hasher.finish())
        })
        .collect()
}

fn same_partition(first: &[[u8; 32]], second: &[[u8; 32]]) -> bool {
    let mut forward = BTreeMap::new();
    let mut reverse = BTreeMap::new();
    first.iter().zip(second).all(|(left, right)| {
        forward.entry(*left).or_insert(*right) == right
            && reverse.entry(*right).or_insert(*left) == left
    })
}

fn partitions(colors: &[[u8; 32]]) -> Vec<Vec<usize>> {
    let mut grouped: BTreeMap<[u8; 32], Vec<usize>> = BTreeMap::new();
    for (index, color) in colors.iter().enumerate() {
        grouped.entry(*color).or_default().push(index);
    }
    grouped.into_values().collect()
}

fn permutation_count(partitions: &[Vec<usize>], maximum: u64, consumed: u64) -> NativeResult<u64> {
    let remaining = maximum.saturating_sub(consumed);
    let mut count = 1_u64;
    for partition in partitions {
        for factor in 2..=partition.len() {
            let factor = usize_u64(factor, "native blank permutation factor exceeds u64")?;
            if count > remaining / factor {
                return Err(NativeError::limit(
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
) -> NativeResult<Vec<u8>> {
    let mut indexes = vec![0_usize; order.len()];
    for (index, label) in order.iter().copied().enumerate() {
        *indexes
            .get_mut(label)
            .ok_or_else(|| NativeError::protocol("native blank order is invalid"))? = index;
    }
    let mut members = BTreeSet::new();
    for arc in arcs {
        let payload = payloads
            .get(arc.payload)
            .ok_or_else(|| NativeError::protocol("native blank graph payload is missing"))?;
        let mut member = Vec::new();
        append_varint(
            &mut member,
            usize_u64(indexes[arc.source], "blank index exceeds u64")?,
        )?;
        append_frame(&mut member, arc.role.as_bytes())?;
        match arc.target {
            None => member.push(0),
            Some(target) => {
                member.push(1);
                append_varint(
                    &mut member,
                    usize_u64(indexes[target], "blank target index exceeds u64")?,
                )?;
            }
        }
        append_frame(&mut member, payload)?;
        members.insert(member);
    }
    let mut graph = Vec::new();
    append_bytes(&mut graph, BLANK_GRAPH_DOMAIN)?;
    append_varint(
        &mut graph,
        usize_u64(order.len(), "blank order exceeds u64")?,
    )?;
    append_varint(
        &mut graph,
        usize_u64(members.len(), "blank graph exceeds u64")?,
    )?;
    for member in members {
        append_frame(&mut graph, &member)?;
    }
    Ok(graph)
}

fn blank_arcs(
    roots: &[Vec<CanonicalNode<'_>>; 3],
    labels: &BTreeMap<&str, usize>,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<(Vec<BlankArc>, Vec<Vec<u8>>)> {
    let mut arcs = Vec::new();
    let mut payloads = Vec::new();
    for root in roots.iter().flatten() {
        cancellation.checkpoint()?;
        let skeleton = skeleton_node(root)?;
        if usize_u64(skeleton.len(), "blank skeleton exceeds u64")? > limits.max_canonical_work {
            return Err(NativeError::limit(
                "native blank skeleton exceeds max_canonical_work",
            ));
        }
        let payload = payloads.len();
        payloads.push(skeleton);
        let (name, _) = constructor_ledger(root.tag)?;
        let mut occurrences = Vec::new();
        blank_occurrences(root, name.to_owned(), &mut occurrences)?;
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
        if usize_u64(following, "native blank arc count exceeds u64")? > limits.max_terms {
            return Err(NativeError::limit(
                "native anonymous canonicalization exceeds max_terms",
            ));
        }
        arcs.try_reserve(following.saturating_sub(arcs.len()))
            .map_err(|_| NativeError::limit("native blank arc allocation failed"))?;
        for (label, path) in &occurrences {
            arcs.push(BlankArc {
                source: *labels.get(label.as_str()).ok_or_else(|| {
                    NativeError::protocol("native blank occurrence label is missing")
                })?,
                role: path.clone(),
                target: None,
                payload,
            });
        }
        for (index, (source, source_path)) in occurrences.iter().enumerate() {
            for (target, target_path) in &occurrences[index + 1..] {
                let mut role = String::new();
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
                    source: labels[source.as_str()],
                    role,
                    target: Some(labels[target.as_str()]),
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
    output: &mut Vec<(String, String)>,
) -> NativeResult<()> {
    if node.tag == 3 {
        if let Some(label) = provisional_label(anonymous_identity(node)?)? {
            output.push((label.to_owned(), path));
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
        let field_path = joined_path(&path, name)?;
        match field {
            CanonicalField::Node(child) => blank_occurrences(child, field_path, output)?,
            CanonicalField::Set(values) => {
                let mut grouped = values
                    .iter()
                    .map(|value| Ok((skeleton_node(value)?, value)))
                    .collect::<NativeResult<Vec<_>>>()?;
                grouped.sort_by(|left, right| left.0.cmp(&right.0));
                for (skeleton, value) in grouped {
                    let marker = first_hex16(sha256(&skeleton));
                    blank_occurrences(
                        value,
                        joined_path(&field_path, &format!("set:{marker}"))?,
                        output,
                    )?;
                }
            }
            CanonicalField::Sequence(values) => {
                for (index, value) in values.iter().enumerate() {
                    blank_occurrences(
                        value,
                        joined_path(&field_path, &index.to_string())?,
                        output,
                    )?;
                }
            }
            CanonicalField::None | CanonicalField::Scalar(_, _) => {}
        }
    }
    Ok(())
}

fn skeleton_node(node: &CanonicalNode<'_>) -> NativeResult<Vec<u8>> {
    if node.tag == 3 {
        return Ok(vec![b'B']);
    }
    if !node.contains_anonymous {
        let mut result = Vec::new();
        result.push(b'C');
        append_frame(&mut result, node.original)?;
        return Ok(result);
    }
    let mut result = Vec::new();
    result.push(b'N');
    append_varint(&mut result, node.tag)?;
    for field in &node.fields {
        let member = skeleton_field(field)?;
        append_frame(&mut result, &member)?;
    }
    Ok(result)
}

fn skeleton_field(field: &CanonicalField<'_>) -> NativeResult<Vec<u8>> {
    match field {
        CanonicalField::None => Ok(vec![b'0']),
        CanonicalField::Node(value) => skeleton_node(value),
        CanonicalField::Scalar(4, value) => {
            let mut result = Vec::new();
            result.push(b'I');
            append_bytes(&mut result, value)?;
            Ok(result)
        }
        CanonicalField::Scalar(2 | 5, value) => {
            let mut result = Vec::new();
            result.push(b'T');
            append_frame(&mut result, value)?;
            Ok(result)
        }
        CanonicalField::Scalar(_, _) => Err(NativeError::protocol(
            "native blank skeleton contains an unsupported scalar field",
        )),
        CanonicalField::Set(values) => {
            let mut members = values
                .iter()
                .map(skeleton_node)
                .collect::<NativeResult<Vec<_>>>()?;
            members.sort_unstable();
            let mut result = Vec::new();
            result.push(b'S');
            append_varint(
                &mut result,
                usize_u64(members.len(), "set size exceeds u64")?,
            )?;
            for member in members {
                append_frame(&mut result, &member)?;
            }
            Ok(result)
        }
        CanonicalField::Sequence(values) => {
            let mut result = Vec::new();
            result.push(b'Q');
            append_varint(
                &mut result,
                usize_u64(values.len(), "sequence size exceeds u64")?,
            )?;
            for value in values {
                append_frame(&mut result, &skeleton_node(value)?)?;
            }
            Ok(result)
        }
    }
}

fn collect_labels(node: &CanonicalNode<'_>, labels: &mut BTreeSet<String>) -> NativeResult<()> {
    if node.tag == 3 {
        if let Some(label) = provisional_label(anonymous_identity(node)?)? {
            labels.insert(label.to_owned());
        }
        return Ok(());
    }
    for field in &node.fields {
        match field {
            CanonicalField::Node(value) => collect_labels(value, labels)?,
            CanonicalField::Set(values) | CanonicalField::Sequence(values) => {
                for value in values {
                    collect_labels(value, labels)?;
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
) -> NativeResult<[Vec<Vec<u8>>; 3]>
where
    F: Fn(Identity<'_>) -> NativeResult<Option<OwnedIdentity>>,
{
    let mut result = [Vec::new(), Vec::new(), Vec::new()];
    for (target, source) in result.iter_mut().zip(parsed) {
        target
            .try_reserve_exact(source.len())
            .map_err(|_| NativeError::limit("native scoped row allocation failed"))?;
        for node in source {
            target.push(encode_replaced(node, &replacement)?);
        }
    }
    Ok(result)
}

fn encode_replaced<F>(node: &CanonicalNode<'_>, replacement: &F) -> NativeResult<Vec<u8>>
where
    F: Fn(Identity<'_>) -> NativeResult<Option<OwnedIdentity>>,
{
    if node.tag == 3 {
        let identity = anonymous_identity(node)?;
        return match replacement(identity)? {
            Some(value) => encode_anonymous(&value.scope, &value.key),
            None => Ok(node.original.to_vec()),
        };
    }
    if !node.contains_anonymous {
        return Ok(node.original.to_vec());
    }
    let mut output = Vec::new();
    append_varint(&mut output, node.tag)?;
    for field in &node.fields {
        match field {
            CanonicalField::None => output.push(0),
            CanonicalField::Node(value) => {
                output.push(1);
                append_frame(&mut output, &encode_replaced(value, replacement)?)?;
            }
            CanonicalField::Scalar(marker, value) => {
                output.push(*marker);
                if *marker == 4 {
                    append_bytes(&mut output, value)?;
                } else {
                    append_frame(&mut output, value)?;
                }
            }
            CanonicalField::Set(values) => {
                output.push(6);
                let mut members = values
                    .iter()
                    .map(|value| encode_replaced(value, replacement))
                    .collect::<NativeResult<Vec<_>>>()?;
                members.sort_unstable();
                members.dedup();
                append_varint(
                    &mut output,
                    usize_u64(members.len(), "set size exceeds u64")?,
                )?;
                for member in members {
                    append_frame(&mut output, &member)?;
                }
            }
            CanonicalField::Sequence(values) => {
                output.push(7);
                append_varint(
                    &mut output,
                    usize_u64(values.len(), "sequence size exceeds u64")?,
                )?;
                for value in values {
                    output.push(1);
                    append_frame(&mut output, &encode_replaced(value, replacement)?)?;
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
) -> NativeResult<CanonicalNode<'a>> {
    let mut terms = 0_u64;
    let (node, consumed) = parse_node(row, 0, row.len(), 0, &mut terms, limits, cancellation)?;
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
) -> NativeResult<(CanonicalNode<'a>, usize)> {
    cancellation.checkpoint()?;
    *terms = terms
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("native anonymous term count overflow"))?;
    if *terms > limits.max_terms || depth > limits.max_nesting_depth.min(1024) {
        return Err(NativeError::limit(
            "native anonymous canonical scan exceeds model limits",
        ));
    }
    let (tag, mut offset) = read_varint(data, start, bound)?;
    let field_count = canonical_field_count(
        u16::try_from(tag).map_err(|_| NativeError::protocol("canonical tag exceeds u16"))?,
    )
    .ok_or_else(|| NativeError::protocol("native anonymous canonical tag is unknown"))?;
    let mut fields = Vec::new();
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
                )?;
                if consumed != frame_end {
                    return Err(NativeError::protocol(
                        "native anonymous child frame is invalid",
                    ));
                }
                contains_anonymous |= child.contains_anonymous;
                offset = frame_end;
                CanonicalField::Node(Box::new(child))
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

fn identities_for_order(
    count: usize,
    order: &[usize],
    scope: [u8; 32],
    graph_digest: [u8; 32],
) -> NativeResult<Vec<OwnedIdentity>> {
    let mut indexes = vec![0_usize; count];
    for (index, label) in order.iter().copied().enumerate() {
        *indexes
            .get_mut(label)
            .ok_or_else(|| NativeError::protocol("native blank binding order is invalid"))? = index;
    }
    indexes
        .into_iter()
        .map(|index| {
            let mut hasher = Sha256::new();
            hasher.update(ANONYMOUS_KEY_DOMAIN);
            hasher.update(&scope);
            hasher.update(&graph_digest);
            let mut encoded = Vec::new();
            append_varint(
                &mut encoded,
                usize_u64(index, "native blank canonical index exceeds u64")?,
            )?;
            hasher.update(&encoded);
            Ok(OwnedIdentity {
                scope,
                key: hasher.finish().to_vec(),
            })
        })
        .collect()
}

fn rescope_identity(identity: Identity<'_>, scope: [u8; 32]) -> OwnedIdentity {
    let mut hasher = Sha256::new();
    hasher.update(ANONYMOUS_KEY_DOMAIN);
    hasher.update(&scope);
    hasher.update(identity.scope);
    hasher.update(identity.key);
    OwnedIdentity {
        scope,
        key: hasher.finish().to_vec(),
    }
}

fn ontology_key(ontology_iri: Option<&str>, version_iri: Option<&str>) -> NativeResult<Vec<u8>> {
    let Some(ontology_iri) = ontology_iri else {
        if version_iri.is_some() {
            return Err(NativeError::protocol(
                "native anonymous scope has a version IRI without an ontology IRI",
            ));
        }
        return Ok(b"anonymous-ontology".to_vec());
    };
    let mut result = iri(ontology_iri.to_owned())?.into_bytes();
    if let Some(version_iri) = version_iri {
        append_bytes(&mut result, iri(version_iri.to_owned())?.as_bytes())?;
    }
    Ok(result)
}

fn framed_digest(domain: &[u8], first: &[u8], second: &[u8]) -> NativeResult<[u8; 32]> {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    for value in [first, second] {
        let mut frame = Vec::new();
        append_frame(&mut frame, value)?;
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
    hasher.update(&[1]); // encode_varint(model schema=1)
    hasher.update(row);
    hasher.finish()
}

fn encode_anonymous(scope: &[u8; 32], key: &[u8]) -> NativeResult<Vec<u8>> {
    let mut result = Vec::new();
    append_varint(&mut result, 3)?;
    result.push(3);
    append_frame(&mut result, scope)?;
    result.push(3);
    append_frame(&mut result, key)?;
    Ok(result)
}

fn canonicalize_collections(rows: &mut [Vec<Vec<u8>>; 3]) {
    for values in rows {
        values.sort_unstable();
        values.dedup();
    }
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

fn joined_path(prefix: &str, suffix: &str) -> NativeResult<String> {
    let mut result = String::new();
    result
        .try_reserve(prefix.len().saturating_add(suffix.len()).saturating_add(1))
        .map_err(|_| NativeError::limit("native blank path allocation failed"))?;
    result.push_str(prefix);
    result.push('/');
    result.push_str(suffix);
    Ok(result)
}

fn first_hex16(digest: [u8; 32]) -> String {
    use std::fmt::Write;
    digest[..8]
        .iter()
        .fold(String::with_capacity(16), |mut output, byte| {
            write!(output, "{byte:02x}").expect("writing to String cannot fail");
            output
        })
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

fn append_frame(output: &mut Vec<u8>, value: &[u8]) -> NativeResult<()> {
    append_varint(
        output,
        usize_u64(value.len(), "native frame length exceeds u64")?,
    )?;
    append_bytes(output, value)
}

fn append_varint(output: &mut Vec<u8>, mut value: u64) -> NativeResult<()> {
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
            return append_bytes(output, &bytes[..length]);
        }
    }
}

fn append_bytes(output: &mut Vec<u8>, value: &[u8]) -> NativeResult<()> {
    output
        .try_reserve(value.len())
        .map_err(|_| NativeError::limit("native anonymous byte allocation failed"))?;
    output.extend_from_slice(value);
    Ok(())
}

fn enforce_work(work: u64, limits: &Limits) -> NativeResult<()> {
    if work > limits.max_canonical_work {
        return Err(NativeError::limit(
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
    use crate::canonical::{anonymous, entity, Field, Node};

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
        let result = scope_rdfxml_anonymous_rows_v2(
            Some("urn:o"),
            None,
            &[],
            [&[], &[axiom], &[]],
            &Limits::default(),
            &Cancellation::with_duration(None),
        )
        .unwrap();
        assert_eq!(result.raw[1].len(), 1);
        assert_eq!(result.effective[1].len(), 1);
        assert_ne!(result.raw[1], result.effective[1]);
        assert_eq!(result.effective_occurrence_digests.len(), 1);
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
        let result = scope_rdfxml_anonymous_rows_v2(
            None,
            None,
            &[],
            [&[], &[axiom], &[]],
            &Limits::default(),
            &Cancellation::with_duration(None),
        )
        .expect("wide integer anonymous scope");
        let mut budget = ScanBudget::from_limits(&Limits::default());
        assert_eq!(
            scan_canonical(&result.raw[1][0], &mut budget).expect("scoped row"),
            crate::model::Category::Axiom,
        );
    }
}
