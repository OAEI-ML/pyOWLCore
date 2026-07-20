//! Bounded reverse mapping for RDF boolean class expressions.

use crate::canonical::{canonical_set, entity, iri, Field, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;

use super::rdf_lists::{RdfListDecoder, RdfResource, RdfTerm, RdfTriple, RDF_TYPE};

const OWL_CLASS: &str = "http://www.w3.org/2002/07/owl#Class";
const OWL_INTERSECTION_OF: &str = "http://www.w3.org/2002/07/owl#intersectionOf";
const OWL_UNION_OF: &str = "http://www.w3.org/2002/07/owl#unionOf";

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
}

impl<'graph, 'data> RdfClassExpressionDecoder<'graph, 'data> {
    pub(crate) fn new(triples: &'graph [RdfTriple<'data>]) -> Self {
        Self {
            triples,
            lists: RdfListDecoder::new(triples),
            active: Vec::new(),
        }
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
        let result = self.decode_boolean(value, consumed, session);
        self.active.pop();
        result
    }

    fn decode_boolean(
        &mut self,
        subject: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<Node> {
        let intersection = self.unique_edge(subject, OWL_INTERSECTION_OF, session)?;
        let union = self.unique_edge(subject, OWL_UNION_OF, session)?;
        let (constructor, tag) = match (intersection, union) {
            (Some(index), None) => (index, 30),
            (None, Some(index)) => (index, 31),
            (Some(_), Some(_)) => {
                return Err(unsupported(
                    "native RDF blank node has conflicting class constructors",
                ));
            }
            (None, None) => {
                return Err(unsupported(
                    "native RDF blank node is not a recognized class expression",
                ));
            }
        };

        push_index(consumed, constructor, session)?;
        self.consume_class_markers(subject, consumed, session)?;
        let head = self.triples[constructor].object;
        let decoded = self.lists.decode(head, session)?;
        let raw_length = decoded.items.len();
        if raw_length < 2 {
            return Err(unsupported(
                "native RDF boolean class expression has fewer than two operands",
            ));
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
    super::check_iri(
        value,
        session,
        "native RDF class-expression IRI exceeds max_iri_bytes",
    )?;
    session.reserve_bytes(value.len())?;
    let mut owned = String::new();
    owned
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native RDF class IRI allocation failed"))?;
    owned.push_str(value);
    entity("class", iri(owned)?)
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
        for (graph, value) in [
            (conflict.as_slice(), blank_term("e")),
            (cycle.as_slice(), blank_term("e")),
            (&[][..], RdfTerm::Literal("not-a-class")),
        ] {
            assert_eq!(
                decode(graph, value).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }
    }
}
