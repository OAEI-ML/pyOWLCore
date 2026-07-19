//! Immutable tables published by the retained arena builder.

use std::sync::Arc;

use crate::error::{NativeError, NativeResult};

use super::canonical::Category;
use super::ids::{CanonicalRowId, DocumentId};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum SequenceKind {
    Ordered,
    CanonicalSet,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct FrozenCanonicalRow {
    category: Category,
    bytes: Box<[u8]>,
}

impl FrozenCanonicalRow {
    pub(crate) fn new(category: Category, bytes: Box<[u8]>) -> Self {
        Self { category, bytes }
    }

    pub(crate) const fn category(&self) -> Category {
        self.category
    }

    pub(crate) fn bytes(&self) -> &[u8] {
        &self.bytes
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct FrozenSequence {
    kind: SequenceKind,
    elements: Box<[CanonicalRowId]>,
}

impl FrozenSequence {
    pub(crate) fn new(kind: SequenceKind, elements: Box<[CanonicalRowId]>) -> Self {
        Self { kind, elements }
    }

    pub(crate) const fn kind(&self) -> SequenceKind {
        self.kind
    }

    pub(crate) fn elements(&self) -> &[CanonicalRowId] {
        &self.elements
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct FrozenDocument {
    scope: [u8; 32],
}

impl FrozenDocument {
    pub(crate) const fn new(scope: [u8; 32]) -> Self {
        Self { scope }
    }

    pub(crate) const fn scope(&self) -> &[u8; 32] {
        &self.scope
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct FrozenAnonymousIdentity {
    document: DocumentId,
    document_scope: [u8; 32],
    local_key: Box<[u8]>,
}

impl FrozenAnonymousIdentity {
    pub(crate) fn new(
        document: DocumentId,
        document_scope: [u8; 32],
        local_key: Box<[u8]>,
    ) -> Self {
        Self {
            document,
            document_scope,
            local_key,
        }
    }

    pub(crate) const fn document(&self) -> DocumentId {
        self.document
    }

    pub(crate) const fn document_scope(&self) -> &[u8; 32] {
        &self.document_scope
    }

    pub(crate) fn local_key(&self) -> &[u8] {
        &self.local_key
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct ArenaCounters {
    pub(crate) row_requests: u64,
    pub(crate) row_hits: u64,
    pub(crate) unique_rows: u64,
    pub(crate) sequence_requests: u64,
    pub(crate) sequence_hits: u64,
    pub(crate) unique_sequences: u64,
    pub(crate) document_requests: u64,
    pub(crate) document_hits: u64,
    pub(crate) unique_documents: u64,
    pub(crate) anonymous_requests: u64,
    pub(crate) anonymous_hits: u64,
    pub(crate) unique_anonymous: u64,
    pub(crate) peak_accounted_bytes: u64,
    pub(crate) retained_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ArenaTables {
    canonical_rows: Box<[FrozenCanonicalRow]>,
    sequences: Box<[FrozenSequence]>,
    documents: Box<[FrozenDocument]>,
    anonymous: Box<[FrozenAnonymousIdentity]>,
}

impl ArenaTables {
    pub(crate) fn new(
        canonical_rows: Box<[FrozenCanonicalRow]>,
        sequences: Box<[FrozenSequence]>,
        documents: Box<[FrozenDocument]>,
        anonymous: Box<[FrozenAnonymousIdentity]>,
    ) -> Self {
        Self {
            canonical_rows,
            sequences,
            documents,
            anonymous,
        }
    }

    pub(crate) fn canonical_rows(&self) -> &[FrozenCanonicalRow] {
        &self.canonical_rows
    }

    pub(crate) fn sequences(&self) -> &[FrozenSequence] {
        &self.sequences
    }

    pub(crate) fn documents(&self) -> &[FrozenDocument] {
        &self.documents
    }

    pub(crate) fn anonymous(&self) -> &[FrozenAnonymousIdentity] {
        &self.anonymous
    }

    pub(crate) fn canonical_row(
        &self,
        identifier: CanonicalRowId,
    ) -> NativeResult<&FrozenCanonicalRow> {
        self.canonical_rows
            .get(identifier.index())
            .ok_or_else(|| NativeError::protocol("native canonical row id is out of bounds"))
    }

    pub(crate) fn document(&self, identifier: DocumentId) -> NativeResult<&FrozenDocument> {
        self.documents
            .get(identifier.index())
            .ok_or_else(|| NativeError::protocol("native document id is out of bounds"))
    }
}

pub(crate) type SharedArenaTables = Arc<ArenaTables>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn immutable_tables_check_identifiers_before_access() {
        let tables = ArenaTables::new(
            vec![FrozenCanonicalRow::new(
                Category::Iri,
                b"iri".to_vec().into_boxed_slice(),
            )]
            .into_boxed_slice(),
            Vec::new().into_boxed_slice(),
            vec![FrozenDocument::new([7; 32])].into_boxed_slice(),
            Vec::new().into_boxed_slice(),
        );
        assert_eq!(
            tables
                .canonical_row(CanonicalRowId::from_raw(0))
                .expect("row")
                .bytes(),
            b"iri"
        );
        assert_eq!(
            tables
                .canonical_row(CanonicalRowId::from_raw(1))
                .unwrap_err()
                .code,
            "NATIVE_PROTOCOL"
        );
        assert_eq!(
            tables
                .document(DocumentId::from_raw(0))
                .expect("document")
                .scope(),
            &[7; 32]
        );
    }

    #[test]
    fn sequence_kinds_remain_semantically_distinct() {
        let row = CanonicalRowId::from_raw(3);
        let ordered = FrozenSequence::new(SequenceKind::Ordered, vec![row].into_boxed_slice());
        let set = FrozenSequence::new(SequenceKind::CanonicalSet, vec![row].into_boxed_slice());
        assert_ne!(ordered, set);
        assert_eq!(ordered.kind(), SequenceKind::Ordered);
        assert_eq!(set.kind(), SequenceKind::CanonicalSet);
    }
}
