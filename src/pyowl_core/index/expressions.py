"""Canonical class/data/property expression occurrence postings."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TypeAlias, cast

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.cancellation import CancellationToken
from pyowl_core.document.composite import OntologyComposite
from pyowl_core.document.overlay import OntologyOverlay
from pyowl_core.document.provenance import OriginOccurrence
from pyowl_core.document.snapshot import AxiomScope, OntologyView, _is_ontology_view
from pyowl_core.model import (
    CLASS_EXPRESSION_TYPES,
    DATA_RANGE_TYPES,
    DataProperty,
    ObjectInverseOf,
    ObjectProperty,
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

Expression: TypeAlias = StructuralNode
_EXPRESSION_TYPES = cast(
    tuple[type[StructuralNode], ...],
    (
        *CLASS_EXPRESSION_TYPES,
        *DATA_RANGE_TYPES,
        ObjectProperty,
        ObjectInverseOf,
        DataProperty,
    ),
)


@dataclass(frozen=True, slots=True)
class ExpressionOccurrenceOptions(ScopedIndexOptions):
    include_annotations: bool = True

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        if not isinstance(self.include_annotations, bool):
            raise TypeError("include_annotations must be bool")


@dataclass(frozen=True, slots=True)
class ExpressionOccurrence:
    expression: Expression
    axiom: AxiomNode | None
    container: StructuralNode
    origins: tuple[OriginOccurrence, ...]
    constructor_path: ConstructorPath
    role: ReferenceRole


class ExpressionOccurrenceIndex:
    """Interned canonical expressions and every containing structural path."""

    SCHEMA_NAME = "pyowl-core/expression-occurrence-index"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = ExpressionOccurrenceOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: ExpressionOccurrenceOptions,
        postings: FrozenMap[Expression, tuple[ExpressionOccurrence, ...]],
        sources: tuple[ExpressionOccurrenceIndex, ...],
        source_indexes: tuple[int | None, ...],
        additions: FrozenMap[Expression, tuple[ExpressionOccurrence, ...]],
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
    ) -> ExpressionOccurrenceIndex:
        if not isinstance(options, ExpressionOccurrenceOptions):
            raise TypeError("options must be ExpressionOccurrenceOptions")
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
                cancellation_token=cancellation_token,
            )
            budget.add_shared_rows(source.report.total_row_count)
            additions: FrozenMap[Expression, tuple[ExpressionOccurrence, ...]] = FrozenMap()
            removals: frozenset[StructuralNode] = frozenset()
            if options.scope is AxiomScope.CLOSURE:
                additions = _scan(
                    view,
                    options,
                    (*ontology.delta.add_ontology_annotations, *ontology.delta.add_axioms),
                    budget,
                    "delta_occurrences",
                )
                removals = frozenset(
                    (*ontology.delta.remove_axioms, *ontology.delta.remove_ontology_annotations)
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
            sources: list[ExpressionOccurrenceIndex] = []
            for member_view in ontology._sources:
                sources.append(
                    member_view.view(
                        cls,
                        include_origins=options.include_origins,
                        include_annotations=options.include_annotations,
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
                "bridge_occurrences",
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
        roots: list[StructuralNode] = []
        if options.include_annotations:
            roots.extend(
                view.ontology_annotations(
                    scope=options.scope,
                    document_key=options.document_key,
                )
            )
        roots.extend(
            cast(
                Iterable[StructuralNode],
                view.iter_axioms(scope=options.scope, document_key=options.document_key),
            )
        )
        roots.extend(view.iter_extensions(scope=options.scope, document_key=options.document_key))
        postings = _scan(view, options, roots, budget, "expression_occurrences")
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
        expression: Expression,
        *,
        role: ReferenceRole | None = None,
        limit: int | None = None,
    ) -> Iterator[ExpressionOccurrence]:
        _validate_expression(expression)
        if role is not None and not isinstance(role, ReferenceRole):
            raise TypeError("role must be ReferenceRole or None")
        iterables: list[Iterable[ExpressionOccurrence]] = [self._postings.get(expression, ())]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            if member_index is None:
                iterables.append(source.iter(expression))
                continue
            for source_expression in source.expressions():
                if self._transform_expression(source_expression, member_index) == expression:
                    iterables.append(
                        self._transform_occurrences(
                            source.iter(source_expression),
                            member_index,
                        )
                    )
        iterables.append(self._additions.get(expression, ()))
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

    def occurrences(
        self,
        expression: Expression,
        *,
        role: ReferenceRole | None = None,
        limit: int | None = None,
    ) -> Iterator[ExpressionOccurrence]:
        yield from self.iter(expression, role=role, limit=limit)

    def expressions(self, *, limit: int | None = None) -> Iterator[Expression]:
        iterables: list[Iterable[Expression]] = [self._postings, self._additions]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            if member_index is None:
                iterables.append(source.expressions())
            else:
                iterables.append(
                    self._transform_expression(expression, member_index)
                    for expression in source.expressions()
                )
        merged = canonical_merge(
            [sorted(values, key=canonical_bytes) for values in iterables],
            key=canonical_bytes,
        )
        yield from bounded(
            (expression for expression in merged if any(self.iter(expression, limit=1))),
            limit,
        )

    def bulk(
        self,
        *,
        limit: int | None = None,
    ) -> Iterator[tuple[Expression, tuple[ExpressionOccurrence, ...]]]:
        for expression in self.expressions(limit=limit):
            yield expression, tuple(self.iter(expression))

    def count(self, expression: Expression) -> int:
        return sum(1 for _ in self.iter(expression))

    def _transform_expression(self, expression: Expression, member_index: int) -> Expression:
        composite = cast(OntologyComposite, self._ontology)
        return composite._scope_value(member_index, expression)

    def _transform_occurrences(
        self,
        values: Iterable[ExpressionOccurrence],
        member_index: int,
    ) -> Iterator[ExpressionOccurrence]:
        composite = cast(OntologyComposite, self._ontology)
        for value in values:
            expression = self._transform_expression(value.expression, member_index)
            container = composite._scope_value(member_index, value.container)
            axiom = (
                cast(AxiomNode, composite._scope_value(member_index, value.axiom))
                if value.axiom is not None
                else None
            )
            yield ExpressionOccurrence(
                expression,
                axiom,
                container,
                (
                    prefix_composite_member_origins(
                        composite,
                        member_index,
                        value.origins,
                    )
                    if self.options.include_origins
                    else ()
                ),
                value.constructor_path,
                value.role,
            )


def _scan(
    ontology: OntologyView,
    options: ExpressionOccurrenceOptions,
    roots: Iterable[StructuralNode],
    budget: IndexBuildBudget,
    table: str,
) -> FrozenMap[Expression, tuple[ExpressionOccurrence, ...]]:
    interned: dict[bytes, Expression] = {}
    values: dict[Expression, list[ExpressionOccurrence]] = {}
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
            include=options.include_origins,
        )
        axiom = root if isinstance(root, AxiomNode) else None
        for node, path, role in iter_structural_occurrences(
            root,
            include_annotations=options.include_annotations,
        ):
            if not isinstance(node, _EXPRESSION_TYPES):
                continue
            encoded = canonical_bytes(node)
            expression = interned.setdefault(encoded, node)
            values.setdefault(expression, []).append(
                ExpressionOccurrence(
                    expression,
                    axiom,
                    root,
                    root_origins,
                    path,
                    role,
                )
            )
            budget.add(table, bytes_=128 + len(encoded))
    return freeze_mapping(
        {
            expression: tuple(sorted(occurrences, key=_occurrence_key))
            for expression, occurrences in values.items()
        }
    )


def _validate_expression(value: object) -> None:
    if not isinstance(value, _EXPRESSION_TYPES):
        raise TypeError("expression must be a class/data/property expression")


def _occurrence_key(value: ExpressionOccurrence) -> bytes:
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
        + canonical_bytes(value.expression)
    )


__all__ = [
    "Expression",
    "ExpressionOccurrence",
    "ExpressionOccurrenceIndex",
    "ExpressionOccurrenceOptions",
]
