"""Structural reports and OWL 2 DL/profile global-analysis foundations."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum

from pyowl_core.exceptions import StructuralConstraintError

from .axioms import (
    AsymmetricObjectProperty,
    AxiomNode,
    Declaration,
    DisjointObjectProperties,
    EquivalentObjectProperties,
    FunctionalObjectProperty,
    HasKey,
    InverseFunctionalObjectProperty,
    InverseObjectProperties,
    IrreflexiveObjectProperty,
    SubObjectPropertyOf,
    SymmetricObjectProperty,
    TransitiveObjectProperty,
)
from .base import StructuralNode
from .canonical import canonical_bytes
from .expressions import (
    ObjectExactCardinality,
    ObjectHasSelf,
    ObjectMaxCardinality,
    ObjectMinCardinality,
)
from .primitives import (
    OWL_BOTTOM_OBJECT_PROPERTY,
    OWL_TOP_OBJECT_PROPERTY,
    XSD_STRING_IRI,
    Entity,
    EntityKind,
    Literal,
    ObjectProperty,
)
from .properties import (
    ObjectInverseOf,
    ObjectPropertyChain,
    ObjectPropertyExpression,
    inverse_property,
)
from .visitor import walk


class ValidationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    severity: ValidationSeverity
    message: str
    constructor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("validation issue code must be a nonempty string")
        if not isinstance(self.severity, ValidationSeverity):
            raise TypeError("validation issue severity must be ValidationSeverity")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("validation issue message must be a nonempty string")
        if self.constructor is not None and (
            not isinstance(self.constructor, str) or not self.constructor
        ):
            raise ValueError("validation issue constructor must be a nonempty string or None")


@dataclass(frozen=True, slots=True)
class StructuralReport:
    issues: tuple[ValidationIssue, ...]
    values_checked: int
    complete: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", _issue_tuple(self.issues))
        _nonnegative_integer(self.values_checked, "values_checked")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be bool")

    @property
    def valid(self) -> bool:
        return self.complete and not any(
            issue.severity is ValidationSeverity.ERROR for issue in self.issues
        )

    def raise_for_errors(self) -> None:
        if not self.valid:
            codes = ", ".join(issue.code for issue in self.issues) or "INCOMPLETE_VALIDATION"
            raise StructuralConstraintError(f"structural validation failed: {codes}")


@dataclass(frozen=True, slots=True)
class RoleEdge:
    sub_property: ObjectPropertyExpression
    super_property: ObjectPropertyExpression

    def __post_init__(self) -> None:
        _object_property_expression(self.sub_property, "sub_property")
        _object_property_expression(self.super_property, "super_property")


@dataclass(frozen=True, slots=True)
class RoleAnalysis:
    """Canonical direct role hierarchy plus W3C-composite/simple-role closure."""

    properties: tuple[ObjectPropertyExpression, ...]
    hierarchy: tuple[RoleEdge, ...]
    composite: tuple[ObjectPropertyExpression, ...]
    non_simple: tuple[ObjectPropertyExpression, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "properties", _property_tuple(self.properties, "properties"))
        hierarchy = tuple(self.hierarchy)
        if not all(isinstance(edge, RoleEdge) for edge in hierarchy):
            raise TypeError("hierarchy must contain RoleEdge values")
        object.__setattr__(self, "hierarchy", hierarchy)
        object.__setattr__(self, "composite", _property_tuple(self.composite, "composite"))
        object.__setattr__(self, "non_simple", _property_tuple(self.non_simple, "non_simple"))

    def is_simple(self, property: ObjectPropertyExpression) -> bool:
        _object_property_expression(property, "property")
        return all(property != candidate for candidate in self.non_simple)


@dataclass(frozen=True, slots=True)
class OWL2DLReport:
    structural: StructuralReport
    issues: tuple[ValidationIssue, ...]
    roles: RoleAnalysis
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.structural, StructuralReport):
            raise TypeError("structural must be StructuralReport")
        object.__setattr__(self, "issues", _issue_tuple(self.issues))
        if not isinstance(self.roles, RoleAnalysis):
            raise TypeError("roles must be RoleAnalysis")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be bool")

    @property
    def conforms(self) -> bool:
        return (
            self.complete
            and self.structural.valid
            and not any(issue.severity is ValidationSeverity.ERROR for issue in self.issues)
        )


class Profile(str, Enum):
    EL = "EL"
    QL = "QL"
    RL = "RL"


@dataclass(frozen=True, slots=True)
class ProfileReport:
    profile: Profile
    issues: tuple[ValidationIssue, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.profile, Profile):
            raise TypeError("profile must be Profile")
        object.__setattr__(self, "issues", _issue_tuple(self.issues))
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be bool")

    @property
    def conforms(self) -> bool:
        return self.complete and not any(
            issue.severity is ValidationSeverity.ERROR for issue in self.issues
        )


def _issue_tuple(values: Iterable[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    issues = tuple(values)
    if not all(isinstance(issue, ValidationIssue) for issue in issues):
        raise TypeError("issues must contain ValidationIssue values")
    return issues


def _nonnegative_integer(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")


def _object_property_expression(value: object, field: str) -> None:
    if not isinstance(value, (ObjectProperty, ObjectInverseOf)):
        raise TypeError(f"{field} must be an object property expression")


def _property_tuple(
    values: Iterable[ObjectPropertyExpression],
    field: str,
) -> tuple[ObjectPropertyExpression, ...]:
    properties = tuple(values)
    for property in properties:
        _object_property_expression(property, field)
    return properties


def _roots(values: StructuralNode | Iterable[StructuralNode]) -> tuple[StructuralNode, ...]:
    if isinstance(values, StructuralNode):
        return (values,)
    roots = tuple(values)
    if not all(isinstance(value, StructuralNode) for value in roots):
        raise TypeError("values must contain StructuralNode instances")
    return roots


def validate_structural(
    values: StructuralNode | Iterable[StructuralNode],
    *,
    limits: object | None = None,
) -> StructuralReport:
    roots = _roots(values)
    count = 0
    issues: list[ValidationIssue] = []
    try:
        for root in roots:
            canonical_bytes(root, limits=limits)
            count += sum(1 for _ in walk(root))
    except (StructuralConstraintError, TypeError, ValueError) as error:
        issues.append(
            ValidationIssue(
                "STRUCTURAL_INVALID",
                ValidationSeverity.ERROR,
                str(error),
            )
        )
    return StructuralReport(tuple(issues), count)


def analyze_object_property_roles(axioms: Iterable[AxiomNode]) -> RoleAnalysis:
    axiom_values = tuple(axioms)
    if not all(isinstance(axiom, AxiomNode) for axiom in axiom_values):
        raise TypeError("axioms must contain AxiomNode values")
    properties: dict[bytes, ObjectPropertyExpression] = {}
    edges: dict[tuple[bytes, bytes], RoleEdge] = {}
    composite: dict[bytes, ObjectPropertyExpression] = {
        canonical_bytes(OWL_TOP_OBJECT_PROPERTY): OWL_TOP_OBJECT_PROPERTY,
        canonical_bytes(OWL_BOTTOM_OBJECT_PROPERTY): OWL_BOTTOM_OBJECT_PROPERTY,
    }

    def retain(property: ObjectPropertyExpression) -> None:
        properties[canonical_bytes(property)] = property
        inverse = inverse_property(property)
        properties[canonical_bytes(inverse)] = inverse

    def edge(sub: ObjectPropertyExpression, sup: ObjectPropertyExpression) -> None:
        retain(sub)
        retain(sup)
        edges[(canonical_bytes(sub), canonical_bytes(sup))] = RoleEdge(sub, sup)
        inverse_sub = inverse_property(sub)
        inverse_sup = inverse_property(sup)
        edges[(canonical_bytes(inverse_sub), canonical_bytes(inverse_sup))] = RoleEdge(
            inverse_sub,
            inverse_sup,
        )

    for axiom in axiom_values:
        for node in walk(axiom):
            if isinstance(node, (ObjectProperty, ObjectInverseOf)):
                retain(node)
        if isinstance(axiom, SubObjectPropertyOf):
            if isinstance(axiom.sub_property, ObjectPropertyChain):
                composite[canonical_bytes(axiom.super_property)] = axiom.super_property
                composite[canonical_bytes(inverse_property(axiom.super_property))] = (
                    inverse_property(axiom.super_property)
                )
            else:
                edge(axiom.sub_property, axiom.super_property)
        elif isinstance(axiom, EquivalentObjectProperties):
            equivalent = tuple(axiom.properties)
            first = equivalent[0]
            for second in equivalent[1:]:
                edge(first, second)
                edge(second, first)
        elif isinstance(axiom, InverseObjectProperties):
            edge(axiom.first, inverse_property(axiom.second))
            edge(axiom.second, inverse_property(axiom.first))
        elif isinstance(axiom, SymmetricObjectProperty):
            edge(axiom.property, inverse_property(axiom.property))
        elif isinstance(axiom, TransitiveObjectProperty):
            retain(axiom.property)
            composite[canonical_bytes(axiom.property)] = axiom.property
            inverse = inverse_property(axiom.property)
            composite[canonical_bytes(inverse)] = inverse

    for property in tuple(composite.values()):
        retain(property)
    adjacency: dict[bytes, set[bytes]] = {key: set() for key in properties}
    for sub, sup in edges:
        adjacency[sub].add(sup)
    non_simple: dict[bytes, ObjectPropertyExpression] = dict(composite)
    pending = list(composite)
    while pending:
        sub = pending.pop()
        for sup in adjacency[sub]:
            if sup not in non_simple:
                non_simple[sup] = properties[sup]
                pending.append(sup)
    hierarchy = tuple(edges[key] for key in sorted(edges))
    return RoleAnalysis(
        properties=tuple(properties[key] for key in sorted(properties)),
        hierarchy=hierarchy,
        composite=tuple(composite[key] for key in sorted(composite)),
        non_simple=tuple(non_simple[key] for key in sorted(non_simple)),
    )


def validate_owl2_dl(axioms: Iterable[AxiomNode]) -> OWL2DLReport:
    values = tuple(axioms)
    structural = validate_structural(values)
    roles = analyze_object_property_roles(values)
    issues: list[ValidationIssue] = []
    declarations: dict[str, set[EntityKind]] = {}
    uses: dict[str, set[EntityKind]] = {}
    for axiom in values:
        if isinstance(axiom, Declaration):
            declarations.setdefault(axiom.entity.iri.value, set()).add(axiom.entity.kind)
        for node in walk(axiom):
            if isinstance(node, Entity):
                uses.setdefault(node.iri.value, set()).add(node.kind)

    property_kinds = {
        EntityKind.OBJECT_PROPERTY,
        EntityKind.DATA_PROPERTY,
        EntityKind.ANNOTATION_PROPERTY,
    }
    for _iri, kinds in declarations.items():
        if len(kinds & property_kinds) > 1:
            issues.append(
                ValidationIssue(
                    "OWL2DL_PROPERTY_PUNNING",
                    ValidationSeverity.ERROR,
                    "one IRI is declared as more than one property kind",
                    "Declaration",
                )
            )
        if EntityKind.CLASS in kinds and EntityKind.DATATYPE in kinds:
            issues.append(
                ValidationIssue(
                    "OWL2DL_CLASS_DATATYPE_PUNNING",
                    ValidationSeverity.ERROR,
                    "one IRI is declared as both class and datatype",
                    "Declaration",
                )
            )
    for iri, kinds in uses.items():
        declared = declarations.get(iri, set())
        for kind in kinds - {EntityKind.NAMED_INDIVIDUAL}:
            if kind not in declared and not _is_builtin_iri(iri):
                issues.append(
                    ValidationIssue(
                        "OWL2DL_MISSING_DECLARATION",
                        ValidationSeverity.ERROR,
                        f"used {kind.value} is not declared",
                    )
                )

    for axiom in values:
        for property in _simple_required_properties(axiom):
            if not roles.is_simple(property):
                issues.append(
                    ValidationIssue(
                        "OWL2DL_NON_SIMPLE_PROPERTY",
                        ValidationSeverity.ERROR,
                        "axiom position requires a simple object property expression",
                        type(axiom).__name__,
                    )
                )
    issues.append(
        ValidationIssue(
            "OWL2DL_CLOSURE_VALIDATION_PENDING",
            ValidationSeverity.INFO,
            "complete import-closure reserved-vocabulary/datatype validation belongs to WP03",
        )
    )
    return OWL2DLReport(structural, tuple(issues), roles, complete=False)


def _simple_required_properties(axiom: AxiomNode) -> Iterator[ObjectPropertyExpression]:
    if isinstance(
        axiom,
        (
            FunctionalObjectProperty,
            InverseFunctionalObjectProperty,
            IrreflexiveObjectProperty,
            AsymmetricObjectProperty,
        ),
    ):
        yield axiom.property
    if isinstance(axiom, DisjointObjectProperties):
        yield from axiom.properties
    if isinstance(axiom, HasKey):
        yield from axiom.object_properties
    for node in walk(axiom):
        if isinstance(
            node,
            (
                ObjectHasSelf,
                ObjectMinCardinality,
                ObjectMaxCardinality,
                ObjectExactCardinality,
            ),
        ):
            yield node.property


def _is_builtin_iri(iri: str) -> bool:
    return iri.startswith(
        (
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "http://www.w3.org/2000/01/rdf-schema#",
            "http://www.w3.org/2001/XMLSchema#",
            "http://www.w3.org/2002/07/owl#",
        )
    )


def validate_profile(
    axioms: Iterable[AxiomNode],
    profile: Profile,
) -> ProfileReport:
    if not isinstance(profile, Profile):
        raise TypeError("profile must be Profile")
    values = tuple(axioms)
    validate_structural(values).raise_for_errors()
    return ProfileReport(
        profile,
        (
            ValidationIssue(
                "PROFILE_CLOSURE_VALIDATION_PENDING",
                ValidationSeverity.INFO,
                "complete profile grammar/global validation requires a resolved closure",
            ),
        ),
        complete=False,
    )


_INTEGER_LEXICAL = re.compile(r"^[+-]?[0-9]+$")
_DECIMAL_LEXICAL = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
_XSD_NAMESPACE = "http://www.w3.org/2001/XMLSchema#"
_INTEGER_DATATYPES = frozenset(
    _XSD_NAMESPACE + local
    for local in (
        "integer",
        "nonPositiveInteger",
        "negativeInteger",
        "long",
        "int",
        "short",
        "byte",
        "nonNegativeInteger",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
        "positiveInteger",
    )
)
_INTEGER_BOUNDS: dict[str, tuple[int | None, int | None]] = {
    _XSD_NAMESPACE + "nonPositiveInteger": (None, 0),
    _XSD_NAMESPACE + "negativeInteger": (None, -1),
    _XSD_NAMESPACE + "long": (-(2**63), 2**63 - 1),
    _XSD_NAMESPACE + "int": (-(2**31), 2**31 - 1),
    _XSD_NAMESPACE + "short": (-(2**15), 2**15 - 1),
    _XSD_NAMESPACE + "byte": (-(2**7), 2**7 - 1),
    _XSD_NAMESPACE + "nonNegativeInteger": (0, None),
    _XSD_NAMESPACE + "unsignedLong": (0, 2**64 - 1),
    _XSD_NAMESPACE + "unsignedInt": (0, 2**32 - 1),
    _XSD_NAMESPACE + "unsignedShort": (0, 2**16 - 1),
    _XSD_NAMESPACE + "unsignedByte": (0, 2**8 - 1),
    _XSD_NAMESPACE + "positiveInteger": (1, None),
}


def validate_lexical_form(literal: Literal) -> tuple[ValidationIssue, ...]:
    if not isinstance(literal, Literal):
        raise TypeError("literal must be Literal")
    iri = literal.datatype.iri.value
    lexical = literal.lexical_form
    valid = True
    if iri in _INTEGER_DATATYPES:
        valid = _INTEGER_LEXICAL.fullmatch(lexical) is not None
        if valid and iri in _INTEGER_BOUNDS:
            lower, upper = _INTEGER_BOUNDS[iri]
            number = int(lexical)
            valid = (lower is None or number >= lower) and (upper is None or number <= upper)
    elif iri == _XSD_NAMESPACE + "decimal":
        valid = _DECIMAL_LEXICAL.fullmatch(lexical) is not None
    elif iri == _XSD_NAMESPACE + "boolean":
        valid = lexical in {"true", "false", "1", "0"}
    elif iri == XSD_STRING_IRI:
        valid = True
    if valid:
        return ()
    return (
        ValidationIssue(
            "INVALID_DATATYPE_LEXICAL_FORM",
            ValidationSeverity.ERROR,
            "literal lexical form is outside the recognized datatype lexical space",
            "Literal",
        ),
    )


__all__ = [
    "OWL2DLReport",
    "Profile",
    "ProfileReport",
    "RoleAnalysis",
    "RoleEdge",
    "StructuralReport",
    "ValidationIssue",
    "ValidationSeverity",
    "analyze_object_property_roles",
    "validate_lexical_form",
    "validate_owl2_dl",
    "validate_profile",
    "validate_structural",
]
