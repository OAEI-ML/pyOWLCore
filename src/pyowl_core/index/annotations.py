"""Annotation assertion lookup, reverse values, and BCP 47 selection."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
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
    Annotation,
    AnnotationProperty,
    AnnotationSubject,
    AnnotationValue,
    AnonymousIndividual,
    Literal,
    StructuralNode,
    canonical_bytes,
)
from pyowl_core.model.axioms import AnnotationAssertion

from .cache import (
    IndexBuildBudget,
    ViewBuildReport,
    ViewBuildStrategy,
    build_report,
)
from .common import ScopedIndexOptions, bounded, canonical_merge, origins_for


@dataclass(frozen=True, slots=True)
class AnnotationAssertionOptions(ScopedIndexOptions):
    include_nested: bool = False

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        if not isinstance(self.include_nested, bool):
            raise TypeError("include_nested must be bool")


@dataclass(frozen=True, slots=True)
class AnnotationAssertionPosting:
    assertion: AnnotationAssertion
    origins: tuple[OriginOccurrence, ...]


@dataclass(frozen=True, slots=True)
class NestedAnnotationOccurrence:
    annotation: Annotation
    container: StructuralNode
    origins: tuple[OriginOccurrence, ...]


class AnnotationAssertionIndex:
    SCHEMA_NAME = "pyowl-core/annotation-assertion-index"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = AnnotationAssertionOptions
    DEPENDENCIES: tuple[type[object], ...] = ()

    def __init__(
        self,
        ontology: OntologyView,
        options: AnnotationAssertionOptions,
        postings: FrozenMap[AnnotationSubject, tuple[AnnotationAssertion, ...]],
        sources: tuple[AnnotationAssertionIndex, ...],
        source_indexes: tuple[int | None, ...],
        additions: FrozenMap[AnnotationSubject, tuple[AnnotationAssertion, ...]],
        removals: frozenset[AnnotationAssertion],
        nested: tuple[NestedAnnotationOccurrence, ...],
        report: ViewBuildReport,
    ) -> None:
        self._ontology = ontology
        self.options = options
        self._postings = postings
        self._sources = sources
        self._source_indexes = source_indexes
        self._additions = additions
        self._removals = removals
        self._nested = nested
        self.report = report

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> AnnotationAssertionIndex:
        if not isinstance(options, AnnotationAssertionOptions):
            raise TypeError("options must be AnnotationAssertionOptions")
        if not _is_ontology_view(ontology):
            raise TypeError("ontology must implement OntologyView")
        view = ontology
        if isinstance(ontology, OntologyOverlay):
            source = ontology.base.view(
                cls,
                scope=options.scope,
                document_key=options.document_key,
                include_origins=options.include_origins,
                include_nested=options.include_nested,
                cancellation_token=cancellation_token,
            )
            budget.add_shared_rows(source.report.total_row_count)
            additions: dict[AnnotationSubject, list[AnnotationAssertion]] = {}
            removals: frozenset[AnnotationAssertion] = frozenset()
            nested: tuple[NestedAnnotationOccurrence, ...] = ()
            if options.scope is AxiomScope.CLOSURE:
                added_roots: tuple[StructuralNode, ...] = (
                    *ontology.delta.add_ontology_annotations,
                    *ontology.delta.add_axioms,
                )
                additions = _assertions(added_roots, budget, "delta_assertions")
                if options.include_nested:
                    nested = _nested_occurrences(view, added_roots, options, budget, "delta_nested")
                removals = frozenset(
                    value
                    for value in ontology.delta.remove_axioms
                    if isinstance(value, AnnotationAssertion)
                )
                for value in (
                    *ontology.delta.remove_axioms,
                    *ontology.delta.remove_ontology_annotations,
                ):
                    budget.add("delta_tombstones", bytes_=64 + len(canonical_bytes(value)))
            return cls(
                view,
                options,
                FrozenMap(),
                (source,),
                (None,),
                _freeze(additions),
                removals,
                nested,
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
            sources: list[AnnotationAssertionIndex] = []
            for member_view in ontology._sources:
                sources.append(
                    member_view.view(
                        cls,
                        include_origins=options.include_origins,
                        include_nested=options.include_nested,
                        cancellation_token=cancellation_token,
                    )
                )
                budget.add_shared_rows(sources[-1].report.total_row_count)
                budget.add("member_adapters", rows=0, bytes_=128)
            additions = _assertions(ontology.delta.add_axioms, budget, "bridge_assertions")
            nested = (
                _nested_occurrences(
                    view,
                    (*ontology.delta.add_ontology_annotations, *ontology.delta.add_axioms),
                    options,
                    budget,
                    "bridge_nested",
                )
                if options.include_nested
                else ()
            )
            removals = frozenset(
                value
                for value in ontology.delta.remove_axioms
                if isinstance(value, AnnotationAssertion)
            )
            for value in (
                *ontology.delta.remove_axioms,
                *ontology.delta.remove_ontology_annotations,
            ):
                budget.add("bridge_tombstones", bytes_=64 + len(canonical_bytes(value)))
            shared = sum(item.report.own_bytes + item.report.shared_bytes for item in sources)
            return cls(
                view,
                options,
                FrozenMap(),
                tuple(sources),
                tuple(range(len(sources))),
                _freeze(additions),
                removals,
                nested,
                build_report(
                    cls,
                    ViewBuildStrategy.MERGED,
                    budget,
                    started,
                    shared_bytes=shared,
                ),
            )
        axioms = tuple(
            cast(
                Iterable[AnnotationAssertion],
                view.iter_axioms(
                    AnnotationAssertion,
                    scope=options.scope,
                    document_key=options.document_key,
                ),
            )
        )
        postings = _freeze(_assertions(axioms, budget, "assertions"))
        roots: tuple[StructuralNode, ...] = (
            *view.ontology_annotations(scope=options.scope, document_key=options.document_key),
            *cast(
                Iterable[StructuralNode],
                view.iter_axioms(scope=options.scope, document_key=options.document_key),
            ),
            *view.iter_extensions(scope=options.scope, document_key=options.document_key),
        )
        nested = (
            _nested_occurrences(view, roots, options, budget, "nested_annotations")
            if options.include_nested
            else ()
        )
        return cls(
            view,
            options,
            postings,
            (),
            (),
            FrozenMap(),
            frozenset(),
            nested,
            build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started),
        )

    def iter_subject(
        self,
        subject: AnnotationSubject,
        *,
        property: AnnotationProperty | None = None,
        limit: int | None = None,
    ) -> Iterator[AnnotationAssertionPosting]:
        _validate_subject(subject)
        if property is not None and not isinstance(property, AnnotationProperty):
            raise TypeError("property must be AnnotationProperty or None")
        iterables: list[Iterable[AnnotationAssertion]] = [self._postings.get(subject, ())]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            if member_index is None:
                iterables.append(source._iter_axioms(subject))
            elif isinstance(subject, AnonymousIndividual):
                for source_subject in source.subjects():
                    if (
                        isinstance(source_subject, AnonymousIndividual)
                        and self._transform_subject(source_subject, member_index) == subject
                    ):
                        iterables.append(
                            self._transform_assertions(
                                source._iter_axioms(source_subject), member_index
                            )
                        )
            else:
                iterables.append(
                    self._transform_assertions(source._iter_axioms(subject), member_index)
                )
        iterables.append(self._additions.get(subject, ()))
        merged = canonical_merge(
            [sorted(values, key=canonical_bytes) for values in iterables],
            key=canonical_bytes,
            excluded=lambda value: value in self._removals,
        )
        if property is not None:
            merged = (value for value in merged if value.property == property)
        yield from bounded(
            (
                AnnotationAssertionPosting(
                    value,
                    origins_for(
                        self._ontology,
                        value,
                        include=self.options.include_origins,
                    ),
                )
                for value in merged
            ),
            limit,
        )

    def assertions(
        self,
        subject: AnnotationSubject,
        *,
        property: AnnotationProperty | None = None,
        limit: int | None = None,
    ) -> Iterator[AnnotationAssertion]:
        yield from (
            item.assertion for item in self.iter_subject(subject, property=property, limit=limit)
        )

    def values(
        self,
        subject: AnnotationSubject,
        property: AnnotationProperty,
        *,
        limit: int | None = None,
    ) -> Iterator[AnnotationValue]:
        values = sorted(
            {assertion.value for assertion in self.assertions(subject, property=property)},
            key=canonical_bytes,
        )
        yield from bounded(values, limit)

    def reverse_iri_value(
        self,
        value: IRI,
        *,
        limit: int | None = None,
    ) -> Iterator[AnnotationAssertionPosting]:
        if not isinstance(value, IRI):
            raise TypeError("value must be IRI")
        iterables = (self.iter_subject(subject) for subject in self.subjects())
        selected = (
            posting
            for values in iterables
            for posting in values
            if posting.assertion.value == value
        )
        yield from bounded(
            sorted(selected, key=lambda item: canonical_bytes(item.assertion)),
            limit,
        )

    def subjects(self, *, limit: int | None = None) -> Iterator[AnnotationSubject]:
        iterables: list[Iterable[AnnotationSubject]] = [self._postings, self._additions]
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            if member_index is None:
                iterables.append(source.subjects())
            else:
                iterables.append(
                    self._transform_subject(value, member_index) for value in source.subjects()
                )
        merged = canonical_merge(
            [sorted(values, key=canonical_bytes) for values in iterables],
            key=canonical_bytes,
        )
        yield from bounded(
            (value for value in merged if any(self.assertions(value, limit=1))),
            limit,
        )

    def nested(
        self,
        *,
        property: AnnotationProperty | None = None,
        limit: int | None = None,
    ) -> Iterator[NestedAnnotationOccurrence]:
        if not self.options.include_nested:
            raise ValueError("include_nested=True is required for nested annotation queries")
        if property is not None and not isinstance(property, AnnotationProperty):
            raise TypeError("property must be AnnotationProperty or None")
        values: list[NestedAnnotationOccurrence] = list(self._nested)
        for ordinal, source in enumerate(self._sources):
            member_index = self._source_indexes[ordinal]
            for occurrence in source.nested():
                if member_index is None:
                    values.append(occurrence)
                else:
                    composite = cast(OntologyComposite, self._ontology)
                    container = composite._scope_value(member_index, occurrence.container)
                    values.append(
                        NestedAnnotationOccurrence(
                            cast(
                                Annotation,
                                composite._scope_value(member_index, occurrence.annotation),
                            ),
                            container,
                            origins_for(
                                self._ontology,
                                container,
                                include=self.options.include_origins,
                            ),
                        )
                    )
        selected = (
            item
            for item in values
            if item.container not in self._removals
            and (property is None or item.annotation.property == property)
        )
        yield from bounded(sorted(selected, key=_nested_key), limit)

    def select_literal(
        self,
        subject: AnnotationSubject,
        property: AnnotationProperty,
        preferred_languages: Sequence[str],
        *,
        include_untagged: bool = True,
    ) -> Literal | None:
        if isinstance(preferred_languages, (str, bytes)):
            raise TypeError("preferred_languages must be a sequence of language tags")
        preferences = tuple(_language_preference(value) for value in preferred_languages)
        literals = tuple(
            value for value in self.values(subject, property) if isinstance(value, Literal)
        )
        for preference in preferences:
            matches = sorted(
                (
                    value
                    for value in literals
                    if value.language is not None
                    and (
                        value.language == preference or value.language.startswith(preference + "-")
                    )
                ),
                key=canonical_bytes,
            )
            if matches:
                return matches[0]
        if include_untagged:
            untagged = sorted(
                (value for value in literals if value.language is None),
                key=canonical_bytes,
            )
            if untagged:
                return untagged[0]
        return None

    def _iter_axioms(self, subject: AnnotationSubject) -> Iterator[AnnotationAssertion]:
        yield from (item.assertion for item in self.iter_subject(subject))

    def _transform_subject(
        self, subject: AnnotationSubject, member_index: int
    ) -> AnnotationSubject:
        if isinstance(subject, IRI):
            return subject
        composite = cast(OntologyComposite, self._ontology)
        return cast(AnnotationSubject, composite._scope_value(member_index, subject))

    def _transform_assertions(
        self,
        values: Iterable[AnnotationAssertion],
        member_index: int,
    ) -> Iterator[AnnotationAssertion]:
        composite = cast(OntologyComposite, self._ontology)
        transformed = [
            cast(AnnotationAssertion, composite._scope_value(member_index, value))
            for value in values
        ]
        yield from sorted(transformed, key=canonical_bytes)


def _assertions(
    roots: Iterable[StructuralNode],
    budget: IndexBuildBudget,
    table: str,
) -> dict[AnnotationSubject, list[AnnotationAssertion]]:
    values: dict[AnnotationSubject, list[AnnotationAssertion]] = {}
    for root in roots:
        if isinstance(root, AnnotationAssertion):
            values.setdefault(root.subject, []).append(root)
            budget.add(table, bytes_=96 + len(canonical_bytes(root)))
    return values


def _freeze(
    values: dict[AnnotationSubject, list[AnnotationAssertion]],
) -> FrozenMap[AnnotationSubject, tuple[AnnotationAssertion, ...]]:
    return freeze_mapping(
        {
            subject: tuple(sorted(assertions, key=canonical_bytes))
            for subject, assertions in values.items()
        }
    )


def _nested_occurrences(
    ontology: OntologyView,
    roots: Iterable[StructuralNode],
    options: AnnotationAssertionOptions,
    budget: IndexBuildBudget,
    table: str,
) -> tuple[NestedAnnotationOccurrence, ...]:
    values: list[NestedAnnotationOccurrence] = []
    for root in roots:
        stack: list[StructuralNode] = []
        if isinstance(root, Annotation):
            stack.append(root)
        stack.extend(cast(Iterable[Annotation], getattr(root, "annotations", ())))
        while stack:
            annotation = cast(Annotation, stack.pop())
            values.append(
                NestedAnnotationOccurrence(
                    annotation,
                    root,
                    origins_for(ontology, root, include=options.include_origins),
                )
            )
            budget.add(table, bytes_=96 + len(canonical_bytes(annotation)))
            stack.extend(annotation.annotations)
    return tuple(sorted(values, key=_nested_key))


def _nested_key(value: NestedAnnotationOccurrence) -> bytes:
    return canonical_bytes(value.container) + b"\x00" + canonical_bytes(value.annotation)


def _validate_subject(subject: object) -> None:
    if not isinstance(subject, (IRI, AnonymousIndividual)):
        raise TypeError("subject must be IRI or AnonymousIndividual")


def _language_preference(value: object) -> str:
    if not isinstance(value, str) or not value or any(ord(item) > 127 for item in value):
        raise ValueError("language preferences must be nonempty ASCII BCP 47 ranges")
    parts = value.split("-")
    if any(not part or len(part) > 8 or not part.isalnum() for part in parts):
        raise ValueError("language preferences must be structurally valid BCP 47 ranges")
    return value.lower()


__all__ = [
    "AnnotationAssertionIndex",
    "AnnotationAssertionOptions",
    "AnnotationAssertionPosting",
    "NestedAnnotationOccurrence",
]
