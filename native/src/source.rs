//! Checked framing for coarse native parser requests.

use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};

const MAGIC: &[u8; 8] = b"PYNFSS1\0";
pub(crate) const HEADER_BYTES: usize = 20;
const SCHEMA: u16 = 1;
const ALLOW_SWRL: u16 = 1;

#[derive(Clone, Copy, Debug)]
pub(crate) struct SourceRequest<'a> {
    pub(crate) source: &'a [u8],
    pub(crate) allow_swrl: bool,
}

impl<'a> SourceRequest<'a> {
    pub(crate) fn decode(data: &'a [u8], limits: &Limits) -> NativeResult<Self> {
        if data.len() < HEADER_BYTES || data.get(..8) != Some(MAGIC) {
            return Err(NativeError::protocol(
                "invalid native parser request framing",
            ));
        }
        let schema = u16::from_le_bytes([data[8], data[9]]);
        let flags = u16::from_le_bytes([data[10], data[11]]);
        let length = u64::from_le_bytes(
            data[12..20]
                .try_into()
                .map_err(|_| NativeError::protocol("truncated native parser request"))?,
        );
        if schema != SCHEMA || flags & !ALLOW_SWRL != 0 {
            return Err(NativeError::protocol("unsupported native parser request"));
        }
        let length = usize::try_from(length)
            .map_err(|_| NativeError::limit("native parser source exceeds address space"))?;
        if length != data.len() - HEADER_BYTES {
            return Err(NativeError::protocol(
                "native parser source length mismatch",
            ));
        }
        let observed = u64::try_from(length)
            .map_err(|_| NativeError::limit("native source length exceeds u64"))?;
        if observed > limits.value(LimitKey::MaxSourceBytes) {
            return Err(limits.resource_limit(
                LimitKey::MaxSourceBytes,
                observed,
                "native source exceeds max_source_bytes",
            ));
        }
        Ok(Self {
            source: &data[HEADER_BYTES..],
            allow_swrl: flags & ALLOW_SWRL != 0,
        })
    }
}
