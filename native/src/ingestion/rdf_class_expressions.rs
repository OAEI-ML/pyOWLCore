//! Bounded reverse mapping for RDF boolean class expressions.

use crate::canonical::{anonymous, canonical_set, entity, iri, Field, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;

use super::rdf_lists::{RdfListDecoder, RdfResource, RdfTerm, RdfTriple, RDF_TYPE};

const OWL_CLASS: &str = "http://www.w3.org/2002/07/owl#Class";
const OWL_COMPLEMENT_OF: &str = "http://www.w3.org/2002/07/owl#complementOf";
const OWL_INTERSECTION_OF: &str = "http://www.w3.org/2002/07/owl#intersectionOf";
const OWL_ONE_OF: &str = "http://www.w3.org/2002/07/owl#oneOf";
const OWL_RESTRICTION: &str = "http://www.w3.org/2002/07/owl#Restriction";
const OWL_ON_PROPERTY: &str = "http://www.w3.org/2002/07/owl#onProperty";
const OWL_SOME_VALUES_FROM: &str = "http://www.w3.org/2002/07/owl#someValuesFrom";
const OWL_ALL_VALUES_FROM: &str = "http://www.w3.org/2002/07/owl#allValuesFrom";
const OWL_UNION_OF: &str = "http://www.w3.org/2002/07/owl#unionOf";

const ROLE_EXPRESSION: u8 = 1;
const ROLE_LIST: u8 = 2;
const ROLE_INDIVIDUAL: u8 = 4;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DecodedClassExpression {
    pub(crate) node: Node,
    pub(crate) consumed: Vec<usize>,
}

/// Retains recursion and RDF-list ownership state across every expression
/// decoded from one source graph.
pub(crate) struct RdfClassExpressionDecoder<'graph, 'data> {
    triples: &'graph [RdfTriple<'data>],
    lists: RdfListDecoder<'graph, 'data>,
    active: Vec<&'data str>,
    blank_roles: Vec<BlankRole<'data>>,
    data_properties: Vec<&'data str>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct BlankRole<'data> {
    label: &'data str,
    roles: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ClassConstructor {
    Boolean { index: usize, tag: u64 },
    Complement { index: usize },
    OneOf { index: usize },
    Restriction { marker: usize },
}

impl ClassConstructor {
    const fn index(self) -> usize {
        match self {
            Self::Boolean { index, .. } | Self::Complement { index } | Self::OneOf { index } => {
                index
            }
            Self::Restriction { marker } => marker,
        }
    }
}

impl<'graph, 'data> RdfClassExpressionDecoder<'graph, 'data> {
    pub(crate) fn new(triples: &'graph [RdfTriple<'data>]) -> Self {
        Self {
            triples,
            lists: RdfListDecoder::new(triples),
            active: Vec::new(),
            blank_roles: Vec::new(),
            data_properties: Vec::new(),
        }
    }

    pub(crate) fn register_data_property(
        &mut self,
        value: &'data str,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        session.step(usize_as_u64(
            self.data_properties.len(),
            "native RDF property-kind work exceeds u64",
        )?)?;
        if !self.data_properties.contains(&value) {
            reserve_item(&mut self.data_properties, session)?;
            self.data_properties.push(value);
        }
        Ok(())
    }

    pub(crate) fn decode_term(
        &mut self,
        value: RdfTerm<'data>,
        session: &mut Session<'_>,
    ) -> NativeResult<DecodedClassExpression> {
        let mut consumed = Vec::new();
        let node = self.decode_into(value, &mut consumed, session)?;
        consumed.sort_unstable();
        consumed.dedup();
        Ok(DecodedClassExpression { node, consumed })
    }

    fn decode_into(
        &mut self,
        value: RdfTerm<'data>,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        session.finish()?;
        match value {
            RdfTerm::Iri(value) => named_class(value, session),
            RdfTerm::Literal(_) => Err(unsupported(
                "native RDF class expression cannot be a literal",
            )),
            RdfTerm::Blank(value) => self.decode_blank(value, consumed, session),
        }
    }

    fn decode_blank(
        &mut self,
        value: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        self.claim_blank(value, ROLE_EXPRESSION, session)?;
        session.step(usize_as_u64(
            self.active.len(),
            "native RDF class-expression cycle work exceeds u64",
        )?)?;
        if self.active.contains(&value) {
            return Err(unsupported("native cyclic RDF structural expression"));
        }
        let next_depth = self
            .active
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF expression depth overflow"))?;
        if usize_as_u64(next_depth, "native RDF expression depth exceeds u64")?
            > session.limits().value(LimitKey::MaxNestingDepth)
        {
            return Err(NativeError::limit(
                "native RDF expression exceeds max_nesting_depth",
            ));
        }
        reserve_item(&mut self.active, session)?;
        self.active.push(value);
        let result = self.decode_constructor(value, consumed, session);
        self.active.pop();
        result
    }

    fn decode_constructor(
        &mut self,
        subject: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let complement = self.unique_edge(subject, OWL_COMPLEMENT_OF, session)?;
        let intersection = self.unique_edge(subject, OWL_INTERSECTION_OF, session)?;
        let one_of = self.unique_edge(subject, OWL_ONE_OF, session)?;
        let restriction = self.unique_marker(subject, OWL_RESTRICTION, session)?;
        let union = self.unique_edge(subject, OWL_UNION_OF, session)?;
        let constructor_count = usize::from(complement.is_some())
            .checked_add(usize::from(intersection.is_some()))
            .and_then(|value| value.checked_add(usize::from(one_of.is_some())))
            .and_then(|value| value.checked_add(usize::from(restriction.is_some())))
            .and_then(|value| value.checked_add(usize::from(union.is_some())))
            .ok_or_else(|| NativeError::limit("native RDF constructor count overflow"))?;
        if constructor_count != 1 {
            return Err(unsupported(if constructor_count == 0 {
                "native RDF blank node is not a recognized class expression"
            } else {
                "native RDF blank node has conflicting class constructors"
            }));
        }
        let constructor = if let Some(index) = intersection {
            ClassConstructor::Boolean { index, tag: 30 }
        } else if let Some(index) = union {
            ClassConstructor::Boolean { index, tag: 31 }
        } else if let Some(index) = complement {
            ClassConstructor::Complement { index }
        } else if let Some(index) = one_of {
            ClassConstructor::OneOf { index }
        } else if let Some(marker) = restriction {
            ClassConstructor::Restriction { marker }
        } else {
            return Err(NativeError::protocol(
                "native RDF constructor ledger is empty",
            ));
        };

        push_index(consumed, constructor.index(), session)?;
        if !matches!(constructor, ClassConstructor::Restriction { .. }) {
            self.consume_class_markers(subject, consumed, session)?;
        }
        let target = self.triples[constructor.index()].object;
        match constructor {
            ClassConstructor::Boolean { tag, .. } => {
                self.decode_boolean(target, tag, consumed, session)
            }
            ClassConstructor::Complement { .. } => {
                let operand = self.decode_into(target, consumed, session)?;
                let fields = reserved_fields([Field::Node(operand)], session)?;
                Node::build(32, fields)
            }
            ClassConstructor::OneOf { .. } => self.decode_one_of(target, consumed, session),
            ClassConstructor::Restriction { .. } => {
                self.decode_restriction(subject, consumed, session)
            }
        }
    }

    fn decode_boolean(
        &mut self,
        head: RdfTerm<'data>,
        tag: u64,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let decoded = self.lists.decode(head, session)?;
        let raw_length = decoded.items.len();
        if raw_length < 2 {
            return Err(unsupported(
                "native RDF boolean class expression has fewer than two operands",
            ));
        }
        for cell in &decoded.cells {
            self.claim_blank(cell, ROLE_LIST, session)?;
        }
        for index in decoded.consumed {
            push_index(consumed, index, session)?;
        }

        let mut operands = reserved_vec(raw_length, session)?;
        for item in decoded.items {
            operands.push(self.decode_into(item, consumed, session)?);
        }
        session.step(usize_as_u64(
            operands.len(),
            "native RDF canonical-set work exceeds u64",
        )?)?;
        session.finish()?;
        let mut operands = canonical_set(operands, 1, Some(tag))?;
        if operands.len() == 1 {
            return operands
                .pop()
                .ok_or_else(|| NativeError::protocol("native RDF operand ledger is empty"));
        }
        let fields = reserved_fields([Field::Set(operands)], session)?;
        let node = Node::build(tag, fields)?;
        session.finish()?;
        Ok(node)
    }

    fn decode_one_of(
        &mut self,
        head: RdfTerm<'data>,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let decoded = self.lists.decode(head, session)?;
        if decoded.items.is_empty() {
            return Err(unsupported(
                "native RDF object enumeration has no individuals",
            ));
        }
        for cell in &decoded.cells {
            self.claim_blank(cell, ROLE_LIST, session)?;
        }
        for index in decoded.consumed {
            push_index(consumed, index, session)?;
        }
        let mut individuals = reserved_vec(decoded.items.len(), session)?;
        for item in decoded.items {
            individuals.push(self.decode_individual(item, session)?);
        }
        session.step(usize_as_u64(
            individuals.len(),
            "native RDF individual-set work exceeds u64",
        )?)?;
        let individuals = canonical_set(individuals, 1, None)?;
        let fields = reserved_fields([Field::Set(individuals)], session)?;
        Node::build(33, fields)
    }

    fn decode_restriction(
        &mut self,
        subject: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let on_property = self
            .unique_edge(subject, OWL_ON_PROPERTY, session)?
            .ok_or_else(|| unsupported("native RDF restriction has no property selector"))?;
        let some = self.unique_edge(subject, OWL_SOME_VALUES_FROM, session)?;
        let all = self.unique_edge(subject, OWL_ALL_VALUES_FROM, session)?;
        let (quantifier, tag) = match (some, all) {
            (Some(index), None) => (index, 34),
            (None, Some(index)) => (index, 35),
            (Some(_), Some(_)) => {
                return Err(unsupported(
                    "native RDF restriction has conflicting quantifiers",
                ));
            }
            (None, None) => {
                return Err(unsupported(
                    "native RDF restriction has no supported quantifier",
                ));
            }
        };
        let property = match self.triples[on_property].object {
            RdfTerm::Iri(value) => value,
            RdfTerm::Blank(_) | RdfTerm::Literal(_) => {
                return Err(unsupported(
                    "native bounded RDF restriction requires a named object property",
                ));
            }
        };
        session.step(usize_as_u64(
            self.data_properties.len(),
            "native RDF property-kind work exceeds u64",
        )?)?;
        if self.data_properties.contains(&property) {
            return Err(unsupported(
                "native bounded RDF restriction does not map data properties",
            ));
        }

        push_index(consumed, on_property, session)?;
        push_index(consumed, quantifier, session)?;
        let filler = self.decode_into(self.triples[quantifier].object, consumed, session)?;
        let property = named_entity("object_property", property, session)?;
        let fields = reserved_fields([Field::Node(property), Field::Node(filler)], session)?;
        Node::build(tag, fields)
    }

    fn decode_individual(
        &mut self,
        value: RdfTerm<'data>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        match value {
            RdfTerm::Iri(value) => named_individual(value, session),
            RdfTerm::Blank(value) => {
                self.claim_blank(value, ROLE_INDIVIDUAL, session)?;
                session.reserve_bytes(value.len())?;
                anonymous(value)
            }
            RdfTerm::Literal(_) => Err(unsupported(
                "native RDF object enumeration item must be a resource",
            )),
        }
    }

    fn claim_blank(
        &mut self,
        label: &'data str,
        role: u8,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        session.step(usize_as_u64(
            self.blank_roles.len(),
            "native RDF blank-role work exceeds u64",
        )?)?;
        if let Some(record) = self
            .blank_roles
            .iter_mut()
            .find(|record| record.label == label)
        {
            let roles = record.roles | role;
            if roles & ROLE_INDIVIDUAL != 0 && roles != ROLE_INDIVIDUAL {
                return Err(unsupported("native RDF blank node has ambiguous roles"));
            }
            record.roles = roles;
            return Ok(());
        }
        reserve_item(&mut self.blank_roles, session)?;
        self.blank_roles.push(BlankRole { label, roles: role });
        Ok(())
    }

    fn unique_edge(
        &mut self,
        subject: &'data str,
        predicate: &str,
        session: &mut Session<'_>,
    ) -> NativeResult<Option<usize>> {
        let mut selected = None;
        for (index, triple) in self.triples.iter().enumerate() {
            session.step(1)?;
            if triple.subject == RdfResource::Blank(subject)
                && triple.predicate == predicate
                && selected.replace(index).is_some()
            {
                return Err(unsupported(
                    "native RDF class constructor has multiple targets",
                ));
            }
        }
        Ok(selected)
    }

    fn unique_marker(
        &mut self,
        subject: &'data str,
        object: &str,
        session: &mut Session<'_>,
    ) -> NativeResult<Option<usize>> {
        let mut selected = None;
        for (index, triple) in self.triples.iter().enumerate() {
            session.step(1)?;
            if triple.subject == RdfResource::Blank(subject)
                && triple.predicate == RDF_TYPE
                && triple.object == RdfTerm::Iri(object)
                && selected.replace(index).is_some()
            {
                return Err(unsupported(
                    "native RDF class marker is duplicated in the source graph",
                ));
            }
        }
        Ok(selected)
    }

    fn consume_class_markers(
        &mut self,
        subject: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        for (index, triple) in self.triples.iter().enumerate() {
            session.step(1)?;
            if triple.subject == RdfResource::Blank(subject)
                && triple.predicate == RDF_TYPE
                && triple.object == RdfTerm::Iri(OWL_CLASS)
            {
                push_index(consumed, index, session)?;
            }
        }
        Ok(())
    }
}

fn named_class(value: &str, session: &mut Session<'_>) -> NativeResult<Node> {
    named_entity("class", value, session)
}

fn named_individual(value: &str, session: &mut Session<'_>) -> NativeResult<Node> {
    named_entity("named_individual", value, session)
}

fn named_entity(kind: &'static str, value: &str, session: &mut Session<'_>) -> NativeResult<Node> {
    super::check_iri(
        value,
        session,
        "native RDF expression IRI exceeds max_iri_bytes",
    )?;
    session.reserve_bytes(value.len())?;
    let mut owned = String::new();
    owned
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native RDF class IRI allocation failed"))?;
    owned.push_str(value);
    entity(kind, iri(owned)?)
}

fn push_index(
    consumed: &mut Vec<usize>,
    index: usize,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    reserve_item(consumed, session)?;
    consumed.push(index);
    Ok(())
}

fn reserved_vec<T>(count: usize, session: &mut Session<'_>) -> NativeResult<Vec<T>> {
    let bytes = count
        .checked_mul(std::mem::size_of::<T>())
        .ok_or_else(|| NativeError::limit("native RDF expression allocation overflow"))?;
    session.reserve_bytes(bytes)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(count)
        .map_err(|_| NativeError::limit("native RDF expression allocation failed"))?;
    Ok(output)
}

fn reserved_fields<const N: usize>(
    values: [Field; N],
    session: &mut Session<'_>,
) -> NativeResult<Vec<Field>> {
    let mut output = reserved_vec(N, session)?;
    output.extend(values);
    Ok(output)
}

fn reserve_item<T>(values: &mut Vec<T>, session: &mut Session<'_>) -> NativeResult<()> {
    if values.len() == values.capacity() {
        session.reserve_bytes(std::mem::size_of::<T>())?;
        values
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native RDF expression allocation failed"))?;
    }
    Ok(())
}

fn usize_as_u64(value: usize, message: &'static str) -> NativeResult<u64> {
    u64::try_from(value).map_err(|_| NativeError::limit(message))
}

fn unsupported(message: &'static str) -> NativeError {
    NativeError::new("NATIVE_RDF_MAPPING_UNSUPPORTED", message)
}

#[cfg(test)]
mod tests {
    use super::super::rdf_lists::{RDF_FIRST, RDF_NIL, RDF_REST};
    use super::*;
    use crate::cancel::{Cancellation, Guard};
    use crate::limits::Limits;

    fn edge(
        subject: &'static str,
        predicate: &'static str,
        object: RdfTerm<'static>,
    ) -> RdfTriple<'static> {
        RdfTriple {
            subject: RdfResource::Blank(subject),
            predicate,
            object,
        }
    }

    fn iri_term(value: &'static str) -> RdfTerm<'static> {
        RdfTerm::Iri(value)
    }

    fn blank_term(value: &'static str) -> RdfTerm<'static> {
        RdfTerm::Blank(value)
    }

    fn decode(
        graph: &[RdfTriple<'static>],
        value: RdfTerm<'static>,
    ) -> NativeResult<DecodedClassExpression> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0)?;
        RdfClassExpressionDecoder::new(graph).decode_term(value, &mut session)
    }

    #[test]
    fn intersection_and_union_match_canonical_constructor_tags() {
        for (predicate, tag) in [(OWL_INTERSECTION_OF, 30), (OWL_UNION_OF, 31)] {
            let graph = [
                edge("e", RDF_TYPE, iri_term(OWL_CLASS)),
                edge("e", predicate, blank_term("h")),
                edge("h", RDF_FIRST, iri_term("urn:b")),
                edge("h", RDF_REST, blank_term("t")),
                edge("t", RDF_FIRST, iri_term("urn:a")),
                edge("t", RDF_REST, iri_term(RDF_NIL)),
            ];
            let decoded = decode(&graph, blank_term("e")).expect("boolean class expression");
            let mut operands = vec![
                entity("class", iri("urn:a".to_owned()).unwrap()).unwrap(),
                entity("class", iri("urn:b".to_owned()).unwrap()).unwrap(),
            ];
            operands = canonical_set(operands, 2, Some(tag)).unwrap();
            let expected = Node::build(tag, vec![Field::Set(operands)]).unwrap();
            assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
            assert_eq!(decoded.consumed, [0, 1, 2, 3, 4, 5]);
        }
    }

    #[test]
    fn duplicate_operands_canonicalize_to_the_sole_class() {
        let graph = [
            edge("e", OWL_INTERSECTION_OF, blank_term("h")),
            edge("h", RDF_FIRST, iri_term("urn:a")),
            edge("h", RDF_REST, blank_term("t")),
            edge("t", RDF_FIRST, iri_term("urn:a")),
            edge("t", RDF_REST, iri_term(RDF_NIL)),
        ];
        let decoded = decode(&graph, blank_term("e")).expect("idempotent expression");
        let expected = entity("class", iri("urn:a".to_owned()).unwrap()).unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
    }

    #[test]
    fn complement_and_one_of_match_canonical_constructor_tags() {
        let complement_graph = [edge("e", OWL_COMPLEMENT_OF, iri_term("urn:a"))];
        let complement =
            decode(&complement_graph, blank_term("e")).expect("object complement expression");
        let expected_complement = Node::build(
            32,
            vec![Field::Node(
                entity("class", iri("urn:a".to_owned()).unwrap()).unwrap(),
            )],
        )
        .unwrap();
        assert_eq!(complement.node.as_bytes(), expected_complement.as_bytes());
        assert_eq!(complement.consumed, [0]);

        let one_of_graph = [
            edge("e", OWL_ONE_OF, blank_term("h")),
            edge("h", RDF_FIRST, iri_term("urn:i")),
            edge("h", RDF_REST, blank_term("t")),
            edge("t", RDF_FIRST, blank_term("anonymous")),
            edge("t", RDF_REST, iri_term(RDF_NIL)),
        ];
        let one_of = decode(&one_of_graph, blank_term("e")).expect("object enumeration");
        let individuals = canonical_set(
            vec![
                entity("named_individual", iri("urn:i".to_owned()).unwrap()).unwrap(),
                anonymous("anonymous").unwrap(),
            ],
            1,
            None,
        )
        .unwrap();
        let expected_one_of = Node::build(33, vec![Field::Set(individuals)]).unwrap();
        assert_eq!(one_of.node.as_bytes(), expected_one_of.as_bytes());
        assert_eq!(one_of.consumed, [0, 1, 2, 3, 4]);
    }

    #[test]
    fn named_object_quantified_restrictions_match_canonical_tags() {
        for (predicate, tag) in [(OWL_SOME_VALUES_FROM, 34), (OWL_ALL_VALUES_FROM, 35)] {
            let graph = [
                edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
                edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
                edge("e", predicate, iri_term("urn:Filler")),
            ];
            let decoded = decode(&graph, blank_term("e")).expect("quantified restriction");
            let expected = Node::build(
                tag,
                vec![
                    Field::Node(
                        entity("object_property", iri("urn:p".to_owned()).unwrap()).unwrap(),
                    ),
                    Field::Node(entity("class", iri("urn:Filler".to_owned()).unwrap()).unwrap()),
                ],
            )
            .unwrap();
            assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
            assert_eq!(decoded.consumed, [0, 1, 2]);
        }
    }

    #[test]
    fn declared_data_property_is_not_misclassified_as_object_restriction() {
        let graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:data")),
            edge("e", OWL_SOME_VALUES_FROM, iri_term("urn:Filler")),
        ];
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut decoder = RdfClassExpressionDecoder::new(&graph);
        decoder
            .register_data_property("urn:data", &mut session)
            .expect("data property kind");
        assert_eq!(
            decoder
                .decode_term(blank_term("e"), &mut session)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
    }

    #[test]
    fn constructor_conflicts_expression_cycles_and_literals_fail_closed() {
        let conflict = [
            edge("e", OWL_INTERSECTION_OF, iri_term(RDF_NIL)),
            edge("e", OWL_UNION_OF, iri_term(RDF_NIL)),
        ];
        let cycle = [
            edge("e", OWL_UNION_OF, blank_term("h")),
            edge("h", RDF_FIRST, blank_term("e")),
            edge("h", RDF_REST, blank_term("t")),
            edge("t", RDF_FIRST, iri_term("urn:a")),
            edge("t", RDF_REST, iri_term(RDF_NIL)),
        ];
        let complement_literal = [edge("e", OWL_COMPLEMENT_OF, RdfTerm::Literal("bad"))];
        let ambiguous_individual = [
            edge("e", OWL_ONE_OF, blank_term("h")),
            edge("h", RDF_FIRST, blank_term("e")),
            edge("h", RDF_REST, iri_term(RDF_NIL)),
        ];
        let conflicting_quantifiers = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_SOME_VALUES_FROM, iri_term("urn:A")),
            edge("e", OWL_ALL_VALUES_FROM, iri_term("urn:B")),
        ];
        let blank_property = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, blank_term("p")),
            edge("e", OWL_SOME_VALUES_FROM, iri_term("urn:A")),
        ];
        for (graph, value) in [
            (conflict.as_slice(), blank_term("e")),
            (cycle.as_slice(), blank_term("e")),
            (complement_literal.as_slice(), blank_term("e")),
            (ambiguous_individual.as_slice(), blank_term("e")),
            (conflicting_quantifiers.as_slice(), blank_term("e")),
            (blank_property.as_slice(), blank_term("e")),
            (&[][..], RdfTerm::Literal("not-a-class")),
        ] {
            assert_eq!(
                decode(graph, value).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }
    }
}
