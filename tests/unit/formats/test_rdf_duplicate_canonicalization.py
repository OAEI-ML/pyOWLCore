from __future__ import annotations

import hashlib

import pytest

from pyowl_core import (
    IRI,
    OWL_NOTHING,
    RDF_PLAIN_LITERAL,
    Annotation,
    AnnotationProperty,
    BackendPreference,
    CanonicalSet,
    Class,
    DocumentFormat,
    Literal,
    LoadOptions,
    ObjectComplementOf,
    SubClassOf,
    parse_document,
)

OPTIONS = LoadOptions(backend=BackendPreference.PYTHON, preserve_source_map=True)
BASE = "https://example.org/rdf-duplicates#"
ONTOLOGY = "https://example.org/rdf-duplicates"


@pytest.mark.parametrize("format", (DocumentFormat.TURTLE, DocumentFormat.RDF_XML))
@pytest.mark.parametrize("operator", ("intersectionOf", "unionOf"))
def test_duplicate_rdf_boolean_operands_canonicalize_to_the_sole_operand(
    format: DocumentFormat,
    operator: str,
) -> None:
    source = _duplicate_boolean_source(format, operator)
    document = parse_document(source, format=format, options=OPTIONS)
    expected = SubClassOf(Class(IRI(BASE + "A")), Class(IRI(BASE + "B")))

    assert expected in document.axioms
    assert len(tuple(document.iter_axioms(SubClassOf))) == 1
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.consumed_triples == document.rdf_mapping_report.total_triples
    assert document.source_map is not None
    assert document.source_map.occurrences_for(expected)
    assert document.origin_index is not None
    assert document.origin_index.origins_for(expected)
    assert document.provenance.source_sha256 == hashlib.sha256(source).digest()
    assert document.provenance.byte_length == len(source)


def test_duplicate_complex_conjuncts_canonicalize_after_structural_mapping() -> None:
    source = f"""\
@prefix : <{BASE}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTOLOGY}> a owl:Ontology .
:A a owl:Class ; rdfs:subClassOf [
    a owl:Class ;
    owl:intersectionOf (
        [ a owl:Class ; owl:complementOf :B ]
        [ a owl:Class ; owl:complementOf :B ]
    )
] .
:B a owl:Class .
""".encode()
    document = parse_document(source, format="turtle", options=OPTIONS)
    expected = SubClassOf(
        Class(IRI(BASE + "A")),
        ObjectComplementOf(Class(IRI(BASE + "B"))),
    )

    assert expected in document.axioms
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant


@pytest.mark.parametrize("format", (DocumentFormat.TURTLE, DocumentFormat.RDF_XML))
def test_self_disjoint_rdf_axiom_canonicalizes_to_bottom_subclass(
    format: DocumentFormat,
) -> None:
    source = _self_disjoint_source(format)
    document = parse_document(source, format=format, options=OPTIONS)
    expected = SubClassOf(Class(IRI(BASE + "C")), OWL_NOTHING)

    assert expected in document.axioms
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.source_map is not None
    assert document.source_map.occurrences_for(expected)
    assert document.origin_index is not None
    assert document.origin_index.origins_for(expected)


@pytest.mark.parametrize("format", (DocumentFormat.TURTLE, DocumentFormat.RDF_XML))
def test_duplicate_all_disjoint_class_members_canonicalize_to_bottom_subclass(
    format: DocumentFormat,
) -> None:
    if format is DocumentFormat.TURTLE:
        source = f"""\
@prefix : <{BASE}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY}> a owl:Ontology .
:C a owl:Class .
[] a owl:AllDisjointClasses ; owl:members ( :C :C ) .
""".encode()
    else:
        source = f'''\
<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="{ONTOLOGY}"/>
  <owl:Class rdf:about="{BASE}C"/>
  <owl:AllDisjointClasses>
    <owl:members rdf:parseType="Collection">
      <owl:Class rdf:about="{BASE}C"/>
      <owl:Class rdf:about="{BASE}C"/>
    </owl:members>
  </owl:AllDisjointClasses>
</rdf:RDF>
'''.encode()
    document = parse_document(source, format=format, options=OPTIONS)
    expected = SubClassOf(Class(IRI(BASE + "C")), OWL_NOTHING)

    assert expected in document.axioms
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant


def test_self_disjoint_axiom_retains_rdf_axiom_annotations_and_exact_source_provenance() -> None:
    source = f'''\
@prefix : <{BASE}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTOLOGY}> a owl:Ontology .
:C a owl:Class ; owl:disjointWith :C .
[] a owl:Axiom ;
   owl:annotatedSource :C ;
   owl:annotatedProperty owl:disjointWith ;
   owl:annotatedTarget :C ;
   rdfs:comment "original disjoint spelling"@EN .
'''.encode()
    document = parse_document(source, format="turtle", options=OPTIONS)
    note = Annotation(
        AnnotationProperty(IRI("http://www.w3.org/2000/01/rdf-schema#comment")),
        Literal("original disjoint spelling", RDF_PLAIN_LITERAL, language="en"),
    )
    expected = SubClassOf(
        Class(IRI(BASE + "C")),
        OWL_NOTHING,
        CanonicalSet((note,)),
    )

    assert expected in document.axioms
    assert document.provenance.source_sha256 == hashlib.sha256(source).digest()
    assert document.provenance.byte_length == len(source)
    assert document.source_map is not None
    occurrences = document.source_map.occurrences_for(expected)
    assert len(occurrences) == 1
    assert occurrences[0].lexical["language-tag"] == "EN"


def _duplicate_boolean_source(format: DocumentFormat, operator: str) -> bytes:
    if format is DocumentFormat.TURTLE:
        return f"""\
@prefix : <{BASE}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTOLOGY}> a owl:Ontology .
:A a owl:Class ; rdfs:subClassOf [
    a owl:Class ; owl:{operator} ( :B :B )
] .
:B a owl:Class .
""".encode()
    return f'''\
<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="{ONTOLOGY}"/>
  <owl:Class rdf:about="{BASE}A">
    <rdfs:subClassOf>
      <owl:Class>
        <owl:{operator} rdf:parseType="Collection">
          <owl:Class rdf:about="{BASE}B"/>
          <owl:Class rdf:about="{BASE}B"/>
        </owl:{operator}>
      </owl:Class>
    </rdfs:subClassOf>
  </owl:Class>
  <owl:Class rdf:about="{BASE}B"/>
</rdf:RDF>
'''.encode()


def _self_disjoint_source(format: DocumentFormat) -> bytes:
    if format is DocumentFormat.TURTLE:
        return f"""\
@prefix : <{BASE}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY}> a owl:Ontology .
:C a owl:Class ; owl:disjointWith :C .
""".encode()
    return f'''\
<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="{ONTOLOGY}"/>
  <owl:Class rdf:about="{BASE}C">
    <owl:disjointWith rdf:resource="{BASE}C"/>
  </owl:Class>
</rdf:RDF>
'''.encode()
