from __future__ import annotations

import gc
import io
import mmap
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    IRI,
    BackendPreference,
    BackendUnavailableError,
    CancellationSource,
    DocumentFormat,
    EncodedStructuralView,
    ImportPolicy,
    ImportResolver,
    ImportStatus,
    LoadOptions,
    MappedOntologySnapshot,
    OntologySyntaxError,
    OperationCancelledError,
    ParseLimits,
    ResourceLimitError,
    UnresolvedImportWarning,
    canonical_bytes,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    open_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.backends.native_ingestion import publish_retained_rdfxml_snapshot_v2
from pyowl_core.cancellation import CancellationToken
from pyowl_core.exceptions import SnapshotInUseError, UnsupportedSyntaxError
from pyowl_core.index import AxiomTypeIndex, OntologyIdentityIndex
from pyowl_core.io.formats.detection import detect_format
from pyowl_core.io.source import acquire_source
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension

SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:rdfxml:retained">
    <owl:imports rdf:resource="urn:rdfxml:ignored"/>
  </owl:Ontology>
  <owl:Class rdf:about="urn:rdfxml:C">
    <rdfs:subClassOf rdf:resource="urn:rdfxml:D"/>
  </owl:Class>
  <owl:Class rdf:about="urn:rdfxml:D"/>
</rdf:RDF>
"""
NO_IMPORT_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:rdfxml:retained"/>
  <owl:Class rdf:about="urn:rdfxml:C">
    <rdfs:subClassOf rdf:resource="urn:rdfxml:D"/>
  </owl:Class>
  <owl:Class rdf:about="urn:rdfxml:D"/>
</rdf:RDF>
"""
XML_ENVELOPE_SOURCE = b"""\
<?xml version = '1.0' encoding='UTF-8' standalone = "yes"?>
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:xml="http://www.w3.org/XML/1998/namespace"
  xmlns="">
  <owl:Class rdf:about="urn:C">
    <rdfs:label xml:lang="EN">Class</rdfs:label>
  </owl:Class>
</rdf:RDF>
"""
PARSE_TYPE_RESOURCE_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:rdfxml:A">
    <rdfs:subClassOf rdf:parseType="Resource">
      <owl:intersectionOf rdf:parseType="Collection">
        <rdf:Description rdf:about="urn:rdfxml:B"/>
        <rdf:Description rdf:about="urn:rdfxml:C"/>
      </owl:intersectionOf>
    </rdfs:subClassOf>
  </owl:Class>
</rdf:RDF>
"""
PROPERTY_ATTRIBUTE_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdf:Description rdf:about="urn:rdfxml:attribute:C"
    rdf:type="http://www.w3.org/2002/07/owl#Class"
    xml:lang="EN" rdfs:label="Class label"/>
</rdf:RDF>
"""
UNICODE_QNAME_SOURCE = """\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:π="urn:unicode:">
  <owl:AnnotationProperty rdf:about="urn:unicode:qualité"/>
  <owl:AnnotationProperty rdf:about="urn:unicode:étiquette"/>
  <owl:Class rdf:about="urn:C" π:qualité="élevée">
    <π:étiquette xml:lang="FR">café</π:étiquette>
  </owl:Class>
</rdf:RDF>
""".encode()
XML_NORMALIZATION_SOURCE = (
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
EMPTY_PROPERTY_ATTRIBUTE_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:e="urn:e:">
  <owl:ObjectProperty rdf:about="urn:e:p"/>
  <owl:NamedIndividual rdf:about="urn:i"/>
  <rdf:Description rdf:about="urn:i">
    <e:p rdf:resource="urn:j"
      rdf:type="http://www.w3.org/2002/07/owl#NamedIndividual"
      rdfs:label="Target"/>
  </rdf:Description>
</rdf:RDF>
"""
RDF_LI_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:AnnotationProperty
    rdf:about="http://www.w3.org/1999/02/22-rdf-syntax-ns#_1"/>
  <owl:AnnotationProperty
    rdf:about="http://www.w3.org/1999/02/22-rdf-syntax-ns#_2"/>
  <owl:Class rdf:about="urn:C">
    <rdf:li>first</rdf:li>
    <rdf:li xml:lang="EN">second</rdf:li>
  </owl:Class>
</rdf:RDF>
"""
PARSE_TYPE_LITERAL_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xml:lang="EN">
  <owl:Class rdf:about="urn:C">
    <rdfs:comment rdf:parseType="Literal">a &amp; <![CDATA[b < c]]></rdfs:comment>
  </owl:Class>
</rdf:RDF>
"""
NESTED_XML_LITERAL_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:x="urn:x:"
  xmlns:y="urn:y:">
  <owl:Class rdf:about="urn:C">
    <rdfs:comment rdf:parseType="Literal">root<x:box z="2" a="1" xml:base="../"><y:item
      x:attr="&quot;">hi &amp;</y:item>tail&lt;</x:box>between<x:empty/>suffix</rdfs:comment>
  </owl:Class>
</rdf:RDF>
"""
PARSE_TYPE_OTHER_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:x="urn:x:" xml:lang="EN">
  <owl:Class rdf:about="urn:C">
    <rdfs:comment rdf:parseType="Other">root<x:value a="1">text</x:value>tail</rdfs:comment>
  </owl:Class>
</rdf:RDF>
"""
DOCUMENT_IRI = IRI("urn:rdfxml:document")


class _UnreadableRdfXml(io.BytesIO):
    def read(self, size: int | None = -1, /) -> bytes:
        raise AssertionError("unsupported forced-native RDF/XML consumed its source")


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        pytest.skip(result.reason or "native backend unavailable")
    if not hasattr(selected, "_parse_rdfxml_retained_v2"):
        pytest.skip("selected native artifact lacks retained RDF/XML production seam")
    return selected


def _options(backend: BackendPreference) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.RDF_XML,
        imports=ImportPolicy.IGNORE,
        backend=backend,
        collect_provenance=False,
    )


def _retained_snapshot(
    source: bytes = SOURCE,
    *,
    options: LoadOptions | None = None,
    document_iri: IRI | None = DOCUMENT_IRI,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
    require_empty_imports: bool | None = None,
) -> object:
    selected_options = _options(BackendPreference.NATIVE) if options is None else options
    payload = acquire_source(
        source,
        format=DocumentFormat.RDF_XML,
        document_iri=document_iri,
        limits=selected_options.limits,
        cancellation_token=cancellation_token,
    )
    detection = detect_format(payload.data, explicit=DocumentFormat.RDF_XML)
    started = time.monotonic()
    if require_empty_imports is None:
        require_empty_imports = selected_options.imports in {
            ImportPolicy.RESOLVE_LOCAL,
            ImportPolicy.RESOLVE_STRICT,
        } or (selected_options.imports is ImportPolicy.RECORD_UNRESOLVED and resolver is not None)
    parsed = native._parse_rdfxml_retained_v2(
        source,
        document_iri=None if document_iri is None else document_iri.value,
        limits=selected_options.limits,
        collect_provenance=selected_options.collect_provenance,
        allow_partial_rdf_mapping=False,
        require_empty_imports=require_empty_imports,
        cancellation_token=cancellation_token,
    )
    if parsed.summary is None or parsed.storage is None:
        raise AssertionError("retained RDF/XML parser returned no owner-first result")
    return publish_retained_rdfxml_snapshot_v2(
        parsed.summary,
        parsed_native_storage=parsed.storage,
        phase_timings=parsed.phase_timings,
        payload=payload,
        detection=detection,
        document_iri=document_iri,
        media_type=None,
        options=selected_options,
        resolver=resolver,
        cancellation_token=cancellation_token,
        load_started=started,
        root_parse_started=started,
    )


def test_private_production_seam_publishes_exact_lazy_rdf_report(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("retained RDF/XML crossed the Python parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot())

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.capabilities.backend == "native"
    assert selected.root.document_fingerprint == reference.root.document_fingerprint
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.import_manifest == reference.import_manifest
    assert selected.report.timings["native_rdfxml_syntax_parse_seconds"] >= 0
    assert selected.report.timings["native_rdf_mapping_seconds"] >= 0
    assert "native_rdfxml_parse_mapping_seconds" not in selected.report.timings

    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    before = raw_owner._publication_counters_v2()
    assert before.parser_bytes == len(SOURCE)
    assert before.retained_rdf_header_rows == 1
    assert before.retained_rdf_triple_rows == 0
    assert before.retained_rdf_rule_rows == 0
    assert before.retained_rdf_diagnostic_rows == 0
    assert before.retained_rdf_bytes == 17
    assert before.publication_structural_rows_copied == 0
    assert before.publication_structural_bytes_copied == 0
    assert before.page_requests == 0

    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    after_report = raw_owner._publication_counters_v2()
    assert after_report.page_requests == 1
    assert after_report.rdf_header_rows_emitted == 1
    assert after_report.auxiliary_payload_bytes_copied == 17

    before_python = selected._native_python_counters()
    scalar_error = AssertionError("retained RDF/XML view crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
    ):
        direct = selected.view(EncodedStructuralView)
    expected_roots = tuple(
        (kind, canonical_bytes(value))
        for kind, values in (
            (1, reference.root.ontology_annotations),
            (2, reference.root.axioms),
            (3, reference.root.extension_components),
        )
        for value in values
    )
    after_direct = raw_owner._publication_counters_v2()
    after_python = selected._native_python_counters()
    assert direct.owner is selected
    assert decode_root_canonical_bytes(direct.buffers) == expected_roots
    assert len({id(value.obj) for value in direct.buffers.values()}) == 1
    assert after_direct.encoded_view_requests == after_report.encoded_view_requests + 1
    assert after_direct.page_requests == after_report.page_requests
    assert after_direct.rows_emitted == after_report.rows_emitted
    assert after_python.model_rows_materialized == before_python.model_rows_materialized
    assert selected.root.axioms == reference.root.axioms
    selected.close()
    assert selected.closed
    assert decode_root_canonical_bytes(direct.buffers) == expected_roots


def test_parse_type_resource_publishes_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        PARSE_TYPE_RESOURCE_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("parseType Resource crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(PARSE_TYPE_RESOURCE_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(PARSE_TYPE_RESOURCE_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_xml_envelope_validation_publishes_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        XML_ENVELOPE_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("validated XML envelope crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(XML_ENVELOPE_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(XML_ENVELOPE_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_node_property_attributes_publish_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        PROPERTY_ATTRIBUTE_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("node property attributes crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(PROPERTY_ATTRIBUTE_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(PROPERTY_ATTRIBUTE_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_unicode_qnames_publish_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        UNICODE_QNAME_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("Unicode QNames crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(UNICODE_QNAME_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(UNICODE_QNAME_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_xml_value_normalization_publishes_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        XML_NORMALIZATION_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("normalized XML values crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(XML_NORMALIZATION_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(XML_NORMALIZATION_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_empty_property_attributes_publish_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        EMPTY_PROPERTY_ATTRIBUTE_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("empty property attributes crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(EMPTY_PROPERTY_ATTRIBUTE_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(EMPTY_PROPERTY_ATTRIBUTE_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_rdf_li_expansion_publishes_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        RDF_LI_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("rdf:li expansion crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(RDF_LI_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(RDF_LI_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_markup_free_parse_type_literal_publishes_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        PARSE_TYPE_LITERAL_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("parseType Literal crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(PARSE_TYPE_LITERAL_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(PARSE_TYPE_LITERAL_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


@pytest.mark.parametrize(
    ("source", "unexpected_message"),
    (
        (NESTED_XML_LITERAL_SOURCE, "nested XML literal crossed the Python RDF/XML parser"),
        (PARSE_TYPE_OTHER_SOURCE, "parseType Other crossed the Python RDF/XML parser"),
    ),
)
def test_xml_literal_forms_publish_from_the_retained_parser_owner(
    extension: NativeTestExtension,
    source: bytes,
    unexpected_message: str,
) -> None:
    reference = load_snapshot(
        source,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError(unexpected_message)
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(source))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(source)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_rdfxml_capability_remains_absent_and_public_dispatch_does_not_fallback(
    extension: NativeTestExtension,
) -> None:
    assert "parse-rdfxml-v1" not in extension.FEATURES
    assert extension.INGESTION_FEATURES == ()
    unexpected = AssertionError("unsupported forced-native RDF/XML executed a parser")
    with (
        patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected),
        patch.object(
            cast(Any, extension),
            "_parse_rdfxml_retained_v2",
            side_effect=unexpected,
        ),
        pytest.raises(BackendUnavailableError, match="parse-rdfxml-v1"),
    ):
        load_snapshot(
            SOURCE,
            document_iri=DOCUMENT_IRI,
            options=_options(BackendPreference.NATIVE),
        )

    unread = _UnreadableRdfXml(SOURCE)
    with pytest.raises(BackendUnavailableError, match="parse-rdfxml-v1"):
        load_snapshot(
            unread,
            document_iri=DOCUMENT_IRI,
            options=_options(BackendPreference.NATIVE),
        )
    assert unread.tell() == 0


def test_private_rdfxml_seam_rejects_unowned_semantics_before_publication() -> None:
    with pytest.raises(UnsupportedSyntaxError, match="does not support partial mapping"):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            allow_partial_rdf_mapping=True,
        )

    anonymous = b"""\
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
      <rdf:Description rdf:nodeID="anonymous">
        <rdfs:comment rdf:resource="urn:value"/>
      </rdf:Description>
    </rdf:RDF>
    """
    with pytest.raises(UnsupportedSyntaxError, match="anonymous re-scoping"):
        native._parse_rdfxml_retained_v2(anonymous, document_iri=None)

    reified = b"""\
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:e="urn:"
      xml:base="http://example.test/doc">
      <owl:AnnotationProperty rdf:about="urn:p"/>
      <owl:Class rdf:about="urn:C">
        <e:p rdf:ID="statement">value</e:p>
      </owl:Class>
    </rdf:RDF>
    """
    with pytest.raises(UnsupportedSyntaxError) as incomplete:
        native._parse_rdfxml_retained_v2(reified, document_iri=None)
    assert incomplete.value.code == "RDF_MAPPING_INCOMPLETE"

    with pytest.raises(UnsupportedSyntaxError, match="resolver-backed imports"):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            require_empty_imports=True,
        )


def test_private_provenance_rows_match_python_and_remain_native_until_access() -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
        )

    reference = load_snapshot(
        SOURCE,
        document_iri=DOCUMENT_IRI,
        options=options(BackendPreference.PYTHON),
    )
    selected = cast(
        Any,
        _retained_snapshot(options=options(BackendPreference.NATIVE)),
    )
    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    before = raw_owner._publication_counters_v2()
    ingestion = selected._native_ingestion_counters_v2()

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    expected_origin_rows = sum(
        len(values) for values in reference.origin_index.entries.values()
    )
    assert before.retained_origin_rows == 2 * expected_origin_rows
    assert before.retained_origin_bytes > 0
    assert before.origin_rows_emitted == 0
    assert ingestion.provenance_occurrence_records_materialized == 0
    assert ingestion.canonical_bytes_copied_to_python == 0
    assert selected.origin_index == reference.origin_index
    after = raw_owner._publication_counters_v2()
    assert after.origin_rows_emitted >= expected_origin_rows
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_private_rdfxml_axiom_index_builds_over_the_retained_arena(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    selected = cast(Any, _retained_snapshot())
    before_python = selected._native_python_counters()
    unexpected = AssertionError("retained RDF/XML index crossed coarse canonical rows")
    with patch.object(native, "partition_axioms", side_effect=unexpected):
        index = selected.view(AxiomTypeIndex)
    reference_index = reference.view(AxiomTypeIndex)
    after_build = selected._native_python_counters()

    native_owner = cast(Any, index)._native_owner
    expected_owner_type = cast(Any, extension)._NativeRetainedAxiomTypeIndexV1
    assert type(native_owner) is expected_owner_type
    tags, offsets, category_codes, category_offsets, postings, counters = native_owner._layout_v1()
    assert counters["axiom_rows"] == len(reference.root.axioms)
    assert counters["constructor_groups"] == len(tags)
    assert counters["category_groups"] == len(category_codes)
    assert counters["retained_buffer_bytes"] > 0
    assert counters["peak_owned_bytes"] >= counters["retained_buffer_bytes"]
    assert counters["complete_root_encode_calls"] == 0
    assert native_owner._canonical_sizes_v1() == tuple(
        len(canonical_bytes(value)) for value in reference_index.iter_all()
    )
    assert offsets[0] == category_offsets[0] == 0
    assert offsets[-1] == category_offsets[-1] == len(postings)
    assert postings == tuple(range(len(reference.root.axioms)))
    assert index.report.tables == reference_index.report.tables
    assert after_build.model_rows_materialized - before_python.model_rows_materialized == 0
    assert tuple(canonical_bytes(value) for value in index.iter_all()) == tuple(
        canonical_bytes(value) for value in reference_index.iter_all()
    )
    after_iteration = selected._native_python_counters()
    assert after_iteration.model_rows_materialized - before_python.model_rows_materialized == len(
        reference.root.axioms
    )
    assert native_owner._layout_v1()[-1]["complete_root_encode_calls"] == len(reference.root.axioms)

    before_close_calls = native_owner._layout_v1()[-1]["complete_root_encode_calls"]
    selected.close()
    assert tuple(index.iter_all()) == tuple(reference_index.iter_all())
    assert native_owner._layout_v1()[-1]["complete_root_encode_calls"] == (
        before_close_calls + len(reference.root.axioms)
    )


def test_private_record_unresolved_policy_matches_python_without_resolver() -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.RECORD_UNRESOLVED,
            backend=backend,
            collect_provenance=False,
        )

    with pytest.warns(UnresolvedImportWarning):
        reference = load_snapshot(
            SOURCE,
            document_iri=DOCUMENT_IRI,
            options=options(BackendPreference.PYTHON),
        )
    with pytest.warns(UnresolvedImportWarning):
        selected = cast(
            Any,
            _retained_snapshot(options=options(BackendPreference.NATIVE)),
        )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.import_manifest == reference.import_manifest
    assert selected.report.diagnostics == reference.report.diagnostics
    assert selected.report.resolution_attempts == reference.report.resolution_attempts == 1
    assert len(selected.import_manifest.edges) == 1
    assert selected.import_manifest.edges[0].status is ImportStatus.UNRESOLVED
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_private_rdfxml_identity_wire_and_mmap_owners_avoid_scalar_materialization(
    tmp_path: Path,
    extension: NativeTestExtension,
) -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.RECORD_UNRESOLVED,
            backend=backend,
            collect_provenance=False,
        )

    with pytest.warns(UnresolvedImportWarning):
        reference = load_snapshot(
            SOURCE,
            document_iri=DOCUMENT_IRI,
            options=options(BackendPreference.PYTHON),
        )
    with pytest.warns(UnresolvedImportWarning):
        selected = cast(
            Any,
            _retained_snapshot(options=options(BackendPreference.NATIVE)),
        )

    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    before_owner = raw_owner._publication_counters_v2()
    before_python = selected._native_python_counters()
    selected_identity = selected.view(OntologyIdentityIndex)
    reference_identity = reference.view(OntologyIdentityIndex)
    after_identity_owner = raw_owner._publication_counters_v2()
    after_identity_python = selected._native_python_counters()

    assert selected_identity.documents == reference_identity.documents
    assert selected_identity.import_manifest_digest == reference_identity.import_manifest_digest
    assert (
        selected_identity.loader_diagnostics_digest
        == reference_identity.loader_diagnostics_digest
    )
    assert selected_identity.is_complete is reference_identity.is_complete is False
    identity_owner = cast(Any, selected_identity)._native_owner
    assert (
        type(identity_owner)
        is cast(Any, extension)._NativeRetainedOntologyIdentityIndexV1
    )
    *_identity_layout, identity_counters = identity_owner._layout_v1()
    assert identity_counters["document_count"] == 1
    assert identity_counters["import_edge_count"] == 1
    assert identity_counters["complete_root_encode_calls"] == 0
    assert after_identity_owner == before_owner
    assert after_identity_python == before_python

    scalar_error = AssertionError("retained RDF/XML wire crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=scalar_error),
        patch.object(type(selected), "iter_extensions", side_effect=scalar_error),
        patch.object(type(selected), "ontology_annotations", side_effect=scalar_error),
        patch.object(type(selected), "signature", side_effect=scalar_error),
    ):
        encoded = encode_snapshot(selected)
    after_wire_owner = raw_owner._publication_counters_v2()
    after_wire_python = selected._native_python_counters()

    assert encoded == encode_snapshot(reference)
    assert after_wire_owner.encoded_view_requests == before_owner.encoded_view_requests + 1
    assert after_wire_owner.page_requests == before_owner.page_requests
    assert after_wire_owner.rows_emitted == before_owner.rows_emitted
    assert after_wire_python.model_rows_materialized == before_python.model_rows_materialized

    decoded = decode_snapshot(encoded)
    decoded_identity = decoded.view(OntologyIdentityIndex)
    assert decoded_identity.documents == reference_identity.documents
    assert decoded_identity.import_manifest_digest == reference_identity.import_manifest_digest
    assert (
        decoded_identity.loader_diagnostics_digest
        == reference_identity.loader_diagnostics_digest
    )

    path = tmp_path / "retained-rdfxml.pyocore"
    path.write_bytes(encoded)
    mapped = open_snapshot(path, mmap=True, verify=True)
    assert isinstance(mapped, MappedOntologySnapshot)
    assert mapped._mapped_state.decoded is None
    mapped_identity = mapped.view(OntologyIdentityIndex)
    assert mapped_identity.documents == reference_identity.documents
    assert mapped_identity.import_manifest_digest == reference_identity.import_manifest_digest
    assert (
        mapped_identity.loader_diagnostics_digest
        == reference_identity.loader_diagnostics_digest
    )
    assert mapped._mapped_state.decoded is None

    mapped_view = mapped.view(EncodedStructuralView)
    expected_roots = tuple(
        (kind, canonical_bytes(value))
        for kind, values in (
            (1, reference.root.ontology_annotations),
            (2, reference.root.axioms),
            (3, reference.root.extension_components),
        )
        for value in values
    )
    assert decode_root_canonical_bytes(mapped_view.buffers) == expected_roots
    assert len({id(value.obj) for value in mapped_view.buffers.values()}) == 1
    assert all(type(value.obj) is mmap.mmap for value in mapped_view.buffers.values())
    assert all(value.readonly for value in mapped_view.buffers.values())
    assert mapped._mapped_state.decoded is None

    with pytest.raises(SnapshotInUseError):
        mapped.close()
    del mapped_view
    gc.collect()
    mapped.close()
    assert mapped.closed
    assert mapped_identity.documents == reference_identity.documents

    selected.close()
    assert selected.closed
    assert selected_identity.documents == reference_identity.documents


@pytest.mark.parametrize(
    "policy",
    (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT),
)
def test_private_empty_resolver_policy_matches_python(policy: ImportPolicy) -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=policy,
            backend=backend,
            collect_provenance=False,
        )

    reference = load_snapshot(
        NO_IMPORT_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=options(BackendPreference.PYTHON),
    )
    selected = cast(
        Any,
        _retained_snapshot(
            NO_IMPORT_SOURCE,
            options=options(BackendPreference.NATIVE),
        ),
    )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.import_manifest == reference.import_manifest
    assert selected.import_manifest.policy is policy
    assert selected.import_manifest.edges == ()
    assert selected.report.resolution_attempts == 0
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_private_production_seam_fails_closed_for_syntax_limits_and_cancellation() -> None:
    with pytest.raises(OntologySyntaxError) as malformed:
        native._parse_rdfxml_retained_v2(b"<rdf:RDF", document_iri=None)
    assert malformed.value.code == "RDFXML_SYNTAX"

    with pytest.raises(ResourceLimitError):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            limits=ParseLimits(max_source_bytes=len(SOURCE) - 1),
        )
    with pytest.raises(ResourceLimitError):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            limits=ParseLimits(max_triples=1),
        )
    with pytest.raises(ResourceLimitError):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            limits=ParseLimits(max_axioms=1),
        )

    cancellation = CancellationSource()
    cancellation.cancel("retained RDF/XML cancellation")
    with pytest.raises(OperationCancelledError, match="retained RDF/XML cancellation"):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            cancellation_token=cancellation.token,
        )
