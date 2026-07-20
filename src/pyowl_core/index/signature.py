"""Typed signature view with declaration and annotation-only policies."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import cast

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import LoadOptions
from pyowl_core.document.composite import OntologyComposite
from pyowl_core.document.overlay import OntologyOverlay
from pyowl_core.document.snapshot import AxiomScope, OntologyView, _is_ontology_view
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.model import IRI, Annotation, Entity, EntityKind, StructuralNode, canonical_bytes
from pyowl_core.model.axioms import ANNOTATION_AXIOM_TYPES, AxiomNode, Declaration

from .cache import (
    IndexBuildBudget,
    ViewBuildReport,
    ViewBuildStrategy,
    build_report,
)
from .common import ScopedIndexOptions, bounded, canonical_merge, iter_structural_occurrences


@dataclass(frozen=True, slots=True)
class SignatureOptions(ScopedIndexOptions):
    kind: EntityKind | None = None
    declared_only: bool = False
    include_builtins: bool = True
    include_annotation_only: bool = True

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        kind = self.kind
        if isinstance(kind, str) and not isinstance(kind, EntityKind):
            try:
                kind = EntityKind(kind)
            except ValueError as error:
                raise ValueError("kind must be a valid EntityKind or None") from error
            object.__setattr__(self, "kind", kind)
        elif kind is not None and not isinstance(kind, EntityKind):
            raise TypeError("kind must be EntityKind or None")
        for name in ("declared_only", "include_builtins", "include_annotation_only"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class _Contribution:
    referenced: frozenset[Entity]
    nonannotation: frozenset[Entity]
    declared: frozenset[Entity]


class SignatureView:
    """A typed, punning-preserving view over every asserted reference."""

    SCHEMA_NAME = "pyowl-core/signature-view"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = SignatureOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: SignatureOptions,
        referenced: FrozenMap[Entity, int],
        nonannotation: FrozenMap[Entity, int],
        declared: FrozenMap[Entity, int],
        sources: tuple[SignatureView, ...],
        adjustments: tuple[
            FrozenMap[Entity, int],
            FrozenMap[Entity, int],
            FrozenMap[Entity, int],
        ],
        report: ViewBuildReport,
        native_owner: object | None = None,
    ) -> None:
        self._ontology = ontology
        self.options = options
        self._referenced = referenced
        self._nonannotation = nonannotation
        self._declared = declared
        self._sources = sources
        self._adjustments = adjustments
        self.report = report
        self._native_owner = native_owner

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> SignatureView:
        if not isinstance(options, SignatureOptions):
            raise TypeError("options must be SignatureOptions")
        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        view = ontology
        if isinstance(ontology, OntologyOverlay):
            source = ontology.base.view(
                cls,
                scope=options.scope,
                document_key=options.document_key,
                include_origins=options.include_origins,
                kind=options.kind,
                declared_only=options.declared_only,
                include_builtins=options.include_builtins,
                include_annotation_only=options.include_annotation_only,
                cancellation_token=cancellation_token,
            )
            budget.add_shared_rows(source.report.total_row_count)
            roots: list[tuple[StructuralNode, int]] = []
            if options.scope is AxiomScope.CLOSURE:
                roots.extend((value, 1) for value in ontology.delta.add_axioms)
                roots.extend((value, -1) for value in ontology.delta.remove_axioms)
                roots.extend((value, 1) for value in ontology.delta.add_ontology_annotations)
                roots.extend((value, -1) for value in ontology.delta.remove_ontology_annotations)
            adjustments = _adjustments(roots, budget)
            return cls(
                view,
                options,
                FrozenMap(),
                FrozenMap(),
                FrozenMap(),
                (source,),
                adjustments,
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
            sources: list[SignatureView] = []
            for member_view in ontology._sources:
                sources.append(
                    member_view.view(
                        cls,
                        include_origins=options.include_origins,
                        kind=options.kind,
                        declared_only=options.declared_only,
                        include_builtins=options.include_builtins,
                        include_annotation_only=options.include_annotation_only,
                        cancellation_token=cancellation_token,
                    )
                )
                budget.add_shared_rows(sources[-1].report.total_row_count)
                budget.add("member_adapters", rows=0, bytes_=128)
            edits: list[tuple[StructuralNode, int]] = [
                *((value, 1) for value in ontology.delta.add_axioms),
                *((value, 1) for value in ontology.delta.add_ontology_annotations),
            ]
            for removed in ontology.delta.remove_axioms:
                matches = 0
                for member_index, member_view in enumerate(ontology._sources):
                    for candidate in member_view.iter_axioms(type(removed)):
                        moved = ontology._scope_value(member_index, cast(StructuralNode, candidate))
                        if moved == removed:
                            matches += 1
                edits.extend((removed, -1) for _ in range(matches))
            for removed_annotation in ontology.delta.remove_ontology_annotations:
                matches = 0
                for member_index, member_view in enumerate(ontology._sources):
                    for annotation_candidate in member_view.ontology_annotations():
                        if (
                            ontology._scope_value(member_index, annotation_candidate)
                            == removed_annotation
                        ):
                            matches += 1
                edits.extend((removed_annotation, -1) for _ in range(matches))
            adjustments = _adjustments(edits, budget)
            shared = sum(item.report.own_bytes + item.report.shared_bytes for item in sources)
            return cls(
                view,
                options,
                FrozenMap(),
                FrozenMap(),
                FrozenMap(),
                tuple(sources),
                adjustments,
                build_report(
                    cls,
                    ViewBuildStrategy.MERGED,
                    budget,
                    started,
                    shared_bytes=shared,
                ),
            )
        load_options = getattr(view, "load_options", None)
        retained = None
        if (
            isinstance(load_options, LoadOptions)
            and getattr(view, "_native_snapshot_state", None) is not None
        ):
            from pyowl_core.backends.native import _retained_signature_counts_v1

            retained = _retained_signature_counts_v1(
                view,
                scope=options.scope,
                document_key=options.document_key,
                limits=load_options.limits,
                cancellation_token=cancellation_token,
            )
        counts: tuple[dict[Entity, int], dict[Entity, int], dict[Entity, int]] = (
            {},
            {},
            {},
        )
        if retained is not None:
            entities = view.signature(
                scope=options.scope,
                document_key=options.document_key,
                include_builtins=True,
            )
            if len(entities) != retained.entity_rows or any(
                not isinstance(entity, Entity) for entity in entities
            ):
                raise BackendProtocolError(
                    "retained signature counts diverge from its ontology",
                    code="NATIVE_INDEX_RESULT",
                )
            for entity, referenced, nonannotation, declared in zip(
                entities,
                retained.referenced_counts,
                retained.nonannotation_counts,
                retained.declaration_counts,
                strict=True,
            ):
                row_bytes = 64 + len(canonical_bytes(entity))
                for target, amount, table in (
                    (counts[0], referenced, "referenced"),
                    (counts[1], nonannotation, "nonannotation"),
                    (counts[2], declared, "declarations"),
                ):
                    if amount:
                        target[entity] = amount
                        budget.add(table, rows=amount, bytes_=amount * row_bytes)
            return cls(
                view,
                options,
                freeze_mapping(counts[0]),
                freeze_mapping(counts[1]),
                freeze_mapping(counts[2]),
                (),
                (FrozenMap(), FrozenMap(), FrozenMap()),
                build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
                retained.owner,
            )
        for root in _roots(view, options):
            contribution = _contribution(root)
            _apply(counts[0], contribution.referenced, 1, budget, "referenced")
            _apply(counts[1], contribution.nonannotation, 1, budget, "nonannotation")
            _apply(counts[2], contribution.declared, 1, budget, "declarations")
        return cls(
            view,
            options,
            freeze_mapping(counts[0]),
            freeze_mapping(counts[1]),
            freeze_mapping(counts[2]),
            (),
            (FrozenMap(), FrozenMap(), FrozenMap()),
            build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
        )

    def iter(self, *, limit: int | None = None) -> Iterator[Entity]:
        candidates: list[Iterable[Entity]] = [self._material_candidates()]
        candidates.extend(source._all_candidates() for source in self._sources)
        candidates.append(self._adjustment_candidates())
        merged = canonical_merge(
            [sorted(values, key=_entity_key) for values in candidates],
            key=_entity_key,
        )
        yield from bounded((value for value in merged if self._selected(value)), limit)

    def as_tuple(self, *, limit: int | None = None) -> tuple[Entity, ...]:
        """Allocate the canonical signature as a convenience tuple."""

        return tuple(self.iter(limit=limit))

    def entities_by_iri(self, iri: IRI | str) -> tuple[Entity, ...]:
        selected = IRI(iri) if isinstance(iri, str) else iri
        if not isinstance(selected, IRI):
            raise TypeError("iri must be IRI or absolute IRI string")
        return tuple(value for value in self.iter() if value.iri == selected)

    def flat_iris(self) -> tuple[IRI, ...]:
        values = {entity.iri for entity in self.iter()}
        return tuple(sorted(values, key=canonical_bytes))

    def reference_count(self, entity: Entity) -> int:
        return self._count(entity, 0)

    def declaration_count(self, entity: Entity) -> int:
        return self._count(entity, 2)

    def _selected(self, entity: Entity) -> bool:
        if self.options.kind is not None and entity.kind is not self.options.kind:
            return False
        if not self.options.include_builtins and _is_builtin(entity):
            return False
        mode = (
            2 if self.options.declared_only else (0 if self.options.include_annotation_only else 1)
        )
        return self._count(entity, mode) > 0

    def _count(self, entity: Entity, mode: int) -> int:
        local = (self._referenced, self._nonannotation, self._declared)[mode].get(entity, 0)
        inherited = sum(source._count(entity, mode) for source in self._sources)
        return max(0, local + inherited + self._adjustments[mode].get(entity, 0))

    def _material_candidates(self) -> Iterator[Entity]:
        yield from self._referenced
        yield from self._nonannotation
        yield from self._declared

    def _adjustment_candidates(self) -> Iterator[Entity]:
        for values in self._adjustments:
            yield from values

    def _all_candidates(self) -> Iterator[Entity]:
        yield from self._material_candidates()
        yield from self._adjustment_candidates()
        for source in self._sources:
            yield from source._all_candidates()


def _roots(view: OntologyView, options: SignatureOptions) -> Iterator[StructuralNode]:
    yield from view.ontology_annotations(scope=options.scope, document_key=options.document_key)
    yield from view.iter_axioms(scope=options.scope, document_key=options.document_key)
    yield from view.iter_extensions(scope=options.scope, document_key=options.document_key)


def _entities(root: StructuralNode, *, include_annotations: bool) -> frozenset[Entity]:
    return frozenset(
        node
        for node, _path, _role in iter_structural_occurrences(
            root,
            include_annotations=include_annotations,
        )
        if isinstance(node, Entity)
    )


def _contribution(root: StructuralNode) -> _Contribution:
    referenced = _entities(root, include_annotations=True)
    annotation_root = isinstance(root, Annotation) or (
        isinstance(root, AxiomNode) and type(root) in ANNOTATION_AXIOM_TYPES
    )
    nonannotation = frozenset() if annotation_root else _entities(root, include_annotations=False)
    declared = frozenset((root.entity,)) if isinstance(root, Declaration) else frozenset()
    return _Contribution(referenced, nonannotation, declared)


def _adjustments(
    roots: Iterable[tuple[StructuralNode, int]],
    budget: IndexBuildBudget,
) -> tuple[FrozenMap[Entity, int], FrozenMap[Entity, int], FrozenMap[Entity, int]]:
    values: tuple[dict[Entity, int], dict[Entity, int], dict[Entity, int]] = ({}, {}, {})
    for root, amount in roots:
        contribution = _contribution(root)
        _apply(values[0], contribution.referenced, amount, budget, "delta_referenced")
        _apply(values[1], contribution.nonannotation, amount, budget, "delta_nonannotation")
        _apply(values[2], contribution.declared, amount, budget, "delta_declarations")
    return freeze_mapping(values[0]), freeze_mapping(values[1]), freeze_mapping(values[2])


def _apply(
    target: dict[Entity, int],
    entities: Iterable[Entity],
    amount: int,
    budget: IndexBuildBudget,
    table: str,
) -> None:
    for entity in entities:
        target[entity] = target.get(entity, 0) + amount
        budget.add(table, bytes_=64 + len(canonical_bytes(entity)))


_KIND_ORDER = {kind: index for index, kind in enumerate(EntityKind)}


def _entity_key(entity: Entity) -> bytes:
    return bytes((_KIND_ORDER[entity.kind],)) + entity.iri.value.encode("utf-8")


def _is_builtin(entity: Entity) -> bool:
    return (entity.kind, entity.iri.value) in _BUILTIN_ENTITIES


_XSD_DATATYPES = {
    "anyURI",
    "base64Binary",
    "boolean",
    "byte",
    "dateTime",
    "dateTimeStamp",
    "decimal",
    "double",
    "float",
    "hexBinary",
    "int",
    "integer",
    "language",
    "long",
    "Name",
    "NCName",
    "negativeInteger",
    "NMTOKEN",
    "nonNegativeInteger",
    "nonPositiveInteger",
    "normalizedString",
    "positiveInteger",
    "short",
    "string",
    "token",
    "unsignedByte",
    "unsignedInt",
    "unsignedLong",
    "unsignedShort",
}
_BUILTIN_ENTITIES = frozenset(
    {
        (EntityKind.CLASS, "http://www.w3.org/2002/07/owl#Thing"),
        (EntityKind.CLASS, "http://www.w3.org/2002/07/owl#Nothing"),
        (
            EntityKind.OBJECT_PROPERTY,
            "http://www.w3.org/2002/07/owl#topObjectProperty",
        ),
        (
            EntityKind.OBJECT_PROPERTY,
            "http://www.w3.org/2002/07/owl#bottomObjectProperty",
        ),
        (EntityKind.DATA_PROPERTY, "http://www.w3.org/2002/07/owl#topDataProperty"),
        (EntityKind.DATA_PROPERTY, "http://www.w3.org/2002/07/owl#bottomDataProperty"),
        (EntityKind.DATATYPE, "http://www.w3.org/2000/01/rdf-schema#Literal"),
        (
            EntityKind.DATATYPE,
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral",
        ),
        (EntityKind.DATATYPE, "http://www.w3.org/1999/02/22-rdf-syntax-ns#XMLLiteral"),
        *(
            (EntityKind.DATATYPE, "http://www.w3.org/2001/XMLSchema#" + local)
            for local in _XSD_DATATYPES
        ),
        *(
            (EntityKind.ANNOTATION_PROPERTY, iri)
            for iri in (
                "http://www.w3.org/2000/01/rdf-schema#label",
                "http://www.w3.org/2000/01/rdf-schema#comment",
                "http://www.w3.org/2000/01/rdf-schema#seeAlso",
                "http://www.w3.org/2000/01/rdf-schema#isDefinedBy",
                "http://www.w3.org/2002/07/owl#deprecated",
                "http://www.w3.org/2002/07/owl#versionInfo",
                "http://www.w3.org/2002/07/owl#priorVersion",
                "http://www.w3.org/2002/07/owl#backwardCompatibleWith",
                "http://www.w3.org/2002/07/owl#incompatibleWith",
            )
        ),
    }
)


__all__ = ["SignatureOptions", "SignatureView"]
