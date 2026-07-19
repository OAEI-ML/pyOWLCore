//! Bounded digest lookup over recursively interned retained components.

use std::mem::size_of;

use crate::cancel::{Cancellation, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::hash::Sha256;
use crate::limits::{LimitKey, Limits};

use super::{scan_canonical, Category, ComponentId, NativeComponentArena, ScanBudget};

pub(crate) type StructuralDigest = [u8; 32];

const STRUCTURAL_DIGEST_DOMAIN_V1: &[u8] = b"pyowl-core:structural-value:v1\0";
const MODEL_SCHEMA_VARINT_V1: &[u8] = &[1];

type DigestFunction = fn(&[u8]) -> StructuralDigest;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct DigestEntry {
    digest: StructuralDigest,
    identifier: ComponentId,
}

#[derive(Clone, Debug)]
pub(crate) struct NativeComponentDigestIndex {
    // Retain the fallibly allocated Vec directly. Converting to a boxed slice
    // may perform an additional infallible shrink allocation on stable Rust.
    entries: Vec<DigestEntry>,
    category: Category,
    retained_bytes: u64,
    digest: DigestFunction,
}

impl NativeComponentDigestIndex {
    pub(crate) fn build(
        arena: &NativeComponentArena,
        identifiers: &[ComponentId],
        category: Category,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<Self> {
        Self::build_with_digest(
            arena,
            identifiers,
            category,
            limits,
            cancellation,
            interrupt,
            structural_digest_v1,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn build_with_digest(
        arena: &NativeComponentArena,
        identifiers: &[ComponentId],
        category: Category,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        digest: DigestFunction,
    ) -> NativeResult<Self> {
        let count = u64::try_from(identifiers.len())
            .map_err(|_| NativeError::limit("native component index row count exceeds u64"))?;
        if count > limits.value(LimitKey::MaxIndexRows) {
            return Err(NativeError::limit(
                "native component digest index exceeds max_index_rows",
            ));
        }
        check_retained_bytes(arena, minimum_index_bytes(identifiers.len())?, limits)?;
        let mut entries = Vec::new();
        entries
            .try_reserve_exact(identifiers.len())
            .map_err(|_| NativeError::limit("native component digest index allocation failed"))?;
        let retained_bytes = minimum_index_bytes(entries.capacity())?;
        check_retained_bytes(arena, retained_bytes, limits)?;
        let external_bytes = usize::try_from(retained_bytes)
            .map_err(|_| NativeError::limit("native component index size exceeds usize"))?;
        for identifier in identifiers {
            if arena.category(*identifier)? != category {
                return Err(NativeError::protocol(
                    "native component digest index category is inconsistent",
                ));
            }
            let canonical = arena.encode(
                *identifier,
                limits,
                cancellation.clone(),
                interrupt.clone(),
                external_bytes,
            )?;
            entries.push(DigestEntry {
                digest: digest(&canonical),
                identifier: *identifier,
            });
        }
        entries.sort_unstable_by_key(|entry| entry.digest);
        reject_duplicate_identifiers(&entries)?;
        Ok(Self {
            entries,
            category,
            retained_bytes,
            digest,
        })
    }

    pub(crate) const fn category(&self) -> Category {
        self.category
    }

    pub(crate) const fn retained_bytes(&self) -> u64 {
        self.retained_bytes
    }

    pub(crate) fn len(&self) -> usize {
        self.entries.len()
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub(crate) fn matching_ids(
        &self,
        digest: StructuralDigest,
    ) -> impl Iterator<Item = ComponentId> + '_ {
        let range = self.digest_range(&digest);
        self.entries[range].iter().map(|entry| entry.identifier)
    }

    pub(crate) fn contains_canonical(
        &self,
        arena: &NativeComponentArena,
        canonical: &[u8],
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<bool> {
        let mut budget = ScanBudget::from_limits(limits);
        if scan_canonical(canonical, &mut budget)? != self.category {
            return Ok(false);
        }
        let digest = (self.digest)(canonical);
        let external_bytes = usize::try_from(self.retained_bytes)
            .map_err(|_| NativeError::limit("native component index size exceeds usize"))?;
        for entry in &self.entries[self.digest_range(&digest)] {
            let retained = arena.encode(
                entry.identifier,
                limits,
                cancellation.clone(),
                interrupt.clone(),
                external_bytes,
            )?;
            if retained == canonical {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn digest_range(&self, digest: &StructuralDigest) -> std::ops::Range<usize> {
        let start = self.entries.partition_point(|entry| entry.digest < *digest);
        let end = self
            .entries
            .partition_point(|entry| entry.digest <= *digest);
        start..end
    }
}

fn minimum_index_bytes(count: usize) -> NativeResult<u64> {
    count
        .checked_mul(size_of::<DigestEntry>())
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| NativeError::limit("native component digest index size overflow"))
}

fn check_retained_bytes(
    arena: &NativeComponentArena,
    retained_bytes: u64,
    limits: &Limits,
) -> NativeResult<()> {
    if retained_bytes > limits.value(LimitKey::MaxIndexBytes) {
        return Err(NativeError::limit(
            "native component digest index exceeds max_index_bytes",
        ));
    }
    let total_retained = arena
        .counters()
        .retained_bytes
        .checked_add(retained_bytes)
        .ok_or_else(|| NativeError::limit("native component index memory overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| total_retained > maximum)
    {
        return Err(NativeError::limit(
            "native component digest index exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

fn reject_duplicate_identifiers(entries: &[DigestEntry]) -> NativeResult<()> {
    let mut start = 0;
    while start < entries.len() {
        let digest = entries[start].digest;
        let end = entries[start..]
            .partition_point(|entry| entry.digest == digest)
            .checked_add(start)
            .ok_or_else(|| NativeError::limit("native digest bucket range overflow"))?;
        for left in start..end {
            for right in left + 1..end {
                if entries[left].identifier == entries[right].identifier {
                    return Err(NativeError::protocol(
                        "native component digest index contains a duplicate identifier",
                    ));
                }
            }
        }
        start = end;
    }
    Ok(())
}

pub(crate) fn structural_digest_v1(canonical: &[u8]) -> StructuralDigest {
    let mut hasher = Sha256::new();
    hasher.update(STRUCTURAL_DIGEST_DOMAIN_V1);
    hasher.update(MODEL_SCHEMA_VARINT_V1);
    hasher.update(canonical);
    hasher.finish()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::NativeComponentBuilder;

    fn declaration(value: &str) -> Vec<u8> {
        let iri = node(1, &[(2, value.as_bytes())]);
        let entity = node(2, &[(5, b"class"), (1, &iri)]);
        let mut result = node(60, &[(1, &entity)]);
        result.extend_from_slice(&[6, 0]);
        result
    }

    fn node(tag: u8, fields: &[(u8, &[u8])]) -> Vec<u8> {
        let mut result = vec![tag];
        for (marker, value) in fields {
            result.push(*marker);
            result.extend(varint(value.len()));
            result.extend_from_slice(value);
        }
        result
    }

    fn varint(mut value: usize) -> Vec<u8> {
        let mut result = Vec::new();
        loop {
            let byte = (value & 0x7f) as u8;
            value >>= 7;
            result.push(if value == 0 { byte } else { byte | 0x80 });
            if value == 0 {
                return result;
            }
        }
    }

    fn constant_digest(_: &[u8]) -> StructuralDigest {
        [7; 32]
    }

    #[test]
    fn digest_vector_matches_the_python_structural_domain() {
        assert_eq!(
            structural_digest_v1(&declaration("urn:index:a")),
            [
                0x03, 0x4c, 0xb0, 0x5c, 0xf1, 0x93, 0x0c, 0x99, 0x64, 0x54, 0x0a, 0x8d, 0xcd, 0x68,
                0xd4, 0x43, 0xb0, 0x0c, 0x67, 0xd3, 0x93, 0x3b, 0xd4, 0x01, 0x78, 0x94, 0xe0, 0x87,
                0xae, 0x49, 0x64, 0xe8,
            ]
        );
    }

    #[test]
    fn lookup_checks_full_canonical_bytes_inside_digest_collisions() {
        let limits = Limits::default();
        let first = declaration("urn:index:a");
        let second = declaration("urn:index:b");
        let absent = declaration("urn:index:absent");
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let first_pending = builder.intern_canonical(&first).expect("first");
        let second_pending = builder.intern_canonical(&second).expect("second");
        let frozen = builder.freeze().expect("arena");
        let first_id = frozen.resolve(first_pending).expect("first id");
        let second_id = frozen.resolve(second_pending).expect("second id");
        let arena = frozen.into_arena();
        let index = NativeComponentDigestIndex::build_with_digest(
            &arena,
            &[first_id, second_id],
            Category::Axiom,
            &limits,
            Cancellation::with_duration(None),
            None,
            constant_digest,
        )
        .expect("digest index");

        assert_eq!(index.len(), 2);
        assert_eq!(index.category(), Category::Axiom);
        assert!(index.retained_bytes() >= 2 * size_of::<DigestEntry>() as u64);
        assert_eq!(index.matching_ids([7; 32]).count(), 2);
        assert!(index
            .contains_canonical(
                &arena,
                &first,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("first membership"));
        assert!(!index
            .contains_canonical(
                &arena,
                &absent,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("absent membership"));
    }

    #[test]
    fn duplicate_foreign_and_wrong_category_roots_fail_closed() {
        let limits = Limits::default();
        let canonical = declaration("urn:index:duplicate");
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let pending = builder.intern_canonical(&canonical).expect("declaration");
        let frozen = builder.freeze().expect("arena");
        let identifier = frozen.resolve(pending).expect("identifier");
        let arena = frozen.into_arena();

        let mut foreign_builder = NativeComponentBuilder::new(&limits).expect("foreign builder");
        let foreign_pending = foreign_builder
            .intern_canonical(&canonical)
            .expect("foreign declaration");
        let foreign_frozen = foreign_builder.freeze().expect("foreign arena");
        let foreign_identifier = foreign_frozen
            .resolve(foreign_pending)
            .expect("foreign identifier");

        assert_eq!(
            NativeComponentDigestIndex::build(
                &arena,
                &[identifier, identifier],
                Category::Axiom,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_PROTOCOL"
        );
        assert_eq!(
            NativeComponentDigestIndex::build(
                &arena,
                &[identifier],
                Category::Entity,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_PROTOCOL"
        );
        assert_eq!(
            NativeComponentDigestIndex::build(
                &arena,
                &[foreign_identifier],
                Category::Axiom,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_PROTOCOL"
        );
    }

    #[test]
    fn retained_index_memory_is_rejected_before_publication() {
        let build_limits = Limits::default();
        let canonical = declaration("urn:index:bounded");
        let mut builder = NativeComponentBuilder::new(&build_limits).expect("builder");
        let pending = builder.intern_canonical(&canonical).expect("declaration");
        let frozen = builder.freeze().expect("arena");
        let identifier = frozen.resolve(pending).expect("identifier");
        let arena = frozen.into_arena();
        let mut operation_limits = build_limits;
        operation_limits.max_memory_bytes = Some(
            arena
                .counters()
                .retained_bytes
                .checked_add(minimum_index_bytes(1).expect("index bytes"))
                .expect("total retained bytes")
                - 1,
        );

        assert_eq!(
            NativeComponentDigestIndex::build(
                &arena,
                &[identifier],
                Category::Axiom,
                &operation_limits,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT"
        );
    }
}
