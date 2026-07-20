//! Stateful, bounded RDF collection decoding shared by RDF syntax mappers.

use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;

pub(crate) const RDF_FIRST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first";
pub(crate) const RDF_REST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest";
pub(crate) const RDF_NIL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil";
pub(crate) const RDF_TYPE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
pub(crate) const RDF_LIST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#List";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RdfResource<'graph> {
    Iri(&'graph str),
    Blank(&'graph str),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RdfTerm<'graph> {
    Iri(&'graph str),
    Blank(&'graph str),
    Literal(&'graph str),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct RdfTriple<'graph> {
    pub(crate) subject: RdfResource<'graph>,
    pub(crate) predicate: &'graph str,
    pub(crate) object: RdfTerm<'graph>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DecodedRdfList<'graph> {
    pub(crate) items: Vec<RdfTerm<'graph>>,
    /// Indices of the exact `rdf:first`, `rdf:rest`, and optional
    /// `rdf:type rdf:List` triples owned by this collection.
    pub(crate) consumed: Vec<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ListOwner<'graph> {
    cell: &'graph str,
    root: &'graph str,
}

/// Decodes multiple collections against one graph while retaining the tail
/// ownership ledger needed to reject cross-collection sharing.
pub(crate) struct RdfListDecoder<'graph, 'data, 'session, 'guard> {
    triples: &'graph [RdfTriple<'data>],
    owners: Vec<ListOwner<'data>>,
    session: &'session mut Session<'guard>,
}

impl<'graph, 'data, 'session, 'guard> RdfListDecoder<'graph, 'data, 'session, 'guard> {
    pub(crate) fn new(
        triples: &'graph [RdfTriple<'data>],
        session: &'session mut Session<'guard>,
    ) -> Self {
        Self {
            triples,
            owners: Vec::new(),
            session,
        }
    }

    pub(crate) fn decode(&mut self, head: RdfTerm<'data>) -> NativeResult<DecodedRdfList<'data>> {
        // Check cancellation/deadline state before even inspecting an
        // attacker-controlled graph or allocating a traversal ledger.
        self.session.finish()?;
        let root = match head {
            RdfTerm::Iri(RDF_NIL) => {
                return Ok(DecodedRdfList {
                    items: Vec::new(),
                    consumed: Vec::new(),
                });
            }
            RdfTerm::Blank(value) => value,
            RdfTerm::Iri(_) | RdfTerm::Literal(_) => {
                return Err(unsupported(
                    "native RDF collection head must be blank or rdf:nil",
                ));
            }
        };

        // Ownership updates are transactional: a malformed traversal cannot
        // poison this decoder if its caller handles the error and continues.
        let mut pending_owners = Vec::new();
        let mut visited = Vec::new();
        let mut items = Vec::new();
        let mut consumed = Vec::new();
        let mut current = root;
        loop {
            let next_length = items
                .len()
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native RDF list length overflow"))?;
            enforce_length(next_length, self.session)?;

            self.session.step(usize_as_u64(
                visited.len(),
                "native RDF list cycle work exceeds u64",
            )?)?;
            if visited.contains(&current) {
                return Err(unsupported("native cyclic RDF collection"));
            }
            reserve_item(&mut visited, self.session)?;
            visited.push(current);

            self.claim(current, root, &mut pending_owners)?;
            self.consume_list_markers(current, &mut consumed)?;
            let first = self.unique_edge(current, RDF_FIRST, "native forked RDF first edge")?;
            let rest = self.unique_edge(current, RDF_REST, "native forked RDF rest edge")?;

            reserve_item(&mut items, self.session)?;
            items.push(self.triples[first].object);
            reserve_item(&mut consumed, self.session)?;
            consumed.push(first);
            reserve_item(&mut consumed, self.session)?;
            consumed.push(rest);

            match self.triples[rest].object {
                RdfTerm::Iri(RDF_NIL) => break,
                RdfTerm::Blank(value) => current = value,
                RdfTerm::Iri(_) | RdfTerm::Literal(_) => {
                    return Err(unsupported(
                        "native RDF collection tail must be blank or rdf:nil",
                    ));
                }
            }
        }

        self.session.finish()?;
        reserve_additional(&mut self.owners, pending_owners.len(), self.session)?;
        self.owners.extend(pending_owners);
        consumed.sort_unstable();
        consumed.dedup();
        Ok(DecodedRdfList { items, consumed })
    }

    fn claim(
        &mut self,
        cell: &'data str,
        root: &'data str,
        pending: &mut Vec<ListOwner<'data>>,
    ) -> NativeResult<()> {
        self.session.step(usize_as_u64(
            self.owners.len(),
            "native RDF list ownership work exceeds u64",
        )?)?;
        if self
            .owners
            .iter()
            .any(|owner| owner.cell == cell && owner.root != root)
        {
            return Err(unsupported("native shared RDF collection tail"));
        }
        self.session.step(usize_as_u64(
            pending.len(),
            "native RDF list ownership work exceeds u64",
        )?)?;
        if pending
            .iter()
            .any(|owner| owner.cell == cell && owner.root != root)
        {
            return Err(unsupported("native shared RDF collection tail"));
        }
        if !self.owners.iter().any(|owner| owner.cell == cell)
            && !pending.iter().any(|owner| owner.cell == cell)
        {
            reserve_item(pending, self.session)?;
            pending.push(ListOwner { cell, root });
        }
        Ok(())
    }

    fn unique_edge(
        &mut self,
        subject: &'data str,
        predicate: &str,
        malformed: &'static str,
    ) -> NativeResult<usize> {
        let mut selected = None;
        for (index, triple) in self.triples.iter().enumerate() {
            self.session.step(1)?;
            if triple.subject == RdfResource::Blank(subject)
                && triple.predicate == predicate
                && selected.replace(index).is_some()
            {
                return Err(unsupported(malformed));
            }
        }
        selected.ok_or_else(|| unsupported("native incomplete RDF collection cell"))
    }

    fn consume_list_markers(
        &mut self,
        subject: &'data str,
        consumed: &mut Vec<usize>,
    ) -> NativeResult<()> {
        for (index, triple) in self.triples.iter().enumerate() {
            self.session.step(1)?;
            if triple.subject == RdfResource::Blank(subject)
                && triple.predicate == RDF_TYPE
                && triple.object == RdfTerm::Iri(RDF_LIST)
            {
                reserve_item(consumed, self.session)?;
                consumed.push(index);
            }
        }
        Ok(())
    }
}

fn enforce_length(value: usize, session: &Session<'_>) -> NativeResult<()> {
    let value = usize_as_u64(value, "native RDF list length exceeds u64")?;
    if value > session.limits().value(LimitKey::MaxRdfListLength) {
        return Err(NativeError::limit(
            "native RDF list exceeds max_rdf_list_length",
        ));
    }
    if value > session.limits().value(LimitKey::MaxSequenceArity) {
        return Err(NativeError::limit(
            "native RDF list exceeds max_sequence_arity",
        ));
    }
    Ok(())
}

fn reserve_item<T>(values: &mut Vec<T>, session: &mut Session<'_>) -> NativeResult<()> {
    if values.len() == values.capacity() {
        session.reserve_bytes(std::mem::size_of::<T>())?;
        values
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native RDF list allocation failed"))?;
    }
    Ok(())
}

fn reserve_additional<T>(
    values: &mut Vec<T>,
    additional: usize,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    if additional == 0 {
        return Ok(());
    }
    let bytes = additional
        .checked_mul(std::mem::size_of::<T>())
        .ok_or_else(|| NativeError::limit("native RDF list allocation accounting overflow"))?;
    session.reserve_bytes(bytes)?;
    values
        .try_reserve_exact(additional)
        .map_err(|_| NativeError::limit("native RDF list allocation failed"))
}

fn usize_as_u64(value: usize, message: &'static str) -> NativeResult<u64> {
    u64::try_from(value).map_err(|_| NativeError::limit(message))
}

fn unsupported(message: &'static str) -> NativeError {
    NativeError::new("NATIVE_RDF_MAPPING_UNSUPPORTED", message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::{Cancellation, Guard};
    use crate::limits::{Limits, CONFIG_BYTES, CONFIG_MAGIC, CONFIG_SCHEMA};
    use std::time::Duration;

    fn blank(value: &'static str) -> RdfResource<'static> {
        RdfResource::Blank(value)
    }

    fn bterm(value: &'static str) -> RdfTerm<'static> {
        RdfTerm::Blank(value)
    }

    fn iri(value: &'static str) -> RdfTerm<'static> {
        RdfTerm::Iri(value)
    }

    fn edge(
        subject: &'static str,
        predicate: &'static str,
        object: RdfTerm<'static>,
    ) -> RdfTriple<'static> {
        RdfTriple {
            subject: blank(subject),
            predicate,
            object,
        }
    }

    fn limits_with(key: LimitKey, value: u64) -> Limits {
        let mut encoded = vec![0_u8; CONFIG_BYTES];
        encoded[..8].copy_from_slice(CONFIG_MAGIC);
        encoded[8..10].copy_from_slice(&CONFIG_SCHEMA.to_le_bytes());
        for index in 0..37 {
            let configured = if matches!(index, 13 | 14) {
                0
            } else {
                1_000_000_000_u64
            };
            encoded[16 + index * 8..24 + index * 8].copy_from_slice(&configured.to_le_bytes());
        }
        let offset = 16 + key as usize * 8;
        encoded[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
        Limits::decode(&encoded).expect("test limits")
    }

    fn decode_with_limits(
        graph: &[RdfTriple<'static>],
        head: RdfTerm<'static>,
        limits: &Limits,
    ) -> NativeResult<DecodedRdfList<'static>> {
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, limits, 0)?;
        RdfListDecoder::new(graph, &mut session).decode(head)
    }

    #[test]
    fn nil_and_well_formed_chain_decode_deterministically() {
        let limits = Limits::default();
        let empty = decode_with_limits(&[], iri(RDF_NIL), &limits).expect("rdf:nil");
        assert!(empty.items.is_empty());
        assert!(empty.consumed.is_empty());

        let graph = [
            edge("h", RDF_TYPE, iri(RDF_LIST)),
            edge("h", RDF_FIRST, iri("urn:a")),
            edge("h", RDF_REST, bterm("t")),
            edge("t", RDF_FIRST, RdfTerm::Literal("lexical")),
            edge("t", RDF_REST, iri(RDF_NIL)),
        ];
        let decoded = decode_with_limits(&graph, bterm("h"), &limits).expect("valid list");
        assert_eq!(decoded.items, [iri("urn:a"), RdfTerm::Literal("lexical")]);
        assert_eq!(decoded.consumed, [0, 1, 2, 3, 4]);
    }

    #[test]
    fn cycles_forks_duplicates_and_wrong_term_types_fail_closed() {
        let limits = Limits::default();
        let cases: &[(&[RdfTriple<'static>], RdfTerm<'static>, &str)] = &[
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, bterm("h")),
                ],
                bterm("h"),
                "cyclic",
            ),
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, iri(RDF_NIL)),
                    edge("h", RDF_REST, bterm("t")),
                ],
                bterm("h"),
                "forked rest",
            ),
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, iri(RDF_NIL)),
                ],
                bterm("h"),
                "duplicate first",
            ),
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, RdfTerm::Literal("bad")),
                ],
                bterm("h"),
                "literal rest",
            ),
            (&[], iri("urn:not-nil"), "named head"),
            (&[], RdfTerm::Literal("head"), "literal head"),
        ];
        for (graph, head, label) in cases {
            assert_eq!(
                decode_with_limits(graph, *head, &limits).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
                "{label}",
            );
        }
    }

    #[test]
    fn separate_roots_cannot_claim_a_shared_tail() {
        let graph = [
            edge("h1", RDF_FIRST, iri("urn:a")),
            edge("h1", RDF_REST, bterm("tail")),
            edge("h2", RDF_FIRST, iri("urn:b")),
            edge("h2", RDF_REST, bterm("tail")),
            edge("tail", RDF_FIRST, iri("urn:c")),
            edge("tail", RDF_REST, iri(RDF_NIL)),
        ];
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut decoder = RdfListDecoder::new(&graph, &mut session);
        assert_eq!(
            decoder.decode(bterm("h1")).expect("first root").items.len(),
            2
        );
        let error = decoder.decode(bterm("h2")).unwrap_err();
        assert_eq!(error.code, "NATIVE_RDF_MAPPING_UNSUPPORTED");
        assert!(error.message.contains("shared"));
    }

    #[test]
    fn list_length_is_preflighted_before_the_next_cell_is_claimed() {
        let graph = [
            edge("h", RDF_FIRST, iri("urn:a")),
            edge("h", RDF_REST, bterm("t")),
            edge("t", RDF_FIRST, iri("urn:b")),
            edge("t", RDF_REST, iri(RDF_NIL)),
        ];
        let limits = limits_with(LimitKey::MaxRdfListLength, 1);
        assert_eq!(
            decode_with_limits(&graph, bterm("h"), &limits)
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT",
        );
    }

    #[test]
    fn cancellation_and_work_limits_checkpoint_during_decode() {
        let graph = [
            edge("h", RDF_FIRST, iri("urn:a")),
            edge("h", RDF_REST, iri(RDF_NIL)),
        ];
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(Some(Duration::ZERO)),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        assert_eq!(
            RdfListDecoder::new(&graph, &mut session)
                .decode(bterm("h"))
                .unwrap_err()
                .code,
            "NATIVE_DEADLINE",
        );

        let bounded = limits_with(LimitKey::MaxCanonicalWork, 1);
        assert_eq!(
            decode_with_limits(&graph, bterm("h"), &bounded)
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT",
        );
    }
}
