"""Exhaustive scalar-view fixture support for encoded-column tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

import pyowl_core.model as m
from pyowl_core import OntologyDocument, OntologySnapshot
from pyowl_core.extensions.swrl import SWRLRule
from tests.conformance._support import every_constructor_document, python_snapshot
from tests.generated.model.fixtures import model_fixtures


def _set(values: Iterable[m.StructuralNode]) -> m.CanonicalSet[m.StructuralNode]:
    return m.CanonicalSet(values)


def complete_constructor_snapshot() -> OntologySnapshot:
    """Return one real scalar snapshot whose reachable graph has every model tag."""

    fixtures = model_fixtures()
    class_value = m.Class(m.IRI("urn:encoded-view:Class"))
    datatype = m.Datatype(m.IRI("urn:encoded-view:Datatype"))
    object_property = m.ObjectProperty(m.IRI("urn:encoded-view:objectProperty"))
    data_property = m.DataProperty(m.IRI("urn:encoded-view:dataProperty"))
    annotation_property = m.AnnotationProperty(m.IRI("urn:encoded-view:annotationProperty"))
    individual = m.NamedIndividual(m.IRI("urn:encoded-view:individual"))
    literal = m.Literal("value", m.XSD_STRING)
    axioms: list[m.AxiomNode] = []
    ontology_annotations: list[m.Annotation] = []
    extensions: list[m.StructuralNode] = []

    for spec in m.CONSTRUCTOR_SPECS:
        fixture = fixtures[spec.constructor]
        if isinstance(fixture, m.AxiomNode):
            axioms.append(fixture)
        elif isinstance(fixture, m.Annotation):
            ontology_annotations.append(fixture)
        elif spec.category == "class_expression":
            axioms.append(m.ClassAssertion(cast(m.ClassExpression, fixture), individual))
        elif spec.category == "data_range":
            axioms.append(m.DatatypeDefinition(datatype, cast(m.DataRange, fixture)))
        elif spec.category == "facet_restriction":
            restriction = m.DatatypeRestriction(
                datatype,
                cast(m.CanonicalSet[m.FacetRestriction], _set((fixture,))),
            )
            axioms.append(m.DatatypeDefinition(datatype, restriction))
        elif spec.category == "property_expression":
            axioms.append(m.FunctionalObjectProperty(cast(m.ObjectPropertyExpression, fixture)))
        elif spec.category == "sub_object_property_expression":
            axioms.append(
                m.SubObjectPropertyOf(cast(m.SubObjectPropertyExpression, fixture), object_property)
            )
        elif spec.category == "primitive":
            if isinstance(fixture, m.IRI):
                axioms.append(m.AnnotationAssertion(annotation_property, fixture, literal))
            elif isinstance(fixture, m.Entity):
                axioms.append(m.Declaration(fixture))
            elif isinstance(fixture, m.AnonymousIndividual):
                axioms.append(m.ClassAssertion(class_value, fixture))
            elif isinstance(fixture, m.Literal):
                axioms.append(m.DataPropertyAssertion(data_property, individual, fixture))
        elif spec.category == "swrl_extension" and isinstance(fixture, SWRLRule):
            extensions.append(fixture)

    source = every_constructor_document()
    document = OntologyDocument(
        source.ontology_id,
        source.document_iri,
        source.direct_imports,
        cast(m.CanonicalSet[m.Annotation], _set(ontology_annotations)),
        cast(m.CanonicalSet[m.AxiomNode], _set(axioms)),
        _set(extensions),
        source.provenance,
    )
    return python_snapshot(document)


def scalar_root_bytes(snapshot: OntologySnapshot) -> tuple[tuple[int, bytes], ...]:
    roots = [
        *((1, m.canonical_bytes(value)) for value in snapshot.ontology_annotations()),
        *((2, m.canonical_bytes(value)) for value in snapshot.iter_axioms()),
        *((3, m.canonical_bytes(value)) for value in snapshot.iter_extensions()),
    ]
    return tuple(sorted(roots))


__all__ = ["complete_constructor_snapshot", "scalar_root_bytes"]
