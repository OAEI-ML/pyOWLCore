//! Process-local atomic cancellation/deadline state.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
#[cfg(all(not(fuzzing), feature = "extension-module"))]
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[cfg(all(not(fuzzing), feature = "extension-module"))]
use pyo3::prelude::*;

use crate::error::{NativeError, NativeResult};

#[cfg(all(not(fuzzing), feature = "extension-module"))]
pub(crate) type InterruptSlot = Arc<Mutex<Option<PyErr>>>;
#[cfg(any(fuzzing, not(feature = "extension-module")))]
pub(crate) type InterruptSlot = Arc<()>;

#[cfg(all(not(fuzzing), feature = "extension-module"))]
pub(crate) fn interrupt_slot() -> InterruptSlot {
    Arc::new(Mutex::new(None))
}

#[cfg(all(not(fuzzing), feature = "extension-module"))]
pub(crate) fn take_interrupt(slot: &InterruptSlot) -> NativeResult<Option<PyErr>> {
    slot.lock()
        .map_err(|_| NativeError::panic())
        .map(|mut retained| retained.take())
}

#[derive(Debug)]
struct SharedCancel {
    cancelled: AtomicBool,
    started: Instant,
    duration: Option<Duration>,
    deadline: Option<Instant>,
}

#[cfg_attr(
    all(not(fuzzing), feature = "extension-module"),
    pyclass(
        module = "pyowl_core._native",
        frozen,
        name = "_Cancellation",
        skip_from_py_object
    )
)]
#[derive(Clone, Debug)]
pub(crate) struct Cancellation {
    inner: Arc<SharedCancel>,
}

#[cfg(all(not(fuzzing), feature = "extension-module"))]
#[pymethods]
impl Cancellation {
    #[new]
    #[pyo3(signature = (deadline_seconds=None))]
    fn new(deadline_seconds: Option<f64>) -> PyResult<Self> {
        let duration = match deadline_seconds {
            None => None,
            Some(value) if value.is_finite() && value > 0.0 => {
                Some(Duration::try_from_secs_f64(value).map_err(|_| {
                    pyo3::exceptions::PyValueError::new_err(
                        "deadline_seconds must fit a native duration",
                    )
                })?)
            }
            Some(_) => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "deadline_seconds must be a positive finite number or None",
                ));
            }
        };
        if duration.is_some_and(|value| Instant::now().checked_add(value).is_none()) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "deadline_seconds must fit the native monotonic clock",
            ));
        }
        Ok(Self::with_duration(duration))
    }

    #[pyo3(name = "cancel")]
    fn cancel_from_python(&self) -> bool {
        self.cancel()
    }

    #[getter]
    fn cancelled(&self) -> bool {
        self.is_cancelled()
    }
}

impl Cancellation {
    pub(crate) fn with_duration(duration: Option<Duration>) -> Self {
        let started = Instant::now();
        Self {
            inner: Arc::new(SharedCancel {
                cancelled: AtomicBool::new(false),
                started,
                duration,
                deadline: duration.and_then(|value| started.checked_add(value)),
            }),
        }
    }

    fn cancel(&self) -> bool {
        !self.inner.cancelled.swap(true, Ordering::AcqRel)
    }

    fn is_cancelled(&self) -> bool {
        self.inner.cancelled.load(Ordering::Acquire)
            || self
                .inner
                .deadline
                .is_some_and(|deadline| Instant::now() >= deadline)
    }

    pub(crate) fn checkpoint(&self) -> NativeResult<()> {
        if self.inner.cancelled.load(Ordering::Acquire) {
            return Err(NativeError::cancelled());
        }
        if self
            .inner
            .deadline
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            let allowed = self
                .inner
                .duration
                .ok_or_else(|| NativeError::protocol("native deadline duration disappeared"))?;
            return Err(NativeError::deadline_limit(
                self.inner.started.elapsed().as_secs_f64(),
                allowed.as_secs_f64(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug)]
pub(crate) struct Guard {
    cancellation: Cancellation,
    started: Instant,
    deadline: Option<Duration>,
    interrupt: Option<InterruptSlot>,
    stride: u64,
    next: u64,
}

impl Guard {
    pub(crate) fn new(cancellation: Cancellation, deadline: Option<Duration>, stride: u32) -> Self {
        Self {
            cancellation,
            started: Instant::now(),
            deadline,
            interrupt: None,
            stride: u64::from(stride),
            next: u64::from(stride),
        }
    }

    pub(crate) fn with_interrupt(
        cancellation: Cancellation,
        deadline: Option<Duration>,
        stride: u32,
        interrupt: InterruptSlot,
    ) -> Self {
        let mut guard = Self::new(cancellation, deadline, stride);
        guard.interrupt = Some(interrupt);
        guard
    }

    pub(crate) fn check(&mut self, work: u64, force: bool) -> NativeResult<()> {
        if !force && work < self.next {
            return Ok(());
        }
        if !force {
            self.next = work
                .checked_add(self.stride)
                .ok_or_else(|| NativeError::limit("native work counter overflow"))?;
        }
        self.cancellation.checkpoint()?;
        #[cfg(all(not(fuzzing), feature = "extension-module"))]
        if let Some(interrupt) = &self.interrupt {
            if let Err(error) = Python::attach(|py| py.check_signals()) {
                let mut retained = interrupt.lock().map_err(|_| NativeError::panic())?;
                if retained.is_none() {
                    *retained = Some(error);
                }
                return Err(NativeError::cancelled());
            }
        }
        if self
            .deadline
            .is_some_and(|deadline| self.started.elapsed() >= deadline)
        {
            let allowed = self
                .deadline
                .ok_or_else(|| NativeError::protocol("native guard deadline disappeared"))?;
            return Err(NativeError::deadline_limit(
                self.started.elapsed().as_secs_f64(),
                allowed.as_secs_f64(),
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cancellation_is_atomic_and_idempotent() {
        let state = Cancellation::with_duration(None);
        assert!(state.cancel());
        assert!(!state.cancel());
        assert_eq!(state.checkpoint().unwrap_err().code, "NATIVE_CANCELLED");
    }

    #[test]
    fn configured_deadline_is_reported() {
        let state = Cancellation::with_duration(Some(Duration::ZERO));
        assert_eq!(state.checkpoint().unwrap_err().code, "NATIVE_DEADLINE");
    }
}
