from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, cast

import pytest

from pyowl_core import (
    IRI,
    OntologySyntaxError,
    ParseLimits,
    ResourceLimitError,
    UnsupportedSyntaxError,
)
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


SWRL_SOURCE = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:swrl='http://www.w3.org/2003/11/swrl#'
 xmlns:e='urn:'>
 <swrl:Variable rdf:about='urn:x'/>
 <swrl:Variable rdf:about='urn:y'/>
 <swrl:Imp rdf:nodeID='rule'>
  <swrl:body rdf:parseType='Collection'>
   <swrl:ClassAtom>
    <swrl:classPredicate rdf:resource='urn:C'/>
    <swrl:argument1 rdf:resource='urn:x'/>
   </swrl:ClassAtom>
   <swrl:DataRangeAtom>
    <swrl:dataRange rdf:resource='urn:D'/>
    <swrl:argument1 rdf:resource='urn:y'/>
   </swrl:DataRangeAtom>
   <swrl:IndividualPropertyAtom>
    <swrl:propertyPredicate>
     <rdf:Description><owl:inverseOf rdf:resource='urn:p'/></rdf:Description>
    </swrl:propertyPredicate>
    <swrl:argument1 rdf:resource='urn:x'/>
    <swrl:argument2 rdf:resource='urn:i'/>
   </swrl:IndividualPropertyAtom>
   <swrl:DatavaluedPropertyAtom>
    <swrl:propertyPredicate rdf:resource='urn:d'/>
    <swrl:argument1 rdf:resource='urn:x'/>
    <swrl:argument2
     rdf:datatype='http://www.w3.org/2001/XMLSchema#integer'>007</swrl:argument2>
   </swrl:DatavaluedPropertyAtom>
   <swrl:BuiltinAtom>
    <swrl:builtin rdf:resource='urn:lessThan'/>
    <swrl:arguments rdf:parseType='Collection'>
     <rdf:Description rdf:about='urn:x'/>
     <rdf:Description rdf:about='urn:y'/>
    </swrl:arguments>
   </swrl:BuiltinAtom>
   <swrl:SameIndividualAtom>
    <swrl:argument1 rdf:resource='urn:x'/>
    <swrl:argument2 rdf:resource='urn:i'/>
   </swrl:SameIndividualAtom>
  </swrl:body>
  <swrl:head rdf:parseType='Collection'>
   <swrl:DifferentIndividualsAtom>
    <swrl:argument1 rdf:resource='urn:x'/>
    <swrl:argument2 rdf:resource='urn:j'/>
   </swrl:DifferentIndividualsAtom>
  </swrl:head>
  <e:note rdf:resource='urn:value'/>
 </swrl:Imp>
</rdf:RDF>"""


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


def test_valid_xml_declaration_and_explicit_xml_binding_match_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<?xml version = '1.0' encoding='UTF-8' standalone = "yes"?>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:xml='http://www.w3.org/XML/1998/namespace' xmlns=''>
 <owl:Class rdf:about='urn:C'>
  <rdfs:label xml:lang='EN'>Class</rdfs:label>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples


@pytest.mark.parametrize("declaration_encoding", ("UTF-8", "UTF8", "US-ASCII"))
def test_utf8_declaration_aliases_match_python(
    extension: NativeTestExtension,
    declaration_encoding: str,
) -> None:
    source = f"""<?xml version='1.0' encoding='{declaration_encoding}'?>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'><rdfs:label>café</rdfs:label></owl:Class>
</rdf:RDF>""".encode()

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples
    assert observed.decoded_codepoints == python.decoded_codepoint_length


@pytest.mark.parametrize(
    ("source", "code"),
    (
        (
            b"<?xml encoding='UTF-8'?><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "RDFXML_SYNTAX",
        ),
        (
            b"<?xml version='1.1'?><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "RDFXML_SYNTAX",
        ),
        (
            b"<?xml version='1.0' standalone='yes' encoding='UTF-8'?><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "RDFXML_SYNTAX",
        ),
        (
            b"<?xml version='1.0' encoding='ISO-8859-1'?><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "XML_FORBIDDEN_CONSTRUCT",
        ),
        (
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'.encode(
                "utf-32"
            ),
            "FORMAT_ENCODING",
        ),
        (b"\xff\xfe\x00\xd8", "FORMAT_ENCODING"),
        (b"\xff\xfe<\x00x", "FORMAT_ENCODING"),
    ),
)
def test_xml_envelope_failures_match_python(
    extension: NativeTestExtension,
    source: bytes,
    code: str,
) -> None:
    with pytest.raises(OntologySyntaxError) as python_raised:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_raised.value.code == code

    with pytest.raises(extension._NativeError) as native_raised:
        _ingest(extension, source)
    assert native_raised.value.args[0] == "NATIVE_" + code


@pytest.mark.parametrize(
    ("little_endian", "bom", "declaration_encoding"),
    (
        (True, True, "UTF-16"),
        (False, True, "UTF-16"),
        (True, False, "UTF-16LE"),
        (False, False, "UTF-16BE"),
    ),
)
def test_utf16_endianness_and_bom_match_python(
    extension: NativeTestExtension,
    little_endian: bool,
    bom: bool,
    declaration_encoding: str,
) -> None:
    text = f"""<?xml version='1.0' encoding='{declaration_encoding}'?>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'><rdfs:label>café 🙂</rdfs:label></owl:Class>
</rdf:RDF>"""
    byteorder = "le" if little_endian else "be"
    prefix = (b"\xff\xfe" if little_endian else b"\xfe\xff") if bom else b""
    source = prefix + text.encode(f"utf-16-{byteorder}")

    owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.decoded_codepoints == len(text)
    assert observed.decoded_codepoints == python.decoded_codepoint_length
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples
    attestation = cast(Any, owner)._publication_attestation_v1()
    assert attestation.total_source_bytes == len(source)


def test_utf16_forbidden_construct_fails_before_publication(
    extension: NativeTestExtension,
) -> None:
    text = """<!DOCTYPE rdf:RDF [<!ENTITY x SYSTEM 'file:///etc/passwd'>]>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>"""
    source = b"\xff\xfe" + text.encode("utf-16-le")
    with pytest.raises(extension._NativeError) as raised:
        _ingest(extension, source)
    assert raised.value.args[0] == "NATIVE_XML_FORBIDDEN_CONSTRUCT"


@pytest.mark.parametrize(
    "class_element",
    (
        "<owl:Class rdf:about='urn:C' rdfs:label='&external;'/>",
        "<owl:Class rdf:about='urn:C' rdfs:label='&entité;'/>",
        (
            "<owl:Class rdf:about='urn:C'>"
            "<rdfs:label>&external;</rdfs:label>"
            "</owl:Class>"
        ),
    ),
)
def test_unknown_entity_references_fail_in_both_backends(
    extension: NativeTestExtension,
    class_element: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'>"
        f"{class_element}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "XML_FORBIDDEN_CONSTRUCT"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_XML_FORBIDDEN_CONSTRUCT"


@pytest.mark.parametrize(
    "class_element",
    (
        "<owl:Class rdf:about='urn:C' rdfs:label='&external'/>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&amp</rdfs:label></owl:Class>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&1bad;</rdfs:label></owl:Class>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&bad name;</rdfs:label></owl:Class>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&;</rdfs:label></owl:Class>",
    ),
)
def test_malformed_entity_references_fail_as_syntax_in_both_backends(
    extension: NativeTestExtension,
    class_element: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'>"
        f"{class_element}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


def test_forbidden_keywords_in_xml_data_regions_match_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<?audit <!DOCTYPE inert>?>
<!-- <!ENTITY inert> -->
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>
  <rdfs:label><![CDATA[<!DOCTYPE inert>]]></rdfs:label>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples


@pytest.mark.parametrize(
    "document",
    (
        (
            "<rdf:RDF {namespaces}>text"
            "<owl:Class rdf:about='urn:C'/>"
            "</rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C'/>tail"
            "</rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C'/>between"
            "<owl:Class rdf:about='urn:D'/>"
            "</rdf:RDF>"
        ),
        "<owl:Class {namespaces} rdf:about='urn:C'>text</owl:Class>",
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C'>"
            "<rdfs:label>value</rdfs:label>tail"
            "</owl:Class>"
            "</rdf:RDF>"
        ),
    ),
)
def test_root_and_node_character_data_fail_in_both_backends(
    extension: NativeTestExtension,
    document: str,
) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "document",
    (
        "<rdf:RDF {namespaces} e:ignored='value'/>",
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:subClassOf rdf:parseType='Resource' e:ignored='value'/>"
            "</owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<owl:equivalentClass rdf:parseType='Collection' e:ignored='value'/>"
            "</owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:label rdf:parseType='Literal' e:ignored='value'>text</rdfs:label>"
            "</owl:Class></rdf:RDF>"
        ),
    ),
)
def test_root_and_parse_type_attributes_fail_in_both_backends(
    extension: NativeTestExtension,
    document: str,
) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:e='urn:e:'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


@pytest.mark.parametrize(
    ("role", "local"),
    [
        *[
            ("node", local)
            for local in (
                "RDF",
                "ID",
                "about",
                "parseType",
                "resource",
                "nodeID",
                "datatype",
                "li",
                "aboutEach",
                "aboutEachPrefix",
                "bagID",
            )
        ],
        *[
            ("property", local)
            for local in (
                "RDF",
                "ID",
                "about",
                "parseType",
                "resource",
                "nodeID",
                "datatype",
                "Description",
                "aboutEach",
                "aboutEachPrefix",
                "bagID",
            )
        ],
    ],
)
def test_reserved_names_fail_in_element_roles_in_both_backends(
    extension: NativeTestExtension,
    role: str,
    local: str,
) -> None:
    content = (
        f"<rdf:{local}/>"
        if role == "node"
        else f"<rdf:Description rdf:about='urn:s'><rdf:{local}/></rdf:Description>"
    )
    source = (
        f"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>{content}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "document",
    (
        "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'><label/></owl:Class></rdf:RDF>",
        "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C' label='value'/></rdf:RDF>",
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:label note='value'/></owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces} xmlns='urn:default:'>"
            "<owl:Class rdf:about='urn:C' label='value'/></rdf:RDF>"
        ),
        "<rdf:RDF {namespaces}><owl:1Class/></rdf:RDF>",
        "<rdf:RDF {namespaces}><owl:Class owl:1label='value'/></rdf:RDF>",
        "<rdf:RDF {namespaces} xmlns:1bad='urn:bad:'/>",
    ),
)
def test_namespace_and_qname_errors_fail_as_syntax_in_both_backends(
    extension: NativeTestExtension,
    document: str,
) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


def test_namespace_declaration_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:e='urn:first:'>
 <owl:Class xmlns:e='urn:second:' rdf:about='urn:C'/>
</rdf:RDF>"""
    boundary = ParseLimits(max_prefixes=4)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 1

    limited = ParseLimits(max_prefixes=3)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_prefixes"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


@pytest.mark.parametrize(
    ("document", "expanded_iri_bytes"),
    (
        ("<rdf:RDF {namespace}/>", 46),
        ("<rdf:Description {namespace} rdf:about='urn:C'/>", 54),
    ),
)
def test_reserved_element_iri_limits_match_python(
    extension: NativeTestExtension,
    document: str,
    expanded_iri_bytes: int,
) -> None:
    source = document.format(
        namespace="xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'"
    ).encode()
    boundary = ParseLimits(max_iri_bytes=expanded_iri_bytes)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))

    limited = ParseLimits(max_iri_bytes=expanded_iri_bytes - 1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_iri_bytes"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_generated_membership_iri_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    members = "".join("<rdf:li rdf:resource='urn:o'/>" for _ in range(100))
    source = f"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:e="urn:e:">
  <e:C rdf:about="urn:s">{members}</e:C>
</rdf:RDF>
""".encode()

    boundary = ParseLimits(max_iri_bytes=47)
    python = parse_rdfxml(
        source,
        limits=boundary,
        document_iri=None,
        allow_partial_rdf_mapping=True,
    )
    assert python.rdf_mapping_report is not None
    assert python.rdf_mapping_report.total_triples == 101
    with pytest.raises(extension._NativeError) as incomplete:
        _ingest(extension, source, limits=boundary)
    assert incomplete.value.args[0] == "NATIVE_RDF_MAPPING_INCOMPLETE"

    limited = ParseLimits(max_iri_bytes=46)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(
            source,
            limits=limited,
            document_iri=None,
            allow_partial_rdf_mapping=True,
        )
    assert python_error.value.limit == "max_iri_bytes"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_canonical_model_nesting_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>
  <owl:equivalentClass rdf:nodeID='x1'/>
 </owl:Class>
 <owl:Class rdf:nodeID='x1'><owl:complementOf rdf:nodeID='x2'/></owl:Class>
 <owl:Class rdf:nodeID='x2'><owl:complementOf rdf:nodeID='x3'/></owl:Class>
 <owl:Class rdf:nodeID='x3'><owl:complementOf rdf:resource='urn:D'/></owl:Class>
</rdf:RDF>"""
    boundary = ParseLimits(max_nesting_depth=5)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))

    limited = ParseLimits(max_nesting_depth=4)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_nesting_depth"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_distinct_axiom_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'/>
 <owl:Class rdf:about='urn:D'/>
</rdf:RDF>"""
    boundary = ParseLimits(max_axioms=2)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert len(observed.axioms) == 2

    limited = ParseLimits(max_axioms=1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_axioms"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_raw_rdf_list_sequence_arity_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>
  <rdfs:subClassOf>
   <owl:Class>
    <owl:oneOf rdf:parseType='Collection'>
     <rdf:Description rdf:about='urn:i'/>
     <rdf:Description rdf:about='urn:i'/>
    </owl:oneOf>
   </owl:Class>
  </rdfs:subClassOf>
 </owl:Class>
</rdf:RDF>"""
    boundary = ParseLimits(max_sequence_arity=2)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))

    limited = ParseLimits(max_sequence_arity=1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_sequence_arity"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


@pytest.mark.parametrize(
    ("operator", "members"),
    (
        ("intersectionOf", ""),
        ("unionOf", ""),
        ("oneOf", ""),
        ("intersectionOf", "<owl:Class rdf:about='urn:B'/>"),
        ("unionOf", "<owl:Class rdf:about='urn:B'/>"),
    ),
)
def test_owl1_class_list_compatibility_matches_python(
    extension: NativeTestExtension,
    operator: str,
    members: str,
) -> None:
    source = f"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:A'>
  <rdfs:subClassOf>
   <owl:Class><owl:{operator} rdf:parseType='Collection'>
    {members}
   </owl:{operator}></owl:Class>
  </rdfs:subClassOf>
 </owl:Class>
</rdf:RDF>""".encode()

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples


def test_unmarked_empty_class_list_rejection_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:A'>
  <rdfs:subClassOf>
   <rdf:Description>
    <owl:intersectionOf
     rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>
   </rdf:Description>
  </rdfs:subClassOf>
 </owl:Class>
</rdf:RDF>"""

    with pytest.raises(UnsupportedSyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDF_MAPPING_UNSUPPORTED"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDF_MAPPING_UNSUPPORTED"


@pytest.mark.parametrize(
    "constructor",
    (
        "<owl:complementOf rdf:resource='urn:A'/>",
        (
            "<owl:intersectionOf "
            "rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>"
        ),
        (
            "<owl:unionOf rdf:parseType='Collection'>"
            "<owl:Class rdf:about='urn:A'/>"
            "</owl:unionOf>"
        ),
        (
            "<owl:oneOf "
            "rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>"
        ),
        (
            "<owl:oneOf rdf:parseType='Collection'>"
            "<owl:NamedIndividual rdf:about='urn:i'/>"
            "</owl:oneOf>"
        ),
    ),
)
def test_owl1_named_class_constructors_match_python(
    extension: NativeTestExtension,
    constructor: str,
) -> None:
    source = f"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>{constructor}</owl:Class>
</rdf:RDF>""".encode()

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples


def test_anonymous_owl1_named_enumeration_rejection_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>
  <owl:oneOf rdf:parseType='Collection'>
   <rdf:Description rdf:nodeID='anonymous'/>
  </owl:oneOf>
 </owl:Class>
</rdf:RDF>"""

    with pytest.raises(UnsupportedSyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDF_MAPPING_UNSUPPORTED"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDF_MAPPING_UNSUPPORTED"


@pytest.mark.parametrize(
    "body",
    (
        (
            "<owl:Class rdf:about='urn:C'>"
            "<rdf:type rdf:resource='http://www.w3.org/2000/01/rdf-schema#Class'/>"
            "</owl:Class>"
        ),
        (
            "<owl:ObjectProperty rdf:about='urn:p'>"
            "<rdf:type "
            "rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#Property'/>"
            "</owl:ObjectProperty>"
        ),
    ),
)
def test_redundant_owl1_types_match_python(
    extension: NativeTestExtension,
    body: str,
) -> None:
    source = f"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>{body}</rdf:RDF>""".encode()

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert len(observed.axioms) == 1
    assert observed.total_triples == observed.consumed_triples == 2


@pytest.mark.parametrize(
    "legacy_type",
    (
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
        "http://www.w3.org/2000/01/rdf-schema#Class",
    ),
)
def test_standalone_owl1_structural_type_rejection_matches_python(
    extension: NativeTestExtension,
    legacy_type: str,
) -> None:
    source = f"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='urn:value'>
  <rdf:type rdf:resource='{legacy_type}'/>
 </rdf:Description>
</rdf:RDF>""".encode()

    with pytest.raises(UnsupportedSyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDF_MAPPING_INCOMPLETE"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDF_MAPPING_INCOMPLETE"


def test_empty_owl1_data_range_matches_python(extension: NativeTestExtension) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:DatatypeProperty rdf:about='urn:d'/>
 <rdf:Description rdf:about='urn:A'>
  <rdfs:subClassOf>
   <owl:Restriction>
    <owl:onProperty rdf:resource='urn:d'/>
    <owl:allValuesFrom>
     <owl:DataRange>
      <rdf:type rdf:resource='http://www.w3.org/2000/01/rdf-schema#Class'/>
      <owl:oneOf rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>
     </owl:DataRange>
    </owl:allValuesFrom>
   </owl:Restriction>
  </rdfs:subClassOf>
 </rdf:Description>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples


def test_empty_modern_data_enumeration_rejection_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:DatatypeProperty rdf:about='urn:d'/>
 <rdf:Description rdf:about='urn:A'>
  <rdfs:subClassOf>
   <owl:Restriction>
    <owl:onProperty rdf:resource='urn:d'/>
    <owl:allValuesFrom>
     <rdfs:Datatype>
      <owl:oneOf rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>
     </rdfs:Datatype>
    </owl:allValuesFrom>
   </owl:Restriction>
  </rdfs:subClassOf>
 </rdf:Description>
</rdf:RDF>"""

    with pytest.raises(UnsupportedSyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDF_MAPPING_UNSUPPORTED"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDF_MAPPING_UNSUPPORTED"


def test_owl1_declarations_characteristics_and_deprecation_match_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>
  <rdf:type rdf:resource='http://www.w3.org/2000/01/rdf-schema#Class'/>
 </owl:Class>
 <owl:ObjectProperty rdf:about='urn:p'>
  <rdf:type rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#Property'/>
 </owl:ObjectProperty>
 <owl:OntologyProperty rdf:about='urn:ap'>
  <rdf:type rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#Property'/>
 </owl:OntologyProperty>
 <rdf:Description rdf:about='urn:inverse'>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#InverseFunctionalProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#Property'/>
 </rdf:Description>
 <rdf:Description rdf:about='urn:symmetric'>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#SymmetricProperty'/>
 </rdf:Description>
 <rdf:Description rdf:about='urn:transitive'>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#TransitiveProperty'/>
  <rdf:type rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#Property'/>
 </rdf:Description>
 <owl:Class rdf:about='urn:Old'>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#DeprecatedClass'/>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert len(observed.axioms) == 11
    assert observed.total_triples == observed.consumed_triples


def test_explicit_inferred_declaration_deduplication_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:ObjectProperty rdf:about='urn:p'>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#SymmetricProperty'/>
 </owl:ObjectProperty>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert len(observed.axioms) == 2


def test_distinct_ontology_annotation_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Ontology rdf:about='urn:o'>
  <rdfs:label>label</rdfs:label>
  <rdfs:comment>comment</rdfs:comment>
 </owl:Ontology>
</rdf:RDF>"""
    boundary = ParseLimits(max_annotations=2)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert len(python.annotations) == 2
    assert observed.ontology_iri == "urn:o"
    assert observed.total_triples == observed.consumed_triples == 3

    limited = ParseLimits(max_annotations=1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_annotations"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_axiom_annotation_reification_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'><rdfs:subClassOf rdf:resource='urn:D'/></owl:Class>
 <owl:Class rdf:about='urn:D'/>
 <owl:Axiom>
  <owl:annotatedSource rdf:resource='urn:C'/>
  <owl:annotatedProperty
   rdf:resource='http://www.w3.org/2000/01/rdf-schema#subClassOf'/>
  <owl:annotatedTarget rdf:resource='urn:D'/>
  <rdfs:label>label</rdfs:label>
 </owl:Axiom>
 <owl:Axiom>
  <owl:annotatedSource rdf:resource='urn:C'/>
  <owl:annotatedProperty
   rdf:resource='http://www.w3.org/2000/01/rdf-schema#subClassOf'/>
  <owl:annotatedTarget rdf:resource='urn:D'/>
  <rdfs:comment>comment</rdfs:comment>
 </owl:Axiom>
</rdf:RDF>"""
    boundary = ParseLimits(max_annotations=2)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    annotated = next(value for value in python.axioms if len(value.annotations) == 2)
    assert type(annotated).__name__ == "SubClassOf"

    limited = ParseLimits(max_annotations=1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_annotations"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_claimed_declaration_reification_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'/>
 <owl:Axiom>
  <owl:annotatedSource rdf:resource='urn:C'/>
  <owl:annotatedProperty
   rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#type'/>
  <owl:annotatedTarget rdf:resource='http://www.w3.org/2002/07/owl#Class'/>
  <rdfs:comment>declared</rdfs:comment>
 </owl:Axiom>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert len(python.axioms) == 1
    assert len(python.axioms[0].annotations) == 1


def test_structural_node_annotation_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:NegativePropertyAssertion>
  <owl:sourceIndividual rdf:resource='urn:i'/>
  <owl:assertionProperty rdf:resource='urn:p'/>
  <owl:targetIndividual rdf:resource='urn:j'/>
  <rdfs:label>label</rdfs:label>
  <rdfs:comment>comment</rdfs:comment>
 </owl:NegativePropertyAssertion>
</rdf:RDF>"""
    boundary = ParseLimits(max_annotations=2)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert len(python.axioms) == 1
    assert len(python.axioms[0].annotations) == 2

    limited = ParseLimits(max_annotations=1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_annotations"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_nested_annotation_reification_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'><rdfs:subClassOf rdf:resource='urn:D'/></owl:Class>
 <owl:Class rdf:about='urn:D'/>
 <owl:Axiom rdf:nodeID='axiom'>
  <owl:annotatedSource rdf:resource='urn:C'/>
  <owl:annotatedProperty
   rdf:resource='http://www.w3.org/2000/01/rdf-schema#subClassOf'/>
  <owl:annotatedTarget rdf:resource='urn:D'/>
  <rdfs:label>base</rdfs:label>
 </owl:Axiom>
 <owl:Annotation>
  <owl:annotatedSource rdf:nodeID='axiom'/>
  <owl:annotatedProperty rdf:resource='http://www.w3.org/2000/01/rdf-schema#label'/>
  <owl:annotatedTarget>base</owl:annotatedTarget>
  <rdfs:comment>nested</rdfs:comment>
  <rdfs:seeAlso rdf:resource='urn:nested'/>
 </owl:Annotation>
</rdf:RDF>"""
    boundary = ParseLimits(max_annotations=2)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    annotated = next(value for value in python.axioms if value.annotations)
    assert len(annotated.annotations) == 1
    assert len(next(iter(annotated.annotations)).annotations) == 2

    limited = ParseLimits(max_annotations=1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_annotations"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


@pytest.mark.parametrize(
    "body",
    (
        """
 <owl:Class rdf:about='urn:C'><rdfs:subClassOf rdf:resource='urn:D'/></owl:Class>
 <owl:Axiom>
  <owl:annotatedSource rdf:resource='urn:C'/>
  <owl:annotatedSource rdf:resource='urn:Other'/>
  <owl:annotatedProperty
   rdf:resource='http://www.w3.org/2000/01/rdf-schema#subClassOf'/>
  <owl:annotatedTarget rdf:resource='urn:D'/>
 </owl:Axiom>
""",
        """
 <owl:Class rdf:about='urn:C'>
  <rdf:type rdf:resource='http://www.w3.org/2000/01/rdf-schema#Class'/>
 </owl:Class>
 <owl:Axiom>
  <owl:annotatedSource rdf:resource='urn:C'/>
  <owl:annotatedProperty
   rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#type'/>
  <owl:annotatedTarget rdf:resource='http://www.w3.org/2000/01/rdf-schema#Class'/>
  <e:note rdf:resource='urn:value'/>
 </owl:Axiom>
""",
        """
 <rdf:Description rdf:about='urn:s'><e:p rdf:resource='urn:o'/></rdf:Description>
 <owl:Annotation>
  <owl:annotatedSource rdf:resource='urn:s'/>
  <owl:annotatedProperty rdf:resource='urn:p'/>
  <owl:annotatedTarget rdf:resource='urn:o'/>
  <e:q rdf:resource='urn:value'/>
 </owl:Annotation>
""",
        """
 <rdf:Description rdf:about='urn:s'><e:p rdf:resource='urn:o'/></rdf:Description>
 <rdf:Description rdf:nodeID='reification'>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#Axiom'/>
  <rdf:type rdf:resource='http://www.w3.org/2002/07/owl#Annotation'/>
  <owl:annotatedSource rdf:resource='urn:s'/>
  <owl:annotatedProperty rdf:resource='urn:p'/>
  <owl:annotatedTarget rdf:resource='urn:o'/>
  <e:q rdf:resource='urn:value'/>
 </rdf:Description>
""",
    ),
)
def test_malformed_or_unclaimed_reification_rejection_matches_python(
    extension: NativeTestExtension,
    body: str,
) -> None:
    source = f"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:e='urn:'>
{body}</rdf:RDF>""".encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDF_AXIOM_REIFICATION"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDF_AXIOM_REIFICATION"


def test_swrl_rule_mapping_matches_python_extension_partition(
    extension: NativeTestExtension,
) -> None:
    owner, observed = _ingest(extension, SWRL_SOURCE)
    python = parse_rdfxml(
        SWRL_SOURCE,
        limits=ParseLimits(),
        document_iri=None,
        allow_swrl=True,
    )

    assert not observed.axioms
    assert not python.axioms
    assert len(python.extensions) == 1
    assert type(python.extensions[0]).__name__ == "SWRLRule"
    assert len(python.extensions[0].body) == 6
    assert len(python.extensions[0].head) == 1
    assert len(python.extensions[0].annotations) == 1
    assert observed.total_triples == observed.consumed_triples == 48
    assert observed.total_triples == python.rdf_mapping_report.total_triples
    attestation = cast(Any, owner)._publication_attestation_v1()
    assert attestation.stored_axiom_count == 0
    assert attestation.extension_count == len(python.extensions)


@pytest.mark.parametrize(
    ("body", "python_code", "native_code"),
    (
        (
            """
 <swrl:Imp>
  <swrl:body rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>
 </swrl:Imp>
""",
            "RDF_MAPPING_CARDINALITY",
            "NATIVE_RDF_MAPPING_CARDINALITY",
        ),
        (
            """
 <swrl:Imp>
  <swrl:body rdf:parseType='Collection'>
   <swrl:ClassAtom>
    <swrl:classPredicate rdf:resource='urn:C'/>
    <swrl:argument1 rdf:resource='urn:i'/>
    <e:extra rdf:resource='urn:value'/>
   </swrl:ClassAtom>
  </swrl:body>
  <swrl:head rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>
 </swrl:Imp>
""",
            "RDF_MAPPING_INCOMPLETE",
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
 <swrl:Variable rdf:nodeID='x'/>
 <swrl:Imp>
  <swrl:body rdf:parseType='Collection'>
   <swrl:ClassAtom>
    <swrl:classPredicate rdf:resource='urn:C'/>
    <swrl:argument1 rdf:nodeID='x'/>
   </swrl:ClassAtom>
  </swrl:body>
  <swrl:head rdf:resource='http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'/>
 </swrl:Imp>
""",
            "RDF_MAPPING_TYPE",
            "NATIVE_RDF_MAPPING_TYPE",
        ),
    ),
)
def test_malformed_swrl_rule_rejection_matches_python(
    extension: NativeTestExtension,
    body: str,
    python_code: str,
    native_code: str,
) -> None:
    source = f"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:swrl='http://www.w3.org/2003/11/swrl#'
 xmlns:e='urn:'>
{body}</rdf:RDF>""".encode()

    with pytest.raises((OntologySyntaxError, UnsupportedSyntaxError)) as python_error:
        parse_rdfxml(
            source,
            limits=ParseLimits(),
            document_iri=None,
            allow_swrl=True,
        )
    assert python_error.value.code == python_code
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == native_code


def test_canonical_swrl_atom_limit_matches_python(
    extension: NativeTestExtension,
) -> None:
    boundary = ParseLimits(max_rule_atoms=6)
    owner, observed = _ingest(extension, SWRL_SOURCE, limits=boundary)
    python = parse_rdfxml(
        SWRL_SOURCE,
        limits=boundary,
        document_iri=None,
        allow_swrl=True,
    )
    assert observed.total_triples == observed.consumed_triples
    assert cast(Any, owner)._publication_attestation_v1().extension_count == 1
    assert len(python.extensions) == 1

    limited = ParseLimits(max_rule_atoms=5)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(
            SWRL_SOURCE,
            limits=limited,
            document_iri=None,
            allow_swrl=True,
        )
    assert python_error.value.limit == "max_rule_atoms"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, SWRL_SOURCE, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


@pytest.mark.parametrize(
    ("document", "literal_bytes"),
    (
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C' rdfs:label='é'/>"
            "</rdf:RDF>",
            2,
        ),
        (
            "<rdf:RDF {namespaces}>"
            "<owl:AnnotationProperty rdf:about='urn:e:p'/>"
            "<owl:Class rdf:about='urn:C'>"
            "<e:p rdf:resource='urn:o' rdfs:label='é'/>"
            "</owl:Class></rdf:RDF>",
            2,
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:comment rdf:parseType='Literal'><e:x/></rdfs:comment>"
            "</owl:Class></rdf:RDF>",
            28,
        ),
    ),
)
def test_all_literal_forms_match_python_utf8_byte_limits(
    extension: NativeTestExtension,
    document: str,
    literal_bytes: int,
) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:e='urn:e:'"
    )
    source = document.format(namespaces=namespaces).encode()
    boundary = ParseLimits(max_literal_bytes=literal_bytes)

    _owner, observed = _ingest(extension, source, limits=boundary)
    python = parse_rdfxml(source, limits=boundary, document_iri=None)
    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))

    limited = ParseLimits(max_literal_bytes=literal_bytes - 1)
    with pytest.raises(ResourceLimitError) as python_error:
        parse_rdfxml(source, limits=limited, document_iri=None)
    assert python_error.value.limit == "max_literal_bytes"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, limits=limited)
    assert native_error.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_processing_instructions_map_to_no_rdf_events(
    extension: NativeTestExtension,
) -> None:
    source = b"""<?audit before?>
<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <?xml-stylesheet href='ignored.xsl'?>
 <owl:Class rdf:about='urn:C'>
  <rdfs:comment>a<?audit nested?>b</rdfs:comment>
 </owl:Class>
</rdf:RDF>
<?audit after?>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples


@pytest.mark.parametrize(
    "instruction",
    ("<??>", "<?1target?>", "<?target/data?>", "<?a:b?>", "<?XML version='1.0'?>"),
)
def test_malformed_processing_instructions_fail_in_both_backends(
    extension: NativeTestExtension,
    instruction: str,
) -> None:
    source = (
        f"{instruction}<rdf:RDF "
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


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


def test_empty_xml_language_resets_inherited_literal_language(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xml:lang='EN'>
 <owl:Class rdf:about='urn:reset:C' xml:lang='' rdfs:label='attribute'>
  <rdfs:comment>element</rdfs:comment>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 3
    assert observed.total_triples == python.rdf_mapping_report.total_triples


@pytest.mark.parametrize(
    "document",
    (
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:label xml:lang='not_valid'>value</rdfs:label>"
            "</owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C' "
            "xml:lang='en--GB' rdfs:label='value'/></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces} xml:lang='x'><owl:Class rdf:about='urn:C'>"
            "<rdfs:label>value</rdfs:label></owl:Class></rdf:RDF>"
        ),
    ),
)
def test_invalid_language_tags_fail_at_mapping_boundary_in_both_backends(
    extension: NativeTestExtension,
    document: str,
) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(UnsupportedSyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDF_MAPPING_UNSUPPORTED"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDF_MAPPING_UNSUPPORTED"


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


def test_datatyped_properties_with_legacy_attributes_match_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:e='urn:e:'>
 <owl:Ontology rdf:about='urn:o'>
  <rdfs:comment rdf:datatype='urn:datatype' e:ignored='discarded'/>
  <rdfs:comment rdf:datatype='urn:datatype' e:ignored='discarded'>kept</rdfs:comment>
 </owl:Ontology>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 3
    assert observed.total_triples == python.rdf_mapping_report.total_triples


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


def test_parse_type_other_matches_python_xml_literal(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:x='urn:x:' xml:lang='EN'>
 <owl:Class rdf:about='urn:C'>
  <rdfs:comment rdf:parseType='Other'>root<x:value a='1'>text</x:value>tail</rdfs:comment>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_literal_bytes=16))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_property_element_id_graph_is_complete_but_strict_mapping_stays_closed(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:e='urn:' xml:base='http://example.test/doc'>
 <owl:AnnotationProperty rdf:about='urn:p'/>
 <owl:Class rdf:about='urn:C'>
  <e:p rdf:ID='statement'>value</e:p>
 </owl:Class>
</rdf:RDF>"""

    with pytest.raises(extension._NativeError) as incomplete:
        _ingest(extension, source)
    assert incomplete.value.args[0] == "NATIVE_RDF_MAPPING_INCOMPLETE"

    with pytest.raises(extension._NativeError) as limited:
        _ingest(extension, source, limits=ParseLimits(max_triples=6))
    assert limited.value.args[0] == "NATIVE_WIRE_LIMIT"


def test_unicode_rdf_ids_match_python_and_are_scoped_by_xml_base(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xml:base='https://example.org/a/doc'>
 <owl:Class rdf:ID='classe-\xc3\xa9'/>
 <owl:Class xml:base='../b/doc' rdf:ID='classe-\xc3\xa9'/>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 2
    assert observed.total_triples == python.rdf_mapping_report.total_triples


@pytest.mark.parametrize(
    "identity",
    (
        "rdf:ID=''",
        "rdf:ID='1leading-digit'",
        "rdf:ID='bad:name'",
        "rdf:nodeID=''",
        "rdf:nodeID='1leading-digit'",
        "rdf:nodeID='bad:name'",
    ),
)
def test_invalid_rdf_identity_ncnames_fail_in_both_backends(
    extension: NativeTestExtension,
    identity: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' "
        "xml:base='https://example.org/doc'>"
        f"<owl:Class {identity}/></rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


def test_duplicate_rdf_id_fails_in_both_backends(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xml:base='https://example.org/doc'>
 <owl:Class rdf:ID='duplicate'/>
 <owl:Class rdf:ID='duplicate'/>
</rdf:RDF>"""

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "property_element",
    (
        "<rdfs:subClassOf rdf:resource='urn:D' rdf:datatype='urn:type'/>",
        "<rdfs:subClassOf rdf:nodeID='target' rdf:datatype='urn:type'/>",
        "<rdfs:subClassOf rdf:resource='urn:D'>text</rdfs:subClassOf>",
        "<rdfs:subClassOf>text<owl:Class rdf:about='urn:D'/></rdfs:subClassOf>",
        "<rdfs:subClassOf><owl:Class rdf:about='urn:D'/>text</rdfs:subClassOf>",
        "<rdfs:label e:note='ignored'>text</rdfs:label>",
        (
            "<rdfs:subClassOf e:note='ignored'>"
            "<owl:Class rdf:about='urn:D'/>"
            "</rdfs:subClassOf>"
        ),
        (
            "<owl:equivalentClass><owl:Class>"
            "<owl:unionOf rdf:parseType='Collection'>text"
            "<rdf:Description rdf:about='urn:D'/>"
            "</owl:unionOf></owl:Class></owl:equivalentClass>"
        ),
        (
            "<rdfs:subClassOf rdf:parseType='Resource'>text"
            "<owl:onProperty rdf:resource='urn:p'/>"
            "<owl:someValuesFrom rdf:resource='urn:D'/>"
            "</rdfs:subClassOf>"
        ),
    ),
)
def test_resource_property_conflicts_and_text_fail_in_both_backends(
    extension: NativeTestExtension,
    property_element: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:e='urn:e:'>"
        f"<owl:Class rdf:about='urn:C'>{property_element}</owl:Class>"
        "</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


def test_resource_property_whitespace_matches_python(
    extension: NativeTestExtension,
) -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'>
 <owl:Class rdf:about='urn:C'>
  <rdfs:subClassOf rdf:resource='urn:D'> \n </rdfs:subClassOf>
  <owl:equivalentClass> \n <owl:Class rdf:about='urn:E'/> \n </owl:equivalentClass>
 </owl:Class>
</rdf:RDF>"""

    _owner, observed = _ingest(extension, source)
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python.rdf_mapping_report is not None

    assert observed.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert observed.total_triples == observed.consumed_triples == 4
    assert observed.total_triples == python.rdf_mapping_report.total_triples


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
 <owl:Class rdf:about='http://example.test/absolute/a/../E'/>
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
    ("reference", "document_iri", "code"),
    (
        ("1:invalid", "http://example.test/doc", "RDFXML_IRI_REFERENCE"),
        (" .", "http://example.test/doc", "RDFXML_SYNTAX"),
        ("g h", "http://example.test/doc", "RDFXML_SYNTAX"),
        ("%zz", "http://example.test/doc", "RDFXML_SYNTAX"),
        ("relative", None, "RDFXML_RELATIVE_IRI_NO_BASE"),
    ),
)
def test_invalid_rfc3986_references_fail_in_both_backends(
    extension: NativeTestExtension,
    reference: str,
    document_iri: str | None,
    code: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'>"
        f"<owl:Class rdf:about='{reference}'/></rdf:RDF>"
    ).encode()
    python_document_iri = None if document_iri is None else IRI(document_iri)

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=python_document_iri)
    assert python_error.value.code == code
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source, document_iri=document_iri)
    assert native_error.value.args[0] == "NATIVE_" + code


@pytest.mark.parametrize(
    "node",
    (
        "<e:Class rdf:about='urn:C'/>",
        "<owl:Class rdf:about='urn:C' e:label='value'/>",
    ),
)
def test_expanded_names_require_absolute_iris_in_both_backends(
    extension: NativeTestExtension,
    node: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:e='relative'>"
        f"{node}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(extension._NativeError) as native_error:
        _ingest(extension, source)
    assert native_error.value.args[0] == "NATIVE_RDFXML_SYNTAX"


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
            b"xmlns:e='urn:e:' xml:base='http://example.test/doc'>"
            b"<rdf:Description rdf:about='urn:s'><e:p rdf:ID=''>value</e:p>"
            b"</rdf:Description></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:' xml:base='http://example.test/doc'>"
            b"<rdf:Description rdf:about='urn:s'><e:p rdf:ID='bad:name'>value</e:p>"
            b"</rdf:Description></rdf:RDF>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:' xml:base='http://example.test/doc'>"
            b"<rdf:Description rdf:about='urn:s'><e:p rdf:ID='statement'>one</e:p>"
            b"<e:q rdf:ID='statement'>two</e:q></rdf:Description></rdf:RDF>",
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
        (
            b"<?xml encoding='UTF-8'?><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<?xml version='1.1'?><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<!--bad---><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "NATIVE_RDFXML_SYNTAX",
        ),
        (
            b"<?xml version='1.0' encoding='ISO-8859-1'?><rdf:RDF "
            b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        ),
        (b"\xff\xfe\x00\xd8", "NATIVE_FORMAT_ENCODING"),
        (b"\xff\xfe<\x00x", "NATIVE_FORMAT_ENCODING"),
        (
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:bad='http://www.w3.org/XML/1998/namespace'/>",
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
