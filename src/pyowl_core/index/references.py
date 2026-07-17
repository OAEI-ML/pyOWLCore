"""Exhaustive typed entity/IRI/anonymous structural reference postings."""

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
from pyowl_core.model import (
    IRI,
    AnonymousIndividual,
    Entity,
    StructuralNode,
    canonical_bytes,
)
from pyowl_core.model.axioms import ANNOTATION_AXIOM_TYPES, AxiomNode

from .cache import (
    IndexBuildBudget,
    ViewBuildReport,
    ViewBuildStrategy,
    build_report,
)
from .common import (
    ConstructorPath,
    ReferenceRole,
    ScopedIndexOptions,
    bounded,
    canonical_merge,
    iter_structural_occurrences,
    origins_for,
    prefix_composite_member_origins,
)

ReferenceKey = Entity | IRI | AnonymousIndividual


@dataclass(frozen=True, slots=True)
class EntityReferenceOptions(ScopedIndexOptions):
    include_annotations: bool = True
    include_source_provenance: bool = True

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        if not isinstance(self.include_annotations, bool):
            raise TypeError("include_annotations must be bool")
        if not isinstance(self.include_source_provenance, bool):
            raise TypeError("include_source_provenance must be bool")


@dataclass(frozen=True, slots=True)
class ReferenceOccurrence:
    key: ReferenceKey
    axiom: AxiomNode | None
    container: StructuralNode
    origins: tuple[OriginOccurrence, ...]
    constructor_path: ConstructorPath
    role: ReferenceRole
    polarity_hint: None = None


class EntityReferenceIndex:
    """Immutable schema-walker postings with delta/member adapters."""

    SCHEMA_NAME = "pyowl-core/entity-reference-index"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = EntityReferenceOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: EntityReferenceOptions,
        postings: FrozenMap[ReferenceKey, tuple[ReferenceOccurrence, ...]],
        sources: tuple[EntityReferenceIndex, ...],
        source_indexes: tuple[int | None, ...],
        additions: FrozenMap[ReferenceKey, tuple[ReferenceOccurrence, ...]],
        removals: frozenset[StructuralNode],
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
    ) -> EntityReferenceIndex:
        if not isinstance(options, EntityReferenceOptions):
            raise TypeError("options must be EntityReferenceOptions")
        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        view = ontology
        if isinstance(ontology, OntologyOverlay):
            source = ontology.base.view(
                cls,
                scope=options.scope,
                document_key=options.document_key,
                include_origins=options.include_origins,
                include_annotations=options.include_annotations,
                include_source_provenance=options.include_source_provenance,
                cancellation_token=cancellation_token,
            )
            budget.add_shared_rows(source.report.total_row_count)
            additions: FrozenMap[ReferenceKey, tuple[ReferenceOccurrence, ...]] = FrozenMap()
            removals: frozenset[StructuralNode] = frozenset()
            if options.scope is AxiomScope.CLOSURE:
                roots: tuple[StructuralNode, ...] = (
                    *ontology.delta.add_ontology_annotations,
                    *ontology.delta.add_axioms,
                )
                additions = _scan(view, options, roots, budget, "delta_postings")
                removals = frozenset(
                    (
                        *ontology.delta.remove_axioms,
                        *ontology.delta.remove_ontology_annotations,
                    )
                )
                for value in removals:
                    budget.add("delta_tombstones", bytes_=64 + len(canonical_bytes(value)))
            return cls(
                view,
                options,
                FrozenMap(),
                (source,),
                (None,),
                additions,
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
            sources: list[EntityReferenceIndex] = []
            for member_view in ontology._sources:
                sources.append(
                    member_view.view(
                        cls,
                        include_origins=options.include_origins,
                        include_annotations=options.include_annotations,
                        include_source_provenance=options.include_source_provenance,
                        cancellation_token=cancellation_token,
                    )
                )
                budget.add_shared_rows(sources[-1].report.total_row_count)
                budget.add("member_adapters", rows=0, bytes_=128)
            additions = _scan(
                view,
                options,
                (*ontology.delta.add_ontology_annotations, *ontology.delta.add_axioms),
                budget,
                "bridge_postings",
            )
            removals = frozenset(
                (*ontology.delta.remove_axioms, *ontology.delta.remove_ontology_annotations)
            )
            for value in removals:
                budget.add("bridge_tombstones", bytes_=64 + len(canonical_bytes(value)))
            shared = sum(item.report.own_bytes + item.report.shared_bytes for item in sources)
            return cls(
                view,
                options,
                FrozenMap(),
                tuple(sources),
                tuple(range(len(sources))),
                additions,
                removals,
                build_report(
                    cls,
                    ViewBuildStrategy.MERGED,
                    budget,
                    started,
                    shared_bytes=shared,
                ),
            )
        full_roots: list[StructuralNode] = []
        if options.include_annotations:
            full_roots.extend(
                view.ontology_annotations(
                    scope=options.scope,
                    document_key=options.document_key,
                )
            )
        full_roots.extend(
            cast(
                Iterable[StructuralNode],
                view.iter_axioms(scope=options.scope, document_key=options.document_key),
            )
        )
        full_roots.extend(
            view.iter_extensions(scope=options.scope, document_key=options.document_key)
        )
        postings = _scan(view, options, full_roots, budget, "reference_postings")
        return cls(
            view,
            options,
            postings,
            (),
            (),
            FrozenMap(),
            frozenset(),
            build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
        )

    def iter(
        self,
        key: ReferenceKey,
        *,
        role: ReferenceRole | None = None,
        limit: int | None = None,
    ) -> Iterator[ReferenceOccurrence]:
        _validate_key(key)
        if role is not None and not isinstance(role, ReferenceRole):
            raise TypeError("role must be ReferenceRole or None")
        iterables: list[Iterable[ReferenceOccurrence]] = []
        local = self._postings.get(key, ())
        if local:
            iterables.append(local)
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            if member_index is None:
                iterables.append(source.iter(key))
                continue
            source_keys: Iterable[ReferenceKey]
            if isinstance(key, AnonymousIndividual):
                source_keys = (
                    candidate
                    for candidate in source
                    if isinstance(candidate, AnonymousIndividual)
                    and self._transform_key(candidate, member_index) == key
                )
            else:
                source_keys = (key,)
            for source_key in source_keys:
                iterables.append(self._transform_occurrences(source.iter(source_key), member_index))
        additions = self._additions.get(key, ())
        if additions:
            iterables.append(additions)
        merged = canonical_merge(
            [sorted(values, key=_occurrence_key) for values in iterables],
            key=_occurrence_key,
            excluded=lambda occurrence: occurrence.container in self._removals,
        )
        selected = (
            merged
            if role is None
            else (occurrence for occurrence in merged if occurrence.role is role)
        )
        yield from bounded(selected, limit)

    def references_to(
        self,
        key: ReferenceKey,
        *,
        role: ReferenceRole | None = None,
        limit: int | None = None,
    ) -> Iterator[ReferenceOccurrence]:
        yield from self.iter(key, role=role, limit=limit)

    def keys(self, *, limit: int | None = None) -> Iterator[ReferenceKey]:
        iterables: list[Iterable[ReferenceKey]] = [self._postings, self._additions]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            if member_index is None:
                iterables.append(source.keys())
            else:
                iterables.append(self._transform_key(value, member_index) for value in source)
        merged = canonical_merge(
            [sorted(values, key=canonical_bytes) for values in iterables],
            key=canonical_bytes,
        )
        yield from bounded((key for key in merged if any(self.iter(key, limit=1))), limit)

    def count(self, key: ReferenceKey) -> int:
        return sum(1 for _ in self.iter(key))

    def __iter__(self) -> Iterator[ReferenceKey]:
        yield from self.keys()

    def tuple(
        self,
        key: ReferenceKey,
        *,
        role: ReferenceRole | None = None,
        limit: int | None = None,
    ) -> tuple[ReferenceOccurrence, ...]:
        return tuple(self.iter(key, role=role, limit=limit))

    def _transform_key(self, key: ReferenceKey, member_index: int) -> ReferenceKey:
        if not isinstance(key, AnonymousIndividual):
            return key
        composite = cast(OntologyComposite, self._ontology)
        return cast(ReferenceKey, composite._scope_value(member_index, key))

    def _transform_occurrences(
        self,
        values: Iterable[ReferenceOccurrence],
        member_index: int,
    ) -> Iterator[ReferenceOccurrence]:
        composite = cast(OntologyComposite, self._ontology)
        for value in values:
            container = composite._scope_value(member_index, value.container)
            axiom = (
                cast(AxiomNode, composite._scope_value(member_index, value.axiom))
                if value.axiom is not None
                else None
            )
            key = self._transform_key(value.key, member_index)
            yield ReferenceOccurrence(
                key,
                axiom,
                container,
                (
                    prefix_composite_member_origins(
                        composite,
                        member_index,
                        value.origins,
                    )
                    if self.options.include_origins and self.options.include_source_provenance
                    else ()
                ),
                value.constructor_path,
                value.role,
            )


def _scan(
    ontology: OntologyView,
    options: EntityReferenceOptions,
    roots: Iterable[StructuralNode],
    budget: IndexBuildBudget,
    table: str,
) -> FrozenMap[ReferenceKey, tuple[ReferenceOccurrence, ...]]:
    values: dict[ReferenceKey, list[ReferenceOccurrence]] = {}
    for root in roots:
        if (
            not options.include_annotations
            and isinstance(root, AxiomNode)
            and type(root) in ANNOTATION_AXIOM_TYPES
        ):
            continue
        root_origins = origins_for(
            ontology,
            root,
            include=options.include_origins and options.include_source_provenance,
        )
        axiom = root if isinstance(root, AxiomNode) else None
        for node, path, role in iter_structural_occurrences(
            root,
            include_annotations=options.include_annotations,
        ):
            if not isinstance(node, (Entity, IRI, AnonymousIndividual)):
                continue
            occurrence = ReferenceOccurrence(
                node,
                axiom,
                root,
                root_origins,
                path,
                role,
            )
            values.setdefault(node, []).append(occurrence)
            budget.add(table, bytes_=128 + len(canonical_bytes(node)))
    return freeze_mapping(
        {key: tuple(sorted(postings, key=_occurrence_key)) for key, postings in values.items()}
    )


def _validate_key(key: object) -> None:
    if not isinstance(key, (Entity, IRI, AnonymousIndividual)):
        raise TypeError("reference key must be Entity, IRI, or AnonymousIndividual")


def _occurrence_key(value: ReferenceOccurrence) -> bytes:
    path = b"".join(
        step.field_id.constructor_tag.to_bytes(2, "big")
        + step.field_id.field_ordinal.to_bytes(2, "big")
        + (0xFFFFFFFF if step.item_index is None else step.item_index).to_bytes(4, "big")
        for step in value.constructor_path
    )
    return (
        canonical_bytes(value.container)
        + b"\x00"
        + path
        + b"\x00"
        + value.role.value.encode("ascii")
        + b"\x00"
        + canonical_bytes(value.key)
    )


__all__ = [
    "EntityReferenceIndex",
    "EntityReferenceOptions",
    "ReferenceKey",
    "ReferenceOccurrence",
]
