//! Owned, private canonical model arena primitives.

mod canonical;

pub(crate) use canonical::{scan_canonical, Category, ScanBudget};

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
    pub(crate) fn try_push(&mut self, row: CanonicalRow) -> crate::error::NativeResult<()> {
        self.rows.try_reserve(1).map_err(|_| {
            crate::error::NativeError::limit("native model arena allocation failed")
        })?;
        self.rows.push(row);
        Ok(())
    }
}
