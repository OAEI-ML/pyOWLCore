//! Direct encoded-structural-columns-v1 construction over retained components.
//!
//! The pre-advertisement builder traverses retained component rows directly,
//! assigns canonical public node IDs from lazily compared canonical streams,
//! and produces the exact eleven schema buffers without reconstructing complete
//! root bytes.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::mem::size_of;

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};

use super::{ComponentFieldRef, ComponentId, ComponentSequenceRef, NativeComponentArena};

pub(crate) const ENCODED_STRUCTURAL_SCHEMA_NAME_V1: &str = "pyowl-core/structural-columns";
pub(crate) const ENCODED_STRUCTURAL_SCHEMA_VERSION_V1: u32 = 1;
pub(crate) const ENCODED_STRUCTURAL_MODEL_SCHEMA_V1: u32 = 1;

const ENCODED_BUFFER_NAMES_V1: [&str; 11] = [
    "root_kinds",
    "root_ids",
    "node_tags",
    "node_field_offsets",
    "field_kinds",
    "field_values",
    "field_lengths",
    "item_kinds",
    "item_values",
    "item_lengths",
    "scalar_bytes",
];

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
#[repr(u8)]
pub(crate) enum EncodedRootKindV1 {
    OntologyAnnotation = 1,
    Axiom = 2,
    Extension = 3,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct EncodedRootV1 {
    kind: EncodedRootKindV1,
    component: ComponentId,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct EncodedRootTableV1<'arena> {
    kind: EncodedRootKindV1,
    components: &'arena [ComponentId],
}

impl<'arena> EncodedRootTableV1<'arena> {
    pub(crate) const fn new(kind: EncodedRootKindV1, components: &'arena [ComponentId]) -> Self {
        Self { kind, components }
    }
}

impl EncodedRootV1 {
    pub(crate) const fn new(kind: EncodedRootKindV1, component: ComponentId) -> Self {
        Self { kind, component }
    }

    pub(crate) const fn kind(self) -> EncodedRootKindV1 {
        self.kind
    }

    pub(crate) const fn component(self) -> ComponentId {
        self.component
    }
}

#[derive(Debug, Default, Eq, PartialEq)]
pub(crate) struct EncodedStructuralBufferSetV1<B> {
    root_kinds: B,
    root_ids: B,
    node_tags: B,
    node_field_offsets: B,
    field_kinds: B,
    field_values: B,
    field_lengths: B,
    item_kinds: B,
    item_values: B,
    item_lengths: B,
    scalar_bytes: B,
}

pub(crate) type EncodedStructuralBuffersV1 = EncodedStructuralBufferSetV1<Vec<u8>>;

impl EncodedStructuralBufferSetV1<Vec<u8>> {
    pub(crate) fn named(&self) -> [(&'static str, &[u8]); 11] {
        [
            (ENCODED_BUFFER_NAMES_V1[0], &self.root_kinds),
            (ENCODED_BUFFER_NAMES_V1[1], &self.root_ids),
            (ENCODED_BUFFER_NAMES_V1[2], &self.node_tags),
            (ENCODED_BUFFER_NAMES_V1[3], &self.node_field_offsets),
            (ENCODED_BUFFER_NAMES_V1[4], &self.field_kinds),
            (ENCODED_BUFFER_NAMES_V1[5], &self.field_values),
            (ENCODED_BUFFER_NAMES_V1[6], &self.field_lengths),
            (ENCODED_BUFFER_NAMES_V1[7], &self.item_kinds),
            (ENCODED_BUFFER_NAMES_V1[8], &self.item_values),
            (ENCODED_BUFFER_NAMES_V1[9], &self.item_lengths),
            (ENCODED_BUFFER_NAMES_V1[10], &self.scalar_bytes),
        ]
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct EncodedStructuralBufferLayoutV1 {
    offsets: [usize; 12],
}

impl EncodedStructuralBufferLayoutV1 {
    pub(crate) const fn total_bytes(self) -> usize {
        self.offsets[11]
    }

    pub(crate) fn named_ranges(self) -> [(&'static str, usize, usize); 11] {
        std::array::from_fn(|index| {
            (
                ENCODED_BUFFER_NAMES_V1[index],
                self.offsets[index],
                self.offsets[index + 1] - self.offsets[index],
            )
        })
    }

    fn lengths(self) -> [usize; 11] {
        std::array::from_fn(|index| self.offsets[index + 1] - self.offsets[index])
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct EncodedColumnCountersV1 {
    pub(crate) root_rows: u64,
    pub(crate) node_rows: u64,
    pub(crate) field_rows: u64,
    pub(crate) item_rows: u64,
    pub(crate) scalar_bytes: u64,
    pub(crate) retained_buffer_bytes: u64,
    pub(crate) retained_metadata_bytes: u64,
    pub(crate) peak_owned_bytes: u64,
    pub(crate) peak_workspace_bytes: u64,
    pub(crate) scalar_copy_bytes: u64,
    pub(crate) canonical_work: u64,
    pub(crate) canonical_comparison_bytes: u64,
    pub(crate) complete_root_encode_calls: u64,
}

#[derive(Debug)]
struct ColumnWork {
    guard: Guard,
    used: u64,
    maximum: u64,
    #[cfg(feature = "test-hooks")]
    allocation_probe: Option<ColumnAllocationProbe>,
}

#[cfg(feature = "test-hooks")]
#[derive(Debug)]
struct ColumnAllocationProbe {
    fail_after: Option<u64>,
    allocations: u64,
}

#[derive(Clone, Copy, Debug)]
struct ColumnLayout {
    root_rows: usize,
    node_rows: usize,
    field_rows: usize,
    item_rows: usize,
    buffer_bytes: usize,
    metadata_bytes: usize,
    workspace_bytes: usize,
}

#[derive(Clone, Copy, Debug)]
struct NodeRow {
    component: ComponentId,
    tag: u16,
}

#[derive(Debug, Default)]
struct DiscoveryState {
    seen: HashSet<ComponentId>,
    nodes: Vec<NodeRow>,
    stack: Vec<ComponentId>,
    caller_external_bytes: usize,
}

#[derive(Clone, Copy, Debug, Default)]
struct ColumnCounts {
    fields: usize,
    items: usize,
    scalar_bytes: usize,
    strings: u64,
    annotations: u64,
    rule_atoms: u64,
}

impl ColumnWork {
    fn new(
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<Self> {
        let mut guard = match interrupt {
            Some(slot) => Guard::with_interrupt(
                cancellation,
                limits.deadline,
                limits.cancellation_stride,
                slot,
            ),
            None => Guard::new(cancellation, limits.deadline, limits.cancellation_stride),
        };
        guard.check(0, true)?;
        Ok(Self {
            guard,
            used: 0,
            maximum: limits.max_canonical_work,
            #[cfg(feature = "test-hooks")]
            allocation_probe: None,
        })
    }

    #[cfg(feature = "test-hooks")]
    fn with_allocation_failure(
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        fail_after: Option<u64>,
    ) -> NativeResult<Self> {
        let mut work = Self::new(limits, cancellation, interrupt)?;
        work.allocation_probe = Some(ColumnAllocationProbe {
            fail_after,
            allocations: 0,
        });
        Ok(work)
    }

    fn allocation_checkpoint(&mut self) -> NativeResult<()> {
        #[cfg(feature = "test-hooks")]
        {
            let Some(probe) = self.allocation_probe.as_mut() else {
                return Ok(());
            };
            if probe
                .fail_after
                .is_some_and(|maximum| probe.allocations >= maximum)
            {
                return Err(NativeError::limit(
                    "injected native encoded-column workspace allocation failure",
                ));
            }
            probe.allocations = probe.allocations.checked_add(1).ok_or_else(|| {
                NativeError::limit("native encoded-column allocation counter overflow")
            })?;
        }
        Ok(())
    }

    fn allocation_checkpoint_for_growth(
        &mut self,
        len: usize,
        capacity: usize,
        additional: usize,
    ) -> NativeResult<()> {
        #[cfg(feature = "test-hooks")]
        {
            let required = len.checked_add(additional).ok_or_else(|| {
                NativeError::limit("native encoded-column allocation length overflow")
            })?;
            if required > capacity {
                self.allocation_checkpoint()?;
            }
        }
        #[cfg(not(feature = "test-hooks"))]
        let _ = (len, capacity, additional);
        Ok(())
    }

    #[cfg(feature = "test-hooks")]
    fn allocation_count(&self) -> u64 {
        self.allocation_probe
            .as_ref()
            .map_or(0, |probe| probe.allocations)
    }

    fn consume(&mut self, amount: usize) -> NativeResult<()> {
        let amount = u64::try_from(amount)
            .map_err(|_| NativeError::limit("native encoded-column work exceeds u64"))?;
        self.used = self
            .used
            .checked_add(amount)
            .ok_or_else(|| NativeError::limit("native encoded-column work counter overflow"))?;
        if self.used > self.maximum {
            return Err(NativeError::limit(
                "native encoded-column build exceeds max_canonical_work",
            ));
        }
        self.guard.check(self.used, false)
    }

    fn finish(&mut self) -> NativeResult<()> {
        self.guard.check(self.used, true)
    }
}

#[derive(Debug)]
pub(crate) struct EncodedStructuralColumnsV1 {
    owner: NativeComponentArena,
    buffers: EncodedStructuralBuffersV1,
    counters: EncodedColumnCountersV1,
}

impl EncodedStructuralColumnsV1 {
    pub(crate) const fn owner(&self) -> &NativeComponentArena {
        &self.owner
    }

    pub(crate) const fn buffers(&self) -> &EncodedStructuralBuffersV1 {
        &self.buffers
    }

    pub(crate) const fn counters(&self) -> &EncodedColumnCountersV1 {
        &self.counters
    }
}

pub(crate) struct PreparedEncodedStructuralColumnsV1<'arena> {
    arena: &'arena NativeComponentArena,
    roots: RetainedRootSourceV1<'arena>,
    root_rows: usize,
    nodes: Vec<NodeRow>,
    node_ids: HashMap<ComponentId, u32>,
    layout: EncodedStructuralBufferLayoutV1,
    counters: EncodedColumnCountersV1,
    #[cfg(feature = "test-hooks")]
    workspace_allocations: u64,
}

impl PreparedEncodedStructuralColumnsV1<'_> {
    pub(crate) const fn layout(&self) -> EncodedStructuralBufferLayoutV1 {
        self.layout
    }

    pub(crate) const fn counters(&self) -> &EncodedColumnCountersV1 {
        &self.counters
    }

    #[cfg(feature = "test-hooks")]
    pub(crate) const fn workspace_allocation_count(&self) -> u64 {
        self.workspace_allocations
    }

    pub(crate) fn write_into(&self, output: &mut [u8]) -> NativeResult<()> {
        let mut buffers = fixed_buffers(output, self.layout)?;
        fill_buffers(
            self.arena,
            &self.roots,
            self.root_rows,
            &self.nodes,
            &self.node_ids,
            &mut buffers,
        )?;
        if !fixed_buffers_full(&buffers)
            || total_buffer_bytes(&buffers)? != self.layout.total_bytes()
        {
            return Err(NativeError::protocol(
                "native encoded-column fixed layout accounting drifted",
            ));
        }
        Ok(())
    }

    pub(crate) fn into_columns(self) -> NativeResult<EncodedStructuralColumnsV1> {
        let mut buffers = allocate_buffers_from_layout(self.layout)?;
        fill_buffers(
            self.arena,
            &self.roots,
            self.root_rows,
            &self.nodes,
            &self.node_ids,
            &mut buffers,
        )?;
        if total_buffer_bytes(&buffers)? != self.layout.total_bytes() {
            return Err(NativeError::protocol(
                "native encoded-column layout accounting drifted",
            ));
        }
        Ok(EncodedStructuralColumnsV1 {
            owner: self.arena.clone(),
            buffers,
            counters: self.counters,
        })
    }
}

pub(crate) fn build_encoded_structural_columns_v1(
    arena: &NativeComponentArena,
    roots: &[EncodedRootV1],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
) -> NativeResult<EncodedStructuralColumnsV1> {
    prepare_encoded_structural_columns_from_source_v1(
        arena,
        RetainedRootSourceV1::Slice(RootSliceV1(roots)),
        limits,
        cancellation,
        interrupt,
        0,
    )?
    .into_columns()
}

pub(crate) fn build_encoded_structural_columns_from_tables_v1(
    arena: &NativeComponentArena,
    tables: &[EncodedRootTableV1<'_>],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<EncodedStructuralColumnsV1> {
    prepare_encoded_structural_columns_from_tables_v1(
        arena,
        tables,
        limits,
        cancellation,
        interrupt,
        caller_external_bytes,
    )?
    .into_columns()
}

pub(crate) fn prepare_encoded_structural_columns_from_tables_v1<'arena>(
    arena: &'arena NativeComponentArena,
    tables: &[EncodedRootTableV1<'arena>],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<PreparedEncodedStructuralColumnsV1<'arena>> {
    let work = ColumnWork::new(limits, cancellation, interrupt)?;
    prepare_encoded_structural_columns_from_tables_with_work_v1(
        arena,
        tables,
        limits,
        caller_external_bytes,
        work,
    )
}

#[cfg(feature = "test-hooks")]
pub(crate) fn prepare_encoded_structural_columns_from_tables_with_allocation_probe_v1<'arena>(
    arena: &'arena NativeComponentArena,
    tables: &[EncodedRootTableV1<'arena>],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
    fail_after: Option<u64>,
) -> NativeResult<PreparedEncodedStructuralColumnsV1<'arena>> {
    let work = ColumnWork::with_allocation_failure(limits, cancellation, interrupt, fail_after)?;
    prepare_encoded_structural_columns_from_tables_with_work_v1(
        arena,
        tables,
        limits,
        caller_external_bytes,
        work,
    )
}

fn prepare_encoded_structural_columns_from_tables_with_work_v1<'arena>(
    arena: &'arena NativeComponentArena,
    tables: &[EncodedRootTableV1<'arena>],
    limits: &Limits,
    caller_external_bytes: usize,
    mut work: ColumnWork,
) -> NativeResult<PreparedEncodedStructuralColumnsV1<'arena>> {
    let roots = RootTablesV1::new(tables, &mut work)?;
    prepare_encoded_structural_columns_from_source_with_work_v1(
        arena,
        RetainedRootSourceV1::Tables(roots),
        limits,
        caller_external_bytes,
        work,
    )
}

trait EncodedRootSourceV1: Send + Sync {
    fn len(&self) -> NativeResult<usize>;
    fn get(&self, index: usize) -> NativeResult<EncodedRootV1>;
    fn workspace_bytes(&self) -> NativeResult<usize>;
}

/// Closed root-source storage keeps preparation fully fallible without an
/// unaccounted trait-object control allocation.
enum RetainedRootSourceV1<'arena> {
    Slice(RootSliceV1<'arena>),
    Tables(RootTablesV1<'arena>),
}

impl EncodedRootSourceV1 for RetainedRootSourceV1<'_> {
    fn len(&self) -> NativeResult<usize> {
        match self {
            Self::Slice(source) => source.len(),
            Self::Tables(source) => source.len(),
        }
    }

    fn get(&self, index: usize) -> NativeResult<EncodedRootV1> {
        match self {
            Self::Slice(source) => source.get(index),
            Self::Tables(source) => source.get(index),
        }
    }

    fn workspace_bytes(&self) -> NativeResult<usize> {
        match self {
            Self::Slice(source) => source.workspace_bytes(),
            Self::Tables(source) => source.workspace_bytes(),
        }
    }
}

struct RootSliceV1<'roots>(&'roots [EncodedRootV1]);

impl EncodedRootSourceV1 for RootSliceV1<'_> {
    fn len(&self) -> NativeResult<usize> {
        Ok(self.0.len())
    }

    fn get(&self, index: usize) -> NativeResult<EncodedRootV1> {
        self.0
            .get(index)
            .copied()
            .ok_or_else(|| NativeError::protocol("native encoded-column root is out of bounds"))
    }

    fn workspace_bytes(&self) -> NativeResult<usize> {
        Ok(0)
    }
}

struct RootTablesV1<'arena>(Vec<EncodedRootTableV1<'arena>>);

impl<'arena> RootTablesV1<'arena> {
    fn new(tables: &[EncodedRootTableV1<'arena>], work: &mut ColumnWork) -> NativeResult<Self> {
        let mut owned = Vec::new();
        work.allocation_checkpoint_for_growth(owned.len(), owned.capacity(), tables.len())?;
        owned
            .try_reserve_exact(tables.len())
            .map_err(|_| NativeError::limit("native encoded-column table allocation failed"))?;
        owned.extend_from_slice(tables);
        Ok(Self(owned))
    }
}

impl EncodedRootSourceV1 for RootTablesV1<'_> {
    fn len(&self) -> NativeResult<usize> {
        self.0.iter().try_fold(0_usize, |total, table| {
            total
                .checked_add(table.components.len())
                .ok_or_else(|| NativeError::limit("native encoded-column root count overflow"))
        })
    }

    fn get(&self, mut index: usize) -> NativeResult<EncodedRootV1> {
        for table in &self.0 {
            if index < table.components.len() {
                return Ok(EncodedRootV1::new(table.kind, table.components[index]));
            }
            index = index.checked_sub(table.components.len()).ok_or_else(|| {
                NativeError::protocol("native encoded-column root table is inconsistent")
            })?;
        }
        Err(NativeError::protocol(
            "native encoded-column root is out of bounds",
        ))
    }

    fn workspace_bytes(&self) -> NativeResult<usize> {
        self.0
            .capacity()
            .checked_mul(size_of::<EncodedRootTableV1<'_>>())
            .ok_or_else(|| NativeError::limit("native encoded-column table size overflow"))
    }
}

fn prepare_encoded_structural_columns_from_source_v1<'arena>(
    arena: &'arena NativeComponentArena,
    roots: RetainedRootSourceV1<'arena>,
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<PreparedEncodedStructuralColumnsV1<'arena>> {
    let work = ColumnWork::new(limits, cancellation, interrupt)?;
    prepare_encoded_structural_columns_from_source_with_work_v1(
        arena,
        roots,
        limits,
        caller_external_bytes,
        work,
    )
}

fn prepare_encoded_structural_columns_from_source_with_work_v1<'arena>(
    arena: &'arena NativeComponentArena,
    roots: RetainedRootSourceV1<'arena>,
    limits: &Limits,
    caller_external_bytes: usize,
    mut work: ColumnWork,
) -> NativeResult<PreparedEncodedStructuralColumnsV1<'arena>> {
    let root_source_workspace = roots.workspace_bytes()?;
    let discovery_external_bytes = caller_external_bytes
        .checked_add(root_source_workspace)
        .ok_or_else(|| NativeError::limit("native encoded-column caller memory overflow"))?;
    let root_rows = roots.len()?;
    let root_count = u64::try_from(root_rows)
        .map_err(|_| NativeError::limit("native encoded-column root count exceeds u64"))?;
    if root_count > limits.value(LimitKey::MaxIndexRows) {
        return Err(NativeError::limit(
            "native encoded-column roots exceed max_index_rows",
        ));
    }
    let mut axiom_count = 0_usize;
    for index in 0..root_rows {
        if roots.get(index)?.kind == EncodedRootKindV1::Axiom {
            axiom_count = axiom_count
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native encoded-column axiom count overflow"))?;
        }
    }
    if u64::try_from(axiom_count).map_or(true, |count| count > limits.max_axioms) {
        return Err(NativeError::limit(
            "native encoded-column roots exceed max_axioms",
        ));
    }

    let mut discovery = DiscoveryState {
        caller_external_bytes: discovery_external_bytes,
        ..DiscoveryState::default()
    };
    for index in 0..root_rows {
        let root = roots.get(index)?;
        work.consume(1)?;
        let row = node_row(arena, root.component)?;
        if !root_accepts(root.kind, row.tag) {
            return Err(NativeError::protocol(
                "native encoded-column root kind does not match its constructor",
            ));
        }
        discover_node(arena, row, &mut discovery, limits, &mut work)?;
    }

    while let Some(identifier) = discovery.stack.pop() {
        let record = arena.record(identifier)?;
        for field_index in 0..record.field_count() {
            work.consume(1)?;
            discover_field_nodes(
                arena,
                record.field(field_index)?,
                &mut discovery,
                limits,
                &mut work,
            )?;
        }
    }
    if discovery.nodes.len() > u32::MAX as usize {
        return Err(NativeError::limit(
            "native encoded-column node ID space is exhausted",
        ));
    }
    let mut workspace_bytes = root_source_workspace
        .checked_add(discovery_workspace_bytes(
            &discovery.seen,
            &discovery.nodes,
            &discovery.stack,
        )?)
        .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))?;
    let mut nodes = std::mem::take(&mut discovery.nodes);
    drop(discovery);

    let mut lengths = HashMap::new();
    work.allocation_checkpoint_for_growth(lengths.len(), lengths.capacity(), nodes.len())?;
    lengths
        .try_reserve(nodes.len())
        .map_err(|_| NativeError::limit("native encoded-column length map allocation failed"))?;
    let mut visiting = HashSet::new();
    for row in &nodes {
        canonical_node_len(arena, row.component, &mut lengths, &mut visiting, &mut work)?;
    }
    workspace_bytes = workspace_bytes.max(
        root_source_workspace
            .checked_add(length_workspace_bytes(&lengths, &visiting, &nodes)?)
            .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))?,
    );
    check_workspace_memory(arena, workspace_bytes, caller_external_bytes, limits)?;
    drop(visiting);
    check_workspace_memory(
        arena,
        root_source_workspace
            .checked_add(canonical_sort_workspace_estimate(
                nodes.len(),
                lengths.capacity(),
                limits.max_nesting_depth,
            )?)
            .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))?,
        caller_external_bytes,
        limits,
    )?;
    let mut comparison_bytes = 0_u64;
    let (ordered, sort_workspace) = canonical_node_order(
        arena,
        &nodes,
        &lengths,
        limits,
        &mut work,
        &mut comparison_bytes,
    )?;
    workspace_bytes = workspace_bytes.max(
        root_source_workspace
            .checked_add(sort_workspace)
            .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))?,
    );
    check_workspace_memory(arena, workspace_bytes, caller_external_bytes, limits)?;
    nodes = ordered;
    validate_root_order(
        arena,
        &roots,
        root_rows,
        &lengths,
        limits,
        &mut work,
        &mut comparison_bytes,
    )?;
    let (deduplicated, node_ids, deduplication_workspace) = deduplicate_canonical_nodes(
        arena,
        nodes,
        &lengths,
        limits,
        &mut work,
        &mut comparison_bytes,
    )?;
    nodes = deduplicated;
    workspace_bytes = workspace_bytes.max(
        root_source_workspace
            .checked_add(deduplication_workspace)
            .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))?,
    );
    check_workspace_memory(arena, workspace_bytes, caller_external_bytes, limits)?;
    drop(lengths);
    workspace_bytes = workspace_bytes.max(
        root_source_workspace
            .checked_add(id_workspace_bytes(&node_ids, &nodes)?)
            .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))?,
    );
    check_workspace_memory(arena, workspace_bytes, caller_external_bytes, limits)?;

    let counts = measure_columns(arena, &nodes, limits, &mut work)?;
    if counts.strings > limits.max_strings
        || counts.annotations > limits.max_annotations
        || counts.rule_atoms > limits.max_rule_atoms
    {
        return Err(NativeError::limit(
            "native encoded-column graph exceeds structural limits",
        ));
    }
    let layout = encoded_buffer_layout(
        root_rows,
        nodes.len(),
        counts.fields,
        counts.items,
        counts.scalar_bytes,
    )?;
    let buffer_bytes = layout.total_bytes();
    let metadata_bytes = 0;
    check_layout_limits(
        arena,
        limits,
        caller_external_bytes,
        ColumnLayout {
            root_rows,
            node_rows: nodes.len(),
            field_rows: counts.fields,
            item_rows: counts.items,
            buffer_bytes,
            metadata_bytes,
            workspace_bytes,
        },
    )?;
    work.consume(buffer_bytes)?;
    work.finish()?;

    let retained_buffer_bytes = u64::try_from(buffer_bytes)
        .map_err(|_| NativeError::limit("native encoded-column buffers exceed u64"))?;
    let retained_metadata_bytes = u64::try_from(metadata_bytes)
        .map_err(|_| NativeError::limit("native encoded-column metadata exceeds u64"))?;
    let peak_workspace_bytes = u64::try_from(workspace_bytes)
        .map_err(|_| NativeError::limit("native encoded-column workspace exceeds u64"))?;
    let peak_owned_bytes = arena
        .counters()
        .retained_bytes
        .checked_add(retained_buffer_bytes)
        .and_then(|value| value.checked_add(retained_metadata_bytes))
        .ok_or_else(|| NativeError::limit("native encoded-column memory overflow"))?;
    let node_rows = u64::try_from(nodes.len())
        .map_err(|_| NativeError::limit("native encoded-column node count exceeds u64"))?;
    let field_rows = u64::try_from(counts.fields)
        .map_err(|_| NativeError::limit("native encoded-column field count exceeds u64"))?;
    let item_rows = u64::try_from(counts.items)
        .map_err(|_| NativeError::limit("native encoded-column item count exceeds u64"))?;
    let scalar_bytes = u64::try_from(counts.scalar_bytes)
        .map_err(|_| NativeError::limit("native encoded-column scalars exceed u64"))?;
    #[cfg(feature = "test-hooks")]
    let workspace_allocations = work.allocation_count();
    Ok(PreparedEncodedStructuralColumnsV1 {
        arena,
        roots,
        root_rows,
        nodes,
        node_ids,
        layout,
        counters: EncodedColumnCountersV1 {
            root_rows: root_count,
            node_rows,
            field_rows,
            item_rows,
            scalar_bytes,
            retained_buffer_bytes,
            retained_metadata_bytes,
            peak_owned_bytes,
            peak_workspace_bytes,
            scalar_copy_bytes: scalar_bytes,
            canonical_work: work.used,
            canonical_comparison_bytes: comparison_bytes,
            complete_root_encode_calls: 0,
        },
        #[cfg(feature = "test-hooks")]
        workspace_allocations,
    })
}

fn fill_buffers<B: ByteOutputV1>(
    arena: &NativeComponentArena,
    roots: &(impl EncodedRootSourceV1 + ?Sized),
    root_rows: usize,
    nodes: &[NodeRow],
    node_ids: &HashMap<ComponentId, u32>,
    buffers: &mut EncodedStructuralBufferSetV1<B>,
) -> NativeResult<()> {
    for index in 0..root_rows {
        let root = roots.get(index)?;
        buffers.root_kinds.push(root.kind as u8)?;
        append_u32(
            &mut buffers.root_ids,
            public_node_id(node_ids, root.component)?,
        )?;
    }
    append_u64(&mut buffers.node_field_offsets, 0)?;
    for row in nodes {
        let record = arena.record(row.component)?;
        append_u16(&mut buffers.node_tags, row.tag)?;
        for field_index in 0..record.field_count() {
            append_field(record.field(field_index)?, node_ids, buffers)?;
        }
        append_u64(
            &mut buffers.node_field_offsets,
            u64::try_from(buffers.field_kinds.len()).map_err(|_| {
                NativeError::limit("native encoded-column field offset exceeds u64")
            })?,
        )?;
    }
    Ok(())
}

fn node_row(arena: &NativeComponentArena, component: ComponentId) -> NativeResult<NodeRow> {
    Ok(NodeRow {
        component,
        tag: arena.tag(component)?,
    })
}

fn root_accepts(kind: EncodedRootKindV1, tag: u16) -> bool {
    match kind {
        EncodedRootKindV1::OntologyAnnotation => tag == 5,
        EncodedRootKindV1::Axiom => matches!(
            tag,
            60..=64 | 70..=82 | 90..=95 | 100..=101 | 110..=116 | 120..=123
        ),
        EncodedRootKindV1::Extension => tag == 148,
    }
}

fn discover_node(
    arena: &NativeComponentArena,
    row: NodeRow,
    state: &mut DiscoveryState,
    limits: &Limits,
    work: &mut ColumnWork,
) -> NativeResult<()> {
    let record = arena.record(row.component)?;
    if record.height().saturating_sub(1) > limits.max_nesting_depth {
        return Err(NativeError::limit(
            "native encoded-column graph exceeds max_nesting_depth",
        ));
    }
    if state.seen.contains(&row.component) {
        return Ok(());
    }
    let next = state
        .nodes
        .len()
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("native encoded-column node count overflow"))?;
    if u64::try_from(next).map_or(true, |count| count > limits.max_terms) {
        return Err(NativeError::limit(
            "native encoded-column nodes exceed max_terms",
        ));
    }
    check_discovery_memory(arena, next, state.caller_external_bytes, limits)?;
    work.allocation_checkpoint_for_growth(state.seen.len(), state.seen.capacity(), 1)?;
    state
        .seen
        .try_reserve(1)
        .map_err(|_| NativeError::limit("native encoded-column visited allocation failed"))?;
    work.allocation_checkpoint_for_growth(state.nodes.len(), state.nodes.capacity(), 1)?;
    state
        .nodes
        .try_reserve(1)
        .map_err(|_| NativeError::limit("native encoded-column node allocation failed"))?;
    work.allocation_checkpoint_for_growth(state.stack.len(), state.stack.capacity(), 1)?;
    state
        .stack
        .try_reserve(1)
        .map_err(|_| NativeError::limit("native encoded-column stack allocation failed"))?;
    state.seen.insert(row.component);
    state.nodes.push(row);
    state.stack.push(row.component);
    work.consume(1)
}

fn discover_field_nodes(
    arena: &NativeComponentArena,
    field: ComponentFieldRef<'_>,
    state: &mut DiscoveryState,
    limits: &Limits,
    work: &mut ColumnWork,
) -> NativeResult<()> {
    match field {
        ComponentFieldRef::Node(component) => {
            discover_node(arena, node_row(arena, component)?, state, limits, work)
        }
        ComponentFieldRef::CanonicalSet(sequence)
        | ComponentFieldRef::OrderedSequence(sequence) => {
            if u64::try_from(sequence.len())
                .map_or(true, |length| length > limits.max_sequence_arity)
            {
                return Err(NativeError::limit(
                    "native encoded-column sequence exceeds max_sequence_arity",
                ));
            }
            for index in 0..sequence.len() {
                work.consume(1)?;
                match sequence.item(index)? {
                    ComponentFieldRef::Node(component) => {
                        discover_node(arena, node_row(arena, component)?, state, limits, work)?
                    }
                    ComponentFieldRef::CanonicalSet(_) | ComponentFieldRef::OrderedSequence(_) => {
                        return Err(NativeError::protocol(
                            "native encoded-column nested collection item is unsupported",
                        ));
                    }
                    ComponentFieldRef::None
                    | ComponentFieldRef::Text(_)
                    | ComponentFieldRef::Bytes(_)
                    | ComponentFieldRef::NonnegativeIntegerVarint(_)
                    | ComponentFieldRef::Enum(_) => {}
                }
            }
            Ok(())
        }
        ComponentFieldRef::None
        | ComponentFieldRef::Text(_)
        | ComponentFieldRef::Bytes(_)
        | ComponentFieldRef::NonnegativeIntegerVarint(_)
        | ComponentFieldRef::Enum(_) => Ok(()),
    }
}

fn check_discovery_memory(
    arena: &NativeComponentArena,
    node_count: usize,
    caller_external_bytes: usize,
    limits: &Limits,
) -> NativeResult<()> {
    let bytes_per_node = size_of::<NodeRow>()
        .checked_add(size_of::<ComponentId>().saturating_mul(2))
        .and_then(|value| value.checked_add(size_of::<usize>().saturating_mul(2)))
        .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))?;
    let workspace = node_count
        .checked_mul(bytes_per_node)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| NativeError::limit("native encoded-column workspace exceeds u64"))?;
    let peak = arena
        .counters()
        .retained_bytes
        .checked_add(u64::try_from(caller_external_bytes).map_err(|_| {
            NativeError::limit("native encoded-column caller allocation exceeds u64")
        })?)
        .and_then(|value| value.checked_add(workspace))
        .ok_or_else(|| NativeError::limit("native encoded-column workspace overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| peak > maximum)
    {
        return Err(NativeError::limit(
            "native encoded-column discovery exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

fn discovery_workspace_bytes(
    seen: &HashSet<ComponentId>,
    nodes: &Vec<NodeRow>,
    stack: &Vec<ComponentId>,
) -> NativeResult<usize> {
    seen.capacity()
        .checked_mul(size_of::<ComponentId>() + size_of::<usize>())
        .and_then(|value| {
            nodes
                .capacity()
                .checked_mul(size_of::<NodeRow>())
                .and_then(|nodes| value.checked_add(nodes))
        })
        .and_then(|value| {
            stack
                .capacity()
                .checked_mul(size_of::<ComponentId>())
                .and_then(|stack| value.checked_add(stack))
        })
        .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))
}

fn length_workspace_bytes(
    lengths: &HashMap<ComponentId, usize>,
    visiting: &HashSet<ComponentId>,
    nodes: &Vec<NodeRow>,
) -> NativeResult<usize> {
    map_workspace_bytes::<usize>(lengths.capacity())?
        .checked_add(
            visiting
                .capacity()
                .checked_mul(size_of::<ComponentId>() + size_of::<usize>())
                .ok_or_else(|| NativeError::limit("native visit workspace size overflow"))?,
        )
        .and_then(|value| {
            nodes
                .capacity()
                .checked_mul(size_of::<NodeRow>())
                .and_then(|nodes| value.checked_add(nodes))
        })
        .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))
}

fn check_workspace_memory(
    arena: &NativeComponentArena,
    workspace_bytes: usize,
    caller_external_bytes: usize,
    limits: &Limits,
) -> NativeResult<()> {
    let workspace_bytes = u64::try_from(workspace_bytes)
        .map_err(|_| NativeError::limit("native encoded-column workspace exceeds u64"))?;
    let peak = arena
        .counters()
        .retained_bytes
        .checked_add(u64::try_from(caller_external_bytes).map_err(|_| {
            NativeError::limit("native encoded-column caller allocation exceeds u64")
        })?)
        .and_then(|value| value.checked_add(workspace_bytes))
        .ok_or_else(|| NativeError::limit("native encoded-column workspace overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| peak > maximum)
    {
        return Err(NativeError::limit(
            "native encoded-column workspace exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

fn canonical_sort_workspace_estimate(
    nodes: usize,
    length_capacity: usize,
    max_depth: u32,
) -> NativeResult<usize> {
    let map = map_workspace_bytes::<usize>(length_capacity)?;
    if nodes < 2 {
        return nodes
            .checked_mul(size_of::<NodeRow>())
            .and_then(|value| value.checked_mul(2))
            .and_then(|value| value.checked_add(map))
            .ok_or_else(|| NativeError::limit("native canonical sort workspace overflow"));
    }
    let rows = nodes
        .checked_mul(size_of::<NodeRow>())
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| NativeError::limit("native canonical row workspace overflow"))?;
    let indices = nodes
        .checked_mul(size_of::<usize>())
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| NativeError::limit("native canonical index workspace overflow"))?;
    let cursor_slots = usize::try_from(max_depth)
        .map_err(|_| NativeError::limit("native cursor depth exceeds usize"))?
        .checked_add(2)
        .and_then(|value| value.checked_mul(16))
        .ok_or_else(|| NativeError::limit("native cursor workspace overflow"))?;
    let cursors = cursor_slots
        .checked_mul(size_of::<EmitTask<'_>>())
        .ok_or_else(|| NativeError::limit("native cursor workspace overflow"))?;
    rows.checked_add(indices)
        .and_then(|value| value.checked_add(cursors))
        .and_then(|value| value.checked_add(map))
        .ok_or_else(|| NativeError::limit("native canonical sort workspace overflow"))
}

fn id_workspace_bytes(
    identifiers: &HashMap<ComponentId, u32>,
    nodes: &Vec<NodeRow>,
) -> NativeResult<usize> {
    map_workspace_bytes::<u32>(identifiers.capacity())?
        .checked_add(
            nodes
                .capacity()
                .checked_mul(size_of::<NodeRow>())
                .ok_or_else(|| NativeError::limit("native node workspace size overflow"))?,
        )
        .ok_or_else(|| NativeError::limit("native encoded-column workspace size overflow"))
}

fn map_workspace_bytes<T>(capacity: usize) -> NativeResult<usize> {
    capacity
        .checked_mul(
            size_of::<ComponentId>()
                .checked_add(size_of::<T>())
                .and_then(|value| value.checked_add(size_of::<usize>()))
                .ok_or_else(|| NativeError::limit("native map slot size overflow"))?,
        )
        .ok_or_else(|| NativeError::limit("native map workspace size overflow"))
}

fn canonical_node_len(
    arena: &NativeComponentArena,
    component: ComponentId,
    memo: &mut HashMap<ComponentId, usize>,
    visiting: &mut HashSet<ComponentId>,
    work: &mut ColumnWork,
) -> NativeResult<usize> {
    if let Some(length) = memo.get(&component).copied() {
        return Ok(length);
    }
    work.allocation_checkpoint_for_growth(visiting.len(), visiting.capacity(), 1)?;
    visiting
        .try_reserve(1)
        .map_err(|_| NativeError::limit("native canonical-length stack allocation failed"))?;
    if !visiting.insert(component) {
        return Err(NativeError::protocol(
            "native encoded-column component graph is cyclic",
        ));
    }
    let record = arena.record(component)?;
    let mut length = varint_width(usize::from(record.tag()));
    for field_index in 0..record.field_count() {
        work.consume(1)?;
        length = length
            .checked_add(canonical_field_len(
                arena,
                record.field(field_index)?,
                memo,
                visiting,
                work,
            )?)
            .ok_or_else(|| NativeError::limit("native canonical node length overflow"))?;
    }
    if !visiting.remove(&component) {
        return Err(NativeError::protocol(
            "native canonical-length stack is inconsistent",
        ));
    }
    if memo.insert(component, length).is_some() {
        return Err(NativeError::protocol(
            "native canonical-length memo assigned a node twice",
        ));
    }
    Ok(length)
}

fn canonical_field_len(
    arena: &NativeComponentArena,
    field: ComponentFieldRef<'_>,
    memo: &mut HashMap<ComponentId, usize>,
    visiting: &mut HashSet<ComponentId>,
    work: &mut ColumnWork,
) -> NativeResult<usize> {
    match field {
        ComponentFieldRef::CanonicalSet(sequence) => {
            let mut length = 1_usize
                .checked_add(varint_width(sequence.len()))
                .ok_or_else(|| NativeError::limit("native canonical set length overflow"))?;
            for index in 0..sequence.len() {
                work.consume(1)?;
                let ComponentFieldRef::Node(component) = sequence.item(index)? else {
                    return Err(NativeError::protocol(
                        "native canonical set contains a scalar",
                    ));
                };
                let child = canonical_node_len(arena, component, memo, visiting, work)?;
                length = length
                    .checked_add(varint_width(child))
                    .and_then(|value| value.checked_add(child))
                    .ok_or_else(|| NativeError::limit("native canonical set length overflow"))?;
            }
            Ok(length)
        }
        ComponentFieldRef::OrderedSequence(sequence) => {
            let mut length = 1_usize
                .checked_add(varint_width(sequence.len()))
                .ok_or_else(|| NativeError::limit("native canonical sequence length overflow"))?;
            for index in 0..sequence.len() {
                work.consume(1)?;
                length = length
                    .checked_add(canonical_leaf_len(
                        arena,
                        sequence.item(index)?,
                        memo,
                        visiting,
                        work,
                    )?)
                    .ok_or_else(|| {
                        NativeError::limit("native canonical sequence length overflow")
                    })?;
            }
            Ok(length)
        }
        leaf => canonical_leaf_len(arena, leaf, memo, visiting, work),
    }
}

fn canonical_leaf_len(
    arena: &NativeComponentArena,
    field: ComponentFieldRef<'_>,
    memo: &mut HashMap<ComponentId, usize>,
    visiting: &mut HashSet<ComponentId>,
    work: &mut ColumnWork,
) -> NativeResult<usize> {
    match field {
        ComponentFieldRef::None => Ok(1),
        ComponentFieldRef::Node(component) => {
            let child = canonical_node_len(arena, component, memo, visiting, work)?;
            1_usize
                .checked_add(varint_width(child))
                .and_then(|value| value.checked_add(child))
                .ok_or_else(|| NativeError::limit("native canonical child length overflow"))
        }
        ComponentFieldRef::Text(value)
        | ComponentFieldRef::Bytes(value)
        | ComponentFieldRef::Enum(value) => 1_usize
            .checked_add(varint_width(value.len()))
            .and_then(|length| length.checked_add(value.len()))
            .ok_or_else(|| NativeError::limit("native canonical scalar length overflow")),
        ComponentFieldRef::NonnegativeIntegerVarint(value) => 1_usize
            .checked_add(value.len())
            .ok_or_else(|| NativeError::limit("native canonical integer length overflow")),
        ComponentFieldRef::CanonicalSet(_) | ComponentFieldRef::OrderedSequence(_) => Err(
            NativeError::protocol("native canonical sequence contains a nested collection"),
        ),
    }
}

fn varint_width(mut value: usize) -> usize {
    let mut width = 1;
    while value >= 0x80 {
        value >>= 7;
        width += 1;
    }
    width
}

#[derive(Clone, Copy, Debug)]
enum EmitTask<'arena> {
    Byte(u8),
    Varint(usize),
    Slice(&'arena [u8], usize),
    Node(ComponentId),
    Field(ComponentFieldRef<'arena>),
    Sequence {
        value: ComponentSequenceRef<'arena>,
        index: usize,
        canonical_set: bool,
    },
}

struct CanonicalCursor<'arena, 'lengths> {
    arena: &'arena NativeComponentArena,
    lengths: &'lengths HashMap<ComponentId, usize>,
    stack: Vec<EmitTask<'arena>>,
}

impl<'arena, 'lengths> CanonicalCursor<'arena, 'lengths> {
    fn new(
        arena: &'arena NativeComponentArena,
        lengths: &'lengths HashMap<ComponentId, usize>,
        max_depth: u32,
        work: &mut ColumnWork,
    ) -> NativeResult<Self> {
        let capacity = usize::try_from(max_depth)
            .map_err(|_| NativeError::limit("native cursor depth exceeds usize"))?
            .checked_add(2)
            .and_then(|value| value.checked_mul(8))
            .ok_or_else(|| NativeError::limit("native cursor stack size overflow"))?;
        let mut stack = Vec::new();
        work.allocation_checkpoint_for_growth(stack.len(), stack.capacity(), capacity)?;
        stack
            .try_reserve_exact(capacity)
            .map_err(|_| NativeError::limit("native canonical cursor allocation failed"))?;
        Ok(Self {
            arena,
            lengths,
            stack,
        })
    }

    fn reset(&mut self, component: ComponentId, work: &mut ColumnWork) -> NativeResult<()> {
        self.stack.clear();
        self.push(EmitTask::Node(component), work)
    }

    fn allocated_bytes(&self) -> NativeResult<usize> {
        self.stack
            .capacity()
            .checked_mul(size_of::<EmitTask<'_>>())
            .ok_or_else(|| NativeError::limit("native cursor workspace size overflow"))
    }

    fn push(&mut self, task: EmitTask<'arena>, work: &mut ColumnWork) -> NativeResult<()> {
        work.allocation_checkpoint_for_growth(self.stack.len(), self.stack.capacity(), 1)?;
        self.stack
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native canonical cursor growth failed"))?;
        self.stack.push(task);
        Ok(())
    }

    fn next_byte(&mut self, work: &mut ColumnWork) -> NativeResult<Option<u8>> {
        loop {
            let Some(task) = self.stack.pop() else {
                return Ok(None);
            };
            match task {
                EmitTask::Byte(byte) => return Ok(Some(byte)),
                EmitTask::Varint(value) => {
                    let following = value >> 7;
                    if following != 0 {
                        self.push(EmitTask::Varint(following), work)?;
                    }
                    return Ok(Some(
                        (value as u8 & 0x7f) | if following == 0 { 0 } else { 0x80 },
                    ));
                }
                EmitTask::Slice(value, index) => {
                    let byte = value.get(index).copied().ok_or_else(|| {
                        NativeError::protocol("native canonical cursor slice is out of bounds")
                    })?;
                    if index + 1 < value.len() {
                        self.push(EmitTask::Slice(value, index + 1), work)?;
                    }
                    return Ok(Some(byte));
                }
                EmitTask::Node(component) => {
                    let record = self.arena.record(component)?;
                    for index in (0..record.field_count()).rev() {
                        self.push(EmitTask::Field(record.field(index)?), work)?;
                    }
                    self.push(EmitTask::Varint(usize::from(record.tag())), work)?;
                }
                EmitTask::Field(field) => self.schedule_field(field, work)?,
                EmitTask::Sequence {
                    value,
                    index,
                    canonical_set,
                } => {
                    if index >= value.len() {
                        continue;
                    }
                    self.push(
                        EmitTask::Sequence {
                            value,
                            index: index + 1,
                            canonical_set,
                        },
                        work,
                    )?;
                    let item = value.item(index)?;
                    if canonical_set {
                        let ComponentFieldRef::Node(component) = item else {
                            return Err(NativeError::protocol(
                                "native canonical set contains a scalar",
                            ));
                        };
                        self.schedule_node_frame(component, false, work)?;
                    } else {
                        self.schedule_leaf(item, work)?;
                    }
                }
            }
        }
    }

    fn schedule_field(
        &mut self,
        field: ComponentFieldRef<'arena>,
        work: &mut ColumnWork,
    ) -> NativeResult<()> {
        match field {
            ComponentFieldRef::CanonicalSet(sequence) => {
                self.push(
                    EmitTask::Sequence {
                        value: sequence,
                        index: 0,
                        canonical_set: true,
                    },
                    work,
                )?;
                self.push(EmitTask::Varint(sequence.len()), work)?;
                self.push(EmitTask::Byte(6), work)
            }
            ComponentFieldRef::OrderedSequence(sequence) => {
                self.push(
                    EmitTask::Sequence {
                        value: sequence,
                        index: 0,
                        canonical_set: false,
                    },
                    work,
                )?;
                self.push(EmitTask::Varint(sequence.len()), work)?;
                self.push(EmitTask::Byte(7), work)
            }
            leaf => self.schedule_leaf(leaf, work),
        }
    }

    fn schedule_leaf(
        &mut self,
        field: ComponentFieldRef<'arena>,
        work: &mut ColumnWork,
    ) -> NativeResult<()> {
        match field {
            ComponentFieldRef::None => self.push(EmitTask::Byte(0), work),
            ComponentFieldRef::Node(component) => self.schedule_node_frame(component, true, work),
            ComponentFieldRef::Text(value) => self.schedule_framed_scalar(2, value, work),
            ComponentFieldRef::Bytes(value) => self.schedule_framed_scalar(3, value, work),
            ComponentFieldRef::NonnegativeIntegerVarint(value) => {
                if !value.is_empty() {
                    self.push(EmitTask::Slice(value, 0), work)?;
                }
                self.push(EmitTask::Byte(4), work)
            }
            ComponentFieldRef::Enum(value) => self.schedule_framed_scalar(5, value, work),
            ComponentFieldRef::CanonicalSet(_) | ComponentFieldRef::OrderedSequence(_) => Err(
                NativeError::protocol("native canonical sequence contains a nested collection"),
            ),
        }
    }

    fn schedule_node_frame(
        &mut self,
        component: ComponentId,
        include_marker: bool,
        work: &mut ColumnWork,
    ) -> NativeResult<()> {
        let length = self.lengths.get(&component).copied().ok_or_else(|| {
            NativeError::protocol("native canonical cursor child length is unavailable")
        })?;
        self.push(EmitTask::Node(component), work)?;
        self.push(EmitTask::Varint(length), work)?;
        if include_marker {
            self.push(EmitTask::Byte(1), work)?;
        }
        Ok(())
    }

    fn schedule_framed_scalar(
        &mut self,
        marker: u8,
        value: &'arena [u8],
        work: &mut ColumnWork,
    ) -> NativeResult<()> {
        if !value.is_empty() {
            self.push(EmitTask::Slice(value, 0), work)?;
        }
        self.push(EmitTask::Varint(value.len()), work)?;
        self.push(EmitTask::Byte(marker), work)
    }
}

fn compare_canonical_nodes(
    left: ComponentId,
    right: ComponentId,
    left_cursor: &mut CanonicalCursor<'_, '_>,
    right_cursor: &mut CanonicalCursor<'_, '_>,
    work: &mut ColumnWork,
    comparison_bytes: &mut u64,
) -> NativeResult<Ordering> {
    left_cursor.reset(left, work)?;
    right_cursor.reset(right, work)?;
    loop {
        let left = left_cursor.next_byte(work)?;
        let right = right_cursor.next_byte(work)?;
        match (left, right) {
            (Some(left), Some(right)) => {
                *comparison_bytes = comparison_bytes.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native canonical comparison counter overflow")
                })?;
                work.consume(1)?;
                let ordering = left.cmp(&right);
                if ordering != Ordering::Equal {
                    return Ok(ordering);
                }
            }
            (None, None) => return Ok(Ordering::Equal),
            (None, Some(_)) => return Ok(Ordering::Less),
            (Some(_), None) => return Ok(Ordering::Greater),
        }
    }
}

fn canonical_node_order(
    arena: &NativeComponentArena,
    nodes: &[NodeRow],
    lengths: &HashMap<ComponentId, usize>,
    limits: &Limits,
    work: &mut ColumnWork,
    comparison_bytes: &mut u64,
) -> NativeResult<(Vec<NodeRow>, usize)> {
    if nodes.len() < 2 {
        let mut ordered = Vec::new();
        work.allocation_checkpoint_for_growth(ordered.len(), ordered.capacity(), nodes.len())?;
        ordered
            .try_reserve_exact(nodes.len())
            .map_err(|_| NativeError::limit("native canonical output allocation failed"))?;
        ordered.extend_from_slice(nodes);
        let map = map_workspace_bytes::<usize>(lengths.capacity())?;
        let workspace = nodes
            .len()
            .checked_mul(size_of::<NodeRow>())
            .and_then(|value| value.checked_mul(2))
            .and_then(|value| value.checked_add(map))
            .ok_or_else(|| NativeError::limit("native canonical sort workspace overflow"))?;
        return Ok((ordered, workspace));
    }
    let mut order = Vec::new();
    work.allocation_checkpoint_for_growth(order.len(), order.capacity(), nodes.len())?;
    order
        .try_reserve_exact(nodes.len())
        .map_err(|_| NativeError::limit("native canonical order allocation failed"))?;
    order.extend(0..nodes.len());
    let mut scratch = Vec::new();
    work.allocation_checkpoint_for_growth(scratch.len(), scratch.capacity(), nodes.len())?;
    scratch
        .try_reserve_exact(nodes.len())
        .map_err(|_| NativeError::limit("native canonical sort allocation failed"))?;
    scratch.resize(nodes.len(), 0);
    let mut left_cursor = CanonicalCursor::new(arena, lengths, limits.max_nesting_depth, work)?;
    let mut right_cursor = CanonicalCursor::new(arena, lengths, limits.max_nesting_depth, work)?;
    let mut width = 1_usize;
    while width < nodes.len() {
        let step = width
            .checked_mul(2)
            .ok_or_else(|| NativeError::limit("native canonical sort width overflow"))?;
        let mut start = 0_usize;
        while start < nodes.len() {
            let middle = start.saturating_add(width).min(nodes.len());
            let end = start.saturating_add(step).min(nodes.len());
            let (mut left, mut right, mut output) = (start, middle, start);
            while left < middle && right < end {
                let ordering = compare_canonical_nodes(
                    nodes[order[left]].component,
                    nodes[order[right]].component,
                    &mut left_cursor,
                    &mut right_cursor,
                    work,
                    comparison_bytes,
                )?;
                if ordering != Ordering::Greater {
                    scratch[output] = order[left];
                    left += 1;
                } else {
                    scratch[output] = order[right];
                    right += 1;
                }
                output += 1;
                work.consume(1)?;
            }
            while left < middle {
                scratch[output] = order[left];
                left += 1;
                output += 1;
                work.consume(1)?;
            }
            while right < end {
                scratch[output] = order[right];
                right += 1;
                output += 1;
                work.consume(1)?;
            }
            start = end;
        }
        std::mem::swap(&mut order, &mut scratch);
        width = step;
    }
    for pair in order.windows(2) {
        if compare_canonical_nodes(
            nodes[pair[0]].component,
            nodes[pair[1]].component,
            &mut left_cursor,
            &mut right_cursor,
            work,
            comparison_bytes,
        )? == Ordering::Greater
        {
            return Err(NativeError::protocol(
                "native encoded-column canonical node order is inconsistent",
            ));
        }
    }
    let mut ordered = Vec::new();
    work.allocation_checkpoint_for_growth(ordered.len(), ordered.capacity(), nodes.len())?;
    ordered
        .try_reserve_exact(nodes.len())
        .map_err(|_| NativeError::limit("native canonical output allocation failed"))?;
    ordered.extend(order.iter().map(|index| nodes[*index]));
    let row_bytes = nodes
        .len()
        .checked_mul(size_of::<NodeRow>())
        .ok_or_else(|| NativeError::limit("native canonical row workspace overflow"))?;
    let index_bytes = order
        .capacity()
        .checked_add(scratch.capacity())
        .and_then(|count| count.checked_mul(size_of::<usize>()))
        .ok_or_else(|| NativeError::limit("native canonical index workspace overflow"))?;
    let output_bytes = ordered
        .capacity()
        .checked_mul(size_of::<NodeRow>())
        .ok_or_else(|| NativeError::limit("native canonical output workspace overflow"))?;
    let cursor_bytes = left_cursor
        .allocated_bytes()?
        .checked_add(right_cursor.allocated_bytes()?)
        .ok_or_else(|| NativeError::limit("native cursor workspace overflow"))?;
    let map_bytes = map_workspace_bytes::<usize>(lengths.capacity())?;
    let workspace = row_bytes
        .checked_add(index_bytes)
        .and_then(|value| value.checked_add(output_bytes))
        .and_then(|value| value.checked_add(cursor_bytes))
        .and_then(|value| value.checked_add(map_bytes))
        .ok_or_else(|| NativeError::limit("native canonical sort workspace overflow"))?;
    Ok((ordered, workspace))
}

fn deduplicate_canonical_nodes(
    arena: &NativeComponentArena,
    mut nodes: Vec<NodeRow>,
    lengths: &HashMap<ComponentId, usize>,
    limits: &Limits,
    work: &mut ColumnWork,
    comparison_bytes: &mut u64,
) -> NativeResult<(Vec<NodeRow>, HashMap<ComponentId, u32>, usize)> {
    let source_capacity = nodes.capacity();
    let mut node_ids = HashMap::new();
    work.allocation_checkpoint_for_growth(node_ids.len(), node_ids.capacity(), nodes.len())?;
    node_ids
        .try_reserve(nodes.len())
        .map_err(|_| NativeError::limit("native encoded-column ID map allocation failed"))?;
    let mut left_cursor = CanonicalCursor::new(arena, lengths, limits.max_nesting_depth, work)?;
    let mut right_cursor = CanonicalCursor::new(arena, lengths, limits.max_nesting_depth, work)?;
    let mut retained = 0_usize;
    for read in 0..nodes.len() {
        let row = nodes[read];
        let distinct = retained == 0
            || compare_canonical_nodes(
                nodes[retained - 1].component,
                row.component,
                &mut left_cursor,
                &mut right_cursor,
                work,
                comparison_bytes,
            )? != Ordering::Equal;
        if distinct {
            nodes[retained] = row;
            retained = retained
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native encoded-column node count overflow"))?;
        }
        let identifier = u32::try_from(retained)
            .map_err(|_| NativeError::limit("native encoded-column node ID exceeds u32"))?;
        if node_ids.insert(row.component, identifier).is_some() {
            return Err(NativeError::protocol(
                "native encoded-column arena contains duplicate node IDs",
            ));
        }
    }
    nodes.truncate(retained);
    let row_bytes = source_capacity
        .checked_mul(size_of::<NodeRow>())
        .ok_or_else(|| NativeError::limit("native canonical deduplication workspace overflow"))?;
    let cursor_bytes = left_cursor
        .allocated_bytes()?
        .checked_add(right_cursor.allocated_bytes()?)
        .ok_or_else(|| NativeError::limit("native cursor workspace overflow"))?;
    let node_id_bytes = map_workspace_bytes::<u32>(node_ids.capacity())?;
    let length_bytes = map_workspace_bytes::<usize>(lengths.capacity())?;
    let workspace = row_bytes
        .checked_add(node_id_bytes)
        .and_then(|value| value.checked_add(length_bytes))
        .and_then(|value| value.checked_add(cursor_bytes))
        .ok_or_else(|| NativeError::limit("native canonical deduplication workspace overflow"))?;
    Ok((nodes, node_ids, workspace))
}

fn validate_root_order(
    arena: &NativeComponentArena,
    roots: &(impl EncodedRootSourceV1 + ?Sized),
    root_rows: usize,
    lengths: &HashMap<ComponentId, usize>,
    limits: &Limits,
    work: &mut ColumnWork,
    comparison_bytes: &mut u64,
) -> NativeResult<()> {
    if root_rows < 2 {
        return Ok(());
    }
    let mut left_cursor = CanonicalCursor::new(arena, lengths, limits.max_nesting_depth, work)?;
    let mut right_cursor = CanonicalCursor::new(arena, lengths, limits.max_nesting_depth, work)?;
    let mut prior = roots.get(0)?;
    for index in 1..root_rows {
        let current = roots.get(index)?;
        let ordering = prior.kind.cmp(&current.kind);
        if ordering == Ordering::Greater
            || (ordering == Ordering::Equal
                && compare_canonical_nodes(
                    prior.component,
                    current.component,
                    &mut left_cursor,
                    &mut right_cursor,
                    work,
                    comparison_bytes,
                )? != Ordering::Less)
        {
            return Err(NativeError::protocol(
                "native encoded-column roots are not canonical and unique",
            ));
        }
        prior = current;
    }
    Ok(())
}

fn measure_columns(
    arena: &NativeComponentArena,
    nodes: &[NodeRow],
    limits: &Limits,
    work: &mut ColumnWork,
) -> NativeResult<ColumnCounts> {
    let mut counts = ColumnCounts::default();
    for row in nodes {
        if row.tag == 5 {
            counts.annotations = counts
                .annotations
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native annotation count overflow"))?;
        }
        if (141..=147).contains(&row.tag) {
            counts.rule_atoms = counts
                .rule_atoms
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native rule atom count overflow"))?;
        }
        let record = arena.record(row.component)?;
        counts.fields = counts
            .fields
            .checked_add(record.field_count())
            .ok_or_else(|| NativeError::limit("native encoded-column field count overflow"))?;
        for field_index in 0..record.field_count() {
            work.consume(1)?;
            measure_field(record.field(field_index)?, &mut counts, limits, work)?;
        }
    }
    Ok(counts)
}

fn measure_field(
    field: ComponentFieldRef<'_>,
    counts: &mut ColumnCounts,
    limits: &Limits,
    work: &mut ColumnWork,
) -> NativeResult<()> {
    match field {
        ComponentFieldRef::CanonicalSet(sequence) => {
            measure_sequence(sequence.len(), counts, limits)?;
            for index in 0..sequence.len() {
                work.consume(1)?;
                let item = sequence.item(index)?;
                if !matches!(item, ComponentFieldRef::Node(_)) {
                    return Err(NativeError::protocol(
                        "native encoded-column canonical sets must contain nodes",
                    ));
                }
            }
            Ok(())
        }
        ComponentFieldRef::OrderedSequence(sequence) => {
            measure_sequence(sequence.len(), counts, limits)?;
            for index in 0..sequence.len() {
                work.consume(1)?;
                measure_leaf(sequence.item(index)?, counts, work)?;
            }
            Ok(())
        }
        leaf => measure_leaf(leaf, counts, work),
    }
}

fn measure_sequence(length: usize, counts: &mut ColumnCounts, limits: &Limits) -> NativeResult<()> {
    if u64::try_from(length).map_or(true, |value| value > limits.max_sequence_arity) {
        return Err(NativeError::limit(
            "native encoded-column sequence exceeds max_sequence_arity",
        ));
    }
    counts.items = counts
        .items
        .checked_add(length)
        .ok_or_else(|| NativeError::limit("native encoded-column item count overflow"))?;
    Ok(())
}

fn measure_leaf(
    field: ComponentFieldRef<'_>,
    counts: &mut ColumnCounts,
    work: &mut ColumnWork,
) -> NativeResult<()> {
    let length =
        match field {
            ComponentFieldRef::None | ComponentFieldRef::Node(_) => return Ok(()),
            ComponentFieldRef::Text(value) => {
                counts.strings = counts.strings.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native encoded-column string count overflow")
                })?;
                std::str::from_utf8(value)
                    .map_err(|_| NativeError::protocol("native text scalar is not UTF-8"))?;
                value.len()
            }
            ComponentFieldRef::Bytes(value) => value.len(),
            ComponentFieldRef::NonnegativeIntegerVarint(value) => integer_magnitude_len(value)?,
            ComponentFieldRef::Enum(value) => {
                counts.strings = counts.strings.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native encoded-column string count overflow")
                })?;
                if value.is_empty() || !value.is_ascii() {
                    return Err(NativeError::protocol(
                        "native enum scalar must be nonempty ASCII",
                    ));
                }
                value.len()
            }
            ComponentFieldRef::CanonicalSet(_) | ComponentFieldRef::OrderedSequence(_) => {
                return Err(NativeError::protocol(
                    "native encoded-column nested collection item is unsupported",
                ));
            }
        };
    counts.scalar_bytes = counts
        .scalar_bytes
        .checked_add(length)
        .ok_or_else(|| NativeError::limit("native encoded-column scalar size overflow"))?;
    work.consume(length.saturating_add(1))
}

fn integer_magnitude_len(varint: &[u8]) -> NativeResult<usize> {
    if varint.is_empty() {
        return Err(NativeError::protocol(
            "native integer varint must be nonempty",
        ));
    }
    for (index, byte) in varint.iter().copied().enumerate() {
        let last = index + 1 == varint.len();
        if last == (byte & 0x80 != 0) {
            return Err(NativeError::protocol(
                "native integer varint continuation is invalid",
            ));
        }
    }
    let final_payload = varint[varint.len() - 1] & 0x7f;
    if varint.len() > 1 && final_payload == 0 {
        return Err(NativeError::protocol(
            "native integer varint is not minimal",
        ));
    }
    let significant = (varint.len() - 1)
        .checked_mul(7)
        .and_then(|value| value.checked_add((u8::BITS - final_payload.leading_zeros()) as usize))
        .ok_or_else(|| NativeError::limit("native integer magnitude size overflow"))?;
    Ok(significant.div_ceil(8).max(1))
}

fn encoded_buffer_layout(
    roots: usize,
    nodes: usize,
    fields: usize,
    items: usize,
    scalars: usize,
) -> NativeResult<EncodedStructuralBufferLayoutV1> {
    let lengths = [
        roots,
        checked_width(roots, 4)?,
        checked_width(nodes, 2)?,
        checked_width(
            nodes
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native node offset count overflow"))?,
            8,
        )?,
        fields,
        checked_width(fields, 8)?,
        checked_width(fields, 8)?,
        items,
        checked_width(items, 8)?,
        checked_width(items, 8)?,
        scalars,
    ];
    let mut offsets = [0_usize; 12];
    for (index, length) in lengths.into_iter().enumerate() {
        offsets[index + 1] = offsets[index]
            .checked_add(length)
            .ok_or_else(|| NativeError::limit("native encoded-column buffer size overflow"))?;
    }
    Ok(EncodedStructuralBufferLayoutV1 { offsets })
}

fn allocate_buffers_from_layout(
    layout: EncodedStructuralBufferLayoutV1,
) -> NativeResult<EncodedStructuralBuffersV1> {
    let lengths = layout.lengths();
    Ok(EncodedStructuralBuffersV1 {
        root_kinds: bytes_with_capacity(lengths[0])?,
        root_ids: bytes_with_capacity(lengths[1])?,
        node_tags: bytes_with_capacity(lengths[2])?,
        node_field_offsets: bytes_with_capacity(lengths[3])?,
        field_kinds: bytes_with_capacity(lengths[4])?,
        field_values: bytes_with_capacity(lengths[5])?,
        field_lengths: bytes_with_capacity(lengths[6])?,
        item_kinds: bytes_with_capacity(lengths[7])?,
        item_values: bytes_with_capacity(lengths[8])?,
        item_lengths: bytes_with_capacity(lengths[9])?,
        scalar_bytes: bytes_with_capacity(lengths[10])?,
    })
}

fn checked_width(count: usize, width: usize) -> NativeResult<usize> {
    count
        .checked_mul(width)
        .ok_or_else(|| NativeError::limit("native encoded-column width overflow"))
}

trait ByteOutputV1 {
    fn len(&self) -> usize;
    fn push(&mut self, value: u8) -> NativeResult<()>;
    fn extend_from_slice(&mut self, value: &[u8]) -> NativeResult<()>;
    fn last(&self) -> Option<u8>;
    fn pop(&mut self) -> Option<u8>;
}

impl ByteOutputV1 for Vec<u8> {
    fn len(&self) -> usize {
        Vec::len(self)
    }

    fn push(&mut self, value: u8) -> NativeResult<()> {
        Vec::push(self, value);
        Ok(())
    }

    fn extend_from_slice(&mut self, value: &[u8]) -> NativeResult<()> {
        Vec::extend_from_slice(self, value);
        Ok(())
    }

    fn last(&self) -> Option<u8> {
        self.as_slice().last().copied()
    }

    fn pop(&mut self) -> Option<u8> {
        Vec::pop(self)
    }
}

#[derive(Debug)]
struct FixedByteOutputV1<'buffer> {
    output: &'buffer mut [u8],
    length: usize,
}

impl<'buffer> FixedByteOutputV1<'buffer> {
    const fn new(output: &'buffer mut [u8]) -> Self {
        Self { output, length: 0 }
    }

    fn full(&self) -> bool {
        self.length == self.output.len()
    }
}

impl ByteOutputV1 for FixedByteOutputV1<'_> {
    fn len(&self) -> usize {
        self.length
    }

    fn push(&mut self, value: u8) -> NativeResult<()> {
        let slot = self
            .output
            .get_mut(self.length)
            .ok_or_else(|| NativeError::protocol("native encoded-column fixed buffer overflow"))?;
        *slot = value;
        self.length = self
            .length
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native encoded-column fixed offset overflow"))?;
        Ok(())
    }

    fn extend_from_slice(&mut self, value: &[u8]) -> NativeResult<()> {
        let end = self
            .length
            .checked_add(value.len())
            .ok_or_else(|| NativeError::limit("native encoded-column fixed offset overflow"))?;
        let output = self
            .output
            .get_mut(self.length..end)
            .ok_or_else(|| NativeError::protocol("native encoded-column fixed buffer overflow"))?;
        output.copy_from_slice(value);
        self.length = end;
        Ok(())
    }

    fn last(&self) -> Option<u8> {
        self.length
            .checked_sub(1)
            .and_then(|index| self.output.get(index))
            .copied()
    }

    fn pop(&mut self) -> Option<u8> {
        let index = self.length.checked_sub(1)?;
        let value = *self.output.get(index)?;
        self.length = index;
        Some(value)
    }
}

fn fixed_buffers<'buffer>(
    output: &'buffer mut [u8],
    layout: EncodedStructuralBufferLayoutV1,
) -> NativeResult<EncodedStructuralBufferSetV1<FixedByteOutputV1<'buffer>>> {
    if output.len() != layout.total_bytes() {
        return Err(NativeError::protocol(
            "native encoded-column fixed arena has the wrong size",
        ));
    }
    let lengths = layout.lengths();
    let mut remaining = output;
    let mut take = |length: usize| -> NativeResult<FixedByteOutputV1<'buffer>> {
        let current = std::mem::take(&mut remaining);
        if length > current.len() {
            return Err(NativeError::protocol(
                "native encoded-column fixed layout exceeds its arena",
            ));
        }
        let (selected, tail) = current.split_at_mut(length);
        remaining = tail;
        Ok(FixedByteOutputV1::new(selected))
    };
    let buffers = EncodedStructuralBufferSetV1 {
        root_kinds: take(lengths[0])?,
        root_ids: take(lengths[1])?,
        node_tags: take(lengths[2])?,
        node_field_offsets: take(lengths[3])?,
        field_kinds: take(lengths[4])?,
        field_values: take(lengths[5])?,
        field_lengths: take(lengths[6])?,
        item_kinds: take(lengths[7])?,
        item_values: take(lengths[8])?,
        item_lengths: take(lengths[9])?,
        scalar_bytes: take(lengths[10])?,
    };
    if !remaining.is_empty() {
        return Err(NativeError::protocol(
            "native encoded-column fixed layout did not cover its arena",
        ));
    }
    Ok(buffers)
}

fn fixed_buffers_full(buffers: &EncodedStructuralBufferSetV1<FixedByteOutputV1<'_>>) -> bool {
    buffers.root_kinds.full()
        && buffers.root_ids.full()
        && buffers.node_tags.full()
        && buffers.node_field_offsets.full()
        && buffers.field_kinds.full()
        && buffers.field_values.full()
        && buffers.field_lengths.full()
        && buffers.item_kinds.full()
        && buffers.item_values.full()
        && buffers.item_lengths.full()
        && buffers.scalar_bytes.full()
}

fn append_field<B: ByteOutputV1>(
    field: ComponentFieldRef<'_>,
    node_ids: &HashMap<ComponentId, u32>,
    buffers: &mut EncodedStructuralBufferSetV1<B>,
) -> NativeResult<()> {
    match field {
        ComponentFieldRef::CanonicalSet(sequence) => {
            let start = buffers.item_kinds.len();
            let mut previous = None;
            for index in 0..sequence.len() {
                let item = sequence.item(index)?;
                let ComponentFieldRef::Node(component) = item else {
                    return Err(NativeError::protocol(
                        "native encoded-column canonical sets must contain nodes",
                    ));
                };
                let identifier = public_node_id(node_ids, component)?;
                if previous.is_some_and(|prior| identifier <= prior) {
                    return Err(NativeError::protocol(
                        "native encoded-column canonical set is not ordered and unique",
                    ));
                }
                previous = Some(identifier);
                buffers.item_kinds.push(1)?;
                append_u64(&mut buffers.item_values, u64::from(identifier))?;
                append_u64(&mut buffers.item_lengths, 0)?;
            }
            append_component_row(
                &mut buffers.field_kinds,
                &mut buffers.field_values,
                &mut buffers.field_lengths,
                6,
                start,
                sequence.len(),
            )
        }
        ComponentFieldRef::OrderedSequence(sequence) => {
            let start = buffers.item_kinds.len();
            for index in 0..sequence.len() {
                append_leaf(
                    sequence.item(index)?,
                    node_ids,
                    &mut buffers.item_kinds,
                    &mut buffers.item_values,
                    &mut buffers.item_lengths,
                    &mut buffers.scalar_bytes,
                )?;
            }
            append_component_row(
                &mut buffers.field_kinds,
                &mut buffers.field_values,
                &mut buffers.field_lengths,
                7,
                start,
                sequence.len(),
            )
        }
        leaf => append_leaf(
            leaf,
            node_ids,
            &mut buffers.field_kinds,
            &mut buffers.field_values,
            &mut buffers.field_lengths,
            &mut buffers.scalar_bytes,
        ),
    }
}

#[allow(clippy::too_many_arguments)]
fn append_leaf<B: ByteOutputV1>(
    field: ComponentFieldRef<'_>,
    node_ids: &HashMap<ComponentId, u32>,
    kinds: &mut B,
    values: &mut B,
    lengths: &mut B,
    scalars: &mut B,
) -> NativeResult<()> {
    let (kind, value, length) = match field {
        ComponentFieldRef::None => (0, 0, 0),
        ComponentFieldRef::Node(component) => (
            1,
            usize::try_from(public_node_id(node_ids, component)?)
                .map_err(|_| NativeError::limit("native encoded-column node ID exceeds usize"))?,
            0,
        ),
        ComponentFieldRef::Text(payload) => append_scalar(2, payload, scalars)?,
        ComponentFieldRef::Bytes(payload) => append_scalar(3, payload, scalars)?,
        ComponentFieldRef::NonnegativeIntegerVarint(varint) => {
            let start = scalars.len();
            append_integer_magnitude(varint, scalars)?;
            (4, start, scalars.len() - start)
        }
        ComponentFieldRef::Enum(payload) => append_scalar(5, payload, scalars)?,
        ComponentFieldRef::CanonicalSet(_) | ComponentFieldRef::OrderedSequence(_) => {
            return Err(NativeError::protocol(
                "native encoded-column nested collection item is unsupported",
            ));
        }
    };
    append_component_row(kinds, values, lengths, kind, value, length)
}

fn append_scalar<B: ByteOutputV1>(
    kind: u8,
    payload: &[u8],
    scalars: &mut B,
) -> NativeResult<(u8, usize, usize)> {
    let start = scalars.len();
    scalars.extend_from_slice(payload)?;
    Ok((kind, start, payload.len()))
}

fn append_integer_magnitude<B: ByteOutputV1>(varint: &[u8], output: &mut B) -> NativeResult<()> {
    let expected = integer_magnitude_len(varint)?;
    let start = output.len();
    let mut accumulator = 0_u16;
    let mut bits = 0_u32;
    for byte in varint {
        accumulator |= u16::from(byte & 0x7f) << bits;
        bits += 7;
        while bits >= 8 {
            output.push(accumulator as u8)?;
            accumulator >>= 8;
            bits -= 8;
        }
    }
    if bits != 0 {
        output.push(accumulator as u8)?;
    }
    while output.len() > start + 1 && output.last() == Some(0) {
        let _ = output.pop();
    }
    if output.len() == start {
        output.push(0)?;
    }
    if output.len() - start != expected {
        return Err(NativeError::protocol(
            "native integer magnitude conversion length drifted",
        ));
    }
    Ok(())
}

fn append_component_row<B: ByteOutputV1>(
    kinds: &mut B,
    values: &mut B,
    lengths: &mut B,
    kind: u8,
    value: usize,
    length: usize,
) -> NativeResult<()> {
    kinds.push(kind)?;
    append_u64(
        values,
        u64::try_from(value)
            .map_err(|_| NativeError::limit("native encoded-column value exceeds u64"))?,
    )?;
    append_u64(
        lengths,
        u64::try_from(length)
            .map_err(|_| NativeError::limit("native encoded-column length exceeds u64"))?,
    )?;
    Ok(())
}

fn public_node_id(
    node_ids: &HashMap<ComponentId, u32>,
    component: ComponentId,
) -> NativeResult<u32> {
    node_ids
        .get(&component)
        .copied()
        .ok_or_else(|| NativeError::protocol("native encoded-column child is unreachable"))
}

fn total_buffer_bytes<B: ByteOutputV1>(
    buffers: &EncodedStructuralBufferSetV1<B>,
) -> NativeResult<usize> {
    [
        buffers.root_kinds.len(),
        buffers.root_ids.len(),
        buffers.node_tags.len(),
        buffers.node_field_offsets.len(),
        buffers.field_kinds.len(),
        buffers.field_values.len(),
        buffers.field_lengths.len(),
        buffers.item_kinds.len(),
        buffers.item_values.len(),
        buffers.item_lengths.len(),
        buffers.scalar_bytes.len(),
    ]
    .into_iter()
    .try_fold(0_usize, |total, length| {
        total
            .checked_add(length)
            .ok_or_else(|| NativeError::limit("native encoded-column buffer size overflow"))
    })
}

fn check_layout_limits(
    arena: &NativeComponentArena,
    limits: &Limits,
    caller_external_bytes: usize,
    layout: ColumnLayout,
) -> NativeResult<()> {
    let row_limit = limits.value(LimitKey::MaxIndexRows);
    if [
        layout.root_rows,
        layout.node_rows,
        layout.field_rows,
        layout.item_rows,
    ]
    .into_iter()
    .any(|rows| u64::try_from(rows).map_or(true, |rows| rows > row_limit))
    {
        return Err(NativeError::limit(
            "native encoded-column rows exceed max_index_rows",
        ));
    }
    let buffer_bytes = u64::try_from(layout.buffer_bytes)
        .map_err(|_| NativeError::limit("native encoded-column buffers exceed u64"))?;
    if buffer_bytes > limits.value(LimitKey::MaxIndexBytes) {
        return Err(NativeError::limit(
            "native encoded-column buffers exceed max_index_bytes",
        ));
    }
    let metadata_bytes = u64::try_from(layout.metadata_bytes)
        .map_err(|_| NativeError::limit("native encoded-column metadata exceeds u64"))?;
    let workspace_bytes = u64::try_from(layout.workspace_bytes)
        .map_err(|_| NativeError::limit("native encoded-column workspace exceeds u64"))?;
    let caller_external_bytes = u64::try_from(caller_external_bytes)
        .map_err(|_| NativeError::limit("native encoded-column caller allocation exceeds u64"))?;
    let peak = arena
        .counters()
        .retained_bytes
        .checked_add(caller_external_bytes)
        .and_then(|value| value.checked_add(buffer_bytes))
        .and_then(|value| value.checked_add(metadata_bytes))
        .and_then(|value| value.checked_add(workspace_bytes))
        .ok_or_else(|| NativeError::limit("native encoded-column memory overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| peak > maximum)
    {
        return Err(NativeError::limit(
            "native encoded-column build exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

fn bytes_with_capacity(capacity: usize) -> NativeResult<Vec<u8>> {
    let mut output = Vec::new();
    output
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native encoded-column buffer allocation failed"))?;
    Ok(output)
}

fn append_u16<B: ByteOutputV1>(output: &mut B, value: u16) -> NativeResult<()> {
    output.extend_from_slice(&value.to_le_bytes())
}

fn append_u32<B: ByteOutputV1>(output: &mut B, value: u32) -> NativeResult<()> {
    output.extend_from_slice(&value.to_le_bytes())
}

fn append_u64<B: ByteOutputV1>(output: &mut B, value: u64) -> NativeResult<()> {
    output.extend_from_slice(&value.to_le_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::NativeComponentBuilder;

    mod generated {
        include!(concat!(env!("OUT_DIR"), "/encoded_view_v1.rs"));
    }

    fn encode_varint(mut value: u64) -> Vec<u8> {
        let mut output = Vec::new();
        loop {
            let byte = (value & 0x7f) as u8;
            value >>= 7;
            output.push(byte | if value == 0 { 0 } else { 0x80 });
            if value == 0 {
                return output;
            }
        }
    }

    fn frame(value: &[u8]) -> Vec<u8> {
        let mut output = encode_varint(value.len() as u64);
        output.extend_from_slice(value);
        output
    }

    fn iri(value: &str) -> Vec<u8> {
        let mut output = vec![1, 2];
        output.extend(frame(value.as_bytes()));
        output
    }

    fn entity(kind: &str, value: &str) -> Vec<u8> {
        let iri = iri(value);
        let mut output = vec![2, 5];
        output.extend(frame(kind.as_bytes()));
        output.push(1);
        output.extend(frame(&iri));
        output
    }

    fn declaration(value: &str) -> Vec<u8> {
        let entity = entity("class", value);
        let mut output = vec![60, 1];
        output.extend(frame(&entity));
        output.extend([6, 0]);
        output
    }

    fn object_property_chain(values: &[&str]) -> Vec<u8> {
        let mut output = vec![11, 7];
        output.extend(encode_varint(values.len() as u64));
        for value in values {
            output.push(1);
            output.extend(frame(&entity("object_property", value)));
        }
        output
    }

    fn sub_object_property_of(chain: &[&str], parent: &str) -> Vec<u8> {
        let mut output = vec![70, 1];
        output.extend(frame(&object_property_chain(chain)));
        output.push(1);
        output.extend(frame(&entity("object_property", parent)));
        output.extend([6, 0]);
        output
    }

    fn class_assertion_with_cardinality(
        cardinality_varint: &[u8],
        property: &str,
        filler: &str,
        individual: &str,
    ) -> Vec<u8> {
        let mut cardinality = vec![38, 4];
        cardinality.extend_from_slice(cardinality_varint);
        cardinality.push(1);
        cardinality.extend(frame(&entity("object_property", property)));
        cardinality.push(1);
        cardinality.extend(frame(&entity("class", filler)));

        let mut output = vec![112, 1];
        output.extend(frame(&cardinality));
        output.push(1);
        output.extend(frame(&entity("named_individual", individual)));
        output.extend([6, 0]);
        output
    }

    fn root_arena(values: &[Vec<u8>]) -> (NativeComponentArena, Vec<ComponentId>) {
        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let pending: Vec<_> = values
            .iter()
            .map(|value| builder.intern_canonical(value).expect("root"))
            .collect();
        let frozen = builder.freeze().expect("freeze");
        let identifiers = pending
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("root id"))
            .collect();
        (frozen.into_arena(), identifiers)
    }

    fn declaration_arena(values: &[&str]) -> (NativeComponentArena, Vec<ComponentId>) {
        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let pending: Vec<_> = values
            .iter()
            .map(|value| {
                builder
                    .intern_canonical(&declaration(value))
                    .expect("declaration")
            })
            .collect();
        let frozen = builder.freeze().expect("freeze");
        let identifiers = pending
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("root id"))
            .collect();
        (frozen.into_arena(), identifiers)
    }

    fn little_u16(values: &[u16]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_le_bytes())
            .collect()
    }

    fn little_u64(values: &[u64]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_le_bytes())
            .collect()
    }

    fn read_u16(data: &[u8], index: usize) -> u16 {
        let offset = index * 2;
        u16::from_le_bytes(data[offset..offset + 2].try_into().expect("u16 row"))
    }

    fn read_u32(data: &[u8], index: usize) -> u32 {
        let offset = index * 4;
        u32::from_le_bytes(data[offset..offset + 4].try_into().expect("u32 row"))
    }

    fn read_u64(data: &[u8], index: usize) -> u64 {
        let offset = index * 8;
        u64::from_le_bytes(data[offset..offset + 8].try_into().expect("u64 row"))
    }

    fn magnitude_to_varint(payload: &[u8]) -> Vec<u8> {
        assert!(!payload.is_empty());
        let significant_bits = payload
            .iter()
            .rposition(|byte| *byte != 0)
            .map_or(0, |index| {
                index * 8 + (u8::BITS - payload[index].leading_zeros()) as usize
            });
        let groups = significant_bits.div_ceil(7).max(1);
        (0..groups)
            .map(|group| {
                let bit = group * 7;
                let byte = bit / 8;
                let shift = bit % 8;
                let window = u16::from(payload[byte])
                    | payload
                        .get(byte + 1)
                        .map_or(0, |next| u16::from(*next) << 8);
                ((window >> shift) as u8 & 0x7f) | if group + 1 == groups { 0 } else { 0x80 }
            })
            .collect()
    }

    fn independently_decode_node(buffers: &EncodedStructuralBuffersV1, node_id: u32) -> Vec<u8> {
        assert_ne!(node_id, 0);
        let node_index = node_id as usize - 1;
        let mut output = encode_varint(u64::from(read_u16(&buffers.node_tags, node_index)));
        let start = read_u64(&buffers.node_field_offsets, node_index) as usize;
        let end = read_u64(&buffers.node_field_offsets, node_index + 1) as usize;
        for field_index in start..end {
            let kind = buffers.field_kinds[field_index];
            let value = read_u64(&buffers.field_values, field_index);
            let length = read_u64(&buffers.field_lengths, field_index);
            output.push(kind);
            match kind {
                1 => {
                    assert_eq!(length, 0);
                    let child =
                        independently_decode_node(buffers, u32::try_from(value).expect("node id"));
                    output.extend(frame(&child));
                }
                2 | 3 | 5 => {
                    let start = value as usize;
                    let end = start + length as usize;
                    output.extend(frame(&buffers.scalar_bytes[start..end]));
                }
                4 => {
                    let start = value as usize;
                    let end = start + length as usize;
                    output.extend(magnitude_to_varint(&buffers.scalar_bytes[start..end]));
                }
                6 | 7 => {
                    output.extend(encode_varint(length));
                    for item_index in value as usize..(value + length) as usize {
                        assert_eq!(buffers.item_kinds[item_index], 1);
                        assert_eq!(read_u64(&buffers.item_lengths, item_index), 0);
                        if kind == 7 {
                            output.push(1);
                        }
                        let child = independently_decode_node(
                            buffers,
                            u32::try_from(read_u64(&buffers.item_values, item_index))
                                .expect("item node id"),
                        );
                        output.extend(frame(&child));
                    }
                }
                other => panic!("unsupported independent fixture kind {other}"),
            }
        }
        output
    }

    #[test]
    fn empty_columns_compile_to_the_frozen_eleven_buffer_shape() {
        let limits = Limits::default();
        let arena = NativeComponentBuilder::new(&limits)
            .expect("builder")
            .freeze()
            .expect("freeze")
            .into_arena();
        let columns = build_encoded_structural_columns_v1(
            &arena,
            &[],
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("empty columns");

        assert_eq!(columns.buffers().named().len(), 11);
        assert_eq!(columns.buffers().node_field_offsets, 0_u64.to_le_bytes());
        assert_eq!(columns.counters().retained_buffer_bytes, 8);
        assert!(columns.owner().shares_storage_with(&arena));
        assert_eq!(columns.counters().root_rows, 0);
    }

    #[test]
    fn prepared_empty_columns_fill_one_exact_backing_arena() {
        let limits = Limits::default();
        let arena = NativeComponentBuilder::new(&limits)
            .expect("builder")
            .freeze()
            .expect("freeze")
            .into_arena();
        let prepared = prepare_encoded_structural_columns_from_source_v1(
            &arena,
            RetainedRootSourceV1::Slice(RootSliceV1(&[])),
            &limits,
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("prepared empty columns");
        let layout = prepared.layout();
        let mut output = vec![0xff; layout.total_bytes()];

        prepared.write_into(&mut output).expect("direct empty fill");

        assert_eq!(output, 0_u64.to_le_bytes());
        assert_eq!(layout.named_ranges()[3], ("node_field_offsets", 0, 8));
        let mut short = vec![0; layout.total_bytes() - 1];
        assert_eq!(
            prepared.write_into(&mut short).unwrap_err().code,
            "NATIVE_PROTOCOL"
        );
    }

    #[test]
    fn declaration_columns_match_frozen_bytes_and_independent_decode() {
        let limits = Limits::default();
        let canonical = declaration("urn:Class");
        let (arena, identifiers) = declaration_arena(&["urn:Class"]);
        let root = EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[0]);
        let columns = build_encoded_structural_columns_v1(
            &arena,
            &[root],
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("declaration columns");
        let buffers = columns.buffers();

        assert_eq!(buffers.root_kinds, [2]);
        assert_eq!(buffers.root_ids, 3_u32.to_le_bytes());
        assert_eq!(buffers.node_tags, little_u16(&[1, 2, 60]));
        assert_eq!(buffers.node_field_offsets, little_u64(&[0, 1, 3, 5]));
        assert_eq!(buffers.field_kinds, [2, 5, 1, 1, 6]);
        assert_eq!(buffers.field_values, little_u64(&[0, 9, 1, 2, 0]));
        assert_eq!(buffers.field_lengths, little_u64(&[9, 5, 0, 0, 0]));
        assert!(buffers.item_kinds.is_empty());
        assert!(buffers.item_values.is_empty());
        assert!(buffers.item_lengths.is_empty());
        assert_eq!(buffers.scalar_bytes, b"urn:Classclass");
        assert_eq!(
            buffers.named().map(|(name, _)| name),
            [
                "root_kinds",
                "root_ids",
                "node_tags",
                "node_field_offsets",
                "field_kinds",
                "field_values",
                "field_lengths",
                "item_kinds",
                "item_values",
                "item_lengths",
                "scalar_bytes",
            ]
        );

        assert_eq!(buffers.root_kinds[0], EncodedRootKindV1::Axiom as u8);
        let decoded = independently_decode_node(buffers, read_u32(&buffers.root_ids, 0));
        assert_eq!(decoded, canonical);
        assert!(columns.owner().shares_storage_with(&arena));
        assert_eq!(columns.counters().root_rows, 1);

        let counters = columns.counters();
        assert_eq!(counters.root_rows, 1);
        assert_eq!(counters.node_rows, 3);
        assert_eq!(counters.field_rows, 5);
        assert_eq!(counters.item_rows, 0);
        assert_eq!(counters.scalar_bytes, 14);
        assert_eq!(counters.retained_buffer_bytes, 142);
        assert_eq!(counters.retained_metadata_bytes, 0);
        assert_eq!(
            counters.peak_owned_bytes,
            arena.counters().retained_bytes
                + counters.retained_buffer_bytes
                + counters.retained_metadata_bytes
        );
        assert!(counters.peak_workspace_bytes >= size_of::<NodeRow>() as u64 * 3);
        assert_eq!(counters.scalar_copy_bytes, 14);
        assert!(counters.canonical_work > counters.retained_buffer_bytes);
        assert!(counters.canonical_comparison_bytes > 0);
        assert_eq!(counters.complete_root_encode_calls, 0);
    }

    #[test]
    fn general_columns_preserve_sequences_large_integers_and_canonical_roots() {
        let limits = Limits::default();
        let canonical = vec![
            sub_object_property_of(&["urn:p", "urn:q"], "urn:r"),
            class_assertion_with_cardinality(&[0xac, 0x02], "urn:p2", "urn:C", "urn:i"),
        ];
        let (arena, identifiers) = root_arena(&canonical);
        let roots = [
            EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[0]),
            EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[1]),
        ];
        let columns = build_encoded_structural_columns_v1(
            &arena,
            &roots,
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("general columns");
        let buffers = columns.buffers();

        assert_eq!(buffers.root_kinds, [2, 2]);
        assert_eq!(columns.counters().root_rows, 2);
        assert_eq!(columns.counters().node_rows, 16);
        assert_eq!(columns.counters().item_rows, 2);
        assert_eq!(buffers.item_kinds, [1, 1]);
        for (index, expected) in canonical.iter().enumerate() {
            assert_eq!(
                independently_decode_node(buffers, read_u32(&buffers.root_ids, index)),
                *expected
            );
        }
        let decoded_nodes: Vec<_> = (1..=columns.counters().node_rows as u32)
            .map(|identifier| independently_decode_node(buffers, identifier))
            .collect();
        for (index, pair) in decoded_nodes.windows(2).enumerate() {
            assert!(
                pair[0] < pair[1],
                "node order drift at {}: {:?} >= {:?}",
                index + 1,
                pair[0],
                pair[1]
            );
        }

        let integer_field = buffers
            .field_kinds
            .iter()
            .position(|kind| *kind == 4)
            .expect("integer field");
        let start = read_u64(&buffers.field_values, integer_field) as usize;
        let length = read_u64(&buffers.field_lengths, integer_field) as usize;
        assert_eq!(&buffers.scalar_bytes[start..start + length], &[0x2c, 0x01]);
        assert_eq!(columns.counters().complete_root_encode_calls, 0);
        assert!(columns.counters().canonical_comparison_bytes > 0);
    }

    #[test]
    fn prepared_general_columns_match_owned_buffers_byte_for_byte() {
        let limits = Limits::default();
        let canonical = vec![
            sub_object_property_of(&["urn:p", "urn:q"], "urn:r"),
            class_assertion_with_cardinality(&[0xac, 0x02], "urn:p2", "urn:C", "urn:i"),
        ];
        let (arena, identifiers) = root_arena(&canonical);
        let roots = [
            EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[0]),
            EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[1]),
        ];
        let owned = build_encoded_structural_columns_v1(
            &arena,
            &roots,
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("owned columns");
        let prepared = prepare_encoded_structural_columns_from_source_v1(
            &arena,
            RetainedRootSourceV1::Slice(RootSliceV1(&roots)),
            &limits,
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("prepared columns");
        let layout = prepared.layout();
        let mut output = vec![0; layout.total_bytes()];

        prepared.write_into(&mut output).expect("direct fill");

        for ((range_name, start, length), (owned_name, owned_bytes)) in layout
            .named_ranges()
            .into_iter()
            .zip(owned.buffers().named())
        {
            assert_eq!(range_name, owned_name);
            assert_eq!(length, owned_bytes.len());
            assert_eq!(&output[start..start + length], owned_bytes);
        }
        assert_eq!(prepared.counters(), owned.counters());
        let mut long = vec![0; layout.total_bytes() + 1];
        assert_eq!(
            prepared.write_into(&mut long).unwrap_err().code,
            "NATIVE_PROTOCOL"
        );
    }

    #[test]
    fn root_order_uses_complete_length_framed_canonical_bytes() {
        let limits = Limits::default();
        let canonical = [declaration("urn:p2"), declaration("urn:q")];
        assert!(canonical[1] < canonical[0]);
        let (arena, identifiers) = root_arena(&canonical);
        let first = EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[1]);
        let second = EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[0]);

        let columns = build_encoded_structural_columns_v1(
            &arena,
            &[first, second],
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("length-framed root order");
        assert_eq!(
            independently_decode_node(columns.buffers(), read_u32(&columns.buffers().root_ids, 0)),
            canonical[1]
        );
        assert_eq!(
            build_encoded_structural_columns_v1(
                &arena,
                &[second, first],
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_PROTOCOL"
        );
    }

    #[test]
    fn frozen_descriptor_stays_unadvertised_and_names_exact_buffers() {
        assert_eq!(generated::NAME, ENCODED_STRUCTURAL_SCHEMA_NAME_V1);
        assert_eq!(generated::VERSION, ENCODED_STRUCTURAL_SCHEMA_VERSION_V1);
        assert_eq!(generated::MODEL_SCHEMA, ENCODED_STRUCTURAL_MODEL_SCHEMA_V1);
        assert_eq!(generated::STATUS, "frozen-unadvertised");
        assert!(!std::hint::black_box(generated::CAPABILITY_ADVERTISED));
        assert_eq!(
            crate::hash::sha256(generated::DESCRIPTOR),
            generated::DESCRIPTOR_SHA256
        );
        let descriptor = std::str::from_utf8(generated::DESCRIPTOR).expect("ASCII descriptor");
        for name in EncodedStructuralBuffersV1::default()
            .named()
            .map(|(name, _)| name)
        {
            assert!(descriptor.contains(&format!("\"name\":\"{name}\"")));
        }
    }

    #[test]
    fn declaration_seam_rejects_foreign_duplicate_order_and_unsupported_roots() {
        let limits = Limits::default();
        let (arena, identifiers) = declaration_arena(&["urn:a", "urn:z"]);
        let first = EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[0]);
        let second = EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[1]);
        let build = |roots: &[EncodedRootV1]| {
            build_encoded_structural_columns_v1(
                &arena,
                roots,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
        };

        assert_eq!(build(&[first, first]).unwrap_err().code, "NATIVE_PROTOCOL");
        assert_eq!(build(&[second, first]).unwrap_err().code, "NATIVE_PROTOCOL");
        let ordered = build(&[first, second]).expect("ordered declarations");
        assert_eq!(ordered.counters().root_rows, 2);
        assert_eq!(ordered.counters().node_rows, 6);
        assert_eq!(
            build(&[EncodedRootV1::new(
                EncodedRootKindV1::OntologyAnnotation,
                first.component(),
            )])
            .unwrap_err()
            .code,
            "NATIVE_PROTOCOL"
        );
        let entity = match arena
            .record(first.component())
            .expect("declaration")
            .field(0)
            .expect("entity")
        {
            ComponentFieldRef::Node(identifier) => identifier,
            other => panic!("expected entity node, got {other:?}"),
        };
        assert_eq!(
            build(&[EncodedRootV1::new(EncodedRootKindV1::Axiom, entity)])
                .unwrap_err()
                .code,
            "NATIVE_PROTOCOL"
        );

        let (foreign, foreign_ids) = declaration_arena(&["urn:foreign"]);
        let foreign_root = EncodedRootV1::new(EncodedRootKindV1::Axiom, foreign_ids[0]);
        assert_eq!(build(&[foreign_root]).unwrap_err().code, "NATIVE_PROTOCOL");
        assert!(!arena.shares_storage_with(&foreign));
    }

    #[test]
    fn declaration_seam_enforces_work_memory_and_cancellation() {
        let limits = Limits::default();
        let (arena, identifiers) = declaration_arena(&["urn:bounded"]);
        let root = EncodedRootV1::new(EncodedRootKindV1::Axiom, identifiers[0]);
        assert_eq!(root.kind(), EncodedRootKindV1::Axiom);
        let baseline = build_encoded_structural_columns_v1(
            &arena,
            &[root],
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("baseline");

        let mut work_limited = limits;
        work_limited.max_canonical_work = 1;
        assert_eq!(
            build_encoded_structural_columns_v1(
                &arena,
                &[root],
                &work_limited,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        let mut depth_limited = limits;
        depth_limited.max_nesting_depth = 0;
        assert_eq!(
            build_encoded_structural_columns_v1(
                &arena,
                &[root],
                &depth_limited,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        let mut memory_limited = limits;
        memory_limited.max_memory_bytes = Some(baseline.counters().peak_owned_bytes - 1);
        assert_eq!(
            build_encoded_structural_columns_v1(
                &arena,
                &[root],
                &memory_limited,
                Cancellation::with_duration(None),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        assert_eq!(
            build_encoded_structural_columns_v1(
                &arena,
                &[root],
                &limits,
                Cancellation::with_duration(Some(std::time::Duration::ZERO)),
                None,
            )
            .unwrap_err()
            .code,
            "NATIVE_DEADLINE"
        );
    }
}
