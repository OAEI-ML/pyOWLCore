"""Exact-constructor and generated category postings for asserted axioms."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar, cast, get_args

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, LoadOptions
from pyowl_core.document.composite import OntologyComposite
from pyowl_core.document.overlay import OntologyOverlay
from pyowl_core.document.provenance import OriginOccurrence
from pyowl_core.document.snapshot import AxiomScope, OntologyView
from pyowl_core.model import canonical_bytes
from pyowl_core.model.axioms import (
    ANNOTATION_AXIOM_TYPES,
    AXIOM_TYPES,
    DECLARATION_AXIOM_TYPES,
    LOGICAL_AXIOM_TYPES,
    AnnotationAxiom,
    AxiomNode,
    DeclarationAxiom,
    LogicalAxiom,
)
from pyowl_core.model.registry import CONSTRUCTOR_SPECS

from .cache import (
    IndexBuildBudget,
    ViewBuildReport,
    ViewBuildStrategy,
    build_report,
)
from .common import (
    ScopedIndexOptions,
    bounded,
    canonical_merge,
    origins_for,
    validate_axiom_type,
)

A = TypeVar("A", bound=AxiomNode)
_NATIVE_AUTO_MIN_ROWS = 4_096


class AxiomCategory(str, Enum):
    DECLARATION = "declaration"
    LOGICAL = "logical"
    ANNOTATION = "annotation"


CATEGORY_TYPES: FrozenMap[AxiomCategory, tuple[type[AxiomNode], ...]] = FrozenMap(
    {
        AxiomCategory.DECLARATION: DECLARATION_AXIOM_TYPES,
        AxiomCategory.LOGICAL: LOGICAL_AXIOM_TYPES,
        AxiomCategory.ANNOTATION: ANNOTATION_AXIOM_TYPES,
    }
)

_REGISTRY_AXIOM_CATEGORIES = {
    spec.constructor: spec.category
    for spec in CONSTRUCTOR_SPECS
    if issubclass(spec.constructor, AxiomNode)
}
if set(_REGISTRY_AXIOM_CATEGORIES) != set(AXIOM_TYPES):
    raise RuntimeError("axiom category table does not cover every registered constructor")
if any(
    _REGISTRY_AXIOM_CATEGORIES[constructor]
    not in {"declaration_axiom", "logical_axiom", "annotation_axiom"}
    for constructor in AXIOM_TYPES
):
    raise RuntimeError("registered axiom has no supported generated category")


@dataclass(frozen=True, slots=True)
class AxiomTypeOptions(ScopedIndexOptions):
    pass


@dataclass(frozen=True, slots=True)
class AxiomPosting:
    axiom: AxiomNode
    origins: tuple[OriginOccurrence, ...]


class AxiomTypeIndex:
    """Immutable exact-tag postings; layered builds retain parent indexes."""

    SCHEMA_NAME = "pyowl-core/axiom-type-index"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = AxiomTypeOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: AxiomTypeOptions,
        postings: FrozenMap[type[AxiomNode], tuple[AxiomNode, ...]],
        sources: tuple[AxiomTypeIndex, ...],
        source_indexes: tuple[int | None, ...],
        additions: FrozenMap[type[AxiomNode], tuple[AxiomNode, ...]],
        removals: frozenset[AxiomNode],
        report: ViewBuildReport,
    ) -> None:
        self._ontology = ontology
        self.options = options
        self._postings = postings
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
    ) -> AxiomTypeIndex:
        if not isinstance(options, AxiomTypeOptions):
            raise TypeError("options must be AxiomTypeOptions")
        from pyowl_core.document.snapshot import _is_ontology_view

        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        if isinstance(ontology, OntologyOverlay):
            base_options = options
            source = ontology.base.view(
                cls,
                scope=base_options.scope,
                document_key=base_options.document_key,
                include_origins=base_options.include_origins,
                cancellation_token=cancellation_token,
            )
            budget.add_shared_rows(source.report.total_row_count)
            additions: dict[type[AxiomNode], list[AxiomNode]] = {}
            removals: frozenset[AxiomNode] = frozenset()
            if options.scope is AxiomScope.CLOSURE:
                for value in ontology.delta.add_axioms:
                    additions.setdefault(type(value), []).append(value)
                    budget.add("delta_postings", bytes_=64 + len(canonical_bytes(value)))
                removals = frozenset(ontology.delta.remove_axioms)
                for value in removals:
                    budget.add("delta_tombstones", bytes_=64 + len(canonical_bytes(value)))
            frozen_additions = freeze_mapping(
                {
                    key: tuple(sorted(values, key=canonical_bytes))
                    for key, values in additions.items()
                }
            )
            return cls(
                ontology,
                options,
                FrozenMap(),
                (source,),
                (None,),
                frozen_additions,
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
                # The composite itself enforces this too; fail before touching members.
                tuple(
                    ontology.iter_axioms(
                        scope=options.scope,
                        document_key=options.document_key,
                    )
                )
            sources: list[AxiomTypeIndex] = []
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
            bridge_additions: dict[type[AxiomNode], list[AxiomNode]] = {}
            for value in ontology.delta.add_axioms:
                bridge_additions.setdefault(type(value), []).append(value)
                budget.add("bridge_postings", bytes_=64 + len(canonical_bytes(value)))
            for value in ontology.delta.remove_axioms:
                budget.add("bridge_tombstones", bytes_=64 + len(canonical_bytes(value)))
            shared = sum(item.report.own_bytes + item.report.shared_bytes for item in sources)
            return cls(
                ontology,
                options,
                FrozenMap(),
                tuple(sources),
                tuple(range(len(sources))),
                freeze_mapping(
                    {
                        key: tuple(sorted(values, key=canonical_bytes))
                        for key, values in bridge_additions.items()
                    }
                ),
                frozenset(ontology.delta.remove_axioms),
                build_report(
                    cls,
                    ViewBuildStrategy.MERGED,
                    budget,
                    started,
                    shared_bytes=shared,
                ),
            )
        load_options = getattr(ontology, "load_options", None)
        use_native = False
        if isinstance(load_options, LoadOptions):
            preference = load_options.backend
            if preference is BackendPreference.NATIVE or (
                preference is BackendPreference.AUTO
                and options.scope is AxiomScope.CLOSURE
                and options.document_key is None
                and ontology.report.effective_axiom_count >= _NATIVE_AUTO_MIN_ROWS
            ):
                from pyowl_core.backends.dispatch import select_backend

                use_native = (
                    select_backend(
                        preference,
                        capability="index-axiom-types-v1",
                        operation="axiom-type index build",
                    ).backend
                    == "native"
                )
        values = tuple(
            ontology.iter_axioms(
                scope=options.scope,
                document_key=options.document_key,
            )
        )
        if use_native:
            from pyowl_core.backends.native import partition_axioms

            native = partition_axioms(
                values,
                limits=cast(LoadOptions, load_options).limits,
                cancellation_token=cancellation_token,
            )
            for size in native.canonical_sizes:
                budget.add("constructor_postings", bytes_=64 + size)
            frozen = freeze_mapping(native.postings)
        else:
            postings: dict[type[AxiomNode], list[AxiomNode]] = {}
            for axiom in values:
                postings.setdefault(type(axiom), []).append(axiom)
                budget.add("constructor_postings", bytes_=64 + len(canonical_bytes(axiom)))
            frozen = freeze_mapping(
                {key: tuple(sorted(items, key=canonical_bytes)) for key, items in postings.items()}
            )
        return cls(
            ontology,
            options,
            frozen,
            (),
            (),
            FrozenMap(),
            frozenset(),
            build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
        )

    def iter(self, axiom_type: type[A], *, limit: int | None = None) -> Iterator[A]:
        constructor = validate_axiom_type(axiom_type)
        iterables: list[Iterator[AxiomNode] | tuple[AxiomNode, ...]] = []
        local = self._postings.get(constructor, ())
        if local:
            iterables.append(local)
        for source_ordinal, source in enumerate(self._sources):
            source_values = cast(Iterator[AxiomNode], source.iter(constructor))
            member_index = self._source_indexes[source_ordinal]
            if member_index is None:
                iterables.append(source_values)
            else:
                iterables.append(self._transformed(source_values, member_index))
        additions = self._additions.get(constructor, ())
        if additions:
            iterables.append(additions)
        merged = canonical_merge(
            iterables,
            key=canonical_bytes,
            excluded=lambda value: value in self._removals,
        )
        yield from cast(Iterator[A], bounded(merged, limit))

    def iter_category(
        self,
        category: AxiomCategory | str | object,
        *,
        limit: int | None = None,
    ) -> Iterator[AxiomNode]:
        selected = _category(category)
        iterables = [self.iter(constructor) for constructor in CATEGORY_TYPES[selected]]
        yield from bounded(canonical_merge(iterables, key=canonical_bytes), limit)

    def iter_all(self, *, limit: int | None = None) -> Iterator[AxiomNode]:
        yield from bounded(
            canonical_merge(
                [self.iter(constructor) for constructor in AXIOM_TYPES],
                key=canonical_bytes,
            ),
            limit,
        )

    def count(self, axiom_type: type[A]) -> int:
        constructor = validate_axiom_type(axiom_type)
        if not self._sources and not self._additions and not self._removals:
            return len(self._postings.get(constructor, ()))
        return sum(1 for _ in self.iter(axiom_type))

    def count_category(self, category: AxiomCategory | str | object) -> int:
        return sum(1 for _ in self.iter_category(category))

    def tuple(self, axiom_type: type[A], *, limit: int | None = None) -> tuple[A, ...]:
        """Allocate a convenience tuple; scalar iteration remains the default."""

        return tuple(self.iter(axiom_type, limit=limit))

    def posting(self, axiom: AxiomNode) -> AxiomPosting | None:
        if not isinstance(axiom, AxiomNode):
            raise TypeError("axiom must be AxiomNode")
        if not any(value == axiom for value in self.iter(type(axiom))):
            return None
        return AxiomPosting(
            axiom,
            origins_for(self._ontology, axiom, include=self.options.include_origins),
        )

    def _transformed(
        self,
        values: Iterator[AxiomNode],
        member_index: int,
    ) -> Iterator[AxiomNode]:
        composite = cast(OntologyComposite, self._ontology)
        transformed = [
            cast(AxiomNode, composite._scope_value(member_index, value)) for value in values
        ]
        yield from sorted(transformed, key=canonical_bytes)


def _category(value: AxiomCategory | str | object) -> AxiomCategory:
    if isinstance(value, AxiomCategory):
        return value
    if isinstance(value, str):
        try:
            return AxiomCategory(value)
        except ValueError as error:
            raise ValueError("unknown axiom category") from error
    arguments = set(get_args(value))
    if value is DeclarationAxiom:
        return AxiomCategory.DECLARATION
    if arguments == set(get_args(LogicalAxiom)):
        return AxiomCategory.LOGICAL
    if arguments == set(get_args(AnnotationAxiom)):
        return AxiomCategory.ANNOTATION
    raise TypeError("category must be AxiomCategory or a closed axiom category union")


__all__ = [
    "CATEGORY_TYPES",
    "AxiomCategory",
    "AxiomPosting",
    "AxiomTypeIndex",
    "AxiomTypeOptions",
]
