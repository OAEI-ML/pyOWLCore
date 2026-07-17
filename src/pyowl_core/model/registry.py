"""Closed constructor registry used by visitors, canonical code, and coverage."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType

from pyowl_core.exceptions import StructuralConstraintError

from . import _tags
from .annotations import Annotation
from .axioms import (
    AnnotationAssertion,
    AnnotationPropertyDomain,
    AnnotationPropertyRange,
    AsymmetricObjectProperty,
    ClassAssertion,
    DataPropertyAssertion,
    DataPropertyDomain,
    DataPropertyRange,
    DatatypeDefinition,
    Declaration,
    DifferentIndividuals,
    DisjointClasses,
    DisjointDataProperties,
    DisjointObjectProperties,
    DisjointUnion,
    EquivalentClasses,
    EquivalentDataProperties,
    EquivalentObjectProperties,
    FunctionalDataProperty,
    FunctionalObjectProperty,
    HasKey,
    InverseFunctionalObjectProperty,
    InverseObjectProperties,
    IrreflexiveObjectProperty,
    NegativeDataPropertyAssertion,
    NegativeObjectPropertyAssertion,
    ObjectPropertyAssertion,
    ObjectPropertyDomain,
    ObjectPropertyRange,
    ReflexiveObjectProperty,
    SameIndividual,
    SubAnnotationPropertyOf,
    SubClassOf,
    SubDataPropertyOf,
    SubObjectPropertyOf,
    SymmetricObjectProperty,
    TransitiveObjectProperty,
)
from .base import StructuralNode
from .dataranges import (
    DataComplementOf,
    DataIntersectionOf,
    DataOneOf,
    DatatypeRestriction,
    DataUnionOf,
    FacetRestriction,
)
from .expressions import (
    DataAllValuesFrom,
    DataExactCardinality,
    DataHasValue,
    DataMaxCardinality,
    DataMinCardinality,
    DataSomeValuesFrom,
    ObjectAllValuesFrom,
    ObjectComplementOf,
    ObjectExactCardinality,
    ObjectHasSelf,
    ObjectHasValue,
    ObjectIntersectionOf,
    ObjectMaxCardinality,
    ObjectMinCardinality,
    ObjectOneOf,
    ObjectSomeValuesFrom,
    ObjectUnionOf,
)
from .primitives import IRI, AnonymousIndividual, Entity, Literal
from .properties import ObjectInverseOf, ObjectPropertyChain
from .swrl import (
    BuiltInAtom,
    ClassAtom,
    DataPropertyAtom,
    DataRangeAtom,
    DifferentIndividualsAtom,
    ObjectPropertyAtom,
    SameIndividualAtom,
    SWRLRule,
    Variable,
)


@dataclass(frozen=True, slots=True)
class ConstructorSpec:
    constructor: type[StructuralNode]
    tag_name: str
    tag: int
    fields: tuple[str, ...]
    category: str
    production: str

    def __post_init__(self) -> None:
        if not isinstance(self.constructor, type) or not issubclass(
            self.constructor, StructuralNode
        ):
            raise TypeError("constructor must be a StructuralNode type")
        if not isinstance(self.tag_name, str) or not self.tag_name:
            raise ValueError("tag_name must be a nonempty string")
        if isinstance(self.tag, bool) or not isinstance(self.tag, int) or self.tag < 1:
            raise ValueError("tag must be a positive integer")
        fields = tuple(self.fields)
        if not all(isinstance(field, str) and field for field in fields):
            raise TypeError("fields must contain nonempty strings")
        if not isinstance(self.category, str) or not self.category:
            raise ValueError("category must be a nonempty string")
        if not isinstance(self.production, str) or not self.production:
            raise ValueError("production must be a nonempty string")
        object.__setattr__(self, "fields", fields)


def _upper_snake(name: str) -> str:
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).upper()


def _spec(
    constructor: type[StructuralNode],
    category: str,
    production: str | None = None,
) -> ConstructorSpec:
    tag_name = _upper_snake(constructor.__name__)
    tag = getattr(_tags, tag_name, None)
    if not isinstance(tag, int):
        raise StructuralConstraintError(
            f"constructor {constructor.__name__} has no model schema tag {tag_name}"
        )
    return ConstructorSpec(
        constructor=constructor,
        tag_name=tag_name,
        tag=tag,
        fields=tuple(field.name for field in fields(constructor)),  # type: ignore[arg-type]
        category=category,
        production=production or constructor.__name__,
    )


CONSTRUCTOR_SPECS = (
    _spec(IRI, "primitive"),
    _spec(Entity, "primitive", "Entity and typed entity productions"),
    _spec(AnonymousIndividual, "primitive"),
    _spec(Literal, "primitive"),
    _spec(Annotation, "annotation"),
    _spec(ObjectInverseOf, "property_expression"),
    _spec(ObjectPropertyChain, "sub_object_property_expression"),
    _spec(FacetRestriction, "facet_restriction"),
    _spec(DataIntersectionOf, "data_range"),
    _spec(DataUnionOf, "data_range"),
    _spec(DataComplementOf, "data_range"),
    _spec(DataOneOf, "data_range"),
    _spec(DatatypeRestriction, "data_range"),
    _spec(ObjectIntersectionOf, "class_expression"),
    _spec(ObjectUnionOf, "class_expression"),
    _spec(ObjectComplementOf, "class_expression"),
    _spec(ObjectOneOf, "class_expression"),
    _spec(ObjectSomeValuesFrom, "class_expression"),
    _spec(ObjectAllValuesFrom, "class_expression"),
    _spec(ObjectHasValue, "class_expression"),
    _spec(ObjectHasSelf, "class_expression"),
    _spec(ObjectMinCardinality, "class_expression"),
    _spec(ObjectMaxCardinality, "class_expression"),
    _spec(ObjectExactCardinality, "class_expression"),
    _spec(DataSomeValuesFrom, "class_expression"),
    _spec(DataAllValuesFrom, "class_expression"),
    _spec(DataHasValue, "class_expression"),
    _spec(DataMinCardinality, "class_expression"),
    _spec(DataMaxCardinality, "class_expression"),
    _spec(DataExactCardinality, "class_expression"),
    _spec(Declaration, "declaration_axiom"),
    _spec(SubClassOf, "logical_axiom"),
    _spec(EquivalentClasses, "logical_axiom"),
    _spec(DisjointClasses, "logical_axiom"),
    _spec(DisjointUnion, "logical_axiom"),
    _spec(SubObjectPropertyOf, "logical_axiom"),
    _spec(EquivalentObjectProperties, "logical_axiom"),
    _spec(DisjointObjectProperties, "logical_axiom"),
    _spec(InverseObjectProperties, "logical_axiom"),
    _spec(ObjectPropertyDomain, "logical_axiom"),
    _spec(ObjectPropertyRange, "logical_axiom"),
    _spec(FunctionalObjectProperty, "logical_axiom"),
    _spec(InverseFunctionalObjectProperty, "logical_axiom"),
    _spec(ReflexiveObjectProperty, "logical_axiom"),
    _spec(IrreflexiveObjectProperty, "logical_axiom"),
    _spec(SymmetricObjectProperty, "logical_axiom"),
    _spec(AsymmetricObjectProperty, "logical_axiom"),
    _spec(TransitiveObjectProperty, "logical_axiom"),
    _spec(SubDataPropertyOf, "logical_axiom"),
    _spec(EquivalentDataProperties, "logical_axiom"),
    _spec(DisjointDataProperties, "logical_axiom"),
    _spec(DataPropertyDomain, "logical_axiom"),
    _spec(DataPropertyRange, "logical_axiom"),
    _spec(FunctionalDataProperty, "logical_axiom"),
    _spec(DatatypeDefinition, "logical_axiom"),
    _spec(HasKey, "logical_axiom"),
    _spec(SameIndividual, "logical_axiom"),
    _spec(DifferentIndividuals, "logical_axiom"),
    _spec(ClassAssertion, "logical_axiom"),
    _spec(ObjectPropertyAssertion, "logical_axiom"),
    _spec(NegativeObjectPropertyAssertion, "logical_axiom"),
    _spec(DataPropertyAssertion, "logical_axiom"),
    _spec(NegativeDataPropertyAssertion, "logical_axiom"),
    _spec(AnnotationAssertion, "annotation_axiom"),
    _spec(SubAnnotationPropertyOf, "annotation_axiom"),
    _spec(AnnotationPropertyDomain, "annotation_axiom"),
    _spec(AnnotationPropertyRange, "annotation_axiom"),
    _spec(Variable, "swrl_extension", "SWRL Variable"),
    _spec(ClassAtom, "swrl_extension", "SWRL ClassAtom"),
    _spec(DataRangeAtom, "swrl_extension", "SWRL DataRangeAtom"),
    _spec(ObjectPropertyAtom, "swrl_extension", "SWRL IndividualPropertyAtom"),
    _spec(DataPropertyAtom, "swrl_extension", "SWRL DatavaluedPropertyAtom"),
    _spec(BuiltInAtom, "swrl_extension", "SWRL BuiltinAtom"),
    _spec(SameIndividualAtom, "swrl_extension", "SWRL SameIndividualAtom"),
    _spec(
        DifferentIndividualsAtom,
        "swrl_extension",
        "SWRL DifferentIndividualsAtom",
    ),
    _spec(SWRLRule, "swrl_extension", "SWRL Imp"),
)

SPEC_BY_TYPE: Mapping[type[StructuralNode], ConstructorSpec] = MappingProxyType(
    {spec.constructor: spec for spec in CONSTRUCTOR_SPECS}
)
SPEC_BY_TAG: Mapping[int, ConstructorSpec] = MappingProxyType(
    {spec.tag: spec for spec in CONSTRUCTOR_SPECS}
)
MODEL_CONSTRUCTORS = tuple(spec.constructor for spec in CONSTRUCTOR_SPECS)

if len(SPEC_BY_TYPE) != len(CONSTRUCTOR_SPECS):
    raise StructuralConstraintError("duplicate constructor in model registry")
if len(SPEC_BY_TAG) != len(CONSTRUCTOR_SPECS):
    raise StructuralConstraintError("duplicate model schema tag in registry")


def constructor_spec(value: StructuralNode | type[StructuralNode]) -> ConstructorSpec:
    constructor = value if isinstance(value, type) else type(value)
    if issubclass(constructor, Entity):
        constructor = Entity
    try:
        return SPEC_BY_TYPE[constructor]
    except KeyError as error:
        raise StructuralConstraintError(
            f"unknown structural constructor: {constructor.__name__}"
        ) from error


__all__ = [
    "CONSTRUCTOR_SPECS",
    "MODEL_CONSTRUCTORS",
    "SPEC_BY_TAG",
    "SPEC_BY_TYPE",
    "ConstructorSpec",
    "constructor_spec",
]
