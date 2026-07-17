"""Complete OWL 2 data-range structural values."""

from __future__ import annotations

from dataclasses import dataclass

from .base import CanonicalSet, StructuralNode, canonical_set, require_node
from .primitives import IRI, Datatype, Literal


@dataclass(frozen=True, slots=True, eq=False)
class FacetRestriction(StructuralNode):
    facet: IRI
    value: Literal

    def __post_init__(self) -> None:
        require_node(self.facet, IRI, "FacetRestriction.facet")
        require_node(self.value, Literal, "FacetRestriction.value")


@dataclass(frozen=True, slots=True, eq=False)
class DataIntersectionOf(StructuralNode):
    operands: CanonicalSet[DataRange]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operands",
            canonical_set(
                self.operands,
                DATA_RANGE_TYPES,
                "DataIntersectionOf.operands",
                minimum=2,
                flatten=DataIntersectionOf,
            ),
        )


@dataclass(frozen=True, slots=True, eq=False)
class DataUnionOf(StructuralNode):
    operands: CanonicalSet[DataRange]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operands",
            canonical_set(
                self.operands,
                DATA_RANGE_TYPES,
                "DataUnionOf.operands",
                minimum=2,
                flatten=DataUnionOf,
            ),
        )


@dataclass(frozen=True, slots=True, eq=False)
class DataComplementOf(StructuralNode):
    operand: DataRange

    def __post_init__(self) -> None:
        require_node(self.operand, DATA_RANGE_TYPES, "DataComplementOf.operand")


@dataclass(frozen=True, slots=True, eq=False)
class DataOneOf(StructuralNode):
    values: CanonicalSet[Literal]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            canonical_set(self.values, Literal, "DataOneOf.values", minimum=1),
        )


@dataclass(frozen=True, slots=True, eq=False)
class DatatypeRestriction(StructuralNode):
    datatype: Datatype
    restrictions: CanonicalSet[FacetRestriction]

    def __post_init__(self) -> None:
        require_node(self.datatype, Datatype, "DatatypeRestriction.datatype")
        object.__setattr__(
            self,
            "restrictions",
            canonical_set(
                self.restrictions,
                FacetRestriction,
                "DatatypeRestriction.restrictions",
                minimum=1,
            ),
        )


DataRange = (
    Datatype | DataIntersectionOf | DataUnionOf | DataComplementOf | DataOneOf | DatatypeRestriction
)
DATA_RANGE_TYPES: tuple[type[StructuralNode], ...] = (
    Datatype,
    DataIntersectionOf,
    DataUnionOf,
    DataComplementOf,
    DataOneOf,
    DatatypeRestriction,
)


__all__ = [
    "DATA_RANGE_TYPES",
    "DataComplementOf",
    "DataIntersectionOf",
    "DataOneOf",
    "DataRange",
    "DataUnionOf",
    "DatatypeRestriction",
    "FacetRestriction",
]
