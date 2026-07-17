from __future__ import annotations

from pyowl_core import (
    IRI,
    AnnotationAssertionIndex,
    AnnotationProperty,
    AssertedClassHierarchyView,
    AssertedPropertyHierarchyView,
    Class,
    DataProperty,
    DeclarationIndex,
    EquivalenceHandling,
    ObjectProperty,
)

from .conftest import snapshot


def test_declarations_report_undeclared_references_without_synthesis() -> None:
    ontology = snapshot(
        "Declaration(Class(:A))",
        "SubClassOf(:A :B)",
    )
    index = ontology.view(DeclarationIndex)
    a = Class(IRI("urn:index#A"))
    b = Class(IRI("urn:index#B"))
    assert index.is_declared(a)
    assert not index.is_declared(b)
    assert tuple(index.undeclared_entities()) == (b,)
    assert tuple(index.declarations(b)) == ()


def test_annotation_subject_property_reverse_nested_and_language_selection() -> None:
    ontology = snapshot(
        'AnnotationAssertion(rdfs:label :A "Hello"@en)',
        'AnnotationAssertion(rdfs:label :A "Hallo"@de)',
        "AnnotationAssertion(<urn:index#see> :A <urn:index#B>)",
        'AnnotationAssertion(Annotation(<urn:index#meta> "nested") rdfs:label :A "x")',
    )
    subject = IRI("urn:index#A")
    label = AnnotationProperty(IRI("http://www.w3.org/2000/01/rdf-schema#label"))
    index = ontology.view(AnnotationAssertionIndex, include_nested=True)
    assert len(tuple(index.assertions(subject, property=label))) == 3
    assert index.select_literal(subject, label, ("de", "en")).lexical_form == "Hallo"
    reverse = tuple(index.reverse_iri_value(IRI("urn:index#B")))
    assert len(reverse) == 1
    assert reverse[0].assertion.subject == subject
    assert any(
        occurrence.annotation.property.iri.value == "urn:index#meta"
        for occurrence in index.nested()
    )


def test_class_hierarchy_is_asserted_only_and_equivalence_is_explicit() -> None:
    ontology = snapshot(
        "SubClassOf(:A :B)",
        "SubClassOf(:B :C)",
        "EquivalentClasses(:A :D)",
        "SubClassOf(ObjectSomeValuesFrom(:p :A) :C)",
    )
    a = Class(IRI("urn:index#A"))
    b = Class(IRI("urn:index#B"))
    c = Class(IRI("urn:index#C"))
    d = Class(IRI("urn:index#D"))
    preserved = ontology.view(AssertedClassHierarchyView)
    assert tuple(preserved.asserted_parents(a)) == (b,)
    assert c not in tuple(preserved.asserted_parents(a))
    assert tuple(preserved.equivalents(a)) == (d,)
    bidirectional = ontology.view(
        AssertedClassHierarchyView,
        equivalence_handling=EquivalenceHandling.BIDIRECTIONAL,
    )
    assert set(bidirectional.asserted_parents(a)) == {b, d}
    component = ontology.view(
        AssertedClassHierarchyView,
        equivalence_handling=EquivalenceHandling.COMPONENT,
    )
    assert component.component(a).members == (a, d)
    assert preserved.ignored_complex_endpoint_count == 1


def test_property_chains_and_inverses_are_separate_not_flattened() -> None:
    ontology = snapshot(
        "SubObjectPropertyOf(:p :q)",
        "SubObjectPropertyOf(ObjectPropertyChain(:p :q) :r)",
        "InverseObjectProperties(:p :inverse)",
        "EquivalentObjectProperties(:q :r)",
        "SubDataPropertyOf(:dp :dq)",
    )
    p = ObjectProperty(IRI("urn:index#p"))
    q = ObjectProperty(IRI("urn:index#q"))
    data = DataProperty(IRI("urn:index#dp"))
    index = ontology.view(AssertedPropertyHierarchyView)
    assert tuple(index.asserted_parents(p)) == (q,)
    assert len(tuple(index.chains())) == 1
    assert len(tuple(index.inverses())) == 1
    assert tuple(index.asserted_parents(data)) == (DataProperty(IRI("urn:index#dq")),)
    assert ObjectProperty(IRI("urn:index#r")) not in tuple(index.asserted_parents(p))
