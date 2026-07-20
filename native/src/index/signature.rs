//! Exact signature contribution counts over retained component identifiers.

use std::collections::{HashMap, HashSet};
use std::hash::Hash;
use std::mem::size_of;

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{Category, ComponentFieldRef, ComponentId, NativeComponentArena};

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct RetainedSignatureIndexCountersV1 {
    pub(crate) structural_root_rows: u64,
    pub(crate) entity_rows: u64,
    pub(crate) referenced_links: u64,
    pub(crate) nonannotation_links: u64,
    pub(crate) declaration_links: u64,
    pub(crate) retained_buffer_bytes: u64,
    pub(crate) peak_owned_bytes: u64,
    pub(crate) canonical_work: u64,
    pub(crate) complete_root_encode_calls: u64,
}

#[derive(Debug)]
pub(crate) struct RetainedSignatureIndexV1 {
    owner: NativeComponentArena,
    referenced_counts: Vec<u64>,
    nonannotation_counts: Vec<u64>,
    declaration_counts: Vec<u64>,
    counters: RetainedSignatureIndexCountersV1,
}

impl RetainedSignatureIndexV1 {
    pub(crate) const fn owner(&self) -> &NativeComponentArena {
        &self.owner
    }

    pub(crate) fn referenced_counts(&self) -> &[u64] {
        &self.referenced_counts
    }

    pub(crate) fn nonannotation_counts(&self) -> &[u64] {
        &self.nonannotation_counts
    }

    pub(crate) fn declaration_counts(&self) -> &[u64] {
        &self.declaration_counts
    }

    pub(crate) const fn counters(&self) -> &RetainedSignatureIndexCountersV1 {
        &self.counters
    }
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn build_retained_signature_index_v1(
    arena: &NativeComponentArena,
    entities: &[ComponentId],
    ontology_annotations: &[ComponentId],
    axioms: &[ComponentId],
    extensions: &[ComponentId],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<RetainedSignatureIndexV1> {
    let structural_root_rows = ontology_annotations
        .len()
        .checked_add(axioms.len())
        .and_then(|value| value.checked_add(extensions.len()))
        .ok_or_else(|| NativeError::limit("retained signature root count overflow"))?;
    if entities.len() > usize::try_from(limits.value(LimitKey::MaxIndexRows)).unwrap_or(usize::MAX)
    {
        return Err(NativeError::limit(
            "retained signature entities exceed max_index_rows",
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

    let retained_minimum = retained_buffer_bytes(entities.len(), entities.len(), entities.len())?;
    check_memory(arena, caller_external_bytes, retained_minimum, 0, limits)?;
    let mut referenced_counts = Vec::new();
    let mut nonannotation_counts = Vec::new();
    let mut declaration_counts = Vec::new();
    for values in [
        &mut referenced_counts,
        &mut nonannotation_counts,
        &mut declaration_counts,
    ] {
        values
            .try_reserve_exact(entities.len())
            .map_err(|_| NativeError::limit("retained signature count allocation failed"))?;
        values.resize(entities.len(), 0_u64);
    }
    let retained_bytes = retained_buffer_bytes(
        referenced_counts.capacity(),
        nonannotation_counts.capacity(),
        declaration_counts.capacity(),
    )?;
    check_memory(arena, caller_external_bytes, retained_bytes, 0, limits)?;

    let mut ordinals = HashMap::new();
    let ordinal_minimum = hash_map_bytes::<ComponentId, usize>(entities.len())?;
    check_memory(
        arena,
        caller_external_bytes,
        retained_bytes,
        ordinal_minimum,
        limits,
    )?;
    ordinals
        .try_reserve(entities.len())
        .map_err(|_| NativeError::limit("retained signature ordinal allocation failed"))?;
    let mut work = 0_u64;
    for (ordinal, identifier) in entities.iter().copied().enumerate() {
        step(&mut guard, &mut work, limits)?;
        if arena.category(identifier)? != Category::Entity
            || ordinals.insert(identifier, ordinal).is_some()
        {
            return Err(NativeError::protocol(
                "retained signature entity table is invalid",
            ));
        }
    }
    let ordinal_bytes = hash_map_bytes::<ComponentId, usize>(ordinals.capacity())?;
    check_memory(
        arena,
        caller_external_bytes,
        retained_bytes,
        ordinal_bytes,
        limits,
    )?;

    let mut found = HashSet::new();
    let mut stack = Vec::new();
    for root in ontology_annotations {
        if arena.category(*root)? != Category::Annotation {
            return Err(NativeError::protocol(
                "retained signature annotation table contains a foreign category",
            ));
        }
        increment_root_entities(
            arena,
            *root,
            true,
            &ordinals,
            &mut referenced_counts,
            &mut found,
            &mut stack,
            &mut guard,
            &mut work,
            limits,
            caller_external_bytes,
            retained_bytes,
            ordinal_bytes,
        )?;
    }
    for root in axioms {
        if arena.category(*root)? != Category::Axiom {
            return Err(NativeError::protocol(
                "retained signature axiom table contains a foreign category",
            ));
        }
        increment_root_entities(
            arena,
            *root,
            true,
            &ordinals,
            &mut referenced_counts,
            &mut found,
            &mut stack,
            &mut guard,
            &mut work,
            limits,
            caller_external_bytes,
            retained_bytes,
            ordinal_bytes,
        )?;
        let tag = arena.tag(*root)?;
        if !matches!(tag, 120..=123) {
            increment_root_entities(
                arena,
                *root,
                false,
                &ordinals,
                &mut nonannotation_counts,
                &mut found,
                &mut stack,
                &mut guard,
                &mut work,
                limits,
                caller_external_bytes,
                retained_bytes,
                ordinal_bytes,
            )?;
        }
        if tag == 60 {
            let entity = match arena.record(*root)?.field(0)? {
                ComponentFieldRef::Node(identifier)
                    if arena.category(identifier)? == Category::Entity =>
                {
                    identifier
                }
                _ => {
                    return Err(NativeError::protocol(
                        "retained signature declaration has an invalid entity field",
                    ));
                }
            };
            let ordinal = ordinals.get(&entity).copied().ok_or_else(|| {
                NativeError::protocol("retained signature declaration entity is absent")
            })?;
            increment(&mut declaration_counts[ordinal])?;
        }
    }
    for root in extensions {
        if arena.category(*root)? != Category::Swrl {
            return Err(NativeError::protocol(
                "retained signature extension table contains a foreign category",
            ));
        }
        increment_root_entities(
            arena,
            *root,
            true,
            &ordinals,
            &mut referenced_counts,
            &mut found,
            &mut stack,
            &mut guard,
            &mut work,
            limits,
            caller_external_bytes,
            retained_bytes,
            ordinal_bytes,
        )?;
        increment_root_entities(
            arena,
            *root,
            false,
            &ordinals,
            &mut nonannotation_counts,
            &mut found,
            &mut stack,
            &mut guard,
            &mut work,
            limits,
            caller_external_bytes,
            retained_bytes,
            ordinal_bytes,
        )?;
    }
    guard.check(work, true)?;

    if referenced_counts.contains(&0)
        || referenced_counts
            .iter()
            .zip(&nonannotation_counts)
            .any(|(referenced, nonannotation)| nonannotation > referenced)
        || referenced_counts
            .iter()
            .zip(&declaration_counts)
            .any(|(referenced, declared)| declared > referenced)
    {
        return Err(NativeError::protocol(
            "retained signature counts diverge from the signature table",
        ));
    }

    let referenced_links = sum_counts(&referenced_counts)?;
    let nonannotation_links = sum_counts(&nonannotation_counts)?;
    let declaration_links = sum_counts(&declaration_counts)?;
    let retained_buffer_bytes = u64::try_from(retained_bytes)
        .map_err(|_| NativeError::limit("retained signature bytes exceed u64"))?;
    let peak_owned_bytes = arena
        .counters()
        .retained_bytes
        .checked_add(retained_buffer_bytes)
        .ok_or_else(|| NativeError::limit("retained signature memory overflow"))?;
    Ok(RetainedSignatureIndexV1 {
        owner: arena.clone(),
        referenced_counts,
        nonannotation_counts,
        declaration_counts,
        counters: RetainedSignatureIndexCountersV1 {
            structural_root_rows: u64::try_from(structural_root_rows)
                .map_err(|_| NativeError::limit("retained signature root count exceeds u64"))?,
            entity_rows: u64::try_from(entities.len())
                .map_err(|_| NativeError::limit("retained signature entity count exceeds u64"))?,
            referenced_links,
            nonannotation_links,
            declaration_links,
            retained_buffer_bytes,
            peak_owned_bytes,
            canonical_work: work,
            complete_root_encode_calls: 0,
        },
    })
}

#[allow(clippy::too_many_arguments)]
fn increment_root_entities(
    arena: &NativeComponentArena,
    root: ComponentId,
    include_annotations: bool,
    ordinals: &HashMap<ComponentId, usize>,
    counts: &mut [u64],
    found: &mut HashSet<ComponentId>,
    stack: &mut Vec<ComponentId>,
    guard: &mut Guard,
    work: &mut u64,
    limits: &Limits,
    caller_external_bytes: usize,
    retained_bytes: usize,
    ordinal_bytes: usize,
) -> NativeResult<()> {
    found.clear();
    stack.clear();
    stack
        .try_reserve(1)
        .map_err(|_| NativeError::limit("retained signature stack allocation failed"))?;
    stack.push(root);
    while let Some(identifier) = stack.pop() {
        step(guard, work, limits)?;
        let category = arena.category(identifier)?;
        if !include_annotations && category == Category::Annotation {
            continue;
        }
        if category == Category::Entity {
            reserve_hash_item(found, "retained signature entity-set allocation failed")?;
            found.insert(identifier);
            check_workspace(
                arena,
                found,
                stack.capacity(),
                limits,
                caller_external_bytes,
                retained_bytes,
                ordinal_bytes,
            )?;
            continue;
        }
        let record = arena.record(identifier)?;
        for index in 0..record.field_count() {
            push_field_nodes(record.field(index)?, stack, guard, work, limits)?;
        }
        check_workspace(
            arena,
            found,
            stack.capacity(),
            limits,
            caller_external_bytes,
            retained_bytes,
            ordinal_bytes,
        )?;
    }
    for identifier in &*found {
        let ordinal = ordinals.get(identifier).copied().ok_or_else(|| {
            NativeError::protocol("retained signature traversal found an unindexed entity")
        })?;
        increment(&mut counts[ordinal])?;
    }
    Ok(())
}

fn check_workspace(
    arena: &NativeComponentArena,
    found: &HashSet<ComponentId>,
    stack_capacity: usize,
    limits: &Limits,
    caller_external_bytes: usize,
    retained_bytes: usize,
    ordinal_bytes: usize,
) -> NativeResult<()> {
    let temporary_bytes = ordinal_bytes
        .checked_add(hash_set_bytes::<ComponentId>(found.capacity())?)
        .and_then(|value| {
            stack_capacity
                .checked_mul(size_of::<ComponentId>())
                .and_then(|part| value.checked_add(part))
        })
        .ok_or_else(|| NativeError::limit("retained signature workspace overflow"))?;
    check_memory(
        arena,
        caller_external_bytes,
        retained_bytes,
        temporary_bytes,
        limits,
    )
}

fn push_field_nodes(
    field: ComponentFieldRef<'_>,
    stack: &mut Vec<ComponentId>,
    guard: &mut Guard,
    work: &mut u64,
    limits: &Limits,
) -> NativeResult<()> {
    step(guard, work, limits)?;
    match field {
        ComponentFieldRef::Node(identifier) => {
            stack
                .try_reserve(1)
                .map_err(|_| NativeError::limit("retained signature stack allocation failed"))?;
            stack.push(identifier);
        }
        ComponentFieldRef::CanonicalSet(sequence)
        | ComponentFieldRef::OrderedSequence(sequence) => {
            for index in 0..sequence.len() {
                push_field_nodes(sequence.item(index)?, stack, guard, work, limits)?;
            }
        }
        ComponentFieldRef::None
        | ComponentFieldRef::Text(_)
        | ComponentFieldRef::Bytes(_)
        | ComponentFieldRef::NonnegativeIntegerVarint(_)
        | ComponentFieldRef::Enum(_) => {}
    }
    Ok(())
}

fn reserve_hash_item<T: Eq + Hash>(
    values: &mut HashSet<T>,
    message: &'static str,
) -> NativeResult<()> {
    if values.len() == values.capacity() {
        values
            .try_reserve(1)
            .map_err(|_| NativeError::limit(message))?;
    }
    Ok(())
}

fn increment(value: &mut u64) -> NativeResult<()> {
    *value = value
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("retained signature contribution count overflow"))?;
    Ok(())
}

fn sum_counts(values: &[u64]) -> NativeResult<u64> {
    values.iter().try_fold(0_u64, |total, value| {
        total
            .checked_add(*value)
            .ok_or_else(|| NativeError::limit("retained signature link count overflow"))
    })
}

fn step(guard: &mut Guard, work: &mut u64, limits: &Limits) -> NativeResult<()> {
    *work = work
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("retained signature work overflow"))?;
    if *work > limits.max_canonical_work {
        return Err(NativeError::limit(
            "retained signature build exceeds max_canonical_work",
        ));
    }
    guard.check(*work, false)
}

fn retained_buffer_bytes(
    referenced: usize,
    nonannotation: usize,
    declarations: usize,
) -> NativeResult<usize> {
    referenced
        .checked_add(nonannotation)
        .and_then(|value| value.checked_add(declarations))
        .and_then(|value| value.checked_mul(size_of::<u64>()))
        .ok_or_else(|| NativeError::limit("retained signature buffer size overflow"))
}

fn hash_map_bytes<K, V>(capacity: usize) -> NativeResult<usize> {
    capacity
        .checked_mul(size_of::<(K, V)>())
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| NativeError::limit("retained signature map size overflow"))
}

fn hash_set_bytes<T>(capacity: usize) -> NativeResult<usize> {
    capacity
        .checked_mul(size_of::<T>())
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| NativeError::limit("retained signature set size overflow"))
}

fn check_memory(
    arena: &NativeComponentArena,
    caller_external_bytes: usize,
    retained_bytes: usize,
    temporary_bytes: usize,
    limits: &Limits,
) -> NativeResult<()> {
    let retained_bytes = u64::try_from(retained_bytes)
        .map_err(|_| NativeError::limit("retained signature bytes exceed u64"))?;
    let temporary_bytes = u64::try_from(temporary_bytes)
        .map_err(|_| NativeError::limit("retained signature workspace exceeds u64"))?;
    if retained_bytes > limits.value(LimitKey::MaxIndexBytes) {
        return Err(NativeError::limit(
            "retained signature buffers exceed max_index_bytes",
        ));
    }
    if temporary_bytes > limits.value(LimitKey::MaxTemporaryBytes) {
        return Err(NativeError::limit(
            "retained signature build exceeds max_temporary_bytes",
        ));
    }
    let caller_external_bytes = u64::try_from(caller_external_bytes)
        .map_err(|_| NativeError::limit("retained signature caller bytes exceed u64"))?;
    let peak = arena
        .counters()
        .retained_bytes
        .checked_add(caller_external_bytes)
        .and_then(|value| value.checked_add(retained_bytes))
        .and_then(|value| value.checked_add(temporary_bytes))
        .ok_or_else(|| NativeError::limit("retained signature memory overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| peak > maximum)
    {
        return Err(NativeError::limit(
            "retained signature build exceeds max_memory_bytes",
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

    fn fixture() -> (NativeComponentArena, Vec<ComponentId>, Vec<ComponentId>) {
        let limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&limits).expect("builder");
        let pending_entities = [entity("urn:a"), entity("urn:b")]
            .iter()
            .map(|row| builder.intern_canonical(row).expect("entity"))
            .collect::<Vec<_>>();
        let pending_axioms = [declaration("urn:a"), subclass("urn:a", "urn:b")]
            .iter()
            .map(|row| builder.intern_canonical(row).expect("axiom"))
            .collect::<Vec<_>>();
        let frozen = builder.freeze().expect("freeze");
        let mut entities = pending_entities
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("entity ID"))
            .collect::<Vec<_>>();
        let axioms = pending_axioms
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("axiom ID"))
            .collect::<Vec<_>>();
        let arena = frozen.into_arena();
        entities.sort_unstable_by_key(|identifier| {
            arena
                .dense_rank_in_category(*identifier)
                .expect("entity rank")
        });
        (arena, entities, axioms)
    }

    #[test]
    fn retained_signature_counts_each_entity_once_per_root_without_encoding() {
        let (arena, entities, axioms) = fixture();
        let index = build_retained_signature_index_v1(
            &arena,
            &entities,
            &[],
            &axioms,
            &[],
            &Limits::default(),
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("retained signature");

        assert_eq!(index.referenced_counts(), [2, 1]);
        assert_eq!(index.nonannotation_counts(), [2, 1]);
        assert_eq!(index.declaration_counts(), [1, 0]);
        assert!(index.owner().shares_storage_with(&arena));
        assert_eq!(index.counters().structural_root_rows, 2);
        assert_eq!(index.counters().entity_rows, 2);
        assert_eq!(index.counters().referenced_links, 3);
        assert_eq!(index.counters().nonannotation_links, 3);
        assert_eq!(index.counters().declaration_links, 1);
        assert_eq!(index.counters().complete_root_encode_calls, 0);
    }

    #[test]
    fn retained_signature_enforces_work_limit() {
        let (arena, entities, axioms) = fixture();
        let mut work_limit = Limits::default();
        work_limit.max_canonical_work = 1;
        assert_eq!(
            build_retained_signature_index_v1(
                &arena,
                &entities,
                &[],
                &axioms,
                &[],
                &work_limit,
                Cancellation::with_duration(None),
                None,
                0,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT"
        );
    }
}
