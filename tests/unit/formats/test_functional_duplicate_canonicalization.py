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
    DataProperty,
    DataPropertyRange,
    Datatype,
    Literal,
    LoadOptions,
    StructuralConstraintError,
    SubClassOf,
    parse_document,
)

OPTIONS = LoadOptions(backend=BackendPreference.PYTHON, preserve_source_map=True)


@pytest.mark.parametrize("constructor", ("ObjectIntersectionOf", "ObjectUnionOf"))
def test_duplicate_functional_boolean_operands_canonicalize_to_the_sole_operand(
    constructor: str,
) -> None:
    source = (
        f"Ontology(SubClassOf({constructor}(<urn:A> <urn:A>) <urn:B>))"
    ).encode()
    direct = b"Ontology(SubClassOf(<urn:A> <urn:B>))"
    document = parse_document(source, format="functional", options=OPTIONS)
    canonical = parse_document(direct, format="functional", options=OPTIONS)
    expected = SubClassOf(Class(IRI("urn:A")), Class(IRI("urn:B")))

    assert document == canonical
    assert document.document_fingerprint == canonical.document_fingerprint
    assert document.axioms == CanonicalSet((expected,))
    assert document.provenance.source_sha256 == hashlib.sha256(source).digest()
    assert document.provenance.byte_length == len(source)
    assert document.source_map is not None
    occurrences = document.source_map.occurrences_for(expected)
    assert len(occurrences) == 1
    assert occurrences[0].span is not None
    assert source[occurrences[0].span.byte_start : occurrences[0].span.byte_end].startswith(
        b"SubClassOf("
    )
    assert document.origin_index is not None
    assert document.origin_index.origins_for(expected)


@pytest.mark.parametrize("constructor", ("DataIntersectionOf", "DataUnionOf"))
def test_duplicate_functional_data_operands_canonicalize_to_the_sole_operand(
    constructor: str,
) -> None:
    source = f"Ontology(DataPropertyRange(<urn:p> {constructor}(<urn:D> <urn:D>)))".encode()
    document = parse_document(source, format="functional", options=OPTIONS)

    assert document.axioms == CanonicalSet(
        (DataPropertyRange(DataProperty(IRI("urn:p")), Datatype(IRI("urn:D"))),)
    )


def test_self_disjoint_functional_axiom_canonicalizes_with_annotations_and_spelling() -> None:
    source = (
        b'Ontology(DisjointClasses('
        b'Annotation(<urn:note> "original disjoint spelling"@EN) '
        b"<urn:A> <urn:A>))"
    )
    note = Annotation(
        AnnotationProperty(IRI("urn:note")),
        Literal("original disjoint spelling", RDF_PLAIN_LITERAL, language="en"),
    )
    expected = SubClassOf(
        Class(IRI("urn:A")),
        OWL_NOTHING,
        CanonicalSet((note,)),
    )
    document = parse_document(source, format="functional", options=OPTIONS)

    assert document.axioms == CanonicalSet((expected,))
    assert document.source_map is not None
    occurrence = document.source_map.occurrences_for(expected)[0]
    assert occurrence.lexical["language-tag"] == "EN"
    assert document.provenance.source_sha256 == hashlib.sha256(source).digest()


@pytest.mark.parametrize(
    "source",
    (
        b"Ontology(SubClassOf(ObjectIntersectionOf(<urn:A>) <urn:B>))",
        b"Ontology(SubClassOf(ObjectUnionOf(<urn:A>) <urn:B>))",
        b"Ontology(DisjointClasses(<urn:A>))",
    ),
)
def test_true_singleton_functional_collections_remain_invalid(source: bytes) -> None:
    with pytest.raises(StructuralConstraintError):
        parse_document(source, format="functional", options=OPTIONS)
