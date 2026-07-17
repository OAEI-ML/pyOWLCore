//! Stable, sanitized errors used on both sides of the PyO3 boundary.

use std::fmt::{Display, Formatter};

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct NativeError {
    pub(crate) code: &'static str,
    pub(crate) message: &'static str,
}

impl NativeError {
    pub(crate) const fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    pub(crate) const fn protocol(message: &'static str) -> Self {
        Self::new("NATIVE_PROTOCOL", message)
    }

    pub(crate) const fn corrupt(message: &'static str) -> Self {
        Self::new("NATIVE_WIRE_CORRUPTION", message)
    }

    pub(crate) const fn version(message: &'static str) -> Self {
        Self::new("NATIVE_WIRE_VERSION", message)
    }

    pub(crate) const fn limit(message: &'static str) -> Self {
        Self::new("NATIVE_WIRE_LIMIT", message)
    }

    pub(crate) const fn cancelled() -> Self {
        Self::new("NATIVE_CANCELLED", "native operation cancelled")
    }

    pub(crate) const fn deadline() -> Self {
        Self::new("NATIVE_DEADLINE", "native operation deadline exceeded")
    }

    pub(crate) const fn panic() -> Self {
        Self::new("NATIVE_PANIC", "native backend panic was contained")
    }
}

impl Display for NativeError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.message)
    }
}

impl std::error::Error for NativeError {}

pub(crate) type NativeResult<T> = Result<T, NativeError>;
