//! Explicit little-endian readers which check every range before access.

use crate::error::{NativeError, NativeResult};

#[derive(Clone, Debug)]
pub(crate) struct Reader<'a> {
    data: &'a [u8],
    offset: usize,
}

impl<'a> Reader<'a> {
    pub(crate) fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }

    pub(crate) fn remaining(&self) -> usize {
        self.data.len() - self.offset
    }

    pub(crate) fn take(&mut self, size: usize) -> NativeResult<&'a [u8]> {
        let end = self
            .offset
            .checked_add(size)
            .ok_or_else(|| NativeError::corrupt("wire row offset overflow"))?;
        let result = self
            .data
            .get(self.offset..end)
            .ok_or_else(|| NativeError::corrupt("truncated wire row"))?;
        self.offset = end;
        Ok(result)
    }

    pub(crate) fn u8(&mut self) -> NativeResult<u8> {
        Ok(self.take(1)?[0])
    }

    pub(crate) fn boolean(&mut self) -> NativeResult<bool> {
        match self.u8()? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(NativeError::corrupt("invalid wire boolean")),
        }
    }

    pub(crate) fn u16(&mut self) -> NativeResult<u16> {
        let value = self.take(2)?;
        Ok(u16::from_le_bytes([value[0], value[1]]))
    }

    pub(crate) fn u32(&mut self) -> NativeResult<u32> {
        let value = self.take(4)?;
        Ok(u32::from_le_bytes([value[0], value[1], value[2], value[3]]))
    }

    pub(crate) fn u64(&mut self) -> NativeResult<u64> {
        let value = self.take(8)?;
        Ok(u64::from_le_bytes([
            value[0], value[1], value[2], value[3], value[4], value[5], value[6], value[7],
        ]))
    }

    pub(crate) fn finish(self) -> NativeResult<()> {
        if self.remaining() == 0 {
            Ok(())
        } else {
            Err(NativeError::corrupt("wire row has trailing bytes"))
        }
    }
}

pub(crate) fn u16_at(data: &[u8], offset: usize) -> NativeResult<u16> {
    let mut reader = Reader::new(
        data.get(offset..)
            .ok_or_else(|| NativeError::corrupt("wire scalar offset exceeds bounds"))?,
    );
    reader.u16()
}

pub(crate) fn u32_at(data: &[u8], offset: usize) -> NativeResult<u32> {
    let mut reader = Reader::new(
        data.get(offset..)
            .ok_or_else(|| NativeError::corrupt("wire scalar offset exceeds bounds"))?,
    );
    reader.u32()
}

pub(crate) fn u64_at(data: &[u8], offset: usize) -> NativeResult<u64> {
    let mut reader = Reader::new(
        data.get(offset..)
            .ok_or_else(|| NativeError::corrupt("wire scalar offset exceeds bounds"))?,
    );
    reader.u64()
}
