//! Checked resource configuration shared by native model and wire operations.

use std::time::Duration;

use crate::error::{NativeError, NativeResult};

pub(crate) const CONFIG_MAGIC: &[u8; 8] = b"PYNCONF\0";
pub(crate) const CONFIG_SCHEMA: u16 = 1;
pub(crate) const CONFIG_BYTES: usize = 312;
const LIMIT_COUNT: usize = 37;
const VERIFY_FILE_DIGEST: u16 = 1;

#[allow(dead_code)]
#[derive(Clone, Copy, Debug)]
#[repr(usize)]
pub(crate) enum LimitKey {
    MaxSourceBytes,
    MaxDocuments,
    MaxTotalSourceBytes,
    MaxAxioms,
    MaxTerms,
    MaxNestingDepth,
    MaxRdfListLength,
    MaxLiteralBytes,
    MaxIriBytes,
    MaxPrefixes,
    MaxImportDepth,
    MaxRedirects,
    MaxDiagnostics,
    MaxMemoryBytes,
    DeadlineNanoseconds,
    MaxTriples,
    MaxStrings,
    MaxAnnotations,
    MaxRuleAtoms,
    MaxSequenceArity,
    MaxCatalogRewrites,
    MaxResolverAttempts,
    MaxConcurrentFetches,
    MaxSourceMapEntries,
    MaxOriginEntries,
    MaxOverlayDepth,
    MaxDeltaEntries,
    MaxCompositeMembers,
    MaxIndexRows,
    MaxIndexBytes,
    MaxWireRows,
    MaxWireBytes,
    MaxTemporaryBytes,
    MaxDiskCacheBytes,
    MaxDecompressedBytes,
    MaxCanonicalWork,
    CancellationCheckInterval,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct Limits {
    values: [u64; LIMIT_COUNT],
    pub(crate) max_source_bytes: u64,
    pub(crate) max_total_source_bytes: u64,
    pub(crate) max_wire_bytes: u64,
    pub(crate) max_wire_rows: u64,
    pub(crate) max_memory_bytes: Option<u64>,
    pub(crate) max_terms: u64,
    pub(crate) max_literal_bytes: u64,
    pub(crate) max_iri_bytes: u64,
    pub(crate) max_strings: u64,
    pub(crate) max_rule_atoms: u64,
    pub(crate) max_sequence_arity: u64,
    pub(crate) max_documents: u64,
    pub(crate) max_axioms: u64,
    pub(crate) max_annotations: u64,
    pub(crate) max_source_map_entries: u64,
    pub(crate) max_origin_entries: u64,
    pub(crate) max_composite_members: u64,
    pub(crate) max_nesting_depth: u32,
    pub(crate) max_canonical_work: u64,
    pub(crate) cancellation_stride: u32,
    pub(crate) deadline: Option<Duration>,
    pub(crate) verify_file_digest: bool,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct MemoryBudget {
    maximum: Option<u64>,
    used: u64,
}

impl MemoryBudget {
    pub(crate) fn new(limits: &Limits, input: usize) -> NativeResult<Self> {
        let used = u64::try_from(input)
            .map_err(|_| NativeError::limit("native memory accounting exceeds u64"))?;
        if limits
            .max_memory_bytes
            .is_some_and(|maximum| used > maximum)
        {
            return Err(NativeError::limit("native input exceeds max_memory_bytes"));
        }
        Ok(Self {
            maximum: limits.max_memory_bytes,
            used,
        })
    }

    pub(crate) fn reserve<T>(&mut self, count: usize) -> NativeResult<()> {
        let bytes = count
            .checked_mul(std::mem::size_of::<T>())
            .ok_or_else(|| NativeError::limit("native allocation accounting overflow"))?;
        let bytes = u64::try_from(bytes)
            .map_err(|_| NativeError::limit("native allocation exceeds u64"))?;
        let next = self
            .used
            .checked_add(bytes)
            .ok_or_else(|| NativeError::limit("native memory accounting overflow"))?;
        if self.maximum.is_some_and(|maximum| next > maximum) {
            return Err(NativeError::limit(
                "native operation exceeds max_memory_bytes",
            ));
        }
        self.used = next;
        Ok(())
    }
}

impl Default for Limits {
    fn default() -> Self {
        Self::from_values(
            [
                2 * 1024 * 1024 * 1024,
                1_000,
                8 * 1024 * 1024 * 1024,
                100_000_000,
                500_000_000,
                512,
                10_000_000,
                64 * 1024 * 1024,
                1024 * 1024,
                1_000_000,
                128,
                5,
                10_000,
                0,
                0,
                100_000_000,
                500_000_000,
                100_000_000,
                10_000_000,
                10_000_000,
                128,
                10_000,
                8,
                100_000_000,
                100_000_000,
                32,
                10_000_000,
                1_024,
                500_000_000,
                16 * 1024 * 1024 * 1024,
                500_000_000,
                16 * 1024 * 1024 * 1024,
                16 * 1024 * 1024 * 1024,
                64 * 1024 * 1024 * 1024,
                8 * 1024 * 1024 * 1024,
                1_000_000_000,
                4096,
            ],
            VERIFY_FILE_DIGEST,
        )
    }
}

impl Limits {
    pub(crate) fn decode(data: &[u8]) -> NativeResult<Self> {
        if data.is_empty() {
            return Ok(Self::default());
        }
        if data.len() != CONFIG_BYTES || data.get(..8) != Some(CONFIG_MAGIC) {
            return Err(NativeError::protocol("invalid native limits framing"));
        }
        let schema = read_u16(data, 8)?;
        let flags = read_u16(data, 10)?;
        let reserved = read_u32(data, 12)?;
        if schema != CONFIG_SCHEMA || reserved != 0 || flags & !VERIFY_FILE_DIGEST != 0 {
            return Err(NativeError::protocol(
                "unsupported native limits configuration",
            ));
        }
        let mut values = [0_u64; LIMIT_COUNT];
        for (index, value) in values.iter_mut().enumerate() {
            *value = read_u64(data, 16 + index * 8)?;
            if !matches!(index, 13 | 14) && *value == 0 {
                return Err(NativeError::protocol("native limits must be positive"));
            }
        }
        Ok(Self::from_values(values, flags))
    }

    fn from_values(values: [u64; LIMIT_COUNT], flags: u16) -> Self {
        let value = |key: LimitKey| values[key as usize];
        let max_memory = value(LimitKey::MaxMemoryBytes);
        let deadline = value(LimitKey::DeadlineNanoseconds);
        Self {
            max_source_bytes: value(LimitKey::MaxSourceBytes),
            max_total_source_bytes: value(LimitKey::MaxTotalSourceBytes),
            max_wire_bytes: value(LimitKey::MaxWireBytes),
            max_wire_rows: value(LimitKey::MaxWireRows),
            max_memory_bytes: (max_memory != 0).then_some(max_memory),
            max_terms: value(LimitKey::MaxTerms),
            max_literal_bytes: value(LimitKey::MaxLiteralBytes),
            max_iri_bytes: value(LimitKey::MaxIriBytes),
            max_strings: value(LimitKey::MaxStrings),
            max_rule_atoms: value(LimitKey::MaxRuleAtoms),
            max_sequence_arity: value(LimitKey::MaxSequenceArity),
            max_documents: value(LimitKey::MaxDocuments),
            max_axioms: value(LimitKey::MaxAxioms),
            max_annotations: value(LimitKey::MaxAnnotations),
            max_source_map_entries: value(LimitKey::MaxSourceMapEntries),
            max_origin_entries: value(LimitKey::MaxOriginEntries),
            max_composite_members: value(LimitKey::MaxCompositeMembers),
            max_nesting_depth: value(LimitKey::MaxNestingDepth).min(u64::from(u32::MAX)) as u32,
            max_canonical_work: value(LimitKey::MaxCanonicalWork),
            cancellation_stride: value(LimitKey::CancellationCheckInterval).min(u64::from(u32::MAX))
                as u32,
            deadline: (deadline != 0).then_some(Duration::from_nanos(deadline)),
            verify_file_digest: flags & VERIFY_FILE_DIGEST != 0,
            values,
        }
    }

    #[allow(dead_code)]
    pub(crate) fn value(&self, key: LimitKey) -> u64 {
        self.values[key as usize]
    }

    pub(crate) fn check_source_size(&self, size: usize) -> NativeResult<()> {
        let size = u64::try_from(size)
            .map_err(|_| NativeError::limit("native input length exceeds u64"))?;
        if size > self.max_wire_bytes {
            return Err(NativeError::limit("native input exceeds max_wire_bytes"));
        }
        if self.max_memory_bytes.is_some_and(|maximum| size > maximum) {
            return Err(NativeError::limit("native input exceeds max_memory_bytes"));
        }
        Ok(())
    }

    pub(crate) fn check_output_size(&self, input: usize, output: usize) -> NativeResult<()> {
        let total = input
            .checked_add(output)
            .ok_or_else(|| NativeError::limit("native memory accounting overflow"))?;
        if self
            .max_memory_bytes
            .is_some_and(|maximum| u64::try_from(total).map_or(true, |value| value > maximum))
        {
            return Err(NativeError::limit(
                "native operation exceeds max_memory_bytes",
            ));
        }
        Ok(())
    }
}

fn read_u16(data: &[u8], offset: usize) -> NativeResult<u16> {
    let value = data
        .get(offset..offset + 2)
        .ok_or_else(|| NativeError::protocol("truncated native limits configuration"))?;
    Ok(u16::from_le_bytes([value[0], value[1]]))
}

fn read_u32(data: &[u8], offset: usize) -> NativeResult<u32> {
    let value = data
        .get(offset..offset + 4)
        .ok_or_else(|| NativeError::protocol("truncated native limits configuration"))?;
    Ok(u32::from_le_bytes([value[0], value[1], value[2], value[3]]))
}

fn read_u64(data: &[u8], offset: usize) -> NativeResult<u64> {
    let value = data
        .get(offset..offset + 8)
        .ok_or_else(|| NativeError::protocol("truncated native limits configuration"))?;
    Ok(u64::from_le_bytes([
        value[0], value[1], value[2], value[3], value[4], value[5], value[6], value[7],
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_finite_and_configuration_is_exact() {
        let defaults = Limits::decode(&[]).expect("default limits");
        assert!(defaults.max_wire_bytes > 0);
        assert_eq!(Limits::decode(b"bad").unwrap_err().code, "NATIVE_PROTOCOL");

        let mut encoded = vec![0_u8; CONFIG_BYTES];
        encoded[..8].copy_from_slice(CONFIG_MAGIC);
        encoded[8..10].copy_from_slice(&CONFIG_SCHEMA.to_le_bytes());
        encoded[10..12].copy_from_slice(&VERIFY_FILE_DIGEST.to_le_bytes());
        for index in 0..LIMIT_COUNT {
            let value = if matches!(index, 13 | 14) {
                0
            } else {
                u64::try_from(index + 1).expect("test limit index")
            };
            encoded[16 + index * 8..24 + index * 8].copy_from_slice(&value.to_le_bytes());
        }
        let decoded = Limits::decode(&encoded).expect("complete native limit ledger");
        assert_eq!(decoded.value(LimitKey::MaxSourceBytes), 1);
        assert_eq!(decoded.max_axioms, 4);
        assert_eq!(decoded.max_iri_bytes, 9);
        assert_eq!(decoded.max_wire_bytes, 32);
        assert_eq!(decoded.max_canonical_work, 36);
        assert_eq!(decoded.cancellation_stride, 37);
        assert!(decoded.verify_file_digest);
    }
}
