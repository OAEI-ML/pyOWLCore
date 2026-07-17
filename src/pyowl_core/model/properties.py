"""OWL 2 object and data property expressions."""

from __future__ import annotations

from dataclasses import dataclass

from .base import StructuralNode, require_node, structural_tuple
from .primitives import DataProperty, ObjectProperty


@dataclass(frozen=True, slots=True, eq=False)
class ObjectInverseOf(StructuralNode):
    property: ObjectProperty

    def __post_init__(self) -> None:
        require_node(self.property, ObjectProperty, "ObjectInverseOf.property")


ObjectPropertyExpression = ObjectProperty | ObjectInverseOf
DataPropertyExpression = DataProperty


@dataclass(frozen=True, slots=True, eq=False)
class ObjectPropertyChain(StructuralNode):
    properties: tuple[ObjectPropertyExpression, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "properties",
            structural_tuple(
                self.properties,
                (ObjectProperty, ObjectInverseOf),
                "ObjectPropertyChain.properties",
                minimum=2,
            ),
        )


SubObjectPropertyExpression = ObjectPropertyExpression | ObjectPropertyChain


def inverse_property(expression: ObjectPropertyExpression) -> ObjectPropertyExpression:
    require_node(expression, (ObjectProperty, ObjectInverseOf), "expression")
    if isinstance(expression, ObjectInverseOf):
        return expression.property
    return ObjectInverseOf(expression)


__all__ = [
    "DataPropertyExpression",
    "ObjectInverseOf",
    "ObjectPropertyChain",
    "ObjectPropertyExpression",
    "SubObjectPropertyExpression",
    "inverse_property",
]
