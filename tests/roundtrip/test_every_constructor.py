from __future__ import annotations

import hashlib

import pytest

import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    DetectionBasis,
    DigestKind,
    DocumentFormat,
    DocumentProvenance,
    LoadOptions,
    OntologyDocument,
    OntologyID,
    parse_document,
    render_document,
)
from tests.generated.model.fixtures import model_fixtures

PYTHON_OPTIONS = LoadOptions(backend=BackendPreference.PYTHON)
ONTOLOGY_IRI = m.IRI("https://example.org/wp02/every-constructor")


def _source_document() -> OntologyDocument:
    fixtures = model_fixtures()
    fixture_axioms = [
        value
        for value in fixtures.values()
        if isinstance(value, m.AxiomNode) and not isinstance(value, m.Declaration)
    ]
    entities = {entity for axiom in fixture_axioms for entity in m.signature(axiom)}
    declarations = [m.Declaration(entity) for entity in entities]
    axioms = m.CanonicalSet((*declarations, *fixture_axioms))
    provenance = DocumentProvenance(
        hashlib.sha256(b"generated-every-constructor").digest(),
        DigestKind.EXACT_BYTES,
        0,
        0,
        ONTOLOGY_IRI,
        None,
        DocumentFormat.FUNCTIONAL,
        DetectionBasis.EXPLICIT,
    )
    return OntologyDocument(
        OntologyID(ONTOLOGY_IRI),
        ONTOLOGY_IRI,
        (m.IRI("https://example.org/wp02/import"),),
        m.CanonicalSet((fixtures[m.Annotation],)),
        axioms,
        m.CanonicalSet(),
        provenance,
    )


@pytest.fixture(scope="module")
def canonical_document() -> OntologyDocument:
    source = _source_document()
    functional = render_document(source, format=DocumentFormat.FUNCTIONAL)
    return parse_document(
        functional,
        format=DocumentFormat.FUNCTIONAL,
        document_iri=ONTOLOGY_IRI,
        options=PYTHON_OPTIONS,
    )


def test_fixture_covers_every_permanent_axiom_constructor(
    canonical_document: OntologyDocument,
) -> None:
    covered = {type(axiom) for axiom in canonical_document.axioms}
    assert set(m.AXIOM_TYPES) <= covered


@pytest.mark.parametrize("format", tuple(DocumentFormat))
def test_every_constructor_round_trips_through_each_required_format(
    canonical_document: OntologyDocument,
    format: DocumentFormat,
) -> None:
    rendered = render_document(canonical_document, format=format)
    assert rendered == render_document(canonical_document, format=format)
    reparsed = parse_document(
        rendered,
        format=format,
        document_iri=ONTOLOGY_IRI,
        options=PYTHON_OPTIONS,
    )
    assert reparsed == canonical_document
    assert reparsed.document_fingerprint == canonical_document.document_fingerprint
    assert reparsed.rdf_mapping_report is None or reparsed.rdf_mapping_report.conformant
