//! Bounded native work session shared by parser and index operations.

use crate::cancel::Guard;
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits, MemoryBudget};

pub(crate) struct Session<'a> {
    guard: &'a mut Guard,
    limits: &'a Limits,
    memory: MemoryBudget,
    temporary_bytes: u64,
    work: u64,
    #[cfg(feature = "test-hooks")]
    allocation_probe: Option<SessionAllocationProbe>,
}

#[cfg(feature = "test-hooks")]
struct SessionAllocationProbe {
    fail_after: Option<u64>,
    allocations: u64,
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
            temporary_bytes: 0,
            work: 0,
            #[cfg(feature = "test-hooks")]
            allocation_probe: None,
        })
    }

    #[cfg(feature = "test-hooks")]
    pub(crate) fn with_allocation_failure(
        guard: &'a mut Guard,
        limits: &'a Limits,
        input_bytes: usize,
        fail_after: Option<u64>,
    ) -> NativeResult<Self> {
        let mut session = Self::new(guard, limits, input_bytes)?;
        session.allocation_probe = Some(SessionAllocationProbe {
            fail_after,
            allocations: 0,
        });
        Ok(session)
    }

    #[cfg(feature = "test-hooks")]
    pub(crate) fn allocation_count(&self) -> u64 {
        self.allocation_probe
            .as_ref()
            .map_or(0, |probe| probe.allocations)
    }

    fn allocation_checkpoint(&mut self, bytes: usize) -> NativeResult<()> {
        #[cfg(feature = "test-hooks")]
        if bytes > 0 {
            let Some(probe) = self.allocation_probe.as_mut() else {
                return Ok(());
            };
            if probe
                .fail_after
                .is_some_and(|maximum| probe.allocations >= maximum)
            {
                return Err(NativeError::limit(
                    "injected native parser allocation failure",
                ));
            }
            probe.allocations = probe
                .allocations
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native parser allocation counter overflow"))?;
        }
        #[cfg(not(feature = "test-hooks"))]
        let _ = bytes;
        Ok(())
    }

    pub(crate) fn limits(&self) -> &Limits {
        self.limits
    }

    /// Return the monotonic operation-wide byte charge at this instant.
    ///
    /// Deltas between two checkpoints describe accounted allocation work;
    /// they are not allocator-live or process-RSS peaks.
    pub(crate) fn accounted_bytes(&self) -> u64 {
        self.memory.used()
    }

    pub(crate) fn step(&mut self, amount: u64) -> NativeResult<()> {
        self.work = self
            .work
            .checked_add(amount)
            .ok_or_else(|| NativeError::limit("native session work counter overflow"))?;
        if self.work > self.limits.max_canonical_work {
            return Err(self.limits.resource_limit(
                LimitKey::MaxCanonicalWork,
                self.work,
                "native operation exceeds max_canonical_work",
            ));
        }
        self.guard.check(self.work, false)
    }

    pub(crate) fn reserve_bytes(&mut self, bytes: usize) -> NativeResult<()> {
        self.allocation_checkpoint(bytes)?;
        self.memory.reserve::<u8>(bytes)
    }

    /// Account a temporary allocation against both the operation-wide live
    /// memory budget and the dedicated workspace ceiling. The counter is
    /// deliberately monotonic: parser phases may transfer buffers to retained
    /// owners, and conservative accounting must never rely on allocator
    /// capacity reuse to remain within the configured limit.
    pub(crate) fn reserve_temporary_bytes(&mut self, bytes: usize) -> NativeResult<()> {
        let bytes = u64::try_from(bytes)
            .map_err(|_| NativeError::limit("native temporary allocation exceeds u64"))?;
        let following = self
            .temporary_bytes
            .checked_add(bytes)
            .ok_or_else(|| NativeError::limit("native temporary memory accounting overflow"))?;
        if following > self.limits.value(LimitKey::MaxTemporaryBytes) {
            return Err(self.limits.resource_limit(
                LimitKey::MaxTemporaryBytes,
                following,
                "native operation exceeds max_temporary_bytes",
            ));
        }
        let bytes = usize::try_from(bytes)
            .map_err(|_| NativeError::limit("native temporary allocation exceeds usize"))?;
        self.allocation_checkpoint(bytes)?;
        self.memory.reserve::<u8>(bytes)?;
        self.temporary_bytes = following;
        Ok(())
    }

    pub(crate) fn finish(&mut self) -> NativeResult<()> {
        self.guard.check(self.work, true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::Cancellation;

    #[test]
    fn accounted_bytes_are_monotonic_across_shared_reservations() {
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session = Session::new(&mut guard, &limits, 3).expect("session");

        assert_eq!(session.accounted_bytes(), 3);
        session.reserve_bytes(7).expect("persistent reservation");
        assert_eq!(session.accounted_bytes(), 10);
        session
            .reserve_temporary_bytes(5)
            .expect("temporary reservation");
        assert_eq!(session.accounted_bytes(), 15);
    }

    #[test]
    fn temporary_limit_failure_does_not_mutate_the_session_budget() {
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let (error, before_failure) = loop {
            let before = session.temporary_bytes;
            match session.reserve_temporary_bytes(usize::MAX) {
                Ok(()) => {}
                Err(error) => break (error, before),
            }
        };
        assert_eq!(error.code, "NATIVE_WIRE_LIMIT");
        assert_eq!(session.temporary_bytes, before_failure);
        session
            .reserve_temporary_bytes(1)
            .expect("budget remains usable");
        assert_eq!(session.temporary_bytes, before_failure + 1);
    }

    #[test]
    fn temporary_reservations_share_the_live_memory_budget_atomically() {
        let mut limits = Limits::default();
        limits.max_memory_bytes = Some(1);
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let error = session
            .reserve_temporary_bytes(2)
            .expect_err("live memory limit");
        assert_eq!(error.code, "NATIVE_WIRE_LIMIT");
        assert!(error.message.contains("max_memory_bytes"));
        assert_eq!(session.temporary_bytes, 0);
        session
            .reserve_temporary_bytes(1)
            .expect("failed reservation did not mutate either budget");
        assert_eq!(session.temporary_bytes, 1);
    }
}
