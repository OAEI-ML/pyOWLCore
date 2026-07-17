"""Caller-owned interning factory and document-scoped anonymous builder."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TypeVar, cast

from .anonymous import canonical_document_scope, re_scope_anonymous
from .base import CanonicalSet, StructuralNode
from .canonical import canonical_bytes, encode_varint
from .dataranges import DataIntersectionOf, DataRange, DataUnionOf
from .expressions import ClassExpression, ObjectIntersectionOf, ObjectUnionOf
from .primitives import (
    IRI,
    RDF_PLAIN_LITERAL,
    AnnotationProperty,
    AnonymousIndividual,
    Class,
    DataProperty,
    Datatype,
    Entity,
    EntityKind,
    Literal,
    NamedIndividual,
    ObjectProperty,
)
from .properties import ObjectPropertyExpression, inverse_property
from .provenance import LexicalProvenance, LexicalProvenanceBuilder, RescopeRecord
from .registry import constructor_spec

T = TypeVar("T", bound=StructuralNode)
_PROGRAMMATIC_KEY_DOMAIN = b"pyowl-core:programmatic-anonymous:v1\x00"


@dataclass(frozen=True, slots=True)
class FactoryStats:
    retained_values: int
    hits: int
    misses: int

    def __post_init__(self) -> None:
        for field, value in (
            ("retained_values", self.retained_values),
            ("hits", self.hits),
            ("misses", self.misses),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a nonnegative integer")


class OWLFactory:
    """Optional strong interner; identity/equality never depends on this object."""

    __slots__ = ("_hits", "_misses", "_values")

    def __init__(self) -> None:
        self._values: dict[bytes, StructuralNode] = {}
        self._hits = 0
        self._misses = 0

    def intern(self, value: T) -> T:
        if not isinstance(value, StructuralNode):
            raise TypeError("value must be a StructuralNode")
        key = canonical_bytes(value)
        retained = self._values.get(key)
        if retained is not None:
            self._hits += 1
            return cast(T, retained)
        self._misses += 1
        self._values[key] = value
        return value

    def create(self, constructor: type[T], /, *args: object, **kwargs: object) -> T:
        if not isinstance(constructor, type) or not issubclass(constructor, StructuralNode):
            raise TypeError("constructor must be a registered StructuralNode type")
        constructor_spec(constructor)
        return self.intern(constructor(*args, **kwargs))

    def iri(self, value: IRI | str) -> IRI:
        return self.intern(value if isinstance(value, IRI) else IRI(value))

    def entity(self, kind: EntityKind, iri: IRI | str) -> Entity:
        constructors = {
            EntityKind.CLASS: Class,
            EntityKind.DATATYPE: Datatype,
            EntityKind.OBJECT_PROPERTY: ObjectProperty,
            EntityKind.DATA_PROPERTY: DataProperty,
            EntityKind.ANNOTATION_PROPERTY: AnnotationProperty,
            EntityKind.NAMED_INDIVIDUAL: NamedIndividual,
        }
        if not isinstance(kind, EntityKind):
            raise TypeError("kind must be EntityKind")
        return self.intern(constructors[kind](self.iri(iri)))

    def class_(self, iri: IRI | str) -> Class:
        return self.intern(Class(self.iri(iri)))

    def datatype(self, iri: IRI | str) -> Datatype:
        return self.intern(Datatype(self.iri(iri)))

    def object_property(self, iri: IRI | str) -> ObjectProperty:
        return self.intern(ObjectProperty(self.iri(iri)))

    def data_property(self, iri: IRI | str) -> DataProperty:
        return self.intern(DataProperty(self.iri(iri)))

    def annotation_property(self, iri: IRI | str) -> AnnotationProperty:
        return self.intern(AnnotationProperty(self.iri(iri)))

    def named_individual(self, iri: IRI | str) -> NamedIndividual:
        return self.intern(NamedIndividual(self.iri(iri)))

    def literal(
        self,
        lexical_form: str,
        datatype: Datatype = RDF_PLAIN_LITERAL,
        language: str | None = None,
    ) -> Literal:
        return self.intern(Literal(lexical_form, datatype, language))

    def inverse(self, property: ObjectPropertyExpression) -> ObjectPropertyExpression:
        return self.intern(inverse_property(property))

    def object_intersection(
        self,
        *operands: ClassExpression,
    ) -> ObjectIntersectionOf:
        return self.intern(ObjectIntersectionOf(CanonicalSet(operands)))

    def object_union(self, *operands: ClassExpression) -> ObjectUnionOf:
        return self.intern(ObjectUnionOf(CanonicalSet(operands)))

    def data_intersection(self, *operands: DataRange) -> DataIntersectionOf:
        return self.intern(DataIntersectionOf(CanonicalSet(operands)))

    def data_union(self, *operands: DataRange) -> DataUnionOf:
        return self.intern(DataUnionOf(CanonicalSet(operands)))

    def stats(self) -> FactoryStats:
        return FactoryStats(len(self._values), self._hits, self._misses)

    def clear(self) -> None:
        self._values.clear()


class DocumentBuilder:
    """Model-only scope for deterministic programmatic anonymous individuals."""

    __slots__ = (
        "_anonymous",
        "_counter",
        "_factory",
        "_provenance",
        "_scope",
    )

    def __init__(
        self,
        document_key: IRI | str | bytes,
        *,
        factory: OWLFactory | None = None,
    ) -> None:
        self._scope = canonical_document_scope(document_key)
        self._factory = factory or OWLFactory()
        self._counter = 0
        self._anonymous: dict[bytes, AnonymousIndividual] = {}
        self._provenance = LexicalProvenanceBuilder()

    @property
    def document_scope(self) -> bytes:
        return self._scope

    @property
    def factory(self) -> OWLFactory:
        return self._factory

    def anonymous(self, key: str | bytes | None = None) -> AnonymousIndividual:
        if key is None:
            token = b"counter:" + encode_varint(self._counter)
            self._counter += 1
        elif isinstance(key, str):
            if not key:
                raise ValueError("anonymous key must be nonempty")
            try:
                token = b"text:" + key.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("anonymous key must contain Unicode scalar values") from error
        elif isinstance(key, bytes):
            if not key:
                raise ValueError("anonymous key must be nonempty")
            token = b"bytes:" + key
        else:
            raise TypeError("anonymous key must be str, bytes, or None")
        local_key = hashlib.sha256(_PROGRAMMATIC_KEY_DOMAIN + self._scope + token).digest()
        retained = self._anonymous.get(local_key)
        if retained is None:
            retained = self._factory.intern(AnonymousIndividual(self._scope, local_key))
            self._anonymous[local_key] = retained
        return retained

    def re_scope(
        self,
        individual: AnonymousIndividual,
    ) -> tuple[AnonymousIndividual, RescopeRecord]:
        moved, record = re_scope_anonymous(individual, self._scope)
        return self._factory.intern(moved), record

    def attach_lexical(self, value: StructuralNode, kind: str, spelling: str) -> None:
        self._provenance.attach(value, kind, spelling)

    def freeze_lexical_provenance(self) -> LexicalProvenance:
        return self._provenance.freeze()


__all__ = ["DocumentBuilder", "FactoryStats", "OWLFactory"]
