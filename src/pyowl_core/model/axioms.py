"""Complete OWL 2 axiom structural values and closed category unions."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyowl_core.exceptions import StructuralConstraintError

from .annotations import (
    Annotation,
    AnnotationSubject,
    AnnotationValue,
    normalize_annotations,
)
from .base import CanonicalSet, StructuralNode, canonical_set, require_node
from .dataranges import DATA_RANGE_TYPES, DataRange
from .expressions import CLASS_EXPRESSION_TYPES, ClassExpression
from .primitives import (
    IRI,
    AnnotationProperty,
    AnonymousIndividual,
    Class,
    DataProperty,
    Datatype,
    Entity,
    Individual,
    Literal,
    NamedIndividual,
    ObjectProperty,
)
from .properties import (
    ObjectInverseOf,
    ObjectPropertyChain,
    ObjectPropertyExpression,
    SubObjectPropertyExpression,
)

OBJECT_PROPERTY_EXPRESSION_TYPES = (ObjectProperty, ObjectInverseOf)
SUB_OBJECT_PROPERTY_EXPRESSION_TYPES = (
    ObjectProperty,
    ObjectInverseOf,
    ObjectPropertyChain,
)
INDIVIDUAL_TYPES = (NamedIndividual, AnonymousIndividual)


class AxiomNode(StructuralNode):
    __slots__ = ()


def _finish_axiom(value: object) -> None:
    object.__setattr__(value, "annotations", normalize_annotations(value.annotations))  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True, eq=False)
class Declaration(AxiomNode):
    entity: Entity
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.entity, Entity, "Declaration.entity")
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class SubClassOf(AxiomNode):
    sub_class: ClassExpression
    super_class: ClassExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.sub_class, CLASS_EXPRESSION_TYPES, "SubClassOf.sub_class")
        require_node(self.super_class, CLASS_EXPRESSION_TYPES, "SubClassOf.super_class")
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class EquivalentClasses(AxiomNode):
    expressions: CanonicalSet[ClassExpression]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expressions",
            canonical_set(
                self.expressions,
                CLASS_EXPRESSION_TYPES,
                "EquivalentClasses.expressions",
                minimum=2,
            ),
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DisjointClasses(AxiomNode):
    expressions: CanonicalSet[ClassExpression]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expressions",
            canonical_set(
                self.expressions,
                CLASS_EXPRESSION_TYPES,
                "DisjointClasses.expressions",
                minimum=2,
            ),
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DisjointUnion(AxiomNode):
    defined_class: Class
    expressions: CanonicalSet[ClassExpression]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.defined_class, Class, "DisjointUnion.defined_class")
        object.__setattr__(
            self,
            "expressions",
            canonical_set(
                self.expressions,
                CLASS_EXPRESSION_TYPES,
                "DisjointUnion.expressions",
                minimum=2,
            ),
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class SubObjectPropertyOf(AxiomNode):
    sub_property: SubObjectPropertyExpression
    super_property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(
            self.sub_property,
            SUB_OBJECT_PROPERTY_EXPRESSION_TYPES,
            "SubObjectPropertyOf.sub_property",
        )
        require_node(
            self.super_property,
            OBJECT_PROPERTY_EXPRESSION_TYPES,
            "SubObjectPropertyOf.super_property",
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class EquivalentObjectProperties(AxiomNode):
    properties: CanonicalSet[ObjectPropertyExpression]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "properties",
            canonical_set(
                self.properties,
                OBJECT_PROPERTY_EXPRESSION_TYPES,
                "EquivalentObjectProperties.properties",
                minimum=2,
            ),
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DisjointObjectProperties(AxiomNode):
    properties: CanonicalSet[ObjectPropertyExpression]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "properties",
            canonical_set(
                self.properties,
                OBJECT_PROPERTY_EXPRESSION_TYPES,
                "DisjointObjectProperties.properties",
                minimum=2,
            ),
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class InverseObjectProperties(AxiomNode):
    first: ObjectPropertyExpression
    second: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.first, OBJECT_PROPERTY_EXPRESSION_TYPES, "InverseObjectProperties.first")
        require_node(
            self.second,
            OBJECT_PROPERTY_EXPRESSION_TYPES,
            "InverseObjectProperties.second",
        )
        if self.second < self.first:
            first = self.first
            object.__setattr__(self, "first", self.second)
            object.__setattr__(self, "second", first)
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class ObjectPropertyDomain(AxiomNode):
    property: ObjectPropertyExpression
    domain: ClassExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_class_axiom(self, "domain")


@dataclass(frozen=True, slots=True, eq=False)
class ObjectPropertyRange(AxiomNode):
    property: ObjectPropertyExpression
    range: ClassExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_class_axiom(self, "range")


def _object_property_class_axiom(value: object, field: str) -> None:
    name = type(value).__name__
    require_node(
        value.property,  # type: ignore[attr-defined]
        OBJECT_PROPERTY_EXPRESSION_TYPES,
        f"{name}.property",
    )
    require_node(
        getattr(value, field),
        CLASS_EXPRESSION_TYPES,
        f"{name}.{field}",
    )
    _finish_axiom(value)


def _object_property_characteristic(value: object) -> None:
    require_node(
        value.property,  # type: ignore[attr-defined]
        OBJECT_PROPERTY_EXPRESSION_TYPES,
        f"{type(value).__name__}.property",
    )
    _finish_axiom(value)


@dataclass(frozen=True, slots=True, eq=False)
class FunctionalObjectProperty(AxiomNode):
    property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_characteristic(self)


@dataclass(frozen=True, slots=True, eq=False)
class InverseFunctionalObjectProperty(AxiomNode):
    property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_characteristic(self)


@dataclass(frozen=True, slots=True, eq=False)
class ReflexiveObjectProperty(AxiomNode):
    property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_characteristic(self)


@dataclass(frozen=True, slots=True, eq=False)
class IrreflexiveObjectProperty(AxiomNode):
    property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_characteristic(self)


@dataclass(frozen=True, slots=True, eq=False)
class SymmetricObjectProperty(AxiomNode):
    property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_characteristic(self)


@dataclass(frozen=True, slots=True, eq=False)
class AsymmetricObjectProperty(AxiomNode):
    property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_characteristic(self)


@dataclass(frozen=True, slots=True, eq=False)
class TransitiveObjectProperty(AxiomNode):
    property: ObjectPropertyExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_property_characteristic(self)


@dataclass(frozen=True, slots=True, eq=False)
class SubDataPropertyOf(AxiomNode):
    sub_property: DataProperty
    super_property: DataProperty
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.sub_property, DataProperty, "SubDataPropertyOf.sub_property")
        require_node(self.super_property, DataProperty, "SubDataPropertyOf.super_property")
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class EquivalentDataProperties(AxiomNode):
    properties: CanonicalSet[DataProperty]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "properties",
            canonical_set(
                self.properties,
                DataProperty,
                "EquivalentDataProperties.properties",
                minimum=2,
            ),
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DisjointDataProperties(AxiomNode):
    properties: CanonicalSet[DataProperty]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "properties",
            canonical_set(
                self.properties,
                DataProperty,
                "DisjointDataProperties.properties",
                minimum=2,
            ),
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DataPropertyDomain(AxiomNode):
    property: DataProperty
    domain: ClassExpression
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.property, DataProperty, "DataPropertyDomain.property")
        require_node(self.domain, CLASS_EXPRESSION_TYPES, "DataPropertyDomain.domain")
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DataPropertyRange(AxiomNode):
    property: DataProperty
    range: DataRange
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.property, DataProperty, "DataPropertyRange.property")
        require_node(self.range, DATA_RANGE_TYPES, "DataPropertyRange.range")
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class FunctionalDataProperty(AxiomNode):
    property: DataProperty
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.property, DataProperty, "FunctionalDataProperty.property")
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DatatypeDefinition(AxiomNode):
    datatype: Datatype
    data_range: DataRange
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.datatype, Datatype, "DatatypeDefinition.datatype")
        require_node(self.data_range, DATA_RANGE_TYPES, "DatatypeDefinition.data_range")
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class HasKey(AxiomNode):
    class_expression: ClassExpression
    object_properties: CanonicalSet[ObjectPropertyExpression]
    data_properties: CanonicalSet[DataProperty]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.class_expression, CLASS_EXPRESSION_TYPES, "HasKey.class_expression")
        object_properties = canonical_set(
            self.object_properties,
            OBJECT_PROPERTY_EXPRESSION_TYPES,
            "HasKey.object_properties",
        )
        data_properties = canonical_set(
            self.data_properties,
            DataProperty,
            "HasKey.data_properties",
        )
        if not object_properties and not data_properties:
            raise StructuralConstraintError("HasKey requires at least one property")
        object.__setattr__(self, "object_properties", object_properties)
        object.__setattr__(self, "data_properties", data_properties)
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class SameIndividual(AxiomNode):
    individuals: CanonicalSet[Individual]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _individual_set_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class DifferentIndividuals(AxiomNode):
    individuals: CanonicalSet[Individual]
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _individual_set_axiom(self)


def _individual_set_axiom(value: object) -> None:
    object.__setattr__(
        value,
        "individuals",
        canonical_set(
            value.individuals,  # type: ignore[attr-defined]
            INDIVIDUAL_TYPES,
            f"{type(value).__name__}.individuals",
            minimum=2,
        ),
    )
    _finish_axiom(value)


@dataclass(frozen=True, slots=True, eq=False)
class ClassAssertion(AxiomNode):
    class_expression: ClassExpression
    individual: Individual
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(
            self.class_expression,
            CLASS_EXPRESSION_TYPES,
            "ClassAssertion.class_expression",
        )
        require_node(self.individual, INDIVIDUAL_TYPES, "ClassAssertion.individual")
        _finish_axiom(self)


def _object_assertion(value: object) -> None:
    name = type(value).__name__
    require_node(
        value.property,  # type: ignore[attr-defined]
        OBJECT_PROPERTY_EXPRESSION_TYPES,
        f"{name}.property",
    )
    require_node(value.source, INDIVIDUAL_TYPES, f"{name}.source")  # type: ignore[attr-defined]
    require_node(value.target, INDIVIDUAL_TYPES, f"{name}.target")  # type: ignore[attr-defined]
    _finish_axiom(value)


@dataclass(frozen=True, slots=True, eq=False)
class ObjectPropertyAssertion(AxiomNode):
    property: ObjectPropertyExpression
    source: Individual
    target: Individual
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_assertion(self)


@dataclass(frozen=True, slots=True, eq=False)
class NegativeObjectPropertyAssertion(AxiomNode):
    property: ObjectPropertyExpression
    source: Individual
    target: Individual
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _object_assertion(self)


def _data_assertion(value: object) -> None:
    name = type(value).__name__
    require_node(value.property, DataProperty, f"{name}.property")  # type: ignore[attr-defined]
    require_node(value.source, INDIVIDUAL_TYPES, f"{name}.source")  # type: ignore[attr-defined]
    require_node(value.value, Literal, f"{name}.value")  # type: ignore[attr-defined]
    _finish_axiom(value)


@dataclass(frozen=True, slots=True, eq=False)
class DataPropertyAssertion(AxiomNode):
    property: DataProperty
    source: Individual
    value: Literal
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _data_assertion(self)


@dataclass(frozen=True, slots=True, eq=False)
class NegativeDataPropertyAssertion(AxiomNode):
    property: DataProperty
    source: Individual
    value: Literal
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _data_assertion(self)


@dataclass(frozen=True, slots=True, eq=False)
class AnnotationAssertion(AxiomNode):
    property: AnnotationProperty
    subject: AnnotationSubject
    value: AnnotationValue
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.property, AnnotationProperty, "AnnotationAssertion.property")
        require_node(
            self.subject,
            (IRI, AnonymousIndividual),
            "AnnotationAssertion.subject",
        )
        require_node(
            self.value,
            (IRI, Literal, AnonymousIndividual),
            "AnnotationAssertion.value",
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class SubAnnotationPropertyOf(AxiomNode):
    sub_property: AnnotationProperty
    super_property: AnnotationProperty
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(
            self.sub_property,
            AnnotationProperty,
            "SubAnnotationPropertyOf.sub_property",
        )
        require_node(
            self.super_property,
            AnnotationProperty,
            "SubAnnotationPropertyOf.super_property",
        )
        _finish_axiom(self)


@dataclass(frozen=True, slots=True, eq=False)
class AnnotationPropertyDomain(AxiomNode):
    property: AnnotationProperty
    domain: IRI
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _annotation_property_iri_axiom(self, "domain")


@dataclass(frozen=True, slots=True, eq=False)
class AnnotationPropertyRange(AxiomNode):
    property: AnnotationProperty
    range: IRI
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        _annotation_property_iri_axiom(self, "range")


def _annotation_property_iri_axiom(value: object, field: str) -> None:
    name = type(value).__name__
    require_node(value.property, AnnotationProperty, f"{name}.property")  # type: ignore[attr-defined]
    require_node(getattr(value, field), IRI, f"{name}.{field}")
    _finish_axiom(value)


DeclarationAxiom = Declaration
AnnotationAxiom = (
    AnnotationAssertion
    | SubAnnotationPropertyOf
    | AnnotationPropertyDomain
    | AnnotationPropertyRange
)
LogicalAxiom = (
    SubClassOf
    | EquivalentClasses
    | DisjointClasses
    | DisjointUnion
    | SubObjectPropertyOf
    | EquivalentObjectProperties
    | DisjointObjectProperties
    | InverseObjectProperties
    | ObjectPropertyDomain
    | ObjectPropertyRange
    | FunctionalObjectProperty
    | InverseFunctionalObjectProperty
    | ReflexiveObjectProperty
    | IrreflexiveObjectProperty
    | SymmetricObjectProperty
    | AsymmetricObjectProperty
    | TransitiveObjectProperty
    | SubDataPropertyOf
    | EquivalentDataProperties
    | DisjointDataProperties
    | DataPropertyDomain
    | DataPropertyRange
    | FunctionalDataProperty
    | DatatypeDefinition
    | HasKey
    | SameIndividual
    | DifferentIndividuals
    | ClassAssertion
    | ObjectPropertyAssertion
    | NegativeObjectPropertyAssertion
    | DataPropertyAssertion
    | NegativeDataPropertyAssertion
)
Axiom = Declaration | LogicalAxiom | AnnotationAxiom

DECLARATION_AXIOM_TYPES = (Declaration,)
ANNOTATION_AXIOM_TYPES = (
    AnnotationAssertion,
    SubAnnotationPropertyOf,
    AnnotationPropertyDomain,
    AnnotationPropertyRange,
)
LOGICAL_AXIOM_TYPES = (
    SubClassOf,
    EquivalentClasses,
    DisjointClasses,
    DisjointUnion,
    SubObjectPropertyOf,
    EquivalentObjectProperties,
    DisjointObjectProperties,
    InverseObjectProperties,
    ObjectPropertyDomain,
    ObjectPropertyRange,
    FunctionalObjectProperty,
    InverseFunctionalObjectProperty,
    ReflexiveObjectProperty,
    IrreflexiveObjectProperty,
    SymmetricObjectProperty,
    AsymmetricObjectProperty,
    TransitiveObjectProperty,
    SubDataPropertyOf,
    EquivalentDataProperties,
    DisjointDataProperties,
    DataPropertyDomain,
    DataPropertyRange,
    FunctionalDataProperty,
    DatatypeDefinition,
    HasKey,
    SameIndividual,
    DifferentIndividuals,
    ClassAssertion,
    ObjectPropertyAssertion,
    NegativeObjectPropertyAssertion,
    DataPropertyAssertion,
    NegativeDataPropertyAssertion,
)
AXIOM_TYPES = DECLARATION_AXIOM_TYPES + LOGICAL_AXIOM_TYPES + ANNOTATION_AXIOM_TYPES


__all__ = [
    "ANNOTATION_AXIOM_TYPES",
    "AXIOM_TYPES",
    "DECLARATION_AXIOM_TYPES",
    "LOGICAL_AXIOM_TYPES",
    "AnnotationAssertion",
    "AnnotationAxiom",
    "AnnotationPropertyDomain",
    "AnnotationPropertyRange",
    "AsymmetricObjectProperty",
    "Axiom",
    "AxiomNode",
    "ClassAssertion",
    "DataPropertyAssertion",
    "DataPropertyDomain",
    "DataPropertyRange",
    "DatatypeDefinition",
    "Declaration",
    "DeclarationAxiom",
    "DifferentIndividuals",
    "DisjointClasses",
    "DisjointDataProperties",
    "DisjointObjectProperties",
    "DisjointUnion",
    "EquivalentClasses",
    "EquivalentDataProperties",
    "EquivalentObjectProperties",
    "FunctionalDataProperty",
    "FunctionalObjectProperty",
    "HasKey",
    "InverseFunctionalObjectProperty",
    "InverseObjectProperties",
    "IrreflexiveObjectProperty",
    "LogicalAxiom",
    "NegativeDataPropertyAssertion",
    "NegativeObjectPropertyAssertion",
    "ObjectPropertyAssertion",
    "ObjectPropertyDomain",
    "ObjectPropertyRange",
    "ReflexiveObjectProperty",
    "SameIndividual",
    "SubAnnotationPropertyOf",
    "SubClassOf",
    "SubDataPropertyOf",
    "SubObjectPropertyOf",
    "SymmetricObjectProperty",
    "TransitiveObjectProperty",
]
