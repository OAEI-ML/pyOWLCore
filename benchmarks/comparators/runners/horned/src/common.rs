//! Independent Horned-OWL to pyowl-core common-contract adapter.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use horned_owl::model::*;
use horned_owl::ontology::iri_mapped::RcIRIMappedOntology;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::canonical::{self as c, Field};
use crate::{hex_digest, Format, RunnerError, ValidatedRequest};

const COMMON_CONTRACT_SCHEMA: &str = "pyowl-core/comparator-common-contract/v1";
const RECORD_INVENTORY_DOMAIN: &[u8] = b"pyowl-core:comparator-record-inventory:v1\0";
const RDF_PLAIN_LITERAL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
const XSD_STRING: &str = "http://www.w3.org/2001/XMLSchema#string";
const PROVISIONAL_SCOPE: [u8; 32] = [
    0x0e, 0xf6, 0xe7, 0x99, 0x0d, 0x6e, 0x56, 0xdc, 0x7c, 0x2f, 0x06, 0xfc, 0xe3, 0xbf, 0xe1, 0x7c,
    0xd3, 0x7f, 0x89, 0x58, 0x2a, 0xfd, 0x4d, 0x37, 0xb9, 0xdf, 0xd6, 0x6a, 0x83, 0xce, 0x26, 0x7c,
];
const LEXICAL_KEY: &[u8] = b"pyowl-core:parser-blank-label:v1\0";

struct MappingContext {
    signature: BTreeSet<Vec<u8>>,
    simple_literal_datatype: &'static str,
}

#[derive(Debug)]
struct MappedAxiom {
    value: Vec<u8>,
    logical: Option<Vec<u8>>,
}

#[derive(Debug)]
struct MappedDocument {
    ontology_iri: Option<String>,
    version_iri: Option<String>,
    imports: Vec<Vec<u8>>,
    annotations: Vec<Vec<u8>>,
    axioms: Vec<MappedAxiom>,
    extensions: Vec<(Vec<u8>, Vec<u8>)>,
    signature: Vec<Vec<u8>>,
}

pub(crate) struct CommonContractBuild {
    pub(crate) contract: Value,
    pub(crate) validation_ns: u64,
}

#[derive(Clone, Debug)]
struct BlankArc {
    source: String,
    role: String,
    target: Option<String>,
    payload: Vec<u8>,
}

fn constructor_schema(tag: u64) -> Option<(&'static str, &'static [&'static str])> {
    Some(match tag {
        1 => ("IRI", &["value"]),
        2 => ("Entity", &["kind", "iri"]),
        3 => ("AnonymousIndividual", &["document_scope", "local_key"]),
        4 => ("Literal", &["lexical_form", "datatype", "language"]),
        5 => ("Annotation", &["property", "value", "annotations"]),
        10 => ("ObjectInverseOf", &["property"]),
        11 => ("ObjectPropertyChain", &["properties"]),
        20 => ("FacetRestriction", &["facet", "value"]),
        21 | 22 => (
            if tag == 21 {
                "DataIntersectionOf"
            } else {
                "DataUnionOf"
            },
            &["operands"],
        ),
        23 => ("DataComplementOf", &["operand"]),
        24 => ("DataOneOf", &["values"]),
        25 => ("DatatypeRestriction", &["datatype", "restrictions"]),
        30 | 31 => (
            if tag == 30 {
                "ObjectIntersectionOf"
            } else {
                "ObjectUnionOf"
            },
            &["operands"],
        ),
        32 => ("ObjectComplementOf", &["operand"]),
        33 => ("ObjectOneOf", &["individuals"]),
        34 | 35 => (
            if tag == 34 {
                "ObjectSomeValuesFrom"
            } else {
                "ObjectAllValuesFrom"
            },
            &["property", "filler"],
        ),
        36 => ("ObjectHasValue", &["property", "value"]),
        37 => ("ObjectHasSelf", &["property"]),
        38..=40 => (
            match tag {
                38 => "ObjectMinCardinality",
                39 => "ObjectMaxCardinality",
                _ => "ObjectExactCardinality",
            },
            &["cardinality", "property", "filler"],
        ),
        41 | 42 => (
            if tag == 41 {
                "DataSomeValuesFrom"
            } else {
                "DataAllValuesFrom"
            },
            &["properties", "filler"],
        ),
        43 => ("DataHasValue", &["property", "value"]),
        44..=46 => (
            match tag {
                44 => "DataMinCardinality",
                45 => "DataMaxCardinality",
                _ => "DataExactCardinality",
            },
            &["cardinality", "property", "filler"],
        ),
        60 => ("Declaration", &["entity", "annotations"]),
        61 => ("SubClassOf", &["sub_class", "super_class", "annotations"]),
        62 | 63 => (
            if tag == 62 {
                "EquivalentClasses"
            } else {
                "DisjointClasses"
            },
            &["expressions", "annotations"],
        ),
        64 => (
            "DisjointUnion",
            &["defined_class", "expressions", "annotations"],
        ),
        70 => (
            "SubObjectPropertyOf",
            &["sub_property", "super_property", "annotations"],
        ),
        71 | 72 => (
            if tag == 71 {
                "EquivalentObjectProperties"
            } else {
                "DisjointObjectProperties"
            },
            &["properties", "annotations"],
        ),
        73 => (
            "InverseObjectProperties",
            &["first", "second", "annotations"],
        ),
        74 => (
            "ObjectPropertyDomain",
            &["property", "domain", "annotations"],
        ),
        75 => ("ObjectPropertyRange", &["property", "range", "annotations"]),
        76..=82 => (
            match tag {
                76 => "FunctionalObjectProperty",
                77 => "InverseFunctionalObjectProperty",
                78 => "ReflexiveObjectProperty",
                79 => "IrreflexiveObjectProperty",
                80 => "SymmetricObjectProperty",
                81 => "AsymmetricObjectProperty",
                _ => "TransitiveObjectProperty",
            },
            &["property", "annotations"],
        ),
        90 => (
            "SubDataPropertyOf",
            &["sub_property", "super_property", "annotations"],
        ),
        91 | 92 => (
            if tag == 91 {
                "EquivalentDataProperties"
            } else {
                "DisjointDataProperties"
            },
            &["properties", "annotations"],
        ),
        93 => ("DataPropertyDomain", &["property", "domain", "annotations"]),
        94 => ("DataPropertyRange", &["property", "range", "annotations"]),
        95 => ("FunctionalDataProperty", &["property", "annotations"]),
        100 => (
            "DatatypeDefinition",
            &["datatype", "data_range", "annotations"],
        ),
        101 => (
            "HasKey",
            &[
                "class_expression",
                "object_properties",
                "data_properties",
                "annotations",
            ],
        ),
        110 | 111 => (
            if tag == 110 {
                "SameIndividual"
            } else {
                "DifferentIndividuals"
            },
            &["individuals", "annotations"],
        ),
        112 => (
            "ClassAssertion",
            &["class_expression", "individual", "annotations"],
        ),
        113 | 114 => (
            if tag == 113 {
                "ObjectPropertyAssertion"
            } else {
                "NegativeObjectPropertyAssertion"
            },
            &["property", "source", "target", "annotations"],
        ),
        115 | 116 => (
            if tag == 115 {
                "DataPropertyAssertion"
            } else {
                "NegativeDataPropertyAssertion"
            },
            &["property", "source", "value", "annotations"],
        ),
        120 => (
            "AnnotationAssertion",
            &["property", "subject", "value", "annotations"],
        ),
        121 => (
            "SubAnnotationPropertyOf",
            &["sub_property", "super_property", "annotations"],
        ),
        122 => (
            "AnnotationPropertyDomain",
            &["property", "domain", "annotations"],
        ),
        123 => (
            "AnnotationPropertyRange",
            &["property", "range", "annotations"],
        ),
        140 => ("Variable", &["iri"]),
        141 => ("ClassAtom", &["predicate", "argument"]),
        142 => ("DataRangeAtom", &["predicate", "argument"]),
        143 => ("ObjectPropertyAtom", &["predicate", "source", "target"]),
        144 => ("DataPropertyAtom", &["predicate", "source", "target"]),
        145 => ("BuiltInAtom", &["predicate", "arguments"]),
        146 | 147 => (
            if tag == 146 {
                "SameIndividualAtom"
            } else {
                "DifferentIndividualsAtom"
            },
            &["first", "second"],
        ),
        148 => ("SWRLRule", &["body", "head", "annotations"]),
        _ => return None,
    })
}

fn provisional_label(node: &c::ParsedNode) -> Result<Option<String>, RunnerError> {
    if node.tag != c::ANONYMOUS_INDIVIDUAL {
        return Ok(None);
    }
    let [c::ParsedField::Bytes(scope), c::ParsedField::Bytes(local_key)] = node.fields.as_slice()
    else {
        return Err(RunnerError::new(
            "anonymous individual fields differ from model schema one",
        ));
    };
    if scope.as_slice() != PROVISIONAL_SCOPE || !local_key.starts_with(LEXICAL_KEY) {
        return Ok(None);
    }
    let (length, offset) = decode_varint(local_key, LEXICAL_KEY.len())
        .ok_or_else(|| RunnerError::new("provisional anonymous key is malformed"))?;
    let end = offset
        .checked_add(
            usize::try_from(length)
                .map_err(|_| RunnerError::new("anonymous label length exceeds usize"))?,
        )
        .ok_or_else(|| RunnerError::new("anonymous label length overflow"))?;
    if end != local_key.len() {
        return Err(RunnerError::new(
            "provisional anonymous key contains trailing bytes",
        ));
    }
    Ok(Some(
        std::str::from_utf8(&local_key[offset..end])
            .map_err(|_| RunnerError::new("Horned anonymous label is not UTF-8"))?
            .to_owned(),
    ))
}

fn skeleton_node(node: &c::ParsedNode) -> Result<Vec<u8>, RunnerError> {
    if node.tag == c::ANONYMOUS_INDIVIDUAL {
        return Ok(b"B".to_vec());
    }
    if !node.contains_tag(c::ANONYMOUS_INDIVIDUAL) {
        let encoded = node.encode()?;
        let mut output = b"C".to_vec();
        output.extend(c::frame(&encoded)?);
        return Ok(output);
    }
    let (_name, fields) = constructor_schema(node.tag)
        .ok_or_else(|| RunnerError::new("blank skeleton encountered an unknown model tag"))?;
    if node.fields.len() != fields.len() {
        return Err(RunnerError::new(
            "blank skeleton field count differs from model schema one",
        ));
    }
    let mut output = b"N".to_vec();
    output.extend(c::encode_varint(node.tag));
    for field in &node.fields {
        output.extend(c::frame(&skeleton_field(field)?)?);
    }
    Ok(output)
}

fn skeleton_field(field: &c::ParsedField) -> Result<Vec<u8>, RunnerError> {
    match field {
        c::ParsedField::None => Ok(b"0".to_vec()),
        c::ParsedField::Node(value) => skeleton_node(value),
        c::ParsedField::Text(value) | c::ParsedField::Enum(value) => {
            let mut output = b"T".to_vec();
            output.extend(c::frame(value.as_bytes())?);
            Ok(output)
        }
        c::ParsedField::Integer(value) => {
            let mut output = b"I".to_vec();
            output.extend(c::encode_varint(*value));
            Ok(output)
        }
        c::ParsedField::Set(values) => {
            let mut members = values
                .iter()
                .map(skeleton_node)
                .collect::<Result<Vec<_>, _>>()?;
            members.sort();
            let mut output = b"S".to_vec();
            append_collection(&mut output, &members)?;
            Ok(output)
        }
        c::ParsedField::Sequence(values) => {
            let members = values
                .iter()
                .map(skeleton_node)
                .collect::<Result<Vec<_>, _>>()?;
            let mut output = b"Q".to_vec();
            append_collection(&mut output, &members)?;
            Ok(output)
        }
        c::ParsedField::Bytes(_) => Err(RunnerError::new(
            "blank skeleton encountered bytes outside an anonymous individual",
        )),
    }
}

fn blank_occurrences(
    node: &c::ParsedNode,
    path: &[String],
    output: &mut Vec<(String, Vec<String>)>,
) -> Result<(), RunnerError> {
    if let Some(label) = provisional_label(node)? {
        output.push((label, path.to_vec()));
        return Ok(());
    }
    let (_name, fields) = constructor_schema(node.tag)
        .ok_or_else(|| RunnerError::new("blank occurrence encountered an unknown model tag"))?;
    if node.fields.len() != fields.len() {
        return Err(RunnerError::new(
            "blank occurrence field count differs from model schema one",
        ));
    }
    for (field, name) in node.fields.iter().zip(fields.iter()) {
        let mut child_path = path.to_vec();
        child_path.push((*name).to_owned());
        blank_occurrences_field(field, &child_path, output)?;
    }
    Ok(())
}

fn blank_occurrences_field(
    field: &c::ParsedField,
    path: &[String],
    output: &mut Vec<(String, Vec<String>)>,
) -> Result<(), RunnerError> {
    match field {
        c::ParsedField::Node(value) => blank_occurrences(value, path, output),
        c::ParsedField::Set(values) => {
            let mut grouped = values
                .iter()
                .map(|value| Ok((skeleton_node(value)?, value)))
                .collect::<Result<Vec<_>, RunnerError>>()?;
            grouped.sort_by(|left, right| left.0.cmp(&right.0));
            for (skeleton, value) in grouped {
                let marker = &hex_digest(&digest(&skeleton))[..16];
                let mut child_path = path.to_vec();
                child_path.push(format!("set:{marker}"));
                blank_occurrences(value, &child_path, output)?;
            }
            Ok(())
        }
        c::ParsedField::Sequence(values) => {
            for (index, value) in values.iter().enumerate() {
                let mut child_path = path.to_vec();
                child_path.push(index.to_string());
                blank_occurrences(value, &child_path, output)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn blank_arcs(
    roots: &[Vec<u8>],
    max_terms: u64,
) -> Result<(Vec<BlankArc>, BTreeSet<String>), RunnerError> {
    let mut arcs = Vec::new();
    let mut labels = BTreeSet::new();
    for root in roots {
        let node = c::parse_node(root)?;
        if !node.contains_tag(c::ANONYMOUS_INDIVIDUAL) {
            continue;
        }
        let (name, _fields) = constructor_schema(node.tag)
            .ok_or_else(|| RunnerError::new("blank root has an unknown model tag"))?;
        let skeleton = skeleton_node(&node)?;
        let mut occurrences = Vec::new();
        blank_occurrences(&node, &[name.to_owned()], &mut occurrences)?;
        for (label, path) in &occurrences {
            labels.insert(label.clone());
            arcs.push(BlankArc {
                source: label.clone(),
                role: path.join("/"),
                target: None,
                payload: skeleton.clone(),
            });
            enforce_blank_terms(labels.len(), arcs.len(), max_terms)?;
        }
        for index in 0..occurrences.len() {
            for target_index in index + 1..occurrences.len() {
                let (source, source_path) = &occurrences[index];
                let (target, target_path) = &occurrences[target_index];
                arcs.push(BlankArc {
                    source: source.clone(),
                    role: format!("{}->{}", source_path.join("/"), target_path.join("/")),
                    target: Some(target.clone()),
                    payload: skeleton.clone(),
                });
                enforce_blank_terms(labels.len(), arcs.len(), max_terms)?;
            }
        }
    }
    Ok((arcs, labels))
}

fn enforce_blank_terms(
    label_count: usize,
    arc_count: usize,
    maximum: u64,
) -> Result<(), RunnerError> {
    let observed = u64::try_from(label_count)
        .ok()
        .and_then(|labels| {
            u64::try_from(arc_count)
                .ok()
                .and_then(|arcs| labels.checked_add(arcs))
        })
        .ok_or_else(|| RunnerError::new("blank canonical term count exceeds u64"))?;
    if observed > maximum {
        return Err(RunnerError::new("blank canonicalization exceeds max_terms"));
    }
    Ok(())
}

fn arc_signature(
    label: &str,
    arc: &BlankArc,
    colors: Option<&BTreeMap<String, Vec<u8>>>,
) -> Result<Option<Vec<u8>>, RunnerError> {
    let (direction, neighbor) = if arc.source == label {
        let neighbor = match &arc.target {
            None => b"N".to_vec(),
            Some(target) if target == label => b"L".to_vec(),
            Some(target) => match colors {
                None => b"B".to_vec(),
                Some(colors) => {
                    let mut value = b"C".to_vec();
                    value.extend(
                        colors
                            .get(target)
                            .ok_or_else(|| RunnerError::new("blank color target is missing"))?,
                    );
                    value
                }
            },
        };
        (b'S', neighbor)
    } else if arc.target.as_deref() == Some(label) {
        let neighbor = match colors {
            None => b"B".to_vec(),
            Some(colors) => {
                let mut value = b"C".to_vec();
                value.extend(
                    colors
                        .get(&arc.source)
                        .ok_or_else(|| RunnerError::new("blank color source is missing"))?,
                );
                value
            }
        };
        (b'T', neighbor)
    } else {
        return Ok(None);
    };
    let mut output = vec![direction];
    output.extend(c::frame(arc.role.as_bytes())?);
    output.extend(neighbor);
    output.extend(c::frame(&arc.payload)?);
    Ok(Some(output))
}

fn blank_colors(
    labels: &BTreeSet<String>,
    arcs: &[BlankArc],
    previous: Option<&BTreeMap<String, Vec<u8>>>,
) -> Result<BTreeMap<String, Vec<u8>>, RunnerError> {
    let mut neighborhoods = labels
        .iter()
        .map(|label| (label.clone(), Vec::new()))
        .collect::<BTreeMap<_, _>>();
    for arc in arcs {
        let source = arc_signature(&arc.source, arc, previous)?
            .ok_or_else(|| RunnerError::new("blank source arc signature is missing"))?;
        neighborhoods
            .get_mut(&arc.source)
            .ok_or_else(|| RunnerError::new("blank graph source label is missing"))?
            .push(source);
        if let Some(target) = &arc.target {
            if target != &arc.source {
                let target_signature = arc_signature(target, arc, previous)?
                    .ok_or_else(|| RunnerError::new("blank target arc signature is missing"))?;
                neighborhoods
                    .get_mut(target)
                    .ok_or_else(|| RunnerError::new("blank graph target label is missing"))?
                    .push(target_signature);
            }
        }
    }
    let mut output = BTreeMap::new();
    for (label, mut signatures) in neighborhoods {
        signatures.sort();
        let mut preimage = b"pyowl-core:blank-color:v1\0".to_vec();
        if let Some(previous) = previous {
            preimage.extend(c::frame(previous.get(&label).ok_or_else(|| {
                RunnerError::new("blank refinement color is missing")
            })?)?);
        }
        for signature in signatures {
            preimage.extend(c::frame(&signature)?);
        }
        output.insert(label, digest(&preimage));
    }
    Ok(output)
}

fn same_partition(
    labels: &BTreeSet<String>,
    first: &BTreeMap<String, Vec<u8>>,
    second: &BTreeMap<String, Vec<u8>>,
) -> bool {
    let mut forward = BTreeMap::<Vec<u8>, Vec<u8>>::new();
    let mut reverse = BTreeMap::<Vec<u8>, Vec<u8>>::new();
    for label in labels {
        let first_color = &first[label];
        let second_color = &second[label];
        if forward
            .entry(first_color.clone())
            .or_insert_with(|| second_color.clone())
            != second_color
            || reverse
                .entry(second_color.clone())
                .or_insert_with(|| first_color.clone())
                != first_color
        {
            return false;
        }
    }
    true
}

fn serialize_blank_graph(order: &[String], arcs: &[BlankArc]) -> Result<Vec<u8>, RunnerError> {
    let indexes = order
        .iter()
        .enumerate()
        .map(|(index, label)| (label.as_str(), index))
        .collect::<BTreeMap<_, _>>();
    let mut encoded_arcs = BTreeSet::new();
    for arc in arcs {
        let mut encoded = c::encode_varint(
            u64::try_from(
                *indexes
                    .get(arc.source.as_str())
                    .ok_or_else(|| RunnerError::new("blank graph source index is missing"))?,
            )
            .map_err(|_| RunnerError::new("blank graph index exceeds u64"))?,
        );
        encoded.extend(c::frame(arc.role.as_bytes())?);
        match &arc.target {
            None => encoded.push(0),
            Some(target) => {
                encoded.push(1);
                encoded.extend(c::encode_varint(
                    u64::try_from(
                        *indexes.get(target.as_str()).ok_or_else(|| {
                            RunnerError::new("blank graph target index is missing")
                        })?,
                    )
                    .map_err(|_| RunnerError::new("blank graph index exceeds u64"))?,
                ));
            }
        }
        encoded.extend(c::frame(&arc.payload)?);
        encoded_arcs.insert(encoded);
    }
    let mut graph = b"pyowl-core:blank-graph:v1\0".to_vec();
    graph.extend(c::encode_varint(
        u64::try_from(order.len()).map_err(|_| RunnerError::new("blank count exceeds u64"))?,
    ));
    graph.extend(c::encode_varint(
        u64::try_from(encoded_arcs.len())
            .map_err(|_| RunnerError::new("blank arc count exceeds u64"))?,
    ));
    for arc in encoded_arcs {
        graph.extend(c::frame(&arc)?);
    }
    Ok(graph)
}

fn visit_permutations(
    values: &[String],
    used: &mut [bool],
    current: &mut Vec<String>,
    visit: &mut impl FnMut(&[String]) -> Result<(), RunnerError>,
) -> Result<(), RunnerError> {
    if current.len() == values.len() {
        return visit(current);
    }
    for index in 0..values.len() {
        if used[index] {
            continue;
        }
        used[index] = true;
        current.push(values[index].clone());
        visit_permutations(values, used, current, visit)?;
        current.pop();
        used[index] = false;
    }
    Ok(())
}

fn visit_candidate_orders(
    partitions: &[Vec<String>],
    partition_index: usize,
    prefix: &mut Vec<String>,
    arcs: &[BlankArc],
    best: &mut Option<(Vec<u8>, Vec<String>)>,
) -> Result<(), RunnerError> {
    if partition_index == partitions.len() {
        let graph = serialize_blank_graph(prefix, arcs)?;
        if best.as_ref().is_none_or(|(current, _)| graph < *current) {
            *best = Some((graph, prefix.clone()));
        }
        return Ok(());
    }
    let partition = &partitions[partition_index];
    let mut used = vec![false; partition.len()];
    let mut permutation = Vec::with_capacity(partition.len());
    visit_permutations(partition, &mut used, &mut permutation, &mut |choice| {
        let retained = prefix.len();
        prefix.extend(choice.iter().cloned());
        let result = visit_candidate_orders(partitions, partition_index + 1, prefix, arcs, best);
        prefix.truncate(retained);
        result
    })
}

fn enforce_blank_work(observed: u64, maximum: u64) -> Result<(), RunnerError> {
    if observed > maximum {
        return Err(RunnerError::new(
            "blank canonicalization exceeds max_canonical_work",
        ));
    }
    Ok(())
}

fn alpha_order(
    labels: &BTreeSet<String>,
    arcs: &[BlankArc],
    max_canonical_work: u64,
) -> Result<(Vec<String>, Vec<u8>), RunnerError> {
    let label_count = u64::try_from(labels.len())
        .map_err(|_| RunnerError::new("blank label count exceeds u64"))?;
    let arc_count =
        u64::try_from(arcs.len()).map_err(|_| RunnerError::new("blank arc count exceeds u64"))?;
    let mut work = label_count
        .checked_add(
            arc_count
                .checked_mul(2)
                .ok_or_else(|| RunnerError::new("blank canonical work exceeds u64"))?,
        )
        .ok_or_else(|| RunnerError::new("blank canonical work exceeds u64"))?;
    enforce_blank_work(work, max_canonical_work)?;
    let mut colors = blank_colors(labels, arcs, None)?;
    let mut converged = false;
    for _round in 0..=labels.len() + 1 {
        let refined = blank_colors(labels, arcs, Some(&colors))?;
        let round_work = label_count
            .checked_mul(2)
            .and_then(|labels| {
                arc_count
                    .checked_mul(2)
                    .and_then(|arcs| labels.checked_add(arcs))
            })
            .ok_or_else(|| RunnerError::new("blank canonical work exceeds u64"))?;
        work = work
            .checked_add(round_work)
            .ok_or_else(|| RunnerError::new("blank canonical work exceeds u64"))?;
        enforce_blank_work(work, max_canonical_work)?;
        if same_partition(labels, &colors, &refined) {
            colors = refined;
            converged = true;
            break;
        }
        colors = refined;
    }
    if !converged {
        return Err(RunnerError::new(
            "blank-node partition refinement did not converge",
        ));
    }
    let mut grouped = BTreeMap::<Vec<u8>, Vec<String>>::new();
    for label in labels {
        grouped
            .entry(colors[label].clone())
            .or_default()
            .push(label.clone());
    }
    let partitions = grouped.into_values().collect::<Vec<_>>();
    let mut permutation_count = 1_u64;
    let remaining = max_canonical_work.saturating_sub(work);
    for partition in &partitions {
        for factor in 2..=partition.len() {
            let factor = u64::try_from(factor)
                .map_err(|_| RunnerError::new("blank permutation factor exceeds u64"))?;
            if permutation_count > remaining / factor {
                return Err(RunnerError::new(
                    "blank canonicalization exceeds max_canonical_work",
                ));
            }
            permutation_count *= factor;
        }
    }
    let candidate_unit_work = label_count.saturating_add(arc_count).max(1);
    let total_work = permutation_count
        .checked_mul(candidate_unit_work)
        .and_then(|candidate_work| work.checked_add(candidate_work))
        .unwrap_or(u64::MAX);
    enforce_blank_work(total_work, max_canonical_work)?;
    let mut best: Option<(Vec<u8>, Vec<String>)> = None;
    visit_candidate_orders(&partitions, 0, &mut Vec::new(), arcs, &mut best)?;
    let (graph, order) = best.ok_or_else(|| RunnerError::new("blank graph has no candidate"))?;
    Ok((order, graph))
}

fn anonymous_node(scope: &[u8], local_key: Vec<u8>) -> Result<c::ParsedNode, RunnerError> {
    if scope.len() != 32 || local_key.is_empty() {
        return Err(RunnerError::new(
            "canonical anonymous identity has invalid dimensions",
        ));
    }
    Ok(c::ParsedNode {
        tag: c::ANONYMOUS_INDIVIDUAL,
        fields: vec![
            c::ParsedField::Bytes(scope.to_vec()),
            c::ParsedField::Bytes(local_key),
        ],
    })
}

fn replace_provisional(
    node: &mut c::ParsedNode,
    replacements: &BTreeMap<String, c::ParsedNode>,
) -> Result<(), RunnerError> {
    if let Some(label) = provisional_label(node)? {
        *node = replacements
            .get(&label)
            .ok_or_else(|| RunnerError::new("anonymous replacement is missing"))?
            .clone();
        return Ok(());
    }
    for field in &mut node.fields {
        replace_field(field, |value| replace_provisional(value, replacements))?;
    }
    Ok(())
}

fn replace_encoded_anonymous(
    node: &mut c::ParsedNode,
    replacements: &BTreeMap<Vec<u8>, c::ParsedNode>,
) -> Result<(), RunnerError> {
    if node.tag == c::ANONYMOUS_INDIVIDUAL {
        let encoded = node.encode()?;
        *node = replacements
            .get(&encoded)
            .ok_or_else(|| RunnerError::new("scoped anonymous replacement is missing"))?
            .clone();
        return Ok(());
    }
    for field in &mut node.fields {
        replace_field(field, |value| {
            replace_encoded_anonymous(value, replacements)
        })?;
    }
    Ok(())
}

fn replace_field(
    field: &mut c::ParsedField,
    mut replace: impl FnMut(&mut c::ParsedNode) -> Result<(), RunnerError>,
) -> Result<(), RunnerError> {
    match field {
        c::ParsedField::Node(value) => replace(value),
        c::ParsedField::Set(values) | c::ParsedField::Sequence(values) => {
            for value in values {
                replace(value)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn transform_value(
    value: &mut Vec<u8>,
    mut transform: impl FnMut(&mut c::ParsedNode) -> Result<(), RunnerError>,
) -> Result<(), RunnerError> {
    let mut parsed = c::parse_node(value)?;
    transform(&mut parsed)?;
    *value = parsed.encode()?;
    Ok(())
}

fn ontology_key(document: &MappedDocument) -> Result<Vec<u8>, RunnerError> {
    match &document.ontology_iri {
        None => Ok(b"anonymous-ontology".to_vec()),
        Some(ontology) => {
            let mut key = c::iri(ontology)?;
            if let Some(version) = &document.version_iri {
                key.extend(c::iri(version)?);
            }
            Ok(key)
        }
    }
}

fn freeze_document_anonymous(
    document: &mut MappedDocument,
    max_canonical_work: u64,
    max_terms: u64,
) -> Result<(), RunnerError> {
    let roots = document
        .annotations
        .iter()
        .cloned()
        .chain(document.axioms.iter().map(|value| value.value.clone()))
        .chain(document.extensions.iter().map(|value| value.0.clone()))
        .collect::<Vec<_>>();
    let (arcs, labels) = blank_arcs(&roots, max_terms)?;
    if labels.is_empty() {
        normalize_document_sets(document);
        return Ok(());
    }
    let (order, canonical_graph) = alpha_order(&labels, &arcs, max_canonical_work)?;
    let key = ontology_key(document)?;
    let mut scope_preimage = b"pyowl-core:document-scope:v1\0".to_vec();
    scope_preimage.extend(c::frame(&key)?);
    scope_preimage.extend(c::frame(&canonical_graph)?);
    let scope = digest(&scope_preimage);
    let graph_digest = digest(&canonical_graph);
    let mut replacements = BTreeMap::new();
    for (index, label) in order.into_iter().enumerate() {
        let mut key_preimage = b"pyowl-core:anonymous-key:v1\0".to_vec();
        key_preimage.extend_from_slice(&scope);
        key_preimage.extend_from_slice(&graph_digest);
        key_preimage
            .extend(c::encode_varint(u64::try_from(index).map_err(|_| {
                RunnerError::new("anonymous canonical index exceeds u64")
            })?));
        replacements.insert(label, anonymous_node(&scope, digest(&key_preimage))?);
    }
    transform_document(document, |node| replace_provisional(node, &replacements))?;
    normalize_document_sets(document);
    Ok(())
}

fn normalize_document_sets(document: &mut MappedDocument) {
    document.imports = c::normalize_set(std::mem::take(&mut document.imports));
    document.annotations = c::normalize_set(std::mem::take(&mut document.annotations));
    document
        .axioms
        .sort_by(|left, right| left.value.cmp(&right.value));
    document
        .axioms
        .dedup_by(|left, right| left.value == right.value);
    document
        .extensions
        .sort_by(|left, right| left.0.cmp(&right.0));
    document
        .extensions
        .dedup_by(|left, right| left.0 == right.0);
}

fn transform_document(
    document: &mut MappedDocument,
    mut transform: impl FnMut(&mut c::ParsedNode) -> Result<(), RunnerError>,
) -> Result<(), RunnerError> {
    for value in &mut document.annotations {
        transform_value(value, &mut transform)?;
    }
    for value in &mut document.axioms {
        transform_value(&mut value.value, &mut transform)?;
        if let Some(logical) = &mut value.logical {
            transform_value(logical, &mut transform)?;
        }
    }
    for (value, logical) in &mut document.extensions {
        transform_value(value, &mut transform)?;
        transform_value(logical, &mut transform)?;
    }
    Ok(())
}

fn snapshot_scope_document(
    document: &mut MappedDocument,
    document_fingerprint: &[u8],
) -> Result<(), RunnerError> {
    let roots = document
        .annotations
        .iter()
        .chain(document.axioms.iter().map(|value| &value.value))
        .chain(document.extensions.iter().map(|value| &value.0));
    let mut identities = BTreeMap::<Vec<u8>, (Vec<u8>, Vec<u8>)>::new();
    for root in roots {
        collect_anonymous_identities(&c::parse_node(root)?, &mut identities)?;
    }
    if identities.is_empty() {
        return Ok(());
    }
    let mut scope_preimage = b"pyowl-core:snapshot-document-scope:v1\0".to_vec();
    scope_preimage.extend_from_slice(document_fingerprint);
    scope_preimage.extend(c::encode_varint(0));
    let new_scope = digest(&scope_preimage);
    let mut replacements = BTreeMap::new();
    for (encoded, (old_scope, old_key)) in identities {
        let mut key_preimage = b"pyowl-core:anonymous-key:v1\0".to_vec();
        key_preimage.extend_from_slice(&new_scope);
        key_preimage.extend_from_slice(&old_scope);
        key_preimage.extend_from_slice(&old_key);
        replacements.insert(encoded, anonymous_node(&new_scope, digest(&key_preimage))?);
    }
    transform_document(document, |node| {
        replace_encoded_anonymous(node, &replacements)
    })?;
    normalize_document_sets(document);
    Ok(())
}

fn collect_anonymous_identities(
    node: &c::ParsedNode,
    output: &mut BTreeMap<Vec<u8>, (Vec<u8>, Vec<u8>)>,
) -> Result<(), RunnerError> {
    if node.tag == c::ANONYMOUS_INDIVIDUAL {
        let [c::ParsedField::Bytes(scope), c::ParsedField::Bytes(key)] = node.fields.as_slice()
        else {
            return Err(RunnerError::new("anonymous identity fields are malformed"));
        };
        output.insert(node.encode()?, (scope.clone(), key.clone()));
        return Ok(());
    }
    for field in &node.fields {
        match field {
            c::ParsedField::Node(value) => collect_anonymous_identities(value, output)?,
            c::ParsedField::Set(values) | c::ParsedField::Sequence(values) => {
                for value in values {
                    collect_anonymous_identities(value, output)?;
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn map_entity(
    context: &mut MappingContext,
    kind: &'static str,
    iri: &str,
) -> Result<Vec<u8>, RunnerError> {
    let value = c::entity(kind, iri)?;
    context.signature.insert(value.clone());
    Ok(value)
}

fn map_class(context: &mut MappingContext, value: &Class<RcStr>) -> Result<Vec<u8>, RunnerError> {
    map_entity(context, "class", value.0.as_ref())
}

fn map_datatype(
    context: &mut MappingContext,
    value: &Datatype<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    map_entity(context, "datatype", value.0.as_ref())
}

fn map_object_property(
    context: &mut MappingContext,
    value: &ObjectProperty<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    map_entity(context, "object_property", value.0.as_ref())
}

fn map_data_property(
    context: &mut MappingContext,
    value: &DataProperty<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    map_entity(context, "data_property", value.0.as_ref())
}

fn map_annotation_property(
    context: &mut MappingContext,
    value: &AnnotationProperty<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    map_entity(context, "annotation_property", value.0.as_ref())
}

fn map_named_individual(
    context: &mut MappingContext,
    value: &NamedIndividual<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    map_entity(context, "named_individual", value.0.as_ref())
}

fn map_anonymous(value: &AnonymousIndividual<RcStr>) -> Result<Vec<u8>, RunnerError> {
    let label = value.0.as_ref().as_bytes();
    if label.is_empty() {
        return Err(RunnerError::new("Horned anonymous label is empty"));
    }
    let mut local_key = LEXICAL_KEY.to_vec();
    local_key
        .extend(c::encode_varint(u64::try_from(label.len()).map_err(
            |_| RunnerError::new("Horned anonymous label exceeds u64"),
        )?));
    local_key.extend_from_slice(label);
    c::node(
        c::ANONYMOUS_INDIVIDUAL,
        [
            Field::Bytes(PROVISIONAL_SCOPE.to_vec()),
            Field::Bytes(local_key),
        ],
    )
}

fn map_individual(
    context: &mut MappingContext,
    value: &Individual<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    match value {
        Individual::Named(value) => map_named_individual(context, value),
        Individual::Anonymous(value) => map_anonymous(value),
    }
}

fn map_literal(
    context: &mut MappingContext,
    value: &Literal<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    let (lexical, datatype, language) = match value {
        Literal::Simple { literal } => (literal, context.simple_literal_datatype, None),
        Literal::Language { literal, lang } => {
            (literal, RDF_PLAIN_LITERAL, Some(lang.to_lowercase()))
        }
        Literal::Datatype {
            literal,
            datatype_iri,
        } => (literal, datatype_iri.as_ref(), None),
    };
    let datatype = map_entity(context, "datatype", datatype)?;
    c::node(
        c::LITERAL,
        [
            Field::Text(lexical.clone()),
            Field::Node(datatype),
            language.map_or(Field::None, Field::Text),
        ],
    )
}

fn map_annotation_value(
    context: &mut MappingContext,
    value: &AnnotationValue<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    match value {
        AnnotationValue::Literal(value) => map_literal(context, value),
        AnnotationValue::IRI(value) => c::iri(value.as_ref()),
        AnnotationValue::AnonymousIndividual(value) => map_anonymous(value),
    }
}

fn map_annotation(
    context: &mut MappingContext,
    value: &Annotation<RcStr>,
    nested: impl IntoIterator<Item = Vec<u8>>,
) -> Result<Vec<u8>, RunnerError> {
    c::node(
        c::ANNOTATION,
        [
            Field::Node(map_annotation_property(context, &value.ap)?),
            Field::Node(map_annotation_value(context, &value.av)?),
            Field::Set(nested.into_iter().collect()),
        ],
    )
}

fn map_annotations(
    context: &mut MappingContext,
    values: &BTreeSet<Annotation<RcStr>>,
) -> Result<Vec<Vec<u8>>, RunnerError> {
    values
        .iter()
        .map(|value| map_annotation(context, value, []))
        .collect()
}

fn map_object_property_expression(
    context: &mut MappingContext,
    value: &ObjectPropertyExpression<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    match value {
        ObjectPropertyExpression::ObjectProperty(value) => map_object_property(context, value),
        ObjectPropertyExpression::InverseObjectProperty(value) => c::node(
            c::OBJECT_INVERSE_OF,
            [Field::Node(map_object_property(context, value)?)],
        ),
    }
}

fn map_facet_restriction(
    context: &mut MappingContext,
    value: &FacetRestriction<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    c::node(
        c::FACET_RESTRICTION,
        [
            Field::Node(c::iri(value.f.as_ref())?),
            Field::Node(map_literal(context, &value.l)?),
        ],
    )
}

fn map_data_range(
    context: &mut MappingContext,
    value: &DataRange<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    match value {
        DataRange::Datatype(value) => map_datatype(context, value),
        DataRange::DataIntersectionOf(values) => {
            let mut mapped = Vec::new();
            collect_data_operands(context, values, true, &mut mapped)?;
            c::node(c::DATA_INTERSECTION_OF, [Field::Set(mapped)])
        }
        DataRange::DataUnionOf(values) => {
            let mut mapped = Vec::new();
            collect_data_operands(context, values, false, &mut mapped)?;
            c::node(c::DATA_UNION_OF, [Field::Set(mapped)])
        }
        DataRange::DataComplementOf(value) => c::node(
            c::DATA_COMPLEMENT_OF,
            [Field::Node(map_data_range(context, value)?)],
        ),
        DataRange::DataOneOf(values) => c::node(
            c::DATA_ONE_OF,
            [Field::Set(
                values
                    .iter()
                    .map(|value| map_literal(context, value))
                    .collect::<Result<_, _>>()?,
            )],
        ),
        DataRange::DatatypeRestriction(datatype, restrictions) => c::node(
            c::DATATYPE_RESTRICTION,
            [
                Field::Node(map_datatype(context, datatype)?),
                Field::Set(
                    restrictions
                        .iter()
                        .map(|value| map_facet_restriction(context, value))
                        .collect::<Result<_, _>>()?,
                ),
            ],
        ),
    }
}

fn collect_data_operands(
    context: &mut MappingContext,
    values: &[DataRange<RcStr>],
    intersection: bool,
    output: &mut Vec<Vec<u8>>,
) -> Result<(), RunnerError> {
    for value in values {
        match (intersection, value) {
            (true, DataRange::DataIntersectionOf(nested))
            | (false, DataRange::DataUnionOf(nested)) => {
                collect_data_operands(context, nested, intersection, output)?;
            }
            _ => output.push(map_data_range(context, value)?),
        }
    }
    Ok(())
}

fn map_class_expression(
    context: &mut MappingContext,
    value: &ClassExpression<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    match value {
        ClassExpression::Class(value) => map_class(context, value),
        ClassExpression::ObjectIntersectionOf(values) => {
            let mut mapped = Vec::new();
            collect_class_operands(context, values, true, &mut mapped)?;
            c::node(c::OBJECT_INTERSECTION_OF, [Field::Set(mapped)])
        }
        ClassExpression::ObjectUnionOf(values) => {
            let mut mapped = Vec::new();
            collect_class_operands(context, values, false, &mut mapped)?;
            c::node(c::OBJECT_UNION_OF, [Field::Set(mapped)])
        }
        ClassExpression::ObjectComplementOf(value) => c::node(
            c::OBJECT_COMPLEMENT_OF,
            [Field::Node(map_class_expression(context, value)?)],
        ),
        ClassExpression::ObjectOneOf(values) => c::node(
            c::OBJECT_ONE_OF,
            [Field::Set(
                values
                    .iter()
                    .map(|value| map_individual(context, value))
                    .collect::<Result<_, _>>()?,
            )],
        ),
        ClassExpression::ObjectSomeValuesFrom { ope, bce } => c::node(
            c::OBJECT_SOME_VALUES_FROM,
            [
                Field::Node(map_object_property_expression(context, ope)?),
                Field::Node(map_class_expression(context, bce)?),
            ],
        ),
        ClassExpression::ObjectAllValuesFrom { ope, bce } => c::node(
            c::OBJECT_ALL_VALUES_FROM,
            [
                Field::Node(map_object_property_expression(context, ope)?),
                Field::Node(map_class_expression(context, bce)?),
            ],
        ),
        ClassExpression::ObjectHasValue { ope, i } => c::node(
            c::OBJECT_HAS_VALUE,
            [
                Field::Node(map_object_property_expression(context, ope)?),
                Field::Node(map_individual(context, i)?),
            ],
        ),
        ClassExpression::ObjectHasSelf(value) => c::node(
            c::OBJECT_HAS_SELF,
            [Field::Node(map_object_property_expression(context, value)?)],
        ),
        ClassExpression::ObjectMinCardinality { n, ope, bce } => {
            map_object_cardinality(context, c::OBJECT_MIN_CARDINALITY, *n, ope, bce)
        }
        ClassExpression::ObjectMaxCardinality { n, ope, bce } => {
            map_object_cardinality(context, c::OBJECT_MAX_CARDINALITY, *n, ope, bce)
        }
        ClassExpression::ObjectExactCardinality { n, ope, bce } => {
            map_object_cardinality(context, c::OBJECT_EXACT_CARDINALITY, *n, ope, bce)
        }
        ClassExpression::DataSomeValuesFrom { dp, dr } => c::node(
            c::DATA_SOME_VALUES_FROM,
            [
                Field::Sequence(vec![map_data_property(context, dp)?]),
                Field::Node(map_data_range(context, dr)?),
            ],
        ),
        ClassExpression::DataAllValuesFrom { dp, dr } => c::node(
            c::DATA_ALL_VALUES_FROM,
            [
                Field::Sequence(vec![map_data_property(context, dp)?]),
                Field::Node(map_data_range(context, dr)?),
            ],
        ),
        ClassExpression::DataHasValue { dp, l } => c::node(
            c::DATA_HAS_VALUE,
            [
                Field::Node(map_data_property(context, dp)?),
                Field::Node(map_literal(context, l)?),
            ],
        ),
        ClassExpression::DataMinCardinality { n, dp, dr } => {
            map_data_cardinality(context, c::DATA_MIN_CARDINALITY, *n, dp, dr)
        }
        ClassExpression::DataMaxCardinality { n, dp, dr } => {
            map_data_cardinality(context, c::DATA_MAX_CARDINALITY, *n, dp, dr)
        }
        ClassExpression::DataExactCardinality { n, dp, dr } => {
            map_data_cardinality(context, c::DATA_EXACT_CARDINALITY, *n, dp, dr)
        }
    }
}

fn collect_class_operands(
    context: &mut MappingContext,
    values: &[ClassExpression<RcStr>],
    intersection: bool,
    output: &mut Vec<Vec<u8>>,
) -> Result<(), RunnerError> {
    for value in values {
        match (intersection, value) {
            (true, ClassExpression::ObjectIntersectionOf(nested))
            | (false, ClassExpression::ObjectUnionOf(nested)) => {
                collect_class_operands(context, nested, intersection, output)?;
            }
            _ => output.push(map_class_expression(context, value)?),
        }
    }
    Ok(())
}

fn map_object_cardinality(
    context: &mut MappingContext,
    tag: u64,
    cardinality: u32,
    property: &ObjectPropertyExpression<RcStr>,
    filler: &ClassExpression<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    c::node(
        tag,
        [
            Field::Integer(u64::from(cardinality)),
            Field::Node(map_object_property_expression(context, property)?),
            Field::Node(map_class_expression(context, filler)?),
        ],
    )
}

fn map_data_cardinality(
    context: &mut MappingContext,
    tag: u64,
    cardinality: u32,
    property: &DataProperty<RcStr>,
    filler: &DataRange<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    c::node(
        tag,
        [
            Field::Integer(u64::from(cardinality)),
            Field::Node(map_data_property(context, property)?),
            Field::Node(map_data_range(context, filler)?),
        ],
    )
}

fn map_variable(value: &Variable<RcStr>) -> Result<Vec<u8>, RunnerError> {
    c::node(c::VARIABLE, [Field::Node(c::iri(value.0.as_ref())?)])
}

fn map_iargument(
    context: &mut MappingContext,
    value: &IArgument<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    match value {
        IArgument::Individual(value) => map_individual(context, value),
        IArgument::Variable(value) => map_variable(value),
    }
}

fn map_dargument(
    context: &mut MappingContext,
    value: &DArgument<RcStr>,
) -> Result<Vec<u8>, RunnerError> {
    match value {
        DArgument::Literal(value) => map_literal(context, value),
        DArgument::Variable(value) => map_variable(value),
    }
}

fn map_atom(context: &mut MappingContext, value: &Atom<RcStr>) -> Result<Vec<u8>, RunnerError> {
    match value {
        Atom::BuiltInAtom { pred, args } => c::node(
            c::BUILT_IN_ATOM,
            [
                Field::Node(c::iri(pred.as_ref())?),
                Field::Sequence(
                    args.iter()
                        .map(|value| map_dargument(context, value))
                        .collect::<Result<_, _>>()?,
                ),
            ],
        ),
        Atom::ClassAtom { pred, arg } => c::node(
            c::CLASS_ATOM,
            [
                Field::Node(map_class_expression(context, pred)?),
                Field::Node(map_iargument(context, arg)?),
            ],
        ),
        Atom::DataPropertyAtom { pred, args } => c::node(
            c::DATA_PROPERTY_ATOM,
            [
                Field::Node(map_data_property(context, pred)?),
                Field::Node(map_dargument(context, &args.0)?),
                Field::Node(map_dargument(context, &args.1)?),
            ],
        ),
        Atom::DataRangeAtom { pred, arg } => c::node(
            c::DATA_RANGE_ATOM,
            [
                Field::Node(map_data_range(context, pred)?),
                Field::Node(map_dargument(context, arg)?),
            ],
        ),
        Atom::DifferentIndividualsAtom(first, second) => c::node(
            c::DIFFERENT_INDIVIDUALS_ATOM,
            [
                Field::Node(map_iargument(context, first)?),
                Field::Node(map_iargument(context, second)?),
            ],
        ),
        Atom::ObjectPropertyAtom { pred, args } => c::node(
            c::OBJECT_PROPERTY_ATOM,
            [
                Field::Node(map_object_property_expression(context, pred)?),
                Field::Node(map_iargument(context, &args.0)?),
                Field::Node(map_iargument(context, &args.1)?),
            ],
        ),
        Atom::SameIndividualAtom(first, second) => c::node(
            c::SAME_INDIVIDUAL_ATOM,
            [
                Field::Node(map_iargument(context, first)?),
                Field::Node(map_iargument(context, second)?),
            ],
        ),
    }
}

fn axiom_annotations(
    context: &mut MappingContext,
    annotations: &BTreeSet<Annotation<RcStr>>,
    include_annotations: bool,
) -> Result<Field, RunnerError> {
    Ok(Field::Set(if include_annotations {
        map_annotations(context, annotations)?
    } else {
        Vec::new()
    }))
}

fn map_component_axiom(
    context: &mut MappingContext,
    component: &Component<RcStr>,
    annotations: &BTreeSet<Annotation<RcStr>>,
    include_annotations: bool,
) -> Result<(Vec<u8>, bool), RunnerError> {
    let mapped_annotations = axiom_annotations(context, annotations, include_annotations)?;
    let ann = || Ok::<Field, RunnerError>(mapped_annotations.clone());
    let mapped = match component {
        Component::DeclareClass(value) => c::node(
            c::DECLARATION,
            [Field::Node(map_class(context, &value.0)?), ann()?],
        )?,
        Component::DeclareObjectProperty(value) => c::node(
            c::DECLARATION,
            [Field::Node(map_object_property(context, &value.0)?), ann()?],
        )?,
        Component::DeclareAnnotationProperty(value) => c::node(
            c::DECLARATION,
            [
                Field::Node(map_annotation_property(context, &value.0)?),
                ann()?,
            ],
        )?,
        Component::DeclareDataProperty(value) => c::node(
            c::DECLARATION,
            [Field::Node(map_data_property(context, &value.0)?), ann()?],
        )?,
        Component::DeclareNamedIndividual(value) => c::node(
            c::DECLARATION,
            [
                Field::Node(map_named_individual(context, &value.0)?),
                ann()?,
            ],
        )?,
        Component::DeclareDatatype(value) => c::node(
            c::DECLARATION,
            [Field::Node(map_datatype(context, &value.0)?), ann()?],
        )?,
        Component::SubClassOf(value) => c::node(
            c::SUB_CLASS_OF,
            [
                Field::Node(map_class_expression(context, &value.sub)?),
                Field::Node(map_class_expression(context, &value.sup)?),
                ann()?,
            ],
        )?,
        Component::EquivalentClasses(value) => c::node(
            c::EQUIVALENT_CLASSES,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_class_expression(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::DisjointClasses(value) => c::node(
            c::DISJOINT_CLASSES,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_class_expression(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::DisjointUnion(value) => c::node(
            c::DISJOINT_UNION,
            [
                Field::Node(map_class(context, &value.0)?),
                Field::Set(
                    value
                        .1
                        .iter()
                        .map(|value| map_class_expression(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::SubObjectPropertyOf(value) => {
            let sub = match &value.sub {
                SubObjectPropertyExpression::ObjectPropertyExpression(value) => {
                    map_object_property_expression(context, value)?
                }
                SubObjectPropertyExpression::ObjectPropertyChain(values) => c::node(
                    c::OBJECT_PROPERTY_CHAIN,
                    [Field::Sequence(
                        values
                            .iter()
                            .map(|value| map_object_property_expression(context, value))
                            .collect::<Result<_, _>>()?,
                    )],
                )?,
            };
            c::node(
                c::SUB_OBJECT_PROPERTY_OF,
                [
                    Field::Node(sub),
                    Field::Node(map_object_property_expression(context, &value.sup)?),
                    ann()?,
                ],
            )?
        }
        Component::EquivalentObjectProperties(value) => c::node(
            c::EQUIVALENT_OBJECT_PROPERTIES,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_object_property_expression(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::DisjointObjectProperties(value) => c::node(
            c::DISJOINT_OBJECT_PROPERTIES,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_object_property_expression(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::InverseObjectProperties(value) => {
            let mut pair = [
                map_object_property(context, &value.0)?,
                map_object_property(context, &value.1)?,
            ];
            pair.sort();
            c::node(
                c::INVERSE_OBJECT_PROPERTIES,
                [
                    Field::Node(pair[0].clone()),
                    Field::Node(pair[1].clone()),
                    ann()?,
                ],
            )?
        }
        Component::ObjectPropertyDomain(value) => c::node(
            c::OBJECT_PROPERTY_DOMAIN,
            [
                Field::Node(map_object_property_expression(context, &value.ope)?),
                Field::Node(map_class_expression(context, &value.ce)?),
                ann()?,
            ],
        )?,
        Component::ObjectPropertyRange(value) => c::node(
            c::OBJECT_PROPERTY_RANGE,
            [
                Field::Node(map_object_property_expression(context, &value.ope)?),
                Field::Node(map_class_expression(context, &value.ce)?),
                ann()?,
            ],
        )?,
        Component::FunctionalObjectProperty(value) => {
            map_object_characteristic(context, c::FUNCTIONAL_OBJECT_PROPERTY, &value.0, ann()?)?
        }
        Component::InverseFunctionalObjectProperty(value) => map_object_characteristic(
            context,
            c::INVERSE_FUNCTIONAL_OBJECT_PROPERTY,
            &value.0,
            ann()?,
        )?,
        Component::ReflexiveObjectProperty(value) => {
            map_object_characteristic(context, c::REFLEXIVE_OBJECT_PROPERTY, &value.0, ann()?)?
        }
        Component::IrreflexiveObjectProperty(value) => {
            map_object_characteristic(context, c::IRREFLEXIVE_OBJECT_PROPERTY, &value.0, ann()?)?
        }
        Component::SymmetricObjectProperty(value) => {
            map_object_characteristic(context, c::SYMMETRIC_OBJECT_PROPERTY, &value.0, ann()?)?
        }
        Component::AsymmetricObjectProperty(value) => {
            map_object_characteristic(context, c::ASYMMETRIC_OBJECT_PROPERTY, &value.0, ann()?)?
        }
        Component::TransitiveObjectProperty(value) => {
            map_object_characteristic(context, c::TRANSITIVE_OBJECT_PROPERTY, &value.0, ann()?)?
        }
        Component::SubDataPropertyOf(value) => c::node(
            c::SUB_DATA_PROPERTY_OF,
            [
                Field::Node(map_data_property(context, &value.sub)?),
                Field::Node(map_data_property(context, &value.sup)?),
                ann()?,
            ],
        )?,
        Component::EquivalentDataProperties(value) => c::node(
            c::EQUIVALENT_DATA_PROPERTIES,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_data_property(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::DisjointDataProperties(value) => c::node(
            c::DISJOINT_DATA_PROPERTIES,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_data_property(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::DataPropertyDomain(value) => c::node(
            c::DATA_PROPERTY_DOMAIN,
            [
                Field::Node(map_data_property(context, &value.dp)?),
                Field::Node(map_class_expression(context, &value.ce)?),
                ann()?,
            ],
        )?,
        Component::DataPropertyRange(value) => c::node(
            c::DATA_PROPERTY_RANGE,
            [
                Field::Node(map_data_property(context, &value.dp)?),
                Field::Node(map_data_range(context, &value.dr)?),
                ann()?,
            ],
        )?,
        Component::FunctionalDataProperty(value) => c::node(
            c::FUNCTIONAL_DATA_PROPERTY,
            [Field::Node(map_data_property(context, &value.0)?), ann()?],
        )?,
        Component::DatatypeDefinition(value) => c::node(
            c::DATATYPE_DEFINITION,
            [
                Field::Node(map_datatype(context, &value.kind)?),
                Field::Node(map_data_range(context, &value.range)?),
                ann()?,
            ],
        )?,
        Component::HasKey(value) => {
            let mut object_properties = Vec::new();
            let mut data_properties = Vec::new();
            for property in &value.vpe {
                match property {
                    PropertyExpression::ObjectPropertyExpression(value) => {
                        object_properties.push(map_object_property_expression(context, value)?);
                    }
                    PropertyExpression::DataProperty(value) => {
                        data_properties.push(map_data_property(context, value)?);
                    }
                    PropertyExpression::AnnotationProperty(_) => {
                        return Err(RunnerError::new(
                            "Horned HasKey contains an annotation property",
                        ));
                    }
                }
            }
            c::node(
                c::HAS_KEY,
                [
                    Field::Node(map_class_expression(context, &value.ce)?),
                    Field::Set(object_properties),
                    Field::Set(data_properties),
                    ann()?,
                ],
            )?
        }
        Component::SameIndividual(value) => c::node(
            c::SAME_INDIVIDUAL,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_individual(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::DifferentIndividuals(value) => c::node(
            c::DIFFERENT_INDIVIDUALS,
            [
                Field::Set(
                    value
                        .0
                        .iter()
                        .map(|value| map_individual(context, value))
                        .collect::<Result<_, _>>()?,
                ),
                ann()?,
            ],
        )?,
        Component::ClassAssertion(value) => c::node(
            c::CLASS_ASSERTION,
            [
                Field::Node(map_class_expression(context, &value.ce)?),
                Field::Node(map_individual(context, &value.i)?),
                ann()?,
            ],
        )?,
        Component::ObjectPropertyAssertion(value) => c::node(
            c::OBJECT_PROPERTY_ASSERTION,
            [
                Field::Node(map_object_property_expression(context, &value.ope)?),
                Field::Node(map_individual(context, &value.from)?),
                Field::Node(map_individual(context, &value.to)?),
                ann()?,
            ],
        )?,
        Component::NegativeObjectPropertyAssertion(value) => c::node(
            c::NEGATIVE_OBJECT_PROPERTY_ASSERTION,
            [
                Field::Node(map_object_property_expression(context, &value.ope)?),
                Field::Node(map_individual(context, &value.from)?),
                Field::Node(map_individual(context, &value.to)?),
                ann()?,
            ],
        )?,
        Component::DataPropertyAssertion(value) => c::node(
            c::DATA_PROPERTY_ASSERTION,
            [
                Field::Node(map_data_property(context, &value.dp)?),
                Field::Node(map_individual(context, &value.from)?),
                Field::Node(map_literal(context, &value.to)?),
                ann()?,
            ],
        )?,
        Component::NegativeDataPropertyAssertion(value) => c::node(
            c::NEGATIVE_DATA_PROPERTY_ASSERTION,
            [
                Field::Node(map_data_property(context, &value.dp)?),
                Field::Node(map_individual(context, &value.from)?),
                Field::Node(map_literal(context, &value.to)?),
                ann()?,
            ],
        )?,
        Component::AnnotationAssertion(value) => {
            let subject = match &value.subject {
                AnnotationSubject::IRI(value) => c::iri(value.as_ref())?,
                AnnotationSubject::AnonymousIndividual(value) => map_anonymous(value)?,
            };
            c::node(
                c::ANNOTATION_ASSERTION,
                [
                    Field::Node(map_annotation_property(context, &value.ann.ap)?),
                    Field::Node(subject),
                    Field::Node(map_annotation_value(context, &value.ann.av)?),
                    ann()?,
                ],
            )?
        }
        Component::SubAnnotationPropertyOf(value) => c::node(
            c::SUB_ANNOTATION_PROPERTY_OF,
            [
                Field::Node(map_annotation_property(context, &value.sub)?),
                Field::Node(map_annotation_property(context, &value.sup)?),
                ann()?,
            ],
        )?,
        Component::AnnotationPropertyDomain(value) => c::node(
            c::ANNOTATION_PROPERTY_DOMAIN,
            [
                Field::Node(map_annotation_property(context, &value.ap)?),
                Field::Node(c::iri(value.iri.as_ref())?),
                ann()?,
            ],
        )?,
        Component::AnnotationPropertyRange(value) => c::node(
            c::ANNOTATION_PROPERTY_RANGE,
            [
                Field::Node(map_annotation_property(context, &value.ap)?),
                Field::Node(c::iri(value.iri.as_ref())?),
                ann()?,
            ],
        )?,
        Component::OntologyID(_)
        | Component::DocIRI(_)
        | Component::OntologyAnnotation(_)
        | Component::Import(_)
        | Component::Rule(_) => {
            return Err(RunnerError::new(
                "Horned metadata or rule was routed through the axiom mapper",
            ));
        }
    };
    let logical = !matches!(
        component,
        Component::DeclareClass(_)
            | Component::DeclareObjectProperty(_)
            | Component::DeclareAnnotationProperty(_)
            | Component::DeclareDataProperty(_)
            | Component::DeclareNamedIndividual(_)
            | Component::DeclareDatatype(_)
            | Component::AnnotationAssertion(_)
            | Component::SubAnnotationPropertyOf(_)
            | Component::AnnotationPropertyDomain(_)
            | Component::AnnotationPropertyRange(_)
    );
    Ok((mapped, logical))
}

fn map_object_characteristic(
    context: &mut MappingContext,
    tag: u64,
    value: &ObjectPropertyExpression<RcStr>,
    annotations: Field,
) -> Result<Vec<u8>, RunnerError> {
    c::node(
        tag,
        [
            Field::Node(map_object_property_expression(context, value)?),
            annotations,
        ],
    )
}

fn map_rule(
    context: &mut MappingContext,
    value: &Rule<RcStr>,
    annotations: &BTreeSet<Annotation<RcStr>>,
    include_annotations: bool,
) -> Result<Vec<u8>, RunnerError> {
    c::node(
        c::SWRL_RULE,
        [
            Field::Set(
                value
                    .body
                    .iter()
                    .map(|value| map_atom(context, value))
                    .collect::<Result<_, _>>()?,
            ),
            Field::Set(
                value
                    .head
                    .iter()
                    .map(|value| map_atom(context, value))
                    .collect::<Result<_, _>>()?,
            ),
            axiom_annotations(context, annotations, include_annotations)?,
        ],
    )
}

fn coalesce_rdf_equivalence_axioms(
    axioms: Vec<MappedAxiom>,
) -> Result<Vec<MappedAxiom>, RunnerError> {
    type EquivalenceComponent = (usize, BTreeSet<Vec<u8>>, BTreeSet<Vec<u8>>);
    let mut output = Vec::with_capacity(axioms.len());
    let mut components = BTreeMap::<u64, Vec<EquivalenceComponent>>::new();
    for (occurrence, axiom) in axioms.into_iter().enumerate() {
        let parsed = c::parse_node(&axiom.value)?;
        if !matches!(
            parsed.tag,
            c::EQUIVALENT_CLASSES
                | c::EQUIVALENT_OBJECT_PROPERTIES
                | c::EQUIVALENT_DATA_PROPERTIES
                | c::SAME_INDIVIDUAL
        ) {
            output.push((occurrence, axiom));
            continue;
        }
        let [c::ParsedField::Set(members), c::ParsedField::Set(annotations)] =
            parsed.fields.as_slice()
        else {
            return Err(RunnerError::new(
                "Horned equivalence axiom has an invalid canonical shape",
            ));
        };
        let members = members
            .iter()
            .map(c::ParsedNode::encode)
            .collect::<Result<BTreeSet<_>, _>>()?;
        if members.len() < 2 {
            return Err(RunnerError::new(
                "Horned equivalence axiom has fewer than two distinct members",
            ));
        }
        let annotations = annotations
            .iter()
            .map(c::ParsedNode::encode)
            .collect::<Result<BTreeSet<_>, _>>()?;
        let grouped = components.entry(parsed.tag).or_default();
        let mut first_occurrence = occurrence;
        let mut merged_members = members;
        let mut merged_annotations = annotations;
        let mut index = 0;
        while index < grouped.len() {
            if merged_members.is_disjoint(&grouped[index].1) {
                index += 1;
                continue;
            }
            let (other_occurrence, members, annotations) = grouped.remove(index);
            first_occurrence = first_occurrence.min(other_occurrence);
            merged_members.extend(members);
            merged_annotations.extend(annotations);
            index = 0;
        }
        grouped.push((first_occurrence, merged_members, merged_annotations));
    }
    for (tag, grouped) in components {
        for (occurrence, members, annotations) in grouped {
            let members = members.into_iter().collect::<Vec<_>>();
            output.push((
                occurrence,
                MappedAxiom {
                    value: c::node(
                        tag,
                        [
                            Field::Set(members.clone()),
                            Field::Set(annotations.into_iter().collect()),
                        ],
                    )?,
                    logical: Some(c::node(tag, [Field::Set(members), Field::Set(Vec::new())])?),
                },
            ));
        }
    }
    output.sort_by_key(|(occurrence, _)| *occurrence);
    Ok(output.into_iter().map(|(_, axiom)| axiom).collect())
}

fn map_document(
    ontology: &RcIRIMappedOntology,
    format: Format,
    max_canonical_work: u64,
    max_terms: u64,
) -> Result<MappedDocument, RunnerError> {
    let mut context = MappingContext {
        signature: BTreeSet::new(),
        simple_literal_datatype: if matches!(format, Format::OwlXml | Format::RdfXml) {
            XSD_STRING
        } else {
            RDF_PLAIN_LITERAL
        },
    };
    let mut ontology_id_seen = false;
    let mut ontology_iri = None;
    let mut version_iri = None;
    let mut imports = Vec::new();
    let mut annotations = Vec::new();
    let mut axioms = Vec::new();
    let mut extensions = Vec::new();
    for annotated in ontology.iter() {
        match &annotated.component {
            Component::OntologyID(value) => {
                if ontology_id_seen {
                    return Err(RunnerError::new(
                        "Horned ontology contains multiple ontology identifiers",
                    ));
                }
                ontology_id_seen = true;
                ontology_iri = value.iri.as_ref().map(ToString::to_string);
                version_iri = value.viri.as_ref().map(ToString::to_string);
            }
            Component::DocIRI(_) => {}
            Component::Import(value) => imports.push(c::iri(value.0.as_ref())?),
            Component::OntologyAnnotation(value) => {
                let nested = map_annotations(&mut context, &annotated.ann)?;
                annotations.push(map_annotation(&mut context, &value.0, nested)?);
            }
            Component::Rule(value) => {
                let mapped = map_rule(&mut context, value, &annotated.ann, true)?;
                let logical = map_rule(&mut context, value, &annotated.ann, false)?;
                extensions.push((mapped, logical));
            }
            component => {
                let (mapped, logical) =
                    map_component_axiom(&mut context, component, &annotated.ann, true)?;
                let logical_value = if logical {
                    Some(map_component_axiom(&mut context, component, &annotated.ann, false)?.0)
                } else {
                    None
                };
                axioms.push(MappedAxiom {
                    value: mapped,
                    logical: logical_value,
                });
            }
        }
    }
    if matches!(format, Format::RdfXml) {
        axioms = coalesce_rdf_equivalence_axioms(axioms)?;
    }
    if version_iri.is_some() && ontology_iri.is_none() {
        return Err(RunnerError::new(
            "Horned ontology version IRI has no ontology IRI",
        ));
    }
    let mut document = MappedDocument {
        ontology_iri,
        version_iri,
        imports,
        annotations,
        axioms,
        extensions,
        signature: context.signature.into_iter().collect(),
    };
    freeze_document_anonymous(&mut document, max_canonical_work, max_terms)?;
    Ok(document)
}

fn append_collection(output: &mut Vec<u8>, values: &[Vec<u8>]) -> Result<(), RunnerError> {
    output.extend(c::encode_varint(u64::try_from(values.len()).map_err(
        |_| RunnerError::new("common-contract collection cardinality exceeds u64"),
    )?));
    for value in values {
        output.extend(c::frame(value)?);
    }
    Ok(())
}

fn append_optional_iri(output: &mut Vec<u8>, value: Option<&str>) -> Result<(), RunnerError> {
    match value {
        None => output.push(b'0'),
        Some(value) => {
            output.push(b'1');
            output.extend(c::frame(&c::iri(value)?)?);
        }
    }
    Ok(())
}

fn append_optional_text(output: &mut Vec<u8>, value: Option<&str>) -> Result<(), RunnerError> {
    match value {
        None => output.push(b'0'),
        Some(value) => {
            output.push(b'1');
            output.extend(c::frame(value.as_bytes())?);
        }
    }
    Ok(())
}

fn digest(value: &[u8]) -> Vec<u8> {
    Sha256::digest(value).to_vec()
}

fn fingerprint_evidence(preimage: &[u8]) -> Value {
    let digest = hex_digest(&digest(preimage));
    json!({
        "algorithm": "sha256",
        "schema": 1,
        "preimage_bytes": preimage.len(),
        "preimage_sha256": digest,
        "digest": digest,
    })
}

fn record_inventory(values: &[Vec<u8>]) -> Result<Value, RunnerError> {
    let values = c::normalize_set(values.iter().cloned());
    let canonical_bytes = values.iter().try_fold(0_u64, |total, value| {
        total
            .checked_add(
                u64::try_from(value.len())
                    .map_err(|_| RunnerError::new("record inventory canonical bytes exceed u64"))?,
            )
            .ok_or_else(|| RunnerError::new("record inventory canonical bytes exceed u64"))
    })?;
    let mut transcript = RECORD_INVENTORY_DOMAIN.to_vec();
    append_collection(&mut transcript, &values)?;
    Ok(json!({
        "count": values.len(),
        "canonical_bytes": canonical_bytes,
        "transcript_bytes": transcript.len(),
        "sha256": hex_digest(&digest(&transcript)),
    }))
}

fn document_fingerprint(document: &MappedDocument) -> Result<Vec<u8>, RunnerError> {
    let mut preimage = b"pyowl-core:document-fingerprint:v1\0".to_vec();
    append_optional_iri(&mut preimage, document.ontology_iri.as_deref())?;
    append_optional_iri(&mut preimage, document.version_iri.as_deref())?;
    append_collection(&mut preimage, &document.imports)?;
    append_collection(&mut preimage, &document.annotations)?;
    append_collection(
        &mut preimage,
        &document
            .axioms
            .iter()
            .map(|value| value.value.clone())
            .collect::<Vec<_>>(),
    )?;
    append_collection(
        &mut preimage,
        &document
            .extensions
            .iter()
            .map(|value| value.0.clone())
            .collect::<Vec<_>>(),
    )?;
    Ok(preimage)
}

fn document_key(
    document: &MappedDocument,
    document_fingerprint: &[u8],
) -> Result<String, RunnerError> {
    let mut payload = Vec::new();
    match (&document.ontology_iri, &document.version_iri) {
        (None, None) => {
            payload.extend_from_slice(b"anonymous");
            payload.extend_from_slice(document_fingerprint);
        }
        (Some(ontology), None) => {
            payload.extend_from_slice(b"named");
            payload.extend(c::frame(b"ontology")?);
            payload.extend(c::frame(ontology.as_bytes())?);
        }
        (Some(ontology), Some(version)) => {
            payload.extend_from_slice(b"named");
            payload.extend(c::frame(b"version")?);
            payload.extend(c::frame(ontology.as_bytes())?);
            payload.extend(c::frame(version.as_bytes())?);
        }
        (None, Some(_)) => {
            return Err(RunnerError::new(
                "version IRI cannot identify an anonymous ontology",
            ));
        }
    }
    let mut preimage = b"pyowl-core:document-key:v1\0".to_vec();
    preimage.extend(payload);
    Ok(format!("d1:{}", hex_digest(&digest(&preimage))))
}

fn resolver_configuration_digest() -> Result<Vec<u8>, RunnerError> {
    let mut preimage = b"pyowl-core:resolver-configuration:v1\0".to_vec();
    preimage.extend(c::frame(b"none")?);
    Ok(digest(&preimage))
}

fn manifest_bytes(
    document: &MappedDocument,
    document_key: &str,
    document_fingerprint: &[u8],
) -> Result<Vec<u8>, RunnerError> {
    let mut output = b"pyowl-core:import-manifest:v1\0".to_vec();
    output.extend(c::frame(b"record_unresolved")?);
    output.push(1);
    output.extend(resolver_configuration_digest()?);
    output.extend(c::encode_varint(1));
    output.extend(c::frame(document_key.as_bytes())?);
    append_optional_iri(&mut output, document.ontology_iri.as_deref())?;
    append_optional_iri(&mut output, document.version_iri.as_deref())?;
    output.extend_from_slice(document_fingerprint);
    output.extend(c::frame(b"root")?);
    output.extend(c::encode_varint(
        u64::try_from(document.imports.len())
            .map_err(|_| RunnerError::new("import edge count exceeds u64"))?,
    ));
    for import in &document.imports {
        output.extend(c::frame(document_key.as_bytes())?);
        output.extend(c::frame(import)?);
        output.extend(c::frame(b"unresolved")?);
        append_optional_text(&mut output, None)?;
        append_optional_text(&mut output, Some("none"))?;
        append_optional_text(&mut output, Some("UNRESOLVED_IMPORT"))?;
    }
    Ok(output)
}

fn structural_preimage(
    document: &MappedDocument,
    document_key: &str,
    manifest: &[u8],
) -> Result<Vec<u8>, RunnerError> {
    let mut output = b"pyowl-core:snapshot-structural:v1\0".to_vec();
    output.extend(c::frame(manifest)?);
    output.extend(c::frame(document_key.as_bytes())?);
    append_collection(&mut output, &document.annotations)?;
    append_collection(
        &mut output,
        &document
            .axioms
            .iter()
            .map(|value| value.value.clone())
            .collect::<Vec<_>>(),
    )?;
    append_collection(
        &mut output,
        &document
            .extensions
            .iter()
            .map(|value| value.0.clone())
            .collect::<Vec<_>>(),
    )?;
    Ok(output)
}

fn logical_preimage(document: &MappedDocument) -> Result<Vec<u8>, RunnerError> {
    let logical = c::normalize_set(
        document
            .axioms
            .iter()
            .filter_map(|value| value.logical.clone()),
    );
    let extensions = c::normalize_set(document.extensions.iter().map(|value| value.1.clone()));
    let mut output = b"pyowl-core:snapshot-logical:v1\0datatype-policy:owl2-v1\0".to_vec();
    append_collection(&mut output, &logical)?;
    output.extend(c::encode_varint(u64::try_from(extensions.len()).map_err(
        |_| RunnerError::new("logical extension count exceeds u64"),
    )?));
    for value in extensions {
        output.push(b'E');
        output.extend(c::frame(&value)?);
    }
    Ok(output)
}

fn signature_preimage(document: &MappedDocument) -> Result<Vec<u8>, RunnerError> {
    let mut output = b"pyowl-core:snapshot-signature:v1\0".to_vec();
    output.push(1);
    append_collection(&mut output, &document.signature)?;
    Ok(output)
}

fn diagnostic_rows(document: &MappedDocument, document_iri: &str) -> Vec<Value> {
    let mut rows = Vec::new();
    for import in document.imports.iter().take(10_000) {
        let import_iri = decode_iri(import).unwrap_or_else(|| "invalid-import-iri".to_owned());
        let sanitized_import_iri = sanitize_diagnostic_iri(&import_iri);
        rows.push(json!({
            "code": "UNRESOLVED_IMPORT",
            "severity": "warning",
            "message": "import could not be resolved (not_found)",
            "document_iri": document_iri,
            "source_span": Value::Null,
            "import_chain": [import_iri],
            "details": {
                "import_iri": sanitized_import_iri,
                "resolver": "none",
            },
        }));
    }
    if document.imports.len() > 10_000 {
        let suppressed = document.imports.len() - 9_999;
        rows.truncate(9_999);
        rows.push(json!({
            "code": "DIAGNOSTICS_SUPPRESSED",
            "severity": "warning",
            "message": "additional import diagnostics were suppressed",
            "document_iri": Value::Null,
            "source_span": Value::Null,
            "import_chain": [],
            "details": {"count": suppressed},
        }));
    }
    rows
}

fn truncate_diagnostic_iri(value: String) -> String {
    if value.chars().count() <= 512 {
        return value;
    }
    let mut output = value.chars().take(509).collect::<String>();
    output.push_str("...");
    output
}

fn sanitize_diagnostic_iri(value: &str) -> String {
    let Some(scheme_end) = value.find(':') else {
        return truncate_diagnostic_iri(value.to_owned());
    };
    let scheme = value[..scheme_end].to_ascii_lowercase();
    if !matches!(scheme.as_str(), "http" | "https") {
        return truncate_diagnostic_iri(value.to_owned());
    }
    let remainder = &value[scheme_end + 1..];
    let authority_start = if let Some(value) = remainder.strip_prefix("//") {
        value
    } else {
        let path = remainder
            .trim_start_matches('/')
            .split(['?', '#'])
            .next()
            .unwrap_or_default();
        return truncate_diagnostic_iri(format!("{scheme}:///{path}"));
    };
    let authority_end = authority_start
        .find(['/', '?', '#'])
        .unwrap_or(authority_start.len());
    let authority = authority_start[..authority_end]
        .rsplit_once('@')
        .map_or(&authority_start[..authority_end], |(_, host)| host);
    let (host, port) = if let Some(bracketed) = authority.strip_prefix('[') {
        match bracketed.find(']') {
            Some(end) => {
                let suffix = &bracketed[end + 1..];
                (&bracketed[..end], suffix.strip_prefix(':'))
            }
            None => (authority, None),
        }
    } else {
        match authority.rsplit_once(':') {
            Some((host, port))
                if !port.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()) =>
            {
                (host, Some(port))
            }
            _ => (authority, None),
        }
    };
    let path_start = scheme_end + 3 + authority_end;
    let path = value
        .get(path_start..)
        .unwrap_or_default()
        .split(['?', '#'])
        .next()
        .unwrap_or_default();
    let mut sanitized = format!("{scheme}://{}", host.to_ascii_lowercase());
    if let Some(port) = port {
        sanitized.push(':');
        sanitized.push_str(port);
    }
    sanitized.push_str(path);
    truncate_diagnostic_iri(sanitized)
}

fn decode_iri(value: &[u8]) -> Option<String> {
    if value.first().copied()? != c::IRI as u8 || value.get(1).copied()? != 2 {
        return None;
    }
    let (length, offset) = decode_varint(value, 2)?;
    let end = offset.checked_add(length as usize)?;
    if end != value.len() {
        return None;
    }
    std::str::from_utf8(&value[offset..end])
        .ok()
        .map(str::to_owned)
}

fn decode_varint(value: &[u8], mut offset: usize) -> Option<(u64, usize)> {
    let mut result = 0_u64;
    let mut shift = 0_u32;
    while offset < value.len() && shift < 64 {
        let byte = value[offset];
        offset += 1;
        result |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Some((result, offset));
        }
        shift += 7;
    }
    None
}

fn optional_canonical_iri(value: Option<&str>) -> Result<Value, RunnerError> {
    Ok(match value {
        None => Value::Null,
        Some(value) => Value::String(hex_digest(&c::iri(value)?)),
    })
}

fn canonical_json(value: &Value) -> Result<Vec<u8>, RunnerError> {
    serde_json::to_vec(value)
        .map_err(|_| RunnerError::new("common contract could not be canonically serialized"))
}

fn provenance_rows(
    mut root_digests: Vec<Vec<u8>>,
    document_key: &str,
    canonical_ordinals: bool,
) -> Result<Vec<Value>, RunnerError> {
    if canonical_ordinals {
        root_digests.sort();
    }
    let mut origins: BTreeMap<Vec<u8>, Vec<Value>> = BTreeMap::new();
    for (occurrence, digest) in root_digests.into_iter().enumerate() {
        origins.entry(digest).or_default().push(json!({
            "document_key": document_key,
            "occurrence": u64::try_from(occurrence)
                .map_err(|_| RunnerError::new("provenance occurrence exceeds u64"))?,
            "span": Value::Null,
        }));
    }
    Ok(origins
        .into_iter()
        .map(|(digest, occurrences)| {
            json!({
                "structural_sha256": hex_digest(&digest),
                "occurrences": occurrences,
            })
        })
        .collect())
}

pub(crate) fn build_common_contract(
    ontology: &RcIRIMappedOntology,
    request: &ValidatedRequest,
    diagnostic_count: u64,
) -> Result<CommonContractBuild, RunnerError> {
    if diagnostic_count != 0 {
        return Err(RunnerError::new(
            "Horned RDF mapping reported incomplete records for the common contract",
        ));
    }
    let mut document = map_document(
        ontology,
        request.format,
        request.max_canonical_work,
        request.max_terms,
    )?;
    let document_preimage = document_fingerprint(&document)?;
    let document_fingerprint = digest(&document_preimage);
    let key = document_key(&document, &document_fingerprint)?;
    let manifest = manifest_bytes(&document, &key, &document_fingerprint)?;
    snapshot_scope_document(&mut document, &document_fingerprint)?;
    let structural = structural_preimage(&document, &key, &manifest)?;
    let logical = logical_preimage(&document)?;
    let signature = signature_preimage(&document)?;
    let diagnostics = diagnostic_rows(&document, &request.document_iri);

    let document_iri = c::iri(&request.document_iri)?;
    let identity = json!({
        "documents": [{
            "document_key": key,
            "document_iri": hex_digest(&document_iri),
            "ontology_iri": optional_canonical_iri(document.ontology_iri.as_deref())?,
            "version_iri": optional_canonical_iri(document.version_iri.as_deref())?,
            "source_sha256": request.source_sha256,
            "document_fingerprint": hex_digest(&document_fingerprint),
            "format": request.format.as_str(),
            "status": "root",
        }],
        "imports": document.imports.iter().map(|value| json!({
            "importing_document_key": key,
            "import_iri": hex_digest(value),
            "status": "unresolved",
            "resolved_document_key": Value::Null,
            "resolver_name": "none",
        })).collect::<Vec<_>>(),
        "import_policy": "record_unresolved",
        "offline": true,
        "resolver_configuration_sha256": hex_digest(&resolver_configuration_digest()?),
        "root_document_key": key,
    });

    let root_digests = if matches!(request.format, Format::RdfXml) {
        document
            .axioms
            .iter()
            .map(|value| c::structural_digest(&value.value))
            .chain(
                document
                    .extensions
                    .iter()
                    .map(|value| c::structural_digest(&value.0)),
            )
            .collect::<Vec<_>>()
    } else {
        document
            .annotations
            .iter()
            .map(|value| c::structural_digest(value))
            .chain(
                document
                    .axioms
                    .iter()
                    .map(|value| c::structural_digest(&value.value)),
            )
            .chain(
                document
                    .extensions
                    .iter()
                    .map(|value| c::structural_digest(&value.0)),
            )
            .collect::<Vec<_>>()
    };
    let provenance_rows =
        provenance_rows(root_digests, &key, matches!(request.format, Format::RdfXml))?;
    let provenance = json!({
        "origins": provenance_rows,
        "origin_entry_count": provenance_rows.len(),
        "source_byte_count": request.source.len(),
        "document_count": 1,
    });

    let mut document_row = c::frame(key.as_bytes())?;
    document_row.extend(
        hex_to_digest(&request.source_sha256)
            .ok_or_else(|| RunnerError::new("validated source digest could not be decoded"))?,
    );
    document_row.extend_from_slice(&document_fingerprint);
    let mut document_transcript = b"pyowl-core:comparator-document-inventory:v1\0".to_vec();
    document_transcript.extend(c::encode_varint(1));
    document_transcript.extend_from_slice(&document_row);
    let inventories = json!({
        "ontology_annotations": record_inventory(&document.annotations)?,
        "axioms": record_inventory(&document.axioms.iter().map(|value| value.value.clone()).collect::<Vec<_>>())?,
        "extensions": record_inventory(&document.extensions.iter().map(|value| value.0.clone()).collect::<Vec<_>>())?,
        "signature": record_inventory(&document.signature)?,
        "documents": {
            "count": 1,
            "canonical_bytes": document_row.len(),
            "transcript_bytes": document_transcript.len(),
            "sha256": hex_digest(&digest(&document_transcript)),
        },
    });
    let identity_bytes = canonical_json(&identity)?;
    let provenance_bytes = canonical_json(&provenance)?;
    let diagnostics_value = Value::Array(diagnostics);
    let diagnostics_bytes = canonical_json(&diagnostics_value)?;
    let ledger = json!({
        "inventories": inventories,
        "identity_sha256": hex_digest(&digest(&identity_bytes)),
        "identity_bytes": identity_bytes.len(),
        "provenance_sha256": hex_digest(&digest(&provenance_bytes)),
        "provenance_bytes": provenance_bytes.len(),
        "diagnostics_sha256": hex_digest(&digest(&diagnostics_bytes)),
        "diagnostics_bytes": diagnostics_bytes.len(),
        "diagnostic_count": diagnostics_value.as_array().map_or(0, Vec::len),
    });
    let mut contract = json!({
        "schema": COMMON_CONTRACT_SCHEMA,
        "model_schema": 1,
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "complete_import_closure": document.imports.is_empty(),
        "root_document_key": key,
        "identity": identity,
        "provenance": provenance,
        "diagnostics": diagnostics_value,
        "fingerprints": {
            "document": fingerprint_evidence(&document_preimage),
            "structural": fingerprint_evidence(&structural),
            "logical": fingerprint_evidence(&logical),
            "signature": fingerprint_evidence(&signature),
        },
        "ledger": ledger,
    });
    let contract_digest = hex_digest(&digest(&canonical_json(&contract)?));
    contract
        .as_object_mut()
        .ok_or_else(|| RunnerError::new("common contract is not an object"))?
        .insert("contract_sha256".to_owned(), Value::String(contract_digest));
    let validation_started = Instant::now();
    validate_contract(&contract)?;
    let validation_ns = u64::try_from(validation_started.elapsed().as_nanos())
        .map_err(|_| RunnerError::new("common contract validation duration exceeds u64"))?;
    Ok(CommonContractBuild {
        contract,
        validation_ns,
    })
}

fn validate_contract(contract: &Value) -> Result<(), RunnerError> {
    let object = contract
        .as_object()
        .ok_or_else(|| RunnerError::new("common contract validation expected an object"))?;
    require_exact_fields(
        object,
        &[
            "schema",
            "model_schema",
            "corpus_id",
            "source_sha256",
            "options_sha256",
            "complete_import_closure",
            "root_document_key",
            "identity",
            "provenance",
            "diagnostics",
            "fingerprints",
            "ledger",
            "contract_sha256",
        ],
        "common contract",
    )?;
    if object.get("schema").and_then(Value::as_str) != Some(COMMON_CONTRACT_SCHEMA) {
        return Err(RunnerError::new("common contract schema is unsupported"));
    }
    if object.get("model_schema").and_then(Value::as_i64).is_none() {
        return Err(RunnerError::new(
            "common contract model_schema is not an integer",
        ));
    }
    for name in ["corpus_id", "root_document_key"] {
        if object
            .get(name)
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        {
            return Err(RunnerError::new(format!(
                "common contract {name} is empty or not text"
            )));
        }
    }
    for name in ["source_sha256", "options_sha256", "contract_sha256"] {
        require_json_digest(
            object
                .get(name)
                .ok_or_else(|| RunnerError::new(format!("common contract {name} is missing")))?,
            name,
        )?;
    }
    if !object
        .get("complete_import_closure")
        .is_some_and(Value::is_boolean)
    {
        return Err(RunnerError::new(
            "common contract closure status is not boolean",
        ));
    }
    if !object.get("diagnostics").is_some_and(Value::is_array) {
        return Err(RunnerError::new(
            "common contract diagnostics are not an array",
        ));
    }
    for name in ["identity", "provenance", "fingerprints", "ledger"] {
        if !object.get(name).is_some_and(Value::is_object) {
            return Err(RunnerError::new(format!(
                "common contract {name} is not an object"
            )));
        }
    }

    let fingerprints = object["fingerprints"]
        .as_object()
        .ok_or_else(|| RunnerError::new("common fingerprints are missing"))?;
    require_exact_fields(
        fingerprints,
        &["document", "structural", "logical", "signature"],
        "common fingerprints",
    )?;
    for (name, raw) in fingerprints {
        let evidence = raw
            .as_object()
            .ok_or_else(|| RunnerError::new(format!("{name} fingerprint is not an object")))?;
        require_exact_fields(
            evidence,
            &[
                "algorithm",
                "schema",
                "preimage_bytes",
                "preimage_sha256",
                "digest",
            ],
            &format!("{name} fingerprint"),
        )?;
        if evidence.get("algorithm").and_then(Value::as_str) != Some("sha256")
            || evidence.get("schema").and_then(Value::as_u64) != Some(1)
        {
            return Err(RunnerError::new(format!(
                "{name} fingerprint algorithm or schema differs"
            )));
        }
        if evidence
            .get("preimage_bytes")
            .and_then(Value::as_u64)
            .is_none_or(|value| value < 1)
        {
            return Err(RunnerError::new(format!(
                "{name} fingerprint preimage byte count is invalid"
            )));
        }
        require_json_digest(&evidence["preimage_sha256"], "fingerprint preimage")?;
        require_json_digest(&evidence["digest"], "fingerprint digest")?;
        if evidence.get("preimage_sha256") != evidence.get("digest") {
            return Err(RunnerError::new(
                "common fingerprint evidence does not hash its preimage",
            ));
        }
    }

    let ledger = object["ledger"]
        .as_object()
        .ok_or_else(|| RunnerError::new("common ledger is missing"))?;
    require_exact_fields(
        ledger,
        &[
            "inventories",
            "identity_sha256",
            "identity_bytes",
            "provenance_sha256",
            "provenance_bytes",
            "diagnostics_sha256",
            "diagnostics_bytes",
            "diagnostic_count",
        ],
        "common ledger",
    )?;
    for name in ["identity_sha256", "provenance_sha256", "diagnostics_sha256"] {
        require_json_digest(&ledger[name], name)?;
    }
    for name in [
        "identity_bytes",
        "provenance_bytes",
        "diagnostics_bytes",
        "diagnostic_count",
    ] {
        if ledger.get(name).and_then(Value::as_u64).is_none() {
            return Err(RunnerError::new(format!(
                "common ledger {name} is not a nonnegative integer"
            )));
        }
    }
    let diagnostic_count = u64::try_from(
        object["diagnostics"]
            .as_array()
            .ok_or_else(|| RunnerError::new("common diagnostics are missing"))?
            .len(),
    )
    .map_err(|_| RunnerError::new("common diagnostic count exceeds u64"))?;
    if ledger.get("diagnostic_count").and_then(Value::as_u64) != Some(diagnostic_count) {
        return Err(RunnerError::new(
            "common diagnostic inventory count differs",
        ));
    }
    validate_inventories(
        ledger
            .get("inventories")
            .ok_or_else(|| RunnerError::new("common inventories are missing"))?,
    )?;

    for (name, value_name, bytes_name) in [
        ("identity", "identity_sha256", "identity_bytes"),
        ("provenance", "provenance_sha256", "provenance_bytes"),
        ("diagnostics", "diagnostics_sha256", "diagnostics_bytes"),
    ] {
        let encoded = canonical_json(&object[name])?;
        if ledger.get(value_name).and_then(Value::as_str)
            != Some(hex_digest(&digest(&encoded)).as_str())
        {
            return Err(RunnerError::new(format!(
                "common {name} inventory digest differs"
            )));
        }
        let encoded_length = u64::try_from(encoded.len())
            .map_err(|_| RunnerError::new(format!("common {name} byte count exceeds u64")))?;
        if ledger.get(bytes_name).and_then(Value::as_u64) != Some(encoded_length) {
            return Err(RunnerError::new(format!(
                "common {name} inventory byte count differs"
            )));
        }
    }

    let observed = object
        .get("contract_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| RunnerError::new("common contract digest is missing"))?;
    let mut unsigned = object.clone();
    unsigned.remove("contract_sha256");
    let expected = hex_digest(&digest(&canonical_json(&Value::Object(unsigned))?));
    if observed != expected {
        return Err(RunnerError::new("common contract digest validation failed"));
    }
    Ok(())
}

fn require_exact_fields(
    object: &serde_json::Map<String, Value>,
    expected: &[&str],
    name: &str,
) -> Result<(), RunnerError> {
    if object.len() != expected.len() || expected.iter().any(|field| !object.contains_key(*field)) {
        return Err(RunnerError::new(format!(
            "{name} fields differ from schema one"
        )));
    }
    Ok(())
}

fn require_json_digest(value: &Value, name: &str) -> Result<(), RunnerError> {
    if value
        .as_str()
        .and_then(hex_to_digest)
        .is_none_or(|digest| digest.len() != 32)
    {
        return Err(RunnerError::new(format!(
            "common contract {name} is not lowercase SHA-256"
        )));
    }
    Ok(())
}

fn validate_inventories(value: &Value) -> Result<(), RunnerError> {
    let inventories = value
        .as_object()
        .ok_or_else(|| RunnerError::new("common inventories are not an object"))?;
    require_exact_fields(
        inventories,
        &[
            "ontology_annotations",
            "axioms",
            "extensions",
            "signature",
            "documents",
        ],
        "common inventories",
    )?;
    for (name, raw) in inventories {
        let row = raw
            .as_object()
            .ok_or_else(|| RunnerError::new(format!("common inventory {name} is not an object")))?;
        require_exact_fields(
            row,
            &["count", "canonical_bytes", "transcript_bytes", "sha256"],
            &format!("common inventory {name}"),
        )?;
        for field in ["count", "canonical_bytes", "transcript_bytes"] {
            if row.get(field).and_then(Value::as_u64).is_none() {
                return Err(RunnerError::new(format!(
                    "common inventory {name}.{field} is invalid"
                )));
            }
        }
        require_json_digest(&row["sha256"], &format!("inventory {name}.sha256"))?;
    }
    Ok(())
}

fn hex_to_digest(value: &str) -> Option<Vec<u8>> {
    if value.len() != 64 {
        return None;
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let high = hex_nibble(pair[0])?;
            let low = hex_nibble(pair[1])?;
            Some((high << 4) | low)
        })
        .collect()
}

fn hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        _ => None,
    }
}

pub(crate) fn object_count(ontology: &RcIRIMappedOntology, contract: &Value) -> usize {
    let roots = contract["ledger"]["inventories"]
        .as_object()
        .map(|inventories| {
            ["ontology_annotations", "axioms", "extensions", "signature"]
                .iter()
                .filter_map(|name| inventories[*name]["count"].as_u64())
                .sum::<u64>()
        })
        .unwrap_or(0);
    ontology.iter().len().saturating_add(roots as usize)
}

#[cfg(test)]
mod tests {
    use std::io::{BufReader, Cursor};

    use super::*;
    use crate::{functional_swrl_edits, parse_common_ontology, sha256_hex, Format};

    const FUNCTIONAL_OPTIONS_SHA256: &str =
        "a68176678f9e39941cd6258b3b7181355afbbf751c89e43cc69e516aed82d24c";
    const OWLXML_OPTIONS_SHA256: &str =
        "a24b7713aa79cad899ffe819abc25ac9e53f8b9657b2e22507b1745073a8253e";
    const RDFXML_OPTIONS_SHA256: &str =
        "fdfc954b7b8f0253c8e90ee4542170f506ca069ac6bd93744ac0ceabf04f8d2f";

    fn contract(source: &[u8], corpus_id: &str) -> Value {
        contract_for(
            source,
            corpus_id,
            Format::Functional,
            FUNCTIONAL_OPTIONS_SHA256,
        )
    }

    fn contract_for(source: &[u8], corpus_id: &str, format: Format, options_sha256: &str) -> Value {
        let source_sha256 = sha256_hex(source);
        let request = ValidatedRequest {
            corpus_id: corpus_id.to_owned(),
            source: source.to_vec(),
            source_sha256: source_sha256.clone(),
            document_iri: format!("urn:pyowl-core:comparator-source:sha256:{source_sha256}"),
            format,
            options_sha256: options_sha256.to_owned(),
            input_mode: "resident-bytes".to_owned(),
            process_mode: "fresh-process".to_owned(),
            max_canonical_work: 1_000_000_000,
            max_terms: 500_000_000,
        };
        let rewrite_swrl = matches!(format, Format::Functional)
            && !functional_swrl_edits(source).unwrap().is_empty();
        let (ontology, diagnostics) =
            parse_common_ontology(BufReader::new(Cursor::new(source)), format, rewrite_swrl)
                .unwrap();
        build_common_contract(&ontology, &request, diagnostics)
            .unwrap()
            .contract
    }

    #[test]
    fn anonymous_labels_match_reference_contract_vectors() {
        let left = contract(
            include_bytes!("../../../../../tests/data/corpus/errata/blank-left.ofn"),
            "probe-blank-left",
        );
        let renamed = contract(
            include_bytes!("../../../../../tests/data/corpus/errata/blank-renamed.ofn"),
            "probe-blank-renamed",
        );
        assert_eq!(
            left["contract_sha256"],
            "7b8c6de9dc11cb21585386278a0138acb25112fcdc0f972a3fe24320171b2fb5"
        );
        assert_eq!(
            renamed["contract_sha256"],
            "8dc792f4d8fa535c459f76fd27d0e4deccf784fcd5d79748c5feb671dd463bc2"
        );
        assert_eq!(left["fingerprints"], renamed["fingerprints"]);
        assert_eq!(
            left["ledger"]["inventories"]["axioms"],
            renamed["ledger"]["inventories"]["axioms"]
        );
    }

    #[test]
    fn annotations_and_language_literals_match_reference_contract_vector() {
        let source = concat!(
            "Ontology(\n",
            "  Annotation(<urn:p> \"value\"@en)\n",
            "  AnnotationAssertion(\n",
            "    Annotation(<urn:q> \"v\")\n",
            "    <urn:p>\n",
            "    <urn:s>\n",
            "    \"Hi\"@EN\n",
            "  )\n",
            ")\n",
        );
        let value = contract(source.as_bytes(), "probe-horned-annotation-probe");
        assert_eq!(
            value["contract_sha256"],
            "01b8fa5f1510fffe9d6efc08619a69d55aa6e081fce813a1535d274c563dc5f2"
        );
    }

    #[test]
    fn owlxml_string_literals_match_reference_contract_vector() {
        let source = concat!(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n",
            "<Ontology xmlns=\"http://www.w3.org/2002/07/owl#\">\n",
            "  <Annotation>\n",
            "    <AnnotationProperty IRI=\"urn:p\"/>\n",
            "    <Literal datatypeIRI=\"http://www.w3.org/2001/XMLSchema#string\">alpha</Literal>\n",
            "  </Annotation>\n",
            "</Ontology>\n",
        );
        let value = contract_for(
            source.as_bytes(),
            "probe-horned-typed-string",
            Format::OwlXml,
            OWLXML_OPTIONS_SHA256,
        );
        assert_eq!(
            value["contract_sha256"],
            "1805ba8326e393c14a9f383ed00df5c948e49ebcf0356d024652a53f427a3e96"
        );
    }

    #[test]
    fn rdfxml_simple_literals_match_reference_contract_vector() {
        let source = concat!(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n",
            "<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\" ",
            "xmlns:owl=\"http://www.w3.org/2002/07/owl#\">\n",
            "  <owl:Ontology rdf:about=\"urn:o\">\n",
            "    <owl:versionInfo>alpha</owl:versionInfo>\n",
            "  </owl:Ontology>\n",
            "</rdf:RDF>\n",
        );
        let value = contract_for(
            source.as_bytes(),
            "probe-horned-rdf-literal",
            Format::RdfXml,
            RDFXML_OPTIONS_SHA256,
        );
        assert_eq!(
            value["contract_sha256"],
            "6bf684d79b456ab3243205d6e9fd436c2f013590c2226b09f83cb37c4c8e008d"
        );
    }

    #[test]
    fn rdfxml_equivalence_components_match_reference_contract_vector() {
        let source = concat!(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n",
            "<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\" ",
            "xmlns:owl=\"http://www.w3.org/2002/07/owl#\">\n",
            "  <owl:Ontology rdf:about=\"urn:o\"/>\n",
            "  <owl:Class rdf:about=\"urn:A\">\n",
            "    <owl:equivalentClass rdf:resource=\"urn:B\"/>\n",
            "    <owl:equivalentClass rdf:resource=\"urn:C\"/>\n",
            "  </owl:Class>\n",
            "  <owl:Class rdf:about=\"urn:B\"/>\n",
            "  <owl:Class rdf:about=\"urn:C\"/>\n",
            "</rdf:RDF>\n",
        );
        let value = contract_for(
            source.as_bytes(),
            "probe-horned-rdf-equivalence",
            Format::RdfXml,
            RDFXML_OPTIONS_SHA256,
        );
        assert_eq!(value["ledger"]["inventories"]["axioms"]["count"], 4);
        assert_eq!(
            value["contract_sha256"],
            "9034dbb5b3448dde1baf34adf9e5b92948dd677e8d83d6d2b29d865dad7c9c3e"
        );
    }

    #[test]
    fn rdfxml_duplicate_axiom_reifications_match_direct_reference_deterministically() {
        let source = concat!(
            "<?xml version=\"1.0\"?>\n",
            "<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"\n",
            "         xmlns:owl=\"http://www.w3.org/2002/07/owl#\"\n",
            "         xmlns:e=\"urn:\">\n",
            "  <owl:AnnotationProperty rdf:about=\"urn:p\"/>\n",
            "  <rdf:Description rdf:about=\"urn:s\">\n",
            "    <e:p rdf:resource=\"urn:o\"/>\n",
            "  </rdf:Description>\n",
            "  <owl:Axiom rdf:nodeID=\"first\">\n",
            "    <owl:annotatedSource rdf:resource=\"urn:s\"/>\n",
            "    <owl:annotatedProperty rdf:resource=\"urn:p\"/>\n",
            "    <owl:annotatedTarget rdf:resource=\"urn:o\"/>\n",
            "    <e:first rdf:resource=\"urn:one\"/>\n",
            "  </owl:Axiom>\n",
            "  <owl:Axiom rdf:nodeID=\"second\">\n",
            "    <owl:annotatedSource rdf:resource=\"urn:s\"/>\n",
            "    <owl:annotatedProperty rdf:resource=\"urn:p\"/>\n",
            "    <owl:annotatedTarget rdf:resource=\"urn:o\"/>\n",
            "    <e:second rdf:resource=\"urn:two\"/>\n",
            "  </owl:Axiom>\n",
            "</rdf:RDF>\n",
        );
        assert_eq!(
            sha256_hex(source.as_bytes()),
            "cd26e53712c8edaf49cd594b3f73ff02d6f3b5b18ccf24d105d1e9303e3134f1"
        );
        let expected = contract_for(
            source.as_bytes(),
            "probe-duplicate-reifications",
            Format::RdfXml,
            RDFXML_OPTIONS_SHA256,
        );
        assert_eq!(
            expected["contract_sha256"],
            "186f5521f2e029ca5cf246c03916719e1c65a73f5f75daa410d9278f170b675d"
        );
        assert_eq!(
            expected["ledger"]["inventories"]["axioms"],
            json!({
                "count": 2,
                "canonical_bytes": 203,
                "transcript_bytes": 249,
                "sha256": "04e80b339173a0746a0ce2cd5acc63ac02978ab3e6fa95936add9e30893ca55c",
            })
        );
        for _ in 0..8 {
            assert_eq!(
                contract_for(
                    source.as_bytes(),
                    "probe-duplicate-reifications",
                    Format::RdfXml,
                    RDFXML_OPTIONS_SHA256,
                ),
                expected
            );
        }
    }

    #[test]
    fn rdfxml_provenance_ordinals_are_digest_canonical_and_evidence_sensitive() {
        let lower = vec![0x10; 32];
        let upper = vec![0x20; 32];
        let canonical = provenance_rows(
            vec![upper.clone(), lower.clone(), upper.clone()],
            "d1:test",
            true,
        )
        .unwrap();
        let reordered = provenance_rows(
            vec![upper.clone(), upper.clone(), lower.clone()],
            "d1:test",
            true,
        )
        .unwrap();

        assert_eq!(canonical, reordered);
        assert_eq!(canonical[0]["occurrences"][0]["occurrence"], 0);
        assert_eq!(canonical[1]["occurrences"][0]["occurrence"], 1);
        assert_eq!(canonical[1]["occurrences"][1]["occurrence"], 2);
        assert_ne!(
            canonical,
            provenance_rows(
                vec![upper.clone(), lower.clone(), upper.clone()],
                "d1:other",
                true,
            )
            .unwrap()
        );
        assert_ne!(
            canonical,
            provenance_rows(vec![upper.clone(), lower], "d1:test", true).unwrap()
        );
        assert_ne!(
            canonical,
            provenance_rows(vec![upper, vec![0x30; 32]], "d1:test", true).unwrap()
        );
    }

    #[test]
    fn full_validation_rejects_tampered_nested_inventory() {
        let mut value = contract(b"Ontology(Declaration(Class(<urn:C>)))", "tamper");
        value["ledger"]["inventories"]["axioms"]["count"] = json!(7);
        assert!(validate_contract(&value).is_err());
    }

    #[test]
    fn swrl_extension_matches_reference_contract_vector() {
        let source = concat!(
            "Prefix(:=<urn:test#>) Ontology(<urn:rule>\n",
            "  SWRLRule(\n",
            "    Annotation(:p :note)\n",
            "    (ClassAtom(:A Variable(:x)))\n",
            "    (ClassAtom(:B Variable(:x)))\n",
            "  )\n",
            ")\n",
        );
        let value = contract(source.as_bytes(), "probe-horned-swrl-probe");
        assert_eq!(
            value["contract_sha256"],
            "4a5d29bc226f43b5d7a1c776374aaf4539cfbb38b7972351f9558a26b6cf3f78"
        );
        assert_eq!(value["ledger"]["inventories"]["extensions"]["count"], 1);
    }

    #[test]
    fn anonymous_work_limit_fails_closed_before_permutation_search() {
        let labels = BTreeSet::from(["left".to_owned(), "right".to_owned()]);
        let arcs = vec![BlankArc {
            source: "left".to_owned(),
            role: "same".to_owned(),
            target: Some("right".to_owned()),
            payload: Vec::new(),
        }];
        assert!(alpha_order(&labels, &arcs, 1).is_err());
    }

    #[test]
    fn unresolved_import_sanitizer_matches_reference_policy() {
        assert_eq!(
            sanitize_diagnostic_iri("HTTP://User:secret@Example.COM:8080/a?token=x#fragment"),
            "http://example.com:8080/a"
        );
        assert_eq!(
            sanitize_diagnostic_iri("https://[::1]:443/a?x"),
            "https://::1:443/a"
        );
        assert_eq!(sanitize_diagnostic_iri("http:/a?x"), "http:///a");
        assert_eq!(sanitize_diagnostic_iri("urn:test?q#f"), "urn:test?q#f");
        let long = format!("urn:{}", "x".repeat(600));
        let sanitized = sanitize_diagnostic_iri(&long);
        assert_eq!(sanitized.chars().count(), 512);
        assert!(sanitized.ends_with("..."));
    }
}
