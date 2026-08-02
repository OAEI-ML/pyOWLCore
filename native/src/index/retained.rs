//! Direct axiom-constructor postings over retained component identifiers.

use std::mem::size_of;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{Category, ComponentId, NativeComponentArena};

const AXIOM_CATEGORY_DECLARATION_V1: u8 = 1;
const AXIOM_CATEGORY_LOGICAL_V1: u8 = 2;
const AXIOM_CATEGORY_ANNOTATION_V1: u8 = 3;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct RetainedAxiomTypeIndexCountersV1 {
    pub(crate) axiom_rows: u64,
    pub(crate) constructor_groups: u64,
    pub(crate) category_groups: u64,
    pub(crate) retained_buffer_bytes: u64,
    pub(crate) peak_owned_bytes: u64,
    pub(crate) canonical_work: u64,
    pub(crate) complete_root_encode_calls: u64,
}

#[derive(Debug)]
pub(crate) struct RetainedAxiomTypeIndexV1 {
    owner: NativeComponentArena,
    roots: Vec<ComponentId>,
    tags: Vec<u16>,
    offsets: Vec<u64>,
    category_codes: Vec<u8>,
    category_offsets: Vec<u64>,
    postings: Vec<u64>,
    canonical_sizes: Vec<u64>,
    caller_external_bytes: usize,
    complete_root_encode_calls: AtomicU64,
    counters: RetainedAxiomTypeIndexCountersV1,
}

#[derive(Debug)]
pub(crate) struct RetainedAxiomTypePageV1 {
    pub(crate) total_count: u64,
    pub(crate) next_cursor: Option<u64>,
    pub(crate) rows: Vec<Vec<u8>>,
}

impl RetainedAxiomTypeIndexV1 {
    pub(crate) const fn owner(&self) -> &NativeComponentArena {
        &self.owner
    }

    pub(crate) fn tags(&self) -> &[u16] {
        &self.tags
    }

    pub(crate) fn offsets(&self) -> &[u64] {
        &self.offsets
    }

    pub(crate) fn postings(&self) -> &[u64] {
        &self.postings
    }

    pub(crate) fn canonical_sizes(&self) -> &[u64] {
        &self.canonical_sizes
    }

    pub(crate) fn category_codes(&self) -> &[u8] {
        &self.category_codes
    }

    pub(crate) fn category_offsets(&self) -> &[u64] {
        &self.category_offsets
    }

    pub(crate) const fn counters(&self) -> &RetainedAxiomTypeIndexCountersV1 {
        &self.counters
    }

    pub(crate) fn complete_root_encode_calls(&self) -> u64 {
        self.complete_root_encode_calls.load(Ordering::Acquire)
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn constructor_page(
        &self,
        tag: u16,
        start: u64,
        max_rows: u32,
        max_bytes: u64,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
    ) -> NativeResult<RetainedAxiomTypePageV1> {
        if max_rows == 0 || max_rows > 64 || max_bytes == 0 || max_bytes > 8 * 1024 * 1024 {
            return Err(NativeError::protocol(
                "retained axiom-type page bounds are invalid",
            ));
        }
        if max_bytes > limits.value(LimitKey::MaxTemporaryBytes) {
            return Err(limits.resource_limit(
                LimitKey::MaxTemporaryBytes,
                max_bytes,
                "retained axiom-type page exceeds max_temporary_bytes",
            ));
        }
        let group = self.tags.binary_search(&tag).ok();
        let (posting_start, posting_stop) = group.map_or((0_usize, 0_usize), |group| {
            (
                usize::try_from(self.offsets[group]).unwrap_or(usize::MAX),
                usize::try_from(self.offsets[group + 1]).unwrap_or(usize::MAX),
            )
        });
        if posting_start > posting_stop || posting_stop > self.postings.len() {
            return Err(NativeError::protocol(
                "retained axiom-type page offsets are invalid",
            ));
        }
        let total_count = u64::try_from(posting_stop - posting_start)
            .map_err(|_| NativeError::limit("retained axiom-type page total exceeds u64"))?;
        if start > total_count {
            return Err(NativeError::protocol(
                "retained axiom-type page start exceeds its total",
            ));
        }
        let start = usize::try_from(start)
            .map_err(|_| NativeError::limit("retained axiom-type page start exceeds usize"))?;
        let available = posting_stop - posting_start - start;
        let row_count = available.min(max_rows as usize);
        let mut rows = Vec::new();
        rows.try_reserve_exact(row_count)
            .map_err(|_| NativeError::limit("retained axiom-type page allocation failed"))?;
        let outer_bytes = rows
            .capacity()
            .checked_mul(size_of::<Vec<u8>>())
            .ok_or_else(|| NativeError::limit("retained axiom-type page size overflow"))?;
        let retained_index_bytes = usize::try_from(self.counters.retained_buffer_bytes)
            .map_err(|_| NativeError::limit("retained axiom-type buffers exceed usize"))?;
        let mut payload_bytes = 0_u64;
        let mut retained_payload_bytes = 0_usize;
        for position in posting_start + start..posting_start + start + row_count {
            cancellation.checkpoint()?;
            let ordinal = usize::try_from(self.postings[position])
                .map_err(|_| NativeError::limit("retained axiom-type posting exceeds usize"))?;
            let identifier = self.roots.get(ordinal).copied().ok_or_else(|| {
                NativeError::protocol("retained axiom-type posting is out of bounds")
            })?;
            let row_bytes = *self.canonical_sizes.get(ordinal).ok_or_else(|| {
                NativeError::protocol("retained axiom-type canonical size is absent")
            })?;
            if !rows.is_empty()
                && payload_bytes
                    .checked_add(row_bytes)
                    .is_none_or(|following| following > max_bytes)
            {
                break;
            }
            let external_bytes = self
                .caller_external_bytes
                .checked_add(retained_index_bytes)
                .and_then(|value| value.checked_add(outer_bytes))
                .and_then(|value| value.checked_add(retained_payload_bytes))
                .ok_or_else(|| NativeError::limit("retained axiom-type page memory overflow"))?;
            let row = self.owner.encode(
                identifier,
                limits,
                cancellation.clone(),
                interrupt.clone(),
                external_bytes,
            )?;
            if u64::try_from(row.len()) != Ok(row_bytes) {
                return Err(NativeError::protocol(
                    "retained axiom-type canonical row size drifted",
                ));
            }
            retained_payload_bytes = retained_payload_bytes
                .checked_add(row.capacity())
                .ok_or_else(|| NativeError::limit("retained axiom-type payload overflow"))?;
            let temporary_bytes = outer_bytes
                .checked_add(retained_payload_bytes)
                .ok_or_else(|| NativeError::limit("retained axiom-type page memory overflow"))?;
            let observed = u64::try_from(temporary_bytes)
                .map_err(|_| NativeError::limit("retained axiom-type page memory exceeds u64"))?;
            if observed > limits.value(LimitKey::MaxTemporaryBytes) {
                return Err(limits.resource_limit(
                    LimitKey::MaxTemporaryBytes,
                    observed,
                    "retained axiom-type page exceeds max_temporary_bytes",
                ));
            }
            payload_bytes = payload_bytes
                .checked_add(row_bytes)
                .ok_or_else(|| NativeError::limit("retained axiom-type page bytes overflow"))?;
            rows.push(row);
        }
        cancellation.checkpoint()?;
        let emitted = u64::try_from(rows.len())
            .map_err(|_| NativeError::limit("retained axiom-type emitted rows exceed u64"))?;
        self.complete_root_encode_calls
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
                current.checked_add(emitted)
            })
            .map_err(|_| NativeError::limit("retained axiom-type encode counter overflow"))?;
        let end = u64::try_from(start)
            .ok()
            .and_then(|value| value.checked_add(emitted))
            .ok_or_else(|| NativeError::limit("retained axiom-type page cursor overflow"))?;
        Ok(RetainedAxiomTypePageV1 {
            total_count,
            next_cursor: (end != total_count).then_some(end),
            rows,
        })
    }
}

pub(crate) fn build_retained_axiom_type_index_v1(
    arena: &NativeComponentArena,
    roots: &[ComponentId],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<RetainedAxiomTypeIndexV1> {
    let size_cancellation = cancellation.clone();
    let size_interrupt = interrupt.clone();
    let root_rows = u64::try_from(roots.len())
        .map_err(|_| NativeError::limit("retained axiom-type row count exceeds u64"))?;
    if root_rows > limits.max_axioms {
        return Err(limits.resource_limit(
            LimitKey::MaxAxioms,
            root_rows,
            "retained axiom-type rows exceed max_axioms",
        ));
    }
    if root_rows > limits.value(LimitKey::MaxIndexRows) {
        return Err(limits.resource_limit(
            LimitKey::MaxIndexRows,
            root_rows,
            "retained axiom-type rows exceed max_index_rows",
        ));
    }
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
    let mut work = 0_u64;
    let mut groups = 0_usize;
    let mut category_groups = 0_usize;
    let mut previous = None;
    let mut previous_category = None;
    for identifier in roots {
        step(&mut guard, &mut work)?;
        if arena.category(*identifier)? != Category::Axiom {
            return Err(NativeError::protocol(
                "retained axiom-type index received a non-axiom root",
            ));
        }
        let tag = arena.tag(*identifier)?;
        let category = axiom_category_code(tag).ok_or_else(|| {
            NativeError::protocol("retained axiom-type index received a non-root axiom constructor")
        })?;
        if previous.is_some_and(|prior| tag < prior) {
            return Err(NativeError::protocol(
                "retained axiom-type roots are not canonical by constructor",
            ));
        }
        if previous != Some(tag) {
            groups = groups
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("retained axiom-type group count overflow"))?;
            previous = Some(tag);
        }
        if previous_category.is_some_and(|prior| category < prior) {
            return Err(NativeError::protocol(
                "retained axiom-type roots are not canonical by category",
            ));
        }
        if previous_category != Some(category) {
            category_groups = category_groups
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("retained axiom-type category count overflow"))?;
            previous_category = Some(category);
        }
    }
    let offset_count = groups
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("retained axiom-type offset count overflow"))?;
    let category_offset_count = category_groups
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("retained axiom-type category offset overflow"))?;
    let minimum_buffer_bytes = retained_buffer_bytes(
        groups,
        offset_count,
        category_groups,
        category_offset_count,
        roots.len(),
        roots.len(),
        roots.len(),
    )?;
    check_memory(arena, caller_external_bytes, minimum_buffer_bytes, limits)?;
    work = work
        .checked_add(
            u64::try_from(minimum_buffer_bytes)
                .map_err(|_| NativeError::limit("retained axiom-type bytes exceed u64"))?,
        )
        .ok_or_else(|| NativeError::limit("retained axiom-type work overflow"))?;
    let mut tags = Vec::new();
    tags.try_reserve_exact(groups)
        .map_err(|_| NativeError::limit("retained axiom-type tag allocation failed"))?;
    let mut offsets = Vec::new();
    offsets
        .try_reserve_exact(offset_count)
        .map_err(|_| NativeError::limit("retained axiom-type offset allocation failed"))?;
    let mut category_codes = Vec::new();
    category_codes
        .try_reserve_exact(category_groups)
        .map_err(|_| NativeError::limit("retained axiom-type category allocation failed"))?;
    let mut category_offsets = Vec::new();
    category_offsets
        .try_reserve_exact(category_offset_count)
        .map_err(|_| NativeError::limit("retained axiom-type category offset allocation failed"))?;
    let mut postings = Vec::new();
    postings
        .try_reserve_exact(roots.len())
        .map_err(|_| NativeError::limit("retained axiom-type posting allocation failed"))?;
    let mut retained_roots = Vec::new();
    retained_roots
        .try_reserve_exact(roots.len())
        .map_err(|_| NativeError::limit("retained axiom-type root allocation failed"))?;
    retained_roots.extend_from_slice(roots);
    let mut canonical_sizes = Vec::new();
    canonical_sizes
        .try_reserve_exact(roots.len())
        .map_err(|_| NativeError::limit("retained axiom-type size allocation failed"))?;
    let buffer_bytes = retained_buffer_bytes(
        tags.capacity(),
        offsets.capacity(),
        category_codes.capacity(),
        category_offsets.capacity(),
        postings.capacity(),
        retained_roots.capacity(),
        canonical_sizes.capacity(),
    )?;
    check_memory(arena, caller_external_bytes, buffer_bytes, limits)?;
    let allocation_slack = buffer_bytes
        .checked_sub(minimum_buffer_bytes)
        .ok_or_else(|| NativeError::protocol("retained axiom-type allocation underflow"))?;
    work = work
        .checked_add(
            u64::try_from(allocation_slack)
                .map_err(|_| NativeError::limit("retained axiom-type slack exceeds u64"))?,
        )
        .ok_or_else(|| NativeError::limit("retained axiom-type work overflow"))?;
    offsets.push(0);
    category_offsets.push(0);
    previous = None;
    previous_category = None;
    for (ordinal, identifier) in roots.iter().copied().enumerate() {
        step(&mut guard, &mut work)?;
        canonical_sizes.push(
            u64::try_from(
                arena.encoded_len(
                    identifier,
                    limits,
                    size_cancellation.clone(),
                    size_interrupt.clone(),
                    caller_external_bytes
                        .checked_add(buffer_bytes)
                        .ok_or_else(|| {
                            NativeError::limit("retained axiom-type size memory overflow")
                        })?,
                )?,
            )
            .map_err(|_| NativeError::limit("retained axiom-type row size exceeds u64"))?,
        );
        let tag = arena.tag(identifier)?;
        let category = axiom_category_code(tag).ok_or_else(|| {
            NativeError::protocol("retained axiom-type category changed during construction")
        })?;
        if previous != Some(tag) {
            if previous.is_some() {
                offsets.push(u64::try_from(postings.len()).map_err(|_| {
                    NativeError::limit("retained axiom-type posting offset exceeds u64")
                })?);
            }
            tags.push(tag);
            previous = Some(tag);
        }
        if previous_category != Some(category) {
            if previous_category.is_some() {
                category_offsets.push(u64::try_from(postings.len()).map_err(|_| {
                    NativeError::limit("retained axiom-type category offset exceeds u64")
                })?);
            }
            category_codes.push(category);
            previous_category = Some(category);
        }
        postings.push(
            u64::try_from(ordinal)
                .map_err(|_| NativeError::limit("retained axiom-type ordinal exceeds u64"))?,
        );
    }
    if !tags.is_empty() {
        offsets.push(
            u64::try_from(postings.len()).map_err(|_| {
                NativeError::limit("retained axiom-type posting offset exceeds u64")
            })?,
        );
    }
    if !category_codes.is_empty() {
        category_offsets.push(
            u64::try_from(postings.len()).map_err(|_| {
                NativeError::limit("retained axiom-type category offset exceeds u64")
            })?,
        );
    }
    if tags.len() != groups
        || offsets.len() != offset_count
        || category_codes.len() != category_groups
        || category_offsets.len() != category_offset_count
        || postings.len() != roots.len()
        || retained_roots.len() != roots.len()
        || canonical_sizes.len() != roots.len()
        || canonical_sizes.contains(&0)
    {
        return Err(NativeError::protocol(
            "retained axiom-type layout accounting drifted",
        ));
    }
    guard.check(work, true)?;

    let retained_buffer_bytes = u64::try_from(buffer_bytes)
        .map_err(|_| NativeError::limit("retained axiom-type bytes exceed u64"))?;
    let peak_owned_bytes = arena
        .counters()
        .retained_bytes
        .checked_add(retained_buffer_bytes)
        .ok_or_else(|| NativeError::limit("retained axiom-type memory overflow"))?;
    let constructor_groups = u64::try_from(groups)
        .map_err(|_| NativeError::limit("retained axiom-type group count exceeds u64"))?;
    let category_groups = u64::try_from(category_groups)
        .map_err(|_| NativeError::limit("retained axiom-type category count exceeds u64"))?;
    Ok(RetainedAxiomTypeIndexV1 {
        owner: arena.clone(),
        roots: retained_roots,
        tags,
        offsets,
        category_codes,
        category_offsets,
        postings,
        canonical_sizes,
        caller_external_bytes,
        complete_root_encode_calls: AtomicU64::new(0),
        counters: RetainedAxiomTypeIndexCountersV1 {
            axiom_rows: root_rows,
            constructor_groups,
            category_groups,
            retained_buffer_bytes,
            peak_owned_bytes,
            canonical_work: work,
            complete_root_encode_calls: 0,
        },
    })
}

fn step(guard: &mut Guard, work: &mut u64) -> NativeResult<()> {
    *work = work
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("retained axiom-type work overflow"))?;
    guard.check(*work, false)
}

fn axiom_category_code(tag: u16) -> Option<u8> {
    match tag {
        60 => Some(AXIOM_CATEGORY_DECLARATION_V1),
        61..=64 | 70..=82 | 90..=95 | 100..=101 | 110..=116 => Some(AXIOM_CATEGORY_LOGICAL_V1),
        120..=123 => Some(AXIOM_CATEGORY_ANNOTATION_V1),
        _ => None,
    }
}

fn retained_buffer_bytes(
    tags: usize,
    offsets: usize,
    categories: usize,
    category_offsets: usize,
    postings: usize,
    roots: usize,
    canonical_sizes: usize,
) -> NativeResult<usize> {
    tags.checked_mul(size_of::<u16>())
        .and_then(|value| {
            offsets
                .checked_mul(size_of::<u64>())
                .and_then(|part| value.checked_add(part))
        })
        .and_then(|value| value.checked_add(categories))
        .and_then(|value| {
            category_offsets
                .checked_mul(size_of::<u64>())
                .and_then(|part| value.checked_add(part))
        })
        .and_then(|value| {
            postings
                .checked_mul(size_of::<u64>())
                .and_then(|part| value.checked_add(part))
        })
        .and_then(|value| {
            roots
                .checked_mul(size_of::<ComponentId>())
                .and_then(|part| value.checked_add(part))
        })
        .and_then(|value| {
            canonical_sizes
                .checked_mul(size_of::<u64>())
                .and_then(|part| value.checked_add(part))
        })
        .ok_or_else(|| NativeError::limit("retained axiom-type buffer size overflow"))
}

fn check_memory(
    arena: &NativeComponentArena,
    caller_external_bytes: usize,
    buffer_bytes: usize,
    limits: &Limits,
) -> NativeResult<()> {
    let buffer_bytes = u64::try_from(buffer_bytes)
        .map_err(|_| NativeError::limit("retained axiom-type bytes exceed u64"))?;
    if buffer_bytes > limits.value(LimitKey::MaxIndexBytes) {
        return Err(limits.resource_limit(
            LimitKey::MaxIndexBytes,
            buffer_bytes,
            "retained axiom-type buffers exceed max_index_bytes",
        ));
    }
    let caller_external_bytes = u64::try_from(caller_external_bytes)
        .map_err(|_| NativeError::limit("retained axiom-type caller bytes exceed u64"))?;
    let peak = arena
        .counters()
        .retained_bytes
        .checked_add(caller_external_bytes)
        .and_then(|value| value.checked_add(buffer_bytes))
        .ok_or_else(|| NativeError::limit("retained axiom-type memory overflow"))?;
    if let Some(maximum) = limits.max_memory_bytes.filter(|maximum| peak > *maximum) {
        return Err(NativeError::resource_limit(
            "max_memory_bytes",
            peak,
            maximum,
            "retained axiom-type build exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::NativeComponentBuilder;

    fn varint(mut value: usize) -> Vec<u8> {
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
        let mut output = varint(value.len());
        output.extend_from_slice(value);
        output
    }

    fn iri(value: &str) -> Vec<u8> {
        let mut output = vec![1, 2];
        output.extend(frame(value.as_bytes()));
        output
    }

    fn entity(value: &str) -> Vec<u8> {
        let iri = iri(value);
        let mut output = vec![2, 5];
        output.extend(frame(b"class"));
        output.push(1);
        output.extend(frame(&iri));
        output
    }

    fn declaration(value: &str) -> Vec<u8> {
        let entity = entity(value);
        let mut output = vec![60, 1];
        output.extend(frame(&entity));
        output.extend([6, 0]);
        output
    }

    fn subclass(sub: &str, sup: &str) -> Vec<u8> {
        let sub = entity(sub);
        let sup = entity(sup);
        let mut output = vec![61, 1];
        output.extend(frame(&sub));
        output.push(1);
        output.extend(frame(&sup));
        output.extend([6, 0]);
        output
    }

    fn arena(rows: &[Vec<u8>]) -> (NativeComponentArena, Vec<ComponentId>) {
        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let pending: Vec<_> = rows
            .iter()
            .map(|row| builder.intern_canonical(row).expect("root"))
            .collect();
        let frozen = builder.freeze().expect("freeze");
        let roots = pending
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("root ID"))
            .collect();
        (frozen.into_arena(), roots)
    }

    #[test]
    fn retained_index_groups_constructor_postings_without_root_encodes() {
        let rows = vec![
            declaration("urn:a"),
            declaration("urn:b"),
            subclass("urn:a", "urn:b"),
        ];
        let (arena, roots) = arena(&rows);
        let index = build_retained_axiom_type_index_v1(
            &arena,
            &roots,
            &Limits::default(),
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("retained index");

        assert_eq!(index.tags(), [60, 61]);
        assert_eq!(index.offsets(), [0, 2, 3]);
        assert_eq!(index.category_codes(), [1, 2]);
        assert_eq!(index.category_offsets(), [0, 2, 3]);
        assert_eq!(index.postings(), [0, 1, 2]);
        assert!(index.owner().shares_storage_with(&arena));
        assert_eq!(index.counters().axiom_rows, 3);
        assert_eq!(index.counters().constructor_groups, 2);
        assert_eq!(index.counters().category_groups, 2);
        assert_eq!(index.counters().complete_root_encode_calls, 0);
        assert_eq!(
            index.counters().retained_buffer_bytes,
            u64::try_from(
                index.tags.capacity() * size_of::<u16>()
                    + index.offsets.capacity() * size_of::<u64>()
                    + index.category_codes.capacity()
                    + index.category_offsets.capacity() * size_of::<u64>()
                    + index.postings.capacity() * size_of::<u64>()
                    + index.roots.capacity() * size_of::<ComponentId>()
                    + index.canonical_sizes.capacity() * size_of::<u64>()
            )
            .expect("allocated byte count")
        );
    }

    #[test]
    fn retained_index_pages_only_the_selected_constructor_with_exact_cursors() {
        let rows = vec![
            declaration("urn:a"),
            declaration("urn:b"),
            subclass("urn:a", "urn:b"),
        ];
        let (arena, roots) = arena(&rows);
        let index = build_retained_axiom_type_index_v1(
            &arena,
            &roots,
            &Limits::default(),
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("retained index");

        assert_eq!(
            index.canonical_sizes(),
            rows.iter()
                .map(|row| u64::try_from(row.len()).expect("row size"))
                .collect::<Vec<_>>()
        );
        let first = index
            .constructor_page(
                60,
                0,
                1,
                8 * 1024 * 1024,
                &Limits::default(),
                Cancellation::with_duration(None),
                None,
            )
            .expect("first page");
        assert_eq!(first.rows, rows[..1]);
        assert_eq!(first.total_count, 2);
        assert_eq!(first.next_cursor, Some(1));
        assert_eq!(index.complete_root_encode_calls(), 1);

        let second = index
            .constructor_page(
                60,
                1,
                64,
                8 * 1024 * 1024,
                &Limits::default(),
                Cancellation::with_duration(None),
                None,
            )
            .expect("second page");
        assert_eq!(second.rows, rows[1..2]);
        assert_eq!(second.total_count, 2);
        assert_eq!(second.next_cursor, None);
        assert_eq!(index.complete_root_encode_calls(), 2);

        let absent = index
            .constructor_page(
                120,
                0,
                64,
                8 * 1024 * 1024,
                &Limits::default(),
                Cancellation::with_duration(None),
                None,
            )
            .expect("absent constructor");
        assert!(absent.rows.is_empty());
        assert_eq!(absent.total_count, 0);
        assert_eq!(absent.next_cursor, None);
        assert_eq!(index.complete_root_encode_calls(), 2);

        assert_eq!(
            index
                .constructor_page(
                    60,
                    3,
                    64,
                    8 * 1024 * 1024,
                    &Limits::default(),
                    Cancellation::with_duration(None),
                    None,
                )
                .unwrap_err()
                .code,
            "NATIVE_PROTOCOL"
        );
        assert_eq!(
            index
                .constructor_page(
                    60,
                    0,
                    65,
                    8 * 1024 * 1024,
                    &Limits::default(),
                    Cancellation::with_duration(None),
                    None,
                )
                .unwrap_err()
                .code,
            "NATIVE_PROTOCOL"
        );
        assert_eq!(
            index
                .constructor_page(
                    61,
                    0,
                    64,
                    8 * 1024 * 1024,
                    &Limits::default(),
                    Cancellation::with_duration(Some(std::time::Duration::ZERO)),
                    None,
                )
                .unwrap_err()
                .code,
            "NATIVE_DEADLINE"
        );
        assert_eq!(index.complete_root_encode_calls(), 2);
    }

    #[test]
    fn axiom_category_table_covers_only_the_complete_root_ledger() {
        assert_eq!(axiom_category_code(60), Some(1));
        for tag in 61..=64 {
            assert_eq!(axiom_category_code(tag), Some(2));
        }
        for tag in 70..=82 {
            assert_eq!(axiom_category_code(tag), Some(2));
        }
        for tag in 90..=95 {
            assert_eq!(axiom_category_code(tag), Some(2));
        }
        for tag in 100..=101 {
            assert_eq!(axiom_category_code(tag), Some(2));
        }
        for tag in 110..=116 {
            assert_eq!(axiom_category_code(tag), Some(2));
        }
        for tag in 120..=123 {
            assert_eq!(axiom_category_code(tag), Some(3));
        }
        for tag in [
            0, 5, 59, 65, 69, 83, 89, 96, 99, 102, 109, 117, 119, 124, 148,
        ] {
            assert_eq!(axiom_category_code(tag), None);
        }
    }

    #[test]
    fn retained_index_enforces_order_memory_and_cancellation() {
        let rows = vec![declaration("urn:a"), subclass("urn:a", "urn:b")];
        let (arena, roots) = arena(&rows);
        let baseline = build_retained_axiom_type_index_v1(
            &arena,
            &roots,
            &Limits::default(),
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("baseline");

        let mut memory = Limits::default();
        memory.max_memory_bytes = Some(baseline.counters().peak_owned_bytes - 1);
        assert_eq!(
            build_retained_axiom_type_index_v1(
                &arena,
                &roots,
                &memory,
                Cancellation::with_duration(None),
                None,
                0,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        let mut count = Limits::default();
        count.max_axioms = 1;
        assert_eq!(
            build_retained_axiom_type_index_v1(
                &arena,
                &roots,
                &count,
                Cancellation::with_duration(None),
                None,
                0,
            )
            .expect_err("retained index row counts remain bounded")
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        let mut progress = Limits::default();
        progress.max_canonical_work = baseline
            .canonical_sizes()
            .iter()
            .copied()
            .max()
            .expect("canonical row size");
        let progress_index = build_retained_axiom_type_index_v1(
            &arena,
            &roots,
            &progress,
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("whole-index traversal is progress, not per-row canonical work");
        assert!(progress_index.counters().canonical_work > progress.max_canonical_work);
        assert_eq!(
            build_retained_axiom_type_index_v1(
                &arena,
                &roots,
                &Limits::default(),
                Cancellation::with_duration(Some(std::time::Duration::ZERO)),
                None,
                0,
            )
            .unwrap_err()
            .code,
            "NATIVE_DEADLINE"
        );

        let reversed = [roots[1], roots[0]];
        assert_eq!(
            build_retained_axiom_type_index_v1(
                &arena,
                &reversed,
                &Limits::default(),
                Cancellation::with_duration(None),
                None,
                0,
            )
            .unwrap_err()
            .code,
            "NATIVE_PROTOCOL"
        );
    }
}
