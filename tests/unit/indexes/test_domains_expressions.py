from __future__ import annotations

from pyowl_core import (
    IRI,
    Class,
    DomainRangeKind,
    ExpressionOccurrenceIndex,
    ObjectProperty,
    ObjectSomeValuesFrom,
    PropertyDomainRangeView,
    ReferenceRole,
)

from .conftest import snapshot


def test_domain_range_keeps_complex_values_and_reports_named_filter_loss() -> None:
    ontology = snapshot(
        "ObjectPropertyDomain(:p ObjectIntersectionOf(:A :B))",
        "ObjectPropertyDomain(:p :NamedDomain)",
        "ObjectPropertyRange(:p :Range)",
        "DataPropertyRange(:dp <http://www.w3.org/2001/XMLSchema#string>)",
        "AnnotationPropertyRange(:ap <urn:index#AnnotationRange>)",
    )
    p = ObjectProperty(IRI("urn:index#p"))
    index = ontology.view(PropertyDomainRangeView)
    assert len(tuple(index.domains(p))) == 2
    named = index.named(p, DomainRangeKind.DOMAIN)
    assert len(named.records) == 1
    assert named.filtered_complex_count == 1
    assert len(tuple(index.ranges(p))) == 1


def test_expression_occurrences_are_interned_and_keep_paths_roles() -> None:
    ontology = snapshot(
        "SubClassOf(:A ObjectSomeValuesFrom(:p :B))",
        "EquivalentClasses(:C ObjectSomeValuesFrom(:p :B))",
    )
    expression = ObjectSomeValuesFrom(
        ObjectProperty(IRI("urn:index#p")),
        Class(IRI("urn:index#B")),
    )
    index = ontology.view(ExpressionOccurrenceIndex)
    occurrences = tuple(index.iter(expression))
    assert len(occurrences) == 2
    assert occurrences[0].expression is occurrences[1].expression
    assert all(value.constructor_path for value in occurrences)
    assert {value.role for value in occurrences} == {
        ReferenceRole.OPERAND,
        ReferenceRole.SUPERCLASS,
    }
    assert all(value.origins for value in occurrences)
