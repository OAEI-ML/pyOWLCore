//! Direct encoded-structural-columns-v1 construction over retained components.
//!
//! This pre-advertisement seam intentionally supports only an empty root set or
//! one unannotated `DECLARATION` root. Every wider graph fails closed until its
//! direct traversal and parity fixtures land.

use std::mem::size_of;

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};

use super::{ComponentFieldRef, ComponentId, NativeComponentArena};

pub(crate) const ENCODED_STRUCTURAL_SCHEMA_NAME_V1: &str = "pyowl-core/structural-columns";
pub(crate) const ENCODED_STRUCTURAL_SCHEMA_VERSION_V1: u32 = 1;
pub(crate) const ENCODED_STRUCTURAL_MODEL_SCHEMA_V1: u32 = 1;

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
pub(crate) struct EncodedStructuralBuffersV1 {
    root_kinds: Vec<u8>,
    root_ids: Vec<u8>,
    node_tags: Vec<u8>,
    node_field_offsets: Vec<u8>,
    field_kinds: Vec<u8>,
    field_values: Vec<u8>,
    field_lengths: Vec<u8>,
    item_kinds: Vec<u8>,
    item_values: Vec<u8>,
    item_lengths: Vec<u8>,
    scalar_bytes: Vec<u8>,
}

impl EncodedStructuralBuffersV1 {
    pub(crate) fn named(&self) -> [(&'static str, &[u8]); 11] {
        [
            ("root_kinds", &self.root_kinds),
            ("root_ids", &self.root_ids),
            ("node_tags", &self.node_tags),
            ("node_field_offsets", &self.node_field_offsets),
            ("field_kinds", &self.field_kinds),
            ("field_values", &self.field_values),
            ("field_lengths", &self.field_lengths),
            ("item_kinds", &self.item_kinds),
            ("item_values", &self.item_values),
            ("item_lengths", &self.item_lengths),
            ("scalar_bytes", &self.scalar_bytes),
        ]
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
}

#[derive(Clone, Copy, Debug)]
struct ColumnLayout {
    root_rows: usize,
    node_rows: usize,
    field_rows: usize,
    item_rows: usize,
    buffer_bytes: usize,
    metadata_bytes: usize,
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
        })
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
    roots: Vec<EncodedRootV1>,
    buffers: EncodedStructuralBuffersV1,
    counters: EncodedColumnCountersV1,
}

impl EncodedStructuralColumnsV1 {
    pub(crate) const fn owner(&self) -> &NativeComponentArena {
        &self.owner
    }

    pub(crate) fn roots(&self) -> &[EncodedRootV1] {
        &self.roots
    }

    pub(crate) const fn buffers(&self) -> &EncodedStructuralBuffersV1 {
        &self.buffers
    }

    pub(crate) const fn counters(&self) -> &EncodedColumnCountersV1 {
        &self.counters
    }
}

pub(crate) fn build_encoded_structural_columns_v1(
    arena: &NativeComponentArena,
    roots: &[EncodedRootV1],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
) -> NativeResult<EncodedStructuralColumnsV1> {
    let mut work = ColumnWork::new(limits, cancellation, interrupt)?;
    if roots.is_empty() {
        return build_empty(arena, limits, work);
    }
    if roots.len() != 1 {
        return Err(NativeError::protocol(
            "native encoded-column declaration seam requires exactly one root",
        ));
    }
    let root = roots[0];
    if root.kind != EncodedRootKindV1::Axiom {
        return Err(NativeError::protocol(
            "native declaration root must use the axiom root kind",
        ));
    }
    let declaration = arena.record(root.component)?;
    work.consume(1)?;
    if declaration.tag() != 60 || declaration.field_count() != 2 {
        return Err(NativeError::protocol(
            "native encoded-column seam supports declaration roots only",
        ));
    }
    let entity_id = expect_node(declaration.field(0)?, "declaration entity")?;
    work.consume(1)?;
    match declaration.field(1)? {
        ComponentFieldRef::CanonicalSet(sequence) if sequence.is_empty() => {}
        _ => {
            return Err(NativeError::protocol(
                "native declaration seam requires an empty annotation set",
            ));
        }
    }
    work.consume(1)?;

    let entity = arena.record(entity_id)?;
    work.consume(1)?;
    if entity.tag() != 2 || entity.field_count() != 2 {
        return Err(NativeError::protocol(
            "native declaration entity is not a canonical ENTITY",
        ));
    }
    let entity_kind = match entity.field(0)? {
        ComponentFieldRef::Enum(value) => value,
        _ => {
            return Err(NativeError::protocol(
                "native declaration entity kind is not an enum",
            ));
        }
    };
    work.consume(entity_kind.len().saturating_add(1))?;
    let iri_id = expect_node(entity.field(1)?, "entity IRI")?;
    work.consume(1)?;

    let iri = arena.record(iri_id)?;
    work.consume(1)?;
    if iri.tag() != 1 || iri.field_count() != 1 {
        return Err(NativeError::protocol(
            "native declaration entity does not reference a canonical IRI",
        ));
    }
    let iri_text = match iri.field(0)? {
        ComponentFieldRef::Text(value) => value,
        _ => return Err(NativeError::protocol("native IRI payload is not text")),
    };
    work.consume(iri_text.len().saturating_add(1))?;

    build_declaration(arena, root, iri_text, entity_kind, limits, work)
}

fn build_empty(
    arena: &NativeComponentArena,
    limits: &Limits,
    mut work: ColumnWork,
) -> NativeResult<EncodedStructuralColumnsV1> {
    check_layout_limits(
        arena,
        limits,
        ColumnLayout {
            root_rows: 0,
            node_rows: 0,
            field_rows: 0,
            item_rows: 0,
            buffer_bytes: 8,
            metadata_bytes: 0,
        },
    )?;
    work.finish()?;
    Ok(EncodedStructuralColumnsV1 {
        owner: arena.clone(),
        roots: Vec::new(),
        buffers: EncodedStructuralBuffersV1 {
            node_field_offsets: 0_u64.to_le_bytes().to_vec(),
            ..EncodedStructuralBuffersV1::default()
        },
        counters: EncodedColumnCountersV1 {
            retained_buffer_bytes: 8,
            peak_owned_bytes: arena
                .counters()
                .retained_bytes
                .checked_add(8)
                .ok_or_else(|| NativeError::limit("native encoded-column memory overflow"))?,
            canonical_work: work.used,
            ..EncodedColumnCountersV1::default()
        },
    })
}

fn build_declaration(
    arena: &NativeComponentArena,
    root: EncodedRootV1,
    iri_text: &[u8],
    entity_kind: &[u8],
    limits: &Limits,
    mut work: ColumnWork,
) -> NativeResult<EncodedStructuralColumnsV1> {
    const ROOT_ROWS: usize = 1;
    const NODE_ROWS: usize = 3;
    const FIELD_ROWS: usize = 5;
    const ITEM_ROWS: usize = 0;
    const FIXED_BUFFER_BYTES: usize = 128;

    let scalar_bytes = iri_text
        .len()
        .checked_add(entity_kind.len())
        .ok_or_else(|| NativeError::limit("native encoded-column scalar size overflow"))?;
    let buffer_bytes = FIXED_BUFFER_BYTES
        .checked_add(scalar_bytes)
        .ok_or_else(|| NativeError::limit("native encoded-column buffer size overflow"))?;
    let metadata_bytes = size_of::<EncodedRootV1>();
    check_layout_limits(
        arena,
        limits,
        ColumnLayout {
            root_rows: ROOT_ROWS,
            node_rows: NODE_ROWS,
            field_rows: FIELD_ROWS,
            item_rows: ITEM_ROWS,
            buffer_bytes,
            metadata_bytes,
        },
    )?;
    if limits.max_terms < NODE_ROWS as u64
        || limits.max_axioms < ROOT_ROWS as u64
        || limits.max_strings < 2
        || u64::try_from(iri_text.len()).map_or(true, |length| length > limits.max_iri_bytes)
    {
        return Err(NativeError::limit(
            "native encoded-column declaration exceeds model limits",
        ));
    }
    work.consume(buffer_bytes)?;

    let mut buffers = EncodedStructuralBuffersV1 {
        root_kinds: bytes_with_capacity(1)?,
        root_ids: bytes_with_capacity(4)?,
        node_tags: bytes_with_capacity(6)?,
        node_field_offsets: bytes_with_capacity(32)?,
        field_kinds: bytes_with_capacity(5)?,
        field_values: bytes_with_capacity(40)?,
        field_lengths: bytes_with_capacity(40)?,
        item_kinds: Vec::new(),
        item_values: Vec::new(),
        item_lengths: Vec::new(),
        scalar_bytes: bytes_with_capacity(scalar_bytes)?,
    };
    buffers.root_kinds.push(EncodedRootKindV1::Axiom as u8);
    append_u32(&mut buffers.root_ids, 3);
    for tag in [1_u16, 2, 60] {
        append_u16(&mut buffers.node_tags, tag);
    }
    for offset in [0_u64, 1, 3, 5] {
        append_u64(&mut buffers.node_field_offsets, offset);
    }
    buffers.field_kinds.extend_from_slice(&[2, 5, 1, 1, 6]);
    for value in [0_u64, iri_text.len() as u64, 1, 2, 0] {
        append_u64(&mut buffers.field_values, value);
    }
    for length in [iri_text.len() as u64, entity_kind.len() as u64, 0, 0, 0] {
        append_u64(&mut buffers.field_lengths, length);
    }
    buffers.scalar_bytes.extend_from_slice(iri_text);
    buffers.scalar_bytes.extend_from_slice(entity_kind);
    work.finish()?;

    let mut retained_roots = Vec::new();
    retained_roots
        .try_reserve_exact(1)
        .map_err(|_| NativeError::limit("native encoded-column root metadata allocation failed"))?;
    retained_roots.push(root);
    let retained_buffer_bytes = u64::try_from(buffer_bytes)
        .map_err(|_| NativeError::limit("native encoded-column buffers exceed u64"))?;
    let retained_metadata_bytes = u64::try_from(metadata_bytes)
        .map_err(|_| NativeError::limit("native encoded-column metadata exceeds u64"))?;
    let peak_owned_bytes = arena
        .counters()
        .retained_bytes
        .checked_add(retained_buffer_bytes)
        .and_then(|value| value.checked_add(retained_metadata_bytes))
        .ok_or_else(|| NativeError::limit("native encoded-column memory overflow"))?;
    Ok(EncodedStructuralColumnsV1 {
        owner: arena.clone(),
        roots: retained_roots,
        buffers,
        counters: EncodedColumnCountersV1 {
            root_rows: ROOT_ROWS as u64,
            node_rows: NODE_ROWS as u64,
            field_rows: FIELD_ROWS as u64,
            item_rows: ITEM_ROWS as u64,
            scalar_bytes: scalar_bytes as u64,
            retained_buffer_bytes,
            retained_metadata_bytes,
            peak_owned_bytes,
            peak_workspace_bytes: 0,
            scalar_copy_bytes: scalar_bytes as u64,
            canonical_work: work.used,
            canonical_comparison_bytes: 0,
            complete_root_encode_calls: 0,
        },
    })
}

fn expect_node(value: ComponentFieldRef<'_>, label: &'static str) -> NativeResult<ComponentId> {
    match value {
        ComponentFieldRef::Node(identifier) => Ok(identifier),
        _ => Err(NativeError::protocol(label)),
    }
}

fn check_layout_limits(
    arena: &NativeComponentArena,
    limits: &Limits,
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
    let peak = arena
        .counters()
        .retained_bytes
        .checked_add(buffer_bytes)
        .and_then(|value| value.checked_add(metadata_bytes))
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

fn append_u16(output: &mut Vec<u8>, value: u16) {
    output.extend_from_slice(&value.to_le_bytes());
}

fn append_u32(output: &mut Vec<u8>, value: u32) {
    output.extend_from_slice(&value.to_le_bytes());
}

fn append_u64(output: &mut Vec<u8>, value: u64) {
    output.extend_from_slice(&value.to_le_bytes());
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
                6 => {
                    output.extend(encode_varint(length));
                    for item_index in value as usize..(value + length) as usize {
                        assert_eq!(buffers.item_kinds[item_index], 1);
                        assert_eq!(read_u64(&buffers.item_lengths, item_index), 0);
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
        assert!(columns.roots().is_empty());
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
        assert_eq!(columns.roots(), &[root]);

        let counters = columns.counters();
        assert_eq!(counters.root_rows, 1);
        assert_eq!(counters.node_rows, 3);
        assert_eq!(counters.field_rows, 5);
        assert_eq!(counters.item_rows, 0);
        assert_eq!(counters.scalar_bytes, 14);
        assert_eq!(counters.retained_buffer_bytes, 142);
        assert_eq!(
            counters.retained_metadata_bytes,
            size_of::<EncodedRootV1>() as u64
        );
        assert_eq!(
            counters.peak_owned_bytes,
            arena.counters().retained_bytes
                + counters.retained_buffer_bytes
                + counters.retained_metadata_bytes
        );
        assert_eq!(counters.peak_workspace_bytes, 0);
        assert_eq!(counters.scalar_copy_bytes, 14);
        assert_eq!(counters.canonical_work, 164);
        assert_eq!(counters.canonical_comparison_bytes, 0);
        assert_eq!(counters.complete_root_encode_calls, 0);
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
