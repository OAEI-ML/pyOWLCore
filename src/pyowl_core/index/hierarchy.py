"""Strictly asserted class and property hierarchy views."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import Any, TypeAlias, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document.composite import OntologyComposite
from pyowl_core.document.overlay import OntologyOverlay
from pyowl_core.document.provenance import OriginOccurrence
from pyowl_core.document.snapshot import AxiomScope, OntologyView, _is_ontology_view
from pyowl_core.model import (
    Class,
    DataProperty,
    Entity,
    ObjectProperty,
    ObjectPropertyChain,
    ObjectPropertyExpression,
    canonical_bytes,
)
from pyowl_core.model.axioms import (
    AxiomNode,
    DisjointUnion,
    EquivalentClasses,
    EquivalentDataProperties,
    EquivalentObjectProperties,
    InverseObjectProperties,
    SubClassOf,
    SubDataPropertyOf,
    SubObjectPropertyOf,
)

from .cache import (
    IndexBuildBudget,
    ViewBuildReport,
    ViewBuildStrategy,
    build_report,
)
from .common import ScopedIndexOptions, bounded, canonical_merge, origins_for


class EquivalenceHandling(str, Enum):
    PRESERVE = "preserve"
    BIDIRECTIONAL = "bidirectional"
    COMPONENT = "component"


@dataclass(frozen=True, slots=True)
class ClassHierarchyOptions(ScopedIndexOptions):
    equivalence_handling: EquivalenceHandling = EquivalenceHandling.PRESERVE
    include_disjoint_union: bool = False

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        handling = self.equivalence_handling
        if isinstance(handling, str) and not isinstance(handling, EquivalenceHandling):
            try:
                handling = EquivalenceHandling(handling)
            except ValueError as error:
                raise ValueError("invalid equivalence_handling") from error
            object.__setattr__(self, "equivalence_handling", handling)
        elif not isinstance(handling, EquivalenceHandling):
            raise TypeError("equivalence_handling must be EquivalenceHandling")
        if not isinstance(self.include_disjoint_union, bool):
            raise TypeError("include_disjoint_union must be bool")


@dataclass(frozen=True, slots=True)
class ClassComponent:
    members: tuple[Class, ...]

    def __post_init__(self) -> None:
        members = tuple(sorted(set(self.members), key=canonical_bytes))
        if not members or not all(isinstance(item, Class) for item in members):
            raise ValueError("class component requires at least one Class")
        object.__setattr__(self, "members", members)


ClassHierarchyNode: TypeAlias = Class | ClassComponent


@dataclass(frozen=True, slots=True)
class ClassHierarchyEdge:
    child: ClassHierarchyNode
    parent: ClassHierarchyNode
    axiom: AxiomNode
    origins: tuple[OriginOccurrence, ...]


@dataclass(frozen=True, slots=True)
class ClassEquivalenceRecord:
    classes: tuple[Class, ...]
    axiom: EquivalentClasses
    origins: tuple[OriginOccurrence, ...]


_CLASS_AXIOM_TYPES = (SubClassOf, EquivalentClasses, DisjointUnion)


class AssertedClassHierarchyView:
    """Named asserted endpoints only; never computes reachability or directness."""

    SCHEMA_NAME = "pyowl-core/asserted-class-hierarchy"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = ClassHierarchyOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: ClassHierarchyOptions,
        axioms: tuple[AxiomNode, ...],
        sources: tuple[AssertedClassHierarchyView, ...],
        source_indexes: tuple[int | None, ...],
        additions: tuple[AxiomNode, ...],
        removals: frozenset[AxiomNode],
        report: ViewBuildReport,
    ) -> None:
        self._ontology = ontology
        self.options = options
        self._axioms = axioms
        self._sources = sources
        self._source_indexes = source_indexes
        self._additions = additions
        self._removals = removals
        own_values = (*axioms, *additions)
        child_records: dict[Class, list[tuple[Class, AxiomNode]]] = {}
        parent_records: dict[Class, list[tuple[Class, AxiomNode]]] = {}
        equivalences: dict[Class, list[EquivalentClasses]] = {}
        disjoint_union: dict[Class, list[tuple[Class, AxiomNode]]] = {}
        for axiom in own_values:
            if (
                isinstance(axiom, SubClassOf)
                and isinstance(axiom.sub_class, Class)
                and isinstance(axiom.super_class, Class)
            ):
                child_records.setdefault(axiom.sub_class, []).append((axiom.super_class, axiom))
                parent_records.setdefault(axiom.super_class, []).append((axiom.sub_class, axiom))
            elif isinstance(axiom, EquivalentClasses):
                for expression in axiom.expressions:
                    if isinstance(expression, Class):
                        equivalences.setdefault(expression, []).append(axiom)
            elif isinstance(axiom, DisjointUnion):
                for expression in axiom.expressions:
                    if isinstance(expression, Class):
                        disjoint_union.setdefault(expression, []).append(
                            (axiom.defined_class, axiom)
                        )
        self._child_records = _freeze_record_map(child_records)
        self._parent_records = _freeze_record_map(parent_records)
        self._equivalences = {
            key: tuple(sorted(values, key=canonical_bytes)) for key, values in equivalences.items()
        }
        self._disjoint_union_records = _freeze_record_map(disjoint_union)
        self.report = report

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> AssertedClassHierarchyView:
        return cast(
            AssertedClassHierarchyView,
            _build_hierarchy(
                cls,
                ontology,
                options,
                budget,
                cancellation_token,
                started,
                _CLASS_AXIOM_TYPES,
            ),
        )

    def iter_edges(self, *, limit: int | None = None) -> Iterator[ClassHierarchyEdge]:
        raw: list[tuple[Class, Class, AxiomNode]] = []
        for axiom in self._iter_axioms():
            if (
                isinstance(axiom, SubClassOf)
                and isinstance(axiom.sub_class, Class)
                and isinstance(axiom.super_class, Class)
            ):
                raw.append((axiom.sub_class, axiom.super_class, axiom))
            elif (
                isinstance(axiom, EquivalentClasses)
                and self.options.equivalence_handling is EquivalenceHandling.BIDIRECTIONAL
            ):
                named = tuple(value for value in axiom.expressions if isinstance(value, Class))
                for left, right in combinations(named, 2):
                    raw.extend(((left, right, axiom), (right, left, axiom)))
            elif isinstance(axiom, DisjointUnion) and self.options.include_disjoint_union:
                raw.extend(
                    (member, axiom.defined_class, axiom)
                    for member in axiom.expressions
                    if isinstance(member, Class)
                )
        components = self._component_map()
        edges: list[ClassHierarchyEdge] = []
        for child, parent, axiom in raw:
            selected_child: ClassHierarchyNode = components.get(child, child)
            selected_parent: ClassHierarchyNode = components.get(parent, parent)
            if selected_child == selected_parent:
                continue
            edges.append(
                ClassHierarchyEdge(
                    selected_child,
                    selected_parent,
                    axiom,
                    origins_for(
                        self._ontology,
                        axiom,
                        include=self.options.include_origins,
                    ),
                )
            )
        unique = {_class_edge_key(value): value for value in edges}
        yield from bounded((unique[key] for key in sorted(unique)), limit)

    def equivalence_sets(self, *, limit: int | None = None) -> Iterator[ClassEquivalenceRecord]:
        values: list[ClassEquivalenceRecord] = []
        for axiom in self._iter_axioms():
            if not isinstance(axiom, EquivalentClasses):
                continue
            classes = tuple(value for value in axiom.expressions if isinstance(value, Class))
            if len(classes) < 2:
                continue
            values.append(
                ClassEquivalenceRecord(
                    classes,
                    axiom,
                    origins_for(
                        self._ontology,
                        axiom,
                        include=self.options.include_origins,
                    ),
                )
            )
        yield from bounded(sorted(values, key=lambda value: canonical_bytes(value.axiom)), limit)

    def asserted_parents(self, value: ClassHierarchyNode) -> Iterator[ClassHierarchyNode]:
        _validate_class_node(value)
        if (
            isinstance(value, Class)
            and self.options.equivalence_handling is not EquivalenceHandling.COMPONENT
        ):
            selected: set[ClassHierarchyNode] = {
                parent for parent, _axiom in self._direct_parent_records(value)
            }
            if self.options.equivalence_handling is EquivalenceHandling.BIDIRECTIONAL:
                selected.update(self.equivalents(value))
            if self.options.include_disjoint_union:
                selected.update(
                    parent for parent, _axiom in self._disjoint_union_parent_records(value)
                )
        else:
            selected = {edge.parent for edge in self.iter_edges() if edge.child == value}
        yield from sorted(selected, key=_class_node_key)

    def asserted_children(self, value: ClassHierarchyNode) -> Iterator[ClassHierarchyNode]:
        _validate_class_node(value)
        if (
            isinstance(value, Class)
            and self.options.equivalence_handling is not EquivalenceHandling.COMPONENT
        ):
            selected: set[ClassHierarchyNode] = {
                child for child, _axiom in self._direct_child_records(value)
            }
            if self.options.equivalence_handling is EquivalenceHandling.BIDIRECTIONAL:
                selected.update(self.equivalents(value))
            if self.options.include_disjoint_union:
                selected.update(
                    child
                    for child in self._all_disjoint_union_children()
                    if any(
                        parent == value
                        for parent, _axiom in self._disjoint_union_parent_records(child)
                    )
                )
        else:
            selected = {edge.child for edge in self.iter_edges() if edge.parent == value}
        yield from sorted(selected, key=_class_node_key)

    def equivalents(self, value: Class) -> Iterator[Class]:
        if not isinstance(value, Class):
            raise TypeError("value must be Class")
        if self.options.equivalence_handling is EquivalenceHandling.COMPONENT:
            component = self._component_map().get(value)
            if component is not None:
                yield from (member for member in component.members if member != value)
            return
        selected: set[Class] = set()
        for axiom in self._equivalence_axioms(value):
            selected.update(item for item in axiom.expressions if isinstance(item, Class))
        selected.discard(value)
        yield from sorted(selected, key=canonical_bytes)

    def component(self, value: Class) -> ClassComponent:
        if not isinstance(value, Class):
            raise TypeError("value must be Class")
        return self._component_map().get(value, ClassComponent((value,)))

    @property
    def ignored_complex_endpoint_count(self) -> int:
        count = 0
        for axiom in self._iter_axioms():
            if isinstance(axiom, SubClassOf):
                count += int(not isinstance(axiom.sub_class, Class))
                count += int(not isinstance(axiom.super_class, Class))
            elif isinstance(axiom, (EquivalentClasses, DisjointUnion)):
                count += sum(not isinstance(value, Class) for value in axiom.expressions)
        return count

    def _component_map(self) -> dict[Class, ClassComponent]:
        if self.options.equivalence_handling is not EquivalenceHandling.COMPONENT:
            return {}
        groups = _components(record.classes for record in self.equivalence_sets())
        return {member: group for group in groups for member in group.members}

    def _iter_axioms(self) -> Iterator[AxiomNode]:
        yield from _iter_layered(self)

    def _direct_parent_records(self, value: Class) -> Iterator[tuple[Class, AxiomNode]]:
        iterables: list[Iterable[tuple[Class, AxiomNode]]] = [self._child_records.get(value, ())]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            records = source._direct_parent_records(value)
            iterables.append(self._transform_class_records(records, member_index))
        yield from canonical_merge(
            [sorted(records, key=_named_record_key) for records in iterables],
            key=_named_record_key,
            excluded=lambda record: record[1] in self._removals,
        )

    def _direct_child_records(self, value: Class) -> Iterator[tuple[Class, AxiomNode]]:
        iterables: list[Iterable[tuple[Class, AxiomNode]]] = [self._parent_records.get(value, ())]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            records = source._direct_child_records(value)
            iterables.append(self._transform_class_records(records, member_index))
        yield from canonical_merge(
            [sorted(records, key=_named_record_key) for records in iterables],
            key=_named_record_key,
            excluded=lambda record: record[1] in self._removals,
        )

    def _equivalence_axioms(self, value: Class) -> Iterator[EquivalentClasses]:
        iterables: list[Iterable[EquivalentClasses]] = [self._equivalences.get(value, ())]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            records = source._equivalence_axioms(value)
            if member_index is None:
                iterables.append(records)
            else:
                composite = cast(OntologyComposite, self._ontology)
                iterables.append(
                    cast(
                        Iterable[EquivalentClasses],
                        sorted(
                            (composite._scope_value(member_index, axiom) for axiom in records),
                            key=canonical_bytes,
                        ),
                    )
                )
        yield from canonical_merge(
            iterables,
            key=canonical_bytes,
            excluded=lambda axiom: axiom in self._removals,
        )

    def _disjoint_union_parent_records(self, value: Class) -> Iterator[tuple[Class, AxiomNode]]:
        iterables: list[Iterable[tuple[Class, AxiomNode]]] = [
            self._disjoint_union_records.get(value, ())
        ]
        for ordinal, source in enumerate(self._sources):
            iterables.append(
                self._transform_class_records(
                    source._disjoint_union_parent_records(value),
                    self._source_indexes[ordinal],
                )
            )
        yield from canonical_merge(
            [sorted(records, key=_named_record_key) for records in iterables],
            key=_named_record_key,
            excluded=lambda record: record[1] in self._removals,
        )

    def _all_disjoint_union_children(self) -> Iterator[Class]:
        iterables: list[Iterable[Class]] = [self._disjoint_union_records]
        iterables.extend(source._all_disjoint_union_children() for source in self._sources)
        yield from canonical_merge(
            [sorted(values, key=canonical_bytes) for values in iterables],
            key=canonical_bytes,
        )

    def _transform_class_records(
        self,
        records: Iterable[tuple[Class, AxiomNode]],
        member_index: int | None,
    ) -> Iterator[tuple[Class, AxiomNode]]:
        if member_index is None:
            yield from records
            return
        composite = cast(OntologyComposite, self._ontology)
        transformed = [
            (
                node,
                cast(AxiomNode, composite._scope_value(member_index, axiom)),
            )
            for node, axiom in records
        ]
        yield from sorted(transformed, key=_named_record_key)


@dataclass(frozen=True, slots=True)
class PropertyHierarchyOptions(ScopedIndexOptions):
    equivalence_handling: EquivalenceHandling = EquivalenceHandling.PRESERVE

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        handling = self.equivalence_handling
        if isinstance(handling, str) and not isinstance(handling, EquivalenceHandling):
            try:
                handling = EquivalenceHandling(handling)
            except ValueError as error:
                raise ValueError("invalid equivalence_handling") from error
            object.__setattr__(self, "equivalence_handling", handling)
        elif not isinstance(handling, EquivalenceHandling):
            raise TypeError("equivalence_handling must be EquivalenceHandling")


@dataclass(frozen=True, slots=True)
class PropertyComponent:
    members: tuple[Entity, ...]

    def __post_init__(self) -> None:
        members = tuple(sorted(set(self.members), key=canonical_bytes))
        if not members or not all(
            isinstance(item, (ObjectProperty, DataProperty)) for item in members
        ):
            raise ValueError("property component requires named object/data properties")
        object.__setattr__(self, "members", members)


PropertyHierarchyNode: TypeAlias = ObjectProperty | DataProperty | PropertyComponent


@dataclass(frozen=True, slots=True)
class PropertyHierarchyEdge:
    child: PropertyHierarchyNode
    parent: PropertyHierarchyNode
    axiom: AxiomNode
    origins: tuple[OriginOccurrence, ...]


@dataclass(frozen=True, slots=True)
class PropertyEquivalenceRecord:
    properties: tuple[ObjectProperty | DataProperty, ...]
    axiom: EquivalentObjectProperties | EquivalentDataProperties
    origins: tuple[OriginOccurrence, ...]


@dataclass(frozen=True, slots=True)
class InversePropertyRecord:
    first: ObjectPropertyExpression
    second: ObjectPropertyExpression
    axiom: InverseObjectProperties
    origins: tuple[OriginOccurrence, ...]


@dataclass(frozen=True, slots=True)
class PropertyChainRecord:
    chain: ObjectPropertyChain
    super_property: ObjectPropertyExpression
    axiom: SubObjectPropertyOf
    origins: tuple[OriginOccurrence, ...]


_PROPERTY_AXIOM_TYPES = (
    SubObjectPropertyOf,
    SubDataPropertyOf,
    EquivalentObjectProperties,
    EquivalentDataProperties,
    InverseObjectProperties,
)


class AssertedPropertyHierarchyView:
    """Named asserted edges plus separate equivalence/inverse/chain records."""

    SCHEMA_NAME = "pyowl-core/asserted-property-hierarchy"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = PropertyHierarchyOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: PropertyHierarchyOptions,
        axioms: tuple[AxiomNode, ...],
        sources: tuple[AssertedPropertyHierarchyView, ...],
        source_indexes: tuple[int | None, ...],
        additions: tuple[AxiomNode, ...],
        removals: frozenset[AxiomNode],
        report: ViewBuildReport,
    ) -> None:
        self._ontology = ontology
        self.options = options
        self._axioms = axioms
        self._sources = sources
        self._source_indexes = source_indexes
        self._additions = additions
        self._removals = removals
        self.report = report

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> AssertedPropertyHierarchyView:
        return cast(
            AssertedPropertyHierarchyView,
            _build_hierarchy(
                cls,
                ontology,
                options,
                budget,
                cancellation_token,
                started,
                _PROPERTY_AXIOM_TYPES,
            ),
        )

    def iter_edges(self, *, limit: int | None = None) -> Iterator[PropertyHierarchyEdge]:
        raw: list[
            tuple[ObjectProperty | DataProperty, ObjectProperty | DataProperty, AxiomNode]
        ] = []
        for axiom in self._iter_axioms():
            if isinstance(axiom, SubObjectPropertyOf):
                if isinstance(axiom.sub_property, ObjectProperty) and isinstance(
                    axiom.super_property, ObjectProperty
                ):
                    raw.append((axiom.sub_property, axiom.super_property, cast(AxiomNode, axiom)))
            elif isinstance(axiom, SubDataPropertyOf):
                raw.append((axiom.sub_property, axiom.super_property, cast(AxiomNode, axiom)))
            elif self.options.equivalence_handling is EquivalenceHandling.BIDIRECTIONAL:
                properties = _named_equivalent_properties(axiom)
                for left, right in combinations(properties, 2):
                    raw.extend(((left, right, axiom), (right, left, axiom)))
        components = self._component_map()
        values: list[PropertyHierarchyEdge] = []
        for child, parent, axiom in raw:
            selected_child: PropertyHierarchyNode = components.get(child, child)
            selected_parent: PropertyHierarchyNode = components.get(parent, parent)
            if selected_child == selected_parent:
                continue
            values.append(
                PropertyHierarchyEdge(
                    selected_child,
                    selected_parent,
                    axiom,
                    origins_for(
                        self._ontology,
                        axiom,
                        include=self.options.include_origins,
                    ),
                )
            )
        unique = {_property_edge_key(value): value for value in values}
        yield from bounded((unique[key] for key in sorted(unique)), limit)

    def equivalence_sets(self, *, limit: int | None = None) -> Iterator[PropertyEquivalenceRecord]:
        values: list[PropertyEquivalenceRecord] = []
        for axiom in self._iter_axioms():
            properties = _named_equivalent_properties(axiom)
            if len(properties) < 2 or not isinstance(
                axiom, (EquivalentObjectProperties, EquivalentDataProperties)
            ):
                continue
            values.append(
                PropertyEquivalenceRecord(
                    properties,
                    axiom,
                    origins_for(
                        self._ontology,
                        axiom,
                        include=self.options.include_origins,
                    ),
                )
            )
        yield from bounded(sorted(values, key=lambda value: canonical_bytes(value.axiom)), limit)

    def inverses(self, *, limit: int | None = None) -> Iterator[InversePropertyRecord]:
        values = (
            InversePropertyRecord(
                axiom.first,
                axiom.second,
                axiom,
                origins_for(
                    self._ontology,
                    axiom,
                    include=self.options.include_origins,
                ),
            )
            for axiom in self._iter_axioms()
            if isinstance(axiom, InverseObjectProperties)
        )
        yield from bounded(values, limit)

    def chains(self, *, limit: int | None = None) -> Iterator[PropertyChainRecord]:
        values = (
            PropertyChainRecord(
                axiom.sub_property,
                axiom.super_property,
                axiom,
                origins_for(
                    self._ontology,
                    axiom,
                    include=self.options.include_origins,
                ),
            )
            for axiom in self._iter_axioms()
            if isinstance(axiom, SubObjectPropertyOf)
            and isinstance(axiom.sub_property, ObjectPropertyChain)
        )
        yield from bounded(values, limit)

    def asserted_parents(self, value: PropertyHierarchyNode) -> Iterator[PropertyHierarchyNode]:
        _validate_property_node(value)
        selected = {edge.parent for edge in self.iter_edges() if edge.child == value}
        yield from sorted(selected, key=_property_node_key)

    def asserted_children(self, value: PropertyHierarchyNode) -> Iterator[PropertyHierarchyNode]:
        _validate_property_node(value)
        selected = {edge.child for edge in self.iter_edges() if edge.parent == value}
        yield from sorted(selected, key=_property_node_key)

    def equivalents(
        self, value: ObjectProperty | DataProperty
    ) -> Iterator[ObjectProperty | DataProperty]:
        if not isinstance(value, (ObjectProperty, DataProperty)):
            raise TypeError("value must be a named object/data property")
        if self.options.equivalence_handling is EquivalenceHandling.COMPONENT:
            component = self._component_map().get(value)
            if component is not None:
                yield from (
                    cast(ObjectProperty | DataProperty, member)
                    for member in component.members
                    if member != value
                )
            return
        selected: set[ObjectProperty | DataProperty] = set()
        for record in self.equivalence_sets():
            if value in record.properties:
                selected.update(record.properties)
        selected.discard(value)
        yield from sorted(selected, key=canonical_bytes)

    @property
    def non_named_endpoint_count(self) -> int:
        count = 0
        for axiom in self._iter_axioms():
            if isinstance(axiom, SubObjectPropertyOf):
                count += int(not isinstance(axiom.sub_property, ObjectProperty))
                count += int(not isinstance(axiom.super_property, ObjectProperty))
            elif isinstance(axiom, EquivalentObjectProperties):
                count += sum(not isinstance(value, ObjectProperty) for value in axiom.properties)
        return count

    def _component_map(self) -> dict[Entity, PropertyComponent]:
        if self.options.equivalence_handling is not EquivalenceHandling.COMPONENT:
            return {}
        groups = _property_components(record.properties for record in self.equivalence_sets())
        return {member: group for group in groups for member in group.members}

    def _iter_axioms(self) -> Iterator[AxiomNode]:
        yield from _iter_layered(self)


def _build_hierarchy(
    factory: Any,
    ontology: object,
    options: object,
    budget: IndexBuildBudget,
    cancellation_token: CancellationToken | None,
    started: float,
    selected_types: tuple[type[AxiomNode], ...],
) -> object:
    if not isinstance(options, (ClassHierarchyOptions, PropertyHierarchyOptions)):
        raise TypeError("invalid hierarchy options")
    if not _is_ontology_view(ontology):
        raise TypeError("ontology must implement OntologyView")
    view = ontology
    if isinstance(ontology, OntologyOverlay):
        source = ontology.base.view(
            factory,
            **_hierarchy_kwargs(options),
            cancellation_token=cancellation_token,
        )
        budget.add_shared_rows(source.report.total_row_count)
        additions: tuple[AxiomNode, ...] = ()
        removals: frozenset[AxiomNode] = frozenset()
        if options.scope is AxiomScope.CLOSURE:
            additions = tuple(
                value for value in ontology.delta.add_axioms if type(value) in selected_types
            )
            removals = frozenset(
                value for value in ontology.delta.remove_axioms if type(value) in selected_types
            )
            for value in (*additions, *removals):
                budget.add("delta_records", bytes_=64 + len(canonical_bytes(value)))
        return factory(
            view,
            options,
            (),
            (source,),
            (None,),
            tuple(sorted(additions, key=canonical_bytes)),
            removals,
            build_report(
                factory,
                ViewBuildStrategy.PATCHED,
                budget,
                started,
                shared_bytes=source.report.own_bytes + source.report.shared_bytes,
            ),
        )
    if isinstance(ontology, OntologyComposite):
        if options.scope is not AxiomScope.CLOSURE:
            tuple(ontology.iter_axioms(scope=options.scope, document_key=options.document_key))
        sources: list[Any] = []
        member_kwargs = _hierarchy_kwargs(options)
        member_kwargs.pop("scope", None)
        member_kwargs.pop("document_key", None)
        for source in ontology._sources:
            child = source.view(
                factory,
                **member_kwargs,
                cancellation_token=cancellation_token,
            )
            sources.append(child)
            budget.add_shared_rows(child.report.total_row_count)
            budget.add("member_adapters", rows=0, bytes_=128)
        additions = tuple(
            value for value in ontology.delta.add_axioms if type(value) in selected_types
        )
        removals = frozenset(
            value for value in ontology.delta.remove_axioms if type(value) in selected_types
        )
        for value in (*additions, *removals):
            budget.add("bridge_records", bytes_=64 + len(canonical_bytes(value)))
        shared = sum(value.report.own_bytes + value.report.shared_bytes for value in sources)
        return factory(
            view,
            options,
            (),
            tuple(sources),
            tuple(range(len(sources))),
            tuple(sorted(additions, key=canonical_bytes)),
            removals,
            build_report(
                factory,
                ViewBuildStrategy.MERGED,
                budget,
                started,
                shared_bytes=shared,
            ),
        )
    values: list[AxiomNode] = []
    for selected_type in selected_types:
        for value in view.iter_axioms(
            selected_type,
            scope=options.scope,
            document_key=options.document_key,
        ):
            axiom = value
            values.append(axiom)
            budget.add("asserted_records", bytes_=64 + len(canonical_bytes(axiom)))
    return factory(
        view,
        options,
        tuple(sorted(values, key=canonical_bytes)),
        (),
        (),
        (),
        frozenset(),
        build_report(factory, ViewBuildStrategy.FULL_BUILD, budget, started),
    )


def _hierarchy_kwargs(options: object) -> dict[str, object]:
    if isinstance(options, ClassHierarchyOptions):
        return {
            "scope": options.scope,
            "document_key": options.document_key,
            "include_origins": options.include_origins,
            "equivalence_handling": options.equivalence_handling,
            "include_disjoint_union": options.include_disjoint_union,
        }
    selected = cast(PropertyHierarchyOptions, options)
    return {
        "scope": selected.scope,
        "document_key": selected.document_key,
        "include_origins": selected.include_origins,
        "equivalence_handling": selected.equivalence_handling,
    }


def _iter_layered(value: Any) -> Iterator[AxiomNode]:
    axioms = cast(tuple[AxiomNode, ...], value._axioms)
    sources = cast(tuple[Any, ...], value._sources)
    source_indexes = cast(tuple[int | None, ...], value._source_indexes)
    additions = cast(tuple[AxiomNode, ...], value._additions)
    removals = cast(frozenset[AxiomNode], value._removals)
    ontology = cast(OntologyView, value._ontology)
    iterables: list[Iterable[AxiomNode]] = [axioms]
    for ordinal, source in enumerate(sources):
        source_values = cast(Iterator[AxiomNode], source._iter_axioms())
        member_index = source_indexes[ordinal]
        if member_index is None:
            iterables.append(source_values)
        else:
            composite = cast(OntologyComposite, ontology)
            iterables.append(
                sorted(
                    (
                        cast(AxiomNode, composite._scope_value(member_index, item))
                        for item in source_values
                    ),
                    key=canonical_bytes,
                )
            )
    iterables.append(additions)
    yield from canonical_merge(
        iterables,
        key=canonical_bytes,
        excluded=lambda item: item in removals,
    )


def _components(groups: Iterable[Iterable[Class]]) -> tuple[ClassComponent, ...]:
    parent: dict[Class, Class] = {}

    def find(value: Class) -> Class:
        retained = parent.setdefault(value, value)
        while retained != parent[retained]:
            retained = parent[retained]
        parent[value] = retained
        return retained

    for group in groups:
        values = tuple(group)
        if not values:
            continue
        anchor = find(values[0])
        for value in values[1:]:
            other = find(value)
            if anchor != other:
                parent[other] = anchor
    collected: dict[Class, list[Class]] = {}
    for parent_value in parent:
        collected.setdefault(find(parent_value), []).append(parent_value)
    return tuple(
        sorted(
            (ClassComponent(tuple(values)) for values in collected.values()),
            key=lambda value: _class_node_key(value),
        )
    )


def _property_components(
    groups: Iterable[Iterable[ObjectProperty | DataProperty]],
) -> tuple[PropertyComponent, ...]:
    parent: dict[Entity, Entity] = {}

    def find(value: Entity) -> Entity:
        retained = parent.setdefault(value, value)
        while retained != parent[retained]:
            retained = parent[retained]
        parent[value] = retained
        return retained

    for group in groups:
        values = tuple(group)
        if not values:
            continue
        anchor = find(values[0])
        for value in values[1:]:
            other = find(value)
            if anchor != other:
                parent[other] = anchor
    collected: dict[Entity, list[Entity]] = {}
    for parent_value in parent:
        collected.setdefault(find(parent_value), []).append(parent_value)
    return tuple(
        sorted(
            (PropertyComponent(tuple(values)) for values in collected.values()),
            key=_property_node_key,
        )
    )


def _named_equivalent_properties(
    axiom: AxiomNode,
) -> tuple[ObjectProperty | DataProperty, ...]:
    if isinstance(axiom, EquivalentObjectProperties):
        return tuple(value for value in axiom.properties if isinstance(value, ObjectProperty))
    if isinstance(axiom, EquivalentDataProperties):
        return tuple(axiom.properties)
    return ()


def _class_node_key(value: ClassHierarchyNode) -> bytes:
    if isinstance(value, Class):
        return b"0" + canonical_bytes(value)
    return b"1" + b"".join(canonical_bytes(item) for item in value.members)


def _property_node_key(value: PropertyHierarchyNode) -> bytes:
    if isinstance(value, (ObjectProperty, DataProperty)):
        return b"0" + canonical_bytes(value)
    return b"1" + b"".join(canonical_bytes(item) for item in value.members)


def _class_edge_key(value: ClassHierarchyEdge) -> bytes:
    return (
        _class_node_key(value.child)
        + b"\x00"
        + _class_node_key(value.parent)
        + b"\x00"
        + canonical_bytes(value.axiom)
    )


def _property_edge_key(value: PropertyHierarchyEdge) -> bytes:
    return (
        _property_node_key(value.child)
        + b"\x00"
        + _property_node_key(value.parent)
        + b"\x00"
        + canonical_bytes(value.axiom)
    )


def _freeze_record_map(
    values: dict[Class, list[tuple[Class, AxiomNode]]],
) -> dict[Class, tuple[tuple[Class, AxiomNode], ...]]:
    return {key: tuple(sorted(records, key=_named_record_key)) for key, records in values.items()}


def _named_record_key(value: tuple[Class, AxiomNode]) -> bytes:
    return canonical_bytes(value[0]) + b"\x00" + canonical_bytes(value[1])


def _validate_class_node(value: object) -> None:
    if not isinstance(value, (Class, ClassComponent)):
        raise TypeError("value must be Class or ClassComponent")


def _validate_property_node(value: object) -> None:
    if not isinstance(value, (ObjectProperty, DataProperty, PropertyComponent)):
        raise TypeError("value must be a named property or PropertyComponent")


__all__ = [
    "AssertedClassHierarchyView",
    "AssertedPropertyHierarchyView",
    "ClassComponent",
    "ClassEquivalenceRecord",
    "ClassHierarchyEdge",
    "ClassHierarchyNode",
    "ClassHierarchyOptions",
    "EquivalenceHandling",
    "InversePropertyRecord",
    "PropertyChainRecord",
    "PropertyComponent",
    "PropertyEquivalenceRecord",
    "PropertyHierarchyEdge",
    "PropertyHierarchyNode",
    "PropertyHierarchyOptions",
]
