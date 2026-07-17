from __future__ import annotations

import pytest

from pyowl_core import (
    BackendPreference,
    CancellationSource,
    DocumentFormat,
    LoadOptions,
    OntologySyntaxError,
    OperationCancelledError,
    ParseLimits,
    PythonParser,
    ResourceLimitError,
    UnsupportedSyntaxError,
    parse_document,
)


def _options(**limits: int) -> LoadOptions:
    return LoadOptions(
        backend=BackendPreference.PYTHON,
        limits=ParseLimits(**limits),
    )


@pytest.mark.parametrize("format", (DocumentFormat.OWL_XML, DocumentFormat.RDF_XML))
def test_xml_dtd_entity_and_xinclude_are_rejected(format: DocumentFormat) -> None:
    with pytest.raises(OntologySyntaxError, match="forbidden"):
        parse_document(
            b'<!DOCTYPE x [<!ENTITY e "expansion">]><x>&e;</x>',
            format=format,
            options=_options(),
        )

    include = b"""\
<Ontology xmlns="http://www.w3.org/2002/07/owl#"
 xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="file:///x"/></Ontology>
"""
    with pytest.raises(OntologySyntaxError, match="forbidden"):
        parse_document(include, format=format, options=_options())

    utf16_entity = '<!DOCTYPE x [<!ENTITY e "expansion">]><x>&e;</x>'.encode("utf-16")
    with pytest.raises(OntologySyntaxError, match="forbidden"):
        parse_document(utf16_entity, format=format, options=_options())

    utf16be_entity = '<!DOCTYPE x [<!ENTITY e "expansion">]><x>&e;</x>'.encode("utf-16-be")
    with pytest.raises(OntologySyntaxError, match="forbidden"):
        parse_document(utf16be_entity, format=format, options=_options())


@pytest.mark.parametrize("format", (DocumentFormat.OWL_XML, DocumentFormat.RDF_XML))
def test_utf16_xml_is_supported_without_weakening_hostile_defaults(
    format: DocumentFormat,
) -> None:
    source = (
        '<Ontology xmlns="http://www.w3.org/2002/07/owl#"/>'
        if format is DocumentFormat.OWL_XML
        else '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
    ).encode("utf-16")
    document = parse_document(
        source,
        format=format,
        document_iri="https://example.org/utf16",
        options=_options(),
    )
    assert document.provenance.decoded_codepoint_length == len(source.decode("utf-16"))


def test_source_literal_nesting_and_encoding_limits() -> None:
    with pytest.raises(ResourceLimitError) as source_error:
        parse_document(
            b"Ontology()",
            format="functional",
            options=_options(max_source_bytes=5),
        )
    assert source_error.value.limit == "max_source_bytes"

    literal = b'Ontology(AnnotationAssertion(<https://e/p> <https://e/s> "123456"))'
    with pytest.raises(ResourceLimitError) as literal_error:
        parse_document(
            literal,
            format="functional",
            options=_options(max_literal_bytes=5),
        )
    assert literal_error.value.limit == "max_literal_bytes"

    nested = (
        b"Ontology(SubClassOf(<https://e/C> "
        b"ObjectComplementOf(ObjectComplementOf(ObjectComplementOf(<https://e/C>)))))"
    )
    with pytest.raises(ResourceLimitError) as depth_error:
        parse_document(
            nested,
            format="functional",
            options=_options(max_nesting_depth=4),
        )
    assert depth_error.value.limit == "max_nesting_depth"

    with pytest.raises(OntologySyntaxError, match="valid UTF-8"):
        parse_document(b"Ontology(\xff)", format="functional", options=_options())


def test_malformed_and_truncated_xml_never_returns_a_document() -> None:
    with pytest.raises(OntologySyntaxError, match="malformed"):
        parse_document(
            b'<Ontology xmlns="http://www.w3.org/2002/07/owl#"><Declaration>',
            format="owlxml",
            options=_options(),
        )
    with pytest.raises(OntologySyntaxError, match="malformed"):
        parse_document(
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
            format="rdfxml",
            options=_options(),
        )


def test_pre_cancelled_parse_stops_before_producing_a_document() -> None:
    source = CancellationSource()
    source.cancel("test cancellation")
    with pytest.raises(OperationCancelledError) as error:
        PythonParser().parse(
            b"Ontology()",
            format="functional",
            options=_options(),
            cancellation_token=source.token,
        )
    assert error.value.reason == "test cancellation"


def test_cyclic_and_shared_rdf_collection_tails_are_rejected() -> None:
    cycle = b"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
<https://e/o> a owl:Ontology .
<https://e/C> owl:equivalentClass _:e .
_:e owl:oneOf _:head .
_:head rdf:first <https://e/i> ; rdf:rest _:head .
"""
    with pytest.raises(UnsupportedSyntaxError, match="cyclic RDF collection"):
        parse_document(
            cycle,
            format="turtle",
            document_iri="https://e/o",
            options=_options(),
        )

    shared = b"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
<https://e/o> a owl:Ontology .
<https://e/C> owl:equivalentClass _:e1, _:e2 .
_:e1 owl:oneOf _:h1 .
_:e2 owl:oneOf _:h2 .
_:h1 rdf:first <https://e/i1> ; rdf:rest _:tail .
_:h2 rdf:first <https://e/i2> ; rdf:rest _:tail .
_:tail rdf:first <https://e/i3> ; rdf:rest rdf:nil .
"""
    with pytest.raises(UnsupportedSyntaxError, match="shared RDF collection tail"):
        parse_document(
            shared,
            format="turtle",
            document_iri="https://e/o",
            options=_options(),
        )
