"""Explicitly namespaced SWRL/DL-safe-rule structural extension values."""

from __future__ import annotations

from dataclasses import dataclass, field

from .annotations import Annotation, normalize_annotations
from .base import CanonicalSet, StructuralNode, canonical_set, require_node, structural_tuple
from .dataranges import DATA_RANGE_TYPES, DataRange
from .expressions import CLASS_EXPRESSION_TYPES, ClassExpression
from .primitives import (
    IRI,
    AnonymousIndividual,
    DataProperty,
    Individual,
    Literal,
    NamedIndividual,
    ObjectProperty,
)
from .properties import ObjectInverseOf, ObjectPropertyExpression


@dataclass(frozen=True, slots=True, eq=False)
class Variable(StructuralNode):
    iri: IRI

    def __post_init__(self) -> None:
        require_node(self.iri, IRI, "Variable.iri")


IndividualArgument = Individual | Variable
DataArgument = Literal | Variable
INDIVIDUAL_ARGUMENT_TYPES = (NamedIndividual, AnonymousIndividual, Variable)
DATA_ARGUMENT_TYPES = (Literal, Variable)


@dataclass(frozen=True, slots=True, eq=False)
class ClassAtom(StructuralNode):
    predicate: ClassExpression
    argument: IndividualArgument

    def __post_init__(self) -> None:
        require_node(self.predicate, CLASS_EXPRESSION_TYPES, "ClassAtom.predicate")
        require_node(self.argument, INDIVIDUAL_ARGUMENT_TYPES, "ClassAtom.argument")


@dataclass(frozen=True, slots=True, eq=False)
class DataRangeAtom(StructuralNode):
    predicate: DataRange
    argument: DataArgument

    def __post_init__(self) -> None:
        require_node(self.predicate, DATA_RANGE_TYPES, "DataRangeAtom.predicate")
        require_node(self.argument, DATA_ARGUMENT_TYPES, "DataRangeAtom.argument")


@dataclass(frozen=True, slots=True, eq=False)
class ObjectPropertyAtom(StructuralNode):
    predicate: ObjectPropertyExpression
    source: IndividualArgument
    target: IndividualArgument

    def __post_init__(self) -> None:
        require_node(
            self.predicate,
            (ObjectProperty, ObjectInverseOf),
            "ObjectPropertyAtom.predicate",
        )
        require_node(self.source, INDIVIDUAL_ARGUMENT_TYPES, "ObjectPropertyAtom.source")
        require_node(self.target, INDIVIDUAL_ARGUMENT_TYPES, "ObjectPropertyAtom.target")


@dataclass(frozen=True, slots=True, eq=False)
class DataPropertyAtom(StructuralNode):
    predicate: DataProperty
    source: IndividualArgument
    target: DataArgument

    def __post_init__(self) -> None:
        require_node(self.predicate, DataProperty, "DataPropertyAtom.predicate")
        require_node(self.source, INDIVIDUAL_ARGUMENT_TYPES, "DataPropertyAtom.source")
        require_node(self.target, DATA_ARGUMENT_TYPES, "DataPropertyAtom.target")


@dataclass(frozen=True, slots=True, eq=False)
class BuiltInAtom(StructuralNode):
    predicate: IRI
    arguments: tuple[DataArgument, ...]

    def __post_init__(self) -> None:
        require_node(self.predicate, IRI, "BuiltInAtom.predicate")
        object.__setattr__(
            self,
            "arguments",
            structural_tuple(self.arguments, DATA_ARGUMENT_TYPES, "BuiltInAtom.arguments"),
        )


@dataclass(frozen=True, slots=True, eq=False)
class SameIndividualAtom(StructuralNode):
    first: IndividualArgument
    second: IndividualArgument

    def __post_init__(self) -> None:
        _individual_pair(self)


@dataclass(frozen=True, slots=True, eq=False)
class DifferentIndividualsAtom(StructuralNode):
    first: IndividualArgument
    second: IndividualArgument

    def __post_init__(self) -> None:
        _individual_pair(self)


def _individual_pair(value: SameIndividualAtom | DifferentIndividualsAtom) -> None:
    require_node(value.first, INDIVIDUAL_ARGUMENT_TYPES, f"{type(value).__name__}.first")
    require_node(value.second, INDIVIDUAL_ARGUMENT_TYPES, f"{type(value).__name__}.second")


Atom = (
    ClassAtom
    | DataRangeAtom
    | ObjectPropertyAtom
    | DataPropertyAtom
    | BuiltInAtom
    | SameIndividualAtom
    | DifferentIndividualsAtom
)
ATOM_TYPES = (
    ClassAtom,
    DataRangeAtom,
    ObjectPropertyAtom,
    DataPropertyAtom,
    BuiltInAtom,
    SameIndividualAtom,
    DifferentIndividualsAtom,
)


@dataclass(frozen=True, slots=True, eq=False)
class SWRLRule(StructuralNode):
    body: CanonicalSet[Atom]
    head: CanonicalSet[Atom]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", canonical_set(self.body, ATOM_TYPES, "SWRLRule.body"))
        object.__setattr__(self, "head", canonical_set(self.head, ATOM_TYPES, "SWRLRule.head"))
        object.__setattr__(self, "annotations", normalize_annotations(self.annotations))


ExtensionComponent = SWRLRule
EXTENSION_COMPONENT_TYPES = (SWRLRule,)


__all__ = [
    "ATOM_TYPES",
    "DATA_ARGUMENT_TYPES",
    "EXTENSION_COMPONENT_TYPES",
    "INDIVIDUAL_ARGUMENT_TYPES",
    "Atom",
    "BuiltInAtom",
    "ClassAtom",
    "DataArgument",
    "DataPropertyAtom",
    "DataRangeAtom",
    "DifferentIndividualsAtom",
    "ExtensionComponent",
    "IndividualArgument",
    "ObjectPropertyAtom",
    "SWRLRule",
    "SameIndividualAtom",
    "Variable",
]
