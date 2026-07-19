//! Checked, arena-local identifiers.

use crate::error::{NativeError, NativeResult};

macro_rules! arena_id {
    ($name:ident, $label:literal) => {
        #[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
        pub(crate) struct $name(u32);

        impl $name {
            pub(crate) fn try_from_index(index: usize) -> NativeResult<Self> {
                let raw = u32::try_from(index)
                    .map_err(|_| NativeError::limit(concat!($label, " id space exhausted")))?;
                Ok(Self(raw))
            }

            pub(crate) const fn from_raw(raw: u32) -> Self {
                Self(raw)
            }

            pub(crate) const fn raw(self) -> u32 {
                self.0
            }

            pub(crate) const fn index(self) -> usize {
                self.0 as usize
            }

            pub(crate) fn checked_next(self) -> NativeResult<Self> {
                self.0
                    .checked_add(1)
                    .map(Self)
                    .ok_or_else(|| NativeError::limit(concat!($label, " id space exhausted")))
            }
        }
    };
}

arena_id!(CanonicalRowId, "native canonical row");
arena_id!(SequenceId, "native sequence");
arena_id!(DocumentId, "native document");
arena_id!(AnonymousId, "native anonymous individual");

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_are_typed_checked_u32_values() {
        let row = CanonicalRowId::try_from_index(7).expect("row id");
        let sequence = SequenceId::try_from_index(7).expect("sequence id");
        assert_eq!(row.raw(), 7);
        assert_eq!(sequence.raw(), 7);
        assert_eq!(row.index(), 7);
        assert_eq!(row.checked_next().expect("next row").raw(), 8);
        assert_eq!(
            CanonicalRowId::from_raw(u32::MAX)
                .checked_next()
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT"
        );
    }

    #[test]
    #[cfg(target_pointer_width = "64")]
    fn identifiers_reject_indexes_wider_than_u32() {
        let too_large = usize::try_from(u64::from(u32::MAX) + 1).expect("64-bit usize");
        assert_eq!(
            DocumentId::try_from_index(too_large).unwrap_err().code,
            "NATIVE_WIRE_LIMIT"
        );
        assert_eq!(
            AnonymousId::try_from_index(too_large).unwrap_err().code,
            "NATIVE_WIRE_LIMIT"
        );
    }
}
