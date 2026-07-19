//! Retained immutable arena ownership and the foundational row smoke arena.

use std::sync::Arc;

use crate::error::NativeResult;

use super::canonical::Category;
use super::ids::{CanonicalRowId, DocumentId};
use super::tables::{
    ArenaCounters, ArenaTables, FrozenCanonicalRow, FrozenDocument, SharedArenaTables,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct CanonicalRow {
    pub(crate) category: Category,
    pub(crate) bytes: Vec<u8>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct ModelArena {
    rows: Vec<CanonicalRow>,
}

impl ModelArena {
    pub(crate) fn try_push(&mut self, row: CanonicalRow) -> NativeResult<()> {
        self.rows.try_reserve(1).map_err(|_| {
            crate::error::NativeError::limit("native model arena allocation failed")
        })?;
        self.rows.push(row);
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub(crate) struct NativeArena {
    tables: SharedArenaTables,
    counters: ArenaCounters,
}

impl NativeArena {
    pub(crate) fn new(tables: ArenaTables, counters: ArenaCounters) -> Self {
        Self {
            tables: Arc::new(tables),
            counters,
        }
    }

    pub(crate) fn tables(&self) -> &ArenaTables {
        &self.tables
    }

    pub(crate) const fn counters(&self) -> &ArenaCounters {
        &self.counters
    }

    pub(crate) fn canonical_rows(&self) -> &[FrozenCanonicalRow] {
        self.tables.canonical_rows()
    }

    pub(crate) fn documents(&self) -> &[FrozenDocument] {
        self.tables.documents()
    }

    pub(crate) fn canonical_row(
        &self,
        identifier: CanonicalRowId,
    ) -> NativeResult<&FrozenCanonicalRow> {
        self.tables.canonical_row(identifier)
    }

    pub(crate) fn document(&self, identifier: DocumentId) -> NativeResult<&FrozenDocument> {
        self.tables.document(identifier)
    }

    pub(crate) fn shares_storage_with(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.tables, &other.tables)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clones_share_one_immutable_table_owner() {
        let arena = NativeArena::new(
            ArenaTables::new(
                Vec::new().into_boxed_slice(),
                Vec::new().into_boxed_slice(),
                Vec::new().into_boxed_slice(),
                Vec::new().into_boxed_slice(),
            ),
            ArenaCounters::default(),
        );
        let clone = arena.clone();
        assert!(arena.shares_storage_with(&clone));
        assert!(arena.canonical_rows().is_empty());
        assert!(arena.documents().is_empty());
    }

    #[test]
    fn foundational_model_arena_preserves_existing_self_test_shape() {
        let mut arena = ModelArena::default();
        arena
            .try_push(CanonicalRow {
                category: Category::Iri,
                bytes: b"opaque".to_vec(),
            })
            .expect("row");
        assert_eq!(arena.rows.len(), 1);
    }
}
