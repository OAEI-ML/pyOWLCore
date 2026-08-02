//! Test-only entry points for exercising production fallible allocations with
//! an executable-owned global allocator.

use crate::cancel::{Cancellation, Guard};
use crate::error::NativeError;
use crate::hash::crc32c;
use crate::index::{
    build_retained_axiom_type_index_v1, build_retained_signature_index_v1, RetainedAxiomTypeIndexV1,
};
use crate::limits::Limits;
use crate::model::{
    prepare_encoded_structural_columns_from_tables_v2, Category, ComponentFieldRef, ComponentId,
    EncodedRootKindV2, EncodedRootTableV2, FrozenComponentBuild, NativeComponentArena,
    NativeComponentBuilder, NativeComponentDigestIndex, PreparedEncodedStructuralColumnsV2,
};
use crate::publication::{
    TypedFacadeBuilderV2, TypedFacadeCollectionV2, TypedFacadeCoordinateV2,
    TypedFacadePageRequestV2, TypedFacadeScopeV2, TypedFacadeStorageV2, TypedFacadeTableV2,
};
use crate::wire::{Validation, MODEL_SCHEMA};

const WIRE_HEADER_BYTES: usize = 96;
const WIRE_DIRECTORY_BYTES: usize = 72;
const WIRE_SECTION_COUNT: usize = 14;
const WIRE_FIXTURE_BYTES: usize = WIRE_HEADER_BYTES + WIRE_DIRECTORY_BYTES * WIRE_SECTION_COUNT;

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
    entities: [ComponentId; 1],
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
        let entity = match frozen.arena().record(identifier)?.field(0)? {
            ComponentFieldRef::Node(entity)
                if frozen.arena().category(entity)? == Category::Entity =>
            {
                entity
            }
            _ => {
                return Err(Failure {
                    code: "NATIVE_PROTOCOL",
                    message: "native allocator declaration fixture lost its entity",
                });
            }
        };
        Ok(Self {
            frozen,
            entities: [entity],
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

    /// Build and consume the production digest index while allocation
    /// injection is armed.
    ///
    /// The primitive summary escapes without another allocation; the retained
    /// index is dropped transactionally before this method returns.
    pub fn build_digest_index(&self) -> Result<(usize, u64), Failure> {
        let index = NativeComponentDigestIndex::build(
            self.frozen.arena(),
            &self.identifiers,
            Category::Axiom,
            &Limits::default(),
            self.cancellation.clone(),
            None,
        )?;
        Ok((index.len(), index.retained_bytes()))
    }

    /// Build and consume the production retained-signature index while
    /// allocation injection is armed.
    ///
    /// Its arena owner, count buffers, ordinal map, traversal stack, and
    /// entity set are all dropped before the allocation-free counter summary
    /// escapes.
    pub fn build_signature_index(&self) -> Result<[u64; 6], Failure> {
        let index = build_retained_signature_index_v1(
            self.frozen.arena(),
            &self.entities,
            &[],
            &self.identifiers,
            &[],
            &Limits::default(),
            self.cancellation.clone(),
            None,
            0,
        )?;
        let counters = index.counters();
        Ok([
            counters.structural_root_rows,
            counters.entity_rows,
            counters.referenced_links,
            counters.nonannotation_links,
            counters.declaration_links,
            counters.complete_root_encode_calls,
        ])
    }

    /// Build and consume the production retained axiom-type index while
    /// allocation injection is armed.
    ///
    /// All retained layout vectors are dropped before the allocation-free
    /// counter summary escapes.
    pub fn build_axiom_type_index(&self) -> Result<[u64; 5], Failure> {
        let index = build_retained_axiom_type_index_v1(
            self.frozen.arena(),
            &self.identifiers,
            &Limits::default(),
            self.cancellation.clone(),
            None,
            0,
        )?;
        let counters = index.counters();
        Ok([
            counters.axiom_rows,
            counters.constructor_groups,
            counters.category_groups,
            counters.retained_buffer_bytes,
            counters.complete_root_encode_calls,
        ])
    }

    /// Prepare a production retained axiom-type index before allocation
    /// injection is armed.
    pub fn prepare_axiom_type_page(&self) -> Result<AxiomTypePageFixture, Failure> {
        let tag = self.frozen.arena().tag(self.identifiers[0])?;
        let index = build_retained_axiom_type_index_v1(
            self.frozen.arena(),
            &self.identifiers,
            &Limits::default(),
            self.cancellation.clone(),
            None,
            0,
        )?;
        Ok(AxiomTypePageFixture {
            index,
            tag,
            cancellation: self.cancellation.clone(),
        })
    }

    /// Prepare a production typed V2 facade and initialize its infallible
    /// platform mutex control block before allocation injection is armed,
    /// retaining only identifiers into the shared component arena.
    pub fn prepare_typed_facade_reads(&self) -> Result<TypedFacadeReadFixture, Failure> {
        self.prepare_typed_facade_effective_reads(TypedFacadeCoordinateV2::document(
            TypedFacadeCollectionV2::Axioms,
            0,
        ))
    }

    /// Prepare a production typed V2 closure facade before allocation
    /// injection is armed, retaining only identifiers into the shared arena.
    pub fn prepare_typed_facade_closure_reads(&self) -> Result<TypedFacadeReadFixture, Failure> {
        self.prepare_typed_facade_effective_reads(TypedFacadeCoordinateV2::closure(
            TypedFacadeCollectionV2::Axioms,
        ))
    }

    fn prepare_typed_facade_effective_reads(
        &self,
        coordinate: TypedFacadeCoordinateV2,
    ) -> Result<TypedFacadeReadFixture, Failure> {
        let mut roots = Vec::new();
        roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator typed root allocation failed"))?;
        roots.push(self.identifiers[0]);
        let mut tables = Vec::new();
        tables
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator typed table allocation failed"))?;
        tables.push(TypedFacadeTableV2::new(coordinate, roots));
        let storage = TypedFacadeStorageV2::freeze(
            self.frozen.arena().clone(),
            tables,
            Vec::new(),
            1,
            Limits::default(),
            self.cancellation.clone(),
            None,
        )?;
        // The standard-library mutex may initialize platform-owned state on
        // its first lock. That infallible control allocation is deliberately
        // prepared before arming, as with the cancellation control block.
        storage.counters()?;
        Ok(TypedFacadeReadFixture {
            storage,
            coordinate,
            raw_document_owner: false,
            cancellation: self.cancellation.clone(),
        })
    }

    /// Prepare a production typed V2 owner with document/raw/closure axiom and
    /// complete signature root tables before allocation injection is armed.
    pub fn prepare_typed_facade_indexes(&self) -> Result<TypedFacadeIndexFixture, Failure> {
        let document_axiom_coordinate =
            TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let document_signature_coordinate =
            TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Signature, 0);
        let closure_axiom_coordinate =
            TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms);
        let closure_signature_coordinate =
            TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Signature);
        let mut document_axiom_roots = Vec::new();
        document_axiom_roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator typed axiom root failed"))?;
        document_axiom_roots.push(self.identifiers[0]);
        let mut raw_axiom_roots = Vec::new();
        raw_axiom_roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator raw typed axiom root failed"))?;
        raw_axiom_roots.push(self.identifiers[0]);
        let mut closure_axiom_roots = Vec::new();
        closure_axiom_roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator closure typed axiom root failed"))?;
        closure_axiom_roots.push(self.identifiers[0]);
        let mut document_signature_roots = Vec::new();
        document_signature_roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator typed signature root failed"))?;
        document_signature_roots.push(self.entities[0]);
        let mut closure_signature_roots = Vec::new();
        closure_signature_roots.try_reserve_exact(1).map_err(|_| {
            NativeError::limit("native allocator closure typed signature root failed")
        })?;
        closure_signature_roots.push(self.entities[0]);
        let mut tables = Vec::new();
        tables
            .try_reserve_exact(4)
            .map_err(|_| NativeError::limit("native allocator typed index tables failed"))?;
        tables.push(TypedFacadeTableV2::new(
            document_axiom_coordinate,
            document_axiom_roots,
        ));
        tables.push(TypedFacadeTableV2::new(
            document_signature_coordinate,
            document_signature_roots,
        ));
        tables.push(TypedFacadeTableV2::new(
            closure_axiom_coordinate,
            closure_axiom_roots,
        ));
        tables.push(TypedFacadeTableV2::new(
            closure_signature_coordinate,
            closure_signature_roots,
        ));
        let mut raw_document_tables = Vec::new();
        raw_document_tables
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator raw typed index table failed"))?;
        raw_document_tables.push(TypedFacadeTableV2::new(
            document_axiom_coordinate,
            raw_axiom_roots,
        ));
        let storage = TypedFacadeStorageV2::freeze(
            self.frozen.arena().clone(),
            tables,
            raw_document_tables,
            1,
            Limits::default(),
            self.cancellation.clone(),
            None,
        )?;
        // Prepare the infallible platform mutex control block before the
        // signature page allocation sweep is armed.
        storage.counters()?;
        Ok(TypedFacadeIndexFixture {
            storage,
            cancellation: self.cancellation.clone(),
        })
    }

    /// Prepare byte-distinct effective and raw typed V2 tables and initialize
    /// the infallible platform mutex control block before allocation injection
    /// is armed, retaining only identifiers into the shared component arena.
    pub fn prepare_typed_facade_raw_reads(
        raw_canonical: &[u8],
        effective_canonical: &[u8],
    ) -> Result<TypedFacadeReadFixture, Failure> {
        let limits = Limits::default();
        let estimated_bytes = raw_canonical
            .len()
            .checked_add(effective_canonical.len())
            .ok_or_else(|| NativeError::limit("native allocator scoped fixture bytes overflow"))?;
        let cancellation = Cancellation::with_duration(None);
        let mut builder = NativeComponentBuilder::with_control(
            &limits,
            cancellation.clone(),
            None,
            estimated_bytes,
        )?;
        let raw_pending = builder.intern_canonical(raw_canonical)?;
        let effective_pending = builder.intern_canonical(effective_canonical)?;
        let frozen = builder.freeze()?;
        let raw_root = frozen.resolve(raw_pending)?;
        let effective_root = frozen.resolve(effective_pending)?;
        if raw_root == effective_root {
            return Err(
                NativeError::protocol("native allocator scoped roots must be distinct").into(),
            );
        }
        let coordinate = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let mut effective_roots = Vec::new();
        effective_roots.try_reserve_exact(1).map_err(|_| {
            NativeError::limit("native allocator effective typed root allocation failed")
        })?;
        effective_roots.push(effective_root);
        let mut raw_roots = Vec::new();
        raw_roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator raw typed root allocation failed"))?;
        raw_roots.push(raw_root);
        let mut tables = Vec::new();
        tables.try_reserve_exact(1).map_err(|_| {
            NativeError::limit("native allocator effective typed table allocation failed")
        })?;
        tables.push(TypedFacadeTableV2::new(coordinate, effective_roots));
        let mut raw_document_tables = Vec::new();
        raw_document_tables.try_reserve_exact(1).map_err(|_| {
            NativeError::limit("native allocator raw typed table allocation failed")
        })?;
        raw_document_tables.push(TypedFacadeTableV2::new(coordinate, raw_roots));
        let storage = TypedFacadeStorageV2::freeze(
            frozen.arena().clone(),
            tables,
            raw_document_tables,
            1,
            limits,
            cancellation.clone(),
            None,
        )?;
        // Keep the same mutex-control preparation boundary as the effective
        // owner fixture so the sweep isolates only production read buffers.
        storage.counters()?;
        Ok(TypedFacadeReadFixture {
            storage,
            coordinate,
            raw_document_owner: true,
            cancellation,
        })
    }

    /// Own one validated typed V2 table before allocation injection is armed,
    /// leaving its production freeze and retained index construction pending.
    pub fn prepare_typed_facade_freeze(&self) -> Result<TypedFacadeFreezeFixture, Failure> {
        let coordinate = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let mut roots = Vec::new();
        roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator typed root allocation failed"))?;
        roots.push(self.identifiers[0]);
        let mut tables = Vec::new();
        tables
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator typed table allocation failed"))?;
        tables.push(TypedFacadeTableV2::new(coordinate, roots));
        Ok(TypedFacadeFreezeFixture {
            arena: self.frozen.arena().clone(),
            tables,
            raw_document_tables: Vec::new(),
            cancellation: self.cancellation.clone(),
        })
    }

    /// Own matching effective and raw document tables before allocation
    /// injection is armed, leaving both production indexes pending.
    pub fn prepare_typed_facade_raw_freeze(&self) -> Result<TypedFacadeFreezeFixture, Failure> {
        let coordinate = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let mut effective_roots = Vec::new();
        effective_roots.try_reserve_exact(1).map_err(|_| {
            NativeError::limit("native allocator effective typed root allocation failed")
        })?;
        effective_roots.push(self.identifiers[0]);
        let mut raw_roots = Vec::new();
        raw_roots
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native allocator raw typed root allocation failed"))?;
        raw_roots.push(self.identifiers[0]);
        let mut tables = Vec::new();
        tables.try_reserve_exact(1).map_err(|_| {
            NativeError::limit("native allocator effective typed table allocation failed")
        })?;
        tables.push(TypedFacadeTableV2::new(coordinate, effective_roots));
        let mut raw_document_tables = Vec::new();
        raw_document_tables.try_reserve_exact(1).map_err(|_| {
            NativeError::limit("native allocator raw typed table allocation failed")
        })?;
        raw_document_tables.push(TypedFacadeTableV2::new(coordinate, raw_roots));
        Ok(TypedFacadeFreezeFixture {
            arena: self.frozen.arena().clone(),
            tables,
            raw_document_tables,
            cancellation: self.cancellation.clone(),
        })
    }

    /// Prepare an empty typed V2 builder and own one canonical axiom row
    /// before allocation injection is armed.
    pub fn prepare_typed_builder_add(
        &self,
        canonical: &[u8],
    ) -> Result<TypedBuilderAddFixture, Failure> {
        let builder =
            TypedFacadeBuilderV2::new(Limits::default(), self.cancellation.clone(), None, 0)?;
        let axioms = owned_single_row(canonical)?;
        Ok(TypedBuilderAddFixture {
            builder,
            axioms,
            effective_axioms: None,
        })
    }

    /// Prepare an empty typed V2 builder and own distinct raw/effective axiom
    /// rows before allocation injection is armed.
    pub fn prepare_typed_builder_add_scoped(
        &self,
        raw_canonical: &[u8],
        effective_canonical: &[u8],
    ) -> Result<TypedBuilderAddFixture, Failure> {
        let builder =
            TypedFacadeBuilderV2::new(Limits::default(), self.cancellation.clone(), None, 0)?;
        let axioms = owned_single_row(raw_canonical)?;
        let effective_axioms = owned_single_row(effective_canonical)?;
        Ok(TypedBuilderAddFixture {
            builder,
            axioms,
            effective_axioms: Some(effective_axioms),
        })
    }

    /// Prepare retained encoded-column metadata before allocation injection.
    ///
    /// The returned one-shot fixture isolates the production publication
    /// allocation from its already validated discovery/sort workspace.
    pub fn prepare_encoded_columns(&self) -> Result<EncodedColumnPublicationFixture<'_>, Failure> {
        let tables = [EncodedRootTableV2::new(
            EncodedRootKindV2::Axiom,
            &self.identifiers,
        )];
        let prepared = prepare_encoded_structural_columns_from_tables_v2(
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

/// One retained axiom-type index whose bounded page buffers have not yet been
/// allocated.
pub struct AxiomTypePageFixture {
    index: RetainedAxiomTypeIndexV1,
    tag: u16,
    cancellation: Cancellation,
}

impl AxiomTypePageFixture {
    /// Allocate and encode one exact constructor page, then return an
    /// allocation-free correctness summary after the page is dropped.
    pub fn page(&self) -> Result<[u64; 6], Failure> {
        let page = self.index.constructor_page(
            self.tag,
            0,
            64,
            8 * 1024 * 1024,
            &Limits::default(),
            self.cancellation.clone(),
            None,
        )?;
        let row = page.rows.first().ok_or_else(|| {
            NativeError::protocol("native allocator axiom-type page fixture emitted no row")
        })?;
        let row_count = u64::try_from(page.rows.len())
            .map_err(|_| NativeError::limit("native allocator page row count exceeds u64"))?;
        let row_bytes = u64::try_from(row.len())
            .map_err(|_| NativeError::limit("native allocator page bytes exceed u64"))?;
        Ok([
            page.total_count,
            page.next_cursor.unwrap_or(u64::MAX),
            row_count,
            row_bytes,
            u64::from(crc32c(row)),
            self.index.complete_root_encode_calls(),
        ])
    }
}

/// One typed V2 facade whose page and contains temporaries have not yet been
/// allocated.
pub struct TypedFacadeReadFixture {
    storage: TypedFacadeStorageV2,
    coordinate: TypedFacadeCoordinateV2,
    raw_document_owner: bool,
    cancellation: Cancellation,
}

/// One typed V2 facade whose retained signature and axiom-type indexes have
/// not yet been allocated.
pub struct TypedFacadeIndexFixture {
    storage: TypedFacadeStorageV2,
    cancellation: Cancellation,
}

/// One typed V2 facade input whose production validation and retained index
/// allocations have not yet run.
pub struct TypedFacadeFreezeFixture {
    arena: NativeComponentArena,
    tables: Vec<TypedFacadeTableV2>,
    raw_document_tables: Vec<TypedFacadeTableV2>,
    cancellation: Cancellation,
}

/// One empty typed V2 builder plus one owned canonical document row.
pub struct TypedBuilderAddFixture {
    builder: TypedFacadeBuilderV2,
    axioms: Vec<Vec<u8>>,
    effective_axioms: Option<Vec<Vec<u8>>>,
}

impl TypedBuilderAddFixture {
    /// Add one document while allocation injection is armed.
    pub fn add_document(mut self) -> Result<PendingTypedBuilderFixture, Failure> {
        let ordinal = match &self.effective_axioms {
            Some(effective_axioms) => self.builder.add_scoped_document(
                &[],
                &self.axioms,
                &[],
                &[],
                effective_axioms,
                &[],
            )?,
            None => self.builder.add_document(&[], &self.axioms, &[])?,
        };
        Ok(PendingTypedBuilderFixture {
            _builder: self.builder,
            ordinal,
        })
    }
}

/// One typed V2 builder returned across the disarmed allocator boundary.
pub struct PendingTypedBuilderFixture {
    _builder: TypedFacadeBuilderV2,
    ordinal: u64,
}

impl PendingTypedBuilderFixture {
    /// Return the stable document ordinal after allocation injection is
    /// disarmed, retaining the builder until this wrapper is dropped.
    pub const fn ordinal(&self) -> u64 {
        self.ordinal
    }
}

impl TypedFacadeFreezeFixture {
    /// Freeze the retained typed V2 owner while allocation injection is armed.
    pub fn freeze(self) -> Result<FrozenTypedFacadeFixture, Failure> {
        let storage = TypedFacadeStorageV2::freeze(
            self.arena,
            self.tables,
            self.raw_document_tables,
            1,
            Limits::default(),
            self.cancellation,
            None,
        )?;
        Ok(FrozenTypedFacadeFixture { storage })
    }
}

fn owned_single_row(canonical: &[u8]) -> Result<Vec<Vec<u8>>, Failure> {
    let mut row = Vec::new();
    row.try_reserve_exact(canonical.len())
        .map_err(|_| NativeError::limit("native allocator canonical row allocation failed"))?;
    row.extend_from_slice(canonical);
    let mut rows = Vec::new();
    rows.try_reserve_exact(1)
        .map_err(|_| NativeError::limit("native allocator row table allocation failed"))?;
    rows.push(row);
    Ok(rows)
}

/// One frozen typed V2 owner returned across the disarmed allocator boundary.
pub struct FrozenTypedFacadeFixture {
    storage: TypedFacadeStorageV2,
}

impl FrozenTypedFacadeFixture {
    /// Observe frozen retained ownership and counters after allocation
    /// injection has been disarmed.
    pub fn summary(&self) -> Result<[u64; 8], Failure> {
        let counters = self.storage.counters()?;
        let structural = self.storage.structural_counts()?;
        Ok([
            self.storage
                .retained_rows(TypedFacadeCollectionV2::Axioms)?,
            counters.canonical_input_rows,
            counters.retained_document_tables,
            counters.retained_root_rows,
            counters.retained_index_bytes,
            counters.retained_owner_bytes,
            structural.stored_axioms,
            structural.effective_axioms,
        ])
    }
}

impl TypedFacadeReadFixture {
    /// Visit and consume the selected canonical axiom roots while allocation
    /// injection is armed, retaining no encoded row after its callback.
    pub fn visit_canonical_roots(&self) -> Result<[u64; 3], Failure> {
        let mut rows = 0_u64;
        let mut bytes = 0_u64;
        let mut checksum = 0_u64;
        self.storage.visit_canonical_roots(
            TypedFacadeCollectionV2::Axioms,
            self.coordinate.scope,
            self.coordinate.document_ordinal,
            self.raw_document_owner,
            self.cancellation.clone(),
            None,
            |row| {
                rows = rows
                    .checked_add(1)
                    .ok_or_else(|| NativeError::limit("native allocator visit count overflow"))?;
                bytes = bytes
                    .checked_add(u64::try_from(row.len()).map_err(|_| {
                        NativeError::limit("native allocator visited row exceeds u64")
                    })?)
                    .ok_or_else(|| NativeError::limit("native allocator visit bytes overflow"))?;
                checksum = checksum.rotate_left(1) ^ u64::from(crc32c(row));
                Ok(())
            },
        )?;
        Ok([rows, bytes, checksum])
    }

    /// Build and consume the direct encoded structural columns selected by
    /// this owner role while allocation injection is armed.
    pub fn encoded_columns(&self) -> Result<[u64; 12], Failure> {
        let columns = self.storage.encoded_structural_columns(
            self.coordinate.scope,
            self.coordinate.document_ordinal,
            self.raw_document_owner,
            &Limits::default(),
            self.cancellation.clone(),
            None,
        )?;
        let buffers = columns.buffers().named();
        let mut summary = [0_u64; 12];
        for (index, (_name, buffer)) in buffers.iter().enumerate() {
            summary[index] = u64::try_from(buffer.len())
                .map_err(|_| NativeError::limit("native allocator encoded buffer exceeds u64"))?;
        }
        summary[11] = u64::from(crc32c(buffers[10].1));
        Ok(summary)
    }

    /// Allocate and encode one bounded page, returning an allocation-free
    /// correctness and counter summary.
    pub fn page(&self) -> Result<[u64; 10], Failure> {
        typed_page_summary(
            &self.storage,
            self.coordinate,
            self.raw_document_owner,
            self.cancellation.clone(),
        )
    }

    /// Probe exact axiom membership through the retained digest index and
    /// canonical encoder, returning an allocation-free counter summary.
    pub fn contains(&self, canonical: &[u8]) -> Result<[u64; 4], Failure> {
        let found = self.storage.contains_axiom(
            self.coordinate,
            self.raw_document_owner,
            canonical,
            self.cancellation.clone(),
            None,
        )?;
        let counters = self.storage.counters()?;
        Ok([
            u64::from(found),
            counters.contains_requests,
            counters.contains_hits,
            counters.canonical_encode_requests,
        ])
    }
}

fn typed_page_summary(
    storage: &TypedFacadeStorageV2,
    coordinate: TypedFacadeCoordinateV2,
    raw_document_owner: bool,
    cancellation: Cancellation,
) -> Result<[u64; 10], Failure> {
    let page = storage.page(
        TypedFacadePageRequestV2::new(coordinate, raw_document_owner, 0, 64, 8 * 1024 * 1024),
        cancellation,
        None,
    )?;
    let row = page
        .rows
        .first()
        .ok_or_else(|| NativeError::protocol("native allocator typed page emitted no row"))?;
    let row_count = u64::try_from(page.rows.len())
        .map_err(|_| NativeError::limit("native allocator typed page rows exceed u64"))?;
    let counters = storage.counters()?;
    Ok([
        page.total_count,
        page.next_cursor.unwrap_or(u64::MAX),
        row_count,
        page.page_bytes,
        u64::from(crc32c(row)),
        counters.page_requests,
        counters.pages_returned,
        counters.rows_emitted,
        counters.payload_bytes_copied,
        counters.canonical_encode_requests,
    ])
}

impl TypedFacadeIndexFixture {
    /// Page and encode the document signature table while allocation injection
    /// is armed.
    pub fn page_signature(&self) -> Result<[u64; 10], Failure> {
        self.page_selected_signature(TypedFacadeScopeV2::Document, Some(0))
    }

    /// Page and encode the closure signature table while allocation injection
    /// is armed.
    pub fn page_closure_signature(&self) -> Result<[u64; 10], Failure> {
        self.page_selected_signature(TypedFacadeScopeV2::Closure, None)
    }

    fn page_selected_signature(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
    ) -> Result<[u64; 10], Failure> {
        typed_page_summary(
            &self.storage,
            TypedFacadeCoordinateV2 {
                collection: TypedFacadeCollectionV2::Signature,
                scope,
                document_ordinal,
                signature_kind: crate::publication::TypedFacadeSignatureKindV2::All,
                include_builtins: true,
            },
            false,
            self.cancellation.clone(),
        )
    }

    /// Build and consume the typed V2 retained axiom-type index while
    /// allocation injection is armed.
    pub fn build_axiom_type_index(&self) -> Result<[u64; 5], Failure> {
        self.build_selected_axiom_type_index(TypedFacadeScopeV2::Document, Some(0), false)
    }

    /// Build and consume the raw document retained axiom-type index while
    /// allocation injection is armed.
    pub fn build_raw_axiom_type_index(&self) -> Result<[u64; 5], Failure> {
        self.build_selected_axiom_type_index(TypedFacadeScopeV2::Document, Some(0), true)
    }

    /// Build and consume the closure retained axiom-type index while allocation
    /// injection is armed.
    pub fn build_closure_axiom_type_index(&self) -> Result<[u64; 5], Failure> {
        self.build_selected_axiom_type_index(TypedFacadeScopeV2::Closure, None, false)
    }

    fn build_selected_axiom_type_index(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
    ) -> Result<[u64; 5], Failure> {
        let index = self.storage.axiom_type_index(
            scope,
            document_ordinal,
            raw_document_owner,
            &Limits::default(),
            self.cancellation.clone(),
            None,
        )?;
        let counters = index.counters();
        Ok([
            counters.axiom_rows,
            counters.constructor_groups,
            counters.category_groups,
            counters.retained_buffer_bytes,
            counters.complete_root_encode_calls,
        ])
    }

    /// Build and consume the typed V2 retained signature index while
    /// allocation injection is armed.
    pub fn build_signature_index(&self) -> Result<[u64; 6], Failure> {
        self.build_selected_signature_index(TypedFacadeScopeV2::Document, Some(0))
    }

    /// Build and consume the closure retained signature index while allocation
    /// injection is armed.
    pub fn build_closure_signature_index(&self) -> Result<[u64; 6], Failure> {
        self.build_selected_signature_index(TypedFacadeScopeV2::Closure, None)
    }

    fn build_selected_signature_index(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
    ) -> Result<[u64; 6], Failure> {
        let index = self.storage.signature_index(
            scope,
            document_ordinal,
            &Limits::default(),
            self.cancellation.clone(),
            None,
        )?;
        let counters = index.counters();
        Ok([
            counters.structural_root_rows,
            counters.entity_rows,
            counters.referenced_links,
            counters.nonannotation_links,
            counters.declaration_links,
            counters.complete_root_encode_calls,
        ])
    }
}

/// One prepared encoded-column result whose eleven production buffers have
/// not yet been allocated.
pub struct EncodedColumnPublicationFixture<'arena> {
    prepared: PreparedEncodedStructuralColumnsV2<'arena>,
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

/// One deterministic wire whose header and complete required directory are
/// valid but whose first empty-section digest is deliberately corrupt.
///
/// Construction happens before allocator injection. Validation therefore
/// reaches the production directory, range, and table-ledger allocations
/// before returning the stable wire corruption.
#[derive(Debug)]
pub struct WireValidationFixture {
    bytes: Vec<u8>,
    cancellation: Cancellation,
    limits: Limits,
}

impl WireValidationFixture {
    /// Prepare the malformed wire outside the allocator sweep.
    pub fn new() -> Result<Self, Failure> {
        let mut bytes = Vec::new();
        bytes
            .try_reserve_exact(WIRE_FIXTURE_BYTES)
            .map_err(|_| Failure {
                code: "NATIVE_WIRE_LIMIT",
                message: "native wire allocator test-fixture allocation failed",
            })?;
        bytes.resize(WIRE_FIXTURE_BYTES, 0);
        bytes[..8].copy_from_slice(b"PYOCORE\0");
        bytes[8..10].copy_from_slice(&1_u16.to_le_bytes());
        bytes[10..12].copy_from_slice(&0_u16.to_le_bytes());
        bytes[12..16].copy_from_slice(&(WIRE_HEADER_BYTES as u32).to_le_bytes());
        bytes[16..20].copy_from_slice(&0_u32.to_le_bytes());
        bytes[20..24].copy_from_slice(&(WIRE_SECTION_COUNT as u32).to_le_bytes());
        bytes[24..28].copy_from_slice(&MODEL_SCHEMA.to_le_bytes());
        bytes[28..32].copy_from_slice(&1_u32.to_le_bytes());
        bytes[32..40].copy_from_slice(&(WIRE_FIXTURE_BYTES as u64).to_le_bytes());
        bytes[40..48].copy_from_slice(&(WIRE_HEADER_BYTES as u64).to_le_bytes());
        bytes[48..56]
            .copy_from_slice(&((WIRE_DIRECTORY_BYTES * WIRE_SECTION_COUNT) as u64).to_le_bytes());
        for index in 0..WIRE_SECTION_COUNT {
            let offset = WIRE_HEADER_BYTES + index * WIRE_DIRECTORY_BYTES;
            let kind = u16::try_from(index + 1).map_err(|_| Failure {
                code: "NATIVE_WIRE_LIMIT",
                message: "native wire allocator test-fixture section count overflowed",
            })?;
            bytes[offset..offset + 2].copy_from_slice(&kind.to_le_bytes());
            bytes[offset + 2..offset + 4].copy_from_slice(&1_u16.to_le_bytes());
            bytes[offset + 4..offset + 8].copy_from_slice(&1_u32.to_le_bytes());
            bytes[offset + 8..offset + 16]
                .copy_from_slice(&(WIRE_FIXTURE_BYTES as u64).to_le_bytes());
        }
        let header_crc = crc32c(&bytes[..WIRE_HEADER_BYTES]);
        bytes[88..92].copy_from_slice(&header_crc.to_le_bytes());
        Ok(Self {
            bytes,
            cancellation: Cancellation::with_duration(None),
            limits: Limits::default(),
        })
    }

    /// Validate through the production wire path and return its deliberate
    /// corruption only after all pre-semantic ledgers have been allocated.
    pub fn validate(&self) -> Result<(), Failure> {
        let mut guard = Guard::new(self.cancellation.clone(), None, 1);
        crate::wire::validate_process_allocator_fixture(&self.bytes, &self.limits, &mut guard)
            .map_err(Failure::from)
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
