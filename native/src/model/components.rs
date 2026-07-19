//! Recursively interned model-schema-1 component storage.
//!
//! The foundational arena retains validated canonical root rows.  This module
//! is the component-oriented path used by retained documents: nested values
//! are decoded once into category-specific dense tables and canonical bytes
//! are reconstructed only when a scalar facade asks for them.

use std::cmp::Ordering;
use std::collections::HashMap;
use std::mem::size_of;
use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};
use std::sync::Arc;

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::Limits;

use super::canonical::{canonical_field_count, scan_canonical, ScanBudget};

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) struct DenseId(u32);

impl DenseId {
    fn try_from_index(index: usize, label: &'static str) -> NativeResult<Self> {
        u32::try_from(index)
            .map(Self)
            .map_err(|_| NativeError::limit(label))
    }

    const fn index(self) -> usize {
        self.0 as usize
    }

    const fn raw(self) -> u32 {
        self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum LocalComponentId {
    Iri(DenseId),
    Entity(DenseId),
    Anonymous(DenseId),
    Literal(DenseId),
    Annotation(DenseId),
    PropertyExpression(DenseId),
    FacetRestriction(DenseId),
    DataRange(DenseId),
    ClassExpression(DenseId),
    Axiom(DenseId),
    Swrl(DenseId),
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct ComponentOwnerId(u64);

static NEXT_COMPONENT_OWNER: AtomicU64 = AtomicU64::new(1);

fn next_component_owner() -> NativeResult<ComponentOwnerId> {
    NEXT_COMPONENT_OWNER
        .fetch_update(AtomicOrdering::Relaxed, AtomicOrdering::Relaxed, |value| {
            value.checked_add(1)
        })
        .map(ComponentOwnerId)
        .map_err(|_| NativeError::limit("native component owner id space exhausted"))
}

/// An immutable-arena component identifier.
///
/// The owner token is private and intentionally absent from canonical output.
/// It prevents a dense identifier from one arena selecting an unrelated row in
/// another arena while leaving deterministic tables independent of build order.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct ComponentId {
    owner: ComponentOwnerId,
    local: LocalComponentId,
}

/// A builder-local component identifier which must be resolved at freeze.
///
/// Freeze canonicalizes every dense table.  Keeping the provisional type
/// distinct prevents a caller from accidentally using an insertion-order ID
/// against the immutable arena.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct PendingComponentId {
    owner: ComponentOwnerId,
    local: LocalComponentId,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
struct StringId(DenseId);

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
struct BytesId(DenseId);

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
struct IntegerId(DenseId);

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
struct ComponentSequenceId(DenseId);

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum ScalarKind {
    Text,
    Enum,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum ComponentValue {
    None,
    String(ScalarKind, StringId),
    Bytes(BytesId),
    Integer(IntegerId),
    Node(LocalComponentId),
    Sequence(ComponentSequenceId),
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum ComponentSequenceKind {
    CanonicalSet,
    Ordered,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct FrozenComponent {
    tag: u16,
    fields: Vec<ComponentValue>,
    height: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct FrozenComponentSequence {
    kind: ComponentSequenceKind,
    elements: Vec<ComponentValue>,
    height: u32,
}

#[derive(Debug)]
struct ComponentWork {
    guard: Guard,
    used: u64,
    maximum: u64,
    max_memory_bytes: Option<u64>,
    max_nesting_depth: u32,
    external_bytes: u64,
    auxiliary_bytes: u64,
    #[cfg(test)]
    allocation_fail_after: Option<u64>,
    #[cfg(test)]
    allocations: u64,
}

impl ComponentWork {
    fn new(
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        external_bytes: usize,
    ) -> NativeResult<Self> {
        let external_bytes = u64::try_from(external_bytes)
            .map_err(|_| NativeError::limit("native external allocation exceeds u64"))?;
        if limits
            .max_memory_bytes
            .is_some_and(|maximum| external_bytes > maximum)
        {
            return Err(NativeError::limit(
                "native external allocation exceeds max_memory_bytes",
            ));
        }
        let guard = match interrupt {
            Some(slot) => Guard::with_interrupt(
                cancellation,
                limits.deadline,
                limits.cancellation_stride,
                slot,
            ),
            None => Guard::new(cancellation, limits.deadline, limits.cancellation_stride),
        };
        Ok(Self {
            guard,
            used: 0,
            maximum: limits.max_canonical_work,
            max_memory_bytes: limits.max_memory_bytes,
            max_nesting_depth: limits.max_nesting_depth,
            external_bytes,
            auxiliary_bytes: 0,
            #[cfg(test)]
            allocation_fail_after: None,
            #[cfg(test)]
            allocations: 0,
        })
    }

    fn consume(&mut self, amount: usize) -> NativeResult<()> {
        let amount = u64::try_from(amount)
            .map_err(|_| NativeError::limit("native component work exceeds u64"))?;
        self.used = self
            .used
            .checked_add(amount)
            .ok_or_else(|| NativeError::limit("native component work counter overflow"))?;
        if self.used > self.maximum {
            return Err(NativeError::limit(
                "native component work exceeds max_canonical_work",
            ));
        }
        self.guard.check(self.used, false)
    }

    fn checkpoint(&mut self, force: bool) -> NativeResult<()> {
        self.guard.check(self.used, force)
    }

    fn allocation_checkpoint(&mut self) -> NativeResult<()> {
        #[cfg(test)]
        {
            if self
                .allocation_fail_after
                .is_some_and(|maximum| self.allocations >= maximum)
            {
                return Err(NativeError::limit(
                    "injected native component allocation failure",
                ));
            }
            self.allocations = self
                .allocations
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native allocation counter overflow"))?;
        }
        Ok(())
    }

    #[cfg(test)]
    fn fail_allocations_after(&mut self, successful: u64) {
        self.allocation_fail_after = Some(successful);
        self.allocations = 0;
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct ComponentCounters {
    pub(crate) node_requests: u64,
    pub(crate) node_hits: u64,
    pub(crate) unique_nodes: u64,
    pub(crate) string_requests: u64,
    pub(crate) string_hits: u64,
    pub(crate) unique_strings: u64,
    pub(crate) bytes_requests: u64,
    pub(crate) bytes_hits: u64,
    pub(crate) unique_bytes: u64,
    pub(crate) integer_requests: u64,
    pub(crate) integer_hits: u64,
    pub(crate) unique_integers: u64,
    pub(crate) sequence_requests: u64,
    pub(crate) sequence_hits: u64,
    pub(crate) unique_sequences: u64,
    pub(crate) peak_builder_bytes: u64,
    pub(crate) retained_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ComponentTables {
    max_encoded_bytes: u64,
    max_nesting_depth: u32,
    max_memory_bytes: Option<u64>,
    retained_bytes: u64,
    // Vec allocations are moved directly out of the builder.  The tables are
    // private behind Arc and expose no mutation, so an additional boxed-slice
    // reallocation at freeze would buy no immutability.
    strings: Vec<Vec<u8>>,
    bytes: Vec<Vec<u8>>,
    integers: Vec<Vec<u8>>,
    sequences: Vec<FrozenComponentSequence>,
    iris: Vec<FrozenComponent>,
    entities: Vec<FrozenComponent>,
    anonymous: Vec<FrozenComponent>,
    literals: Vec<FrozenComponent>,
    annotations: Vec<FrozenComponent>,
    property_expressions: Vec<FrozenComponent>,
    facet_restrictions: Vec<FrozenComponent>,
    data_ranges: Vec<FrozenComponent>,
    class_expressions: Vec<FrozenComponent>,
    axioms: Vec<FrozenComponent>,
    swrl: Vec<FrozenComponent>,
}

impl ComponentTables {
    fn component(&self, identifier: LocalComponentId) -> NativeResult<&FrozenComponent> {
        let selected = match identifier {
            LocalComponentId::Iri(id) => self.iris.get(id.index()),
            LocalComponentId::Entity(id) => self.entities.get(id.index()),
            LocalComponentId::Anonymous(id) => self.anonymous.get(id.index()),
            LocalComponentId::Literal(id) => self.literals.get(id.index()),
            LocalComponentId::Annotation(id) => self.annotations.get(id.index()),
            LocalComponentId::PropertyExpression(id) => self.property_expressions.get(id.index()),
            LocalComponentId::FacetRestriction(id) => self.facet_restrictions.get(id.index()),
            LocalComponentId::DataRange(id) => self.data_ranges.get(id.index()),
            LocalComponentId::ClassExpression(id) => self.class_expressions.get(id.index()),
            LocalComponentId::Axiom(id) => self.axioms.get(id.index()),
            LocalComponentId::Swrl(id) => self.swrl.get(id.index()),
        };
        selected.ok_or_else(|| NativeError::protocol("native component id is out of bounds"))
    }

    fn encode_with_work(
        &self,
        identifier: LocalComponentId,
        work: &mut ComponentWork,
    ) -> NativeResult<Vec<u8>> {
        work.checkpoint(true)?;
        let encoded_len = self.encoded_node_len(identifier, 0, work)?;
        if encoded_len > self.max_encoded_bytes {
            return Err(NativeError::limit(
                "native component encoding exceeds max_canonical_work",
            ));
        }
        let capacity = usize::try_from(encoded_len)
            .map_err(|_| NativeError::limit("native component encoding exceeds usize"))?;
        let memory_peak = self
            .retained_bytes
            .checked_add(work.external_bytes)
            .and_then(|value| value.checked_add(work.auxiliary_bytes))
            .and_then(|value| value.checked_add(encoded_len))
            .ok_or_else(|| NativeError::limit("native component encoding memory overflow"))?;
        let maximum_memory = match (self.max_memory_bytes, work.max_memory_bytes) {
            (Some(left), Some(right)) => Some(left.min(right)),
            (Some(value), None) | (None, Some(value)) => Some(value),
            (None, None) => None,
        };
        if maximum_memory.is_some_and(|maximum| memory_peak > maximum) {
            return Err(NativeError::limit(
                "native component encoding exceeds max_memory_bytes",
            ));
        }
        work.allocation_checkpoint()?;
        let mut output = Vec::new();
        output
            .try_reserve_exact(capacity)
            .map_err(|_| NativeError::limit("native component encoding allocation failed"))?;
        self.encode_node(identifier, &mut output, 0, work)?;
        if output.len() != capacity {
            return Err(NativeError::protocol(
                "native component encoding length calculation diverged",
            ));
        }
        work.checkpoint(true)?;
        Ok(output)
    }

    fn encoded_node_len(
        &self,
        identifier: LocalComponentId,
        depth: u32,
        work: &mut ComponentWork,
    ) -> NativeResult<u64> {
        self.check_encode_depth(depth, work)?;
        work.consume(1)?;
        let component = self.component(identifier)?;
        let mut length = varint_len(u64::from(component.tag));
        for value in &component.fields {
            length = checked_add_u64(length, self.encoded_value_len(*value, depth, work)?)?;
        }
        if length > self.max_encoded_bytes {
            return Err(NativeError::limit(
                "native component encoding exceeds max_canonical_work",
            ));
        }
        Ok(length)
    }

    fn encoded_value_len(
        &self,
        value: ComponentValue,
        depth: u32,
        work: &mut ComponentWork,
    ) -> NativeResult<u64> {
        work.consume(1)?;
        let payload =
            match value {
                ComponentValue::None => return Ok(1),
                ComponentValue::String(_, identifier) => {
                    let value = self.strings.get(identifier.0.index()).ok_or_else(|| {
                        NativeError::protocol("native string id is out of bounds")
                    })?;
                    work.consume(value.len())?;
                    u64::try_from(value.len())
                        .map_err(|_| NativeError::limit("native string length exceeds u64"))?
                }
                ComponentValue::Bytes(identifier) => {
                    let value = self
                        .bytes
                        .get(identifier.0.index())
                        .ok_or_else(|| NativeError::protocol("native bytes id is out of bounds"))?;
                    work.consume(value.len())?;
                    u64::try_from(value.len())
                        .map_err(|_| NativeError::limit("native bytes length exceeds u64"))?
                }
                ComponentValue::Integer(identifier) => {
                    let value = self.integers.get(identifier.0.index()).ok_or_else(|| {
                        NativeError::protocol("native integer id is out of bounds")
                    })?;
                    work.consume(value.len())?;
                    return checked_add_u64(
                        1,
                        u64::try_from(value.len())
                            .map_err(|_| NativeError::limit("native integer length exceeds u64"))?,
                    );
                }
                ComponentValue::Node(child) => self.encoded_node_len(
                    child,
                    depth.checked_add(1).ok_or_else(|| {
                        NativeError::limit("native component encoding depth overflow")
                    })?,
                    work,
                )?,
                ComponentValue::Sequence(identifier) => {
                    return self.encoded_sequence_len(identifier, depth, work)
                }
            };
        checked_add_u64(checked_add_u64(1, varint_len(payload))?, payload)
    }

    fn encoded_sequence_len(
        &self,
        identifier: ComponentSequenceId,
        depth: u32,
        work: &mut ComponentWork,
    ) -> NativeResult<u64> {
        work.consume(1)?;
        let sequence = self.sequences.get(identifier.0.index()).ok_or_else(|| {
            NativeError::protocol("native component sequence id is out of bounds")
        })?;
        let count = u64::try_from(sequence.elements.len())
            .map_err(|_| NativeError::limit("native component sequence length exceeds u64"))?;
        let mut length = checked_add_u64(1, varint_len(count))?;
        for element in &sequence.elements {
            let item = match sequence.kind {
                ComponentSequenceKind::CanonicalSet => {
                    let ComponentValue::Node(child) = element else {
                        return Err(NativeError::protocol(
                            "native canonical set contains a scalar",
                        ));
                    };
                    let child_len = self.encoded_node_len(
                        *child,
                        depth.checked_add(1).ok_or_else(|| {
                            NativeError::limit("native component encoding depth overflow")
                        })?,
                        work,
                    )?;
                    checked_add_u64(varint_len(child_len), child_len)?
                }
                ComponentSequenceKind::Ordered => self.encoded_value_len(*element, depth, work)?,
            };
            length = checked_add_u64(length, item)?;
        }
        Ok(length)
    }

    fn check_encode_depth(&self, depth: u32, work: &ComponentWork) -> NativeResult<()> {
        if depth > self.max_nesting_depth.min(work.max_nesting_depth).min(1024) {
            return Err(NativeError::limit(
                "native component encoding nesting exceeds configured limits",
            ));
        }
        Ok(())
    }

    fn encode_node(
        &self,
        identifier: LocalComponentId,
        output: &mut Vec<u8>,
        depth: u32,
        work: &mut ComponentWork,
    ) -> NativeResult<()> {
        self.check_encode_depth(depth, work)?;
        work.consume(1)?;
        let component = self.component(identifier)?;
        encode_varint(u64::from(component.tag), output)?;
        for value in &component.fields {
            match value {
                ComponentValue::None => push_byte(output, 0)?,
                ComponentValue::String(ScalarKind::Text, identifier) => {
                    push_byte(output, 2)?;
                    let value = self.strings.get(identifier.0.index()).ok_or_else(|| {
                        NativeError::protocol("native string id is out of bounds")
                    })?;
                    work.consume(value.len())?;
                    encode_frame(value, output)?;
                }
                ComponentValue::Bytes(identifier) => {
                    push_byte(output, 3)?;
                    let value = self
                        .bytes
                        .get(identifier.0.index())
                        .ok_or_else(|| NativeError::protocol("native bytes id is out of bounds"))?;
                    work.consume(value.len())?;
                    encode_frame(value, output)?;
                }
                ComponentValue::Integer(identifier) => {
                    push_byte(output, 4)?;
                    let value = self.integers.get(identifier.0.index()).ok_or_else(|| {
                        NativeError::protocol("native integer id is out of bounds")
                    })?;
                    work.consume(value.len())?;
                    append(output, value)?;
                }
                ComponentValue::String(ScalarKind::Enum, identifier) => {
                    push_byte(output, 5)?;
                    let value = self.strings.get(identifier.0.index()).ok_or_else(|| {
                        NativeError::protocol("native enum string id is out of bounds")
                    })?;
                    work.consume(value.len())?;
                    encode_frame(value, output)?;
                }
                ComponentValue::Node(child) => {
                    push_byte(output, 1)?;
                    let child_depth = depth.checked_add(1).ok_or_else(|| {
                        NativeError::limit("native component encoding depth overflow")
                    })?;
                    let child_len = self.encoded_node_len(*child, child_depth, work)?;
                    encode_varint(child_len, output)?;
                    self.encode_node(*child, output, child_depth, work)?;
                }
                ComponentValue::Sequence(identifier) => {
                    let sequence = self.sequences.get(identifier.0.index()).ok_or_else(|| {
                        NativeError::protocol("native component sequence id is out of bounds")
                    })?;
                    match sequence.kind {
                        ComponentSequenceKind::CanonicalSet => {
                            push_byte(output, 6)?;
                            encode_varint(sequence.elements.len() as u64, output)?;
                            for element in &sequence.elements {
                                let ComponentValue::Node(child) = element else {
                                    return Err(NativeError::protocol(
                                        "native canonical set contains a scalar",
                                    ));
                                };
                                let child_depth = depth.checked_add(1).ok_or_else(|| {
                                    NativeError::limit("native component encoding depth overflow")
                                })?;
                                let child_len = self.encoded_node_len(*child, child_depth, work)?;
                                encode_varint(child_len, output)?;
                                self.encode_node(*child, output, child_depth, work)?;
                            }
                        }
                        ComponentSequenceKind::Ordered => {
                            push_byte(output, 7)?;
                            encode_varint(sequence.elements.len() as u64, output)?;
                            for element in &sequence.elements {
                                let element_depth = depth.checked_add(1).ok_or_else(|| {
                                    NativeError::limit("native component encoding depth overflow")
                                })?;
                                self.encode_sequence_element(
                                    *element,
                                    output,
                                    element_depth,
                                    work,
                                )?;
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    fn encode_sequence_element(
        &self,
        value: ComponentValue,
        output: &mut Vec<u8>,
        depth: u32,
        work: &mut ComponentWork,
    ) -> NativeResult<()> {
        work.consume(1)?;
        match value {
            ComponentValue::Node(child) => {
                push_byte(output, 1)?;
                let child_len = self.encoded_node_len(child, depth, work)?;
                encode_varint(child_len, output)?;
                self.encode_node(child, output, depth, work)
            }
            ComponentValue::None => push_byte(output, 0),
            ComponentValue::String(ScalarKind::Text, identifier) => {
                push_byte(output, 2)?;
                let value = self
                    .strings
                    .get(identifier.0.index())
                    .ok_or_else(|| NativeError::protocol("native string id is out of bounds"))?;
                work.consume(value.len())?;
                encode_frame(value, output)
            }
            ComponentValue::Bytes(identifier) => {
                push_byte(output, 3)?;
                let value = self
                    .bytes
                    .get(identifier.0.index())
                    .ok_or_else(|| NativeError::protocol("native bytes id is out of bounds"))?;
                work.consume(value.len())?;
                encode_frame(value, output)
            }
            ComponentValue::Integer(identifier) => {
                push_byte(output, 4)?;
                let value = self
                    .integers
                    .get(identifier.0.index())
                    .ok_or_else(|| NativeError::protocol("native integer id is out of bounds"))?;
                work.consume(value.len())?;
                append(output, value)
            }
            ComponentValue::String(ScalarKind::Enum, identifier) => {
                push_byte(output, 5)?;
                let value = self.strings.get(identifier.0.index()).ok_or_else(|| {
                    NativeError::protocol("native enum string id is out of bounds")
                })?;
                work.consume(value.len())?;
                encode_frame(value, output)
            }
            ComponentValue::Sequence(_) => Err(NativeError::protocol(
                "native ordered sequence contains an invalid value",
            )),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct NativeComponentArena {
    owner: ComponentOwnerId,
    tables: Arc<ComponentTables>,
    counters: ComponentCounters,
}

impl NativeComponentArena {
    fn tables(&self) -> &ComponentTables {
        &self.tables
    }

    fn encode_with_work(
        &self,
        identifier: ComponentId,
        work: &mut ComponentWork,
    ) -> NativeResult<Vec<u8>> {
        if identifier.owner != self.owner {
            return Err(NativeError::protocol(
                "native component id belongs to a different arena",
            ));
        }
        self.tables.encode_with_work(identifier.local, work)
    }

    pub(crate) fn encode(
        &self,
        identifier: ComponentId,
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        external_bytes: usize,
    ) -> NativeResult<Vec<u8>> {
        let mut work = ComponentWork::new(limits, cancellation, interrupt, external_bytes)?;
        self.encode_with_work(identifier, &mut work)
    }

    pub(crate) const fn counters(&self) -> &ComponentCounters {
        &self.counters
    }

    pub(crate) fn shares_storage_with(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.tables, &other.tables)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ComponentIdRemap {
    iris: Vec<DenseId>,
    entities: Vec<DenseId>,
    anonymous: Vec<DenseId>,
    literals: Vec<DenseId>,
    annotations: Vec<DenseId>,
    property_expressions: Vec<DenseId>,
    facet_restrictions: Vec<DenseId>,
    data_ranges: Vec<DenseId>,
    class_expressions: Vec<DenseId>,
    axioms: Vec<DenseId>,
    swrl: Vec<DenseId>,
}

impl ComponentIdRemap {
    fn sentinel(builder: &NativeComponentBuilder, work: &mut ComponentWork) -> NativeResult<Self> {
        Ok(Self {
            iris: sentinel_mapping(builder.iris.len(), work)?,
            entities: sentinel_mapping(builder.entities.len(), work)?,
            anonymous: sentinel_mapping(builder.anonymous.len(), work)?,
            literals: sentinel_mapping(builder.literals.len(), work)?,
            annotations: sentinel_mapping(builder.annotations.len(), work)?,
            property_expressions: sentinel_mapping(builder.property_expressions.len(), work)?,
            facet_restrictions: sentinel_mapping(builder.facet_restrictions.len(), work)?,
            data_ranges: sentinel_mapping(builder.data_ranges.len(), work)?,
            class_expressions: sentinel_mapping(builder.class_expressions.len(), work)?,
            axioms: sentinel_mapping(builder.axioms.len(), work)?,
            swrl: sentinel_mapping(builder.swrl.len(), work)?,
        })
    }

    fn mapping(&self, category: ComponentCategory) -> &[DenseId] {
        match category {
            ComponentCategory::Iri => &self.iris,
            ComponentCategory::Entity => &self.entities,
            ComponentCategory::Anonymous => &self.anonymous,
            ComponentCategory::Literal => &self.literals,
            ComponentCategory::Annotation => &self.annotations,
            ComponentCategory::PropertyExpression => &self.property_expressions,
            ComponentCategory::FacetRestriction => &self.facet_restrictions,
            ComponentCategory::DataRange => &self.data_ranges,
            ComponentCategory::ClassExpression => &self.class_expressions,
            ComponentCategory::Axiom => &self.axioms,
            ComponentCategory::Swrl => &self.swrl,
        }
    }

    fn mapping_mut(&mut self, category: ComponentCategory) -> &mut [DenseId] {
        match category {
            ComponentCategory::Iri => &mut self.iris,
            ComponentCategory::Entity => &mut self.entities,
            ComponentCategory::Anonymous => &mut self.anonymous,
            ComponentCategory::Literal => &mut self.literals,
            ComponentCategory::Annotation => &mut self.annotations,
            ComponentCategory::PropertyExpression => &mut self.property_expressions,
            ComponentCategory::FacetRestriction => &mut self.facet_restrictions,
            ComponentCategory::DataRange => &mut self.data_ranges,
            ComponentCategory::ClassExpression => &mut self.class_expressions,
            ComponentCategory::Axiom => &mut self.axioms,
            ComponentCategory::Swrl => &mut self.swrl,
        }
    }

    fn stable_rank(&self, identifier: LocalComponentId) -> NativeResult<(u64, u64)> {
        let (mapping, category, old) = match identifier {
            LocalComponentId::Iri(id) => (&self.iris, ComponentCategory::Iri, id),
            LocalComponentId::Entity(id) => (&self.entities, ComponentCategory::Entity, id),
            LocalComponentId::Anonymous(id) => (&self.anonymous, ComponentCategory::Anonymous, id),
            LocalComponentId::Literal(id) => (&self.literals, ComponentCategory::Literal, id),
            LocalComponentId::Annotation(id) => {
                (&self.annotations, ComponentCategory::Annotation, id)
            }
            LocalComponentId::PropertyExpression(id) => (
                &self.property_expressions,
                ComponentCategory::PropertyExpression,
                id,
            ),
            LocalComponentId::FacetRestriction(id) => (
                &self.facet_restrictions,
                ComponentCategory::FacetRestriction,
                id,
            ),
            LocalComponentId::DataRange(id) => {
                (&self.data_ranges, ComponentCategory::DataRange, id)
            }
            LocalComponentId::ClassExpression(id) => (
                &self.class_expressions,
                ComponentCategory::ClassExpression,
                id,
            ),
            LocalComponentId::Axiom(id) => (&self.axioms, ComponentCategory::Axiom, id),
            LocalComponentId::Swrl(id) => (&self.swrl, ComponentCategory::Swrl, id),
        };
        let rank = assigned_rank(mapping, old, "native child component rank is unavailable")?;
        Ok((category.order_code(), u64::from(rank.raw())))
    }

    fn verify_complete(&self, work: &mut ComponentWork) -> NativeResult<()> {
        for category in ComponentCategory::ALL {
            verify_complete_mapping(
                self.mapping(category),
                "native component rank mapping is incomplete",
                work,
            )?;
        }
        Ok(())
    }

    fn resolve_component(&self, identifier: LocalComponentId) -> NativeResult<LocalComponentId> {
        let (mapping, category, old) = match identifier {
            LocalComponentId::Iri(id) => (&self.iris, ComponentCategory::Iri, id),
            LocalComponentId::Entity(id) => (&self.entities, ComponentCategory::Entity, id),
            LocalComponentId::Anonymous(id) => (&self.anonymous, ComponentCategory::Anonymous, id),
            LocalComponentId::Literal(id) => (&self.literals, ComponentCategory::Literal, id),
            LocalComponentId::Annotation(id) => {
                (&self.annotations, ComponentCategory::Annotation, id)
            }
            LocalComponentId::PropertyExpression(id) => (
                &self.property_expressions,
                ComponentCategory::PropertyExpression,
                id,
            ),
            LocalComponentId::FacetRestriction(id) => (
                &self.facet_restrictions,
                ComponentCategory::FacetRestriction,
                id,
            ),
            LocalComponentId::DataRange(id) => {
                (&self.data_ranges, ComponentCategory::DataRange, id)
            }
            LocalComponentId::ClassExpression(id) => (
                &self.class_expressions,
                ComponentCategory::ClassExpression,
                id,
            ),
            LocalComponentId::Axiom(id) => (&self.axioms, ComponentCategory::Axiom, id),
            LocalComponentId::Swrl(id) => (&self.swrl, ComponentCategory::Swrl, id),
        };
        let mapped = mapping
            .get(old.index())
            .copied()
            .ok_or_else(|| NativeError::protocol("provisional native component id is invalid"))?;
        Ok(category.with_id(mapped))
    }
}

#[derive(Debug)]
pub(crate) struct FrozenComponentBuild {
    arena: NativeComponentArena,
    roots: ComponentIdRemap,
    work: ComponentWork,
}

impl FrozenComponentBuild {
    pub(crate) fn resolve(&self, identifier: PendingComponentId) -> NativeResult<ComponentId> {
        if identifier.owner != self.arena.owner {
            return Err(NativeError::protocol(
                "pending component id belongs to a different builder",
            ));
        }
        Ok(ComponentId {
            owner: self.arena.owner,
            local: self.roots.resolve_component(identifier.local)?,
        })
    }

    const fn arena(&self) -> &NativeComponentArena {
        &self.arena
    }

    pub(crate) fn into_arena(self) -> NativeComponentArena {
        self.arena
    }

    pub(crate) fn encode(&mut self, identifier: ComponentId) -> NativeResult<Vec<u8>> {
        self.arena.encode_with_work(identifier, &mut self.work)
    }
}

type BucketTransform = fn(u64) -> u64;

const fn identity_bucket(value: u64) -> u64 {
    value
}

#[derive(Debug)]
pub(crate) struct NativeComponentBuilder {
    owner: ComponentOwnerId,
    limits: Limits,
    work: Option<ComponentWork>,
    accounted_bytes: u64,
    transient_bytes: u64,
    strings: Vec<Vec<u8>>,
    string_buckets: HashMap<u64, Vec<StringId>>,
    bytes: Vec<Vec<u8>>,
    bytes_buckets: HashMap<u64, Vec<BytesId>>,
    integers: Vec<Vec<u8>>,
    integer_buckets: HashMap<u64, Vec<IntegerId>>,
    sequences: Vec<FrozenComponentSequence>,
    sequence_buckets: HashMap<u64, Vec<ComponentSequenceId>>,
    component_buckets: HashMap<u64, Vec<LocalComponentId>>,
    iris: Vec<FrozenComponent>,
    entities: Vec<FrozenComponent>,
    anonymous: Vec<FrozenComponent>,
    literals: Vec<FrozenComponent>,
    annotations: Vec<FrozenComponent>,
    property_expressions: Vec<FrozenComponent>,
    facet_restrictions: Vec<FrozenComponent>,
    data_ranges: Vec<FrozenComponent>,
    class_expressions: Vec<FrozenComponent>,
    axioms: Vec<FrozenComponent>,
    swrl: Vec<FrozenComponent>,
    counters: ComponentCounters,
    poisoned: bool,
    bucket_transform: BucketTransform,
}

impl NativeComponentBuilder {
    pub(crate) fn new(limits: &Limits) -> NativeResult<Self> {
        Self::with_control(limits, Cancellation::with_duration(None), None, 0)
    }

    pub(crate) fn with_control(
        limits: &Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        external_bytes: usize,
    ) -> NativeResult<Self> {
        Ok(Self {
            owner: next_component_owner()?,
            limits: *limits,
            work: Some(ComponentWork::new(
                limits,
                cancellation,
                interrupt,
                external_bytes,
            )?),
            accounted_bytes: 0,
            transient_bytes: 0,
            strings: Vec::new(),
            string_buckets: HashMap::new(),
            bytes: Vec::new(),
            bytes_buckets: HashMap::new(),
            integers: Vec::new(),
            integer_buckets: HashMap::new(),
            sequences: Vec::new(),
            sequence_buckets: HashMap::new(),
            component_buckets: HashMap::new(),
            iris: Vec::new(),
            entities: Vec::new(),
            anonymous: Vec::new(),
            literals: Vec::new(),
            annotations: Vec::new(),
            property_expressions: Vec::new(),
            facet_restrictions: Vec::new(),
            data_ranges: Vec::new(),
            class_expressions: Vec::new(),
            axioms: Vec::new(),
            swrl: Vec::new(),
            counters: ComponentCounters::default(),
            poisoned: false,
            bucket_transform: identity_bucket,
        })
    }

    #[cfg(test)]
    fn with_bucket_transform(
        limits: &Limits,
        bucket_transform: BucketTransform,
    ) -> NativeResult<Self> {
        let mut builder = Self::new(limits)?;
        builder.bucket_transform = bucket_transform;
        Ok(builder)
    }

    pub(crate) fn intern_canonical(
        &mut self,
        canonical: &[u8],
    ) -> NativeResult<PendingComponentId> {
        if self.poisoned {
            return Err(NativeError::protocol(
                "native component builder is poisoned after a failed mutation",
            ));
        }
        self.work_mut()?.checkpoint(true)?;
        let mut budget = ScanBudget::from_limits(&self.limits);
        scan_canonical(canonical, &mut budget)?;
        let decoded = self.decode_node(canonical, 0, canonical.len());
        let (identifier, consumed) = match decoded {
            Ok(value) => value,
            Err(error) => {
                self.poisoned = true;
                return Err(error);
            }
        };
        if consumed != canonical.len() {
            self.poisoned = true;
            return Err(NativeError::corrupt(
                "native component decoder left trailing canonical bytes",
            ));
        }
        self.work_mut()?.checkpoint(true)?;
        Ok(PendingComponentId {
            owner: self.owner,
            local: identifier,
        })
    }

    pub(crate) fn freeze(mut self) -> NativeResult<FrozenComponentBuild> {
        if self.poisoned {
            return Err(NativeError::protocol(
                "cannot freeze a poisoned native component builder",
            ));
        }
        if self.transient_bytes != 0 {
            return Err(NativeError::protocol(
                "native component builder retained temporary allocation accounting",
            ));
        }
        let mut work = self
            .work
            .take()
            .ok_or_else(|| NativeError::protocol("native component work state is unavailable"))?;
        work.checkpoint(true)?;
        // Interning indexes are builder-only.  Drop them before allocating the
        // bounded sort/remap workspace so the peak is not needlessly doubled.
        // The forced checkpoint above catches already-cancelled work before
        // this potentially large, uninterruptible destructor run.
        self.drop_interners();
        self.accounted_bytes = retained_builder_bytes(&self, &mut work)?;
        let (workspace, maximum_height) = self.freeze_workspace_bound(&mut work)?;
        self.check_temporary(workspace, work.external_bytes)?;

        let remaps = FreezeRemaps::build(&self, maximum_height, &mut work)?;
        self.apply_freeze_remaps(&remaps, &mut work)?;
        let mut tables = ComponentTables {
            max_encoded_bytes: self.limits.max_canonical_work,
            max_nesting_depth: self.limits.max_nesting_depth,
            max_memory_bytes: self.limits.max_memory_bytes,
            retained_bytes: 0,
            strings: self.strings,
            bytes: self.bytes,
            integers: self.integers,
            sequences: self.sequences,
            iris: self.iris,
            entities: self.entities,
            anonymous: self.anonymous,
            literals: self.literals,
            annotations: self.annotations,
            property_expressions: self.property_expressions,
            facet_restrictions: self.facet_restrictions,
            data_ranges: self.data_ranges,
            class_expressions: self.class_expressions,
            axioms: self.axioms,
            swrl: self.swrl,
        };
        self.counters.retained_bytes = retained_table_bytes(&tables, &mut work)?;
        tables.retained_bytes = self.counters.retained_bytes;
        let workspace_bytes = u64::try_from(workspace)
            .map_err(|_| NativeError::limit("native freeze workspace exceeds u64"))?;
        let frozen_peak = self
            .counters
            .retained_bytes
            .checked_add(work.external_bytes)
            .and_then(|value| value.checked_add(workspace_bytes))
            .ok_or_else(|| NativeError::limit("native freeze memory accounting overflow"))?;
        if self
            .limits
            .max_memory_bytes
            .is_some_and(|maximum| frozen_peak > maximum)
        {
            return Err(NativeError::limit(
                "native frozen arena exceeds max_memory_bytes",
            ));
        }
        self.counters.peak_builder_bytes = self
            .counters
            .peak_builder_bytes
            .max(self.accounted_bytes)
            .max(frozen_peak);
        work.auxiliary_bytes = component_remap_retained_bytes(&remaps.components)?;
        Ok(FrozenComponentBuild {
            arena: NativeComponentArena {
                owner: self.owner,
                // Stable Rust exposes no fallible Arc constructor without the
                // unstable allocator_api.  All payload/workspace growth above
                // is fallible and preflighted; this one small control-block
                // allocation is the explicit safe-Rust residual.
                tables: Arc::new(tables),
                counters: self.counters,
            },
            roots: remaps.components,
            work,
        })
    }

    fn work_mut(&mut self) -> NativeResult<&mut ComponentWork> {
        self.work
            .as_mut()
            .ok_or_else(|| NativeError::protocol("native component work state is unavailable"))
    }

    fn external_bytes(&self) -> NativeResult<u64> {
        self.work
            .as_ref()
            .map(|work| work.external_bytes)
            .ok_or_else(|| NativeError::protocol("native component work state is unavailable"))
    }

    fn consume_work(&mut self, amount: usize) -> NativeResult<()> {
        self.work_mut()?.consume(amount)
    }

    fn allocation_checkpoint(&mut self) -> NativeResult<()> {
        self.work_mut()?.allocation_checkpoint()
    }

    fn fallible_owned_bytes(&mut self, value: &[u8], label: &'static str) -> NativeResult<Vec<u8>> {
        self.allocation_checkpoint()?;
        let mut owned = Vec::new();
        owned
            .try_reserve_exact(value.len())
            .map_err(|_| NativeError::limit(label))?;
        for chunk in value.chunks(4096) {
            owned.extend_from_slice(chunk);
            self.consume_work(chunk.len())?;
        }
        Ok(owned)
    }

    fn value_height(&self, value: ComponentValue) -> NativeResult<u32> {
        match value {
            ComponentValue::Node(identifier) => self
                .builder_component(identifier)
                .map(|component| component.height)
                .ok_or_else(|| NativeError::protocol("native child component id is invalid")),
            ComponentValue::Sequence(identifier) => self
                .sequences
                .get(identifier.0.index())
                .map(|sequence| sequence.height)
                .ok_or_else(|| NativeError::protocol("native child sequence id is invalid")),
            _ => Ok(0),
        }
    }

    fn values_height(&mut self, values: &[ComponentValue]) -> NativeResult<u32> {
        let mut maximum = 0_u32;
        for value in values {
            maximum = maximum.max(self.value_height(*value)?);
            self.consume_work(1)?;
        }
        maximum
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native component height overflow"))
    }

    fn decode_node(
        &mut self,
        data: &[u8],
        offset: usize,
        end: usize,
    ) -> NativeResult<(LocalComponentId, usize)> {
        let (raw_tag, mut cursor) = decode_u64_varint(data, offset, end)?;
        let tag = u16::try_from(raw_tag)
            .map_err(|_| NativeError::corrupt("canonical model tag exceeds u16"))?;
        let fields = canonical_field_count(tag)
            .ok_or_else(|| NativeError::corrupt("unknown canonical model tag"))?;
        let field_payload_bytes = usize::from(fields)
            .checked_mul(size_of::<ComponentValue>())
            .ok_or_else(|| NativeError::limit("native component field size overflow"))?;
        let field_bytes = temporary_vector_bytes(field_payload_bytes, fields != 0)?;
        self.reserve_transient(field_bytes)?;
        self.allocation_checkpoint()?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(usize::from(fields))
            .map_err(|_| NativeError::limit("native component field allocation failed"))?;
        for _ in 0..fields {
            let marker = *data
                .get(cursor)
                .ok_or_else(|| NativeError::corrupt("truncated canonical model component"))?;
            cursor += 1;
            let (value, following) = self.decode_value(marker, data, cursor, end)?;
            values.push(value);
            cursor = following;
        }
        bump(
            &mut self.counters.node_requests,
            "component request counter overflow",
        )?;
        let bucket_value = {
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            component_bucket(tag, &values, work)?
        };
        let bucket = (self.bucket_transform)(bucket_value);
        let candidate_count = self.component_buckets.get(&bucket).map_or(0, Vec::len);
        let mut retained = None;
        for index in 0..candidate_count {
            let identifier = self
                .component_buckets
                .get(&bucket)
                .and_then(|identifiers| identifiers.get(index))
                .copied()
                .ok_or_else(|| NativeError::protocol("native component bucket is inconsistent"))?;
            if self.metered_component_equal(identifier, tag, &values)? {
                retained = Some(identifier);
                break;
            }
        }
        if let Some(identifier) = retained {
            bump(
                &mut self.counters.node_hits,
                "component hit counter overflow",
            )?;
            self.release_transient(field_bytes)?;
            return Ok((identifier, cursor));
        }
        self.check_new_term("native component count exceeds max_terms")?;
        self.prepare_component_bucket(bucket)?;
        let identifier = self.push_component(tag, values, field_bytes)?;
        self.component_buckets
            .entry(bucket)
            .or_default()
            .push(identifier);
        bump(
            &mut self.counters.unique_nodes,
            "unique component counter overflow",
        )?;
        Ok((identifier, cursor))
    }

    fn decode_value(
        &mut self,
        marker: u8,
        data: &[u8],
        offset: usize,
        end: usize,
    ) -> NativeResult<(ComponentValue, usize)> {
        match marker {
            0 => Ok((ComponentValue::None, offset)),
            1 => {
                let (start, frame_end, following) = decode_frame(data, offset, end)?;
                let (identifier, consumed) = self.decode_node(data, start, frame_end)?;
                if consumed != frame_end {
                    return Err(NativeError::corrupt(
                        "nested canonical component has trailing bytes",
                    ));
                }
                Ok((ComponentValue::Node(identifier), following))
            }
            2 | 5 => {
                let (start, frame_end, following) = decode_frame(data, offset, end)?;
                let text = std::str::from_utf8(&data[start..frame_end])
                    .map_err(|_| NativeError::corrupt("canonical text is not UTF-8"))?;
                let identifier = self.intern_string(text)?;
                let kind = if marker == 2 {
                    ScalarKind::Text
                } else {
                    ScalarKind::Enum
                };
                Ok((ComponentValue::String(kind, identifier), following))
            }
            3 => {
                let (start, frame_end, following) = decode_frame(data, offset, end)?;
                let identifier = self.intern_bytes(&data[start..frame_end])?;
                Ok((ComponentValue::Bytes(identifier), following))
            }
            4 => {
                let following = scan_integer_varint(data, offset, end)?;
                let identifier = self.intern_integer(&data[offset..following])?;
                Ok((ComponentValue::Integer(identifier), following))
            }
            6 | 7 => {
                let (count, mut cursor) = decode_u64_varint(data, offset, end)?;
                if count > self.limits.max_sequence_arity {
                    return Err(NativeError::limit(
                        "native component sequence arity exceeds limits",
                    ));
                }
                let count = usize::try_from(count)
                    .map_err(|_| NativeError::limit("native component sequence exceeds usize"))?;
                let element_payload_bytes = count
                    .checked_mul(size_of::<ComponentValue>())
                    .ok_or_else(|| NativeError::limit("component sequence allocation overflow"))?;
                let element_bytes = temporary_vector_bytes(element_payload_bytes, count != 0)?;
                self.reserve_transient(element_bytes)?;
                self.allocation_checkpoint()?;
                let mut elements = Vec::new();
                elements.try_reserve_exact(count).map_err(|_| {
                    NativeError::limit("native component sequence allocation failed")
                })?;
                for _ in 0..count {
                    if marker == 6 {
                        let (start, frame_end, following) = decode_frame(data, cursor, end)?;
                        let (identifier, consumed) = self.decode_node(data, start, frame_end)?;
                        if consumed != frame_end {
                            return Err(NativeError::corrupt(
                                "canonical set member has trailing bytes",
                            ));
                        }
                        elements.push(ComponentValue::Node(identifier));
                        cursor = following;
                    } else {
                        let item_marker = *data.get(cursor).ok_or_else(|| {
                            NativeError::corrupt("truncated canonical sequence component")
                        })?;
                        cursor += 1;
                        let (element, following) =
                            self.decode_value(item_marker, data, cursor, end)?;
                        if matches!(element, ComponentValue::Sequence(_)) {
                            return Err(NativeError::corrupt(
                                "nested canonical sequence scalar is unsupported",
                            ));
                        }
                        elements.push(element);
                        cursor = following;
                    }
                    self.consume_work(1)?;
                }
                let kind = if marker == 6 {
                    ComponentSequenceKind::CanonicalSet
                } else {
                    ComponentSequenceKind::Ordered
                };
                let identifier = self.intern_sequence(kind, elements, element_bytes)?;
                Ok((ComponentValue::Sequence(identifier), cursor))
            }
            _ => Err(NativeError::corrupt("unknown canonical component marker")),
        }
    }

    fn intern_string(&mut self, value: &str) -> NativeResult<StringId> {
        bump(
            &mut self.counters.string_requests,
            "string request counter overflow",
        )?;
        let bucket_value = {
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            metered_scalar_bucket(0x5354_5249_4e47, value.as_bytes(), work)?
        };
        let bucket = (self.bucket_transform)(bucket_value);
        let candidate_count = self.string_buckets.get(&bucket).map_or(0, Vec::len);
        let mut retained = None;
        for index in 0..candidate_count {
            let identifier = self
                .string_buckets
                .get(&bucket)
                .and_then(|identifiers| identifiers.get(index))
                .copied()
                .ok_or_else(|| NativeError::protocol("native string bucket is inconsistent"))?;
            let strings = &self.strings;
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            let retained_value = strings
                .get(identifier.0.index())
                .ok_or_else(|| NativeError::protocol("native string id is out of bounds"))?;
            if metered_slice_equal(retained_value, value.as_bytes(), work)? {
                retained = Some(identifier);
                break;
            }
        }
        if let Some(identifier) = retained {
            bump(
                &mut self.counters.string_hits,
                "string hit counter overflow",
            )?;
            return Ok(identifier);
        }
        check_count(
            self.strings.len(),
            self.limits.max_strings,
            "native string table exceeds limits",
        )?;
        let identifier = StringId(DenseId::try_from_index(
            self.strings.len(),
            "native string id space exhausted",
        )?);
        let new_bucket = !self.string_buckets.contains_key(&bucket);
        self.charge(scalar_retained_charge::<Vec<u8>, StringId>(
            value.len(),
            new_bucket,
        )?)?;
        self.allocation_checkpoint()?;
        self.strings
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native string table allocation failed"))?;
        self.allocation_checkpoint()?;
        prepare_bucket(&mut self.string_buckets, bucket, "native string interner")?;
        let owned =
            self.fallible_owned_bytes(value.as_bytes(), "native string payload allocation failed")?;
        self.strings.push(owned);
        self.string_buckets
            .entry(bucket)
            .or_default()
            .push(identifier);
        bump(
            &mut self.counters.unique_strings,
            "unique string counter overflow",
        )?;
        Ok(identifier)
    }

    fn intern_bytes(&mut self, value: &[u8]) -> NativeResult<BytesId> {
        bump(
            &mut self.counters.bytes_requests,
            "bytes request counter overflow",
        )?;
        let bucket_value = {
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            metered_scalar_bucket(0x4259_5445_535f, value, work)?
        };
        let bucket = (self.bucket_transform)(bucket_value);
        let candidate_count = self.bytes_buckets.get(&bucket).map_or(0, Vec::len);
        let mut retained = None;
        for index in 0..candidate_count {
            let identifier = self
                .bytes_buckets
                .get(&bucket)
                .and_then(|identifiers| identifiers.get(index))
                .copied()
                .ok_or_else(|| NativeError::protocol("native bytes bucket is inconsistent"))?;
            let bytes = &self.bytes;
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            let retained_value = bytes
                .get(identifier.0.index())
                .ok_or_else(|| NativeError::protocol("native bytes id is out of bounds"))?;
            if metered_slice_equal(retained_value, value, work)? {
                retained = Some(identifier);
                break;
            }
        }
        if let Some(identifier) = retained {
            bump(&mut self.counters.bytes_hits, "bytes hit counter overflow")?;
            return Ok(identifier);
        }
        let identifier = BytesId(DenseId::try_from_index(
            self.bytes.len(),
            "native bytes id space exhausted",
        )?);
        let new_bucket = !self.bytes_buckets.contains_key(&bucket);
        self.charge(scalar_retained_charge::<Vec<u8>, BytesId>(
            value.len(),
            new_bucket,
        )?)?;
        self.allocation_checkpoint()?;
        self.bytes
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native bytes table allocation failed"))?;
        self.allocation_checkpoint()?;
        prepare_bucket(&mut self.bytes_buckets, bucket, "native bytes interner")?;
        let owned = self.fallible_owned_bytes(value, "native bytes payload allocation failed")?;
        self.bytes.push(owned);
        self.bytes_buckets
            .entry(bucket)
            .or_default()
            .push(identifier);
        bump(
            &mut self.counters.unique_bytes,
            "unique bytes counter overflow",
        )?;
        Ok(identifier)
    }

    fn intern_integer(&mut self, value: &[u8]) -> NativeResult<IntegerId> {
        bump(
            &mut self.counters.integer_requests,
            "integer request counter overflow",
        )?;
        let bucket_value = {
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            metered_scalar_bucket(0x494e_5445_4745, value, work)?
        };
        let bucket = (self.bucket_transform)(bucket_value);
        let candidate_count = self.integer_buckets.get(&bucket).map_or(0, Vec::len);
        let mut retained = None;
        for index in 0..candidate_count {
            let identifier = self
                .integer_buckets
                .get(&bucket)
                .and_then(|identifiers| identifiers.get(index))
                .copied()
                .ok_or_else(|| NativeError::protocol("native integer bucket is inconsistent"))?;
            let integers = &self.integers;
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            let retained_value = integers
                .get(identifier.0.index())
                .ok_or_else(|| NativeError::protocol("native integer id is out of bounds"))?;
            if metered_slice_equal(retained_value, value, work)? {
                retained = Some(identifier);
                break;
            }
        }
        if let Some(identifier) = retained {
            bump(
                &mut self.counters.integer_hits,
                "integer hit counter overflow",
            )?;
            return Ok(identifier);
        }
        let identifier = IntegerId(DenseId::try_from_index(
            self.integers.len(),
            "native integer id space exhausted",
        )?);
        let new_bucket = !self.integer_buckets.contains_key(&bucket);
        self.charge(scalar_retained_charge::<Vec<u8>, IntegerId>(
            value.len(),
            new_bucket,
        )?)?;
        self.allocation_checkpoint()?;
        self.integers
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native integer table allocation failed"))?;
        self.allocation_checkpoint()?;
        prepare_bucket(&mut self.integer_buckets, bucket, "native integer interner")?;
        let owned = self.fallible_owned_bytes(value, "native integer payload allocation failed")?;
        self.integers.push(owned);
        self.integer_buckets
            .entry(bucket)
            .or_default()
            .push(identifier);
        bump(
            &mut self.counters.unique_integers,
            "unique integer counter overflow",
        )?;
        Ok(identifier)
    }

    fn intern_sequence(
        &mut self,
        kind: ComponentSequenceKind,
        elements: Vec<ComponentValue>,
        transient_bytes: usize,
    ) -> NativeResult<ComponentSequenceId> {
        bump(
            &mut self.counters.sequence_requests,
            "component sequence request counter overflow",
        )?;
        let bucket_value = {
            let work = self.work.as_mut().ok_or_else(|| {
                NativeError::protocol("native component work state is unavailable")
            })?;
            component_sequence_bucket(kind, &elements, work)?
        };
        let bucket = (self.bucket_transform)(bucket_value);
        let candidate_count = self.sequence_buckets.get(&bucket).map_or(0, Vec::len);
        let mut retained = None;
        for index in 0..candidate_count {
            let identifier = self
                .sequence_buckets
                .get(&bucket)
                .and_then(|identifiers| identifiers.get(index))
                .copied()
                .ok_or_else(|| NativeError::protocol("native sequence bucket is inconsistent"))?;
            if self.metered_sequence_equal(identifier, kind, &elements)? {
                retained = Some(identifier);
                break;
            }
        }
        if let Some(identifier) = retained {
            bump(
                &mut self.counters.sequence_hits,
                "component sequence hit counter overflow",
            )?;
            self.release_transient(transient_bytes)?;
            return Ok(identifier);
        }
        self.check_new_term("native component count exceeds max_terms")?;
        check_dense_count(
            self.sequences.len(),
            "native component sequence id space exhausted",
        )?;
        let identifier = ComponentSequenceId(DenseId::try_from_index(
            self.sequences.len(),
            "native component sequence id space exhausted",
        )?);
        let height = self.values_height(&elements)?;
        let new_bucket = !self.sequence_buckets.contains_key(&bucket);
        self.promote_transient(
            transient_bytes,
            bucketed_table_charge::<FrozenComponentSequence, ComponentSequenceId>(new_bucket)?,
        )?;
        self.allocation_checkpoint()?;
        self.sequences
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("component sequence table allocation failed"))?;
        self.allocation_checkpoint()?;
        prepare_bucket(
            &mut self.sequence_buckets,
            bucket,
            "native component sequence interner",
        )?;
        self.sequences.push(FrozenComponentSequence {
            kind,
            elements,
            height,
        });
        self.sequence_buckets
            .entry(bucket)
            .or_default()
            .push(identifier);
        bump(
            &mut self.counters.unique_sequences,
            "unique component sequence counter overflow",
        )?;
        Ok(identifier)
    }

    fn push_component(
        &mut self,
        tag: u16,
        fields: Vec<ComponentValue>,
        transient_bytes: usize,
    ) -> NativeResult<LocalComponentId> {
        let category = tag_category(tag)
            .ok_or_else(|| NativeError::protocol("component tag category ledger is incomplete"))?;
        let table_len = self.table_mut(category).len();
        self.check_category_limit(category, table_len)?;
        check_dense_count(table_len, "native typed component id space exhausted")?;
        let identifier =
            DenseId::try_from_index(table_len, "native typed component id space exhausted")?;
        let height = self.values_height(&fields)?;
        self.promote_transient(transient_bytes, table_slot_charge::<FrozenComponent>()?)?;
        self.allocation_checkpoint()?;
        self.table_mut(category)
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native typed component allocation failed"))?;
        self.table_mut(category).push(FrozenComponent {
            tag,
            fields,
            height,
        });
        Ok(category.with_id(identifier))
    }

    fn table_mut(&mut self, category: ComponentCategory) -> &mut Vec<FrozenComponent> {
        match category {
            ComponentCategory::Iri => &mut self.iris,
            ComponentCategory::Entity => &mut self.entities,
            ComponentCategory::Anonymous => &mut self.anonymous,
            ComponentCategory::Literal => &mut self.literals,
            ComponentCategory::Annotation => &mut self.annotations,
            ComponentCategory::PropertyExpression => &mut self.property_expressions,
            ComponentCategory::FacetRestriction => &mut self.facet_restrictions,
            ComponentCategory::DataRange => &mut self.data_ranges,
            ComponentCategory::ClassExpression => &mut self.class_expressions,
            ComponentCategory::Axiom => &mut self.axioms,
            ComponentCategory::Swrl => &mut self.swrl,
        }
    }

    fn builder_component(&self, identifier: LocalComponentId) -> Option<&FrozenComponent> {
        match identifier {
            LocalComponentId::Iri(id) => self.iris.get(id.index()),
            LocalComponentId::Entity(id) => self.entities.get(id.index()),
            LocalComponentId::Anonymous(id) => self.anonymous.get(id.index()),
            LocalComponentId::Literal(id) => self.literals.get(id.index()),
            LocalComponentId::Annotation(id) => self.annotations.get(id.index()),
            LocalComponentId::PropertyExpression(id) => self.property_expressions.get(id.index()),
            LocalComponentId::FacetRestriction(id) => self.facet_restrictions.get(id.index()),
            LocalComponentId::DataRange(id) => self.data_ranges.get(id.index()),
            LocalComponentId::ClassExpression(id) => self.class_expressions.get(id.index()),
            LocalComponentId::Axiom(id) => self.axioms.get(id.index()),
            LocalComponentId::Swrl(id) => self.swrl.get(id.index()),
        }
    }

    fn metered_component_equal(
        &mut self,
        identifier: LocalComponentId,
        tag: u16,
        values: &[ComponentValue],
    ) -> NativeResult<bool> {
        let component = self
            .builder_component(identifier)
            .ok_or_else(|| NativeError::protocol("native component id is out of bounds"))?;
        let retained_tag = component.tag;
        let retained_length = component.fields.len();
        self.consume_work(1)?;
        if retained_tag != tag || retained_length != values.len() {
            return Ok(false);
        }
        for (index, value) in values.iter().enumerate() {
            let retained = self
                .builder_component(identifier)
                .and_then(|component| component.fields.get(index))
                .copied()
                .ok_or_else(|| NativeError::protocol("native component field is out of bounds"))?;
            self.consume_work(1)?;
            if retained != *value {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn metered_sequence_equal(
        &mut self,
        identifier: ComponentSequenceId,
        kind: ComponentSequenceKind,
        values: &[ComponentValue],
    ) -> NativeResult<bool> {
        let sequence = self
            .sequences
            .get(identifier.0.index())
            .ok_or_else(|| NativeError::protocol("native sequence id is out of bounds"))?;
        let retained_kind = sequence.kind;
        let retained_length = sequence.elements.len();
        self.consume_work(1)?;
        if retained_kind != kind || retained_length != values.len() {
            return Ok(false);
        }
        for (index, value) in values.iter().enumerate() {
            let retained = self
                .sequences
                .get(identifier.0.index())
                .and_then(|sequence| sequence.elements.get(index))
                .copied()
                .ok_or_else(|| NativeError::protocol("native sequence item is out of bounds"))?;
            self.consume_work(1)?;
            if retained != *value {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn prepare_component_bucket(&mut self, bucket: u64) -> NativeResult<()> {
        let new_bucket = !self.component_buckets.contains_key(&bucket);
        self.charge(bucket_charge::<LocalComponentId>(new_bucket)?)?;
        self.allocation_checkpoint()?;
        prepare_bucket(
            &mut self.component_buckets,
            bucket,
            "native component interner",
        )
    }

    fn check_new_term(&self, message: &'static str) -> NativeResult<()> {
        let following = self
            .counters
            .unique_nodes
            .checked_add(self.counters.unique_sequences)
            .and_then(|value| value.checked_add(1))
            .ok_or_else(|| NativeError::limit(message))?;
        if following > self.limits.max_terms {
            return Err(NativeError::limit(message));
        }
        Ok(())
    }

    fn check_category_limit(
        &self,
        category: ComponentCategory,
        current: usize,
    ) -> NativeResult<()> {
        let (maximum, message) = match category {
            ComponentCategory::Annotation => (
                self.limits.max_annotations,
                "native annotation table exceeds max_annotations",
            ),
            ComponentCategory::Axiom => (
                self.limits.max_axioms,
                "native axiom table exceeds max_axioms",
            ),
            _ => return Ok(()),
        };
        check_count(current, maximum, message)
    }

    fn drop_interners(&mut self) {
        self.string_buckets = HashMap::new();
        self.bytes_buckets = HashMap::new();
        self.integer_buckets = HashMap::new();
        self.sequence_buckets = HashMap::new();
        self.component_buckets = HashMap::new();
    }

    fn freeze_workspace_bound(&self, work: &mut ComponentWork) -> NativeResult<(usize, u32)> {
        let scalar_and_sequence = self
            .strings
            .len()
            .checked_add(self.bytes.len())
            .and_then(|value| value.checked_add(self.integers.len()))
            .and_then(|value| value.checked_add(self.sequences.len()))
            .ok_or_else(|| NativeError::limit("native freeze workspace count overflow"))?;
        let component_count = self.component_count()?;
        let mapping_count = scalar_and_sequence
            .checked_add(component_count)
            .ok_or_else(|| NativeError::limit("native freeze workspace count overflow"))?;
        let largest = self
            .table_lengths()
            .into_iter()
            .chain([
                self.strings.len(),
                self.bytes.len(),
                self.integers.len(),
                self.sequences.len(),
            ])
            .max()
            .unwrap_or(0);
        let base_allocation_count = self
            .table_lengths()
            .into_iter()
            .chain([
                self.strings.len(),
                self.bytes.len(),
                self.integers.len(),
                self.sequences.len(),
            ])
            .filter(|length| *length != 0)
            .count();
        let rank_items = component_count
            .checked_add(self.sequences.len())
            .ok_or_else(|| NativeError::limit("native freeze rank count overflow"))?;
        let mut edge_count = 0_usize;
        let mut maximum_height = 0_u32;
        for sequence in &self.sequences {
            work.consume(1)?;
            edge_count = edge_count
                .checked_add(sequence.elements.len())
                .ok_or_else(|| NativeError::limit("native freeze edge count overflow"))?;
            maximum_height = maximum_height.max(sequence.height);
        }
        for category in ComponentCategory::ALL {
            for component in self.component_table(category) {
                work.consume(1)?;
                edge_count = edge_count
                    .checked_add(component.fields.len())
                    .ok_or_else(|| NativeError::limit("native freeze edge count overflow"))?;
                maximum_height = maximum_height.max(component.height);
            }
        }
        let height_count = usize::try_from(maximum_height)
            .map_err(|_| NativeError::limit("native component height exceeds usize"))?
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native component height count overflow"))?;
        let height_offset_count = height_count
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native component height offsets overflow"))?;
        let height_plan_count = ComponentCategory::COUNT
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native height plan count overflow"))?;
        let height_offset_slots = height_offset_count
            .checked_mul(height_plan_count)
            .ok_or_else(|| NativeError::limit("native height plan offsets overflow"))?;
        let height_plan_allocations = height_plan_count
            .checked_mul(2)
            .and_then(|value| value.checked_add(2))
            .ok_or_else(|| NativeError::limit("native height plan allocation count overflow"))?;
        let allocation_count = base_allocation_count
            .checked_add(rank_items)
            .and_then(|value| value.checked_add(4))
            .and_then(|value| value.checked_add(height_plan_allocations))
            .ok_or_else(|| NativeError::limit("native freeze workspace count overflow"))?;
        let key_words = edge_count
            .checked_mul(3)
            .and_then(|value| {
                rank_items
                    .checked_mul(3)
                    .and_then(|base| value.checked_add(base))
            })
            .ok_or_else(|| NativeError::limit("native freeze order key size overflow"))?;
        let workspace = mapping_count
            .checked_mul(size_of::<DenseId>())
            .and_then(|value| {
                rank_items
                    .checked_mul(size_of::<RankedIndex>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                key_words
                    .checked_mul(size_of::<u64>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                largest
                    .checked_mul(2 * size_of::<usize>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                largest
                    .checked_mul(size_of::<DenseId>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                rank_items
                    .checked_mul(size_of::<usize>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                height_offset_slots
                    .checked_mul(size_of::<usize>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                height_count
                    .checked_mul(size_of::<usize>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                ComponentCategory::COUNT
                    .checked_mul(size_of::<HeightPlan>())
                    .and_then(|temporary| value.checked_add(temporary))
            })
            .and_then(|value| {
                allocation_count
                    .checked_mul(HEAP_ALLOCATION_OVERHEAD)
                    .and_then(|overhead| value.checked_add(overhead))
            })
            .ok_or_else(|| NativeError::limit("native freeze workspace size overflow"))?;
        Ok((workspace, maximum_height))
    }

    fn component_count(&self) -> NativeResult<usize> {
        self.table_lengths()
            .into_iter()
            .try_fold(0_usize, |total, value| {
                total
                    .checked_add(value)
                    .ok_or_else(|| NativeError::limit("native component count overflow"))
            })
    }

    fn table_lengths(&self) -> [usize; 11] {
        [
            self.iris.len(),
            self.entities.len(),
            self.anonymous.len(),
            self.literals.len(),
            self.annotations.len(),
            self.property_expressions.len(),
            self.facet_restrictions.len(),
            self.data_ranges.len(),
            self.class_expressions.len(),
            self.axioms.len(),
            self.swrl.len(),
        ]
    }

    fn check_temporary(&mut self, bytes: usize, external_bytes: u64) -> NativeResult<()> {
        let bytes = u64::try_from(bytes)
            .map_err(|_| NativeError::limit("native freeze workspace exceeds u64"))?;
        let peak = self
            .accounted_bytes
            .checked_add(external_bytes)
            .and_then(|value| value.checked_add(bytes))
            .ok_or_else(|| NativeError::limit("native freeze memory accounting overflow"))?;
        if self
            .limits
            .max_memory_bytes
            .is_some_and(|maximum| peak > maximum)
        {
            return Err(NativeError::limit(
                "native freeze workspace exceeds max_memory_bytes",
            ));
        }
        self.counters.peak_builder_bytes = self.counters.peak_builder_bytes.max(peak);
        Ok(())
    }

    fn apply_freeze_remaps(
        &mut self,
        remaps: &FreezeRemaps,
        work: &mut ComponentWork,
    ) -> NativeResult<()> {
        reorder(&mut self.strings, &remaps.strings, work)?;
        reorder(&mut self.bytes, &remaps.bytes, work)?;
        reorder(&mut self.integers, &remaps.integers, work)?;
        reorder(&mut self.sequences, &remaps.sequences, work)?;
        reorder(&mut self.iris, &remaps.components.iris, work)?;
        reorder(&mut self.entities, &remaps.components.entities, work)?;
        reorder(&mut self.anonymous, &remaps.components.anonymous, work)?;
        reorder(&mut self.literals, &remaps.components.literals, work)?;
        reorder(&mut self.annotations, &remaps.components.annotations, work)?;
        reorder(
            &mut self.property_expressions,
            &remaps.components.property_expressions,
            work,
        )?;
        reorder(
            &mut self.facet_restrictions,
            &remaps.components.facet_restrictions,
            work,
        )?;
        reorder(&mut self.data_ranges, &remaps.components.data_ranges, work)?;
        reorder(
            &mut self.class_expressions,
            &remaps.components.class_expressions,
            work,
        )?;
        reorder(&mut self.axioms, &remaps.components.axioms, work)?;
        reorder(&mut self.swrl, &remaps.components.swrl, work)?;

        for sequence in &mut self.sequences {
            for value in &mut sequence.elements {
                *value = remaps.resolve_value(*value)?;
                work.consume(1)?;
            }
        }
        for table in [
            &mut self.iris,
            &mut self.entities,
            &mut self.anonymous,
            &mut self.literals,
            &mut self.annotations,
            &mut self.property_expressions,
            &mut self.facet_restrictions,
            &mut self.data_ranges,
            &mut self.class_expressions,
            &mut self.axioms,
            &mut self.swrl,
        ] {
            for component in table {
                for value in &mut component.fields {
                    *value = remaps.resolve_value(*value)?;
                    work.consume(1)?;
                }
            }
        }
        Ok(())
    }

    fn charge(&mut self, additional: usize) -> NativeResult<()> {
        let additional = u64::try_from(additional)
            .map_err(|_| NativeError::limit("native component allocation exceeds u64"))?;
        let following = self
            .accounted_bytes
            .checked_add(additional)
            .ok_or_else(|| NativeError::limit("native component memory accounting overflow"))?;
        let peak = following
            .checked_add(self.transient_bytes)
            .and_then(|value| value.checked_add(self.external_bytes().ok()?))
            .ok_or_else(|| NativeError::limit("native component memory accounting overflow"))?;
        if self
            .limits
            .max_memory_bytes
            .is_some_and(|maximum| peak > maximum)
        {
            return Err(NativeError::limit(
                "native component allocation exceeds max_memory_bytes",
            ));
        }
        self.accounted_bytes = following;
        self.counters.peak_builder_bytes = self.counters.peak_builder_bytes.max(peak);
        Ok(())
    }

    fn promote_transient(&mut self, promoted: usize, overhead: usize) -> NativeResult<()> {
        let promoted = u64::try_from(promoted)
            .map_err(|_| NativeError::limit("native promoted allocation exceeds u64"))?;
        let overhead = u64::try_from(overhead)
            .map_err(|_| NativeError::limit("native retained allocation exceeds u64"))?;
        let remaining = self
            .transient_bytes
            .checked_sub(promoted)
            .ok_or_else(|| NativeError::protocol("native transient promotion underflow"))?;
        let retained = self
            .accounted_bytes
            .checked_add(promoted)
            .and_then(|value| value.checked_add(overhead))
            .ok_or_else(|| NativeError::limit("native component memory accounting overflow"))?;
        let peak = retained
            .checked_add(remaining)
            .and_then(|value| value.checked_add(self.external_bytes().ok()?))
            .ok_or_else(|| NativeError::limit("native component memory accounting overflow"))?;
        if self
            .limits
            .max_memory_bytes
            .is_some_and(|maximum| peak > maximum)
        {
            return Err(NativeError::limit(
                "native component allocation exceeds max_memory_bytes",
            ));
        }
        self.accounted_bytes = retained;
        self.transient_bytes = remaining;
        self.counters.peak_builder_bytes = self.counters.peak_builder_bytes.max(peak);
        Ok(())
    }

    fn reserve_transient(&mut self, additional: usize) -> NativeResult<()> {
        let additional = u64::try_from(additional)
            .map_err(|_| NativeError::limit("native transient allocation exceeds u64"))?;
        let following = self
            .transient_bytes
            .checked_add(additional)
            .ok_or_else(|| NativeError::limit("native transient memory accounting overflow"))?;
        let peak = self
            .accounted_bytes
            .checked_add(following)
            .and_then(|value| value.checked_add(self.external_bytes().ok()?))
            .ok_or_else(|| NativeError::limit("native component memory accounting overflow"))?;
        if self
            .limits
            .max_memory_bytes
            .is_some_and(|maximum| peak > maximum)
        {
            return Err(NativeError::limit(
                "native transient allocation exceeds max_memory_bytes",
            ));
        }
        self.transient_bytes = following;
        self.counters.peak_builder_bytes = self.counters.peak_builder_bytes.max(peak);
        Ok(())
    }

    fn release_transient(&mut self, released: usize) -> NativeResult<()> {
        let released = u64::try_from(released)
            .map_err(|_| NativeError::protocol("native transient release exceeds u64"))?;
        self.transient_bytes = self
            .transient_bytes
            .checked_sub(released)
            .ok_or_else(|| NativeError::protocol("native transient memory accounting underflow"))?;
        Ok(())
    }
}

#[derive(Debug)]
struct FreezeRemaps {
    strings: Vec<DenseId>,
    bytes: Vec<DenseId>,
    integers: Vec<DenseId>,
    sequences: Vec<DenseId>,
    components: ComponentIdRemap,
}

#[derive(Debug)]
struct HeightPlan {
    offsets: Vec<usize>,
    indices: Vec<usize>,
}

impl HeightPlan {
    fn build<T>(
        values: &[T],
        maximum_height: u32,
        height_of: impl Fn(&T) -> u32,
        work: &mut ComponentWork,
    ) -> NativeResult<Self> {
        let height_count = usize::try_from(maximum_height)
            .map_err(|_| NativeError::limit("native component height exceeds usize"))?
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native component height count overflow"))?;
        let offset_count = height_count
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native component height offsets overflow"))?;

        work.allocation_checkpoint()?;
        let mut offsets = Vec::new();
        offsets
            .try_reserve_exact(offset_count)
            .map_err(|_| NativeError::limit("native height offsets allocation failed"))?;
        for _ in 0..offset_count {
            offsets.push(0_usize);
            work.consume(1)?;
        }
        for value in values {
            let height = usize::try_from(height_of(value))
                .map_err(|_| NativeError::limit("native component height exceeds usize"))?;
            let count =
                offsets
                    .get_mut(height.checked_add(1).ok_or_else(|| {
                        NativeError::limit("native component height index overflow")
                    })?)
                    .ok_or_else(|| {
                        NativeError::protocol("native component height exceeds freeze plan")
                    })?;
            *count = count
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native component height count overflow"))?;
            work.consume(1)?;
        }
        for index in 1..offsets.len() {
            offsets[index] = offsets[index]
                .checked_add(offsets[index - 1])
                .ok_or_else(|| NativeError::limit("native height offset overflow"))?;
            work.consume(1)?;
        }

        work.allocation_checkpoint()?;
        let mut positions = Vec::new();
        positions
            .try_reserve_exact(height_count)
            .map_err(|_| NativeError::limit("native height cursor allocation failed"))?;
        for offset in offsets.iter().take(height_count) {
            positions.push(*offset);
            work.consume(1)?;
        }

        work.allocation_checkpoint()?;
        let mut indices = Vec::new();
        indices
            .try_reserve_exact(values.len())
            .map_err(|_| NativeError::limit("native height index allocation failed"))?;
        for _ in values {
            indices.push(0_usize);
            work.consume(1)?;
        }
        for (index, value) in values.iter().enumerate() {
            let height = usize::try_from(height_of(value))
                .map_err(|_| NativeError::limit("native component height exceeds usize"))?;
            let position = positions.get_mut(height).ok_or_else(|| {
                NativeError::protocol("native component height exceeds freeze plan")
            })?;
            let destination = indices.get_mut(*position).ok_or_else(|| {
                NativeError::protocol("native height plan destination is out of bounds")
            })?;
            *destination = index;
            *position = position
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native height cursor overflow"))?;
            work.consume(1)?;
        }
        for height in 0..height_count {
            if positions[height] != offsets[height + 1] {
                return Err(NativeError::protocol("native height plan count diverged"));
            }
            work.consume(1)?;
        }
        Ok(Self { offsets, indices })
    }

    fn at(&self, height: u32) -> NativeResult<&[usize]> {
        let height = usize::try_from(height)
            .map_err(|_| NativeError::limit("native component height exceeds usize"))?;
        let start = self
            .offsets
            .get(height)
            .copied()
            .ok_or_else(|| NativeError::protocol("native height plan is incomplete"))?;
        let end = self
            .offsets
            .get(
                height
                    .checked_add(1)
                    .ok_or_else(|| NativeError::limit("native component height overflow"))?,
            )
            .copied()
            .ok_or_else(|| NativeError::protocol("native height plan is incomplete"))?;
        self.indices
            .get(start..end)
            .ok_or_else(|| NativeError::protocol("native height plan range is invalid"))
    }
}

#[derive(Debug)]
struct FreezeHeightPlans {
    sequences: HeightPlan,
    components: Vec<HeightPlan>,
}

impl FreezeHeightPlans {
    fn build(
        builder: &NativeComponentBuilder,
        maximum_height: u32,
        work: &mut ComponentWork,
    ) -> NativeResult<Self> {
        let sequences = HeightPlan::build(
            &builder.sequences,
            maximum_height,
            |sequence| sequence.height,
            work,
        )?;
        work.allocation_checkpoint()?;
        let mut components = Vec::new();
        components
            .try_reserve_exact(ComponentCategory::COUNT)
            .map_err(|_| NativeError::limit("native component height plans allocation failed"))?;
        for category in ComponentCategory::ALL {
            components.push(HeightPlan::build(
                builder.component_table(category),
                maximum_height,
                |component| component.height,
                work,
            )?);
        }
        Ok(Self {
            sequences,
            components,
        })
    }

    fn component(&self, category: ComponentCategory) -> NativeResult<&HeightPlan> {
        self.components
            .get(category.index())
            .ok_or_else(|| NativeError::protocol("native component height plan is incomplete"))
    }
}

impl FreezeRemaps {
    fn build(
        builder: &NativeComponentBuilder,
        maximum_height: u32,
        work: &mut ComponentWork,
    ) -> NativeResult<Self> {
        let strings = sorted_remap(builder.strings.len(), work, |left, right, work| {
            metered_slice_compare(&builder.strings[left], &builder.strings[right], work)
        })?;
        let bytes = sorted_remap(builder.bytes.len(), work, |left, right, work| {
            metered_slice_compare(&builder.bytes[left], &builder.bytes[right], work)
        })?;
        let integers = sorted_remap(builder.integers.len(), work, |left, right, work| {
            metered_slice_compare(&builder.integers[left], &builder.integers[right], work)
        })?;

        let height_plans = FreezeHeightPlans::build(builder, maximum_height, work)?;
        let mut sequences = sentinel_mapping(builder.sequences.len(), work)?;
        let mut components = ComponentIdRemap::sentinel(builder, work)?;
        let mut next_sequences = 0_usize;
        let mut next_components = [0_usize; ComponentCategory::COUNT];
        for height in 1..=maximum_height {
            rank_sequence_height(
                builder,
                height,
                height_plans.sequences.at(height)?,
                &strings,
                &bytes,
                &integers,
                &mut sequences,
                &components,
                &mut next_sequences,
                work,
            )?;
            for category in ComponentCategory::ALL {
                rank_component_height(
                    builder,
                    category,
                    height,
                    height_plans.component(category)?.at(height)?,
                    &strings,
                    &bytes,
                    &integers,
                    &sequences,
                    &mut components,
                    &mut next_components[category.index()],
                    work,
                )?;
            }
            work.consume(1)?;
        }
        verify_complete_mapping(
            &sequences,
            "native sequence rank mapping is incomplete",
            work,
        )?;
        components.verify_complete(work)?;
        Ok(Self {
            strings,
            bytes,
            integers,
            sequences,
            components,
        })
    }

    fn resolve_value(&self, value: ComponentValue) -> NativeResult<ComponentValue> {
        match value {
            ComponentValue::None => Ok(value),
            ComponentValue::String(kind, identifier) => Ok(ComponentValue::String(
                kind,
                StringId(resolve_dense(&self.strings, identifier.0, "string")?),
            )),
            ComponentValue::Bytes(identifier) => Ok(ComponentValue::Bytes(BytesId(resolve_dense(
                &self.bytes,
                identifier.0,
                "bytes",
            )?))),
            ComponentValue::Integer(identifier) => Ok(ComponentValue::Integer(IntegerId(
                resolve_dense(&self.integers, identifier.0, "integer")?,
            ))),
            ComponentValue::Node(identifier) => Ok(ComponentValue::Node(
                self.components.resolve_component(identifier)?,
            )),
            ComponentValue::Sequence(identifier) => Ok(ComponentValue::Sequence(
                ComponentSequenceId(resolve_dense(&self.sequences, identifier.0, "sequence")?),
            )),
        }
    }
}

impl NativeComponentBuilder {
    fn component_table(&self, category: ComponentCategory) -> &[FrozenComponent] {
        match category {
            ComponentCategory::Iri => &self.iris,
            ComponentCategory::Entity => &self.entities,
            ComponentCategory::Anonymous => &self.anonymous,
            ComponentCategory::Literal => &self.literals,
            ComponentCategory::Annotation => &self.annotations,
            ComponentCategory::PropertyExpression => &self.property_expressions,
            ComponentCategory::FacetRestriction => &self.facet_restrictions,
            ComponentCategory::DataRange => &self.data_ranges,
            ComponentCategory::ClassExpression => &self.class_expressions,
            ComponentCategory::Axiom => &self.axioms,
            ComponentCategory::Swrl => &self.swrl,
        }
    }
}

fn sorted_remap(
    length: usize,
    work: &mut ComponentWork,
    mut compare: impl FnMut(usize, usize, &mut ComponentWork) -> NativeResult<Ordering>,
) -> NativeResult<Vec<DenseId>> {
    let order = fallible_index_order(length, work, |left, right, work| compare(left, right, work))?;
    for pair in order.windows(2) {
        if compare(pair[0], pair[1], work)? == Ordering::Equal {
            return Err(NativeError::protocol(
                "native component interner retained a structural duplicate",
            ));
        }
    }
    work.allocation_checkpoint()?;
    let mut mapping = Vec::new();
    mapping
        .try_reserve_exact(length)
        .map_err(|_| NativeError::limit("native freeze remap allocation failed"))?;
    for _ in 0..length {
        mapping.push(DenseId(0));
        work.consume(1)?;
    }
    for (new, old) in order.into_iter().enumerate() {
        mapping[old] = DenseId::try_from_index(new, "native frozen id space exhausted")?;
        work.consume(1)?;
    }
    Ok(mapping)
}

const UNASSIGNED_DENSE: DenseId = DenseId(u32::MAX);

fn sentinel_mapping(length: usize, work: &mut ComponentWork) -> NativeResult<Vec<DenseId>> {
    work.allocation_checkpoint()?;
    let mut mapping = Vec::new();
    mapping
        .try_reserve_exact(length)
        .map_err(|_| NativeError::limit("native freeze rank allocation failed"))?;
    for _ in 0..length {
        mapping.push(UNASSIGNED_DENSE);
        work.consume(1)?;
    }
    Ok(mapping)
}

fn verify_complete_mapping(
    mapping: &[DenseId],
    message: &'static str,
    work: &mut ComponentWork,
) -> NativeResult<()> {
    for identifier in mapping {
        work.consume(1)?;
        if *identifier == UNASSIGNED_DENSE {
            return Err(NativeError::protocol(message));
        }
    }
    Ok(())
}

fn fallible_index_order(
    length: usize,
    work: &mut ComponentWork,
    mut compare: impl FnMut(usize, usize, &mut ComponentWork) -> NativeResult<Ordering>,
) -> NativeResult<Vec<usize>> {
    work.allocation_checkpoint()?;
    let mut order = Vec::new();
    order
        .try_reserve_exact(length)
        .map_err(|_| NativeError::limit("native freeze sort allocation failed"))?;
    for index in 0..length {
        order.push(index);
        work.consume(1)?;
    }
    if length < 2 {
        return Ok(order);
    }
    work.allocation_checkpoint()?;
    let mut scratch = Vec::new();
    scratch
        .try_reserve_exact(length)
        .map_err(|_| NativeError::limit("native freeze sort workspace allocation failed"))?;
    for _ in 0..length {
        scratch.push(0);
        work.consume(1)?;
    }
    let mut width = 1_usize;
    while width < length {
        let step = width
            .checked_mul(2)
            .ok_or_else(|| NativeError::limit("native freeze sort width overflow"))?;
        let mut start = 0_usize;
        while start < length {
            let middle = start.saturating_add(width).min(length);
            let end = start.saturating_add(step).min(length);
            let (mut left, mut right, mut output) = (start, middle, start);
            while left < middle && right < end {
                if compare(order[left], order[right], work)? != Ordering::Greater {
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
    Ok(order)
}

fn metered_slice_compare<T: Ord>(
    left: &[T],
    right: &[T],
    work: &mut ComponentWork,
) -> NativeResult<Ordering> {
    for (left, right) in left.iter().zip(right) {
        work.consume(1)?;
        let ordering = left.cmp(right);
        if ordering != Ordering::Equal {
            return Ok(ordering);
        }
    }
    work.consume(1)?;
    Ok(left.len().cmp(&right.len()))
}

fn metered_slice_equal<T: Eq>(
    left: &[T],
    right: &[T],
    work: &mut ComponentWork,
) -> NativeResult<bool> {
    work.consume(1)?;
    if left.len() != right.len() {
        return Ok(false);
    }
    for (left, right) in left.iter().zip(right) {
        work.consume(1)?;
        if left != right {
            return Ok(false);
        }
    }
    Ok(true)
}

#[derive(Debug)]
struct RankedIndex {
    index: usize,
    key: Vec<u64>,
}

#[allow(clippy::too_many_arguments)]
fn rank_sequence_height(
    builder: &NativeComponentBuilder,
    height: u32,
    selected_indices: &[usize],
    strings: &[DenseId],
    bytes: &[DenseId],
    integers: &[DenseId],
    sequences: &mut [DenseId],
    components: &ComponentIdRemap,
    next_rank: &mut usize,
    work: &mut ComponentWork,
) -> NativeResult<()> {
    let mut batch = ranked_batch(selected_indices.len(), work)?;
    for index in selected_indices {
        let sequence = builder.sequences.get(*index).ok_or_else(|| {
            NativeError::protocol("native sequence height index is out of bounds")
        })?;
        if sequence.height != height {
            return Err(NativeError::protocol(
                "native sequence height plan selected the wrong row",
            ));
        }
        work.consume(1)?;
        let key = structural_order_key(
            1,
            u64::from(sequence_kind_rank(sequence.kind)),
            &sequence.elements,
            builder,
            strings,
            bytes,
            integers,
            sequences,
            components,
            work,
        )?;
        batch.push(RankedIndex { index: *index, key });
    }
    assign_rank_batch(
        &batch,
        sequences,
        next_rank,
        "native sequence interner retained a structural duplicate",
        work,
    )
}

#[allow(clippy::too_many_arguments)]
fn rank_component_height(
    builder: &NativeComponentBuilder,
    category: ComponentCategory,
    height: u32,
    selected_indices: &[usize],
    strings: &[DenseId],
    bytes: &[DenseId],
    integers: &[DenseId],
    sequences: &[DenseId],
    components: &mut ComponentIdRemap,
    next_rank: &mut usize,
    work: &mut ComponentWork,
) -> NativeResult<()> {
    let table = builder.component_table(category);
    let mut batch = ranked_batch(selected_indices.len(), work)?;
    for index in selected_indices {
        let component = table.get(*index).ok_or_else(|| {
            NativeError::protocol("native component height index is out of bounds")
        })?;
        if component.height != height {
            return Err(NativeError::protocol(
                "native component height plan selected the wrong row",
            ));
        }
        work.consume(1)?;
        let key = structural_order_key(
            0,
            u64::from(component.tag),
            &component.fields,
            builder,
            strings,
            bytes,
            integers,
            sequences,
            components,
            work,
        )?;
        batch.push(RankedIndex { index: *index, key });
    }
    assign_rank_batch(
        &batch,
        components.mapping_mut(category),
        next_rank,
        "native component interner retained a structural duplicate",
        work,
    )
}

fn ranked_batch(length: usize, work: &mut ComponentWork) -> NativeResult<Vec<RankedIndex>> {
    work.allocation_checkpoint()?;
    let mut batch = Vec::new();
    batch
        .try_reserve_exact(length)
        .map_err(|_| NativeError::limit("native structural rank batch allocation failed"))?;
    Ok(batch)
}

fn assign_rank_batch(
    batch: &[RankedIndex],
    mapping: &mut [DenseId],
    next_rank: &mut usize,
    duplicate_message: &'static str,
    work: &mut ComponentWork,
) -> NativeResult<()> {
    let order = fallible_index_order(batch.len(), work, |left, right, work| {
        metered_slice_compare(&batch[left].key, &batch[right].key, work)
    })?;
    for pair in order.windows(2) {
        if metered_slice_compare(&batch[pair[0]].key, &batch[pair[1]].key, work)? == Ordering::Equal
        {
            return Err(NativeError::protocol(duplicate_message));
        }
    }
    for selected in order {
        let item = batch
            .get(selected)
            .ok_or_else(|| NativeError::protocol("native rank order is out of bounds"))?;
        let destination = mapping
            .get_mut(item.index)
            .ok_or_else(|| NativeError::protocol("native rank mapping is out of bounds"))?;
        if *destination != UNASSIGNED_DENSE {
            return Err(NativeError::protocol(
                "native rank mapping assigned a component twice",
            ));
        }
        *destination = DenseId::try_from_index(*next_rank, "native rank id space exhausted")?;
        *next_rank = next_rank
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native rank count overflow"))?;
        work.consume(1)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn structural_order_key(
    structure_kind: u64,
    tag_or_kind: u64,
    values: &[ComponentValue],
    builder: &NativeComponentBuilder,
    strings: &[DenseId],
    bytes: &[DenseId],
    integers: &[DenseId],
    sequences: &[DenseId],
    components: &ComponentIdRemap,
    work: &mut ComponentWork,
) -> NativeResult<Vec<u64>> {
    let capacity = values
        .len()
        .checked_mul(3)
        .and_then(|value| value.checked_add(3))
        .ok_or_else(|| NativeError::limit("native structural order key size overflow"))?;
    work.allocation_checkpoint()?;
    let mut key = Vec::new();
    key.try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native structural order key allocation failed"))?;
    key.extend([
        structure_kind,
        tag_or_kind,
        u64::try_from(values.len())
            .map_err(|_| NativeError::limit("native structural arity exceeds u64"))?,
    ]);
    work.consume(3)?;
    for value in values {
        let (marker, subtype, rank) = stable_value_key(
            *value, builder, strings, bytes, integers, sequences, components,
        )?;
        key.extend([marker, subtype, rank]);
        work.consume(3)?;
    }
    if key.len() != capacity {
        return Err(NativeError::protocol(
            "native structural order key length diverged",
        ));
    }
    Ok(key)
}

#[allow(clippy::too_many_arguments)]
fn stable_value_key(
    value: ComponentValue,
    builder: &NativeComponentBuilder,
    strings: &[DenseId],
    bytes: &[DenseId],
    integers: &[DenseId],
    sequences: &[DenseId],
    components: &ComponentIdRemap,
) -> NativeResult<(u64, u64, u64)> {
    match value {
        ComponentValue::None => Ok((0, 0, 0)),
        ComponentValue::String(ScalarKind::Text, identifier) => Ok((
            2,
            0,
            u64::from(
                assigned_rank(strings, identifier.0, "native string rank is unavailable")?.raw(),
            ),
        )),
        ComponentValue::Bytes(identifier) => Ok((
            3,
            0,
            u64::from(
                assigned_rank(bytes, identifier.0, "native bytes rank is unavailable")?.raw(),
            ),
        )),
        ComponentValue::Integer(identifier) => Ok((
            4,
            0,
            u64::from(
                assigned_rank(integers, identifier.0, "native integer rank is unavailable")?.raw(),
            ),
        )),
        ComponentValue::String(ScalarKind::Enum, identifier) => Ok((
            5,
            0,
            u64::from(
                assigned_rank(strings, identifier.0, "native string rank is unavailable")?.raw(),
            ),
        )),
        ComponentValue::Node(identifier) => {
            let (category, rank) = components.stable_rank(identifier)?;
            Ok((1, category, rank))
        }
        ComponentValue::Sequence(identifier) => {
            let sequence = builder.sequences.get(identifier.0.index()).ok_or_else(|| {
                NativeError::protocol("native component sequence id is out of bounds")
            })?;
            Ok((
                u64::from(sequence_marker(sequence.kind)),
                0,
                u64::from(
                    assigned_rank(
                        sequences,
                        identifier.0,
                        "native sequence rank is unavailable",
                    )?
                    .raw(),
                ),
            ))
        }
    }
}

fn assigned_rank(
    mapping: &[DenseId],
    old: DenseId,
    message: &'static str,
) -> NativeResult<DenseId> {
    let rank = mapping
        .get(old.index())
        .copied()
        .ok_or_else(|| NativeError::protocol(message))?;
    if rank == UNASSIGNED_DENSE {
        return Err(NativeError::protocol(message));
    }
    Ok(rank)
}

fn resolve_dense(mapping: &[DenseId], old: DenseId, label: &'static str) -> NativeResult<DenseId> {
    mapping.get(old.index()).copied().ok_or_else(|| {
        NativeError::protocol(match label {
            "string" => "native string remap is incomplete",
            "bytes" => "native bytes remap is incomplete",
            "integer" => "native integer remap is incomplete",
            "sequence" => "native sequence remap is incomplete",
            _ => "native dense remap is incomplete",
        })
    })
}

fn reorder<T>(values: &mut [T], mapping: &[DenseId], work: &mut ComponentWork) -> NativeResult<()> {
    if values.len() != mapping.len() {
        return Err(NativeError::protocol(
            "native freeze permutation length mismatch",
        ));
    }
    work.allocation_checkpoint()?;
    let mut destinations = Vec::new();
    destinations
        .try_reserve_exact(mapping.len())
        .map_err(|_| NativeError::limit("native freeze permutation allocation failed"))?;
    for destination in mapping {
        destinations.push(*destination);
        work.consume(1)?;
    }
    for index in 0..destinations.len() {
        while destinations[index].index() != index {
            let destination = destinations[index].index();
            if destination >= destinations.len() {
                return Err(NativeError::protocol(
                    "native freeze permutation is out of bounds",
                ));
            }
            values.swap(index, destination);
            destinations.swap(index, destination);
            work.consume(1)?;
        }
    }
    Ok(())
}

const fn sequence_kind_rank(kind: ComponentSequenceKind) -> u8 {
    match kind {
        ComponentSequenceKind::CanonicalSet => 0,
        ComponentSequenceKind::Ordered => 1,
    }
}

const fn sequence_marker(kind: ComponentSequenceKind) -> u8 {
    match kind {
        ComponentSequenceKind::CanonicalSet => 6,
        ComponentSequenceKind::Ordered => 7,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ComponentCategory {
    Iri,
    Entity,
    Anonymous,
    Literal,
    Annotation,
    PropertyExpression,
    FacetRestriction,
    DataRange,
    ClassExpression,
    Axiom,
    Swrl,
}

impl ComponentCategory {
    const COUNT: usize = 11;
    const ALL: [Self; Self::COUNT] = [
        Self::Iri,
        Self::Entity,
        Self::Anonymous,
        Self::Literal,
        Self::Annotation,
        Self::PropertyExpression,
        Self::FacetRestriction,
        Self::DataRange,
        Self::ClassExpression,
        Self::Axiom,
        Self::Swrl,
    ];

    const fn index(self) -> usize {
        match self {
            Self::Iri => 0,
            Self::Entity => 1,
            Self::Anonymous => 2,
            Self::Literal => 3,
            Self::Annotation => 4,
            Self::PropertyExpression => 5,
            Self::FacetRestriction => 6,
            Self::DataRange => 7,
            Self::ClassExpression => 8,
            Self::Axiom => 9,
            Self::Swrl => 10,
        }
    }

    const fn order_code(self) -> u64 {
        self.index() as u64
    }

    const fn with_id(self, identifier: DenseId) -> LocalComponentId {
        match self {
            Self::Iri => LocalComponentId::Iri(identifier),
            Self::Entity => LocalComponentId::Entity(identifier),
            Self::Anonymous => LocalComponentId::Anonymous(identifier),
            Self::Literal => LocalComponentId::Literal(identifier),
            Self::Annotation => LocalComponentId::Annotation(identifier),
            Self::PropertyExpression => LocalComponentId::PropertyExpression(identifier),
            Self::FacetRestriction => LocalComponentId::FacetRestriction(identifier),
            Self::DataRange => LocalComponentId::DataRange(identifier),
            Self::ClassExpression => LocalComponentId::ClassExpression(identifier),
            Self::Axiom => LocalComponentId::Axiom(identifier),
            Self::Swrl => LocalComponentId::Swrl(identifier),
        }
    }
}

fn tag_category(tag: u16) -> Option<ComponentCategory> {
    match tag {
        1 => Some(ComponentCategory::Iri),
        2 => Some(ComponentCategory::Entity),
        3 => Some(ComponentCategory::Anonymous),
        4 => Some(ComponentCategory::Literal),
        5 => Some(ComponentCategory::Annotation),
        10 | 11 => Some(ComponentCategory::PropertyExpression),
        20 => Some(ComponentCategory::FacetRestriction),
        21..=25 => Some(ComponentCategory::DataRange),
        30..=46 => Some(ComponentCategory::ClassExpression),
        60..=123 => Some(ComponentCategory::Axiom),
        140..=148 => Some(ComponentCategory::Swrl),
        _ => None,
    }
}

const HEAP_ALLOCATION_OVERHEAD: usize = 16;
const HASH_BUCKET_BYTES: usize = 96;

fn retained_builder_bytes(
    builder: &NativeComponentBuilder,
    work: &mut ComponentWork,
) -> NativeResult<u64> {
    retained_storage_bytes(
        &builder.strings,
        &builder.bytes,
        &builder.integers,
        &builder.sequences,
        [
            &builder.iris,
            &builder.entities,
            &builder.anonymous,
            &builder.literals,
            &builder.annotations,
            &builder.property_expressions,
            &builder.facet_restrictions,
            &builder.data_ranges,
            &builder.class_expressions,
            &builder.axioms,
            &builder.swrl,
        ],
        0,
        work,
    )
}

fn retained_table_bytes(tables: &ComponentTables, work: &mut ComponentWork) -> NativeResult<u64> {
    retained_storage_bytes(
        &tables.strings,
        &tables.bytes,
        &tables.integers,
        &tables.sequences,
        [
            &tables.iris,
            &tables.entities,
            &tables.anonymous,
            &tables.literals,
            &tables.annotations,
            &tables.property_expressions,
            &tables.facet_restrictions,
            &tables.data_ranges,
            &tables.class_expressions,
            &tables.axioms,
            &tables.swrl,
        ],
        size_of::<ComponentTables>() + 2 * size_of::<usize>() + HEAP_ALLOCATION_OVERHEAD,
        work,
    )
}

fn component_remap_retained_bytes(remap: &ComponentIdRemap) -> NativeResult<u64> {
    let mut total = size_of::<ComponentIdRemap>();
    for mapping in [
        &remap.iris,
        &remap.entities,
        &remap.anonymous,
        &remap.literals,
        &remap.annotations,
        &remap.property_expressions,
        &remap.facet_restrictions,
        &remap.data_ranges,
        &remap.class_expressions,
        &remap.axioms,
        &remap.swrl,
    ] {
        total = total
            .checked_add(vector_allocation_bytes::<DenseId>(mapping.capacity())?)
            .ok_or_else(|| NativeError::limit("native component remap size overflow"))?;
    }
    u64::try_from(total).map_err(|_| NativeError::limit("native component remap size exceeds u64"))
}

fn retained_storage_bytes(
    strings: &Vec<Vec<u8>>,
    bytes: &Vec<Vec<u8>>,
    integers: &Vec<Vec<u8>>,
    sequences: &Vec<FrozenComponentSequence>,
    component_tables: [&Vec<FrozenComponent>; 11],
    base: usize,
    work: &mut ComponentWork,
) -> NativeResult<u64> {
    let mut total = u64::try_from(base)
        .map_err(|_| NativeError::limit("native retained component bytes exceed u64"))?;
    let mut add = |value: usize| -> NativeResult<()> {
        let value = u64::try_from(value)
            .map_err(|_| NativeError::limit("native retained component bytes exceed u64"))?;
        total = total
            .checked_add(value)
            .ok_or_else(|| NativeError::limit("native retained component bytes overflow"))?;
        Ok(())
    };
    for table in [strings, bytes, integers] {
        add(vector_allocation_bytes::<Vec<u8>>(table.capacity())?)?;
        for value in table {
            work.consume(1)?;
            add(vector_allocation_bytes::<u8>(value.capacity())?)?;
        }
    }
    add(vector_allocation_bytes::<FrozenComponentSequence>(
        sequences.capacity(),
    )?)?;
    for sequence in sequences {
        work.consume(1)?;
        add(vector_allocation_bytes::<ComponentValue>(
            sequence.elements.capacity(),
        )?)?;
    }
    for table in component_tables {
        add(vector_allocation_bytes::<FrozenComponent>(
            table.capacity(),
        )?)?;
        for component in table {
            work.consume(1)?;
            add(vector_allocation_bytes::<ComponentValue>(
                component.fields.capacity(),
            )?)?;
        }
    }
    Ok(total)
}

fn vector_allocation_bytes<T>(capacity: usize) -> NativeResult<usize> {
    if capacity == 0 {
        return Ok(0);
    }
    capacity
        .checked_mul(size_of::<T>())
        .and_then(|value| value.checked_add(HEAP_ALLOCATION_OVERHEAD))
        .ok_or_else(|| NativeError::limit("native retained vector size overflow"))
}

fn check_count(current: usize, maximum: u64, message: &'static str) -> NativeResult<()> {
    let following = u64::try_from(current)
        .map_err(|_| NativeError::limit(message))?
        .checked_add(1)
        .ok_or_else(|| NativeError::limit(message))?;
    if following > maximum || current >= u32::MAX as usize {
        return Err(NativeError::limit(message));
    }
    Ok(())
}

fn check_dense_count(current: usize, message: &'static str) -> NativeResult<()> {
    if current >= u32::MAX as usize {
        return Err(NativeError::limit(message));
    }
    Ok(())
}

fn table_slot_charge<T>() -> NativeResult<usize> {
    size_of::<T>()
        .checked_mul(2)
        .and_then(|value| value.checked_add(HEAP_ALLOCATION_OVERHEAD))
        .ok_or_else(|| NativeError::limit("native table growth accounting overflow"))
}

fn bucket_charge<I>(new_bucket: bool) -> NativeResult<usize> {
    let identifier_slots = size_of::<I>()
        .checked_mul(2)
        .ok_or_else(|| NativeError::limit("native bucket growth accounting overflow"))?;
    if new_bucket {
        identifier_slots
            .checked_add(HASH_BUCKET_BYTES)
            .and_then(|value| value.checked_add(HEAP_ALLOCATION_OVERHEAD))
            .ok_or_else(|| NativeError::limit("native bucket growth accounting overflow"))
    } else {
        Ok(identifier_slots)
    }
}

fn bucketed_table_charge<T, I>(new_bucket: bool) -> NativeResult<usize> {
    table_slot_charge::<T>()?
        .checked_add(bucket_charge::<I>(new_bucket)?)
        .ok_or_else(|| NativeError::limit("native table growth accounting overflow"))
}

fn temporary_vector_bytes(payload: usize, allocated: bool) -> NativeResult<usize> {
    payload
        .checked_add(if allocated {
            HEAP_ALLOCATION_OVERHEAD
        } else {
            0
        })
        .ok_or_else(|| NativeError::limit("native temporary vector accounting overflow"))
}

fn scalar_retained_charge<T, I>(payload: usize, new_bucket: bool) -> NativeResult<usize> {
    payload
        .checked_add(HEAP_ALLOCATION_OVERHEAD)
        .and_then(|value| value.checked_add(table_slot_charge::<T>().ok()?))
        .and_then(|value| value.checked_add(bucket_charge::<I>(new_bucket).ok()?))
        .ok_or_else(|| NativeError::limit("native scalar growth accounting overflow"))
}

fn prepare_bucket<I: Copy>(
    buckets: &mut HashMap<u64, Vec<I>>,
    bucket: u64,
    label: &'static str,
) -> NativeResult<()> {
    if let Some(identifiers) = buckets.get_mut(&bucket) {
        return identifiers
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit(label));
    }
    buckets
        .try_reserve(1)
        .map_err(|_| NativeError::limit(label))?;
    let mut identifiers = Vec::new();
    identifiers
        .try_reserve_exact(1)
        .map_err(|_| NativeError::limit(label))?;
    buckets.insert(bucket, identifiers);
    Ok(())
}

fn metered_scalar_bucket(domain: u64, value: &[u8], work: &mut ComponentWork) -> NativeResult<u64> {
    let mut hash = domain;
    for byte in value {
        work.consume(1)?;
        hash = fnv_step(hash, *byte);
    }
    Ok(hash)
}

fn component_bucket(
    tag: u16,
    values: &[ComponentValue],
    work: &mut ComponentWork,
) -> NativeResult<u64> {
    let mut hash = metered_scalar_bucket(0x434f_4d50_4f4e, &tag.to_le_bytes(), work)?;
    for value in values {
        work.consume(1)?;
        hash = hash_component_value(hash, *value);
    }
    Ok(hash)
}

fn component_sequence_bucket(
    kind: ComponentSequenceKind,
    values: &[ComponentValue],
    work: &mut ComponentWork,
) -> NativeResult<u64> {
    let mut hash = match kind {
        ComponentSequenceKind::CanonicalSet => 0x5345_545f_434f_4d50_u64,
        ComponentSequenceKind::Ordered => 0x4f52_445f_434f_4d50_u64,
    };
    for value in values {
        work.consume(1)?;
        let (tag, raw) = match value {
            ComponentValue::None => (0_u8, 0_u32),
            ComponentValue::String(ScalarKind::Text, id) => (2, id.0.raw()),
            ComponentValue::Bytes(id) => (3, id.0.raw()),
            ComponentValue::Integer(id) => (4, id.0.raw()),
            ComponentValue::String(ScalarKind::Enum, id) => (5, id.0.raw()),
            ComponentValue::Node(id) => component_bucket_value(*id),
            ComponentValue::Sequence(id) => (7, id.0.raw()),
        };
        hash = fnv_step(hash, tag);
        for byte in raw.to_le_bytes() {
            hash = fnv_step(hash, byte);
        }
    }
    Ok(hash)
}

fn hash_component_value(mut hash: u64, value: ComponentValue) -> u64 {
    let (tag, raw) = match value {
        ComponentValue::None => (0_u8, 0_u32),
        ComponentValue::String(ScalarKind::Text, id) => (2, id.0.raw()),
        ComponentValue::Bytes(id) => (3, id.0.raw()),
        ComponentValue::Integer(id) => (4, id.0.raw()),
        ComponentValue::String(ScalarKind::Enum, id) => (5, id.0.raw()),
        ComponentValue::Node(id) => component_bucket_value(id),
        ComponentValue::Sequence(id) => (7, id.0.raw()),
    };
    hash = fnv_step(hash, tag);
    for byte in raw.to_le_bytes() {
        hash = fnv_step(hash, byte);
    }
    hash
}

const fn fnv_step(hash: u64, byte: u8) -> u64 {
    (hash ^ byte as u64).wrapping_mul(0x0000_0100_0000_01b3)
}

fn component_bucket_value(value: LocalComponentId) -> (u8, u32) {
    match value {
        LocalComponentId::Iri(id) => (10, id.raw()),
        LocalComponentId::Entity(id) => (11, id.raw()),
        LocalComponentId::Anonymous(id) => (12, id.raw()),
        LocalComponentId::Literal(id) => (13, id.raw()),
        LocalComponentId::Annotation(id) => (14, id.raw()),
        LocalComponentId::PropertyExpression(id) => (15, id.raw()),
        LocalComponentId::FacetRestriction(id) => (16, id.raw()),
        LocalComponentId::DataRange(id) => (17, id.raw()),
        LocalComponentId::ClassExpression(id) => (18, id.raw()),
        LocalComponentId::Axiom(id) => (19, id.raw()),
        LocalComponentId::Swrl(id) => (20, id.raw()),
    }
}

fn bump(value: &mut u64, message: &'static str) -> NativeResult<()> {
    *value = value
        .checked_add(1)
        .ok_or_else(|| NativeError::limit(message))?;
    Ok(())
}

fn checked_add(left: usize, right: usize) -> NativeResult<usize> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("native component allocation size overflow"))
}

fn checked_add_u64(left: u64, right: u64) -> NativeResult<u64> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("native component encoding size overflow"))
}

const fn varint_len(mut value: u64) -> u64 {
    let mut length = 1_u64;
    while value >= 0x80 {
        value >>= 7;
        length += 1;
    }
    length
}

fn decode_frame(data: &[u8], offset: usize, end: usize) -> NativeResult<(usize, usize, usize)> {
    let (length, start) = decode_u64_varint(data, offset, end)?;
    let length = usize::try_from(length)
        .map_err(|_| NativeError::corrupt("canonical frame length exceeds address space"))?;
    let frame_end = start
        .checked_add(length)
        .ok_or_else(|| NativeError::corrupt("canonical frame length overflow"))?;
    if frame_end > end || frame_end > data.len() {
        return Err(NativeError::corrupt("truncated canonical framed component"));
    }
    Ok((start, frame_end, frame_end))
}

fn decode_u64_varint(data: &[u8], offset: usize, end: usize) -> NativeResult<(u64, usize)> {
    let start = offset;
    let mut cursor = offset;
    let mut value = 0_u64;
    let mut shift = 0_u32;
    while cursor < end {
        let byte = data[cursor];
        cursor += 1;
        let payload = byte & 0x7f;
        if cursor - start > 10 || (shift == 63 && payload > 1) {
            return Err(NativeError::corrupt("canonical count varint is too large"));
        }
        value |= u64::from(payload) << shift;
        if byte & 0x80 == 0 {
            if cursor - start > 1 && byte == 0 {
                return Err(NativeError::corrupt("canonical varint is nonminimal"));
            }
            return Ok((value, cursor));
        }
        shift += 7;
    }
    Err(NativeError::corrupt("truncated canonical varint"))
}

fn scan_integer_varint(data: &[u8], offset: usize, end: usize) -> NativeResult<usize> {
    let start = offset;
    let mut cursor = offset;
    while cursor < end {
        let byte = data[cursor];
        cursor += 1;
        if byte & 0x80 == 0 {
            if cursor - start > 1 && byte == 0 {
                return Err(NativeError::corrupt("canonical integer is nonminimal"));
            }
            return Ok(cursor);
        }
        if cursor - start >= 142_858 {
            return Err(NativeError::corrupt(
                "canonical integer is unreasonably long",
            ));
        }
    }
    Err(NativeError::corrupt("truncated canonical integer"))
}

fn encode_varint(mut value: u64, output: &mut Vec<u8>) -> NativeResult<()> {
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        push_byte(output, byte | if value == 0 { 0 } else { 0x80 })?;
        if value == 0 {
            return Ok(());
        }
    }
}

fn encode_frame(value: &[u8], output: &mut Vec<u8>) -> NativeResult<()> {
    let length = u64::try_from(value.len())
        .map_err(|_| NativeError::limit("native component frame length exceeds u64"))?;
    encode_varint(length, output)?;
    append(output, value)
}

fn push_byte(output: &mut Vec<u8>, value: u8) -> NativeResult<()> {
    output
        .try_reserve_exact(1)
        .map_err(|_| NativeError::limit("native component encoding allocation failed"))?;
    output.push(value);
    Ok(())
}

fn append(output: &mut Vec<u8>, value: &[u8]) -> NativeResult<()> {
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native component encoding allocation failed"))?;
    output.extend_from_slice(value);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn component_builder(limits: &Limits) -> NativeComponentBuilder {
        NativeComponentBuilder::new(limits).expect("component builder")
    }

    fn frame(value: &[u8]) -> Vec<u8> {
        let mut result = Vec::new();
        encode_varint(value.len() as u64, &mut result).expect("frame length");
        result.extend_from_slice(value);
        result
    }

    fn iri(value: &str) -> Vec<u8> {
        let mut result = vec![1, 2];
        result.extend(frame(value.as_bytes()));
        result
    }

    fn entity(kind: &str, value: &str) -> Vec<u8> {
        let iri = iri(value);
        let mut result = vec![2, 5];
        result.extend(frame(kind.as_bytes()));
        result.push(1);
        result.extend(frame(&iri));
        result
    }

    fn declaration(value: &str) -> Vec<u8> {
        let entity = entity("class", value);
        let mut result = vec![60, 1];
        result.extend(frame(&entity));
        result.extend([6, 0]);
        result
    }

    fn annotation(value: &str) -> Vec<u8> {
        let property = entity("annotation_property", "urn:property");
        let value = iri(value);
        let mut result = vec![5, 1];
        result.extend(frame(&property));
        result.push(1);
        result.extend(frame(&value));
        result.extend([6, 0]);
        result
    }

    const fn constant_bucket(_: u64) -> u64 {
        0
    }

    #[test]
    fn recursively_interns_nested_values_and_reencodes_exact_bytes() {
        let limits = Limits::default();
        let first = declaration("urn:shared");
        let second = declaration("urn:shared");
        let mut builder = component_builder(&limits);
        let first_id = builder.intern_canonical(&first).expect("first declaration");
        let second_id = builder
            .intern_canonical(&second)
            .expect("second declaration");
        assert_eq!(first_id, second_id);
        let mut frozen = builder.freeze().expect("component arena");
        let first_id = frozen.resolve(first_id).expect("frozen root id");
        assert_eq!(frozen.encode(first_id).expect("encoded"), first);
        assert_eq!(frozen.arena().counters().unique_nodes, 3);
        assert!(frozen.arena().counters().node_hits >= 3);
        assert_eq!(frozen.arena().counters().unique_strings, 2);
    }

    #[test]
    fn categories_cover_the_complete_model_tag_ledger() {
        let tags = [
            1_u16, 2, 3, 4, 5, 10, 11, 20, 21, 22, 23, 24, 25, 30, 31, 32, 33, 34, 35, 36, 37, 38,
            39, 40, 41, 42, 43, 44, 45, 46, 60, 61, 62, 63, 64, 70, 71, 72, 73, 74, 75, 76, 77, 78,
            79, 80, 81, 82, 90, 91, 92, 93, 94, 95, 100, 101, 110, 111, 112, 113, 114, 115, 116,
            120, 121, 122, 123, 140, 141, 142, 143, 144, 145, 146, 147, 148,
        ];
        assert_eq!(tags.len(), 76);
        for tag in tags {
            assert!(
                canonical_field_count(tag).is_some(),
                "field ledger missing tag {tag}"
            );
            assert!(
                tag_category(tag).is_some(),
                "category ledger missing tag {tag}"
            );
        }
    }

    #[test]
    fn clone_shares_one_immutable_component_owner() {
        let limits = Limits::default();
        let mut builder = component_builder(&limits);
        let identifier = builder
            .intern_canonical(&iri("urn:component"))
            .expect("IRI");
        let frozen = builder.freeze().expect("arena");
        let identifier = frozen.resolve(identifier).expect("frozen root id");
        let arena = frozen.into_arena();
        let clone = arena.clone();
        assert!(arena.shares_storage_with(&clone));
        assert_eq!(
            clone
                .encode(
                    identifier,
                    &limits,
                    Cancellation::with_duration(None),
                    None,
                    0,
                )
                .expect("encoded"),
            iri("urn:component")
        );
    }

    #[test]
    fn digest_bucket_collisions_use_full_structural_equality() {
        let limits = Limits::default();
        let first = declaration("urn:first");
        let second = declaration("urn:second");
        let mut builder = NativeComponentBuilder::with_bucket_transform(&limits, constant_bucket)
            .expect("collision builder");
        let first_pending = builder.intern_canonical(&first).expect("first");
        let second_pending = builder.intern_canonical(&second).expect("second");
        assert_ne!(first_pending, second_pending);
        assert_eq!(
            builder.intern_canonical(&first).expect("first repeat"),
            first_pending
        );
        let mut frozen = builder.freeze().expect("collision-safe freeze");
        let first_id = frozen.resolve(first_pending).expect("first id");
        let second_id = frozen.resolve(second_pending).expect("second id");
        assert_ne!(first_id, second_id);
        assert_eq!(frozen.encode(first_id).expect("first row"), first);
        assert_eq!(frozen.encode(second_id).expect("second row"), second);
    }

    #[test]
    fn freeze_is_deterministic_and_remaps_reverse_insertion_roots() {
        let first = declaration("urn:a");
        let second = declaration("urn:z");
        let limits = Limits::default();

        let mut forward = component_builder(&limits);
        let forward_first = forward.intern_canonical(&first).expect("forward first");
        let forward_second = forward.intern_canonical(&second).expect("forward second");
        let forward = forward.freeze().expect("forward freeze");

        let mut reverse = component_builder(&limits);
        let reverse_second = reverse.intern_canonical(&second).expect("reverse second");
        let reverse_first = reverse.intern_canonical(&first).expect("reverse first");
        let reverse = reverse.freeze().expect("reverse freeze");

        assert_eq!(forward.arena().tables(), reverse.arena().tables());
        assert_eq!(
            forward
                .resolve(forward_first)
                .expect("forward first id")
                .local,
            reverse
                .resolve(reverse_first)
                .expect("reverse first id")
                .local
        );
        assert_eq!(
            forward
                .resolve(forward_second)
                .expect("forward second id")
                .local,
            reverse
                .resolve(reverse_second)
                .expect("reverse second id")
                .local
        );
    }

    #[test]
    fn global_term_and_category_limits_span_repeated_roots() {
        let row = declaration("urn:bounded");
        let mut term_limits = Limits::default();
        term_limits.max_terms = 4;
        let mut builder = component_builder(&term_limits);
        let root = builder.intern_canonical(&row).expect("bounded row");
        assert_eq!(builder.intern_canonical(&row).expect("repeat"), root);
        assert_eq!(builder.counters.unique_nodes, 3);
        assert_eq!(builder.counters.unique_sequences, 1);
        assert_eq!(
            builder
                .intern_canonical(&declaration("urn:overflow"))
                .expect_err("global max_terms"),
            NativeError::limit("native component count exceeds max_terms")
        );
        assert!(builder.freeze().is_err(), "failed mutation poisons freeze");

        let mut axiom_limits = Limits::default();
        axiom_limits.max_axioms = 1;
        let mut builder = component_builder(&axiom_limits);
        builder.intern_canonical(&row).expect("first axiom");
        builder.intern_canonical(&row).expect("repeated axiom");
        assert!(builder
            .intern_canonical(&declaration("urn:second"))
            .is_err());

        let mut annotation_limits = Limits::default();
        annotation_limits.max_annotations = 1;
        let mut builder = component_builder(&annotation_limits);
        let first = annotation("urn:value-a");
        builder.intern_canonical(&first).expect("first annotation");
        builder
            .intern_canonical(&first)
            .expect("repeated annotation");
        assert!(builder
            .intern_canonical(&annotation("urn:value-b"))
            .is_err());
    }

    #[test]
    fn allocation_failure_poisoning_is_fail_closed() {
        let mut limits = Limits::default();
        limits.max_memory_bytes = Some(1);
        let mut builder = component_builder(&limits);
        assert!(builder.intern_canonical(&iri("urn:too-large")).is_err());
        let following = builder
            .intern_canonical(&iri("urn:still-poisoned"))
            .expect_err("poisoned builder rejects later roots");
        assert_eq!(following.code, "NATIVE_PROTOCOL");
        assert!(builder.freeze().is_err());
    }

    #[test]
    fn injected_payload_allocation_failure_poisoning_is_fail_closed() {
        let mut builder = component_builder(&Limits::default());
        // The IRI field vector, scalar table, and scalar interner allocations
        // succeed; the next checkpoint is the retained scalar payload.
        builder.work_mut().expect("work").fail_allocations_after(3);
        let failure = builder
            .intern_canonical(&iri("urn:injected"))
            .expect_err("injected allocation failure");
        assert_eq!(failure.code, "NATIVE_WIRE_LIMIT");
        assert!(failure.message.contains("injected"));
        assert_eq!(
            builder
                .intern_canonical(&iri("urn:poisoned"))
                .expect_err("poisoned")
                .code,
            "NATIVE_PROTOCOL"
        );
        assert!(builder.freeze().is_err());
    }

    #[test]
    fn injected_freeze_and_output_allocations_fail_deterministically() {
        let limits = Limits::default();
        let row = iri("urn:injected");

        let mut freeze_builder = component_builder(&limits);
        freeze_builder.intern_canonical(&row).expect("IRI");
        freeze_builder
            .work_mut()
            .expect("work")
            .fail_allocations_after(0);
        let failure = freeze_builder
            .freeze()
            .expect_err("injected freeze allocation failure");
        assert_eq!(failure.code, "NATIVE_WIRE_LIMIT");
        assert!(failure.message.contains("injected"));

        let mut output_builder = component_builder(&limits);
        let pending = output_builder.intern_canonical(&row).expect("IRI");
        let mut frozen = output_builder.freeze().expect("freeze");
        let identifier = frozen.resolve(pending).expect("id");
        frozen.work.fail_allocations_after(0);
        let failure = frozen
            .encode(identifier)
            .expect_err("injected output allocation failure");
        assert_eq!(failure.code, "NATIVE_WIRE_LIMIT");
        assert!(failure.message.contains("injected"));
    }

    #[test]
    fn pending_and_frozen_ids_are_bound_to_their_owner() {
        let limits = Limits::default();
        let mut first = component_builder(&limits);
        let first_pending = first.intern_canonical(&iri("urn:first")).expect("first");
        let first = first.freeze().expect("first freeze");
        let first_id = first.resolve(first_pending).expect("first id");

        let mut second = component_builder(&limits);
        let second_pending = second.intern_canonical(&iri("urn:second")).expect("second");
        let second = second.freeze().expect("second freeze");
        assert_eq!(
            second
                .resolve(first_pending)
                .expect_err("foreign pending id")
                .code,
            "NATIVE_PROTOCOL"
        );
        assert_eq!(
            second
                .arena()
                .encode(
                    first_id,
                    &limits,
                    Cancellation::with_duration(None),
                    None,
                    0,
                )
                .expect_err("foreign frozen id")
                .code,
            "NATIVE_PROTOCOL"
        );
        assert!(second.resolve(second_pending).is_ok());
    }

    #[test]
    fn collision_and_freeze_work_are_globally_bounded() {
        let limits = Limits::default();
        let mut collision = NativeComponentBuilder::with_bucket_transform(&limits, constant_bucket)
            .expect("collision builder");
        collision
            .intern_canonical(&declaration("urn:first"))
            .expect("first");
        let used = collision.work.as_ref().expect("work").used;
        collision.work.as_mut().expect("work").maximum = used + 1;
        let failure = collision
            .intern_canonical(&declaration("urn:second"))
            .expect_err("bounded collision scan");
        assert_eq!(failure.code, "NATIVE_WIRE_LIMIT");

        let mut freeze = component_builder(&limits);
        freeze
            .intern_canonical(&declaration("urn:a"))
            .expect("first");
        freeze
            .intern_canonical(&declaration("urn:z"))
            .expect("second");
        let used = freeze.work.as_ref().expect("work").used;
        freeze.work.as_mut().expect("work").maximum = used;
        assert_eq!(
            freeze.freeze().expect_err("bounded freeze").code,
            "NATIVE_WIRE_LIMIT"
        );
    }

    #[test]
    fn freeze_polls_deadline_and_encode_bounds_live_memory() {
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(Some(std::time::Duration::from_millis(20)));
        let mut cancelled =
            NativeComponentBuilder::with_control(&limits, cancellation.clone(), None, 0)
                .expect("builder");
        cancelled
            .intern_canonical(&declaration("urn:cancel"))
            .expect("row");
        std::thread::sleep(std::time::Duration::from_millis(30));
        assert_eq!(
            cancelled
                .freeze()
                .expect_err("expired freeze deadline")
                .code,
            "NATIVE_DEADLINE"
        );

        let row = declaration("urn:memory");
        let mut builder = component_builder(&limits);
        let pending = builder.intern_canonical(&row).expect("row");
        let mut frozen = builder.freeze().expect("freeze");
        let identifier = frozen.resolve(pending).expect("id");
        let retained = frozen.arena.tables.retained_bytes;
        let encoded = u64::try_from(row.len()).expect("row length");

        let mut operation_limits = limits;
        operation_limits.max_memory_bytes = Some(retained + encoded - 1);
        assert_eq!(
            frozen
                .arena()
                .encode(
                    identifier,
                    &operation_limits,
                    Cancellation::with_duration(None),
                    None,
                    0,
                )
                .expect_err("fresh operation limit includes retained arena")
                .code,
            "NATIVE_WIRE_LIMIT"
        );

        let mut depth_limits = limits;
        depth_limits.max_nesting_depth = 0;
        assert_eq!(
            frozen
                .arena()
                .encode(
                    identifier,
                    &depth_limits,
                    Cancellation::with_duration(None),
                    None,
                    0,
                )
                .expect_err("fresh operation depth is stricter than arena depth")
                .code,
            "NATIVE_WIRE_LIMIT"
        );

        let mut work_limits = limits;
        work_limits.max_canonical_work = 1;
        assert_eq!(
            frozen
                .arena()
                .encode(
                    identifier,
                    &work_limits,
                    Cancellation::with_duration(None),
                    None,
                    0,
                )
                .expect_err("fresh operation work limit is enforced")
                .code,
            "NATIVE_WIRE_LIMIT"
        );

        let expired = Cancellation::with_duration(Some(std::time::Duration::from_millis(20)));
        std::thread::sleep(std::time::Duration::from_millis(30));
        assert_eq!(
            frozen
                .arena()
                .encode(identifier, &limits, expired, None, 0)
                .expect_err("expired encode deadline")
                .code,
            "NATIVE_DEADLINE"
        );

        Arc::get_mut(&mut frozen.arena.tables)
            .expect("single owner")
            .max_memory_bytes = Some(retained + encoded - 1);
        assert_eq!(
            frozen
                .encode(identifier)
                .expect_err("live arena plus output exceeds memory")
                .code,
            "NATIVE_WIRE_LIMIT"
        );

        let mut workspace_builder = component_builder(&limits);
        workspace_builder
            .intern_canonical(&declaration("urn:workspace"))
            .expect("workspace row");
        let mut accounting_work =
            ComponentWork::new(&limits, Cancellation::with_duration(None), None, 0)
                .expect("accounting work");
        let retained = retained_builder_bytes(&workspace_builder, &mut accounting_work)
            .expect("retained bytes");
        let (workspace, _) = workspace_builder
            .freeze_workspace_bound(&mut accounting_work)
            .expect("workspace bound");
        let cap = retained
            .checked_add(u64::try_from(workspace).expect("workspace u64"))
            .and_then(|value| value.checked_sub(1))
            .expect("workspace cap");
        workspace_builder.limits.max_memory_bytes = Some(cap);
        workspace_builder.work_mut().expect("work").max_memory_bytes = Some(cap);
        assert_eq!(
            workspace_builder
                .freeze()
                .expect_err("freeze workspace exceeds memory cap")
                .code,
            "NATIVE_WIRE_LIMIT"
        );
    }

    #[test]
    fn freeze_transfers_owned_vectors_without_copying_payloads() {
        let mut builder = component_builder(&Limits::default());
        let pending = builder.intern_canonical(&iri("urn:owned")).expect("IRI");
        let fields_pointer = builder.iris[0].fields.as_ptr();
        let frozen = builder.freeze().expect("freeze");
        let identifier = frozen.resolve(pending).expect("frozen id");
        let frozen_pointer = frozen
            .arena()
            .tables()
            .component(identifier.local)
            .expect("component")
            .fields
            .as_ptr();
        assert_eq!(fields_pointer, frozen_pointer);
        assert!(frozen.arena().counters().retained_bytes > 0);
        assert!(
            frozen.arena().counters().peak_builder_bytes
                >= frozen.arena().counters().retained_bytes
        );
    }
}
