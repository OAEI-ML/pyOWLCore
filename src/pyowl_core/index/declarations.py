"""Typed declaration postings and undeclared-reference reporting."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import cast

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.cancellation import CancellationToken
from pyowl_core.document.composite import OntologyComposite
from pyowl_core.document.overlay import OntologyOverlay
from pyowl_core.document.provenance import OriginOccurrence
from pyowl_core.document.snapshot import AxiomScope, OntologyView, _is_ontology_view
from pyowl_core.model import Entity, canonical_bytes
from pyowl_core.model.axioms import Declaration

from .cache import (
    IndexBuildBudget,
    ViewBuildReport,
    ViewBuildStrategy,
    build_report,
)
from .common import ScopedIndexOptions, bounded, canonical_merge, origins_for
from .signature import SignatureView


@dataclass(frozen=True, slots=True)
class DeclarationOptions(ScopedIndexOptions):
    include_builtins: bool = True
    include_annotation_only: bool = True

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        if not isinstance(self.include_builtins, bool):
            raise TypeError("include_builtins must be bool")
        if not isinstance(self.include_annotation_only, bool):
            raise TypeError("include_annotation_only must be bool")


@dataclass(frozen=True, slots=True)
class DeclarationPosting:
    declaration: Declaration
    origins: tuple[OriginOccurrence, ...]


class DeclarationIndex:
    SCHEMA_NAME = "pyowl-core/declaration-index"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = DeclarationOptions
    DEPENDENCIES = (SignatureView,)

    def __init__(
        self,
        ontology: OntologyView,
        options: DeclarationOptions,
        postings: FrozenMap[Entity, tuple[Declaration, ...]],
        sources: tuple[DeclarationIndex, ...],
        additions: FrozenMap[Entity, tuple[Declaration, ...]],
        removals: frozenset[Declaration],
        report: ViewBuildReport,
    ) -> None:
        self._ontology = ontology
        self.options = options
        self._postings = postings
        self._sources = sources
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
    ) -> DeclarationIndex:
        if not isinstance(options, DeclarationOptions):
            raise TypeError("options must be DeclarationOptions")
        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        view = ontology
        if isinstance(ontology, OntologyOverlay):
            source = ontology.base.view(
                cls,
                scope=options.scope,
                document_key=options.document_key,
                include_origins=options.include_origins,
                include_builtins=options.include_builtins,
                include_annotation_only=options.include_annotation_only,
                cancellation_token=cancellation_token,
            )
            budget.add_shared_rows(source.report.total_row_count)
            additions: dict[Entity, list[Declaration]] = {}
            removals: frozenset[Declaration] = frozenset()
            if options.scope is AxiomScope.CLOSURE:
                for value in ontology.delta.add_axioms:
                    if isinstance(value, Declaration):
                        additions.setdefault(value.entity, []).append(value)
                        budget.add("delta_declarations", bytes_=64 + len(canonical_bytes(value)))
                removals = frozenset(
                    value
                    for value in ontology.delta.remove_axioms
                    if isinstance(value, Declaration)
                )
                for value in removals:
                    budget.add("delta_tombstones", bytes_=64 + len(canonical_bytes(value)))
            return cls(
                view,
                options,
                FrozenMap(),
                (source,),
                _freeze(additions),
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
            sources: list[DeclarationIndex] = []
            for member_view in ontology._sources:
                sources.append(
                    member_view.view(
                        cls,
                        include_origins=options.include_origins,
                        include_builtins=options.include_builtins,
                        include_annotation_only=options.include_annotation_only,
                        cancellation_token=cancellation_token,
                    )
                )
                budget.add_shared_rows(sources[-1].report.total_row_count)
                budget.add("member_adapters", rows=0, bytes_=128)
            bridge_additions: dict[Entity, list[Declaration]] = {}
            for value in ontology.delta.add_axioms:
                if isinstance(value, Declaration):
                    bridge_additions.setdefault(value.entity, []).append(value)
                    budget.add("bridge_declarations", bytes_=64 + len(canonical_bytes(value)))
            removals = frozenset(
                value for value in ontology.delta.remove_axioms if isinstance(value, Declaration)
            )
            for value in removals:
                budget.add("bridge_tombstones", bytes_=64 + len(canonical_bytes(value)))
            shared = sum(item.report.own_bytes + item.report.shared_bytes for item in sources)
            return cls(
                view,
                options,
                FrozenMap(),
                tuple(sources),
                _freeze(bridge_additions),
                removals,
                build_report(
                    cls,
                    ViewBuildStrategy.MERGED,
                    budget,
                    started,
                    shared_bytes=shared,
                ),
            )
        postings: dict[Entity, list[Declaration]] = {}
        for value in view.iter_axioms(
            Declaration,
            scope=options.scope,
            document_key=options.document_key,
        ):
            declaration = cast(Declaration, value)
            postings.setdefault(declaration.entity, []).append(declaration)
            budget.add("declarations", bytes_=64 + len(canonical_bytes(declaration)))
        return cls(
            view,
            options,
            _freeze(postings),
            (),
            FrozenMap(),
            frozenset(),
            build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
        )

    def iter(self, entity: Entity, *, limit: int | None = None) -> Iterator[DeclarationPosting]:
        if not isinstance(entity, Entity):
            raise TypeError("entity must be Entity")
        iterables: list[Iterable[Declaration]] = [
            self._postings.get(entity, ()),
            *(source._iter_axioms(entity) for source in self._sources),
            self._additions.get(entity, ()),
        ]
        axioms = canonical_merge(
            iterables,
            key=canonical_bytes,
            excluded=lambda value: value in self._removals,
        )
        yield from bounded(
            (
                DeclarationPosting(
                    value,
                    origins_for(
                        self._ontology,
                        value,
                        include=self.options.include_origins,
                    ),
                )
                for value in axioms
            ),
            limit,
        )

    def declarations(self, entity: Entity, *, limit: int | None = None) -> Iterator[Declaration]:
        yield from (posting.declaration for posting in self.iter(entity, limit=limit))

    def is_declared(self, entity: Entity) -> bool:
        return any(self.declarations(entity, limit=1))

    def entities(self, *, limit: int | None = None) -> Iterator[Entity]:
        iterables: list[Iterable[Entity]] = [self._postings, self._additions]
        iterables.extend(source.entities() for source in self._sources)
        merged = canonical_merge(
            [sorted(values, key=canonical_bytes) for values in iterables],
            key=canonical_bytes,
        )
        yield from bounded((entity for entity in merged if self.is_declared(entity)), limit)

    def undeclared_entities(self, *, limit: int | None = None) -> Iterator[Entity]:
        signature = self._ontology.view(
            SignatureView,
            scope=self.options.scope,
            document_key=self.options.document_key,
            include_origins=False,
            include_builtins=self.options.include_builtins,
            include_annotation_only=self.options.include_annotation_only,
        )
        yield from bounded(
            (entity for entity in signature.iter() if not self.is_declared(entity)),
            limit,
        )

    def _iter_axioms(self, entity: Entity) -> Iterator[Declaration]:
        yield from (posting.declaration for posting in self.iter(entity))


def _freeze(
    values: dict[Entity, list[Declaration]],
) -> FrozenMap[Entity, tuple[Declaration, ...]]:
    return freeze_mapping(
        {
            entity: tuple(sorted(declarations, key=canonical_bytes))
            for entity, declarations in values.items()
        }
    )


__all__ = [
    "DeclarationIndex",
    "DeclarationOptions",
    "DeclarationPosting",
]
