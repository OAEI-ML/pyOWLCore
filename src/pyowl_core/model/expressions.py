"""Complete OWL 2 class-expression structural values."""

from __future__ import annotations

from dataclasses import dataclass

from .base import (
    CanonicalSet,
    StructuralNode,
    canonical_set,
    require_node,
    require_nonnegative_integer,
    structural_tuple,
)
from .dataranges import DATA_RANGE_TYPES, DataRange
from .primitives import (
    AnonymousIndividual,
    Class,
    DataProperty,
    Individual,
    Literal,
    NamedIndividual,
    ObjectProperty,
)
from .properties import ObjectInverseOf, ObjectPropertyExpression


@dataclass(frozen=True, slots=True, eq=False)
class ObjectIntersectionOf(StructuralNode):
    operands: CanonicalSet[ClassExpression]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operands",
            canonical_set(
                self.operands,
                CLASS_EXPRESSION_TYPES,
                "ObjectIntersectionOf.operands",
                minimum=2,
                flatten=ObjectIntersectionOf,
            ),
        )


@dataclass(frozen=True, slots=True, eq=False)
class ObjectUnionOf(StructuralNode):
    operands: CanonicalSet[ClassExpression]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operands",
            canonical_set(
                self.operands,
                CLASS_EXPRESSION_TYPES,
                "ObjectUnionOf.operands",
                minimum=2,
                flatten=ObjectUnionOf,
            ),
        )


@dataclass(frozen=True, slots=True, eq=False)
class ObjectComplementOf(StructuralNode):
    operand: ClassExpression

    def __post_init__(self) -> None:
        require_node(self.operand, CLASS_EXPRESSION_TYPES, "ObjectComplementOf.operand")


@dataclass(frozen=True, slots=True, eq=False)
class ObjectOneOf(StructuralNode):
    individuals: CanonicalSet[Individual]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "individuals",
            canonical_set(
                self.individuals,
                (NamedIndividual, AnonymousIndividual),
                "ObjectOneOf.individuals",
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True, eq=False)
class ObjectSomeValuesFrom(StructuralNode):
    property: ObjectPropertyExpression
    filler: ClassExpression

    def __post_init__(self) -> None:
        _object_restriction(self.property, self.filler, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class ObjectAllValuesFrom(StructuralNode):
    property: ObjectPropertyExpression
    filler: ClassExpression

    def __post_init__(self) -> None:
        _object_restriction(self.property, self.filler, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class ObjectHasValue(StructuralNode):
    property: ObjectPropertyExpression
    value: Individual

    def __post_init__(self) -> None:
        require_node(self.property, OBJECT_PROPERTY_EXPRESSION_TYPES, "ObjectHasValue.property")
        require_node(
            self.value,
            (NamedIndividual, AnonymousIndividual),
            "ObjectHasValue.value",
        )


@dataclass(frozen=True, slots=True, eq=False)
class ObjectHasSelf(StructuralNode):
    property: ObjectPropertyExpression

    def __post_init__(self) -> None:
        require_node(self.property, OBJECT_PROPERTY_EXPRESSION_TYPES, "ObjectHasSelf.property")


@dataclass(frozen=True, slots=True, eq=False)
class ObjectMinCardinality(StructuralNode):
    cardinality: int
    property: ObjectPropertyExpression
    filler: ClassExpression

    def __post_init__(self) -> None:
        _object_cardinality(self.cardinality, self.property, self.filler, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class ObjectMaxCardinality(StructuralNode):
    cardinality: int
    property: ObjectPropertyExpression
    filler: ClassExpression

    def __post_init__(self) -> None:
        _object_cardinality(self.cardinality, self.property, self.filler, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class ObjectExactCardinality(StructuralNode):
    cardinality: int
    property: ObjectPropertyExpression
    filler: ClassExpression

    def __post_init__(self) -> None:
        _object_cardinality(self.cardinality, self.property, self.filler, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class DataSomeValuesFrom(StructuralNode):
    properties: tuple[DataProperty, ...]
    filler: DataRange

    def __post_init__(self) -> None:
        _data_quantifier(self, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class DataAllValuesFrom(StructuralNode):
    properties: tuple[DataProperty, ...]
    filler: DataRange

    def __post_init__(self) -> None:
        _data_quantifier(self, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class DataHasValue(StructuralNode):
    property: DataProperty
    value: Literal

    def __post_init__(self) -> None:
        require_node(self.property, DataProperty, "DataHasValue.property")
        require_node(self.value, Literal, "DataHasValue.value")


@dataclass(frozen=True, slots=True, eq=False)
class DataMinCardinality(StructuralNode):
    cardinality: int
    property: DataProperty
    filler: DataRange

    def __post_init__(self) -> None:
        _data_cardinality(self.cardinality, self.property, self.filler, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class DataMaxCardinality(StructuralNode):
    cardinality: int
    property: DataProperty
    filler: DataRange

    def __post_init__(self) -> None:
        _data_cardinality(self.cardinality, self.property, self.filler, type(self).__name__)


@dataclass(frozen=True, slots=True, eq=False)
class DataExactCardinality(StructuralNode):
    cardinality: int
    property: DataProperty
    filler: DataRange

    def __post_init__(self) -> None:
        _data_cardinality(self.cardinality, self.property, self.filler, type(self).__name__)


ClassExpression = (
    Class
    | ObjectIntersectionOf
    | ObjectUnionOf
    | ObjectComplementOf
    | ObjectOneOf
    | ObjectSomeValuesFrom
    | ObjectAllValuesFrom
    | ObjectHasValue
    | ObjectHasSelf
    | ObjectMinCardinality
    | ObjectMaxCardinality
    | ObjectExactCardinality
    | DataSomeValuesFrom
    | DataAllValuesFrom
    | DataHasValue
    | DataMinCardinality
    | DataMaxCardinality
    | DataExactCardinality
)
CLASS_EXPRESSION_TYPES: tuple[type[StructuralNode], ...] = (
    Class,
    ObjectIntersectionOf,
    ObjectUnionOf,
    ObjectComplementOf,
    ObjectOneOf,
    ObjectSomeValuesFrom,
    ObjectAllValuesFrom,
    ObjectHasValue,
    ObjectHasSelf,
    ObjectMinCardinality,
    ObjectMaxCardinality,
    ObjectExactCardinality,
    DataSomeValuesFrom,
    DataAllValuesFrom,
    DataHasValue,
    DataMinCardinality,
    DataMaxCardinality,
    DataExactCardinality,
)
OBJECT_PROPERTY_EXPRESSION_TYPES = (ObjectProperty, ObjectInverseOf)


def _object_restriction(
    property: object,
    filler: object,
    constructor: str,
) -> None:
    require_node(property, OBJECT_PROPERTY_EXPRESSION_TYPES, f"{constructor}.property")
    require_node(filler, CLASS_EXPRESSION_TYPES, f"{constructor}.filler")


def _object_cardinality(
    cardinality: object,
    property: object,
    filler: object,
    constructor: str,
) -> None:
    require_nonnegative_integer(cardinality, f"{constructor}.cardinality")
    _object_restriction(property, filler, constructor)


def _data_quantifier(value: DataSomeValuesFrom | DataAllValuesFrom, constructor: str) -> None:
    properties = structural_tuple(
        value.properties,
        DataProperty,
        f"{constructor}.properties",
        minimum=1,
    )
    object.__setattr__(value, "properties", properties)
    require_node(value.filler, DATA_RANGE_TYPES, f"{constructor}.filler")


def _data_cardinality(
    cardinality: object,
    property: object,
    filler: object,
    constructor: str,
) -> None:
    require_nonnegative_integer(cardinality, f"{constructor}.cardinality")
    require_node(property, DataProperty, f"{constructor}.property")
    require_node(filler, DATA_RANGE_TYPES, f"{constructor}.filler")


__all__ = [
    "CLASS_EXPRESSION_TYPES",
    "ClassExpression",
    "DataAllValuesFrom",
    "DataExactCardinality",
    "DataHasValue",
    "DataMaxCardinality",
    "DataMinCardinality",
    "DataSomeValuesFrom",
    "ObjectAllValuesFrom",
    "ObjectComplementOf",
    "ObjectExactCardinality",
    "ObjectHasSelf",
    "ObjectHasValue",
    "ObjectIntersectionOf",
    "ObjectMaxCardinality",
    "ObjectMinCardinality",
    "ObjectOneOf",
    "ObjectSomeValuesFrom",
    "ObjectUnionOf",
]
