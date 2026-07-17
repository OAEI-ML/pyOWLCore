//! Bounded native work session shared by parser and index operations.

use crate::cancel::Guard;
use crate::error::{NativeError, NativeResult};
use crate::limits::{Limits, MemoryBudget};

pub(crate) struct Session<'a> {
    guard: &'a mut Guard,
    limits: &'a Limits,
    memory: MemoryBudget,
    work: u64,
}

impl<'a> Session<'a> {
    pub(crate) fn new(
        guard: &'a mut Guard,
        limits: &'a Limits,
        input_bytes: usize,
    ) -> NativeResult<Self> {
        Ok(Self {
            guard,
            limits,
            memory: MemoryBudget::new(limits, input_bytes)?,
            work: 0,
        })
    }

    pub(crate) fn limits(&self) -> &Limits {
        self.limits
    }

    pub(crate) fn step(&mut self, amount: u64) -> NativeResult<()> {
        self.work = self
            .work
            .checked_add(amount)
            .ok_or_else(|| NativeError::limit("native session work counter overflow"))?;
        if self.work > self.limits.max_canonical_work {
            return Err(NativeError::limit(
                "native operation exceeds max_canonical_work",
            ));
        }
        self.guard.check(self.work, false)
    }

    pub(crate) fn reserve_bytes(&mut self, bytes: usize) -> NativeResult<()> {
        self.memory.reserve::<u8>(bytes)
    }

    pub(crate) fn finish(&mut self) -> NativeResult<()> {
        self.guard.check(self.work, true)
    }
}
