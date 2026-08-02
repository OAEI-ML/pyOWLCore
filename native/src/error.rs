//! Stable, sanitized errors used on both sides of the PyO3 boundary.

use std::fmt::{Display, Formatter};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum NativeNumber {
    Integer(u64),
    FloatBits(u64),
}

impl NativeNumber {
    pub(crate) const fn integer(value: u64) -> Self {
        Self::Integer(value)
    }

    pub(crate) fn float(value: f64) -> Self {
        Self::FloatBits(value.to_bits())
    }
}

impl From<u64> for NativeNumber {
    fn from(value: u64) -> Self {
        Self::integer(value)
    }
}

impl From<f64> for NativeNumber {
    fn from(value: f64) -> Self {
        Self::float(value)
    }
}

/// Bounded canonicalization observations safe to expose to callers.
///
/// Blank labels and source excerpts deliberately have no representation here.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct NativeLimitDetails {
    pub(crate) component_count: Option<u64>,
    pub(crate) largest_component_labels: Option<u64>,
    pub(crate) largest_component_arcs: Option<u64>,
    pub(crate) refinement_rounds: Option<u64>,
    pub(crate) work_term: Option<&'static str>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct NativeResourceLimit {
    pub(crate) limit: &'static str,
    pub(crate) observed: NativeNumber,
    pub(crate) allowed: NativeNumber,
    pub(crate) details: NativeLimitDetails,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum NativeErrorPayload {
    None,
    ResourceLimit(NativeResourceLimit),
    /// Extensible bounded binary payload for other structured native failures.
    Opaque {
        kind: &'static str,
        data: Vec<u8>,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct NativeError {
    pub(crate) code: &'static str,
    pub(crate) message: &'static str,
    pub(crate) payload: NativeErrorPayload,
}

impl NativeError {
    pub(crate) const fn new(code: &'static str, message: &'static str) -> Self {
        Self {
            code,
            message,
            payload: NativeErrorPayload::None,
        }
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

    pub(crate) fn resource_limit(
        limit: &'static str,
        observed: impl Into<NativeNumber>,
        allowed: impl Into<NativeNumber>,
        message: &'static str,
    ) -> Self {
        Self::resource_limit_with_details(
            limit,
            observed,
            allowed,
            NativeLimitDetails::default(),
            message,
        )
    }

    pub(crate) fn resource_limit_with_details(
        limit: &'static str,
        observed: impl Into<NativeNumber>,
        allowed: impl Into<NativeNumber>,
        details: NativeLimitDetails,
        message: &'static str,
    ) -> Self {
        Self {
            code: "NATIVE_WIRE_LIMIT",
            message,
            payload: NativeErrorPayload::ResourceLimit(NativeResourceLimit {
                limit,
                observed: observed.into(),
                allowed: allowed.into(),
                details,
            }),
        }
    }

    /// Attach an already bounded, schema-tagged payload to a non-limit error.
    ///
    /// The producer owns bounding before construction; the Python boundary also
    /// rejects payloads larger than its fixed safety ceiling.
    pub(crate) fn with_opaque_payload(mut self, kind: &'static str, data: Vec<u8>) -> Self {
        self.payload = NativeErrorPayload::Opaque { kind, data };
        self
    }

    pub(crate) const fn cancelled() -> Self {
        Self::new("NATIVE_CANCELLED", "native operation cancelled")
    }

    pub(crate) fn deadline_limit(observed_seconds: f64, allowed_seconds: f64) -> Self {
        let mut error = Self::resource_limit(
            "deadline_seconds",
            observed_seconds,
            allowed_seconds,
            "native operation deadline exceeded",
        );
        error.code = "NATIVE_DEADLINE";
        error
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
