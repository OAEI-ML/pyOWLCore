//! Checked axiom-type partition construction over coarse canonical rows.

use std::collections::BTreeMap;

use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{scan_canonical, Category, ScanBudget};
use crate::session::Session;

const SOURCE_MAGIC: &[u8; 8] = b"PYNIDXS1";
const REQUEST_MAGIC: &[u8; 8] = b"PYNIDXQ1";
const RESULT_MAGIC: &[u8; 8] = b"PYNIDXR1";
const SCHEMA: u16 = 1;
const HEADER_BYTES: usize = 20;

pub(crate) fn decode_limits(request: &[u8]) -> NativeResult<Limits> {
    if request.len() != 8 + crate::limits::CONFIG_BYTES || request.get(..8) != Some(REQUEST_MAGIC) {
        return Err(NativeError::protocol(
            "invalid native index request framing",
        ));
    }
    Limits::decode(&request[8..])
}

pub(crate) fn build(data: &[u8], session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
    if data.len() < HEADER_BYTES || data.get(..8) != Some(SOURCE_MAGIC) {
        return Err(NativeError::protocol("invalid native index source framing"));
    }
    let schema = read_u16(data, 8)?;
    let reserved = read_u16(data, 10)?;
    let count = read_u64(data, 12)?;
    if schema != SCHEMA || reserved != 0 {
        return Err(NativeError::protocol("unsupported native index source"));
    }
    if count > session.limits().value(LimitKey::MaxIndexRows) {
        return Err(NativeError::limit("native index exceeds max_index_rows"));
    }
    if u64::try_from(data.len()).map_or(true, |value| {
        value > session.limits().value(LimitKey::MaxIndexBytes)
    }) {
        return Err(NativeError::limit(
            "native index source exceeds max_index_bytes",
        ));
    }
    let capacity = usize::try_from(count)
        .map_err(|_| NativeError::limit("native index row count exceeds address space"))?;
    session.reserve_bytes(capacity.saturating_mul(std::mem::size_of::<u64>()))?;
    let mut groups: BTreeMap<u64, Vec<u64>> = BTreeMap::new();
    let mut budget = ScanBudget::from_limits(session.limits());
    let mut offset = HEADER_BYTES;
    for ordinal in 0..count {
        session.step(1)?;
        let length = usize::try_from(read_u64(data, offset)?)
            .map_err(|_| NativeError::limit("native index row exceeds address space"))?;
        offset = offset
            .checked_add(8)
            .ok_or_else(|| NativeError::protocol("native index source offset overflow"))?;
        let end = offset
            .checked_add(length)
            .ok_or_else(|| NativeError::protocol("native index row length overflow"))?;
        let row = data
            .get(offset..end)
            .ok_or_else(|| NativeError::protocol("truncated native index row"))?;
        let category = scan_canonical(row, &mut budget)?;
        if category != Category::Axiom {
            return Err(NativeError::protocol(
                "native axiom-type index received a non-axiom row",
            ));
        }
        let tag = root_tag(row)?;
        let posting = groups.entry(tag).or_default();
        posting
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native index posting allocation failed"))?;
        posting.push(ordinal);
        offset = end;
    }
    if offset != data.len() {
        return Err(NativeError::protocol(
            "native index source contains trailing bytes",
        ));
    }
    encode(groups, session)
}

fn encode(groups: BTreeMap<u64, Vec<u64>>, session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
    let rows = groups.values().try_fold(0_usize, |total, values| {
        total
            .checked_add(values.len())
            .ok_or_else(|| NativeError::limit("native index result row count overflow"))
    })?;
    let size = HEADER_BYTES
        .checked_add(groups.len().saturating_mul(16))
        .and_then(|value| value.checked_add(rows.saturating_mul(8)))
        .ok_or_else(|| NativeError::limit("native index result size overflow"))?;
    if u64::try_from(size).map_or(true, |value| {
        value > session.limits().value(LimitKey::MaxIndexBytes)
    }) {
        return Err(NativeError::limit(
            "native index result exceeds max_index_bytes",
        ));
    }
    session.reserve_bytes(size)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native index result allocation failed"))?;
    output.extend_from_slice(RESULT_MAGIC);
    output.extend_from_slice(&SCHEMA.to_le_bytes());
    output.extend_from_slice(&0_u16.to_le_bytes());
    output.extend_from_slice(
        &u64::try_from(groups.len())
            .map_err(|_| NativeError::limit("native index group count exceeds u64"))?
            .to_le_bytes(),
    );
    for (tag, ordinals) in groups {
        output.extend_from_slice(&tag.to_le_bytes());
        output.extend_from_slice(
            &u64::try_from(ordinals.len())
                .map_err(|_| NativeError::limit("native index posting count exceeds u64"))?
                .to_le_bytes(),
        );
        for ordinal in ordinals {
            output.extend_from_slice(&ordinal.to_le_bytes());
        }
    }
    session.finish()?;
    Ok(output)
}

fn read_u16(data: &[u8], offset: usize) -> NativeResult<u16> {
    let value = data
        .get(offset..offset.saturating_add(2))
        .ok_or_else(|| NativeError::protocol("truncated native index source"))?;
    Ok(u16::from_le_bytes([value[0], value[1]]))
}

fn read_u64(data: &[u8], offset: usize) -> NativeResult<u64> {
    let value = data
        .get(offset..offset.saturating_add(8))
        .ok_or_else(|| NativeError::protocol("truncated native index source"))?;
    Ok(u64::from_le_bytes(value.try_into().map_err(|_| {
        NativeError::protocol("truncated native index source")
    })?))
}

fn root_tag(data: &[u8]) -> NativeResult<u64> {
    let mut value = 0_u64;
    for (index, byte) in data.iter().copied().take(10).enumerate() {
        let shift = u32::try_from(index.saturating_mul(7))
            .map_err(|_| NativeError::protocol("native index tag shift overflow"))?;
        if index == 9 && byte > 1 {
            return Err(NativeError::corrupt("canonical model tag exceeds u64"));
        }
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Ok(value);
        }
    }
    Err(NativeError::corrupt("truncated canonical model tag"))
}
