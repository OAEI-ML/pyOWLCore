"""Shared immutable options, constructor paths, and posting utilities."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar, cast

from pyowl_core.document.provenance import OriginOccurrence
from pyowl_core.document.snapshot import AxiomScope, OntologyView
from pyowl_core.model import Annotation, CanonicalSet, StructuralNode, canonical_bytes
from pyowl_core.model.axioms import AxiomNode
from pyowl_core.model.registry import CONSTRUCTOR_SPECS, constructor_spec

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ScopedIndexOptions:
    """Canonical scope/origin options shared by asserted structural views."""

    scope: AxiomScope = AxiomScope.CLOSURE
    document_key: str | None = None
    include_origins: bool = True

    def __post_init__(self) -> None:
        scope = self.scope
        if isinstance(scope, str) and not isinstance(scope, AxiomScope):
            try:
                scope = AxiomScope(scope)
            except ValueError as error:
                raise ValueError("scope must be a valid AxiomScope") from error
            object.__setattr__(self, "scope", scope)
        elif not isinstance(scope, AxiomScope):
            raise TypeError("scope must be AxiomScope")
        if scope is AxiomScope.DOCUMENT:
            if not isinstance(self.document_key, str) or not self.document_key:
                raise ValueError("document scope requires a nonempty document_key")
        elif self.document_key is not None:
            raise ValueError(f"document_key is not valid for {scope.value} scope")
        if not isinstance(self.include_origins, bool):
            raise TypeError("include_origins must be bool")


class ReferenceRole(str, Enum):
    ROOT = "root"
    STRUCTURAL = "structural"
    IRI = "iri"
    DECLARATION = "declaration"
    ANNOTATION = "annotation"
    SUBJECT = "subject"
    OBJECT = "object"
    PROPERTY = "property"
    SUB_PROPERTY = "sub_property"
    SUPER_PROPERTY = "super_property"
    SUBCLASS = "subclass"
    SUPERCLASS = "superclass"
    CLASS_EXPRESSION = "class_expression"
    FILLER = "filler"
    DOMAIN = "domain"
    RANGE = "range"
    VALUE = "value"
    INDIVIDUAL = "individual"
    OPERAND = "operand"
    DEFINED_CLASS = "defined_class"
    DATATYPE = "datatype"
    FACET = "facet"
    RULE_BODY = "rule_body"
    RULE_HEAD = "rule_head"
    RULE_PREDICATE = "rule_predicate"
    RULE_ARGUMENT = "rule_argument"


@dataclass(frozen=True, slots=True, order=True)
class FieldID:
    """Schema-stable constructor tag plus field ordinal."""

    constructor_tag: int
    field_ordinal: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.constructor_tag, bool)
            or not isinstance(self.constructor_tag, int)
            or self.constructor_tag < 1
        ):
            raise ValueError("constructor_tag must be positive")
        if (
            isinstance(self.field_ordinal, bool)
            or not isinstance(self.field_ordinal, int)
            or self.field_ordinal < 0
        ):
            raise ValueError("field_ordinal must be nonnegative")


@dataclass(frozen=True, slots=True, order=True)
class ConstructorPathStep:
    field_id: FieldID
    item_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.field_id, FieldID):
            raise TypeError("field_id must be FieldID")
        if self.item_index is not None and (
            isinstance(self.item_index, bool)
            or not isinstance(self.item_index, int)
            or self.item_index < 0
        ):
            raise ValueError("item_index must be nonnegative or None")


ConstructorPath = tuple[ConstructorPathStep, ...]


def _role_for(constructor_name: str, field: str) -> ReferenceRole:
    if field == "annotations":
        return ReferenceRole.ANNOTATION
    if field == "entity":
        return ReferenceRole.DECLARATION
    if field == "sub_class":
        return ReferenceRole.SUBCLASS
    if field == "super_class":
        return ReferenceRole.SUPERCLASS
    if field == "sub_property":
        return ReferenceRole.SUB_PROPERTY
    if field == "super_property":
        return ReferenceRole.SUPER_PROPERTY
    if field == "defined_class":
        return ReferenceRole.DEFINED_CLASS
    if field == "domain":
        return ReferenceRole.DOMAIN
    if field in {"range", "data_range"}:
        return ReferenceRole.RANGE
    if field == "filler":
        return ReferenceRole.FILLER
    if field in {"expressions", "operands", "properties", "restrictions"}:
        return ReferenceRole.OPERAND
    if field in {"object_properties", "data_properties", "property"}:
        return ReferenceRole.PROPERTY
    if field == "individuals":
        return ReferenceRole.INDIVIDUAL
    if field in {"subject", "source", "first"}:
        return ReferenceRole.SUBJECT
    if field in {"target", "second"}:
        return (
            ReferenceRole.RULE_ARGUMENT
            if constructor_name.endswith("Atom")
            else ReferenceRole.OBJECT
        )
    if field in {"argument", "arguments"}:
        return ReferenceRole.RULE_ARGUMENT
    if field == "predicate":
        return ReferenceRole.RULE_PREDICATE
    if field == "body":
        return ReferenceRole.RULE_BODY
    if field == "head":
        return ReferenceRole.RULE_HEAD
    if field == "class_expression":
        return ReferenceRole.CLASS_EXPRESSION
    if field == "datatype":
        return ReferenceRole.DATATYPE
    if field in {"facet", "value"}:
        return ReferenceRole.VALUE if field == "value" else ReferenceRole.FACET
    if field == "iri":
        return ReferenceRole.IRI
    return ReferenceRole.STRUCTURAL


FIELD_ROLE_TABLE = {
    FieldID(spec.tag, ordinal): _role_for(spec.constructor.__name__, name)
    for spec in CONSTRUCTOR_SPECS
    for ordinal, name in enumerate(spec.fields)
}


def iter_structural_occurrences(
    root: StructuralNode,
    *,
    include_annotations: bool = True,
) -> Iterator[tuple[StructuralNode, ConstructorPath, ReferenceRole]]:
    """Walk every registered constructor using only schema field identifiers."""

    if not isinstance(root, StructuralNode):
        raise TypeError("root must be StructuralNode")
    if not isinstance(include_annotations, bool):
        raise TypeError("include_annotations must be bool")
    stack: list[tuple[StructuralNode, ConstructorPath, ReferenceRole, bool]] = [
        (root, (), ReferenceRole.ROOT, False)
    ]
    active: set[int] = set()
    while stack:
        current, path, role, exiting = stack.pop()
        identity = id(current)
        if exiting:
            active.remove(identity)
            continue
        if identity in active:
            raise ValueError("cyclic structural value graph")
        active.add(identity)
        yield current, path, role
        spec = constructor_spec(current)
        children: list[tuple[StructuralNode, ConstructorPath, ReferenceRole, bool]] = []
        for ordinal, field_name in enumerate(spec.fields):
            field_id = FieldID(spec.tag, ordinal)
            child_role = FIELD_ROLE_TABLE[field_id]
            if not include_annotations and child_role is ReferenceRole.ANNOTATION:
                continue
            value = getattr(current, field_name)
            if isinstance(value, StructuralNode):
                children.append(
                    (
                        value,
                        (*path, ConstructorPathStep(field_id)),
                        child_role,
                        False,
                    )
                )
            elif isinstance(value, (CanonicalSet, tuple)):
                for index, item in enumerate(value):
                    if isinstance(item, StructuralNode):
                        children.append(
                            (
                                item,
                                (*path, ConstructorPathStep(field_id, index)),
                                child_role,
                                False,
                            )
                        )
        stack.append((current, path, role, True))
        stack.extend(reversed(children))


def roots_for_options(
    ontology: OntologyView,
    options: ScopedIndexOptions,
    *,
    include_annotations: bool = True,
    include_extensions: bool = True,
) -> Iterator[StructuralNode]:
    if include_annotations:
        yield from ontology.ontology_annotations(
            scope=options.scope, document_key=options.document_key
        )
    yield from ontology.iter_axioms(scope=options.scope, document_key=options.document_key)
    if include_extensions:
        yield from ontology.iter_extensions(scope=options.scope, document_key=options.document_key)


def origins_for(
    ontology: OntologyView,
    root: StructuralNode,
    *,
    include: bool,
) -> tuple[OriginOccurrence, ...]:
    if not include:
        return ()
    from pyowl_core.document.composite import OntologyComposite

    if isinstance(ontology, OntologyComposite):
        return _composite_origins_for(ontology, root)
    method = getattr(ontology, "origins_for", None)
    if callable(method):
        return tuple(cast(Iterable[OriginOccurrence], method(root)))
    return ontology.origin_index.origins_for(root)


def prefix_composite_member_origins(
    composite: object,
    member_index: int,
    source_origins: Iterable[OriginOccurrence],
) -> tuple[OriginOccurrence, ...]:
    """Prefix already-local origins without constructing a composite origin index."""

    from pyowl_core.document.composite import OntologyComposite

    if not isinstance(composite, OntologyComposite):
        raise TypeError("composite must be OntologyComposite")
    if (
        isinstance(member_index, bool)
        or not isinstance(member_index, int)
        or not 0 <= member_index < len(composite._sources)
    ):
        raise IndexError("member_index is outside the composition")
    origins = tuple(source_origins)
    if not origins:
        origins = (OriginOccurrence("unknown", 0),)
    token = composite._source_tokens()[member_index]
    prefixed = tuple(
        OriginOccurrence(
            "member:" + token.hex() + ":" + origin.document_key,
            origin.occurrence,
            origin.span,
        )
        for origin in origins
    )
    composite.limits.enforce("max_origin_entries", len(prefixed))
    return prefixed


def _composite_origins_for(
    composite: object,
    root: StructuralNode,
) -> tuple[OriginOccurrence, ...]:
    from pyowl_core.document.composite import OntologyComposite

    selected = cast(OntologyComposite, composite)
    if root in selected.delta.remove_axioms or root in selected.delta.remove_ontology_annotations:
        return ()
    bridge_values = (
        *selected.delta.add_axioms,
        *selected.delta.add_ontology_annotations,
    )
    for occurrence, value in enumerate(bridge_values):
        if value == root:
            return (
                OriginOccurrence(
                    "bridge:" + selected.composition_provenance_digest.hex(),
                    occurrence,
                ),
            )
    gathered: set[OriginOccurrence] = set()
    for member_index, source in enumerate(selected._sources):
        if isinstance(root, AxiomNode):
            candidates: Iterable[StructuralNode] = cast(
                Iterable[StructuralNode], source.iter_axioms(type(root))
            )
        elif isinstance(root, Annotation):
            candidates = source.ontology_annotations()
        else:
            candidates = source.iter_extensions()
        for original in candidates:
            if selected._scope_value(member_index, original) != root:
                continue
            gathered.update(
                prefix_composite_member_origins(
                    selected,
                    member_index,
                    origins_for(source, original, include=True),
                )
            )
            selected.limits.enforce("max_origin_entries", len(gathered))
    return tuple(sorted(gathered))


def bounded(iterator: Iterable[T], limit: int | None = None) -> Iterator[T]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a nonnegative integer or None")
    for index, value in enumerate(iterator):
        if limit is not None and index >= limit:
            return
        yield value


def canonical_merge(
    iterables: Sequence[Iterable[T]],
    *,
    key: Callable[[T], bytes],
    excluded: Callable[[T], bool] | None = None,
) -> Iterator[T]:
    """K-way canonical merge that never copies complete source postings."""

    heap: list[tuple[bytes, int, T, Iterator[T]]] = []
    for ordinal, values in enumerate(iterables):
        iterator = iter(values)
        try:
            value = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (key(value), ordinal, value, iterator))
    previous: bytes | None = None
    while heap:
        selected, ordinal, value, iterator = heapq.heappop(heap)
        if selected != previous and (excluded is None or not excluded(value)):
            yield value
            previous = selected
        try:
            following = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (key(following), ordinal, following, iterator))


def structural_key(value: StructuralNode) -> bytes:
    return canonical_bytes(value)


def validate_axiom_type(value: object) -> type[AxiomNode]:
    if not isinstance(value, type) or not issubclass(value, AxiomNode):
        raise TypeError("axiom_type must be an exact axiom constructor")
    return value


def annotation_roots(axiom: AxiomNode) -> tuple[Annotation, ...]:
    return tuple(cast(Iterable[Annotation], getattr(axiom, "annotations", ())))


__all__ = [
    "FIELD_ROLE_TABLE",
    "ConstructorPath",
    "ConstructorPathStep",
    "FieldID",
    "ReferenceRole",
    "ScopedIndexOptions",
    "annotation_roots",
    "bounded",
    "canonical_merge",
    "iter_structural_occurrences",
    "origins_for",
    "prefix_composite_member_origins",
    "roots_for_options",
    "structural_key",
    "validate_axiom_type",
]
