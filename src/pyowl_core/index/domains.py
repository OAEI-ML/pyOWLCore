"""Asserted object/data/annotation property domain and range postings."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document.composite import OntologyComposite
from pyowl_core.document.overlay import OntologyOverlay
from pyowl_core.document.provenance import OriginOccurrence
from pyowl_core.document.snapshot import AxiomScope, OntologyView, _is_ontology_view
from pyowl_core.model import (
    IRI,
    AnnotationProperty,
    Class,
    DataProperty,
    Datatype,
    ObjectInverseOf,
    ObjectProperty,
    StructuralNode,
    canonical_bytes,
)
from pyowl_core.model.axioms import (
    AnnotationPropertyDomain,
    AnnotationPropertyRange,
    AxiomNode,
    DataPropertyDomain,
    DataPropertyRange,
    ObjectPropertyDomain,
    ObjectPropertyRange,
)

from .cache import (
    IndexBuildBudget,
    ViewBuildReport,
    ViewBuildStrategy,
    build_report,
)
from .common import ScopedIndexOptions, bounded, canonical_merge, origins_for

DomainRangeProperty: TypeAlias = (
    ObjectProperty | ObjectInverseOf | DataProperty | AnnotationProperty
)
DomainRangeValue: TypeAlias = StructuralNode


class DomainRangeKind(str, Enum):
    DOMAIN = "domain"
    RANGE = "range"


@dataclass(frozen=True, slots=True)
class PropertyDomainRangeOptions(ScopedIndexOptions):
    pass


@dataclass(frozen=True, slots=True)
class DomainRangeRecord:
    property: DomainRangeProperty
    kind: DomainRangeKind
    value: DomainRangeValue
    axiom: AxiomNode
    origins: tuple[OriginOccurrence, ...]

    @builtins.property
    def is_named(self) -> bool:
        if isinstance(self.axiom, (AnnotationPropertyDomain, AnnotationPropertyRange)):
            return isinstance(self.value, IRI)
        return isinstance(self.value, (Class, Datatype))


@dataclass(frozen=True, slots=True)
class NamedDomainRangeResult:
    records: tuple[DomainRangeRecord, ...]
    filtered_complex_count: int


_DOMAIN_RANGE_TYPES = (
    ObjectPropertyDomain,
    ObjectPropertyRange,
    DataPropertyDomain,
    DataPropertyRange,
    AnnotationPropertyDomain,
    AnnotationPropertyRange,
)


class PropertyDomainRangeView:
    """Indexes every asserted value and makes named-only loss explicit."""

    SCHEMA_NAME = "pyowl-core/property-domain-range"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = PropertyDomainRangeOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: PropertyDomainRangeOptions,
        axioms: tuple[AxiomNode, ...],
        sources: tuple[PropertyDomainRangeView, ...],
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
    ) -> PropertyDomainRangeView:
        if not isinstance(options, PropertyDomainRangeOptions):
            raise TypeError("options must be PropertyDomainRangeOptions")
        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        view = ontology
        if isinstance(ontology, OntologyOverlay):
            source = ontology.base.view(
                cls,
                scope=options.scope,
                document_key=options.document_key,
                include_origins=options.include_origins,
                cancellation_token=cancellation_token,
            )
            budget.add_shared_rows(source.report.total_row_count)
            additions: tuple[AxiomNode, ...] = ()
            removals: frozenset[AxiomNode] = frozenset()
            if options.scope is AxiomScope.CLOSURE:
                additions = tuple(
                    value
                    for value in ontology.delta.add_axioms
                    if type(value) in _DOMAIN_RANGE_TYPES
                )
                removals = frozenset(
                    value
                    for value in ontology.delta.remove_axioms
                    if type(value) in _DOMAIN_RANGE_TYPES
                )
                for value in (*additions, *removals):
                    budget.add("delta_records", bytes_=64 + len(canonical_bytes(value)))
            return cls(
                view,
                options,
                (),
                (source,),
                (None,),
                tuple(sorted(additions, key=canonical_bytes)),
                removals,
                build_report(
                    cls,
                    ViewBuildStrategy.PATCHED,
                    budget,
                    started,
                    shared_bytes=source.report.own_bytes + source.report.shared_bytes,
                ),
            )
        if isinstance(ontology, OntologyComposite):
            if options.scope is not AxiomScope.CLOSURE:
                tuple(
                    ontology.iter_axioms(
                        scope=options.scope,
                        document_key=options.document_key,
                    )
                )
            sources: list[PropertyDomainRangeView] = []
            for member_view in ontology._sources:
                sources.append(
                    member_view.view(
                        cls,
                        include_origins=options.include_origins,
                        cancellation_token=cancellation_token,
                    )
                )
                budget.add_shared_rows(sources[-1].report.total_row_count)
                budget.add("member_adapters", rows=0, bytes_=128)
            additions = tuple(
                value for value in ontology.delta.add_axioms if type(value) in _DOMAIN_RANGE_TYPES
            )
            removals = frozenset(
                value
                for value in ontology.delta.remove_axioms
                if type(value) in _DOMAIN_RANGE_TYPES
            )
            for value in (*additions, *removals):
                budget.add("bridge_records", bytes_=64 + len(canonical_bytes(value)))
            shared = sum(item.report.own_bytes + item.report.shared_bytes for item in sources)
            return cls(
                view,
                options,
                (),
                tuple(sources),
                tuple(range(len(sources))),
                tuple(sorted(additions, key=canonical_bytes)),
                removals,
                build_report(
                    cls,
                    ViewBuildStrategy.MERGED,
                    budget,
                    started,
                    shared_bytes=shared,
                ),
            )
        values: list[AxiomNode] = []
        for selected_type in _DOMAIN_RANGE_TYPES:
            for axiom in view.iter_axioms(
                selected_type,
                scope=options.scope,
                document_key=options.document_key,
            ):
                values.append(axiom)
                budget.add("domain_range_records", bytes_=64 + len(canonical_bytes(axiom)))
        return cls(
            view,
            options,
            tuple(sorted(values, key=canonical_bytes)),
            (),
            (),
            (),
            frozenset(),
            build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
        )

    def iter(
        self,
        property: DomainRangeProperty,
        kind: DomainRangeKind | str | None = None,
        *,
        named_only: bool = False,
        limit: int | None = None,
    ) -> Iterator[DomainRangeRecord]:
        _validate_property(property)
        selected_kind = _kind(kind)
        if not isinstance(named_only, bool):
            raise TypeError("named_only must be bool")
        records = (
            record
            for axiom in self._iter_axioms()
            for record in (_record(self._ontology, self.options, axiom),)
            if record.property == property
            and (selected_kind is None or record.kind is selected_kind)
            and (not named_only or record.is_named)
        )
        yield from bounded(records, limit)

    def domains(
        self,
        property: DomainRangeProperty,
        *,
        named_only: bool = False,
        limit: int | None = None,
    ) -> Iterator[DomainRangeRecord]:
        yield from self.iter(
            property,
            DomainRangeKind.DOMAIN,
            named_only=named_only,
            limit=limit,
        )

    def ranges(
        self,
        property: DomainRangeProperty,
        *,
        named_only: bool = False,
        limit: int | None = None,
    ) -> Iterator[DomainRangeRecord]:
        yield from self.iter(
            property,
            DomainRangeKind.RANGE,
            named_only=named_only,
            limit=limit,
        )

    def named(
        self,
        property: DomainRangeProperty,
        kind: DomainRangeKind | str,
    ) -> NamedDomainRangeResult:
        all_records = tuple(self.iter(property, kind))
        retained = tuple(record for record in all_records if record.is_named)
        return NamedDomainRangeResult(retained, len(all_records) - len(retained))

    def properties(self, *, limit: int | None = None) -> Iterator[DomainRangeProperty]:
        values = {
            _record(self._ontology, self.options, axiom).property for axiom in self._iter_axioms()
        }
        yield from bounded(sorted(values, key=canonical_bytes), limit)

    def _iter_axioms(self) -> Iterator[AxiomNode]:
        iterables: list[Iterable[AxiomNode]] = [self._axioms]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            if member_index is None:
                iterables.append(source._iter_axioms())
            else:
                composite = cast(OntologyComposite, self._ontology)
                iterables.append(
                    sorted(
                        (
                            cast(AxiomNode, composite._scope_value(member_index, axiom))
                            for axiom in source._iter_axioms()
                        ),
                        key=canonical_bytes,
                    )
                )
        iterables.append(self._additions)
        yield from canonical_merge(
            iterables,
            key=canonical_bytes,
            excluded=lambda value: value in self._removals,
        )


def _record(
    ontology: OntologyView,
    options: PropertyDomainRangeOptions,
    axiom: AxiomNode,
) -> DomainRangeRecord:
    value: StructuralNode
    if isinstance(axiom, (ObjectPropertyDomain, DataPropertyDomain, AnnotationPropertyDomain)):
        kind = DomainRangeKind.DOMAIN
        value = axiom.domain
    elif isinstance(axiom, (ObjectPropertyRange, DataPropertyRange, AnnotationPropertyRange)):
        kind = DomainRangeKind.RANGE
        value = axiom.range
    else:
        raise TypeError("axiom is not a domain/range constructor")
    return DomainRangeRecord(
        axiom.property,
        kind,
        value,
        axiom,
        origins_for(ontology, axiom, include=options.include_origins),
    )


def _kind(value: DomainRangeKind | str | None) -> DomainRangeKind | None:
    if value is None or isinstance(value, DomainRangeKind):
        return value
    if isinstance(value, str):
        try:
            return DomainRangeKind(value)
        except ValueError as error:
            raise ValueError("kind must be domain or range") from error
    raise TypeError("kind must be DomainRangeKind, string, or None")


def _validate_property(value: object) -> None:
    if not isinstance(
        value,
        (ObjectProperty, ObjectInverseOf, DataProperty, AnnotationProperty),
    ):
        raise TypeError("property must be an object/data/annotation property expression")


__all__ = [
    "DomainRangeKind",
    "DomainRangeProperty",
    "DomainRangeRecord",
    "DomainRangeValue",
    "NamedDomainRangeResult",
    "PropertyDomainRangeOptions",
    "PropertyDomainRangeView",
]
