from __future__ import annotations

from unittest.mock import patch

import pytest

import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    BackendUnavailableError,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OptionConflictError,
    PythonParser,
    UnsupportedSyntaxError,
    load_snapshot,
    parse_document,
    render_document,
)
from pyowl_core.backends.native import NativeProbe

PYTHON_OPTIONS = LoadOptions(backend=BackendPreference.PYTHON)
ONTOLOGY = "https://example.org/w3c-derived"
CLASS = ONTOLOGY + "#C"

FUNCTIONAL = f"Ontology(<{ONTOLOGY}> Declaration(Class(<{CLASS}>)))".encode()
OWL_XML = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="{ONTOLOGY}">
  <Declaration><Class IRI="{CLASS}"/></Declaration>
</Ontology>
""".encode()
TURTLE = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY}> a owl:Ontology .
<{CLASS}> a owl:Class .
""".encode()
RDF_XML = f"""\
<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="{ONTOLOGY}"/>
  <owl:Class rdf:about="{CLASS}"/>
</rdf:RDF>
""".encode()


@pytest.mark.parametrize(
    ("format", "source"),
    [
        (DocumentFormat.FUNCTIONAL, FUNCTIONAL),
        (DocumentFormat.OWL_XML, OWL_XML),
        (DocumentFormat.TURTLE, TURTLE),
        (DocumentFormat.RDF_XML, RDF_XML),
    ],
)
def test_w3c_derived_minimal_documents_have_one_structure(
    format: DocumentFormat, source: bytes
) -> None:
    document = parse_document(
        source,
        format=format,
        document_iri=ONTOLOGY,
        options=PYTHON_OPTIONS,
    )
    assert document.ontology_id.ontology_iri == m.IRI(ONTOLOGY)
    assert document.axioms == m.CanonicalSet((m.Declaration(m.Class(m.IRI(CLASS))),))


def test_disabled_provenance_omits_document_and_snapshot_origins() -> None:
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
        collect_provenance=False,
    )
    document = parse_document(FUNCTIONAL, options=options)
    snapshot = load_snapshot(FUNCTIONAL, options=options)

    assert document.origin_index is None
    assert not snapshot.origin_index.entries


@pytest.mark.parametrize("format", tuple(DocumentFormat))
def test_each_writer_is_deterministic_and_round_trips(format: DocumentFormat) -> None:
    document = parse_document(FUNCTIONAL, format="functional", options=PYTHON_OPTIONS)
    first = render_document(document, format=format)
    second = render_document(document, format=format)
    assert first == second
    reparsed = parse_document(
        first,
        format=format,
        document_iri=ONTOLOGY,
        options=PYTHON_OPTIONS,
    )
    assert reparsed == document
    assert reparsed.document_fingerprint == document.document_fingerprint


def test_blank_node_labels_are_document_local_and_alpha_invariant() -> None:
    first = b"Ontology(ClassAssertion(ObjectOneOf(_:left) _:left))"
    second = b"Ontology(ClassAssertion(ObjectOneOf(_:renamed) _:renamed))"
    first_document = parse_document(first, format="functional", options=PYTHON_OPTIONS)
    second_document = parse_document(second, format="functional", options=PYTHON_OPTIONS)
    assert first_document == second_document
    assert first_document.document_fingerprint == second_document.document_fingerprint


def test_plain_literal_at_sign_is_not_reinterpreted_as_a_language_tag() -> None:
    source = b'Ontology(AnnotationAssertion(<https://e/p> <https://e/s> "email@example"))'
    document = parse_document(source, format="functional", options=PYTHON_OPTIONS)
    assertion = next(document.iter_axioms(m.AnnotationAssertion))
    assert isinstance(assertion.value, m.Literal)
    assert assertion.value.lexical_form == "email@example"
    assert assertion.value.language is None


def test_prefixed_has_key_class_is_not_mistaken_for_a_constructor() -> None:
    source = b"Prefix(:=<urn:key#>) Ontology(HasKey(:A (:r) ()))"
    document = parse_document(source, format="functional", options=PYTHON_OPTIONS)
    assert document.axioms == m.CanonicalSet(
        (
            m.HasKey(
                m.Class(m.IRI("urn:key#A")),
                m.CanonicalSet((m.ObjectProperty(m.IRI("urn:key#r")),)),
                m.CanonicalSet(),
            ),
        )
    )


def test_partial_rdf_mapping_is_expert_only_and_reported() -> None:
    source = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY}> a owl:Ontology .
<{ONTOLOGY}#s> <{ONTOLOGY}#unknown> <{ONTOLOGY}#o> .
""".encode()
    with pytest.raises(UnsupportedSyntaxError, match="not completely mappable"):
        parse_document(
            source,
            format="turtle",
            document_iri=ONTOLOGY,
            options=PYTHON_OPTIONS,
        )
    partial = PythonParser().parse(
        source,
        format="turtle",
        document_iri=ONTOLOGY,
        options=PYTHON_OPTIONS,
        allow_partial_rdf_mapping=True,
    )
    assert partial.rdf_mapping_report is not None
    assert not partial.rdf_mapping_report.conformant
    assert partial.rdf_mapping_report.total_triples == 2
    assert partial.rdf_mapping_report.consumed_triples == 1
    assert len(partial.rdf_mapping_report.unconsumed) == 1


def test_backend_and_format_option_conflicts_are_explicit() -> None:
    with (
        patch(
            "pyowl_core.backends.native.probe",
            return_value=NativeProbe(False, "test backend unavailable", None, ()),
        ),
        pytest.raises(BackendUnavailableError),
    ):
        parse_document(
            FUNCTIONAL,
            format="functional",
            options=LoadOptions(backend=BackendPreference.NATIVE),
        )
    with pytest.raises(OptionConflictError):
        parse_document(
            FUNCTIONAL,
            format="functional",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                format=DocumentFormat.TURTLE,
            ),
        )
