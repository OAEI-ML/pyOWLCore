//! Direct axiom-constructor postings over retained component identifiers.

use std::mem::size_of;

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{Category, ComponentId, NativeComponentArena};

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct RetainedAxiomTypeIndexCountersV1 {
    pub(crate) axiom_rows: u64,
    pub(crate) constructor_groups: u64,
    pub(crate) retained_buffer_bytes: u64,
    pub(crate) peak_owned_bytes: u64,
    pub(crate) canonical_work: u64,
    pub(crate) complete_root_encode_calls: u64,
}

#[derive(Debug)]
pub(crate) struct RetainedAxiomTypeIndexV1 {
    owner: NativeComponentArena,
    tags: Vec<u16>,
    offsets: Vec<u64>,
    postings: Vec<u64>,
    counters: RetainedAxiomTypeIndexCountersV1,
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

    pub(crate) const fn counters(&self) -> &RetainedAxiomTypeIndexCountersV1 {
        &self.counters
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
    let root_rows = u64::try_from(roots.len())
        .map_err(|_| NativeError::limit("retained axiom-type row count exceeds u64"))?;
    if root_rows > limits.max_axioms || root_rows > limits.value(LimitKey::MaxIndexRows) {
        return Err(NativeError::limit(
            "retained axiom-type rows exceed configured limits",
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
    let mut previous = None;
    for identifier in roots {
        step(&mut guard, &mut work, limits)?;
        if arena.category(*identifier)? != Category::Axiom {
            return Err(NativeError::protocol(
                "retained axiom-type index received a non-axiom root",
            ));
        }
        let tag = arena.tag(*identifier)?;
        if !axiom_root_tag(tag) {
            return Err(NativeError::protocol(
                "retained axiom-type index received a non-root axiom constructor",
            ));
        }
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
    }
    let offset_count = groups
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("retained axiom-type offset count overflow"))?;
    let minimum_buffer_bytes = retained_buffer_bytes(groups, offset_count, roots.len())?;
    check_memory(arena, caller_external_bytes, minimum_buffer_bytes, limits)?;
    work = work
        .checked_add(
            u64::try_from(minimum_buffer_bytes)
                .map_err(|_| NativeError::limit("retained axiom-type bytes exceed u64"))?,
        )
        .ok_or_else(|| NativeError::limit("retained axiom-type work overflow"))?;
    if work > limits.max_canonical_work {
        return Err(NativeError::limit(
            "retained axiom-type build exceeds max_canonical_work",
        ));
    }

    let mut tags = Vec::new();
    tags.try_reserve_exact(groups)
        .map_err(|_| NativeError::limit("retained axiom-type tag allocation failed"))?;
    let mut offsets = Vec::new();
    offsets
        .try_reserve_exact(offset_count)
        .map_err(|_| NativeError::limit("retained axiom-type offset allocation failed"))?;
    let mut postings = Vec::new();
    postings
        .try_reserve_exact(roots.len())
        .map_err(|_| NativeError::limit("retained axiom-type posting allocation failed"))?;
    let buffer_bytes =
        retained_buffer_bytes(tags.capacity(), offsets.capacity(), postings.capacity())?;
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
    if work > limits.max_canonical_work {
        return Err(NativeError::limit(
            "retained axiom-type build exceeds max_canonical_work",
        ));
    }
    offsets.push(0);
    previous = None;
    for (ordinal, identifier) in roots.iter().copied().enumerate() {
        step(&mut guard, &mut work, limits)?;
        let tag = arena.tag(identifier)?;
        if previous != Some(tag) {
            if previous.is_some() {
                offsets.push(u64::try_from(postings.len()).map_err(|_| {
                    NativeError::limit("retained axiom-type posting offset exceeds u64")
                })?);
            }
            tags.push(tag);
            previous = Some(tag);
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
    if tags.len() != groups || offsets.len() != offset_count || postings.len() != roots.len() {
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
    Ok(RetainedAxiomTypeIndexV1 {
        owner: arena.clone(),
        tags,
        offsets,
        postings,
        counters: RetainedAxiomTypeIndexCountersV1 {
            axiom_rows: root_rows,
            constructor_groups,
            retained_buffer_bytes,
            peak_owned_bytes,
            canonical_work: work,
            complete_root_encode_calls: 0,
        },
    })
}

fn step(guard: &mut Guard, work: &mut u64, limits: &Limits) -> NativeResult<()> {
    *work = work
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("retained axiom-type work overflow"))?;
    if *work > limits.max_canonical_work {
        return Err(NativeError::limit(
            "retained axiom-type build exceeds max_canonical_work",
        ));
    }
    guard.check(*work, false)
}

fn axiom_root_tag(tag: u16) -> bool {
    matches!(
        tag,
        60..=64 | 70..=82 | 90..=95 | 100..=101 | 110..=116 | 120..=123
    )
}

fn retained_buffer_bytes(tags: usize, offsets: usize, postings: usize) -> NativeResult<usize> {
    tags.checked_mul(size_of::<u16>())
        .and_then(|value| {
            offsets
                .checked_mul(size_of::<u64>())
                .and_then(|part| value.checked_add(part))
        })
        .and_then(|value| {
            postings
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
        return Err(NativeError::limit(
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
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| peak > maximum)
    {
        return Err(NativeError::limit(
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
        assert_eq!(index.postings(), [0, 1, 2]);
        assert!(index.owner().shares_storage_with(&arena));
        assert_eq!(index.counters().axiom_rows, 3);
        assert_eq!(index.counters().constructor_groups, 2);
        assert_eq!(index.counters().complete_root_encode_calls, 0);
        assert_eq!(
            index.counters().retained_buffer_bytes,
            u64::try_from(
                index.tags.capacity() * size_of::<u16>()
                    + index.offsets.capacity() * size_of::<u64>()
                    + index.postings.capacity() * size_of::<u64>()
            )
            .expect("allocated byte count")
        );
    }

    #[test]
    fn retained_index_enforces_order_memory_work_and_cancellation() {
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

        let mut work = Limits::default();
        work.max_canonical_work = 1;
        assert_eq!(
            build_retained_axiom_type_index_v1(
                &arena,
                &roots,
                &work,
                Cancellation::with_duration(None),
                None,
                0,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT"
        );
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
