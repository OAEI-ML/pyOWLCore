//! Test-only entry points for exercising production fallible allocations with
//! an executable-owned global allocator.

use crate::cancel::Cancellation;
use crate::error::NativeError;
use crate::limits::Limits;
use crate::model::{
    prepare_encoded_structural_columns_from_tables_v1, ComponentId, EncodedRootKindV1,
    EncodedRootTableV1, FrozenComponentBuild, NativeComponentBuilder,
    PreparedEncodedStructuralColumnsV1,
};
use crate::wire::Validation;

/// A stable, allocation-free view of a native failure for the external
/// allocator harness.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Failure {
    pub code: &'static str,
    pub message: &'static str,
}

impl From<NativeError> for Failure {
    fn from(error: NativeError) -> Self {
        Self {
            code: error.code,
            message: error.message,
        }
    }
}

/// A frozen component fixture whose encode operation uses the production
/// allocation path.
#[derive(Debug)]
pub struct ComponentEncodingFixture {
    frozen: FrozenComponentBuild,
    identifiers: [ComponentId; 1],
    cancellation: Cancellation,
}

impl ComponentEncodingFixture {
    /// Build and freeze the fixture before process-allocation injection is
    /// armed.
    pub fn new(canonical: &[u8]) -> Result<Self, Failure> {
        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::with_control(
            &limits,
            Cancellation::with_duration(None),
            None,
            canonical.len(),
        )?;
        let pending = builder.intern_canonical(canonical)?;
        let frozen = builder.freeze()?;
        let identifier = frozen.resolve(pending)?;
        Ok(Self {
            frozen,
            identifiers: [identifier],
            cancellation: Cancellation::with_duration(None),
        })
    }

    /// Encode through the same fallibly reserved output buffer used by the
    /// native component boundary.
    pub fn encode(&mut self) -> Result<Vec<u8>, Failure> {
        self.frozen
            .encode(self.identifiers[0])
            .map_err(Failure::from)
    }

    /// Prepare retained encoded-column metadata before allocation injection.
    ///
    /// The returned one-shot fixture isolates the production publication
    /// allocation from its already validated discovery/sort workspace.
    pub fn prepare_encoded_columns(&self) -> Result<EncodedColumnPublicationFixture<'_>, Failure> {
        let tables = [EncodedRootTableV1::new(
            EncodedRootKindV1::Axiom,
            &self.identifiers,
        )];
        let prepared = prepare_encoded_structural_columns_from_tables_v1(
            self.frozen.arena(),
            &tables,
            &Limits::default(),
            self.cancellation.clone(),
            None,
            0,
        )?;
        Ok(EncodedColumnPublicationFixture { prepared })
    }
}

/// One prepared encoded-column result whose eleven production buffers have
/// not yet been allocated.
pub struct EncodedColumnPublicationFixture<'arena> {
    prepared: PreparedEncodedStructuralColumnsV1<'arena>,
}

impl EncodedColumnPublicationFixture<'_> {
    /// Allocate and fill the exact production buffers, returning their stable
    /// lengths without introducing another heap allocation.
    pub fn publish(self) -> Result<[usize; 11], Failure> {
        let columns = self.prepared.into_columns()?;
        Ok(columns
            .buffers()
            .named()
            .map(|(_name, buffer)| buffer.len()))
    }
}

/// An owned component-build fixture whose infallible cancellation control
/// block is prepared before process-allocation injection is armed.
#[derive(Debug)]
pub struct ComponentBuildFixture {
    canonical: Vec<u8>,
    cancellation: Cancellation,
    limits: Limits,
}

impl ComponentBuildFixture {
    /// Own the canonical input and cancellation state before injection starts.
    pub fn new(canonical: &[u8]) -> Result<Self, Failure> {
        let mut owned = Vec::new();
        owned
            .try_reserve_exact(canonical.len())
            .map_err(|_| Failure {
                code: "NATIVE_WIRE_LIMIT",
                message: "native allocator test-fixture allocation failed",
            })?;
        owned.extend_from_slice(canonical);
        Ok(Self {
            canonical: owned,
            cancellation: Cancellation::with_duration(None),
            limits: Limits::default(),
        })
    }

    /// Decode and intern through the production builder. The builder is
    /// dropped before this method returns, so no partial state can escape.
    pub fn build(&self) -> Result<(), Failure> {
        let mut builder = NativeComponentBuilder::with_control(
            &self.limits,
            self.cancellation.clone(),
            None,
            self.canonical.len(),
        )?;
        builder.intern_canonical(&self.canonical)?;
        Ok(())
    }
}

/// Publish the frozen native wire-validation receipt through its production
/// fallibly reserved output buffer.
pub fn wire_validation_receipt() -> Result<Vec<u8>, Failure> {
    Validation {
        minor: 0,
        feature_flags: 0,
        total_length: 123,
        file_digest: [7; 32],
        section_count: 14,
        total_rows: 99,
    }
    .receipt()
    .map_err(Failure::from)
}
