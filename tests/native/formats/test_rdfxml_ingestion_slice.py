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
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
            b"xmlns:e='urn:e:'><rdf:Description rdf:about='urn:s'>"
            b"<e:p rdf:resource='urn:o'/></rdf:Description></rdf:RDF>",
            "NATIVE_RDF_MAPPING_INCOMPLETE",
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
