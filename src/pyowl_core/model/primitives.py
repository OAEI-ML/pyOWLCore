"""IRI, entity, individual, and literal structural values."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import cast

from pyowl_core.exceptions import (
    InvalidIRIError,
    InvalidLiteralError,
    StructuralConstraintError,
)

from .base import StructuralNode, require_node, require_text

_ABSOLUTE_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_FORBIDDEN_IRI = frozenset('<>"{}|\\^`')
_LANGUAGE = re.compile(
    r"^(?:"
    r"(?:[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}|[A-Za-z]{4}|[A-Za-z]{5,8})"
    r"(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(?:-[0-9A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*"
    r"(?:-x(?:-[A-Za-z0-9]{1,8})+)?"
    r"|x(?:-[A-Za-z0-9]{1,8})+"
    r"|(?:en-GB-oed|i-ami|i-bnn|i-default|i-enochian|i-hak|i-klingon|i-lux|"
    r"i-mingo|i-navajo|i-pwn|i-tao|i-tay|i-tsu|sgn-BE-FR|sgn-BE-NL|sgn-CH-DE)"
    r"|(?:art-lojban|cel-gaulish|no-bok|no-nyn|zh-guoyu|zh-hakka|zh-min|"
    r"zh-min-nan|zh-xiang)"
    r")$",
    re.IGNORECASE,
)
_GRANDFATHERED_LANGUAGES = frozenset(
    {
        "art-lojban",
        "cel-gaulish",
        "en-gb-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "no-bok",
        "no-nyn",
        "sgn-be-fr",
        "sgn-be-nl",
        "sgn-ch-de",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    }
)

RDF_PLAIN_LITERAL_IRI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral"
RDF_LANG_STRING_IRI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
RDFS_LITERAL_IRI = "http://www.w3.org/2000/01/rdf-schema#Literal"
XSD_STRING_IRI = "http://www.w3.org/2001/XMLSchema#string"


@dataclass(frozen=True, slots=True, eq=False)
class IRI(StructuralNode):
    value: str

    def __post_init__(self) -> None:
        try:
            require_text(self.value, "IRI.value", nonempty=True)
        except (TypeError, StructuralConstraintError) as error:
            raise InvalidIRIError(str(error)) from error
        if not _ABSOLUTE_IRI.match(self.value):
            raise InvalidIRIError("IRI must be absolute and include a valid scheme")
        for character in self.value:
            codepoint = ord(character)
            if character in _FORBIDDEN_IRI or codepoint <= 0x20:
                raise InvalidIRIError("IRI contains a forbidden character")
            if (
                0x7F <= codepoint <= 0x9F
                or 0xD800 <= codepoint <= 0xDFFF
                or 0xFDD0 <= codepoint <= 0xFDEF
                or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
            ):
                raise InvalidIRIError("IRI contains a forbidden Unicode scalar")
        index = 0
        while True:
            index = self.value.find("%", index)
            if index < 0:
                break
            if _PERCENT_ESCAPE.match(self.value, index) is None:
                raise InvalidIRIError("IRI contains an invalid percent escape")
            index += 3

    def __str__(self) -> str:
        return self.value


class EntityKind(str, Enum):
    CLASS = "class"
    DATATYPE = "datatype"
    OBJECT_PROPERTY = "object_property"
    DATA_PROPERTY = "data_property"
    ANNOTATION_PROPERTY = "annotation_property"
    NAMED_INDIVIDUAL = "named_individual"


class _EntityMeta(type):
    """Make the public generic constructor return its typed entity subclass."""

    def __call__(cls, *args: object, **kwargs: object) -> Entity:
        if cls is Entity:
            if len(args) == 2 and not kwargs:
                kind, iri = args
            elif len(args) == 1 and set(kwargs) == {"iri"}:
                kind, iri = args[0], kwargs["iri"]
            elif not args and set(kwargs) == {"kind", "iri"}:
                kind, iri = kwargs["kind"], kwargs["iri"]
            else:
                return cast(Entity, super().__call__(*args, **kwargs))
            constructor = _ENTITY_CLASSES.get(kind)
            if constructor is not None:
                return constructor(cast(IRI, iri))
        return cast(Entity, super().__call__(*args, **kwargs))


@dataclass(frozen=True, slots=True, eq=False)
class Entity(StructuralNode, metaclass=_EntityMeta):
    kind: EntityKind
    iri: IRI

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EntityKind):
            raise TypeError("Entity.kind must be EntityKind")
        require_node(self.iri, IRI, "Entity.iri")


class Class(Entity):
    __slots__ = ()

    def __init__(self, iri: IRI) -> None:
        super().__init__(EntityKind.CLASS, iri)


class Datatype(Entity):
    __slots__ = ()

    def __init__(self, iri: IRI) -> None:
        super().__init__(EntityKind.DATATYPE, iri)


class ObjectProperty(Entity):
    __slots__ = ()

    def __init__(self, iri: IRI) -> None:
        super().__init__(EntityKind.OBJECT_PROPERTY, iri)


class DataProperty(Entity):
    __slots__ = ()

    def __init__(self, iri: IRI) -> None:
        super().__init__(EntityKind.DATA_PROPERTY, iri)


class AnnotationProperty(Entity):
    __slots__ = ()

    def __init__(self, iri: IRI) -> None:
        super().__init__(EntityKind.ANNOTATION_PROPERTY, iri)


class NamedIndividual(Entity):
    __slots__ = ()

    def __init__(self, iri: IRI) -> None:
        super().__init__(EntityKind.NAMED_INDIVIDUAL, iri)


_ENTITY_CLASSES: dict[object, Callable[[IRI], Entity]] = {
    EntityKind.CLASS: Class,
    EntityKind.DATATYPE: Datatype,
    EntityKind.OBJECT_PROPERTY: ObjectProperty,
    EntityKind.DATA_PROPERTY: DataProperty,
    EntityKind.ANNOTATION_PROPERTY: AnnotationProperty,
    EntityKind.NAMED_INDIVIDUAL: NamedIndividual,
}


@dataclass(frozen=True, slots=True, eq=False)
class AnonymousIndividual(StructuralNode):
    document_scope: bytes
    local_key: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.document_scope, bytes) or len(self.document_scope) != 32:
            raise StructuralConstraintError("document_scope must be exactly 32 bytes")
        if not isinstance(self.local_key, bytes) or not self.local_key:
            raise StructuralConstraintError("local_key must be nonempty bytes")


@dataclass(frozen=True, slots=True, eq=False)
class Literal(StructuralNode):
    lexical_form: str
    datatype: Datatype
    language: str | None = None

    def __post_init__(self) -> None:
        try:
            require_text(self.lexical_form, "Literal.lexical_form")
            self.lexical_form.encode("utf-8")
            require_node(self.datatype, Datatype, "Literal.datatype")
        except (TypeError, UnicodeEncodeError, StructuralConstraintError) as error:
            raise InvalidLiteralError(str(error)) from error
        language = self.language
        if language is not None:
            if not isinstance(language, str) or not _valid_language_tag(language):
                raise InvalidLiteralError("language must be a structurally valid BCP 47 tag")
            if self.datatype.iri.value != RDF_PLAIN_LITERAL_IRI:
                raise InvalidLiteralError("language is permitted only with rdf:PlainLiteral")
            object.__setattr__(self, "language", language.lower())
        if self.datatype.iri.value == RDF_LANG_STRING_IRI:
            raise InvalidLiteralError(
                "rdf:langString is mapped to rdf:PlainLiteral at the RDF/OWL boundary"
            )


def _valid_language_tag(language: str) -> bool:
    if _LANGUAGE.fullmatch(language) is None:
        return False
    lowered = language.lower()
    if lowered in _GRANDFATHERED_LANGUAGES or lowered.startswith("x-"):
        return True
    parts = lowered.split("-")
    index = 1
    if len(parts[0]) in {2, 3}:
        extlangs = 0
        while index < len(parts) and len(parts[index]) == 3 and parts[index].isalpha():
            extlangs += 1
            index += 1
            if extlangs == 3:
                break
    if index < len(parts) and len(parts[index]) == 4 and parts[index].isalpha():
        index += 1
    if index < len(parts) and (
        (len(parts[index]) == 2 and parts[index].isalpha())
        or (len(parts[index]) == 3 and parts[index].isdigit())
    ):
        index += 1
    variants: set[str] = set()
    while index < len(parts) and (
        5 <= len(parts[index]) <= 8 or (len(parts[index]) == 4 and parts[index][0].isdigit())
    ):
        if parts[index] in variants:
            return False
        variants.add(parts[index])
        index += 1
    singletons: set[str] = set()
    while index < len(parts) and len(parts[index]) == 1 and parts[index] != "x":
        if parts[index] in singletons:
            return False
        singletons.add(parts[index])
        index += 1
        while index < len(parts) and 2 <= len(parts[index]) <= 8:
            index += 1
    return True


OWL_THING = Class(IRI("http://www.w3.org/2002/07/owl#Thing"))
OWL_NOTHING = Class(IRI("http://www.w3.org/2002/07/owl#Nothing"))
OWL_TOP_OBJECT_PROPERTY = ObjectProperty(IRI("http://www.w3.org/2002/07/owl#topObjectProperty"))
OWL_BOTTOM_OBJECT_PROPERTY = ObjectProperty(
    IRI("http://www.w3.org/2002/07/owl#bottomObjectProperty")
)
OWL_TOP_DATA_PROPERTY = DataProperty(IRI("http://www.w3.org/2002/07/owl#topDataProperty"))
OWL_BOTTOM_DATA_PROPERTY = DataProperty(IRI("http://www.w3.org/2002/07/owl#bottomDataProperty"))
RDFS_LITERAL = Datatype(IRI(RDFS_LITERAL_IRI))
RDF_PLAIN_LITERAL = Datatype(IRI(RDF_PLAIN_LITERAL_IRI))
XSD_STRING = Datatype(IRI(XSD_STRING_IRI))

Individual = NamedIndividual | AnonymousIndividual


__all__ = [
    "IRI",
    "OWL_BOTTOM_DATA_PROPERTY",
    "OWL_BOTTOM_OBJECT_PROPERTY",
    "OWL_NOTHING",
    "OWL_THING",
    "OWL_TOP_DATA_PROPERTY",
    "OWL_TOP_OBJECT_PROPERTY",
    "RDFS_LITERAL",
    "RDFS_LITERAL_IRI",
    "RDF_LANG_STRING_IRI",
    "RDF_PLAIN_LITERAL",
    "RDF_PLAIN_LITERAL_IRI",
    "XSD_STRING",
    "XSD_STRING_IRI",
    "AnnotationProperty",
    "AnonymousIndividual",
    "Class",
    "DataProperty",
    "Datatype",
    "Entity",
    "EntityKind",
    "Individual",
    "Literal",
    "NamedIndividual",
    "ObjectProperty",
]
