//! Production typed storage for the V2 lazy structural facade.
//!
//! This owner retains component identifiers into exactly one immutable
//! [`NativeComponentArena`]. Canonical bytes are produced only for bounded
//! validation, paging, and digest-collision checks; they are never retained as
//! a second complete row table.

use std::mem::size_of;
use std::sync::Mutex;

use crate::cancel::{Cancellation, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::index::{build_retained_axiom_type_index_v1, RetainedAxiomTypeIndexV1};
use crate::limits::{LimitKey, Limits};
use crate::model::{
    prepare_encoded_structural_columns_from_tables_v1, scan_canonical, structural_digest_v1,
    Category, ComponentCounters, ComponentId, EncodedRootKindV1, EncodedRootTableV1,
    EncodedStructuralColumnsV1, NativeComponentArena, NativeComponentDigestIndex,
    PreparedEncodedStructuralColumnsV1, ScanBudget,
};

const MAX_TYPED_FACADE_TABLES_V2: usize = 100_000;
const MAX_FACADE_PAGE_ROWS_V2: u32 = 64;
const MAX_FACADE_PAGE_BYTES_V2: u64 = 8 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct TableValidationV2 {
    maximum_row_bytes: u64,
    peak_workspace_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) enum TypedFacadeCollectionV2 {
    OntologyAnnotations,
    Axioms,
    Extensions,
    Signature,
}

impl TypedFacadeCollectionV2 {
    const fn category(self) -> Category {
        match self {
            Self::OntologyAnnotations => Category::Annotation,
            Self::Axioms => Category::Axiom,
            Self::Extensions => Category::Swrl,
            Self::Signature => Category::Entity,
        }
    }

    const fn raw_for_document_owner(self) -> bool {
        matches!(
            self,
            Self::OntologyAnnotations | Self::Axioms | Self::Extensions
        )
    }

    const fn accepts_root_tag(self, tag: u16) -> bool {
        match self {
            Self::OntologyAnnotations => tag == 5,
            Self::Axioms => matches!(
                tag,
                60..=64 | 70..=82 | 90..=95 | 100..=101 | 110..=116 | 120..=123
            ),
            Self::Extensions => tag == 148,
            Self::Signature => tag == 2,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) enum TypedFacadeScopeV2 {
    Document,
    Closure,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) enum TypedFacadeSignatureKindV2 {
    All,
    Class,
    Datatype,
    ObjectProperty,
    DataProperty,
    AnnotationProperty,
    NamedIndividual,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) struct TypedFacadeCoordinateV2 {
    pub(crate) collection: TypedFacadeCollectionV2,
    pub(crate) scope: TypedFacadeScopeV2,
    pub(crate) document_ordinal: Option<u64>,
    pub(crate) signature_kind: TypedFacadeSignatureKindV2,
    pub(crate) include_builtins: bool,
}

impl TypedFacadeCoordinateV2 {
    pub(crate) const fn document(
        collection: TypedFacadeCollectionV2,
        document_ordinal: u64,
    ) -> Self {
        Self {
            collection,
            scope: TypedFacadeScopeV2::Document,
            document_ordinal: Some(document_ordinal),
            signature_kind: TypedFacadeSignatureKindV2::All,
            include_builtins: true,
        }
    }

    pub(crate) const fn closure(collection: TypedFacadeCollectionV2) -> Self {
        Self {
            collection,
            scope: TypedFacadeScopeV2::Closure,
            document_ordinal: None,
            signature_kind: TypedFacadeSignatureKindV2::All,
            include_builtins: true,
        }
    }
}

#[derive(Debug)]
pub(crate) struct TypedFacadeTableV2 {
    coordinate: TypedFacadeCoordinateV2,
    roots: Vec<ComponentId>,
    axiom_index: Option<NativeComponentDigestIndex>,
}

impl TypedFacadeTableV2 {
    pub(crate) const fn new(coordinate: TypedFacadeCoordinateV2, roots: Vec<ComponentId>) -> Self {
        Self {
            coordinate,
            roots,
            axiom_index: None,
        }
    }

    pub(crate) const fn coordinate(&self) -> TypedFacadeCoordinateV2 {
        self.coordinate
    }

    pub(crate) fn len(&self) -> usize {
        self.roots.len()
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.roots.is_empty()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct TypedFacadePageV2 {
    pub(crate) total_count: u64,
    pub(crate) next_cursor: Option<u64>,
    pub(crate) rows: Vec<Vec<u8>>,
    pub(crate) page_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct TypedFacadePageRequestV2 {
    pub(crate) coordinate: TypedFacadeCoordinateV2,
    pub(crate) raw_document_owner: bool,
    pub(crate) start: u64,
    pub(crate) max_rows: u32,
    pub(crate) max_bytes: u64,
}

impl TypedFacadePageRequestV2 {
    pub(crate) const fn new(
        coordinate: TypedFacadeCoordinateV2,
        raw_document_owner: bool,
        start: u64,
        max_rows: u32,
        max_bytes: u64,
    ) -> Self {
        Self {
            coordinate,
            raw_document_owner,
            start,
            max_rows,
            max_bytes,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct RuntimeCountersV2 {
    page_requests: u64,
    pages_returned: u64,
    rows_emitted: u64,
    payload_bytes_copied: u64,
    contains_requests: u64,
    contains_hits: u64,
    canonical_encode_requests: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct TypedFacadeCounterSnapshotV2 {
    pub(crate) component: ComponentCounters,
    pub(crate) canonical_input_rows: u64,
    pub(crate) canonical_input_bytes: u64,
    pub(crate) retained_document_tables: u64,
    pub(crate) retained_root_rows: u64,
    pub(crate) retained_component_bytes: u64,
    pub(crate) retained_root_bytes: u64,
    pub(crate) retained_index_bytes: u64,
    pub(crate) retained_metadata_bytes: u64,
    pub(crate) retained_owner_bytes: u64,
    pub(crate) peak_builder_live_bytes: u64,
    pub(crate) peak_freeze_live_bytes: u64,
    pub(crate) publication_structural_rows_copied: u64,
    pub(crate) publication_structural_bytes_copied: u64,
    pub(crate) page_requests: u64,
    pub(crate) pages_returned: u64,
    pub(crate) rows_emitted: u64,
    pub(crate) payload_bytes_copied: u64,
    pub(crate) contains_requests: u64,
    pub(crate) contains_hits: u64,
    pub(crate) canonical_encode_requests: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct TypedFacadeStructuralCountsV2 {
    pub(crate) ontology_annotations: u64,
    pub(crate) stored_axioms: u64,
    pub(crate) effective_axioms: u64,
    pub(crate) extensions: u64,
}

#[derive(Debug)]
pub(crate) struct TypedFacadeStorageV2 {
    // This is the only arena handle retained by this owner. Tables and indexes
    // below contain owner-checked ComponentIds only.
    arena: NativeComponentArena,
    effective_tables: Vec<TypedFacadeTableV2>,
    raw_document_tables: Vec<TypedFacadeTableV2>,
    document_count: u64,
    maximum_row_bytes: u64,
    limits: Limits,
    external_retained_bytes: usize,
    frozen_counters: TypedFacadeCounterSnapshotV2,
    runtime_counters: Mutex<RuntimeCountersV2>,
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct TypedFacadeStorageObservationV2 {
    pub(crate) arena_fields: u64,
    pub(crate) root_identifier_rows: u64,
    pub(crate) axiom_index_rows: u64,
    pub(crate) retained_canonical_byte_rows: u64,
}

impl TypedFacadeStorageV2 {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn freeze(
        arena: NativeComponentArena,
        effective_tables: Vec<TypedFacadeTableV2>,
        raw_document_tables: Vec<TypedFacadeTableV2>,
        document_count: u64,
        limits: Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<Self> {
        Self::freeze_with_external(
            arena,
            effective_tables,
            raw_document_tables,
            document_count,
            limits,
            cancellation,
            interrupt,
            0,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn freeze_with_external(
        arena: NativeComponentArena,
        mut effective_tables: Vec<TypedFacadeTableV2>,
        mut raw_document_tables: Vec<TypedFacadeTableV2>,
        document_count: u64,
        limits: Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        caller_external_bytes: usize,
    ) -> NativeResult<Self> {
        cancellation.checkpoint()?;
        if document_count > limits.max_documents {
            return Err(NativeError::limit(
                "typed V2 publication exceeds max_documents",
            ));
        }
        let table_count = effective_tables
            .len()
            .checked_add(raw_document_tables.len())
            .ok_or_else(|| NativeError::limit("typed V2 facade table count overflow"))?;
        if table_count > MAX_TYPED_FACADE_TABLES_V2 {
            return Err(NativeError::limit(
                "typed V2 publication has too many facade tables",
            ));
        }
        effective_tables.sort_unstable_by_key(|table| table.coordinate);
        raw_document_tables.sort_unstable_by_key(|table| table.coordinate);
        reject_duplicate_coordinates(&effective_tables)?;
        reject_duplicate_coordinates(&raw_document_tables)?;
        validate_coordinates(&effective_tables, document_count, false)?;
        validate_coordinates(&raw_document_tables, document_count, true)?;

        let retained_root_rows = root_count(&effective_tables, &raw_document_tables)?;
        if retained_root_rows > limits.value(LimitKey::MaxIndexRows) {
            return Err(NativeError::limit(
                "typed V2 root tables exceed max_index_rows",
            ));
        }
        let retained_root_bytes = root_allocation_bytes(&effective_tables, &raw_document_tables)?;
        let retained_metadata_bytes =
            metadata_allocation_bytes(effective_tables.capacity(), raw_document_tables.capacity())?;
        let retained_base_external = checked_add(retained_root_bytes, retained_metadata_bytes)?;
        let caller_external = u64::try_from(caller_external_bytes)
            .map_err(|_| NativeError::limit("typed V2 caller allocation exceeds u64"))?;
        let live_base_external = checked_add(retained_base_external, caller_external)?;
        check_retained_limit(&arena, live_base_external, 0, &limits)?;
        let live_base_external_usize = usize::try_from(live_base_external)
            .map_err(|_| NativeError::limit("typed V2 retained metadata exceeds usize"))?;

        let mut maximum_row_bytes = 1_u64;
        let mut peak_ordering_workspace_bytes = 0_u64;
        for table in effective_tables.iter().chain(&raw_document_tables) {
            let validation = validate_table(
                &arena,
                table,
                &limits,
                cancellation.clone(),
                interrupt.clone(),
                live_base_external_usize,
            )?;
            maximum_row_bytes = maximum_row_bytes.max(validation.maximum_row_bytes);
            peak_ordering_workspace_bytes =
                peak_ordering_workspace_bytes.max(validation.peak_workspace_bytes);
        }

        let mut retained_index_bytes = 0_u64;
        for table in effective_tables.iter_mut().chain(&mut raw_document_tables) {
            if table.coordinate.collection != TypedFacadeCollectionV2::Axioms {
                continue;
            }
            let index_external = checked_add(live_base_external, retained_index_bytes)?;
            let index_external = usize::try_from(index_external)
                .map_err(|_| NativeError::limit("typed V2 index workspace exceeds usize"))?;
            let index = NativeComponentDigestIndex::build_with_external(
                &arena,
                &table.roots,
                Category::Axiom,
                &limits,
                cancellation.clone(),
                interrupt.clone(),
                index_external,
            )?;
            retained_index_bytes = checked_add(retained_index_bytes, index.retained_bytes())?;
            if retained_index_bytes > limits.value(LimitKey::MaxIndexBytes) {
                return Err(NativeError::limit(
                    "typed V2 axiom indexes exceed max_index_bytes",
                ));
            }
            check_retained_limit(&arena, live_base_external, retained_index_bytes, &limits)?;
            table.axiom_index = Some(index);
        }

        let external_retained = checked_add(retained_base_external, retained_index_bytes)?;
        let external_retained_bytes = usize::try_from(external_retained)
            .map_err(|_| NativeError::limit("typed V2 retained owner exceeds usize"))?;
        let component = *arena.counters();
        let retained_owner_bytes = checked_add(component.retained_bytes, external_retained)?;
        let canonical_input = canonical_input_counters(
            &arena,
            &effective_tables,
            &raw_document_tables,
            &limits,
            cancellation.clone(),
            interrupt,
            external_retained_bytes
                .checked_add(caller_external_bytes)
                .ok_or_else(|| NativeError::limit("typed V2 canonical input memory overflow"))?,
        )?;
        let publication_peak = retained_owner_bytes
            .checked_add(caller_external)
            .and_then(|value| value.checked_add(maximum_row_bytes))
            .ok_or_else(|| NativeError::limit("typed V2 publication memory peak overflow"))?;
        let ordering_peak = component
            .retained_bytes
            .checked_add(live_base_external)
            .and_then(|value| value.checked_add(peak_ordering_workspace_bytes))
            .ok_or_else(|| NativeError::limit("typed V2 ordering memory peak overflow"))?;
        let temporary_peak = publication_peak.max(ordering_peak);
        if limits
            .max_memory_bytes
            .is_some_and(|maximum| temporary_peak > maximum)
        {
            return Err(NativeError::limit(
                "typed V2 freeze exceeds max_memory_bytes",
            ));
        }
        let frozen_counters = TypedFacadeCounterSnapshotV2 {
            component,
            canonical_input_rows: canonical_input.0,
            canonical_input_bytes: canonical_input.1,
            retained_document_tables: document_count,
            retained_root_rows,
            retained_component_bytes: component.retained_bytes,
            retained_root_bytes,
            retained_index_bytes,
            retained_metadata_bytes,
            retained_owner_bytes,
            peak_builder_live_bytes: component.peak_builder_bytes,
            peak_freeze_live_bytes: component.peak_builder_bytes.max(temporary_peak),
            publication_structural_rows_copied: 0,
            publication_structural_bytes_copied: 0,
            ..TypedFacadeCounterSnapshotV2::default()
        };

        Ok(Self {
            arena,
            effective_tables,
            raw_document_tables,
            document_count,
            maximum_row_bytes,
            limits,
            external_retained_bytes,
            frozen_counters,
            runtime_counters: Mutex::new(RuntimeCountersV2::default()),
        })
    }

    pub(crate) const fn document_count(&self) -> u64 {
        self.document_count
    }

    pub(crate) const fn maximum_row_bytes(&self) -> u64 {
        self.maximum_row_bytes
    }

    pub(crate) const fn max_memory_bytes(&self) -> Option<u64> {
        self.limits.max_memory_bytes
    }

    pub(crate) const fn arena(&self) -> &NativeComponentArena {
        &self.arena
    }

    pub(crate) fn page(
        &self,
        request: TypedFacadePageRequestV2,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<TypedFacadePageV2> {
        self.validate_request(request.coordinate)?;
        validate_page_bounds(request.max_rows, request.max_bytes, &self.limits)?;
        let empty = &[];
        let roots = self
            .select_table(request.coordinate, request.raw_document_owner)
            .map_or(empty.as_slice(), |table| table.roots.as_slice());
        let total_count = u64::try_from(roots.len())
            .map_err(|_| NativeError::limit("typed V2 page total exceeds u64"))?;
        if request.start > total_count {
            return Err(NativeError::protocol(
                "typed V2 page start exceeds the selected collection total",
            ));
        }
        let start_index = usize::try_from(request.start)
            .map_err(|_| NativeError::limit("typed V2 page start exceeds usize"))?;
        let requested_stop = start_index.saturating_add(request.max_rows as usize);
        let stop = roots.len().min(requested_stop);
        let mut rows = Vec::new();
        rows.try_reserve_exact(stop.saturating_sub(start_index))
            .map_err(|_| NativeError::limit("typed V2 page allocation failed"))?;
        let outer_bytes = rows
            .capacity()
            .checked_mul(size_of::<Vec<u8>>())
            .ok_or_else(|| NativeError::limit("typed V2 page allocation size overflow"))?;
        let mut page_bytes = 0_u64;
        let mut retained_row_bytes = 0_u64;
        for identifier in &roots[start_index..stop] {
            let external = self.encoding_external_bytes(outer_bytes, retained_row_bytes)?;
            let row_len = self.arena.encoded_len(
                *identifier,
                &self.limits,
                cancellation.clone(),
                interrupt.clone(),
                external,
            )?;
            let row_len = u64::try_from(row_len)
                .map_err(|_| NativeError::limit("typed V2 page row exceeds u64"))?;
            if !rows.is_empty()
                && page_bytes
                    .checked_add(row_len)
                    .is_none_or(|following| following > request.max_bytes)
            {
                break;
            }
            let row = self.arena.encode(
                *identifier,
                &self.limits,
                cancellation.clone(),
                interrupt.clone(),
                external,
            )?;
            retained_row_bytes = checked_add(
                retained_row_bytes,
                u64::try_from(row.capacity())
                    .map_err(|_| NativeError::limit("typed V2 page allocation exceeds u64"))?,
            )?;
            self.check_page_memory(outer_bytes, retained_row_bytes)?;
            page_bytes = checked_add(page_bytes, row_len)?;
            rows.push(row);
        }
        cancellation.checkpoint()?;
        let emitted = u64::try_from(rows.len())
            .map_err(|_| NativeError::limit("typed V2 emitted row count exceeds u64"))?;
        let end = request
            .start
            .checked_add(emitted)
            .ok_or_else(|| NativeError::limit("typed V2 page cursor overflow"))?;
        let page = TypedFacadePageV2 {
            total_count,
            next_cursor: (end != total_count).then_some(end),
            rows,
            page_bytes,
        };
        self.update_page_counters(emitted, page_bytes)?;
        Ok(page)
    }

    pub(crate) fn contains_axiom(
        &self,
        coordinate: TypedFacadeCoordinateV2,
        raw_document_owner: bool,
        canonical: &[u8],
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<bool> {
        self.validate_request(coordinate)?;
        if coordinate.collection != TypedFacadeCollectionV2::Axioms {
            return Err(NativeError::protocol("typed V2 contains is axioms-only"));
        }
        let mut scan = ScanBudget::from_limits(&self.limits);
        if scan_canonical(canonical, &mut scan)? != Category::Axiom {
            self.update_contains_counters(false, 0)?;
            return Ok(false);
        }
        let Some(index) = self
            .select_table(coordinate, raw_document_owner)
            .and_then(|table| table.axiom_index.as_ref())
        else {
            self.update_contains_counters(false, 0)?;
            return Ok(false);
        };
        let mut encode_requests = 0_u64;
        for identifier in index.matching_ids(structural_digest_v1(canonical)) {
            let encoded = self.arena.encode(
                identifier,
                &self.limits,
                cancellation.clone(),
                interrupt.clone(),
                self.external_retained_bytes,
            )?;
            encode_requests = checked_add(encode_requests, 1)?;
            if encoded == canonical {
                self.update_contains_counters(true, encode_requests)?;
                return Ok(true);
            }
        }
        cancellation.checkpoint()?;
        self.update_contains_counters(false, encode_requests)?;
        Ok(false)
    }

    pub(crate) fn counters(&self) -> NativeResult<TypedFacadeCounterSnapshotV2> {
        let runtime = *self
            .runtime_counters
            .lock()
            .map_err(|_| NativeError::panic())?;
        Ok(TypedFacadeCounterSnapshotV2 {
            page_requests: runtime.page_requests,
            pages_returned: runtime.pages_returned,
            rows_emitted: runtime.rows_emitted,
            payload_bytes_copied: runtime.payload_bytes_copied,
            contains_requests: runtime.contains_requests,
            contains_hits: runtime.contains_hits,
            canonical_encode_requests: runtime.canonical_encode_requests,
            ..self.frozen_counters
        })
    }

    pub(crate) fn retained_rows(&self, collection: TypedFacadeCollectionV2) -> NativeResult<u64> {
        self.effective_tables
            .iter()
            .chain(&self.raw_document_tables)
            .filter(|table| table.coordinate.collection == collection)
            .try_fold(0_u64, |total, table| {
                checked_add(
                    total,
                    u64::try_from(table.roots.len()).map_err(|_| {
                        NativeError::limit("typed V2 retained row count exceeds u64")
                    })?,
                )
            })
    }

    /// Derive the structural counts attested by the publication envelope from
    /// retained root identifiers only. Stored counts use raw document-owner
    /// overrides when present and otherwise fall back to the effective
    /// document table; the effective axiom count is the closure table.
    pub(crate) fn structural_counts(&self) -> NativeResult<TypedFacadeStructuralCountsV2> {
        Ok(TypedFacadeStructuralCountsV2 {
            ontology_annotations: self
                .raw_document_count(TypedFacadeCollectionV2::OntologyAnnotations)?,
            stored_axioms: self.raw_document_count(TypedFacadeCollectionV2::Axioms)?,
            effective_axioms: self.table_count(TypedFacadeCoordinateV2::closure(
                TypedFacadeCollectionV2::Axioms,
            ))?,
            extensions: self.raw_document_count(TypedFacadeCollectionV2::Extensions)?,
        })
    }

    /// Build direct structural columns by borrowing the retained root tables.
    /// The encoded owner keeps the shared component arena alive but does not
    /// retain or construct a second root-identifier table.
    pub(crate) fn encoded_structural_columns(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<EncodedStructuralColumnsV1> {
        self.prepare_encoded_structural_columns(
            scope,
            document_ordinal,
            raw_document_owner,
            limits,
            cancellation,
            interrupt,
        )?
        .into_columns()
    }

    /// Prepare an exact direct-fill layout over the retained root tables. The
    /// returned plan borrows this owner and can fill one caller-provided byte
    /// arena without first materializing per-column Rust buffers.
    pub(crate) fn prepare_encoded_structural_columns(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<PreparedEncodedStructuralColumnsV1<'_>> {
        let annotations = self.structural_roots(
            TypedFacadeCollectionV2::OntologyAnnotations,
            scope,
            document_ordinal,
            raw_document_owner,
        )?;
        let axioms = self.structural_roots(
            TypedFacadeCollectionV2::Axioms,
            scope,
            document_ordinal,
            raw_document_owner,
        )?;
        let extensions = self.structural_roots(
            TypedFacadeCollectionV2::Extensions,
            scope,
            document_ordinal,
            raw_document_owner,
        )?;
        let tables = [
            EncodedRootTableV1::new(EncodedRootKindV1::OntologyAnnotation, annotations),
            EncodedRootTableV1::new(EncodedRootKindV1::Axiom, axioms),
            EncodedRootTableV1::new(EncodedRootKindV1::Extension, extensions),
        ];
        prepare_encoded_structural_columns_from_tables_v1(
            &self.arena,
            &tables,
            limits,
            cancellation,
            interrupt,
            self.external_retained_bytes,
        )
    }

    /// Build exact-constructor postings directly over a retained axiom root
    /// table. Postings are stable table ordinals and the returned index shares
    /// this facade's component arena rather than materializing axiom rows.
    pub(crate) fn axiom_type_index(
        &self,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<RetainedAxiomTypeIndexV1> {
        let roots = self.structural_roots(
            TypedFacadeCollectionV2::Axioms,
            scope,
            document_ordinal,
            raw_document_owner,
        )?;
        let index = build_retained_axiom_type_index_v1(
            &self.arena,
            roots,
            limits,
            cancellation,
            interrupt,
            self.external_retained_bytes,
        )?;
        let expected_offset_count = index
            .tags()
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("typed V2 axiom-type offset count overflow"))?;
        let expected_category_offset_count =
            index.category_codes().len().checked_add(1).ok_or_else(|| {
                NativeError::limit("typed V2 axiom category offset count overflow")
            })?;
        let counters = index.counters();
        if !index.owner().shares_storage_with(&self.arena)
            || index.tags().windows(2).any(|pair| pair[0] >= pair[1])
            || index.offsets().len() != expected_offset_count
            || index.offsets().first() != Some(&0)
            || index.offsets().last().copied()
                != Some(u64::try_from(index.postings().len()).map_err(|_| {
                    NativeError::limit("typed V2 axiom-type posting count exceeds u64")
                })?)
            || index.offsets().windows(2).any(|pair| pair[0] > pair[1])
            || index
                .category_codes()
                .windows(2)
                .any(|pair| pair[0] >= pair[1])
            || index.category_offsets().len() != expected_category_offset_count
            || index.category_offsets().first() != Some(&0)
            || index.category_offsets().last().copied()
                != Some(u64::try_from(index.postings().len()).map_err(|_| {
                    NativeError::limit("typed V2 axiom category posting count exceeds u64")
                })?)
            || index
                .category_offsets()
                .windows(2)
                .any(|pair| pair[0] > pair[1])
            || index
                .postings()
                .iter()
                .copied()
                .enumerate()
                .any(|(ordinal, posting)| u64::try_from(ordinal) != Ok(posting))
            || counters.axiom_rows
                != u64::try_from(roots.len())
                    .map_err(|_| NativeError::limit("typed V2 axiom-type root count exceeds u64"))?
            || counters.constructor_groups
                != u64::try_from(index.tags().len()).map_err(|_| {
                    NativeError::limit("typed V2 axiom-type group count exceeds u64")
                })?
            || counters.category_groups
                != u64::try_from(index.category_codes().len()).map_err(|_| {
                    NativeError::limit("typed V2 axiom category group count exceeds u64")
                })?
            || counters.complete_root_encode_calls != 0
        {
            return Err(NativeError::protocol(
                "typed V2 retained axiom-type layout drifted",
            ));
        }
        Ok(index)
    }

    #[cfg(test)]
    pub(crate) fn observation_for_tests(&self) -> NativeResult<TypedFacadeStorageObservationV2> {
        let root_identifier_rows = root_count(&self.effective_tables, &self.raw_document_tables)?;
        let axiom_index_rows = self
            .effective_tables
            .iter()
            .chain(&self.raw_document_tables)
            .filter_map(|table| table.axiom_index.as_ref())
            .try_fold(0_u64, |total, index| {
                checked_add(
                    total,
                    u64::try_from(index.len()).map_err(|_| {
                        NativeError::limit("typed V2 observed index rows exceed u64")
                    })?,
                )
            })?;
        Ok(TypedFacadeStorageObservationV2 {
            arena_fields: 1,
            root_identifier_rows,
            axiom_index_rows,
            // This owner has no canonical byte-row field. Page and contains
            // temporaries are returned or dropped before this observation.
            retained_canonical_byte_rows: 0,
        })
    }

    fn validate_request(&self, coordinate: TypedFacadeCoordinateV2) -> NativeResult<()> {
        validate_coordinate(coordinate, self.document_count, false)
    }

    fn select_table(
        &self,
        coordinate: TypedFacadeCoordinateV2,
        raw_document_owner: bool,
    ) -> Option<&TypedFacadeTableV2> {
        if raw_document_owner
            && coordinate.scope == TypedFacadeScopeV2::Document
            && coordinate.collection.raw_for_document_owner()
        {
            if let Ok(index) = self
                .raw_document_tables
                .binary_search_by_key(&coordinate, |table| table.coordinate)
            {
                return self.raw_document_tables.get(index);
            }
        }
        self.effective_tables
            .binary_search_by_key(&coordinate, |table| table.coordinate)
            .ok()
            .and_then(|index| self.effective_tables.get(index))
    }

    fn raw_document_count(&self, collection: TypedFacadeCollectionV2) -> NativeResult<u64> {
        let mut total = 0_u64;
        for table in &self.effective_tables {
            if table.coordinate.collection != collection
                || table.coordinate.scope != TypedFacadeScopeV2::Document
                || self
                    .raw_document_tables
                    .binary_search_by_key(&table.coordinate, |candidate| candidate.coordinate)
                    .is_ok()
            {
                continue;
            }
            total = checked_add(total, table_count(table)?)?;
        }
        for table in &self.raw_document_tables {
            if table.coordinate.collection == collection {
                total = checked_add(total, table_count(table)?)?;
            }
        }
        Ok(total)
    }

    fn structural_roots(
        &self,
        collection: TypedFacadeCollectionV2,
        scope: TypedFacadeScopeV2,
        document_ordinal: Option<u64>,
        raw_document_owner: bool,
    ) -> NativeResult<&[ComponentId]> {
        let coordinate = TypedFacadeCoordinateV2 {
            collection,
            scope,
            document_ordinal,
            signature_kind: TypedFacadeSignatureKindV2::All,
            include_builtins: true,
        };
        validate_coordinate(coordinate, self.document_count, raw_document_owner)?;
        Ok(self
            .select_table(coordinate, raw_document_owner)
            .map_or(&[], |table| table.roots.as_slice()))
    }

    fn table_count(&self, coordinate: TypedFacadeCoordinateV2) -> NativeResult<u64> {
        self.effective_tables
            .binary_search_by_key(&coordinate, |table| table.coordinate)
            .ok()
            .and_then(|index| self.effective_tables.get(index))
            .map_or(Ok(0), table_count)
    }

    fn encoding_external_bytes(
        &self,
        outer_bytes: usize,
        allocated_row_bytes: u64,
    ) -> NativeResult<usize> {
        self.external_retained_bytes
            .checked_add(outer_bytes)
            .and_then(|value| {
                usize::try_from(allocated_row_bytes)
                    .ok()?
                    .checked_add(value)
            })
            .ok_or_else(|| NativeError::limit("typed V2 page memory accounting overflow"))
    }

    fn check_page_memory(&self, outer_bytes: usize, allocated_row_bytes: u64) -> NativeResult<()> {
        let external = self.encoding_external_bytes(outer_bytes, allocated_row_bytes)?;
        let external = u64::try_from(external)
            .map_err(|_| NativeError::limit("typed V2 page allocation exceeds u64"))?;
        let live = self
            .arena
            .counters()
            .retained_bytes
            .checked_add(external)
            .ok_or_else(|| NativeError::limit("typed V2 page memory accounting overflow"))?;
        if self
            .limits
            .max_memory_bytes
            .is_some_and(|maximum| live > maximum)
        {
            return Err(NativeError::limit("typed V2 page exceeds max_memory_bytes"));
        }
        Ok(())
    }

    fn update_page_counters(&self, rows: u64, bytes: u64) -> NativeResult<()> {
        let mut counters = self
            .runtime_counters
            .lock()
            .map_err(|_| NativeError::panic())?;
        let following = RuntimeCountersV2 {
            page_requests: checked_add(counters.page_requests, 1)?,
            pages_returned: checked_add(counters.pages_returned, 1)?,
            rows_emitted: checked_add(counters.rows_emitted, rows)?,
            payload_bytes_copied: checked_add(counters.payload_bytes_copied, bytes)?,
            canonical_encode_requests: checked_add(counters.canonical_encode_requests, rows)?,
            ..*counters
        };
        *counters = following;
        Ok(())
    }

    fn update_contains_counters(&self, found: bool, encodes: u64) -> NativeResult<()> {
        let mut counters = self
            .runtime_counters
            .lock()
            .map_err(|_| NativeError::panic())?;
        let following = RuntimeCountersV2 {
            contains_requests: checked_add(counters.contains_requests, 1)?,
            contains_hits: checked_add(counters.contains_hits, u64::from(found))?,
            canonical_encode_requests: checked_add(counters.canonical_encode_requests, encodes)?,
            ..*counters
        };
        *counters = following;
        Ok(())
    }
}

fn validate_page_bounds(max_rows: u32, max_bytes: u64, limits: &Limits) -> NativeResult<()> {
    if !(1..=MAX_FACADE_PAGE_ROWS_V2).contains(&max_rows)
        || u64::from(max_rows) > limits.max_wire_rows
    {
        return Err(NativeError::limit(
            "typed V2 page max_rows is zero or exceeds its bound",
        ));
    }
    if !(1..=MAX_FACADE_PAGE_BYTES_V2).contains(&max_bytes) || max_bytes > limits.max_wire_bytes {
        return Err(NativeError::limit(
            "typed V2 page max_bytes is zero or exceeds its bound",
        ));
    }
    Ok(())
}

fn validate_coordinates(
    tables: &[TypedFacadeTableV2],
    document_count: u64,
    raw: bool,
) -> NativeResult<()> {
    for table in tables {
        validate_coordinate(table.coordinate, document_count, raw)?;
    }
    Ok(())
}

fn validate_coordinate(
    coordinate: TypedFacadeCoordinateV2,
    document_count: u64,
    raw: bool,
) -> NativeResult<()> {
    match (coordinate.scope, coordinate.document_ordinal) {
        (TypedFacadeScopeV2::Document, Some(ordinal)) if ordinal < document_count => {}
        (TypedFacadeScopeV2::Closure, None) => {}
        _ => {
            return Err(NativeError::protocol(
                "typed V2 facade coordinate has invalid scope or document ordinal",
            ));
        }
    }
    if raw
        && (coordinate.scope != TypedFacadeScopeV2::Document
            || !coordinate.collection.raw_for_document_owner())
    {
        return Err(NativeError::protocol(
            "typed V2 raw override has an ineligible coordinate",
        ));
    }
    if coordinate.collection != TypedFacadeCollectionV2::Signature
        && (coordinate.signature_kind != TypedFacadeSignatureKindV2::All
            || !coordinate.include_builtins)
    {
        return Err(NativeError::protocol(
            "typed V2 non-signature coordinate has signature selectors",
        ));
    }
    Ok(())
}

fn validate_table(
    arena: &NativeComponentArena,
    table: &TypedFacadeTableV2,
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    external_bytes: usize,
) -> NativeResult<TableValidationV2> {
    let expected = table.coordinate.collection.category();
    let mut previous: Option<Vec<u8>> = None;
    let mut validation = TableValidationV2::default();
    for identifier in &table.roots {
        cancellation.checkpoint()?;
        if arena.category(*identifier)? != expected
            || !table
                .coordinate
                .collection
                .accepts_root_tag(arena.tag(*identifier)?)
        {
            return Err(NativeError::protocol(
                "typed V2 root constructor is inconsistent with its collection",
            ));
        }
        let prior_bytes = previous.as_ref().map_or(0, Vec::capacity);
        let live_external = external_bytes
            .checked_add(prior_bytes)
            .ok_or_else(|| NativeError::limit("typed V2 ordering workspace overflow"))?;
        let measured = arena.encoded_len(
            *identifier,
            limits,
            cancellation.clone(),
            interrupt.clone(),
            live_external,
        )?;
        let canonical = arena.encode(
            *identifier,
            limits,
            cancellation.clone(),
            interrupt.clone(),
            live_external,
        )?;
        if canonical.len() != measured {
            return Err(NativeError::protocol(
                "typed V2 component length diverged during freeze",
            ));
        }
        if previous
            .as_ref()
            .is_some_and(|prior| prior.as_slice() >= canonical.as_slice())
        {
            return Err(NativeError::protocol(
                "typed V2 structural roots are not canonical ascending unique",
            ));
        }
        let canonical_capacity = u64::try_from(canonical.capacity())
            .map_err(|_| NativeError::limit("typed V2 canonical allocation exceeds u64"))?;
        let workspace_bytes = u64::try_from(prior_bytes)
            .ok()
            .and_then(|value| value.checked_add(canonical_capacity))
            .ok_or_else(|| NativeError::limit("typed V2 ordering workspace overflow"))?;
        check_temporary_limit(arena, external_bytes, workspace_bytes, limits)?;
        validation.maximum_row_bytes = validation.maximum_row_bytes.max(
            u64::try_from(canonical.len())
                .map_err(|_| NativeError::limit("typed V2 canonical row exceeds u64"))?,
        );
        validation.peak_workspace_bytes = validation.peak_workspace_bytes.max(workspace_bytes);
        previous = Some(canonical);
    }
    Ok(validation)
}

fn check_temporary_limit(
    arena: &NativeComponentArena,
    external_bytes: usize,
    workspace_bytes: u64,
    limits: &Limits,
) -> NativeResult<()> {
    let external_bytes = u64::try_from(external_bytes)
        .map_err(|_| NativeError::limit("typed V2 external allocation exceeds u64"))?;
    let live = arena
        .counters()
        .retained_bytes
        .checked_add(external_bytes)
        .and_then(|value| value.checked_add(workspace_bytes))
        .ok_or_else(|| NativeError::limit("typed V2 temporary memory overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| live > maximum)
    {
        return Err(NativeError::limit(
            "typed V2 temporary workspace exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

fn canonical_input_counters(
    arena: &NativeComponentArena,
    effective: &[TypedFacadeTableV2],
    raw: &[TypedFacadeTableV2],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    external_bytes: usize,
) -> NativeResult<(u64, u64)> {
    let mut rows = 0_u64;
    let mut bytes = 0_u64;
    for effective_table in effective.iter().filter(|table| {
        table.coordinate.scope == TypedFacadeScopeV2::Document
            && matches!(
                table.coordinate.collection,
                TypedFacadeCollectionV2::OntologyAnnotations
                    | TypedFacadeCollectionV2::Axioms
                    | TypedFacadeCollectionV2::Extensions
            )
    }) {
        let selected = raw
            .binary_search_by_key(&effective_table.coordinate, |table| table.coordinate)
            .ok()
            .and_then(|index| raw.get(index))
            .unwrap_or(effective_table);
        rows = checked_add(
            rows,
            u64::try_from(selected.roots.len())
                .map_err(|_| NativeError::limit("typed V2 canonical input rows exceed u64"))?,
        )?;
        for identifier in &selected.roots {
            bytes = checked_add(
                bytes,
                u64::try_from(arena.encoded_len(
                    *identifier,
                    limits,
                    cancellation.clone(),
                    interrupt.clone(),
                    external_bytes,
                )?)
                .map_err(|_| NativeError::limit("typed V2 canonical input bytes exceed u64"))?,
            )?;
        }
    }
    for raw_table in raw.iter().filter(|table| {
        table.coordinate.scope == TypedFacadeScopeV2::Document
            && matches!(
                table.coordinate.collection,
                TypedFacadeCollectionV2::OntologyAnnotations
                    | TypedFacadeCollectionV2::Axioms
                    | TypedFacadeCollectionV2::Extensions
            )
            && effective
                .binary_search_by_key(&table.coordinate, |item| item.coordinate)
                .is_err()
    }) {
        rows = checked_add(
            rows,
            u64::try_from(raw_table.roots.len())
                .map_err(|_| NativeError::limit("typed V2 canonical input rows exceed u64"))?,
        )?;
        for identifier in &raw_table.roots {
            bytes = checked_add(
                bytes,
                u64::try_from(arena.encoded_len(
                    *identifier,
                    limits,
                    cancellation.clone(),
                    interrupt.clone(),
                    external_bytes,
                )?)
                .map_err(|_| NativeError::limit("typed V2 canonical input bytes exceed u64"))?,
            )?;
        }
    }
    Ok((rows, bytes))
}

fn root_count(effective: &[TypedFacadeTableV2], raw: &[TypedFacadeTableV2]) -> NativeResult<u64> {
    effective.iter().chain(raw).try_fold(0_u64, |total, table| {
        checked_add(
            total,
            u64::try_from(table.roots.len())
                .map_err(|_| NativeError::limit("typed V2 root count exceeds u64"))?,
        )
    })
}

fn root_allocation_bytes(
    effective: &[TypedFacadeTableV2],
    raw: &[TypedFacadeTableV2],
) -> NativeResult<u64> {
    effective.iter().chain(raw).try_fold(0_u64, |total, table| {
        let bytes = table
            .roots
            .capacity()
            .checked_mul(size_of::<ComponentId>())
            .and_then(|value| u64::try_from(value).ok())
            .ok_or_else(|| NativeError::limit("typed V2 root allocation size overflow"))?;
        checked_add(total, bytes)
    })
}

fn metadata_allocation_bytes(effective_capacity: usize, raw_capacity: usize) -> NativeResult<u64> {
    let table_capacity = effective_capacity
        .checked_add(raw_capacity)
        .and_then(|count| count.checked_mul(size_of::<TypedFacadeTableV2>()))
        .ok_or_else(|| NativeError::limit("typed V2 table metadata size overflow"))?;
    size_of::<TypedFacadeStorageV2>()
        .checked_add(table_capacity)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| NativeError::limit("typed V2 owner metadata size overflow"))
}

fn check_retained_limit(
    arena: &NativeComponentArena,
    external: u64,
    index: u64,
    limits: &Limits,
) -> NativeResult<()> {
    let retained = arena
        .counters()
        .retained_bytes
        .checked_add(external)
        .and_then(|value| value.checked_add(index))
        .ok_or_else(|| NativeError::limit("typed V2 retained owner size overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| retained > maximum)
    {
        return Err(NativeError::limit(
            "typed V2 owner exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

fn reject_duplicate_coordinates(tables: &[TypedFacadeTableV2]) -> NativeResult<()> {
    if tables
        .windows(2)
        .any(|pair| pair[0].coordinate == pair[1].coordinate)
    {
        return Err(NativeError::protocol(
            "typed V2 facade tables contain duplicate coordinates",
        ));
    }
    Ok(())
}

fn checked_add(left: u64, right: u64) -> NativeResult<u64> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("typed V2 counter overflow"))
}

fn table_count(table: &TypedFacadeTableV2) -> NativeResult<u64> {
    u64::try_from(table.roots.len())
        .map_err(|_| NativeError::limit("typed V2 structural count exceeds u64"))
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;
    use crate::limits::{CONFIG_BYTES, CONFIG_MAGIC, CONFIG_SCHEMA};
    use crate::model::{NativeComponentBuilder, PendingComponentId};

    fn frame(value: &[u8]) -> Vec<u8> {
        let mut result = varint(value.len());
        result.extend_from_slice(value);
        result
    }

    fn varint(mut value: usize) -> Vec<u8> {
        let mut result = Vec::new();
        loop {
            let byte = (value & 0x7f) as u8;
            value >>= 7;
            result.push(if value == 0 { byte } else { byte | 0x80 });
            if value == 0 {
                return result;
            }
        }
    }

    fn iri(value: &str) -> Vec<u8> {
        let mut result = vec![1, 2];
        result.extend(frame(value.as_bytes()));
        result
    }

    fn entity(value: &str) -> Vec<u8> {
        let iri = iri(value);
        let mut result = vec![2, 5];
        result.extend(frame(b"class"));
        result.push(1);
        result.extend(frame(&iri));
        result
    }

    fn declaration(value: &str) -> Vec<u8> {
        let entity = entity(value);
        let mut result = vec![60, 1];
        result.extend(frame(&entity));
        result.extend([6, 0]);
        result
    }

    fn swrl_variable(value: &str) -> Vec<u8> {
        let iri = iri(value);
        let mut result = varint(140);
        result.push(1);
        result.extend(frame(&iri));
        result
    }

    fn frozen_axioms(rows: &[Vec<u8>]) -> (NativeComponentArena, Vec<ComponentId>, ComponentId) {
        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let entity_pending = builder
            .intern_canonical(&entity("urn:typed:entity"))
            .expect("entity");
        let pending: Vec<PendingComponentId> = rows
            .iter()
            .map(|row| builder.intern_canonical(row).expect("axiom"))
            .collect();
        let frozen = builder.freeze().expect("freeze");
        let roots = pending
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("root"))
            .collect();
        let entity_id = frozen.resolve(entity_pending).expect("entity root");
        (frozen.into_arena(), roots, entity_id)
    }

    fn table(coordinate: TypedFacadeCoordinateV2, roots: Vec<ComponentId>) -> TypedFacadeTableV2 {
        TypedFacadeTableV2::new(coordinate, roots)
    }

    fn owner(rows: &[Vec<u8>]) -> (TypedFacadeStorageV2, NativeComponentArena, Vec<ComponentId>) {
        let (arena, roots, _entity) = frozen_axioms(rows);
        let witness = arena.clone();
        let storage = TypedFacadeStorageV2::freeze(
            arena,
            vec![
                table(
                    TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0),
                    roots.clone(),
                ),
                table(
                    TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                    roots.clone(),
                ),
            ],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect("typed owner");
        (storage, witness, roots)
    }

    #[test]
    fn pages_and_contains_use_one_arena_without_a_retained_byte_row_store() {
        let rows = vec![
            declaration("urn:typed:a"),
            declaration("urn:typed:b"),
            declaration("urn:typed:c"),
        ];
        let (storage, witness, _roots) = owner(&rows);
        assert!(storage.arena().shares_storage_with(&witness));
        assert_eq!(storage.document_count(), 1);
        assert_eq!(storage.maximum_row_bytes(), rows[2].len() as u64);

        let coordinate = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let first = storage
            .page(
                TypedFacadePageRequestV2::new(coordinate, false, 0, 64, rows[0].len() as u64),
                Cancellation::with_duration(None),
                None,
            )
            .expect("first page");
        assert_eq!(first.total_count, 3);
        assert_eq!(first.next_cursor, Some(1));
        assert_eq!(first.rows, rows[..1]);
        assert_eq!(first.page_bytes, rows[0].len() as u64);
        let second = storage
            .page(
                TypedFacadePageRequestV2::new(coordinate, false, 1, 64, MAX_FACADE_PAGE_BYTES_V2),
                Cancellation::with_duration(None),
                None,
            )
            .expect("second page");
        assert_eq!(second.next_cursor, None);
        assert_eq!(second.rows, rows[1..]);
        assert!(storage
            .contains_axiom(
                coordinate,
                false,
                &rows[1],
                Cancellation::with_duration(None),
                None,
            )
            .expect("present"));
        assert!(!storage
            .contains_axiom(
                coordinate,
                false,
                &declaration("urn:typed:absent"),
                Cancellation::with_duration(None),
                None,
            )
            .expect("absent"));

        let observation = storage.observation_for_tests().expect("observation");
        assert_eq!(observation.arena_fields, 1);
        assert_eq!(observation.root_identifier_rows, 6);
        assert_eq!(observation.axiom_index_rows, 6);
        assert_eq!(observation.retained_canonical_byte_rows, 0);

        let counters = storage.counters().expect("counters");
        assert_eq!(counters.canonical_input_rows, 3);
        assert_eq!(
            counters.canonical_input_bytes,
            rows.iter().map(Vec::len).sum::<usize>() as u64
        );
        assert_eq!(counters.retained_document_tables, 1);
        assert_eq!(counters.retained_root_rows, 6);
        assert_eq!(
            counters.retained_component_bytes,
            witness.counters().retained_bytes
        );
        assert!(counters.retained_root_bytes > 0);
        assert!(counters.retained_index_bytes > 0);
        assert_eq!(
            counters.retained_owner_bytes,
            counters.retained_component_bytes
                + counters.retained_root_bytes
                + counters.retained_index_bytes
                + counters.retained_metadata_bytes
        );
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(counters.publication_structural_bytes_copied, 0);
        assert_eq!(counters.page_requests, 2);
        assert_eq!(counters.pages_returned, 2);
        assert_eq!(counters.rows_emitted, 3);
        assert_eq!(
            counters.payload_bytes_copied,
            rows.iter().map(Vec::len).sum::<usize>() as u64
        );
        assert_eq!(counters.contains_requests, 2);
        assert_eq!(counters.contains_hits, 1);
        assert!(counters.canonical_encode_requests >= 4);
    }

    #[test]
    fn sparse_raw_document_roots_have_owner_role_semantics() {
        let effective_rows = vec![declaration("urn:typed:a"), declaration("urn:typed:b")];
        let (arena, roots, _entity) = frozen_axioms(&effective_rows);
        let storage = TypedFacadeStorageV2::freeze(
            arena,
            vec![table(
                TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0),
                roots.clone(),
            )],
            vec![table(
                TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0),
                roots[..1].to_vec(),
            )],
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect("raw override");
        let coordinate = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let snapshot = storage
            .page(
                TypedFacadePageRequestV2::new(coordinate, false, 0, 64, MAX_FACADE_PAGE_BYTES_V2),
                Cancellation::with_duration(None),
                None,
            )
            .expect("effective page");
        let document = storage
            .page(
                TypedFacadePageRequestV2::new(coordinate, true, 0, 64, MAX_FACADE_PAGE_BYTES_V2),
                Cancellation::with_duration(None),
                None,
            )
            .expect("raw page");
        assert_eq!(snapshot.rows, effective_rows);
        assert_eq!(document.rows, effective_rows[..1]);
        assert!(!storage
            .contains_axiom(
                coordinate,
                true,
                &effective_rows[1],
                Cancellation::with_duration(None),
                None,
            )
            .expect("raw absence"));
    }

    #[test]
    fn category_order_duplicate_and_coordinate_mismatches_fail_closed() {
        let rows = vec![declaration("urn:typed:a"), declaration("urn:typed:b")];
        let (arena, roots, entity_id) = frozen_axioms(&rows);
        let witness = arena.clone();
        let wrong_category = TypedFacadeStorageV2::freeze(
            arena,
            vec![table(
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                vec![entity_id],
            )],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("wrong category");
        assert_eq!(wrong_category.code, "NATIVE_PROTOCOL");

        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let variable = builder
            .intern_canonical(&swrl_variable("urn:typed:variable"))
            .expect("SWRL variable");
        let frozen = builder.freeze().expect("freeze variable");
        let variable = frozen.resolve(variable).expect("variable root");
        let wrong_extension_root = TypedFacadeStorageV2::freeze(
            frozen.into_arena(),
            vec![table(
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Extensions),
                vec![variable],
            )],
            Vec::new(),
            1,
            limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("non-rule SWRL extension root");
        assert_eq!(wrong_extension_root.code, "NATIVE_PROTOCOL");

        let reversed = TypedFacadeStorageV2::freeze(
            witness.clone(),
            vec![table(
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                vec![roots[1], roots[0]],
            )],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("reversed roots");
        assert_eq!(reversed.code, "NATIVE_PROTOCOL");

        let duplicate = TypedFacadeStorageV2::freeze(
            witness.clone(),
            vec![table(
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                vec![roots[0], roots[0]],
            )],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("duplicate roots");
        assert_eq!(duplicate.code, "NATIVE_PROTOCOL");

        let duplicate_coordinate = TypedFacadeStorageV2::freeze(
            witness,
            vec![
                table(
                    TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                    roots.clone(),
                ),
                table(
                    TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                    roots,
                ),
            ],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("duplicate coordinate");
        assert_eq!(duplicate_coordinate.code, "NATIVE_PROTOCOL");
    }

    #[test]
    fn limits_and_deadlines_are_checked_during_freeze_and_reads() {
        let rows = vec![declaration("urn:typed:a"), declaration("urn:typed:b")];
        let (arena, roots, _entity) = frozen_axioms(&rows);
        let expired = Cancellation::with_duration(Some(Duration::ZERO));
        let deadline = TypedFacadeStorageV2::freeze(
            arena.clone(),
            vec![table(
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                roots.clone(),
            )],
            Vec::new(),
            1,
            Limits::default(),
            expired,
            None,
        )
        .expect_err("expired freeze");
        assert_eq!(deadline.code, "NATIVE_DEADLINE");

        let bounded = limits_with_index_rows(1);
        let index_limit = TypedFacadeStorageV2::freeze(
            arena.clone(),
            vec![table(
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                roots.clone(),
            )],
            Vec::new(),
            1,
            bounded,
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("root limit");
        assert_eq!(index_limit.code, "NATIVE_WIRE_LIMIT");

        let storage = TypedFacadeStorageV2::freeze(
            arena,
            vec![table(
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                roots,
            )],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect("owner");
        let coordinate = TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms);
        assert_eq!(
            storage
                .page(
                    TypedFacadePageRequestV2::new(coordinate, false, 0, 0, 1),
                    Cancellation::with_duration(None),
                    None,
                )
                .expect_err("zero rows")
                .code,
            "NATIVE_WIRE_LIMIT"
        );
        assert_eq!(
            storage
                .page(
                    TypedFacadePageRequestV2::new(coordinate, false, 0, 1, 1),
                    Cancellation::with_duration(Some(Duration::ZERO)),
                    None,
                )
                .expect_err("expired read")
                .code,
            "NATIVE_DEADLINE"
        );
    }

    #[test]
    fn tight_memory_limits_cover_freeze_and_page_allocation_capacities() {
        let rows = vec![declaration("urn:typed:a"), declaration("urn:typed:b")];
        let (arena, roots, _entity) = frozen_axioms(&rows);
        let coordinate = TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms);
        let baseline = TypedFacadeStorageV2::freeze(
            arena.clone(),
            vec![table(coordinate, roots.clone())],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
        )
        .expect("baseline owner");
        let counters = baseline.counters().expect("baseline counters");
        let baseline_owner_bytes = counters.retained_owner_bytes;
        let freeze_peak = counters
            .retained_owner_bytes
            .checked_add(baseline.maximum_row_bytes())
            .expect("freeze peak");
        drop(baseline);

        let caller_external_bytes = 4_096_usize;
        let external_baseline = TypedFacadeStorageV2::freeze_with_external(
            arena.clone(),
            vec![table(coordinate, roots.clone())],
            Vec::new(),
            1,
            Limits::default(),
            Cancellation::with_duration(None),
            None,
            caller_external_bytes,
        )
        .expect("external baseline");
        let external_peak = external_baseline
            .counters()
            .expect("external counters")
            .peak_freeze_live_bytes;
        assert_eq!(
            external_baseline
                .counters()
                .expect("external counters")
                .retained_owner_bytes,
            baseline_owner_bytes
        );
        drop(external_baseline);

        let external_error = TypedFacadeStorageV2::freeze_with_external(
            arena.clone(),
            vec![table(coordinate, roots.clone())],
            Vec::new(),
            1,
            limits_with_memory(external_peak - 1),
            Cancellation::with_duration(None),
            None,
            caller_external_bytes,
        )
        .expect_err("caller-owned live bytes must be inside the freeze envelope");
        assert_eq!(external_error.code, "NATIVE_WIRE_LIMIT");

        let external_owner = TypedFacadeStorageV2::freeze_with_external(
            arena.clone(),
            vec![table(coordinate, roots.clone())],
            Vec::new(),
            1,
            limits_with_memory(external_peak),
            Cancellation::with_duration(None),
            None,
            caller_external_bytes,
        )
        .expect("exact caller-owned freeze peak");
        let external_counters = external_owner.counters().expect("external counters");
        assert_eq!(external_counters.retained_owner_bytes, baseline_owner_bytes);
        assert_eq!(external_counters.peak_freeze_live_bytes, external_peak);
        drop(external_owner);

        let freeze_error = TypedFacadeStorageV2::freeze(
            arena.clone(),
            vec![table(coordinate, roots.clone())],
            Vec::new(),
            1,
            limits_with_memory(freeze_peak - 1),
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("tight freeze must include its canonical workspace");
        assert_eq!(freeze_error.code, "NATIVE_WIRE_LIMIT");

        let storage = TypedFacadeStorageV2::freeze(
            arena,
            vec![table(coordinate, roots)],
            Vec::new(),
            1,
            limits_with_memory(freeze_peak),
            Cancellation::with_duration(None),
            None,
        )
        .expect("exact freeze peak");
        let page_error = storage
            .page(
                TypedFacadePageRequestV2::new(
                    coordinate,
                    false,
                    0,
                    MAX_FACADE_PAGE_ROWS_V2,
                    MAX_FACADE_PAGE_BYTES_V2,
                ),
                Cancellation::with_duration(None),
                None,
            )
            .expect_err("page Vec capacities exceed the freeze-only peak");
        assert_eq!(page_error.code, "NATIVE_WIRE_LIMIT");
    }

    #[test]
    fn ordering_validation_accounts_previous_and_current_vec_capacities() {
        let rows = vec![declaration("urn:typed:a"), declaration("urn:typed:b")];
        let (arena, roots, _entity) = frozen_axioms(&rows);
        let first_capacity = arena
            .encode(
                roots[0],
                &Limits::default(),
                Cancellation::with_duration(None),
                None,
                0,
            )
            .expect("first canonical")
            .capacity();
        let second_capacity = arena
            .encode(
                roots[1],
                &Limits::default(),
                Cancellation::with_duration(None),
                None,
                0,
            )
            .expect("second canonical")
            .capacity();
        let workspace_bytes = first_capacity
            .checked_add(second_capacity)
            .and_then(|value| u64::try_from(value).ok())
            .expect("ordering workspace");
        let exact_peak = arena
            .counters()
            .retained_bytes
            .checked_add(workspace_bytes)
            .expect("ordering peak");
        let selected = table(
            TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
            roots,
        );

        let error = validate_table(
            &arena,
            &selected,
            &limits_with_memory(exact_peak - 1),
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect_err("ordering workspace must retain the previous row");
        assert_eq!(error.code, "NATIVE_WIRE_LIMIT");

        let validation = validate_table(
            &arena,
            &selected,
            &limits_with_memory(exact_peak),
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("exact ordering workspace");
        assert_eq!(validation.peak_workspace_bytes, workspace_bytes);
    }

    fn limits_with_index_rows(maximum: u64) -> Limits {
        limits_with_overrides(Some(maximum), None)
    }

    fn limits_with_memory(maximum: u64) -> Limits {
        limits_with_overrides(None, Some(maximum))
    }

    fn limits_with_overrides(
        maximum_index_rows: Option<u64>,
        maximum_memory_bytes: Option<u64>,
    ) -> Limits {
        let mut encoded = vec![0_u8; CONFIG_BYTES];
        encoded[..8].copy_from_slice(CONFIG_MAGIC);
        encoded[8..10].copy_from_slice(&CONFIG_SCHEMA.to_le_bytes());
        for index in 0..37 {
            let value = if matches!(index, 13 | 14) {
                0
            } else {
                1_000_000_000_u64
            };
            encoded[16 + index * 8..24 + index * 8].copy_from_slice(&value.to_le_bytes());
        }
        if let Some(maximum) = maximum_index_rows {
            let offset = 16 + LimitKey::MaxIndexRows as usize * 8;
            encoded[offset..offset + 8].copy_from_slice(&maximum.to_le_bytes());
        }
        if let Some(maximum) = maximum_memory_bytes {
            let offset = 16 + LimitKey::MaxMemoryBytes as usize * 8;
            encoded[offset..offset + 8].copy_from_slice(&maximum.to_le_bytes());
        }
        Limits::decode(&encoded).expect("test limits")
    }
}
