from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, cast

import pytest

from pyowl_core import IRI, ParseLimits
from pyowl_core.backends import native
from pyowl_core.io.formats.rdfxml import parse_rdfxml
from pyowl_core.model import canonical_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension


@dataclass(frozen=True, slots=True)
class _Observation:
    decoded_codepoints: int
    total_triples: int
    consumed_triples: int
    ontology_iri: str | None
    version_iri: str | None
    imports: tuple[str, ...]
    axioms: tuple[bytes, ...]


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    if not hasattr(selected, "_ingest_rdfxml_slice_v1"):
        pytest.skip("selected native artifact lacks the WP16 RDF/XML test hook")
    return selected


def _ingest(
    extension: NativeTestExtension,
    source: object,
    *,
    document_iri: object | None = None,
    limits: ParseLimits | None = None,
    cancel: object | None = None,
) -> tuple[object, _Observation]:
    selected = ParseLimits() if limits is None else limits
    config = cast(Any, native)._encode_config(selected, None, verify=False)
    owner, encoded = cast(Any, extension)._ingest_rdfxml_slice_v1(
        source,
        document_iri,
        config,
        cancel,
    )
    return owner, _decode_observation(encoded)


def _decode_observation(data: bytes) -> _Observation:
    if not isinstance(data, bytes) or len(data) < 36:
        raise AssertionError("truncated RDF/XML observation")
    magic, schema, flags = struct.unpack_from("<8sHH", data)
    if (magic, schema, flags) != (b"PYRXOBS1", 1, 0):
        raise AssertionError("invalid RDF/XML observation header")
    decoded, total, consumed = struct.unpack_from("<QQQ", data, 12)
    offset = 36

    def frame() -> bytes:
        nonlocal offset
        (size,) = struct.unpack_from("<Q", data, offset)
        offset += 8
        end = offset + size
        value = data[offset:end]
        if len(value) != size:
            raise AssertionError("truncated RDF/XML observation frame")
        offset = end
        return value

    def optional() -> str | None:
        nonlocal offset
        present = data[offset]
        offset += 1
        if present == 0:
            return None
        if present != 1:
            raise AssertionError("invalid RDF/XML observation optional marker")
        return frame().decode()

    ontology_iri = optional()
    version_iri = optional()
    (import_count,) = struct.unpack_from("<Q", data, offset)
    offset += 8
    imports = tuple(frame().decode() for _ in range(import_count))
    (axiom_count,) = struct.unpack_from("<Q", data, offset)
    offset += 8
    axioms = tuple(frame() for _ in range(axiom_count))
    if offset != len(data):
        raise AssertionError("trailing RDF/XML observation bytes")
    return _Observation(
        decoded,
        total,
        consumed,
        ontology_iri,
        version_iri,
        imports,
        axioms,
    )


def test_supported_slice_matches_python_mapping_and_crosses_v1_freeze(
    extension: NativeTestExtension,
) -> None:
    source = b"""<?xml version='1.0' encoding='UTF-8'?>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Ontology rdf:about='urn:ontology'>
  <owl:versionIRI rdf:resource='urn:version'/>
  <owl:imports rdf:resource='urn:z-import'/>
  <owl:imports rdf:resource='urn:a-import'/>
 </owl:Ontology>
 <owl:Class rdf:about='urn:C'/>
 <rdfs:Datatype rdf:about='urn:D'/>
 <owl:ObjectProperty rdf:about='urn:op'/>
 <owl:DatatypeProperty rdf:about='urn:dp'/>
 <owl:AnnotationProperty rdf:about='urn:ap'/>
 <owl:NamedIndividual rdf:about='urn:i'/>
</rdf:RDF>"""
    owner, observed = _ingest(extension, source, document_iri="urn:document")
    python = parse_rdfxml(
        source,
        limits=ParseLimits(),
        document_iri=IRI("urn:document"),
    )
    assert python.rdf_mapping_report is not None
    assert python.ontology_id.ontology_iri is not None
    assert python.ontology_id.version_iri is not None

    assert observed.ontology_iri == python.ontology_id.ontology_iri.value
    assert observed.version_iri == python.ontology_id.version_iri.value
    assert observed.imports == tuple(value.value for value in python.imports)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples
    assert observed.total_triples == python.rdf_mapping_report.total_triples
    assert observed.decoded_codepoints == python.decoded_codepoint_length

    attestation = cast(Any, owner)._publication_attestation_v1()
    assert attestation.stored_axiom_count == len(python.axioms)
    assert attestation.total_source_bytes == len(source)
    assert attestation.rdf_mapping_report_count == 1
    assert extension.INGESTION_FEATURES == ()
    assert "parse-rdfxml-v1" not in extension.FEATURES


def test_named_node_axiom_slice_matches_python_canonical_bytes(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>
  <rdfs:subClassOf rdf:resource='urn:D'/>
  <owl:equivalentClass rdf:resource='urn:E'/>
  <owl:disjointWith rdf:resource='urn:F'/>
 </owl:Class>
 <owl:Class rdf:about='urn:D'/>
 <owl:Class rdf:about='urn:E'/>
 <owl:Class rdf:about='urn:F'/>
 <owl:NamedIndividual rdf:about='urn:i'>
  <rdf:type rdf:resource='urn:C'/>
 </owl:NamedIndividual>
 <owl:ObjectProperty rdf:about='urn:op'>
  <rdfs:subPropertyOf rdf:resource='urn:oq'/>
  <owl:equivalentProperty rdf:resource='urn:oe'/>
  <owl:propertyDisjointWith rdf:resource='urn:od'/>
  <owl:inverseOf rdf:resource='urn:oi'/>
  <rdfs:domain rdf:resource='urn:C'/>
  <rdfs:range rdf:resource='urn:D'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#FunctionalProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#InverseFunctionalProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#ReflexiveProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#IrreflexiveProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#SymmetricProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#AsymmetricProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#TransitiveProperty'/>
 </owl:ObjectProperty>
 <owl:ObjectProperty rdf:about='urn:oq'/>
 <owl:ObjectProperty rdf:about='urn:oe'/>
 <owl:ObjectProperty rdf:about='urn:od'/>
 <owl:ObjectProperty rdf:about='urn:oi'/>
 <owl:DatatypeProperty rdf:about='urn:dp'>
  <rdfs:subPropertyOf rdf:resource='urn:dq'/>
  <owl:equivalentProperty rdf:resource='urn:de'/>
  <owl:propertyDisjointWith rdf:resource='urn:dd'/>
  <rdfs:domain rdf:resource='urn:C'/>
  <rdfs:range rdf:resource='http://www.w3.org/2001/XMLSchema#string'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#FunctionalProperty'/>
 </owl:DatatypeProperty>
 <owl:DatatypeProperty rdf:about='urn:dq'/>
 <owl:DatatypeProperty rdf:about='urn:de'/>
 <owl:DatatypeProperty rdf:about='urn:dd'/>
</rdf:RDF>"""
    owner, observed = _ingest(extension, source, document_iri="urn:document")
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=IRI("urn:document"))
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == python.rdf_mapping_report.total_triples
    assert observed.consumed_triples == python.rdf_mapping_report.consumed_triples
    assert observed.total_triples == observed.consumed_triples
    attestation = cast(Any, owner)._publication_attestation_v1()
    assert attestation.stored_axiom_count == len(python.axioms)


def test_parse_type_resource_class_expression_matches_python_canonical_bytes(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:A'>
  <rdfs:subClassOf rdf:parseType='Resource'>
   <owl:intersectionOf rdf:parseType='Collection'>
    <rdf:Description rdf:about='urn:B'/>
    <rdf:Description rdf:about='urn:C'/>
   </owl:intersectionOf>
  </rdfs:subClassOf>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_triples=2))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_node_property_attributes_match_python_types_and_language_literals(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <rdf:Description rdf:about='urn:attribute:C'
  rdf:type='http://www.w3.org/2002/07/owl#Class'
  xml:lang='EN' rdfs:label='Class label'/>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_literal_bytes=4))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_unicode_xml_qnames_match_python_mapping(
    extension: NativeTestExtension,
) -> None:
    source = """<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:π='urn:unicode:'>
 <owl:AnnotationProperty rdf:about='urn:unicode:qualité'/>
 <owl:AnnotationProperty rdf:about='urn:unicode:étiquette'/>
 <owl:Class rdf:about='urn:C' π:qualité='élevée'>
  <π:étiquette xml:lang='FR'>café</π:étiquette>
 </owl:Class>
</rdf:RDF>""".encode()

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 5
    assert observed.total_triples == python.rdf_mapping_report.total_triples
    assert observed.decoded_codepoints == python.decoded_codepoint_length

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_literal_bytes=6))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_xml_text_cdata_and_attribute_normalization_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = (
        b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        b"xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        b"xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:x='urn:x:'>"
        b"<owl:Class rdf:about='urn:C' rdfs:label='a\tb\r\nc&#10;d&#9;e'>"
        b"<rdfs:comment>one\r\n<![CDATA[two\rthree]]>&#13;four</rdfs:comment>"
        b"<rdfs:comment rdf:parseType='Literal'>one\r\n"
        b"<x:a v='a\tb\r\nc&#10;d&#9;e'>two\rthree&#13;four</x:a>tail\r\n"
        b"</rdfs:comment>"
        b"</owl:Class></rdf:RDF>"
    )

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 4
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_literal_bytes=12))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_empty_property_attributes_match_python_object_descriptions(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:e='urn:e:'>
 <owl:ObjectProperty rdf:about='urn:e:p'/>
 <owl:NamedIndividual rdf:about='urn:i'/>
 <rdf:Description rdf:about='urn:i'>
  <e:p rdf:resource='urn:j'
   rdf:type='http://www.w3.org/2002/07/owl#NamedIndividual'
   rdfs:label='Target'/>
 </rdf:Description>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 5
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as literal_limited:
        _ingest(extension, source, limits=ParseLimits(max_literal_bytes=5))
    assert literal_limited.value.args[0] == "NATIVE_WIRE_LIMIT"
    with pytest.raises(extension._NativeError) as triple_limited:
        _ingest(extension, source, limits=ParseLimits(max_triples=4))
    assert triple_limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_rdf_li_expansion_matches_python_annotation_properties(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:AnnotationProperty
  rdf:about='http://www.w3.org/1999/02/22-rdf-syntax-ns#_1'/>
 <owl:AnnotationProperty
  rdf:about='http://www.w3.org/1999/02/22-rdf-syntax-ns#_2'/>
 <owl:Class rdf:about='urn:C'>
  <rdf:li>first</rdf:li>
  <rdf:li xml:lang='EN'>second</rdf:li>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 5
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_triples=4))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_markup_free_parse_type_literal_matches_python_xml_literal(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xml:lang='EN'>
 <owl:Class rdf:about='urn:C'>
  <rdfs:comment rdf:parseType='Literal'>a &amp; <![CDATA[b < c]]></rdfs:comment>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_literal_bytes=8))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_nested_parse_type_literal_matches_python_element_tree_serialization(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:x='urn:x:' xmlns:y='urn:y:'>
 <owl:Class rdf:about='urn:C'>
  <rdfs:comment rdf:parseType='Literal'>root<x:box z='2' a='1' xml:base='../'><y:item
   x:attr='&quot;'>hi &amp;</y:item>tail&lt;</x:box>between<x:empty/>suffix</rdfs:comment>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_literal_bytes=64))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_rfc3986_document_and_nested_xml_bases_match_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xml:base='../onto/'>
 <owl:Ontology rdf:about='?rev=1#ontology'>
  <owl:versionIRI xml:base='./versions/v1/' rdf:resource='../current?x#f'/>
  <owl:imports rdf:resource='../imports/a.owl'/>
 </owl:Ontology>
 <owl:Class rdf:about='./C#c'/>
 <owl:Class rdf:about='nested/../D'>
  <rdfs:subClassOf rdf:resource='./C#c'/>
 </owl:Class>
</rdf:RDF>"""
    document_iri = "http://example.test/root/doc.owl"
    _, observed = _ingest(extension, source, document_iri=document_iri)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=IRI(document_iri))
    assert python.rdf_mapping_report is not None
    assert python.ontology_id.ontology_iri is not None
    assert python.ontology_id.version_iri is not None

    assert observed.ontology_iri == python.ontology_id.ontology_iri.value
    assert observed.version_iri == python.ontology_id.version_iri.value
    assert observed.imports == tuple(value.value for value in python.imports)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == python.rdf_mapping_report.total_triples
    assert observed.consumed_triples == python.rdf_mapping_report.consumed_triples


@pytest.mark.parametrize(
    ("source", "code"),
    (
        (
            b"<!DOCTYPE rdf:RDF [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><rdf:RDF/>",
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        ),
        (
            b"<r:RDF xmlns:r='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>&x;</r:RDF>",
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:a='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:b='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
            b"<rdf:Description a:about='urn:a' b:about='urn:b'/></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
            b"<rdf:about/></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:'><rdf:Description rdf:about='urn:s'>"
            b"<e:p rdf:resource='urn:o'/></rdf:Description></rdf:RDF>",
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:' xmlns:xi='http://www.w3.org/2001/XInclude'>"
            b"<rdf:Description rdf:about='urn:s'>"
            b"<e:p rdf:parseType='Literal'><xi:include href='other.xml'/></e:p>"
            b"</rdf:Description></rdf:RDF>",
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:'><rdf:Description rdf:about='urn:s'>"
            b"<e:bad:name/></rdf:Description></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:'><rdf:Description rdf:about='urn:s'>"
            b"<e:p rdf:nodeID=''/></rdf:Description></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:'><rdf:Description rdf:about='urn:s'>"
            b"<e:p>bad\x01</e:p></rdf:Description></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:'><rdf:Description rdf:about='urn:s'>"
            b"<e:p>bad]]></e:p></rdf:Description></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
    ),
)
def test_hostile_or_incomplete_input_publishes_no_owner(
    extension: NativeTestExtension,
    source: bytes,
    code: str,
) -> None:
    with pytest.raises(extension._NativeError) as raised:
        _ingest(extension, source)
    assert raised.value.args[0] == code


def test_boundary_temporary_limit_and_precancellation_fail_before_publication(
    extension: NativeTestExtension,
) -> None:
    source = (
        b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
        b"</rdf:RDF>"
    )
    limits = ParseLimits(max_temporary_bytes=len(source) * 3 - 1)
    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, memoryview(source), limits=limits)
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"

    cancellation = extension._Cancellation(None)
    cancellation.cancel()
    with pytest.raises(extension._NativeError) as cancelled:
        _ingest(extension, memoryview(source), cancel=cancellation)
    assert cancelled.value.args[0] == "NATIVE_CANCELLED"


@pytest.mark.parametrize("document_iri", (True, b"urn:document"))
def test_document_iri_requires_an_exact_string(
    extension: NativeTestExtension,
    document_iri: object,
) -> None:
    with pytest.raises(TypeError, match="document_iri must be an exact str or None"):
        _ingest(extension, b"<rdf:RDF/>", document_iri=document_iri)


def test_document_iri_is_bounded_before_native_copy(
    extension: NativeTestExtension,
) -> None:
    with pytest.raises(extension._NativeError) as raised:
        _ingest(
            extension,
            b"<rdf:RDF/>",
            document_iri="urn:oversized",
            limits=ParseLimits(max_iri_bytes=4),
        )
    assert raised.value.args[0] == "NATIVE_WIRE_LIMIT"


@pytest.mark.parametrize(
    ("source", "document_iri", "code"),
    (
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xml:base='relative/'/>",
            None,
            "NATIVE_RDFXML_RELATIVE_IRI_NO_BASE",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xml:base='1:invalid'/>",
            "http://example.test/base",
            "NATIVE_RDFXML_IRI_REFERENCE",
        ),
    ),
)
def test_invalid_base_resolution_fails_closed(
    extension: NativeTestExtension,
    source: bytes,
    document_iri: str | None,
    code: str,
) -> None:
    with pytest.raises(extension._NativeError) as raised:
        _ingest(extension, source, document_iri=document_iri)
    assert raised.value.args[0] == code


def test_resolved_iri_is_limited_before_growth(extension: NativeTestExtension) -> None:
    document_iri = "http://e/a"
    source = (
        b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        b"xml:base='relative'/>")
    with pytest.raises(extension._NativeError) as raised:
        _ingest(
            extension,
            source,
            document_iri=document_iri,
            limits=ParseLimits(max_iri_bytes=len(document_iri)),
        )
    assert raised.value.args[0] == "NATIVE_WIRE_LIMIT"
