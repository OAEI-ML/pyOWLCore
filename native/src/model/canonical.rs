//! Bounds-first scanner for frozen model-schema-1 canonical values.

use crate::error::{NativeError, NativeResult};
use crate::limits::Limits;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Category {
    Iri,
    Entity,
    Anonymous,
    Literal,
    Annotation,
    Term,
    Axiom,
    Swrl,
}

#[derive(Clone, Copy, Debug)]
struct Spec {
    fields: u8,
    category: Category,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EntityKind {
    Class,
    Datatype,
    ObjectProperty,
    DataProperty,
    AnnotationProperty,
    NamedIndividual,
}

#[derive(Clone, Copy, Debug)]
struct NodeMeta<'a> {
    tag: u64,
    category: Category,
    entity_kind: Option<EntityKind>,
    iri: Option<&'a str>,
    encoded: &'a [u8],
}

#[derive(Clone, Copy, Debug)]
enum NodeRule {
    Iri,
    Entity,
    Class,
    Datatype,
    ObjectProperty,
    DataProperty,
    AnnotationProperty,
    Literal,
    Annotation,
    FacetRestriction,
    DataRange,
    ClassExpression,
    ObjectPropertyExpression,
    SubObjectPropertyExpression,
    Individual,
    AnnotationSubject,
    AnnotationValue,
    IndividualArgument,
    DataArgument,
    Atom,
}

#[derive(Clone, Copy, Debug)]
enum ScalarRule {
    Text,
    BytesExact(usize),
    BytesNonempty,
    Integer,
    EntityKind,
    OptionalText,
}

#[derive(Clone, Copy, Debug)]
enum FieldRule {
    Scalar(ScalarRule),
    Node(NodeRule),
    Set {
        node: NodeRule,
        minimum: u64,
        forbidden_tag: Option<u64>,
    },
    Sequence {
        node: NodeRule,
        minimum: u64,
    },
}

#[derive(Clone, Copy, Debug)]
enum FieldValue<'a> {
    Null,
    Text(&'a str),
    Bytes,
    Integer,
    EntityKind(EntityKind),
    Node(NodeMeta<'a>),
    Collection(u64),
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct ScanBudget {
    pub(crate) max_depth: u32,
    pub(crate) max_terms: u64,
    pub(crate) max_sequence_arity: u64,
    max_iri_bytes: u64,
    max_literal_bytes: u64,
    max_rule_atoms: u64,
    max_canonical_work: u64,
    terms: u64,
}

impl ScanBudget {
    pub(crate) fn from_limits(limits: &Limits) -> Self {
        Self {
            // This scanner is recursive.  Preserve the public budget while
            // retaining a native hard ceiling against stack exhaustion.
            max_depth: limits.max_nesting_depth.min(1024),
            max_terms: limits.max_terms,
            max_sequence_arity: limits.max_sequence_arity,
            max_iri_bytes: limits.max_iri_bytes,
            max_literal_bytes: limits.max_literal_bytes,
            max_rule_atoms: limits.max_rule_atoms,
            max_canonical_work: limits.max_canonical_work,
            terms: 0,
        }
    }

    fn enter(&mut self, depth: u32) -> NativeResult<()> {
        self.terms = self
            .terms
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("canonical term counter overflow"))?;
        if depth > self.max_depth {
            return Err(NativeError::resource_limit(
                "max_nesting_depth",
                u64::from(depth),
                u64::from(self.max_depth),
                "canonical model row exceeds max_nesting_depth",
            ));
        }
        if self.terms > self.max_terms {
            return Err(NativeError::resource_limit(
                "max_terms",
                self.terms,
                self.max_terms,
                "canonical model row exceeds max_terms",
            ));
        }
        Ok(())
    }
}

pub(crate) fn scan_canonical(data: &[u8], budget: &mut ScanBudget) -> NativeResult<Category> {
    scan_canonical_observing(data, budget, None).map(|(category, _found)| category)
}

/// Validate one canonical row while proving whether any nested node uses the
/// requested constructor tag.  This keeps parser routing decisions inside the
/// native structural scan instead of reconstructing Python model objects.
pub(crate) fn canonical_contains_tag(
    data: &[u8],
    budget: &mut ScanBudget,
    target_tag: u64,
) -> NativeResult<bool> {
    scan_canonical_observing(data, budget, Some(target_tag)).map(|(_category, found)| found)
}

fn scan_canonical_observing(
    data: &[u8],
    budget: &mut ScanBudget,
    target_tag: Option<u64>,
) -> NativeResult<(Category, bool)> {
    let size = u64::try_from(data.len())
        .map_err(|_| NativeError::limit("canonical model row size exceeds u64"))?;
    if size > budget.max_canonical_work {
        return Err(NativeError::resource_limit(
            "max_canonical_work",
            size,
            budget.max_canonical_work,
            "canonical model row exceeds max_canonical_work",
        ));
    }
    let mut found = false;
    let (meta, offset) = scan_node(data, 0, data.len(), budget, 0, target_tag, &mut found)?;
    if offset != data.len() {
        return Err(NativeError::corrupt(
            "canonical model row has trailing bytes",
        ));
    }
    Ok((meta.category, found))
}

pub(crate) fn canonical_field_count(tag: u16) -> Option<u8> {
    spec(u64::from(tag)).map(|value| value.fields)
}

fn scan_node<'a>(
    data: &'a [u8],
    mut offset: usize,
    end: usize,
    budget: &mut ScanBudget,
    depth: u32,
    target_tag: Option<u64>,
    found: &mut bool,
) -> NativeResult<(NodeMeta<'a>, usize)> {
    budget.enter(depth)?;
    let node_start = offset;
    let (tag, next) = scan_bounded_varint(data, offset, end)?;
    if target_tag == Some(tag) {
        *found = true;
    }
    offset = next;
    let spec = spec(tag).ok_or_else(|| NativeError::corrupt("unknown canonical model tag"))?;
    let mut values: [Option<FieldValue<'a>>; 4] = [None; 4];
    for field_index in 0..spec.fields {
        let rule = field_rule(tag, field_index)
            .ok_or_else(|| NativeError::protocol("canonical field ledger is incomplete"))?;
        let marker = *data
            .get(offset)
            .ok_or_else(|| NativeError::corrupt("truncated canonical model component"))?;
        offset += 1;
        let value = match rule {
            FieldRule::Scalar(scalar) => {
                let (value, after) = scan_scalar(scalar, marker, data, offset, end)?;
                offset = after;
                value
            }
            FieldRule::Node(node_rule) => {
                if marker != 1 {
                    return Err(NativeError::corrupt(
                        "canonical node field has the wrong marker",
                    ));
                }
                let (start, frame_end, after) = scan_frame(data, offset, end)?;
                let (child, consumed) = scan_node(
                    data,
                    start,
                    frame_end,
                    budget,
                    depth
                        .checked_add(1)
                        .ok_or_else(|| NativeError::limit("canonical depth overflow"))?,
                    target_tag,
                    found,
                )?;
                if consumed != frame_end {
                    return Err(NativeError::corrupt(
                        "canonical nested node has trailing bytes",
                    ));
                }
                if !node_rule.accepts(child) {
                    return Err(NativeError::corrupt(
                        "canonical node has an invalid structural role",
                    ));
                }
                offset = after;
                FieldValue::Node(child)
            }
            FieldRule::Set {
                node,
                minimum,
                forbidden_tag,
            } => {
                if marker != 6 {
                    return Err(NativeError::corrupt(
                        "canonical set field has the wrong marker",
                    ));
                }
                let (count, next) = scan_bounded_varint(data, offset, end)?;
                offset = next;
                if count < minimum {
                    return Err(NativeError::corrupt("canonical set has too few members"));
                }
                if count > budget.max_sequence_arity {
                    return Err(NativeError::resource_limit(
                        "max_sequence_arity",
                        count,
                        budget.max_sequence_arity,
                        "canonical set arity exceeds max_sequence_arity",
                    ));
                }
                if count > remaining_frames(offset, end) {
                    return Err(NativeError::corrupt(
                        "canonical set arity exceeds remaining input",
                    ));
                }
                let mut previous: Option<&[u8]> = None;
                for _ in 0..count {
                    let (start, frame_end, after) = scan_frame(data, offset, end)?;
                    let current = data
                        .get(start..frame_end)
                        .ok_or_else(|| NativeError::corrupt("invalid canonical set frame"))?;
                    if previous.is_some_and(|value| current <= value) {
                        return Err(NativeError::corrupt(
                            "canonical set members are not strictly ordered",
                        ));
                    }
                    let (child, consumed) = scan_node(
                        data,
                        start,
                        frame_end,
                        budget,
                        depth
                            .checked_add(1)
                            .ok_or_else(|| NativeError::limit("canonical depth overflow"))?,
                        target_tag,
                        found,
                    )?;
                    if consumed != frame_end {
                        return Err(NativeError::corrupt(
                            "canonical set member has trailing bytes",
                        ));
                    }
                    if !node.accepts(child) || forbidden_tag == Some(child.tag) {
                        return Err(NativeError::corrupt(
                            "canonical set member has an invalid structural role",
                        ));
                    }
                    previous = Some(current);
                    offset = after;
                }
                FieldValue::Collection(count)
            }
            FieldRule::Sequence { node, minimum } => {
                if marker != 7 {
                    return Err(NativeError::corrupt(
                        "canonical sequence field has the wrong marker",
                    ));
                }
                let (count, next) = scan_bounded_varint(data, offset, end)?;
                offset = next;
                if count < minimum {
                    return Err(NativeError::corrupt(
                        "canonical sequence has too few members",
                    ));
                }
                if count > budget.max_sequence_arity {
                    return Err(NativeError::resource_limit(
                        "max_sequence_arity",
                        count,
                        budget.max_sequence_arity,
                        "canonical sequence arity exceeds max_sequence_arity",
                    ));
                }
                if count > (end - offset) as u64 {
                    return Err(NativeError::corrupt(
                        "canonical sequence arity exceeds remaining input",
                    ));
                }
                for _ in 0..count {
                    let item_marker = *data
                        .get(offset)
                        .ok_or_else(|| NativeError::corrupt("truncated canonical sequence"))?;
                    offset += 1;
                    if item_marker != 1 {
                        return Err(NativeError::corrupt(
                            "canonical sequence member has the wrong marker",
                        ));
                    }
                    let (start, frame_end, after) = scan_frame(data, offset, end)?;
                    let (child, consumed) = scan_node(
                        data,
                        start,
                        frame_end,
                        budget,
                        depth
                            .checked_add(1)
                            .ok_or_else(|| NativeError::limit("canonical depth overflow"))?,
                        target_tag,
                        found,
                    )?;
                    if consumed != frame_end || !node.accepts(child) {
                        return Err(NativeError::corrupt(
                            "canonical sequence member has an invalid structural role",
                        ));
                    }
                    offset = after;
                }
                FieldValue::Collection(count)
            }
        };
        values[usize::from(field_index)] = Some(value);
    }
    let mut meta = NodeMeta {
        tag,
        category: spec.category,
        entity_kind: None,
        iri: None,
        encoded: data
            .get(node_start..offset)
            .ok_or_else(|| NativeError::corrupt("canonical node range exceeds bounds"))?,
    };
    validate_local(&mut meta, &values, budget)?;
    Ok((meta, offset))
}

fn scan_scalar(
    rule: ScalarRule,
    marker: u8,
    data: &[u8],
    offset: usize,
    end: usize,
) -> NativeResult<(FieldValue<'_>, usize)> {
    match rule {
        ScalarRule::OptionalText if marker == 0 => Ok((FieldValue::Null, offset)),
        ScalarRule::OptionalText | ScalarRule::Text if marker == 2 => {
            let (start, frame_end, after) = scan_frame(data, offset, end)?;
            let payload = data
                .get(start..frame_end)
                .ok_or_else(|| NativeError::corrupt("invalid canonical scalar frame"))?;
            let text = std::str::from_utf8(payload)
                .map_err(|_| NativeError::corrupt("canonical text is not UTF-8"))?;
            Ok((FieldValue::Text(text), after))
        }
        ScalarRule::BytesExact(_) | ScalarRule::BytesNonempty if marker == 3 => {
            let (start, frame_end, after) = scan_frame(data, offset, end)?;
            let payload = data
                .get(start..frame_end)
                .ok_or_else(|| NativeError::corrupt("invalid canonical bytes frame"))?;
            if matches!(rule, ScalarRule::BytesNonempty) && payload.is_empty() {
                return Err(NativeError::corrupt(
                    "canonical bytes field must be nonempty",
                ));
            }
            if let ScalarRule::BytesExact(expected) = rule {
                if payload.len() != expected {
                    return Err(NativeError::corrupt(
                        "canonical bytes field has an invalid width",
                    ));
                }
            }
            Ok((FieldValue::Bytes, after))
        }
        ScalarRule::Integer if marker == 4 => {
            Ok((FieldValue::Integer, scan_any_varint(data, offset, end)?))
        }
        ScalarRule::EntityKind if marker == 5 => {
            let (start, frame_end, after) = scan_frame(data, offset, end)?;
            let payload = data
                .get(start..frame_end)
                .ok_or_else(|| NativeError::corrupt("invalid canonical enum frame"))?;
            let entity = EntityKind::decode(payload)
                .ok_or_else(|| NativeError::corrupt("unknown canonical entity kind"))?;
            Ok((FieldValue::EntityKind(entity), after))
        }
        _ => Err(NativeError::corrupt(
            "canonical scalar field has the wrong marker",
        )),
    }
}

const fn scalar(rule: ScalarRule) -> FieldRule {
    FieldRule::Scalar(rule)
}

const fn node(rule: NodeRule) -> FieldRule {
    FieldRule::Node(rule)
}

const fn set(node: NodeRule, minimum: u64) -> FieldRule {
    FieldRule::Set {
        node,
        minimum,
        forbidden_tag: None,
    }
}

const fn flattened_set(node: NodeRule, minimum: u64, tag: u64) -> FieldRule {
    FieldRule::Set {
        node,
        minimum,
        forbidden_tag: Some(tag),
    }
}

const fn sequence(node: NodeRule, minimum: u64) -> FieldRule {
    FieldRule::Sequence { node, minimum }
}

fn field_rule(tag: u64, index: u8) -> Option<FieldRule> {
    let rule = match (tag, index) {
        (1, 0) => scalar(ScalarRule::Text),
        (2, 0) => scalar(ScalarRule::EntityKind),
        (2, 1) => node(NodeRule::Iri),
        (3, 0) => scalar(ScalarRule::BytesExact(32)),
        (3, 1) => scalar(ScalarRule::BytesNonempty),
        (4, 0) => scalar(ScalarRule::Text),
        (4, 1) => node(NodeRule::Datatype),
        (4, 2) => scalar(ScalarRule::OptionalText),
        (5, 0) => node(NodeRule::AnnotationProperty),
        (5, 1) => node(NodeRule::AnnotationValue),
        (5, 2) => set(NodeRule::Annotation, 0),
        (10, 0) => node(NodeRule::ObjectProperty),
        (11, 0) => sequence(NodeRule::ObjectPropertyExpression, 2),
        (20, 0) => node(NodeRule::Iri),
        (20, 1) => node(NodeRule::Literal),
        (21, 0) => flattened_set(NodeRule::DataRange, 2, 21),
        (22, 0) => flattened_set(NodeRule::DataRange, 2, 22),
        (23, 0) => node(NodeRule::DataRange),
        (24, 0) => set(NodeRule::Literal, 1),
        (25, 0) => node(NodeRule::Datatype),
        (25, 1) => set(NodeRule::FacetRestriction, 1),
        (30, 0) => flattened_set(NodeRule::ClassExpression, 2, 30),
        (31, 0) => flattened_set(NodeRule::ClassExpression, 2, 31),
        (32, 0) => node(NodeRule::ClassExpression),
        (33, 0) => set(NodeRule::Individual, 1),
        (34 | 35, 0) => node(NodeRule::ObjectPropertyExpression),
        (34 | 35, 1) => node(NodeRule::ClassExpression),
        (36, 0) => node(NodeRule::ObjectPropertyExpression),
        (36, 1) => node(NodeRule::Individual),
        (37, 0) => node(NodeRule::ObjectPropertyExpression),
        (38..=40, 0) => scalar(ScalarRule::Integer),
        (38..=40, 1) => node(NodeRule::ObjectPropertyExpression),
        (38..=40, 2) => node(NodeRule::ClassExpression),
        (41 | 42, 0) => sequence(NodeRule::DataProperty, 1),
        (41 | 42, 1) => node(NodeRule::DataRange),
        (43, 0) => node(NodeRule::DataProperty),
        (43, 1) => node(NodeRule::Literal),
        (44..=46, 0) => scalar(ScalarRule::Integer),
        (44..=46, 1) => node(NodeRule::DataProperty),
        (44..=46, 2) => node(NodeRule::DataRange),
        (60, 0) => node(NodeRule::Entity),
        (60, 1) => set(NodeRule::Annotation, 0),
        (61, 0 | 1) => node(NodeRule::ClassExpression),
        (61, 2) => set(NodeRule::Annotation, 0),
        (62 | 63, 0) => set(NodeRule::ClassExpression, 2),
        (62 | 63, 1) => set(NodeRule::Annotation, 0),
        (64, 0) => node(NodeRule::Class),
        (64, 1) => set(NodeRule::ClassExpression, 2),
        (64, 2) => set(NodeRule::Annotation, 0),
        (70, 0) => node(NodeRule::SubObjectPropertyExpression),
        (70, 1) => node(NodeRule::ObjectPropertyExpression),
        (70, 2) => set(NodeRule::Annotation, 0),
        (71 | 72, 0) => set(NodeRule::ObjectPropertyExpression, 2),
        (71 | 72, 1) => set(NodeRule::Annotation, 0),
        (73, 0 | 1) => node(NodeRule::ObjectPropertyExpression),
        (73, 2) => set(NodeRule::Annotation, 0),
        (74 | 75, 0) => node(NodeRule::ObjectPropertyExpression),
        (74 | 75, 1) => node(NodeRule::ClassExpression),
        (74 | 75, 2) => set(NodeRule::Annotation, 0),
        (76..=82, 0) => node(NodeRule::ObjectPropertyExpression),
        (76..=82, 1) => set(NodeRule::Annotation, 0),
        (90, 0 | 1) => node(NodeRule::DataProperty),
        (90, 2) => set(NodeRule::Annotation, 0),
        (91 | 92, 0) => set(NodeRule::DataProperty, 2),
        (91 | 92, 1) => set(NodeRule::Annotation, 0),
        (93, 0) => node(NodeRule::DataProperty),
        (93, 1) => node(NodeRule::ClassExpression),
        (93, 2) => set(NodeRule::Annotation, 0),
        (94, 0) => node(NodeRule::DataProperty),
        (94, 1) => node(NodeRule::DataRange),
        (94, 2) => set(NodeRule::Annotation, 0),
        (95, 0) => node(NodeRule::DataProperty),
        (95, 1) => set(NodeRule::Annotation, 0),
        (100, 0) => node(NodeRule::Datatype),
        (100, 1) => node(NodeRule::DataRange),
        (100, 2) => set(NodeRule::Annotation, 0),
        (101, 0) => node(NodeRule::ClassExpression),
        (101, 1) => set(NodeRule::ObjectPropertyExpression, 0),
        (101, 2) => set(NodeRule::DataProperty, 0),
        (101, 3) => set(NodeRule::Annotation, 0),
        (110 | 111, 0) => set(NodeRule::Individual, 2),
        (110 | 111, 1) => set(NodeRule::Annotation, 0),
        (112, 0) => node(NodeRule::ClassExpression),
        (112, 1) => node(NodeRule::Individual),
        (112, 2) => set(NodeRule::Annotation, 0),
        (113 | 114, 0) => node(NodeRule::ObjectPropertyExpression),
        (113 | 114, 1 | 2) => node(NodeRule::Individual),
        (113 | 114, 3) => set(NodeRule::Annotation, 0),
        (115 | 116, 0) => node(NodeRule::DataProperty),
        (115 | 116, 1) => node(NodeRule::Individual),
        (115 | 116, 2) => node(NodeRule::Literal),
        (115 | 116, 3) => set(NodeRule::Annotation, 0),
        (120, 0) => node(NodeRule::AnnotationProperty),
        (120, 1) => node(NodeRule::AnnotationSubject),
        (120, 2) => node(NodeRule::AnnotationValue),
        (120, 3) => set(NodeRule::Annotation, 0),
        (121, 0 | 1) => node(NodeRule::AnnotationProperty),
        (121, 2) => set(NodeRule::Annotation, 0),
        (122 | 123, 0) => node(NodeRule::AnnotationProperty),
        (122 | 123, 1) => node(NodeRule::Iri),
        (122 | 123, 2) => set(NodeRule::Annotation, 0),
        (140, 0) => node(NodeRule::Iri),
        (141, 0) => node(NodeRule::ClassExpression),
        (141, 1) => node(NodeRule::IndividualArgument),
        (142, 0) => node(NodeRule::DataRange),
        (142, 1) => node(NodeRule::DataArgument),
        (143, 0) => node(NodeRule::ObjectPropertyExpression),
        (143, 1 | 2) => node(NodeRule::IndividualArgument),
        (144, 0) => node(NodeRule::DataProperty),
        (144, 1) => node(NodeRule::IndividualArgument),
        (144, 2) => node(NodeRule::DataArgument),
        (145, 0) => node(NodeRule::Iri),
        (145, 1) => sequence(NodeRule::DataArgument, 0),
        (146 | 147, 0 | 1) => node(NodeRule::IndividualArgument),
        (148, 0 | 1) => set(NodeRule::Atom, 0),
        (148, 2) => set(NodeRule::Annotation, 0),
        _ => return None,
    };
    Some(rule)
}

impl EntityKind {
    fn decode(value: &[u8]) -> Option<Self> {
        match value {
            b"class" => Some(Self::Class),
            b"datatype" => Some(Self::Datatype),
            b"object_property" => Some(Self::ObjectProperty),
            b"data_property" => Some(Self::DataProperty),
            b"annotation_property" => Some(Self::AnnotationProperty),
            b"named_individual" => Some(Self::NamedIndividual),
            _ => None,
        }
    }
}

impl NodeRule {
    fn accepts(self, value: NodeMeta<'_>) -> bool {
        let entity = |kind| value.tag == 2 && value.entity_kind == Some(kind);
        match self {
            Self::Iri => value.tag == 1,
            Self::Entity => value.tag == 2 && value.entity_kind.is_some(),
            Self::Class => entity(EntityKind::Class),
            Self::Datatype => entity(EntityKind::Datatype),
            Self::ObjectProperty => entity(EntityKind::ObjectProperty),
            Self::DataProperty => entity(EntityKind::DataProperty),
            Self::AnnotationProperty => entity(EntityKind::AnnotationProperty),
            Self::Literal => value.tag == 4,
            Self::Annotation => value.tag == 5,
            Self::FacetRestriction => value.tag == 20,
            Self::DataRange => entity(EntityKind::Datatype) || (21..=25).contains(&value.tag),
            Self::ClassExpression => entity(EntityKind::Class) || (30..=46).contains(&value.tag),
            Self::ObjectPropertyExpression => entity(EntityKind::ObjectProperty) || value.tag == 10,
            Self::SubObjectPropertyExpression => {
                entity(EntityKind::ObjectProperty) || matches!(value.tag, 10 | 11)
            }
            Self::Individual => entity(EntityKind::NamedIndividual) || value.tag == 3,
            Self::AnnotationSubject => matches!(value.tag, 1 | 3),
            Self::AnnotationValue => matches!(value.tag, 1 | 3 | 4),
            Self::IndividualArgument => {
                entity(EntityKind::NamedIndividual) || matches!(value.tag, 3 | 140)
            }
            Self::DataArgument => matches!(value.tag, 4 | 140),
            Self::Atom => (141..=147).contains(&value.tag),
        }
    }
}

fn validate_local<'a>(
    meta: &mut NodeMeta<'a>,
    values: &[Option<FieldValue<'a>>; 4],
    budget: &ScanBudget,
) -> NativeResult<()> {
    match meta.tag {
        1 => {
            let FieldValue::Text(iri) = required_value(values, 0)? else {
                return Err(NativeError::protocol("IRI field ledger mismatch"));
            };
            if iri.len() as u64 > budget.max_iri_bytes {
                return Err(NativeError::resource_limit(
                    "max_iri_bytes",
                    iri.len() as u64,
                    budget.max_iri_bytes,
                    "canonical IRI exceeds max_iri_bytes",
                ));
            }
            validate_iri(iri)?;
            meta.iri = Some(iri);
        }
        2 => {
            let FieldValue::EntityKind(kind) = required_value(values, 0)? else {
                return Err(NativeError::protocol("entity kind ledger mismatch"));
            };
            let FieldValue::Node(iri) = required_value(values, 1)? else {
                return Err(NativeError::protocol("entity IRI ledger mismatch"));
            };
            meta.entity_kind = Some(kind);
            meta.iri = iri.iri;
        }
        4 => validate_literal(values, budget)?,
        73 => {
            let FieldValue::Node(first) = required_value(values, 0)? else {
                return Err(NativeError::protocol("inverse property ledger mismatch"));
            };
            let FieldValue::Node(second) = required_value(values, 1)? else {
                return Err(NativeError::protocol("inverse property ledger mismatch"));
            };
            if second.encoded < first.encoded {
                return Err(NativeError::corrupt(
                    "inverse object properties are not canonically ordered",
                ));
            }
        }
        101 => {
            let FieldValue::Collection(objects) = required_value(values, 1)? else {
                return Err(NativeError::protocol("HasKey ledger mismatch"));
            };
            let FieldValue::Collection(data) = required_value(values, 2)? else {
                return Err(NativeError::protocol("HasKey ledger mismatch"));
            };
            if objects == 0 && data == 0 {
                return Err(NativeError::corrupt("HasKey has no properties"));
            }
        }
        148 => {
            let FieldValue::Collection(body) = required_value(values, 0)? else {
                return Err(NativeError::protocol("SWRL rule ledger mismatch"));
            };
            let FieldValue::Collection(head) = required_value(values, 1)? else {
                return Err(NativeError::protocol("SWRL rule ledger mismatch"));
            };
            if body > budget.max_rule_atoms || head > budget.max_rule_atoms {
                return Err(NativeError::resource_limit(
                    "max_rule_atoms",
                    body.max(head),
                    budget.max_rule_atoms,
                    "SWRL rule exceeds max_rule_atoms",
                ));
            }
        }
        _ => {}
    }
    Ok(())
}

fn required_value<'a>(
    values: &[Option<FieldValue<'a>>; 4],
    index: usize,
) -> NativeResult<FieldValue<'a>> {
    values[index].ok_or_else(|| NativeError::protocol("canonical field ledger is incomplete"))
}

fn validate_literal(values: &[Option<FieldValue<'_>>; 4], budget: &ScanBudget) -> NativeResult<()> {
    const RDF_PLAIN_LITERAL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
    const RDF_LANG_STRING: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString";
    let FieldValue::Text(lexical) = required_value(values, 0)? else {
        return Err(NativeError::protocol("literal lexical ledger mismatch"));
    };
    if lexical.len() as u64 > budget.max_literal_bytes {
        return Err(NativeError::resource_limit(
            "max_literal_bytes",
            lexical.len() as u64,
            budget.max_literal_bytes,
            "canonical literal exceeds max_literal_bytes",
        ));
    }
    let FieldValue::Node(datatype) = required_value(values, 1)? else {
        return Err(NativeError::protocol("literal datatype ledger mismatch"));
    };
    let iri = datatype
        .iri
        .ok_or_else(|| NativeError::protocol("literal datatype has no IRI"))?;
    if iri == RDF_LANG_STRING {
        return Err(NativeError::corrupt(
            "rdf:langString is not a canonical literal datatype",
        ));
    }
    match required_value(values, 2)? {
        FieldValue::Null => Ok(()),
        FieldValue::Text(language) => {
            if iri != RDF_PLAIN_LITERAL || !valid_language(language) {
                return Err(NativeError::corrupt(
                    "literal language/datatype relationship is invalid",
                ));
            }
            Ok(())
        }
        _ => Err(NativeError::protocol("literal language ledger mismatch")),
    }
}

pub(crate) fn validate_iri(value: &str) -> NativeResult<()> {
    let bytes = value.as_bytes();
    let Some(colon) = bytes.iter().position(|byte| *byte == b':') else {
        return Err(NativeError::corrupt("IRI is not absolute"));
    };
    if colon == 0
        || !bytes[0].is_ascii_alphabetic()
        || !bytes[1..colon]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'+' | b'-' | b'.'))
    {
        return Err(NativeError::corrupt("IRI has an invalid scheme"));
    }
    for character in value.chars() {
        let codepoint = u32::from(character);
        if codepoint <= 0x20
            || matches!(
                character,
                '<' | '>' | '"' | '{' | '}' | '|' | '\\' | '^' | '`'
            )
            || (0x7f..=0x9f).contains(&codepoint)
            || (0xfdd0..=0xfdef).contains(&codepoint)
            || matches!(codepoint & 0xffff, 0xfffe | 0xffff)
        {
            return Err(NativeError::corrupt("IRI contains a forbidden scalar"));
        }
    }
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            if index + 2 >= bytes.len()
                || !bytes[index + 1].is_ascii_hexdigit()
                || !bytes[index + 2].is_ascii_hexdigit()
            {
                return Err(NativeError::corrupt(
                    "IRI contains an invalid percent escape",
                ));
            }
            index += 3;
        } else {
            index += 1;
        }
    }
    Ok(())
}

fn valid_language(value: &str) -> bool {
    const GRANDFATHERED: [&str; 26] = [
        "art-lojban",
        "cel-gaulish",
        "en-gb-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "no-bok",
        "no-nyn",
        "sgn-be-fr",
        "sgn-be-nl",
        "sgn-ch-de",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    ];
    if value.is_empty()
        || value.bytes().any(|byte| {
            byte.is_ascii_uppercase() || !(byte.is_ascii_alphanumeric() || byte == b'-')
        })
        || value.starts_with('-')
        || value.ends_with('-')
        || value.contains("--")
    {
        return false;
    }
    if GRANDFATHERED.contains(&value) {
        return true;
    }
    let mut parts = value.split('-').peekable();
    let Some(first) = parts.next() else {
        return false;
    };
    if first == "x" {
        let mut count = 0_u64;
        for part in parts {
            if !(1..=8).contains(&part.len()) {
                return false;
            }
            count += 1;
        }
        return count != 0;
    }
    if !first.bytes().all(|byte| byte.is_ascii_lowercase()) || !matches!(first.len(), 2..=8) {
        return false;
    }
    if matches!(first.len(), 2 | 3) {
        let mut extlangs = 0;
        while parts.peek().is_some_and(|part| {
            part.len() == 3 && part.bytes().all(|byte| byte.is_ascii_lowercase()) && extlangs < 3
        }) {
            extlangs += 1;
            parts.next();
        }
    }
    if parts
        .peek()
        .is_some_and(|part| part.len() == 4 && part.bytes().all(|byte| byte.is_ascii_lowercase()))
    {
        parts.next();
    }
    if parts.peek().is_some_and(|part| {
        (part.len() == 2 && part.bytes().all(|byte| byte.is_ascii_lowercase()))
            || (part.len() == 3 && part.bytes().all(|byte| byte.is_ascii_digit()))
    }) {
        parts.next();
    }
    let variants_start = parts.peek().map_or(value.len(), |part| {
        part.as_ptr() as usize - value.as_ptr() as usize
    });
    while parts.peek().is_some_and(|part| {
        (5..=8).contains(&part.len()) || (part.len() == 4 && part.as_bytes()[0].is_ascii_digit())
    }) {
        let Some(part) = parts.next() else {
            return false;
        };
        let offset = part.as_ptr() as usize - value.as_ptr() as usize;
        if value[variants_start..offset]
            .trim_end_matches('-')
            .split('-')
            .any(|previous| previous == part)
        {
            return false;
        }
    }
    let extensions_start = parts.peek().map_or(value.len(), |part| {
        part.as_ptr() as usize - value.as_ptr() as usize
    });
    while parts
        .peek()
        .is_some_and(|part| part.len() == 1 && *part != "x")
    {
        let Some(singleton) = parts.next() else {
            return false;
        };
        let offset = singleton.as_ptr() as usize - value.as_ptr() as usize;
        if value[extensions_start..offset]
            .trim_end_matches('-')
            .split('-')
            .any(|previous| previous.len() == 1 && previous == singleton)
        {
            return false;
        }
        let mut count = 0_u64;
        while parts
            .peek()
            .is_some_and(|part| (2..=8).contains(&part.len()))
        {
            parts.next();
            count += 1;
        }
        if count == 0 {
            return false;
        }
    }
    if parts.peek().is_some_and(|part| *part == "x") {
        parts.next();
        let mut count = 0_u64;
        while parts
            .peek()
            .is_some_and(|part| (1..=8).contains(&part.len()))
        {
            parts.next();
            count += 1;
        }
        if count == 0 {
            return false;
        }
    }
    parts.next().is_none()
}

fn remaining_frames(offset: usize, end: usize) -> u64 {
    u64::try_from(end - offset).unwrap_or(u64::MAX)
}

fn scan_frame(data: &[u8], offset: usize, end: usize) -> NativeResult<(usize, usize, usize)> {
    let (length, start) = scan_bounded_varint(data, offset, end)?;
    let length = usize::try_from(length)
        .map_err(|_| NativeError::corrupt("canonical frame length exceeds address space"))?;
    let frame_end = start
        .checked_add(length)
        .ok_or_else(|| NativeError::corrupt("canonical frame length overflow"))?;
    if frame_end > end || frame_end > data.len() {
        return Err(NativeError::corrupt("truncated canonical framed component"));
    }
    Ok((start, frame_end, frame_end))
}

fn scan_bounded_varint(data: &[u8], offset: usize, end: usize) -> NativeResult<(u64, usize)> {
    let start = offset;
    let mut cursor = offset;
    let mut value = 0_u64;
    let mut shift = 0_u32;
    while cursor < end {
        let byte = data[cursor];
        cursor += 1;
        let payload = byte & 0x7f;
        if cursor - start > 10 || (shift == 63 && payload > 1) {
            return Err(NativeError::corrupt("canonical count varint is too large"));
        }
        value |= u64::from(payload) << shift;
        if byte & 0x80 == 0 {
            if cursor - start > 1 && byte == 0 {
                return Err(NativeError::corrupt("canonical varint is nonminimal"));
            }
            return Ok((value, cursor));
        }
        shift += 7;
    }
    Err(NativeError::corrupt("truncated canonical varint"))
}

fn scan_any_varint(data: &[u8], offset: usize, end: usize) -> NativeResult<usize> {
    let start = offset;
    let mut cursor = offset;
    while cursor < end {
        let byte = data[cursor];
        cursor += 1;
        if byte & 0x80 == 0 {
            if cursor - start > 1 && byte == 0 {
                return Err(NativeError::corrupt("canonical integer is nonminimal"));
            }
            return Ok(cursor);
        }
        if cursor - start >= 142_858 {
            return Err(NativeError::corrupt(
                "canonical integer is unreasonably long",
            ));
        }
    }
    Err(NativeError::corrupt("truncated canonical integer"))
}

fn spec(tag: u64) -> Option<Spec> {
    let (fields, category) = match tag {
        1 => (1, Category::Iri),
        2 => (2, Category::Entity),
        3 => (2, Category::Anonymous),
        4 => (3, Category::Literal),
        5 => (3, Category::Annotation),
        10 => (1, Category::Term),
        11 => (1, Category::Term),
        20 => (2, Category::Term),
        21 | 22 | 23 | 24 | 30 | 31 | 32 | 33 | 37 => (1, Category::Term),
        25 | 34 | 35 | 36 | 41 | 42 | 43 => (2, Category::Term),
        38 | 39 | 40 | 44 | 45 | 46 => (3, Category::Term),
        60 | 62 | 63 | 71 | 72 | 76 | 77 | 78 | 79 | 80 | 81 | 82 | 91 | 92 | 95 | 110 | 111 => {
            (2, Category::Axiom)
        }
        61 | 64 | 70 | 73 | 74 | 75 | 90 | 93 | 94 | 100 | 112 | 121 | 122 | 123 => {
            (3, Category::Axiom)
        }
        101 | 113 | 114 | 115 | 116 | 120 => (4, Category::Axiom),
        140 => (1, Category::Swrl),
        141 | 142 | 145 | 146 | 147 => (2, Category::Swrl),
        143 | 144 | 148 => (3, Category::Swrl),
        _ => return None,
    };
    Some(Spec { fields, category })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_budget() -> ScanBudget {
        ScanBudget::from_limits(&Limits::default())
    }

    #[test]
    fn scans_minimal_iri_and_rejects_nonminimal_tag() {
        let mut budget = default_budget();
        assert_eq!(
            scan_canonical(&[1, 2, 5, b'u', b'r', b'n', b':', b'x'], &mut budget).unwrap(),
            Category::Iri
        );
        let mut budget = default_budget();
        assert_eq!(
            scan_canonical(&[0x81, 0, 2, 5, b'u', b'r', b'n', b':', b'x'], &mut budget,)
                .unwrap_err()
                .code,
            "NATIVE_WIRE_CORRUPTION"
        );
    }

    #[test]
    fn accepts_arbitrarily_wide_canonical_integer() {
        // ObjectMinCardinality(integer, object property, class), with a 70-bit integer.
        let row = [
            38, 4, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 1, 1, 28, 2, 5, 15, 111, 98,
            106, 101, 99, 116, 95, 112, 114, 111, 112, 101, 114, 116, 121, 1, 8, 1, 2, 5, 117, 114,
            110, 58, 112, 1, 18, 2, 5, 5, 99, 108, 97, 115, 115, 1, 8, 1, 2, 5, 117, 114, 110, 58,
            67,
        ];
        let mut budget = default_budget();
        assert_eq!(scan_canonical(&row, &mut budget).unwrap(), Category::Term);
    }
}
