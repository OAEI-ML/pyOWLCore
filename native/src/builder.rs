//! Checked mutable construction for immutable retained native arena tables.

use std::collections::HashMap;
use std::mem::size_of;

use crate::error::{NativeError, NativeResult};
use crate::limits::Limits;

use super::arena::NativeArena;
use super::canonical::{scan_canonical, Category, ScanBudget};
use super::ids::{AnonymousId, CanonicalRowId, DocumentId, SequenceId};
use super::tables::{
    ArenaCounters, ArenaTables, FrozenAnonymousIdentity, FrozenCanonicalRow, FrozenDocument,
    FrozenSequence, SequenceKind,
};

type RowBucketer = fn(&[u8]) -> u64;
type SequenceBucketer = fn(SequenceKind, &[CanonicalRowId]) -> u64;
type AnonymousBucketer = fn(&[u8; 32], &[u8]) -> u64;

#[derive(Clone, Copy, Debug)]
struct BuilderLimits {
    max_rows: u64,
    max_sequences: u64,
    max_documents: u64,
    max_anonymous: u64,
    max_sequence_arity: u64,
    max_local_key_bytes: u64,
    max_memory_bytes: Option<u64>,
}

impl BuilderLimits {
    fn from_native(limits: &Limits) -> Self {
        Self {
            max_rows: limits.max_terms,
            max_sequences: limits.max_terms,
            max_documents: limits.max_documents,
            max_anonymous: limits.max_terms,
            max_sequence_arity: limits.max_sequence_arity,
            max_local_key_bytes: limits.max_canonical_work,
            max_memory_bytes: limits.max_memory_bytes,
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct AllocationBudget {
    maximum: Option<u64>,
    used: u64,
    peak: u64,
}

impl AllocationBudget {
    fn new(maximum: Option<u64>) -> Self {
        Self {
            maximum,
            used: 0,
            peak: 0,
        }
    }

    fn plan(&self, additional: usize) -> NativeResult<u64> {
        let additional = u64::try_from(additional)
            .map_err(|_| NativeError::limit("native arena allocation exceeds u64"))?;
        let next = self
            .used
            .checked_add(additional)
            .ok_or_else(|| NativeError::limit("native arena memory accounting overflow"))?;
        if self.maximum.is_some_and(|maximum| next > maximum) {
            return Err(NativeError::limit(
                "native arena allocation exceeds max_memory_bytes",
            ));
        }
        Ok(next)
    }

    fn commit(&mut self, next: u64) {
        self.used = next;
        self.peak = self.peak.max(next);
    }
}

#[derive(Clone, Debug)]
struct DraftCanonicalRow {
    identifier: CanonicalRowId,
    category: Category,
    bytes: Vec<u8>,
}

#[derive(Clone, Debug)]
struct DraftSequence {
    identifier: SequenceId,
    kind: SequenceKind,
    elements: Vec<CanonicalRowId>,
}

#[derive(Clone, Copy, Debug)]
struct DraftDocument {
    identifier: DocumentId,
    scope: [u8; 32],
}

#[derive(Clone, Debug)]
struct DraftAnonymousIdentity {
    identifier: AnonymousId,
    document: DocumentId,
    document_scope: [u8; 32],
    local_key: Vec<u8>,
}

#[derive(Debug)]
pub(crate) struct NativeArenaBuilder {
    scan_limits: Limits,
    limits: BuilderLimits,
    budget: AllocationBudget,
    rows: Vec<DraftCanonicalRow>,
    row_buckets: HashMap<u64, Vec<CanonicalRowId>>,
    sequences: Vec<DraftSequence>,
    sequence_buckets: HashMap<u64, Vec<SequenceId>>,
    documents: Vec<DraftDocument>,
    anonymous: Vec<DraftAnonymousIdentity>,
    anonymous_buckets: HashMap<u64, Vec<AnonymousId>>,
    counters: ArenaCounters,
    row_bucketer: RowBucketer,
    sequence_bucketer: SequenceBucketer,
    anonymous_bucketer: AnonymousBucketer,
}

impl NativeArenaBuilder {
    pub(crate) fn new(limits: &Limits) -> Self {
        Self::with_configuration(
            *limits,
            BuilderLimits::from_native(limits),
            bucket_bytes,
            bucket_sequence,
            bucket_anonymous,
        )
    }

    fn with_configuration(
        scan_limits: Limits,
        limits: BuilderLimits,
        row_bucketer: RowBucketer,
        sequence_bucketer: SequenceBucketer,
        anonymous_bucketer: AnonymousBucketer,
    ) -> Self {
        Self {
            scan_limits,
            limits,
            budget: AllocationBudget::new(limits.max_memory_bytes),
            rows: Vec::new(),
            row_buckets: HashMap::new(),
            sequences: Vec::new(),
            sequence_buckets: HashMap::new(),
            documents: Vec::new(),
            anonymous: Vec::new(),
            anonymous_buckets: HashMap::new(),
            counters: ArenaCounters::default(),
            row_bucketer,
            sequence_bucketer,
            anonymous_bucketer,
        }
    }

    pub(crate) fn intern_canonical_row(&mut self, data: &[u8]) -> NativeResult<CanonicalRowId> {
        let mut scan_budget = ScanBudget::from_limits(&self.scan_limits);
        let category = scan_canonical(data, &mut scan_budget)?;
        bump(
            &mut self.counters.row_requests,
            "native row request counter overflow",
        )?;
        let bucket_key = (self.row_bucketer)(data);
        if let Some(bucket) = self.row_buckets.get(&bucket_key) {
            for identifier in bucket {
                let retained = self.row_draft(*identifier)?;
                if retained.bytes == data {
                    bump(
                        &mut self.counters.row_hits,
                        "native row hit counter overflow",
                    )?;
                    return Ok(*identifier);
                }
            }
        }

        check_next_count(
            self.rows.len(),
            self.limits.max_rows,
            "native canonical row count exceeds limits",
        )?;
        let identifier = CanonicalRowId::try_from_index(self.rows.len())?;
        let additional = checked_size_sum(&[
            data.len(),
            size_of::<DraftCanonicalRow>(),
            size_of::<CanonicalRowId>(),
        ])?;
        let next_budget = self.budget.plan(additional)?;
        let mut owned = Vec::new();
        owned
            .try_reserve_exact(data.len())
            .map_err(|_| NativeError::limit("native canonical row allocation failed"))?;
        owned.extend_from_slice(data);
        self.rows
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native canonical row table allocation failed"))?;
        let new_bucket = !self.row_buckets.contains_key(&bucket_key);
        if new_bucket {
            self.row_buckets
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native row interner allocation failed"))?;
        } else {
            self.row_buckets
                .get_mut(&bucket_key)
                .ok_or_else(|| NativeError::protocol("native row interner bucket disappeared"))?
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native row interner bucket allocation failed"))?;
        }
        let mut created_bucket = Vec::new();
        if new_bucket {
            created_bucket
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native row interner bucket allocation failed"))?;
        }

        self.rows.push(DraftCanonicalRow {
            identifier,
            category,
            bytes: owned,
        });
        if new_bucket {
            created_bucket.push(identifier);
            self.row_buckets.insert(bucket_key, created_bucket);
        } else {
            self.row_buckets
                .get_mut(&bucket_key)
                .ok_or_else(|| NativeError::protocol("native row interner bucket disappeared"))?
                .push(identifier);
        }
        self.budget.commit(next_budget);
        bump(
            &mut self.counters.unique_rows,
            "native unique row counter overflow",
        )?;
        Ok(identifier)
    }

    pub(crate) fn intern_sequence(
        &mut self,
        kind: SequenceKind,
        elements: &[CanonicalRowId],
    ) -> NativeResult<SequenceId> {
        let arity = u64::try_from(elements.len())
            .map_err(|_| NativeError::limit("native sequence arity exceeds u64"))?;
        if arity > self.limits.max_sequence_arity {
            return Err(NativeError::limit("native sequence arity exceeds limits"));
        }
        for identifier in elements {
            self.row_draft(*identifier)?;
        }
        bump(
            &mut self.counters.sequence_requests,
            "native sequence request counter overflow",
        )?;

        let element_bytes = checked_allocation_bytes::<CanonicalRowId>(elements.len())?;
        self.budget.plan(element_bytes)?;
        let mut canonical = Vec::new();
        canonical
            .try_reserve_exact(elements.len())
            .map_err(|_| NativeError::limit("native sequence member allocation failed"))?;
        canonical.extend_from_slice(elements);
        if kind == SequenceKind::CanonicalSet {
            canonical.sort_unstable_by(|left, right| {
                self.rows[left.index()]
                    .bytes
                    .cmp(&self.rows[right.index()].bytes)
            });
            canonical.dedup();
        }

        let bucket_key = (self.sequence_bucketer)(kind, &canonical);
        if let Some(bucket) = self.sequence_buckets.get(&bucket_key) {
            for identifier in bucket {
                let retained = self.sequence_draft(*identifier)?;
                if retained.kind == kind && retained.elements == canonical {
                    bump(
                        &mut self.counters.sequence_hits,
                        "native sequence hit counter overflow",
                    )?;
                    return Ok(*identifier);
                }
            }
        }

        check_next_count(
            self.sequences.len(),
            self.limits.max_sequences,
            "native sequence count exceeds limits",
        )?;
        let identifier = SequenceId::try_from_index(self.sequences.len())?;
        let additional = checked_size_sum(&[
            element_bytes,
            size_of::<DraftSequence>(),
            size_of::<SequenceId>(),
        ])?;
        let next_budget = self.budget.plan(additional)?;
        self.sequences
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native sequence table allocation failed"))?;
        let new_bucket = !self.sequence_buckets.contains_key(&bucket_key);
        if new_bucket {
            self.sequence_buckets
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native sequence interner allocation failed"))?;
        } else {
            self.sequence_buckets
                .get_mut(&bucket_key)
                .ok_or_else(|| {
                    NativeError::protocol("native sequence interner bucket disappeared")
                })?
                .try_reserve(1)
                .map_err(|_| {
                    NativeError::limit("native sequence interner bucket allocation failed")
                })?;
        }
        let mut created_bucket = Vec::new();
        if new_bucket {
            created_bucket.try_reserve(1).map_err(|_| {
                NativeError::limit("native sequence interner bucket allocation failed")
            })?;
        }

        self.sequences.push(DraftSequence {
            identifier,
            kind,
            elements: canonical,
        });
        if new_bucket {
            created_bucket.push(identifier);
            self.sequence_buckets.insert(bucket_key, created_bucket);
        } else {
            self.sequence_buckets
                .get_mut(&bucket_key)
                .ok_or_else(|| {
                    NativeError::protocol("native sequence interner bucket disappeared")
                })?
                .push(identifier);
        }
        self.budget.commit(next_budget);
        bump(
            &mut self.counters.unique_sequences,
            "native unique sequence counter overflow",
        )?;
        Ok(identifier)
    }

    pub(crate) fn intern_document_scope(
        &mut self,
        document_scope: [u8; 32],
    ) -> NativeResult<DocumentId> {
        bump(
            &mut self.counters.document_requests,
            "native document request counter overflow",
        )?;
        if let Some(retained) = self
            .documents
            .iter()
            .find(|document| document.scope == document_scope)
        {
            bump(
                &mut self.counters.document_hits,
                "native document hit counter overflow",
            )?;
            return Ok(retained.identifier);
        }
        check_next_count(
            self.documents.len(),
            self.limits.max_documents,
            "native document count exceeds limits",
        )?;
        let identifier = DocumentId::try_from_index(self.documents.len())?;
        let next_budget = self.budget.plan(size_of::<DraftDocument>())?;
        self.documents
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native document table allocation failed"))?;
        self.documents.push(DraftDocument {
            identifier,
            scope: document_scope,
        });
        self.budget.commit(next_budget);
        bump(
            &mut self.counters.unique_documents,
            "native unique document counter overflow",
        )?;
        Ok(identifier)
    }

    pub(crate) fn intern_anonymous(
        &mut self,
        document: DocumentId,
        local_key: &[u8],
    ) -> NativeResult<AnonymousId> {
        if local_key.is_empty() {
            return Err(NativeError::protocol(
                "native anonymous local key must be nonempty",
            ));
        }
        let local_size = u64::try_from(local_key.len())
            .map_err(|_| NativeError::limit("native anonymous local key exceeds u64"))?;
        if local_size > self.limits.max_local_key_bytes {
            return Err(NativeError::limit(
                "native anonymous local key exceeds limits",
            ));
        }
        let document_scope = self.document_draft(document)?.scope;
        bump(
            &mut self.counters.anonymous_requests,
            "native anonymous request counter overflow",
        )?;
        let bucket_key = (self.anonymous_bucketer)(&document_scope, local_key);
        if let Some(bucket) = self.anonymous_buckets.get(&bucket_key) {
            for identifier in bucket {
                let retained = self.anonymous_draft(*identifier)?;
                if retained.document_scope == document_scope && retained.local_key == local_key {
                    bump(
                        &mut self.counters.anonymous_hits,
                        "native anonymous hit counter overflow",
                    )?;
                    return Ok(*identifier);
                }
            }
        }

        check_next_count(
            self.anonymous.len(),
            self.limits.max_anonymous,
            "native anonymous count exceeds limits",
        )?;
        let identifier = AnonymousId::try_from_index(self.anonymous.len())?;
        let additional = checked_size_sum(&[
            local_key.len(),
            size_of::<DraftAnonymousIdentity>(),
            size_of::<AnonymousId>(),
        ])?;
        let next_budget = self.budget.plan(additional)?;
        let mut owned_key = Vec::new();
        owned_key
            .try_reserve_exact(local_key.len())
            .map_err(|_| NativeError::limit("native anonymous local-key allocation failed"))?;
        owned_key.extend_from_slice(local_key);
        self.anonymous
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native anonymous table allocation failed"))?;
        let new_bucket = !self.anonymous_buckets.contains_key(&bucket_key);
        if new_bucket {
            self.anonymous_buckets
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native anonymous interner allocation failed"))?;
        } else {
            self.anonymous_buckets
                .get_mut(&bucket_key)
                .ok_or_else(|| {
                    NativeError::protocol("native anonymous interner bucket disappeared")
                })?
                .try_reserve(1)
                .map_err(|_| {
                    NativeError::limit("native anonymous interner bucket allocation failed")
                })?;
        }
        let mut created_bucket = Vec::new();
        if new_bucket {
            created_bucket.try_reserve(1).map_err(|_| {
                NativeError::limit("native anonymous interner bucket allocation failed")
            })?;
        }

        self.anonymous.push(DraftAnonymousIdentity {
            identifier,
            document,
            document_scope,
            local_key: owned_key,
        });
        if new_bucket {
            created_bucket.push(identifier);
            self.anonymous_buckets.insert(bucket_key, created_bucket);
        } else {
            self.anonymous_buckets
                .get_mut(&bucket_key)
                .ok_or_else(|| {
                    NativeError::protocol("native anonymous interner bucket disappeared")
                })?
                .push(identifier);
        }
        self.budget.commit(next_budget);
        bump(
            &mut self.counters.unique_anonymous,
            "native unique anonymous counter overflow",
        )?;
        Ok(identifier)
    }

    pub(crate) fn freeze(mut self) -> NativeResult<NativeArena> {
        self.rows
            .sort_unstable_by(|left, right| left.bytes.cmp(&right.bytes));
        let row_remap_bytes = checked_allocation_bytes::<CanonicalRowId>(self.rows.len())?;
        let next_budget = self.budget.plan(row_remap_bytes)?;
        let mut row_remap = Vec::new();
        row_remap
            .try_reserve_exact(self.rows.len())
            .map_err(|_| NativeError::limit("native row remap allocation failed"))?;
        row_remap.resize(self.rows.len(), CanonicalRowId::from_raw(0));
        for (new_index, row) in self.rows.iter().enumerate() {
            let new_identifier = CanonicalRowId::try_from_index(new_index)?;
            row_remap[row.identifier.index()] = new_identifier;
        }
        self.budget.commit(next_budget);

        for sequence in &mut self.sequences {
            for identifier in &mut sequence.elements {
                *identifier = *row_remap.get(identifier.index()).ok_or_else(|| {
                    NativeError::protocol("native sequence row remap is out of bounds")
                })?;
            }
            if sequence.kind == SequenceKind::CanonicalSet {
                sequence.elements.sort_unstable();
                sequence.elements.dedup();
            }
        }
        self.sequences.sort_unstable_by(|left, right| {
            (left.kind, left.elements.as_slice()).cmp(&(right.kind, right.elements.as_slice()))
        });

        self.documents
            .sort_unstable_by_key(|document| document.scope);
        let document_remap_bytes = checked_allocation_bytes::<DocumentId>(self.documents.len())?;
        let next_budget = self.budget.plan(document_remap_bytes)?;
        let mut document_remap = Vec::new();
        document_remap
            .try_reserve_exact(self.documents.len())
            .map_err(|_| NativeError::limit("native document remap allocation failed"))?;
        document_remap.resize(self.documents.len(), DocumentId::from_raw(0));
        for (new_index, document) in self.documents.iter().enumerate() {
            document_remap[document.identifier.index()] = DocumentId::try_from_index(new_index)?;
        }
        self.budget.commit(next_budget);
        for identity in &mut self.anonymous {
            identity.document =
                *document_remap
                    .get(identity.document.index())
                    .ok_or_else(|| {
                        NativeError::protocol("native anonymous document remap is out of bounds")
                    })?;
        }
        self.anonymous.sort_unstable_by(|left, right| {
            (left.document_scope, left.local_key.as_slice())
                .cmp(&(right.document_scope, right.local_key.as_slice()))
        });

        let frozen_metadata = checked_size_sum(&[
            checked_allocation_bytes::<FrozenCanonicalRow>(self.rows.len())?,
            checked_allocation_bytes::<FrozenSequence>(self.sequences.len())?,
            checked_allocation_bytes::<FrozenDocument>(self.documents.len())?,
            checked_allocation_bytes::<FrozenAnonymousIdentity>(self.anonymous.len())?,
        ])?;
        let next_budget = self.budget.plan(frozen_metadata)?;
        let mut rows = Vec::new();
        rows.try_reserve_exact(self.rows.len())
            .map_err(|_| NativeError::limit("native frozen row table allocation failed"))?;
        let mut retained_bytes = 0_u64;
        for row in self.rows {
            retained_bytes = add_retained(retained_bytes, row.bytes.len())?;
            rows.push(FrozenCanonicalRow::new(
                row.category,
                row.bytes.into_boxed_slice(),
            ));
        }
        let mut sequences = Vec::new();
        sequences
            .try_reserve_exact(self.sequences.len())
            .map_err(|_| NativeError::limit("native frozen sequence table allocation failed"))?;
        for sequence in self.sequences {
            retained_bytes = add_retained(
                retained_bytes,
                checked_allocation_bytes::<CanonicalRowId>(sequence.elements.len())?,
            )?;
            sequences.push(FrozenSequence::new(
                sequence.kind,
                sequence.elements.into_boxed_slice(),
            ));
        }
        let mut documents = Vec::new();
        documents
            .try_reserve_exact(self.documents.len())
            .map_err(|_| NativeError::limit("native frozen document table allocation failed"))?;
        for document in self.documents {
            documents.push(FrozenDocument::new(document.scope));
        }
        let mut anonymous = Vec::new();
        anonymous
            .try_reserve_exact(self.anonymous.len())
            .map_err(|_| NativeError::limit("native frozen anonymous table allocation failed"))?;
        for identity in self.anonymous {
            retained_bytes = add_retained(retained_bytes, identity.local_key.len())?;
            anonymous.push(FrozenAnonymousIdentity::new(
                identity.document,
                identity.document_scope,
                identity.local_key.into_boxed_slice(),
            ));
        }
        retained_bytes = add_retained(retained_bytes, frozen_metadata)?;
        self.budget.commit(next_budget);
        self.counters.peak_accounted_bytes = self.budget.peak;
        self.counters.retained_bytes = retained_bytes;
        Ok(NativeArena::new(
            ArenaTables::new(
                rows.into_boxed_slice(),
                sequences.into_boxed_slice(),
                documents.into_boxed_slice(),
                anonymous.into_boxed_slice(),
            ),
            self.counters,
        ))
    }

    fn row_draft(&self, identifier: CanonicalRowId) -> NativeResult<&DraftCanonicalRow> {
        let row = self
            .rows
            .get(identifier.index())
            .ok_or_else(|| NativeError::protocol("native canonical row id is out of bounds"))?;
        if row.identifier != identifier {
            return Err(NativeError::protocol(
                "native canonical row identifier is inconsistent",
            ));
        }
        Ok(row)
    }

    fn sequence_draft(&self, identifier: SequenceId) -> NativeResult<&DraftSequence> {
        let sequence = self
            .sequences
            .get(identifier.index())
            .ok_or_else(|| NativeError::protocol("native sequence id is out of bounds"))?;
        if sequence.identifier != identifier {
            return Err(NativeError::protocol(
                "native sequence identifier is inconsistent",
            ));
        }
        Ok(sequence)
    }

    fn document_draft(&self, identifier: DocumentId) -> NativeResult<&DraftDocument> {
        let document = self
            .documents
            .get(identifier.index())
            .ok_or_else(|| NativeError::protocol("native document id is out of bounds"))?;
        if document.identifier != identifier {
            return Err(NativeError::protocol(
                "native document identifier is inconsistent",
            ));
        }
        Ok(document)
    }

    fn anonymous_draft(&self, identifier: AnonymousId) -> NativeResult<&DraftAnonymousIdentity> {
        let identity = self
            .anonymous
            .get(identifier.index())
            .ok_or_else(|| NativeError::protocol("native anonymous id is out of bounds"))?;
        if identity.identifier != identifier {
            return Err(NativeError::protocol(
                "native anonymous identifier is inconsistent",
            ));
        }
        Ok(identity)
    }
}

fn check_next_count(current: usize, maximum: u64, message: &'static str) -> NativeResult<()> {
    let next = current
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("native arena count overflow"))?;
    if u64::try_from(next).map_or(true, |value| value > maximum) {
        return Err(NativeError::limit(message));
    }
    Ok(())
}

fn bump(counter: &mut u64, message: &'static str) -> NativeResult<()> {
    *counter = counter
        .checked_add(1)
        .ok_or_else(|| NativeError::limit(message))?;
    Ok(())
}

fn checked_allocation_bytes<T>(count: usize) -> NativeResult<usize> {
    count
        .checked_mul(size_of::<T>())
        .ok_or_else(|| NativeError::limit("native arena allocation size overflow"))
}

fn checked_size_sum(values: &[usize]) -> NativeResult<usize> {
    values.iter().try_fold(0_usize, |total, value| {
        total
            .checked_add(*value)
            .ok_or_else(|| NativeError::limit("native arena allocation size overflow"))
    })
}

fn add_retained(total: u64, additional: usize) -> NativeResult<u64> {
    total
        .checked_add(
            u64::try_from(additional)
                .map_err(|_| NativeError::limit("native retained size exceeds u64"))?,
        )
        .ok_or_else(|| NativeError::limit("native retained size overflow"))
}

fn bucket_bytes(value: &[u8]) -> u64 {
    value.iter().fold(0xcbf2_9ce4_8422_2325_u64, |hash, byte| {
        (hash ^ u64::from(*byte)).wrapping_mul(0x0000_0100_0000_01b3)
    })
}

fn bucket_sequence(kind: SequenceKind, elements: &[CanonicalRowId]) -> u64 {
    let initial = match kind {
        SequenceKind::Ordered => 0x4f52_4445_5245_4401_u64,
        SequenceKind::CanonicalSet => 0x5345_545f_4341_4e01_u64,
    };
    elements.iter().fold(initial, |hash, identifier| {
        identifier
            .raw()
            .to_le_bytes()
            .iter()
            .fold(hash, |retained, byte| {
                (retained ^ u64::from(*byte)).wrapping_mul(0x0000_0100_0000_01b3)
            })
    })
}

fn bucket_anonymous(document_scope: &[u8; 32], local_key: &[u8]) -> u64 {
    let scope_hash = bucket_bytes(document_scope);
    local_key.iter().fold(scope_hash, |hash, byte| {
        (hash ^ u64::from(*byte)).wrapping_mul(0x0000_0100_0000_01b3)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn iri(value: &str) -> Vec<u8> {
        let mut encoded = vec![1, 2, u8::try_from(value.len()).expect("short test IRI")];
        encoded.extend_from_slice(value.as_bytes());
        encoded
    }

    fn collide_bytes(_value: &[u8]) -> u64 {
        0
    }

    fn collide_sequence(_kind: SequenceKind, _elements: &[CanonicalRowId]) -> u64 {
        0
    }

    fn collide_anonymous(_scope: &[u8; 32], _local_key: &[u8]) -> u64 {
        0
    }

    fn generous_limits() -> BuilderLimits {
        BuilderLimits {
            max_rows: 1_000,
            max_sequences: 1_000,
            max_documents: 100,
            max_anonymous: 1_000,
            max_sequence_arity: 1_000,
            max_local_key_bytes: 1_000,
            max_memory_bytes: Some(16 * 1024 * 1024),
        }
    }

    fn collision_builder() -> NativeArenaBuilder {
        NativeArenaBuilder::with_configuration(
            Limits::default(),
            generous_limits(),
            collide_bytes,
            collide_sequence,
            collide_anonymous,
        )
    }

    #[test]
    fn full_bytes_resolve_deliberate_row_hash_collisions() {
        let mut builder = collision_builder();
        let first_bytes = iri("urn:first");
        let second_bytes = iri("urn:second");
        let first = builder
            .intern_canonical_row(&first_bytes)
            .expect("first row");
        let second = builder
            .intern_canonical_row(&second_bytes)
            .expect("second row");
        let repeated = builder
            .intern_canonical_row(&first_bytes)
            .expect("repeated row");
        assert_ne!(first, second);
        assert_eq!(first, repeated);

        let arena = builder.freeze().expect("frozen arena");
        let retained: Vec<&[u8]> = arena
            .canonical_rows()
            .iter()
            .map(FrozenCanonicalRow::bytes)
            .collect();
        assert_eq!(
            retained,
            vec![first_bytes.as_slice(), second_bytes.as_slice()]
        );
        assert_eq!(arena.counters().row_requests, 3);
        assert_eq!(arena.counters().row_hits, 1);
        assert_eq!(arena.counters().unique_rows, 2);
    }

    #[test]
    fn ordered_and_canonical_set_sequences_have_distinct_identity() {
        let mut builder = collision_builder();
        let first = builder
            .intern_canonical_row(&iri("urn:A"))
            .expect("first row");
        let second = builder
            .intern_canonical_row(&iri("urn:B"))
            .expect("second row");
        let ordered = builder
            .intern_sequence(SequenceKind::Ordered, &[second, first, first])
            .expect("ordered sequence");
        let different_order = builder
            .intern_sequence(SequenceKind::Ordered, &[first, second, first])
            .expect("different order");
        let set = builder
            .intern_sequence(SequenceKind::CanonicalSet, &[second, first, first])
            .expect("canonical set");
        let repeated_set = builder
            .intern_sequence(SequenceKind::CanonicalSet, &[first, second])
            .expect("repeated canonical set");
        assert_ne!(ordered, different_order);
        assert_ne!(ordered, set);
        assert_eq!(set, repeated_set);

        let arena = builder.freeze().expect("frozen arena");
        let sequences = arena.tables().sequences();
        assert_eq!(sequences.len(), 3);
        assert_eq!(
            sequences
                .iter()
                .find(|value| value.kind() == SequenceKind::CanonicalSet)
                .expect("set")
                .elements()
                .len(),
            2
        );
        assert_eq!(arena.counters().sequence_requests, 4);
        assert_eq!(arena.counters().sequence_hits, 1);
    }

    #[test]
    fn anonymous_keys_include_document_scope_and_compare_full_bytes() {
        let mut builder = collision_builder();
        let first_document = builder
            .intern_document_scope([1; 32])
            .expect("first document");
        let second_document = builder
            .intern_document_scope([2; 32])
            .expect("second document");
        let first = builder
            .intern_anonymous(first_document, b"same-local-key")
            .expect("first anonymous");
        let repeated = builder
            .intern_anonymous(first_document, b"same-local-key")
            .expect("repeated anonymous");
        let other_document = builder
            .intern_anonymous(second_document, b"same-local-key")
            .expect("other document anonymous");
        let colliding_key = builder
            .intern_anonymous(first_document, b"different-local-key")
            .expect("colliding local key");
        assert_eq!(first, repeated);
        assert_ne!(first, other_document);
        assert_ne!(first, colliding_key);

        let arena = builder.freeze().expect("frozen arena");
        assert_eq!(arena.tables().anonymous().len(), 3);
        for identity in arena.tables().anonymous() {
            let document = arena
                .document(identity.document())
                .expect("identity document");
            assert_eq!(document.scope(), identity.document_scope());
            assert!(!identity.local_key().is_empty());
        }
        assert_eq!(arena.counters().anonymous_hits, 1);
    }

    #[test]
    fn limits_fail_before_partial_row_or_sequence_publication() {
        let limits = BuilderLimits {
            max_rows: 1,
            max_sequences: 1,
            max_sequence_arity: 1,
            ..generous_limits()
        };
        let mut builder = NativeArenaBuilder::with_configuration(
            Limits::default(),
            limits,
            bucket_bytes,
            bucket_sequence,
            bucket_anonymous,
        );
        let row = builder
            .intern_canonical_row(&iri("urn:retained"))
            .expect("retained row");
        assert_eq!(
            builder
                .intern_canonical_row(&iri("urn:rejected"))
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT"
        );
        assert_eq!(
            builder
                .intern_sequence(SequenceKind::Ordered, &[row, row])
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT"
        );
        let arena = builder.freeze().expect("frozen arena");
        assert_eq!(arena.canonical_rows().len(), 1);
        assert!(arena.tables().sequences().is_empty());
    }

    #[test]
    fn memory_limit_and_invalid_values_publish_nothing() {
        let limits = BuilderLimits {
            max_memory_bytes: Some(1),
            ..generous_limits()
        };
        let mut builder = NativeArenaBuilder::with_configuration(
            Limits::default(),
            limits,
            bucket_bytes,
            bucket_sequence,
            bucket_anonymous,
        );
        assert_eq!(
            builder
                .intern_canonical_row(&iri("urn:too-large"))
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT"
        );
        assert_eq!(
            builder.intern_canonical_row(b"bad").unwrap_err().code,
            "NATIVE_WIRE_CORRUPTION"
        );
        assert_eq!(
            builder
                .intern_anonymous(DocumentId::from_raw(0), b"key")
                .unwrap_err()
                .code,
            "NATIVE_PROTOCOL"
        );
    }

    #[test]
    fn consuming_freeze_publishes_shareable_immutable_tables_and_counters() {
        let mut builder = NativeArenaBuilder::new(&Limits::default());
        builder
            .intern_canonical_row(&iri("urn:freeze"))
            .expect("row");
        let arena = builder.freeze().expect("one-shot consuming freeze");
        let clone = arena.clone();
        assert!(arena.shares_storage_with(&clone));
        assert_eq!(arena.counters().unique_rows, 1);
        assert!(arena.counters().retained_bytes > 0);
        assert!(arena.counters().peak_accounted_bytes >= arena.counters().retained_bytes);
    }

    #[test]
    fn freeze_order_is_independent_of_builder_insertion_order() {
        let mut forward = collision_builder();
        let forward_a = forward
            .intern_canonical_row(&iri("urn:A"))
            .expect("forward A");
        let forward_b = forward
            .intern_canonical_row(&iri("urn:B"))
            .expect("forward B");
        forward
            .intern_sequence(SequenceKind::Ordered, &[forward_a, forward_b])
            .expect("forward sequence");
        let forward_one = forward
            .intern_document_scope([1; 32])
            .expect("forward document one");
        let forward_two = forward
            .intern_document_scope([2; 32])
            .expect("forward document two");
        forward
            .intern_anonymous(forward_one, b"one")
            .expect("forward anonymous one");
        forward
            .intern_anonymous(forward_two, b"two")
            .expect("forward anonymous two");

        let mut reverse = collision_builder();
        let reverse_b = reverse
            .intern_canonical_row(&iri("urn:B"))
            .expect("reverse B");
        let reverse_a = reverse
            .intern_canonical_row(&iri("urn:A"))
            .expect("reverse A");
        reverse
            .intern_sequence(SequenceKind::Ordered, &[reverse_a, reverse_b])
            .expect("reverse sequence");
        let reverse_two = reverse
            .intern_document_scope([2; 32])
            .expect("reverse document two");
        let reverse_one = reverse
            .intern_document_scope([1; 32])
            .expect("reverse document one");
        reverse
            .intern_anonymous(reverse_two, b"two")
            .expect("reverse anonymous two");
        reverse
            .intern_anonymous(reverse_one, b"one")
            .expect("reverse anonymous one");

        let forward = forward.freeze().expect("forward arena");
        let reverse = reverse.freeze().expect("reverse arena");
        assert_eq!(forward.tables(), reverse.tables());
    }
}
