from __future__ import annotations

import random

import pytest

from pyowl_core import ParseLimits
from pyowl_core.backends import native
from pyowl_core.exceptions import OntologySyntaxError, PyOWLCoreError
from pyowl_core.io.formats.functional import parse_functional
from tests.native.foundation._support import load_extension


@pytest.fixture(scope="module", autouse=True)
def native_parser() -> None:
    load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")


VALID = (
    b"Ontology()",
    b"Prefix(:=<urn:x#>) Ontology(<urn:o> <urn:v> Import(<urn:i>) Declaration(Class(:C)))",
    b'Ontology(Annotation(<urn:p> "value"@en) AnnotationAssertion(<urn:p> <urn:s> "v"))',
    b"Ontology(SubClassOf(<urn:A> ObjectIntersectionOf(<urn:B> <urn:C>)))",
    b"Ontology(SubClassOf(<urn:A> ObjectMinCardinality(2 <urn:p>)))",
    b"Ontology(SubClassOf(<urn:A> DataExactCardinality(1 <urn:d>)))",
    b"Ontology(SubClassOf(<urn:A> DataSomeValuesFrom(<urn:d1> <urn:d2> <urn:D>)))",
    b"Ontology(SubObjectPropertyOf(ObjectPropertyChain(<urn:p> <urn:q>) <urn:r>))",
    b'Ontology(DataPropertyAssertion(<urn:p> _:subject "3"^^<http://www.w3.org/2001/XMLSchema#integer>))',
    b"Ontology(HasKey(<urn:C> (<urn:p> ObjectInverseOf(<urn:q>)) (<urn:d>)))",
    b"Prefix(:=<urn:key#>) Ontology(HasKey(:A (:r) ()))",
    b'Ontology(DatatypeDefinition(<urn:D> DatatypeRestriction(<urn:B> <urn:f> "1")))',
)


@pytest.mark.parametrize("source", VALID)
def test_valid_corpus_has_exact_structural_and_occurrence_parity(source: bytes) -> None:
    limits = ParseLimits()
    assert native.parse_functional(source, limits=limits) == parse_functional(
        source,
        limits=limits,
    )


def test_seeded_generated_documents_have_exact_parity() -> None:
    generator = random.Random(0x08_2026)
    constructors = (
        lambda index: f"SubClassOf(<urn:C{index}> <urn:C{index + 1}>)",
        lambda index: (
            f"SubClassOf(<urn:C{index}> "
            f"ObjectSomeValuesFrom(<urn:p{index % 11}> <urn:C{index + 1}>))"
        ),
        lambda index: (
            f"ClassAssertion(ObjectUnionOf(<urn:C{index}> <urn:C{index + 1}>) _:i{index % 17})"
        ),
        lambda index: f'DataPropertyAssertion(<urn:d{index % 7}> <urn:i{index % 13}> "{index}")',
    )
    members = [generator.choice(constructors)(index) for index in range(500)]
    source = ("Ontology(" + " ".join(members) + ")").encode()
    assert native.parse_functional(source) == parse_functional(
        source,
        limits=ParseLimits(),
    )


def test_unused_tight_prefix_and_iri_limits_preserve_reference_behavior() -> None:
    limits = ParseLimits(max_prefixes=1, max_iri_bytes=1)
    source = b"Ontology()"
    assert native.parse_functional(source, limits=limits) == parse_functional(
        source,
        limits=limits,
    )


INVALID = (
    b"",
    b"Ontology(",
    b"Ontology(Unknown(<urn:x>))",
    b"Ontology(EquivalentClasses(<urn:A>))",
    b"Ontology(ObjectPropertyAssertion(<urn:p> <urn:a>))",
    b"Ontology(DataSomeValuesFrom(<urn:datatype-only>))",
    b"Ontology(Declaration(Class(undefined:C)))",
    b'Ontology(Annotation(<urn:p> "unterminated))',
    b"Ontology(Declaration(Class(<urn:bad\\q>)))",
    b"Ontology(Declaration(Class(<urn:%GG>)))",
    b'Ontology(DataPropertyAssertion(<urn:p> <urn:i> "x"@abc-123456789))',
    b"\xffOntology()",
)


@pytest.mark.parametrize("source", INVALID)
def test_invalid_corpus_is_rejected_by_both_backends(source: bytes) -> None:
    with pytest.raises(PyOWLCoreError):
        parse_functional(source, limits=ParseLimits())
    with pytest.raises(OntologySyntaxError):
        native.parse_functional(source)
