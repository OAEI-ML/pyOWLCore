from __future__ import annotations

from pyowl_core import (
    FIELD_ROLE_TABLE,
    IRI,
    AnnotationAxiom,
    AnnotationProperty,
    AxiomCategory,
    AxiomTypeIndex,
    Class,
    Declaration,
    EntityKind,
    EntityReferenceIndex,
    FieldID,
    LogicalAxiom,
    ObjectProperty,
    ReferenceRole,
    SignatureView,
    SubClassOf,
)
from pyowl_core.index.common import iter_structural_occurrences
from pyowl_core.model import CONSTRUCTOR_SPECS, CanonicalSet, StructuralNode, constructor_spec
from tests.generated.model.fixtures import model_fixtures

from .conftest import snapshot


def test_typed_signature_punning_annotation_and_exact_builtins() -> None:
    ontology = snapshot(
        "Declaration(Class(:P))",
        "Declaration(ObjectProperty(:P))",
        "Declaration(Class(:A))",
        "Declaration(Class(<http://www.w3.org/2002/07/owl#Custom>))",
        "SubClassOf(:A ObjectSomeValuesFrom(:P <http://www.w3.org/2002/07/owl#Thing>))",
        'AnnotationAssertion(rdfs:label :A "label"@en)',
    )
    signature = ontology.view(SignatureView)
    punned = signature.entities_by_iri(IRI("urn:index#P"))
    assert {value.kind for value in punned} == {
        EntityKind.CLASS,
        EntityKind.OBJECT_PROPERTY,
    }
    declared = ontology.view(SignatureView, declared_only=True)
    assert {value.iri.value for value in declared.iter()} == {
        "urn:index#A",
        "urn:index#P",
        "http://www.w3.org/2002/07/owl#Custom",
    }
    without_annotations = ontology.view(SignatureView, include_annotation_only=False)
    assert AnnotationProperty(IRI("http://www.w3.org/2000/01/rdf-schema#label")) not in tuple(
        without_annotations.iter()
    )
    without_builtins = ontology.view(SignatureView, include_builtins=False)
    iris = {value.iri.value for value in without_builtins.iter()}
    assert "http://www.w3.org/2002/07/owl#Thing" not in iris
    assert "http://www.w3.org/2002/07/owl#Custom" in iris
    assert signature.flat_iris() == tuple(sorted(set(signature.flat_iris())))


def test_exact_axiom_tags_and_generated_categories() -> None:
    ontology = snapshot(
        "Declaration(Class(:A))",
        "Declaration(Class(:B))",
        "SubClassOf(:A :B)",
        'AnnotationAssertion(rdfs:label :A "A")',
    )
    index = ontology.view(AxiomTypeIndex)
    assert index.count(Declaration) == 2
    assert index.count(SubClassOf) == 1
    assert index.count_category(AxiomCategory.DECLARATION) == 2
    assert index.count_category(LogicalAxiom) == 1
    assert index.count_category(AnnotationAxiom) == 1
    assert tuple(index.iter(SubClassOf)) == tuple(ontology.iter_axioms(SubClassOf))
    assert index.posting(next(iter(index.iter(SubClassOf)))).origins


def test_reference_paths_roles_annotations_and_typed_keys() -> None:
    ontology = snapshot(
        "Declaration(Class(:A))",
        "Declaration(ObjectProperty(:p))",
        'SubClassOf(Annotation(rdfs:label "edge") :A ObjectSomeValuesFrom(:p :B))',
    )
    a = Class(IRI("urn:index#A"))
    p = ObjectProperty(IRI("urn:index#p"))
    index = ontology.view(EntityReferenceIndex)
    a_occurrences = tuple(index.iter(a))
    assert {value.role for value in a_occurrences} == {
        ReferenceRole.DECLARATION,
        ReferenceRole.SUBCLASS,
    }
    assert all(
        isinstance(step.field_id, FieldID) and isinstance(step.field_id.constructor_tag, int)
        for value in a_occurrences
        for step in value.constructor_path
    )
    assert any(value.role is ReferenceRole.PROPERTY for value in index.iter(p))
    annotation_property = AnnotationProperty(IRI("http://www.w3.org/2000/01/rdf-schema#label"))
    assert tuple(index.iter(annotation_property))
    no_annotations = ontology.view(EntityReferenceIndex, include_annotations=False)
    assert not tuple(no_annotations.iter(annotation_property))
    no_origins = ontology.view(
        EntityReferenceIndex,
        include_source_provenance=False,
    )
    assert all(not value.origins for value in no_origins.iter(a))


def test_generated_field_role_ledger_covers_every_constructor_field() -> None:
    expected = {
        FieldID(spec.tag, ordinal)
        for spec in CONSTRUCTOR_SPECS
        for ordinal, _field in enumerate(spec.fields)
    }
    assert set(FIELD_ROLE_TABLE) == expected
    observed: set[FieldID] = set()
    structural_fields: set[FieldID] = set()
    for fixture in model_fixtures().values():
        spec = constructor_spec(fixture)
        for ordinal, field_name in enumerate(spec.fields):
            value = getattr(fixture, field_name)
            if isinstance(value, StructuralNode) or (
                isinstance(value, (CanonicalSet, tuple))
                and any(isinstance(item, StructuralNode) for item in value)
            ):
                structural_fields.add(FieldID(spec.tag, ordinal))
        for _node, path, role in iter_structural_occurrences(fixture):
            assert isinstance(role, ReferenceRole)
            observed.update(step.field_id for step in path)
    assert observed == structural_fields
