//! Stateful, bounded RDF collection decoding shared by RDF syntax mappers.

use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;
use std::collections::{HashMap, HashSet};

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
    /// Blank labels traversed from head to tail, used by semantic mappers to
    /// enforce compatible blank-node roles.
    pub(crate) cells: Vec<&'graph str>,
    /// Indices of the exact `rdf:first`, `rdf:rest`, and optional
    /// `rdf:type rdf:List` triples owned by this collection.
    pub(crate) consumed: Vec<usize>,
}

/// Decodes multiple collections against one graph while retaining the tail
/// ownership ledger needed to reject cross-collection sharing.
pub(crate) struct RdfListDecoder<'graph, 'data> {
    triples: &'graph [RdfTriple<'data>],
    by_blank_subject: HashMap<&'data str, Vec<usize>>,
    owners: HashMap<&'data str, &'data str>,
}

impl<'graph, 'data> RdfListDecoder<'graph, 'data> {
    pub(crate) fn new(
        triples: &'graph [RdfTriple<'data>],
        session: &mut Session<'_>,
    ) -> NativeResult<Self> {
        let mut by_blank_subject = HashMap::new();
        for (index, triple) in triples.iter().enumerate() {
            session.step(1)?;
            let RdfResource::Blank(subject) = triple.subject else {
                continue;
            };
            if !by_blank_subject.contains_key(subject) {
                reserve_hash_map_item(&mut by_blank_subject, session)?;
                by_blank_subject.insert(subject, Vec::new());
            }
            let indexes = by_blank_subject.get_mut(subject).ok_or_else(|| {
                NativeError::protocol("native RDF list subject index changed during construction")
            })?;
            reserve_item(indexes, session)?;
            indexes.push(index);
        }
        Ok(Self {
            triples,
            by_blank_subject,
            owners: HashMap::new(),
        })
    }

    pub(crate) fn blank_subject_indexes(&self, subject: &str) -> &[usize] {
        self.by_blank_subject
            .get(subject)
            .map_or(&[], Vec::as_slice)
    }

    pub(crate) fn decode(
        &mut self,
        head: RdfTerm<'data>,
        session: &mut Session<'_>,
    ) -> NativeResult<DecodedRdfList<'data>> {
        // Check cancellation/deadline state before even inspecting an
        // attacker-controlled graph or allocating a traversal ledger.
        session.finish()?;
        let root = match head {
            RdfTerm::Iri(RDF_NIL) => {
                return Ok(DecodedRdfList {
                    items: Vec::new(),
                    cells: Vec::new(),
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
        let mut pending_owners = HashMap::new();
        let mut visited = Vec::new();
        let mut visited_set = HashSet::new();
        let mut items = Vec::new();
        let mut consumed = Vec::new();
        let mut current = root;
        loop {
            let next_length = items
                .len()
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native RDF list length overflow"))?;
            enforce_length(next_length, session)?;

            session.step(1)?;
            if visited_set.contains(current) {
                return Err(unsupported("native cyclic RDF collection"));
            }
            reserve_hash_item(&mut visited_set, session)?;
            if !visited_set.insert(current) {
                return Err(NativeError::protocol(
                    "native RDF list cycle index changed during insertion",
                ));
            }
            reserve_item(&mut visited, session)?;
            visited.push(current);

            self.claim(current, root, &mut pending_owners, session)?;
            self.consume_list_markers(current, &mut consumed, session)?;
            let first =
                self.unique_edge(current, RDF_FIRST, "native forked RDF first edge", session)?;
            let rest =
                self.unique_edge(current, RDF_REST, "native forked RDF rest edge", session)?;

            reserve_item(&mut items, session)?;
            items.push(self.triples[first].object);
            reserve_item(&mut consumed, session)?;
            consumed.push(first);
            reserve_item(&mut consumed, session)?;
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

        session.finish()?;
        reserve_hash_map_additional(&mut self.owners, pending_owners.len(), session)?;
        self.owners.extend(pending_owners);
        consumed.sort_unstable();
        consumed.dedup();
        Ok(DecodedRdfList {
            items,
            cells: visited,
            consumed,
        })
    }

    fn claim(
        &mut self,
        cell: &'data str,
        root: &'data str,
        pending: &mut HashMap<&'data str, &'data str>,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        session.step(1)?;
        if self
            .owners
            .get(cell)
            .is_some_and(|existing| *existing != root)
        {
            return Err(unsupported("native shared RDF collection tail"));
        }
        session.step(1)?;
        if pending.get(cell).is_some_and(|existing| *existing != root) {
            return Err(unsupported("native shared RDF collection tail"));
        }
        if !self.owners.contains_key(cell) && !pending.contains_key(cell) {
            reserve_hash_map_item(pending, session)?;
            pending.insert(cell, root);
        }
        Ok(())
    }

    fn unique_edge(
        &mut self,
        subject: &'data str,
        predicate: &str,
        malformed: &'static str,
        session: &mut Session<'_>,
    ) -> NativeResult<usize> {
        let mut selected = None;
        for index in self.blank_subject_indexes(subject) {
            session.step(1)?;
            let triple = self.triples.get(*index).ok_or_else(|| {
                NativeError::protocol("native RDF list subject index exceeds graph")
            })?;
            if triple.predicate == predicate && selected.replace(*index).is_some() {
                return Err(cardinality(malformed));
            }
        }
        selected.ok_or_else(|| cardinality("native incomplete RDF collection cell"))
    }

    fn consume_list_markers(
        &mut self,
        subject: &'data str,
        consumed: &mut Vec<usize>,
        session: &mut Session<'_>,
    ) -> NativeResult<()> {
        for index in self.blank_subject_indexes(subject) {
            session.step(1)?;
            let triple = self.triples.get(*index).ok_or_else(|| {
                NativeError::protocol("native RDF list subject index exceeds graph")
            })?;
            if triple.predicate == RDF_TYPE && triple.object == RdfTerm::Iri(RDF_LIST) {
                reserve_item(consumed, session)?;
                consumed.push(*index);
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

fn reserve_hash_item<T: Eq + std::hash::Hash>(
    values: &mut HashSet<T>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let bytes = std::mem::size_of::<T>()
        .checked_add(std::mem::size_of::<usize>())
        .ok_or_else(|| NativeError::limit("native RDF list allocation accounting overflow"))?;
    session.reserve_bytes(bytes)?;
    values
        .try_reserve(1)
        .map_err(|_| NativeError::limit("native RDF list allocation failed"))
}

fn reserve_hash_map_item<K: Eq + std::hash::Hash, V>(
    values: &mut HashMap<K, V>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    reserve_hash_map_additional(values, 1, session)
}

fn reserve_hash_map_additional<K: Eq + std::hash::Hash, V>(
    values: &mut HashMap<K, V>,
    additional: usize,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    if additional == 0 {
        return Ok(());
    }
    let bytes = std::mem::size_of::<K>()
        .checked_add(std::mem::size_of::<V>())
        .and_then(|value| value.checked_add(std::mem::size_of::<usize>()))
        .and_then(|value| value.checked_mul(additional))
        .ok_or_else(|| NativeError::limit("native RDF list allocation accounting overflow"))?;
    session.reserve_bytes(bytes)?;
    values
        .try_reserve(additional)
        .map_err(|_| NativeError::limit("native RDF list allocation failed"))
}

fn usize_as_u64(value: usize, message: &'static str) -> NativeResult<u64> {
    u64::try_from(value).map_err(|_| NativeError::limit(message))
}

fn unsupported(message: &'static str) -> NativeError {
    NativeError::new("NATIVE_RDF_MAPPING_UNSUPPORTED", message)
}

fn cardinality(message: &'static str) -> NativeError {
    NativeError::new("NATIVE_RDF_MAPPING_CARDINALITY", message)
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
        RdfListDecoder::new(graph, &mut session)?.decode(head, &mut session)
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
    fn cycles_cardinality_conflicts_and_wrong_term_types_fail_closed() {
        let limits = Limits::default();
        let cases: &[(&[RdfTriple<'static>], RdfTerm<'static>, &str, &str)] = &[
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, bterm("h")),
                ],
                bterm("h"),
                "cyclic",
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            ),
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, iri(RDF_NIL)),
                    edge("h", RDF_REST, bterm("t")),
                ],
                bterm("h"),
                "forked rest",
                "NATIVE_RDF_MAPPING_CARDINALITY",
            ),
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, iri(RDF_NIL)),
                ],
                bterm("h"),
                "duplicate first",
                "NATIVE_RDF_MAPPING_CARDINALITY",
            ),
            (
                &[edge("h", RDF_REST, iri(RDF_NIL))],
                bterm("h"),
                "missing first",
                "NATIVE_RDF_MAPPING_CARDINALITY",
            ),
            (
                &[edge("h", RDF_FIRST, iri("urn:a"))],
                bterm("h"),
                "missing rest",
                "NATIVE_RDF_MAPPING_CARDINALITY",
            ),
            (
                &[
                    edge("h", RDF_FIRST, iri("urn:a")),
                    edge("h", RDF_REST, RdfTerm::Literal("bad")),
                ],
                bterm("h"),
                "literal rest",
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            ),
            (
                &[],
                iri("urn:not-nil"),
                "named head",
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            ),
            (
                &[],
                RdfTerm::Literal("head"),
                "literal head",
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            ),
        ];
        for (graph, head, label, code) in cases {
            assert_eq!(
                decode_with_limits(graph, *head, &limits).unwrap_err().code,
                *code,
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
        let mut decoder = RdfListDecoder::new(&graph, &mut session).expect("decoder");
        assert_eq!(
            decoder
                .decode(bterm("h1"), &mut session)
                .expect("first root")
                .items
                .len(),
            2
        );
        let error = decoder.decode(bterm("h2"), &mut session).unwrap_err();
        assert_eq!(error.code, "NATIVE_RDF_MAPPING_UNSUPPORTED");
        assert!(error.message.contains("shared"));
    }

    #[test]
    fn large_list_subject_and_pending_owner_indexes_stay_within_linear_work() {
        const CELLS: usize = 1_024;
        let labels = (0..CELLS)
            .map(|index| format!("cell-{index}"))
            .collect::<Vec<_>>();
        let mut graph = Vec::with_capacity(CELLS * 2);
        for index in 0..CELLS {
            graph.push(RdfTriple {
                subject: RdfResource::Blank(labels[index].as_str()),
                predicate: RDF_FIRST,
                object: RdfTerm::Iri("urn:item"),
            });
            graph.push(RdfTriple {
                subject: RdfResource::Blank(labels[index].as_str()),
                predicate: RDF_REST,
                object: if index + 1 == CELLS {
                    RdfTerm::Iri(RDF_NIL)
                } else {
                    RdfTerm::Blank(labels[index + 1].as_str())
                },
            });
        }

        let mut limits = Limits::default();
        limits.max_canonical_work = 20_000;
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut decoder = RdfListDecoder::new(&graph, &mut session)
            .expect("linear subject index stays within work budget");
        let decoded = decoder
            .decode(RdfTerm::Blank(labels[0].as_str()), &mut session)
            .expect("linear pending-owner ledger stays within work budget");
        assert_eq!(decoded.items.len(), CELLS);
        assert_eq!(decoded.cells.len(), CELLS);
        assert_eq!(decoded.consumed.len(), CELLS * 2);
        assert_eq!(decoder.owners.len(), CELLS);
    }

    #[test]
    fn malformed_list_does_not_commit_pending_owner_index() {
        let graph = [
            edge("h", RDF_FIRST, iri("urn:a")),
            edge("h", RDF_REST, bterm("tail")),
            edge("tail", RDF_FIRST, iri("urn:b")),
            edge("tail", RDF_REST, RdfTerm::Literal("bad-tail")),
        ];
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut decoder = RdfListDecoder::new(&graph, &mut session).expect("decoder");
        let error = decoder.decode(bterm("h"), &mut session).unwrap_err();
        assert_eq!(error.code, "NATIVE_RDF_MAPPING_UNSUPPORTED");
        assert!(decoder.owners.is_empty());
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
                .expect("decoder")
                .decode(bterm("h"), &mut session)
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
