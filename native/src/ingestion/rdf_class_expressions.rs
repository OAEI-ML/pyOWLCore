//! Bounded reverse mapping for RDF boolean class expressions.

use crate::canonical::{anonymous, canonical_set, entity, iri, literal, Field, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::model::{scan_canonical, Category, ScanBudget};
use crate::session::Session;

use super::rdf_lists::{RdfListDecoder, RdfResource, RdfTerm, RdfTriple, RDF_FIRST, RDF_TYPE};

const OWL_CLASS: &str = "http://www.w3.org/2002/07/owl#Class";
const OWL_COMPLEMENT_OF: &str = "http://www.w3.org/2002/07/owl#complementOf";
const OWL_DATATYPE_COMPLEMENT_OF: &str = "http://www.w3.org/2002/07/owl#datatypeComplementOf";
const OWL_INTERSECTION_OF: &str = "http://www.w3.org/2002/07/owl#intersectionOf";
const OWL_ONE_OF: &str = "http://www.w3.org/2002/07/owl#oneOf";
const OWL_RESTRICTION: &str = "http://www.w3.org/2002/07/owl#Restriction";
const OWL_ON_PROPERTY: &str = "http://www.w3.org/2002/07/owl#onProperty";
const OWL_SOME_VALUES_FROM: &str = "http://www.w3.org/2002/07/owl#someValuesFrom";
const OWL_ALL_VALUES_FROM: &str = "http://www.w3.org/2002/07/owl#allValuesFrom";
const OWL_HAS_VALUE: &str = "http://www.w3.org/2002/07/owl#hasValue";
const OWL_HAS_SELF: &str = "http://www.w3.org/2002/07/owl#hasSelf";
const OWL_MIN_CARDINALITY: &str = "http://www.w3.org/2002/07/owl#minCardinality";
const OWL_MAX_CARDINALITY: &str = "http://www.w3.org/2002/07/owl#maxCardinality";
const OWL_CARDINALITY: &str = "http://www.w3.org/2002/07/owl#cardinality";
const OWL_MIN_QUALIFIED_CARDINALITY: &str = "http://www.w3.org/2002/07/owl#minQualifiedCardinality";
const OWL_MAX_QUALIFIED_CARDINALITY: &str = "http://www.w3.org/2002/07/owl#maxQualifiedCardinality";
const OWL_QUALIFIED_CARDINALITY: &str = "http://www.w3.org/2002/07/owl#qualifiedCardinality";
const OWL_ON_CLASS: &str = "http://www.w3.org/2002/07/owl#onClass";
const OWL_ON_DATA_RANGE: &str = "http://www.w3.org/2002/07/owl#onDataRange";
const OWL_ON_DATATYPE: &str = "http://www.w3.org/2002/07/owl#onDatatype";
const OWL_THING: &str = "http://www.w3.org/2002/07/owl#Thing";
const OWL_UNION_OF: &str = "http://www.w3.org/2002/07/owl#unionOf";
const OWL_WITH_RESTRICTIONS: &str = "http://www.w3.org/2002/07/owl#withRestrictions";
const RDF_PLAIN_LITERAL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
const RDFS_LITERAL: &str = "http://www.w3.org/2000/01/rdf-schema#Literal";
const RDFS_DATATYPE: &str = "http://www.w3.org/2000/01/rdf-schema#Datatype";
const XSD_STRING: &str = "http://www.w3.org/2001/XMLSchema#string";

const ROLE_EXPRESSION: u8 = 1;
const ROLE_LIST: u8 = 2;
const ROLE_INDIVIDUAL: u8 = 4;
const ROLE_FACET: u8 = 8;

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
    active_data: Vec<&'data str>,
    blank_roles: Vec<BlankRole<'data>>,
    data_properties: Vec<&'data str>,
    datatypes: Vec<&'data str>,
    literals: Vec<RdfLiteralMetadata<'data>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct BlankRole<'data> {
    label: &'data str,
    roles: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct RdfLiteralMetadata<'data> {
    triple_index: usize,
    datatype: Option<&'data str>,
    language: Option<&'data str>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ClassConstructor {
    Boolean { index: usize, tag: u64 },
    Complement { index: usize },
    OneOf { index: usize },
    Restriction { marker: usize },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RestrictionOperator {
    Quantified {
        index: usize,
        tag: u64,
    },
    HasValue {
        index: usize,
    },
    HasSelf {
        index: usize,
    },
    Cardinality {
        index: usize,
        tag: u64,
        qualified: bool,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DataRangeConstructor {
    Boolean {
        index: usize,
        tag: u64,
    },
    Complement {
        index: usize,
    },
    OneOf {
        index: usize,
    },
    DatatypeRestriction {
        on_datatype: usize,
        with_restrictions: usize,
    },
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
            active_data: Vec::new(),
            blank_roles: Vec::new(),
            data_properties: Vec::new(),
            datatypes: Vec::new(),
            literals: Vec::new(),
        }
    }

    pub(crate) fn register_data_property(
        &mut self,
        value: &'data str,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        register_kind_value(&mut self.data_properties, value, session)
    }

    pub(crate) fn register_datatype(
        &mut self,
        value: &'data str,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        register_kind_value(&mut self.datatypes, value, session)
    }

    pub(crate) fn register_literal(
        &mut self,
        triple_index: usize,
        datatype: Option<&'data str>,
        language: Option<&'data str>,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        let triple = self
            .triples
            .get(triple_index)
            .ok_or_else(|| NativeError::protocol("native RDF literal index exceeds graph"))?;
        if !matches!(triple.object, RdfTerm::Literal(_)) {
            return Err(NativeError::protocol(
                "native RDF literal metadata targets a resource",
            ));
        }
        session.step(usize_as_u64(
            self.literals.len(),
            "native RDF literal-metadata work exceeds u64",
        )?)?;
        if self
            .literals
            .iter()
            .any(|metadata| metadata.triple_index == triple_index)
        {
            return Err(NativeError::protocol(
                "native RDF literal metadata is duplicated",
            ));
        }
        reserve_item(&mut self.literals, session)?;
        self.literals.push(RdfLiteralMetadata {
            triple_index,
            datatype,
            language,
        });
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

    fn decode_data_range(
        &mut self,
        value: RdfTerm<'data>,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        session.finish()?;
        match value {
            RdfTerm::Iri(value) => named_entity("datatype", value, session),
            RdfTerm::Blank(value) => self.decode_data_blank(value, consumed, session),
            RdfTerm::Literal(_) => Err(unsupported(
                "native RDF data restriction filler cannot be a literal",
            )),
        }
    }

    fn decode_data_blank(
        &mut self,
        value: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        self.claim_blank(value, ROLE_EXPRESSION, session)?;
        let active_length = self
            .active
            .len()
            .checked_add(self.active_data.len())
            .ok_or_else(|| NativeError::limit("native RDF data-range cycle work overflow"))?;
        session.step(usize_as_u64(
            active_length,
            "native RDF data-range cycle work exceeds u64",
        )?)?;
        if self.active.contains(&value) || self.active_data.contains(&value) {
            return Err(unsupported("native cyclic RDF structural expression"));
        }
        let next_depth = active_length
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF expression depth overflow"))?;
        if usize_as_u64(next_depth, "native RDF expression depth exceeds u64")?
            > session.limits().value(LimitKey::MaxNestingDepth)
        {
            return Err(NativeError::limit(
                "native RDF expression exceeds max_nesting_depth",
            ));
        }
        reserve_item(&mut self.active_data, session)?;
        self.active_data.push(value);
        let result = self.decode_data_constructor(value, consumed, session);
        self.active_data.pop();
        result
    }

    fn decode_data_constructor(
        &mut self,
        subject: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let intersection = self.unique_edge(subject, OWL_INTERSECTION_OF, session)?;
        let union = self.unique_edge(subject, OWL_UNION_OF, session)?;
        let complement = self.unique_edge(subject, OWL_DATATYPE_COMPLEMENT_OF, session)?;
        let one_of = self.unique_edge(subject, OWL_ONE_OF, session)?;
        let on_datatype = self.unique_edge(subject, OWL_ON_DATATYPE, session)?;
        let with_restrictions = self.unique_edge(subject, OWL_WITH_RESTRICTIONS, session)?;
        let marker = self.unique_marker(subject, RDFS_DATATYPE, session)?;
        let datatype_restriction = on_datatype.is_some() || with_restrictions.is_some();
        let constructor_count = [
            intersection.is_some(),
            union.is_some(),
            complement.is_some(),
            one_of.is_some(),
            datatype_restriction,
        ]
        .into_iter()
        .filter(|value| *value)
        .count();
        if constructor_count != 1 {
            return Err(unsupported(if constructor_count == 0 {
                "native RDF blank node is not a recognized data range"
            } else {
                "native RDF blank node has conflicting data-range constructors"
            }));
        }
        let constructor = if let Some(index) = intersection {
            DataRangeConstructor::Boolean { index, tag: 21 }
        } else if let Some(index) = union {
            DataRangeConstructor::Boolean { index, tag: 22 }
        } else if let Some(index) = complement {
            DataRangeConstructor::Complement { index }
        } else if let Some(index) = one_of {
            DataRangeConstructor::OneOf { index }
        } else {
            match (on_datatype, with_restrictions) {
                (Some(on_datatype), Some(with_restrictions)) => {
                    DataRangeConstructor::DatatypeRestriction {
                        on_datatype,
                        with_restrictions,
                    }
                }
                (Some(_), None) | (None, Some(_)) => {
                    return Err(unsupported("native RDF datatype restriction is incomplete"));
                }
                (None, None) => {
                    return Err(NativeError::protocol(
                        "native RDF data-range constructor ledger is empty",
                    ));
                }
            }
        };
        if let Some(marker) = marker {
            push_index(consumed, marker, session)?;
        }
        match constructor {
            DataRangeConstructor::Boolean { index, tag } => {
                push_index(consumed, index, session)?;
                let target = self.triples[index].object;
                self.decode_data_boolean(target, tag, consumed, session)
            }
            DataRangeConstructor::Complement { index } => {
                push_index(consumed, index, session)?;
                let target = self.triples[index].object;
                let operand = self.decode_data_range(target, consumed, session)?;
                let fields = reserved_fields([Field::Node(operand)], session)?;
                Node::build(23, fields)
            }
            DataRangeConstructor::OneOf { index } => {
                push_index(consumed, index, session)?;
                let target = self.triples[index].object;
                self.decode_data_one_of(target, consumed, session)
            }
            DataRangeConstructor::DatatypeRestriction {
                on_datatype,
                with_restrictions,
            } => {
                self.decode_datatype_restriction(on_datatype, with_restrictions, consumed, session)
            }
        }
    }

    fn decode_data_boolean(
        &mut self,
        head: RdfTerm<'data>,
        tag: u64,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let decoded = self.lists.decode(head, session)?;
        if decoded.items.len() < 2 {
            return Err(unsupported(
                "native RDF boolean data range has fewer than two operands",
            ));
        }
        for cell in &decoded.cells {
            self.claim_blank(cell, ROLE_LIST, session)?;
        }
        for index in decoded.consumed {
            push_index(consumed, index, session)?;
        }
        let mut operands = reserved_vec(decoded.items.len(), session)?;
        for item in decoded.items {
            operands.push(self.decode_data_range(item, consumed, session)?);
        }
        session.step(usize_as_u64(
            operands.len(),
            "native RDF canonical-set work exceeds u64",
        )?)?;
        let mut operands = canonical_set(operands, 1, Some(tag))?;
        if operands.len() == 1 {
            return operands
                .pop()
                .ok_or_else(|| NativeError::protocol("native RDF data-range ledger is empty"));
        }
        let fields = reserved_fields([Field::Set(operands)], session)?;
        Node::build(tag, fields)
    }

    fn decode_data_one_of(
        &mut self,
        head: RdfTerm<'data>,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let decoded = self.lists.decode(head, session)?;
        if decoded.items.is_empty() || decoded.items.len() != decoded.cells.len() {
            return Err(unsupported(
                "native RDF data enumeration has no literal values",
            ));
        }
        for cell in &decoded.cells {
            self.claim_blank(cell, ROLE_LIST, session)?;
        }
        for index in decoded.consumed {
            push_index(consumed, index, session)?;
        }
        let mut values = reserved_vec(decoded.items.len(), session)?;
        for (cell, item) in decoded.cells.into_iter().zip(decoded.items) {
            if !matches!(item, RdfTerm::Literal(_)) {
                return Err(unsupported(
                    "native RDF data enumeration item must be a literal",
                ));
            }
            let first = self
                .unique_edge(cell, RDF_FIRST, session)?
                .ok_or_else(|| NativeError::protocol("native RDF list item ledger is empty"))?;
            values.push(self.decode_literal(first, session)?);
        }
        session.step(usize_as_u64(
            values.len(),
            "native RDF literal-set work exceeds u64",
        )?)?;
        let values = canonical_set(values, 1, None)?;
        let fields = reserved_fields([Field::Set(values)], session)?;
        Node::build(24, fields)
    }

    fn decode_datatype_restriction(
        &mut self,
        on_datatype: usize,
        with_restrictions: usize,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let datatype = match self.triples[on_datatype].object {
            RdfTerm::Iri(value) => named_entity("datatype", value, session)?,
            RdfTerm::Blank(_) | RdfTerm::Literal(_) => {
                return Err(unsupported(
                    "native RDF datatype restriction requires a named datatype",
                ));
            }
        };
        let decoded = self
            .lists
            .decode(self.triples[with_restrictions].object, session)?;
        if decoded.items.is_empty() {
            return Err(unsupported("native RDF datatype restriction has no facets"));
        }
        for cell in &decoded.cells {
            self.claim_blank(cell, ROLE_LIST, session)?;
        }
        push_index(consumed, on_datatype, session)?;
        push_index(consumed, with_restrictions, session)?;
        for index in decoded.consumed {
            push_index(consumed, index, session)?;
        }
        let mut facets = reserved_vec(decoded.items.len(), session)?;
        for item in decoded.items {
            let RdfTerm::Blank(item) = item else {
                return Err(unsupported(
                    "native RDF facet restriction list item must be blank",
                ));
            };
            self.claim_blank(item, ROLE_FACET, session)?;
            let facet_index = self
                .unique_literal_edge(item, session)?
                .ok_or_else(|| unsupported("native RDF facet restriction has no literal value"))?;
            push_index(consumed, facet_index, session)?;
            let facet_iri = iri_node(self.triples[facet_index].predicate, session)?;
            let value = self.decode_literal(facet_index, session)?;
            let fields = reserved_fields([Field::Node(facet_iri), Field::Node(value)], session)?;
            facets.push(Node::build(20, fields)?);
        }
        session.step(usize_as_u64(
            facets.len(),
            "native RDF facet-set work exceeds u64",
        )?)?;
        let facets = canonical_set(facets, 1, None)?;
        let fields = reserved_fields([Field::Node(datatype), Field::Set(facets)], session)?;
        Node::build(25, fields)
    }

    fn unique_literal_edge(
        &mut self,
        subject: &'data str,
        session: &mut Session<'_>,
    ) -> NativeResult<Option<usize>> {
        let mut selected = None;
        for (index, triple) in self.triples.iter().enumerate() {
            session.step(1)?;
            if triple.subject == RdfResource::Blank(subject)
                && matches!(triple.object, RdfTerm::Literal(_))
                && selected.replace(index).is_some()
            {
                return Err(unsupported(
                    "native RDF facet restriction has multiple literal values",
                ));
            }
        }
        Ok(selected)
    }

    fn restriction_uses_data_range(
        &self,
        property: &str,
        filler: RdfTerm<'data>,
        session: &mut Session<'_>,
    ) -> NativeResult<bool> {
        session.step(usize_as_u64(
            self.data_properties.len(),
            "native RDF property-kind work exceeds u64",
        )?)?;
        if self.data_properties.contains(&property) {
            return Ok(true);
        }
        session.step(usize_as_u64(
            self.datatypes.len(),
            "native RDF datatype-kind work exceeds u64",
        )?)?;
        match filler {
            RdfTerm::Iri(value) => Ok(self.datatypes.contains(&value)),
            RdfTerm::Blank(value) => {
                for triple in self.triples {
                    session.step(1)?;
                    if triple.subject == RdfResource::Blank(value)
                        && (triple.predicate == OWL_ON_DATATYPE
                            || (triple.predicate == RDF_TYPE
                                && triple.object == RdfTerm::Iri(RDFS_DATATYPE)))
                    {
                        return Ok(true);
                    }
                }
                Ok(false)
            }
            RdfTerm::Literal(_) => Ok(false),
        }
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
        let has_value = self.unique_edge(subject, OWL_HAS_VALUE, session)?;
        let has_self = self.unique_edge(subject, OWL_HAS_SELF, session)?;
        let min = self.unique_edge(subject, OWL_MIN_CARDINALITY, session)?;
        let max = self.unique_edge(subject, OWL_MAX_CARDINALITY, session)?;
        let exact = self.unique_edge(subject, OWL_CARDINALITY, session)?;
        let min_qualified = self.unique_edge(subject, OWL_MIN_QUALIFIED_CARDINALITY, session)?;
        let max_qualified = self.unique_edge(subject, OWL_MAX_QUALIFIED_CARDINALITY, session)?;
        let exact_qualified = self.unique_edge(subject, OWL_QUALIFIED_CARDINALITY, session)?;
        let operator_count = [
            some,
            all,
            has_value,
            has_self,
            min,
            max,
            exact,
            min_qualified,
            max_qualified,
            exact_qualified,
        ]
        .into_iter()
        .flatten()
        .count();
        if operator_count != 1 {
            return Err(unsupported(if operator_count == 0 {
                "native RDF restriction has no supported operator"
            } else {
                "native RDF restriction has conflicting operators"
            }));
        }
        let operator = if let Some(index) = some {
            RestrictionOperator::Quantified { index, tag: 34 }
        } else if let Some(index) = all {
            RestrictionOperator::Quantified { index, tag: 35 }
        } else if let Some(index) = has_value {
            RestrictionOperator::HasValue { index }
        } else if let Some(index) = has_self {
            RestrictionOperator::HasSelf { index }
        } else if let Some(index) = min {
            RestrictionOperator::Cardinality {
                index,
                tag: 38,
                qualified: false,
            }
        } else if let Some(index) = max {
            RestrictionOperator::Cardinality {
                index,
                tag: 39,
                qualified: false,
            }
        } else if let Some(index) = exact {
            RestrictionOperator::Cardinality {
                index,
                tag: 40,
                qualified: false,
            }
        } else if let Some(index) = min_qualified {
            RestrictionOperator::Cardinality {
                index,
                tag: 38,
                qualified: true,
            }
        } else if let Some(index) = max_qualified {
            RestrictionOperator::Cardinality {
                index,
                tag: 39,
                qualified: true,
            }
        } else if let Some(index) = exact_qualified {
            RestrictionOperator::Cardinality {
                index,
                tag: 40,
                qualified: true,
            }
        } else {
            return Err(NativeError::protocol(
                "native RDF restriction operator ledger is empty",
            ));
        };
        let property_iri = match self.triples[on_property].object {
            RdfTerm::Iri(value) => value,
            RdfTerm::Blank(_) | RdfTerm::Literal(_) => {
                return Err(unsupported(
                    "native bounded RDF restriction requires a named object property",
                ));
            }
        };
        push_index(consumed, on_property, session)?;
        match operator {
            RestrictionOperator::Quantified { index, tag } => {
                push_index(consumed, index, session)?;
                let filler_term = self.triples[index].object;
                if self.restriction_uses_data_range(property_iri, filler_term, session)? {
                    let property = named_entity("data_property", property_iri, session)?;
                    let mut properties = reserved_vec(1, session)?;
                    properties.push(property);
                    let filler = self.decode_data_range(filler_term, consumed, session)?;
                    let fields = reserved_fields(
                        [Field::Sequence(properties), Field::Node(filler)],
                        session,
                    )?;
                    Node::build(if tag == 34 { 41 } else { 42 }, fields)
                } else {
                    let filler = self.decode_into(filler_term, consumed, session)?;
                    let property = named_entity("object_property", property_iri, session)?;
                    let fields =
                        reserved_fields([Field::Node(property), Field::Node(filler)], session)?;
                    Node::build(tag, fields)
                }
            }
            RestrictionOperator::HasValue { index } => {
                push_index(consumed, index, session)?;
                if matches!(self.triples[index].object, RdfTerm::Literal(_)) {
                    let value = self.decode_literal(index, session)?;
                    let property = named_entity("data_property", property_iri, session)?;
                    let fields =
                        reserved_fields([Field::Node(property), Field::Node(value)], session)?;
                    Node::build(43, fields)
                } else {
                    let individual = self.decode_individual(self.triples[index].object, session)?;
                    let property = named_entity("object_property", property_iri, session)?;
                    let fields =
                        reserved_fields([Field::Node(property), Field::Node(individual)], session)?;
                    Node::build(36, fields)
                }
            }
            RestrictionOperator::HasSelf { index } => {
                if !matches!(
                    self.triples[index].object,
                    RdfTerm::Literal(value) if value.eq_ignore_ascii_case("true")
                ) {
                    return Err(unsupported("native RDF owl:hasSelf value must be true"));
                }
                push_index(consumed, index, session)?;
                let property = named_entity("object_property", property_iri, session)?;
                let fields = reserved_fields([Field::Node(property)], session)?;
                Node::build(37, fields)
            }
            RestrictionOperator::Cardinality {
                index,
                tag,
                qualified,
            } => self.decode_cardinality(
                subject,
                property_iri,
                index,
                tag,
                qualified,
                consumed,
                session,
            ),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn decode_cardinality(
        &mut self,
        subject: &'data str,
        property_iri: &'data str,
        cardinality_index: usize,
        tag: u64,
        qualified: bool,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let cardinality = nonnegative_integer(self.triples[cardinality_index].object, session)?;
        let on_class = self.unique_edge(subject, OWL_ON_CLASS, session)?;
        let on_data = self.unique_edge(subject, OWL_ON_DATA_RANGE, session)?;
        session.step(usize_as_u64(
            self.data_properties.len(),
            "native RDF property-kind work exceeds u64",
        )?)?;
        let declared_data = self.data_properties.contains(&property_iri);
        let (filler_index, data_cardinality) = if qualified {
            match (on_class, on_data) {
                (Some(index), None) => (Some(index), false),
                (None, Some(index)) => (Some(index), true),
                (Some(_), Some(_)) | (None, None) => {
                    return Err(unsupported(
                        "native qualified RDF cardinality requires one qualified filler",
                    ));
                }
            }
        } else {
            if on_class.is_some() && (on_data.is_some() || declared_data) {
                return Err(unsupported(
                    "native RDF cardinality has conflicting object and data selectors",
                ));
            }
            match (on_class, on_data) {
                (Some(index), None) => (Some(index), false),
                (None, Some(index)) => (Some(index), true),
                (None, None) => (None, declared_data),
                (Some(_), Some(_)) => {
                    return Err(unsupported(
                        "native RDF cardinality has conflicting qualified fillers",
                    ));
                }
            }
        };

        push_index(consumed, cardinality_index, session)?;
        let filler_term = filler_index.map(|index| self.triples[index].object);
        if let Some(index) = filler_index {
            push_index(consumed, index, session)?;
        }
        let (property, filler, tag) = if data_cardinality {
            let filler = if let Some(value) = filler_term {
                self.decode_data_range(value, consumed, session)?
            } else {
                named_entity("datatype", RDFS_LITERAL, session)?
            };
            (
                named_entity("data_property", property_iri, session)?,
                filler,
                tag + 6,
            )
        } else {
            let filler = if let Some(value) = filler_term {
                self.decode_into(value, consumed, session)?
            } else {
                named_class(OWL_THING, session)?
            };
            (
                named_entity("object_property", property_iri, session)?,
                filler,
                tag,
            )
        };
        let fields = reserved_fields(
            [
                Field::Integer(cardinality),
                Field::Node(property),
                Field::Node(filler),
            ],
            session,
        )?;
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
            RdfTerm::Literal(_) => Err(unsupported("native RDF individual must be a resource")),
        }
    }

    fn decode_literal(
        &mut self,
        triple_index: usize,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let RdfTerm::Literal(lexical) = self
            .triples
            .get(triple_index)
            .ok_or_else(|| NativeError::protocol("native RDF literal index exceeds graph"))?
            .object
        else {
            return Err(NativeError::protocol(
                "native RDF literal decoder received a resource",
            ));
        };
        session.step(usize_as_u64(
            self.literals.len(),
            "native RDF literal-metadata work exceeds u64",
        )?)?;
        let metadata = self
            .literals
            .iter()
            .find(|metadata| metadata.triple_index == triple_index)
            .copied()
            .ok_or_else(|| unsupported("native RDF literal metadata is unavailable"))?;
        if metadata.datatype.is_some() && metadata.language.is_some() {
            return Err(unsupported(
                "native RDF literal cannot select both datatype and language",
            ));
        }
        let (lexical, datatype, language) = if let Some(language) = metadata.language {
            (
                lexical,
                RDF_PLAIN_LITERAL,
                Some(owned_lowercase(language, session)?),
            )
        } else {
            let datatype = metadata.datatype.unwrap_or(XSD_STRING);
            let lexical = if datatype == RDF_PLAIN_LITERAL {
                lexical.strip_suffix('@').unwrap_or(lexical)
            } else {
                lexical
            };
            (lexical, datatype, None)
        };
        if usize_as_u64(lexical.len(), "native RDF literal length exceeds u64")?
            > session.limits().value(LimitKey::MaxLiteralBytes)
        {
            return Err(NativeError::limit(
                "native RDF literal exceeds max_literal_bytes",
            ));
        }
        let lexical = owned_value(lexical, session, "native RDF literal allocation failed")?;
        let datatype = named_entity("datatype", datatype, session)?;
        let value = literal(lexical, datatype, language)?;
        session.step(usize_as_u64(
            value.as_bytes().len(),
            "native RDF literal validation work exceeds u64",
        )?)?;
        let mut budget = ScanBudget::from_limits(session.limits());
        match scan_canonical(value.as_bytes(), &mut budget) {
            Ok(Category::Literal) => Ok(value),
            Ok(_) => Err(NativeError::protocol(
                "native RDF literal has the wrong canonical category",
            )),
            Err(error) if error.code == "NATIVE_WIRE_CORRUPTION" => Err(unsupported(
                "native RDF literal violates the structural model",
            )),
            Err(error) => Err(error),
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
            if (roles & ROLE_INDIVIDUAL != 0 && roles != ROLE_INDIVIDUAL)
                || (roles & ROLE_FACET != 0 && roles != ROLE_FACET)
            {
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

fn owned_lowercase(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    let mut output = owned_value(value, session, "native RDF language-tag allocation failed")?;
    output.make_ascii_lowercase();
    Ok(output)
}

fn owned_value(
    value: &str,
    session: &mut Session<'_>,
    allocation_error: &'static str,
) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit(allocation_error))?;
    output.push_str(value);
    Ok(output)
}

fn nonnegative_integer(value: RdfTerm<'_>, session: &mut Session<'_>) -> NativeResult<String> {
    let RdfTerm::Literal(value) = value else {
        return Err(unsupported(
            "native RDF cardinality must be a nonnegative integer literal",
        ));
    };
    if usize_as_u64(value.len(), "native RDF cardinality length exceeds u64")?
        > session.limits().value(LimitKey::MaxLiteralBytes)
    {
        return Err(NativeError::limit(
            "native RDF cardinality exceeds max_literal_bytes",
        ));
    }
    session.step(usize_as_u64(
        value.len(),
        "native RDF cardinality work exceeds u64",
    )?)?;
    if value.is_empty() || !value.bytes().all(|value| value.is_ascii_digit()) {
        return Err(unsupported(
            "native RDF cardinality must be a nonnegative integer literal",
        ));
    }
    let significant = value.trim_start_matches('0');
    let significant = if significant.is_empty() {
        "0"
    } else {
        significant
    };
    session.reserve_bytes(significant.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(significant.len())
        .map_err(|_| NativeError::limit("native RDF cardinality allocation failed"))?;
    output.push_str(significant);
    Ok(output)
}

fn named_entity(kind: &'static str, value: &str, session: &mut Session<'_>) -> NativeResult<Node> {
    entity(kind, iri_node(value, session)?)
}

fn iri_node(value: &str, session: &mut Session<'_>) -> NativeResult<Node> {
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
    iri(owned)
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

fn register_kind_value<'data>(
    values: &mut Vec<&'data str>,
    value: &'data str,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.step(usize_as_u64(
        values.len(),
        "native RDF entity-kind work exceeds u64",
    )?)?;
    if !values.contains(&value) {
        reserve_item(values, session)?;
        values.push(value);
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
        decode_with_kinds(graph, value, &[], &[])
    }

    fn decode_with_kinds(
        graph: &[RdfTriple<'static>],
        value: RdfTerm<'static>,
        data_properties: &[&'static str],
        datatypes: &[&'static str],
    ) -> NativeResult<DecodedClassExpression> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0)?;
        let mut decoder = RdfClassExpressionDecoder::new(graph);
        for property in data_properties {
            decoder.register_data_property(property, &mut session)?;
        }
        for datatype in datatypes {
            decoder.register_datatype(datatype, &mut session)?;
        }
        decoder.decode_term(value, &mut session)
    }

    fn decode_data(
        graph: &[RdfTriple<'static>],
        value: RdfTerm<'static>,
        literals: &[(usize, Option<&'static str>, Option<&'static str>)],
    ) -> NativeResult<DecodedClassExpression> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0)?;
        let mut decoder = RdfClassExpressionDecoder::new(graph);
        for (index, datatype, language) in literals {
            decoder.register_literal(*index, *datatype, *language, &mut session)?;
        }
        let mut consumed = Vec::new();
        let node = decoder.decode_data_range(value, &mut consumed, &mut session)?;
        consumed.sort_unstable();
        consumed.dedup();
        Ok(DecodedClassExpression { node, consumed })
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
    fn boolean_and_complement_data_ranges_match_canonical_tags() {
        for (predicate, tag) in [(OWL_INTERSECTION_OF, 21), (OWL_UNION_OF, 22)] {
            let graph = [
                edge("e", RDF_TYPE, iri_term(RDFS_DATATYPE)),
                edge("e", predicate, blank_term("h")),
                edge("h", RDF_FIRST, iri_term("urn:B")),
                edge("h", RDF_REST, blank_term("t")),
                edge("t", RDF_FIRST, iri_term("urn:A")),
                edge("t", RDF_REST, iri_term(RDF_NIL)),
            ];
            let decoded = decode_data(&graph, blank_term("e"), &[]).expect("boolean data range");
            let values = canonical_set(
                vec![
                    entity("datatype", iri("urn:A".to_owned()).unwrap()).unwrap(),
                    entity("datatype", iri("urn:B".to_owned()).unwrap()).unwrap(),
                ],
                2,
                Some(tag),
            )
            .unwrap();
            let expected = Node::build(tag, vec![Field::Set(values)]).unwrap();
            assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
            assert_eq!(decoded.consumed, [0, 1, 2, 3, 4, 5]);
        }

        let complement_graph = [
            edge("e", RDF_TYPE, iri_term(RDFS_DATATYPE)),
            edge("e", OWL_DATATYPE_COMPLEMENT_OF, iri_term("urn:A")),
        ];
        let complement =
            decode_data(&complement_graph, blank_term("e"), &[]).expect("data complement");
        let expected = Node::build(
            23,
            vec![Field::Node(
                entity("datatype", iri("urn:A".to_owned()).unwrap()).unwrap(),
            )],
        )
        .unwrap();
        assert_eq!(complement.node.as_bytes(), expected.as_bytes());
        assert_eq!(complement.consumed, [0, 1]);
    }

    #[test]
    fn data_enumeration_preserves_each_list_literal_identity() {
        let graph = [
            edge("e", RDF_TYPE, iri_term(RDFS_DATATYPE)),
            edge("e", OWL_ONE_OF, blank_term("h")),
            edge("h", RDF_FIRST, RdfTerm::Literal("007")),
            edge("h", RDF_REST, blank_term("t")),
            edge("t", RDF_FIRST, RdfTerm::Literal("colour")),
            edge("t", RDF_REST, iri_term(RDF_NIL)),
        ];
        let decoded = decode_data(
            &graph,
            blank_term("e"),
            &[
                (2, Some("http://www.w3.org/2001/XMLSchema#integer"), None),
                (4, None, Some("EN-gb")),
            ],
        )
        .expect("data enumeration");
        let values = canonical_set(
            vec![
                literal(
                    "007".to_owned(),
                    entity(
                        "datatype",
                        iri("http://www.w3.org/2001/XMLSchema#integer".to_owned()).unwrap(),
                    )
                    .unwrap(),
                    None,
                )
                .unwrap(),
                literal(
                    "colour".to_owned(),
                    entity("datatype", iri(RDF_PLAIN_LITERAL.to_owned()).unwrap()).unwrap(),
                    Some("en-gb".to_owned()),
                )
                .unwrap(),
            ],
            1,
            None,
        )
        .unwrap();
        let expected = Node::build(24, vec![Field::Set(values)]).unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2, 3, 4, 5]);
    }

    #[test]
    fn datatype_restriction_maps_exact_facet_literal() {
        const MIN_INCLUSIVE: &str = "http://www.w3.org/2001/XMLSchema#minInclusive";
        let graph = [
            edge("e", RDF_TYPE, iri_term(RDFS_DATATYPE)),
            edge("e", OWL_ON_DATATYPE, iri_term("urn:Datatype")),
            edge("e", OWL_WITH_RESTRICTIONS, blank_term("h")),
            edge("h", RDF_FIRST, blank_term("facet")),
            edge("h", RDF_REST, iri_term(RDF_NIL)),
            edge("facet", MIN_INCLUSIVE, RdfTerm::Literal("007")),
        ];
        let decoded = decode_data(
            &graph,
            blank_term("e"),
            &[(5, Some("http://www.w3.org/2001/XMLSchema#integer"), None)],
        )
        .expect("datatype restriction");
        let value = literal(
            "007".to_owned(),
            entity(
                "datatype",
                iri("http://www.w3.org/2001/XMLSchema#integer".to_owned()).unwrap(),
            )
            .unwrap(),
            None,
        )
        .unwrap();
        let facet = Node::build(
            20,
            vec![
                Field::Node(iri(MIN_INCLUSIVE.to_owned()).unwrap()),
                Field::Node(value),
            ],
        )
        .unwrap();
        let expected = Node::build(
            25,
            vec![
                Field::Node(entity("datatype", iri("urn:Datatype".to_owned()).unwrap()).unwrap()),
                Field::Set(vec![facet]),
            ],
        )
        .unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2, 3, 4, 5]);
    }

    #[test]
    fn conflicting_cyclic_and_wrong_typed_data_ranges_fail_closed() {
        let conflict = [
            edge("e", OWL_INTERSECTION_OF, iri_term(RDF_NIL)),
            edge("e", OWL_ONE_OF, iri_term(RDF_NIL)),
        ];
        let cycle = [edge("e", OWL_DATATYPE_COMPLEMENT_OF, blank_term("e"))];
        let resource_enumeration = [
            edge("e", OWL_ONE_OF, blank_term("h")),
            edge("h", RDF_FIRST, iri_term("urn:not-a-literal")),
            edge("h", RDF_REST, iri_term(RDF_NIL)),
        ];
        let datatype_restriction = [
            edge("e", OWL_ON_DATATYPE, iri_term("urn:Datatype")),
            edge("e", OWL_WITH_RESTRICTIONS, iri_term(RDF_NIL)),
        ];
        let multiple_facet_values = [
            edge("e", OWL_ON_DATATYPE, iri_term("urn:Datatype")),
            edge("e", OWL_WITH_RESTRICTIONS, blank_term("h")),
            edge("h", RDF_FIRST, blank_term("facet")),
            edge("h", RDF_REST, iri_term(RDF_NIL)),
            edge("facet", "urn:min", RdfTerm::Literal("1")),
            edge("facet", "urn:max", RdfTerm::Literal("2")),
        ];
        let resource_facet = [
            edge("e", OWL_ON_DATATYPE, iri_term("urn:Datatype")),
            edge("e", OWL_WITH_RESTRICTIONS, blank_term("h")),
            edge("h", RDF_FIRST, iri_term("urn:not-a-facet-node")),
            edge("h", RDF_REST, iri_term(RDF_NIL)),
        ];
        for graph in [
            conflict.as_slice(),
            cycle.as_slice(),
            resource_enumeration.as_slice(),
            datatype_restriction.as_slice(),
            multiple_facet_values.as_slice(),
            resource_facet.as_slice(),
        ] {
            assert_eq!(
                decode_data(graph, blank_term("e"), &[]).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }
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
    fn object_value_and_self_restrictions_match_canonical_tags() {
        let has_value_graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_VALUE, iri_term("urn:i")),
        ];
        let has_value =
            decode(&has_value_graph, blank_term("e")).expect("object value restriction");
        let expected_has_value = Node::build(
            36,
            vec![
                Field::Node(entity("object_property", iri("urn:p".to_owned()).unwrap()).unwrap()),
                Field::Node(entity("named_individual", iri("urn:i".to_owned()).unwrap()).unwrap()),
            ],
        )
        .unwrap();
        assert_eq!(has_value.node.as_bytes(), expected_has_value.as_bytes());
        assert_eq!(has_value.consumed, [0, 1, 2]);

        let has_self_graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_SELF, RdfTerm::Literal("TRUE")),
        ];
        let has_self = decode(&has_self_graph, blank_term("e")).expect("self restriction");
        let expected_has_self = Node::build(
            37,
            vec![Field::Node(
                entity("object_property", iri("urn:p".to_owned()).unwrap()).unwrap(),
            )],
        )
        .unwrap();
        assert_eq!(has_self.node.as_bytes(), expected_has_self.as_bytes());
        assert_eq!(has_self.consumed, [0, 1, 2]);
    }

    #[test]
    fn data_has_value_preserves_literal_datatype_and_language() {
        let typed_graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_VALUE, RdfTerm::Literal("007")),
        ];
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut decoder = RdfClassExpressionDecoder::new(&typed_graph);
        decoder
            .register_literal(
                2,
                Some("http://www.w3.org/2001/XMLSchema#integer"),
                None,
                &mut session,
            )
            .expect("typed literal metadata");
        let decoded = decoder
            .decode_term(blank_term("e"), &mut session)
            .expect("typed data value restriction");
        let expected_literal = literal(
            "007".to_owned(),
            entity(
                "datatype",
                iri("http://www.w3.org/2001/XMLSchema#integer".to_owned()).unwrap(),
            )
            .unwrap(),
            None,
        )
        .unwrap();
        let expected = Node::build(
            43,
            vec![
                Field::Node(entity("data_property", iri("urn:p".to_owned()).unwrap()).unwrap()),
                Field::Node(expected_literal),
            ],
        )
        .unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2]);

        let language_graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_VALUE, RdfTerm::Literal("colour")),
        ];
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut decoder = RdfClassExpressionDecoder::new(&language_graph);
        decoder
            .register_literal(2, None, Some("EN-gb"), &mut session)
            .expect("language literal metadata");
        let decoded = decoder
            .decode_term(blank_term("e"), &mut session)
            .expect("language data value restriction");
        let expected_literal = literal(
            "colour".to_owned(),
            entity("datatype", iri(RDF_PLAIN_LITERAL.to_owned()).unwrap()).unwrap(),
            Some("en-gb".to_owned()),
        )
        .unwrap();
        let expected = Node::build(
            43,
            vec![
                Field::Node(entity("data_property", iri("urn:p".to_owned()).unwrap()).unwrap()),
                Field::Node(expected_literal),
            ],
        )
        .unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2]);
    }

    #[test]
    fn ambiguous_and_invalid_literal_metadata_fails_closed() {
        let graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_VALUE, RdfTerm::Literal("value")),
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
            .register_literal(2, Some(XSD_STRING), Some("en"), &mut session)
            .expect("ambiguous literal metadata registration");
        assert_eq!(
            decoder
                .decode_term(blank_term("e"), &mut session)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );

        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut decoder = RdfClassExpressionDecoder::new(&graph);
        decoder
            .register_literal(2, None, Some("not_valid"), &mut session)
            .expect("invalid language metadata registration");
        assert_eq!(
            decoder
                .decode_term(blank_term("e"), &mut session)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
        assert_eq!(
            decoder
                .register_literal(2, None, Some("en"), &mut session)
                .unwrap_err()
                .code,
            "NATIVE_PROTOCOL",
        );
    }

    #[test]
    fn object_cardinalities_match_canonical_tags_and_arbitrary_integers() {
        for (predicate, tag, qualified, lexical, normalized) in [
            (OWL_MIN_CARDINALITY, 38, false, "0002", "2"),
            (OWL_MAX_CARDINALITY, 39, false, "0", "0"),
            (
                OWL_CARDINALITY,
                40,
                false,
                "18446744073709551616",
                "18446744073709551616",
            ),
            (OWL_MIN_QUALIFIED_CARDINALITY, 38, true, "2", "2"),
            (OWL_MAX_QUALIFIED_CARDINALITY, 39, true, "3", "3"),
            (OWL_QUALIFIED_CARDINALITY, 40, true, "4", "4"),
        ] {
            let mut graph = vec![
                edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
                edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
                edge("e", predicate, RdfTerm::Literal(lexical)),
            ];
            if qualified {
                graph.push(edge("e", OWL_ON_CLASS, iri_term("urn:Filler")));
            }
            let decoded = decode(&graph, blank_term("e")).expect("object cardinality");
            let filler = if qualified {
                entity("class", iri("urn:Filler".to_owned()).unwrap()).unwrap()
            } else {
                entity("class", iri(OWL_THING.to_owned()).unwrap()).unwrap()
            };
            let expected = Node::build(
                tag,
                vec![
                    Field::Integer(normalized.to_owned()),
                    Field::Node(
                        entity("object_property", iri("urn:p".to_owned()).unwrap()).unwrap(),
                    ),
                    Field::Node(filler),
                ],
            )
            .unwrap();
            assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
            assert_eq!(decoded.consumed, (0..graph.len()).collect::<Vec<_>>());
        }
    }

    #[test]
    fn quantified_data_restrictions_follow_property_and_datatype_kinds() {
        let graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:data")),
            edge("e", OWL_SOME_VALUES_FROM, iri_term("urn:Filler")),
        ];
        let decoded = decode_with_kinds(&graph, blank_term("e"), &["urn:data"], &[])
            .expect("data-property restriction");
        let expected = Node::build(
            41,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:data".to_owned()).unwrap(),
                )
                .unwrap()]),
                Field::Node(entity("datatype", iri("urn:Filler".to_owned()).unwrap()).unwrap()),
            ],
        )
        .unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2]);

        let datatype_graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:inferred-data")),
            edge("e", OWL_ALL_VALUES_FROM, iri_term("urn:Datatype")),
        ];
        let decoded = decode_with_kinds(&datatype_graph, blank_term("e"), &[], &["urn:Datatype"])
            .expect("datatype-filler restriction");
        let expected = Node::build(
            42,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:inferred-data".to_owned()).unwrap(),
                )
                .unwrap()]),
                Field::Node(entity("datatype", iri("urn:Datatype".to_owned()).unwrap()).unwrap()),
            ],
        )
        .unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2]);

        let structural_graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:inferred-data")),
            edge("e", OWL_ALL_VALUES_FROM, blank_term("range")),
            edge("range", RDF_TYPE, iri_term(RDFS_DATATYPE)),
            edge(
                "range",
                OWL_DATATYPE_COMPLEMENT_OF,
                iri_term("urn:Datatype"),
            ),
        ];
        let decoded = decode(&structural_graph, blank_term("e"))
            .expect("structural datatype-filler restriction");
        let complement = Node::build(
            23,
            vec![Field::Node(
                entity("datatype", iri("urn:Datatype".to_owned()).unwrap()).unwrap(),
            )],
        )
        .unwrap();
        let expected = Node::build(
            42,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:inferred-data".to_owned()).unwrap(),
                )
                .unwrap()]),
                Field::Node(complement),
            ],
        )
        .unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2, 3, 4]);
    }

    #[test]
    fn data_cardinalities_map_declared_properties_and_on_data_range() {
        let graph = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:data")),
            edge("e", OWL_MIN_CARDINALITY, RdfTerm::Literal("1")),
        ];
        let decoded = decode_with_kinds(&graph, blank_term("e"), &["urn:data"], &[])
            .expect("unqualified data cardinality");
        let expected = Node::build(
            44,
            vec![
                Field::Integer("1".to_owned()),
                Field::Node(entity("data_property", iri("urn:data".to_owned()).unwrap()).unwrap()),
                Field::Node(entity("datatype", iri(RDFS_LITERAL.to_owned()).unwrap()).unwrap()),
            ],
        )
        .unwrap();
        assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
        assert_eq!(decoded.consumed, [0, 1, 2]);

        for (predicate, tag, cardinality) in [
            (OWL_MAX_QUALIFIED_CARDINALITY, 45, "2"),
            (OWL_QUALIFIED_CARDINALITY, 46, "3"),
        ] {
            let qualified = [
                edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
                edge("e", OWL_ON_PROPERTY, iri_term("urn:data")),
                edge("e", predicate, RdfTerm::Literal(cardinality)),
                edge("e", OWL_ON_DATA_RANGE, iri_term("urn:Datatype")),
            ];
            let decoded = decode(&qualified, blank_term("e")).expect("qualified data cardinality");
            let expected = Node::build(
                tag,
                vec![
                    Field::Integer(cardinality.to_owned()),
                    Field::Node(
                        entity("data_property", iri("urn:data".to_owned()).unwrap()).unwrap(),
                    ),
                    Field::Node(
                        entity("datatype", iri("urn:Datatype".to_owned()).unwrap()).unwrap(),
                    ),
                ],
            )
            .unwrap();
            assert_eq!(decoded.node.as_bytes(), expected.as_bytes());
            assert_eq!(decoded.consumed, [0, 1, 2, 3]);
        }

        let conflicting_kind = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:data")),
            edge("e", OWL_CARDINALITY, RdfTerm::Literal("1")),
            edge("e", OWL_ON_CLASS, iri_term("urn:Class")),
        ];
        assert_eq!(
            decode_with_kinds(&conflicting_kind, blank_term("e"), &["urn:data"], &[],)
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
        let conflicting_operators = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_SOME_VALUES_FROM, iri_term("urn:A")),
            edge("e", OWL_HAS_VALUE, iri_term("urn:i")),
        ];
        let literal_has_value = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_VALUE, RdfTerm::Literal("value")),
        ];
        let false_has_self = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_SELF, RdfTerm::Literal("false")),
        ];
        let resource_has_self = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_HAS_SELF, iri_term("urn:true")),
        ];
        let conflicting_cardinalities = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_MIN_CARDINALITY, RdfTerm::Literal("1")),
            edge("e", OWL_MAX_CARDINALITY, RdfTerm::Literal("2")),
        ];
        let nonliteral_cardinality = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_CARDINALITY, iri_term("urn:one")),
        ];
        let negative_cardinality = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_CARDINALITY, RdfTerm::Literal("-1")),
        ];
        let qualified_without_filler = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_MIN_QUALIFIED_CARDINALITY, RdfTerm::Literal("1")),
        ];
        let qualified_conflicting_fillers = [
            edge("e", RDF_TYPE, iri_term(OWL_RESTRICTION)),
            edge("e", OWL_ON_PROPERTY, iri_term("urn:p")),
            edge("e", OWL_QUALIFIED_CARDINALITY, RdfTerm::Literal("1")),
            edge("e", OWL_ON_CLASS, iri_term("urn:Class")),
            edge("e", OWL_ON_DATA_RANGE, iri_term("urn:datatype")),
        ];
        for (graph, value) in [
            (conflict.as_slice(), blank_term("e")),
            (cycle.as_slice(), blank_term("e")),
            (complement_literal.as_slice(), blank_term("e")),
            (ambiguous_individual.as_slice(), blank_term("e")),
            (conflicting_quantifiers.as_slice(), blank_term("e")),
            (blank_property.as_slice(), blank_term("e")),
            (conflicting_operators.as_slice(), blank_term("e")),
            (literal_has_value.as_slice(), blank_term("e")),
            (false_has_self.as_slice(), blank_term("e")),
            (resource_has_self.as_slice(), blank_term("e")),
            (conflicting_cardinalities.as_slice(), blank_term("e")),
            (nonliteral_cardinality.as_slice(), blank_term("e")),
            (negative_cardinality.as_slice(), blank_term("e")),
            (qualified_without_filler.as_slice(), blank_term("e")),
            (qualified_conflicting_fillers.as_slice(), blank_term("e")),
            (&[][..], RdfTerm::Literal("not-a-class")),
        ] {
            assert_eq!(
                decode(graph, value).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }
    }
}
