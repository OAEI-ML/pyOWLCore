from __future__ import annotations

import gc
import io
import mmap
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

import pyowl_core.model as m
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
    render_document,
)
from pyowl_core.backends import native
from pyowl_core.backends.native_ingestion import publish_retained_rdfxml_snapshot_v2
from pyowl_core.cancellation import CancellationToken
from pyowl_core.exceptions import SnapshotInUseError, UnsupportedSyntaxError
from pyowl_core.index import AxiomTypeIndex, OntologyIdentityIndex
from pyowl_core.io.formats.detection import detect_format
from pyowl_core.io.formats.rdfxml import parse_rdfxml
from pyowl_core.io.source import acquire_source
from tests.conformance._support import every_constructor_document
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.formats.test_rdfxml_ingestion_slice import (
    LEGACY_UNQUALIFIED_RDF_ATTRIBUTE_SOURCE,
    QUALIFIED_RDF_ATTRIBUTE_SOURCE,
    SWRL_SOURCE,
    W3C_RDFXML_SOURCE,
)
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
UTF16_SOURCE_TEXT = """\
<?xml version='1.0' encoding='UTF-16'?>
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <rdfs:label>café 🙂</rdfs:label>
  </owl:Class>
</rdf:RDF>
"""
UTF16_SOURCE = b"\xff\xfe" + UTF16_SOURCE_TEXT.encode("utf-16-le")
PROCESSING_INSTRUCTION_SOURCE = b"""\
<?audit before?>
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <?xml-stylesheet href="ignored.xsl"?>
  <owl:Class rdf:about="urn:C">
    <rdfs:comment>a<?audit nested?>b</rdfs:comment>
  </owl:Class>
</rdf:RDF>
<?audit after?>
"""
UNKNOWN_XML_ATTRIBUTE_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:e="urn:xml-attribute:"
  xmlns:XmLmeta="urn:xml-metadata:"
  xmlns:XmLrdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:XML="urn:xml-uppercase:"
  xml:trace="root" xmlroot="root" XmLmeta:trace="root" XML:trace="root">
  <owl:Ontology rdf:about="urn:xml-attribute:ontology"
    xml:trace="node" XMLnode="node" XmLmeta:trace="node">
    <rdfs:label xml:trace="property" xmlnewthing="property"
      XmLmeta:trace="property">Ontology</rdfs:label>
    <rdfs:seeAlso XmLrdf:resource="urn:wrong"/>
    <rdfs:comment rdf:parseType="Literal" xml:trace="outer"
      XmlOuter="outer" XmLmeta:trace="outer"><e:mark
      xml:trace="literal"/></rdfs:comment>
  </owl:Ontology>
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
EMPTY_LANGUAGE_RESET_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xml:lang="EN">
  <owl:Class rdf:about="urn:reset:C" xml:lang="" rdfs:label="attribute">
    <rdfs:comment>element</rdfs:comment>
  </owl:Class>
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
UNICODE_RDF_ID_SOURCE = """\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xml:base="https://example.org/a/doc">
  <owl:Class rdf:ID="classe-é"/>
  <owl:Class xml:base="../b/doc" rdf:ID="classe-é"/>
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
DATATYPED_EMPTY_PROPERTY_ATTRIBUTE_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:e="urn:e:">
  <owl:Ontology rdf:about="urn:o">
    <rdfs:comment rdf:datatype="urn:datatype" e:ignored="discarded"/>
  </owl:Ontology>
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
ANONYMOUS_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdf:Description rdf:nodeID="lexical-z">
    <owl:sameAs rdf:nodeID="lexical-a"/>
  </rdf:Description>
</rdf:RDF>
"""
ANONYMOUS_ASSERTION_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:owl="http://www.w3.org/2002/07/owl#"
  xmlns:e="urn:">
  <owl:Ontology rdf:about="urn:anonymous:ontology">
    <owl:versionIRI rdf:resource="urn:anonymous:version"/>
  </owl:Ontology>
  <owl:ObjectProperty rdf:about="urn:p"/>
  <rdf:Description rdf:nodeID="lexical-source">
    <e:p rdf:nodeID="lexical-target"/>
  </rdf:Description>
</rdf:RDF>
"""
ANONYMOUS_SYMMETRIC_SOURCE = (
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    + b"".join(
        f'<rdf:Description rdf:nodeID="b{index}"><rdf:type rdf:resource="urn:C"/>'
        f"</rdf:Description>".encode()
        for index in range(6)
    )
    + b"</rdf:RDF>"
)
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
    allow_swrl: bool = False,
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
        preserve_source_map=selected_options.preserve_source_map,
        allow_partial_rdf_mapping=False,
        allow_swrl=allow_swrl,
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


def test_retained_swrl_extension_matches_python_canonical_bytes() -> None:
    reference = parse_rdfxml(
        SWRL_SOURCE,
        limits=ParseLimits(),
        document_iri=None,
        allow_swrl=True,
    )
    with pytest.raises(UnsupportedSyntaxError) as disabled:
        native._parse_rdfxml_retained_v2(SWRL_SOURCE, document_iri=None)
    assert disabled.value.code == "EXTENSION_DISABLED"
    with pytest.raises(TypeError, match="allow_swrl must be bool"):
        native._parse_rdfxml_retained_v2(
            SWRL_SOURCE,
            document_iri=None,
            allow_swrl=cast(Any, 1),
        )
    selected = cast(
        Any,
        _retained_snapshot(SWRL_SOURCE, document_iri=None, allow_swrl=True),
    )

    expected = tuple(canonical_bytes(value) for value in reference.extensions)
    assert not selected.root.axioms
    assert tuple(canonical_bytes(value) for value in selected.root.extension_components) == expected
    direct = selected.view(EncodedStructuralView)
    assert decode_root_canonical_bytes(direct.buffers) == tuple((3, value) for value in expected)
    selected.close()
    assert decode_root_canonical_bytes(direct.buffers) == tuple((3, value) for value in expected)


def test_generated_every_constructor_corpus_publishes_from_retained_owner(
    extension: NativeTestExtension,
) -> None:
    source = render_document(
        every_constructor_document(),
        format=DocumentFormat.RDF_XML,
    )
    reference = load_snapshot(
        source,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("every-constructor corpus crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(source))

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.document_fingerprint == reference.root.document_fingerprint
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.import_manifest == reference.import_manifest
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert set(m.AXIOM_TYPES) <= {type(value) for value in reference.root.axioms}

    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    before = raw_owner._publication_counters_v2()
    before_python = selected._native_python_counters()
    assert before.parser_bytes == len(source)
    assert before.publication_structural_rows_copied == 0
    assert before.publication_structural_bytes_copied == 0

    expected_roots = tuple(
        (kind, canonical_bytes(value))
        for kind, values in (
            (1, reference.root.ontology_annotations),
            (2, tuple(reference.iter_axioms())),
            (3, tuple(reference.iter_extensions())),
        )
        for value in values
    )
    direct = selected.view(EncodedStructuralView)
    assert decode_root_canonical_bytes(direct.buffers) == expected_roots
    assert encode_snapshot(selected) == encode_snapshot(reference)

    after = raw_owner._publication_counters_v2()
    after_python = selected._native_python_counters()
    assert after.publication_structural_rows_copied == 0
    assert after.publication_structural_bytes_copied == 0
    assert after_python.model_rows_materialized == before_python.model_rows_materialized
    selected.close()
    assert decode_root_canonical_bytes(direct.buffers) == expected_roots


def test_locked_w3c_rdfxml_corpus_publishes_from_retained_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        W3C_RDFXML_SOURCE,
        document_iri=None,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("W3C RDF/XML corpus crossed the Python parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(
            Any,
            _retained_snapshot(W3C_RDFXML_SOURCE, document_iri=None),
        )

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.document_fingerprint == reference.root.document_fingerprint
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.import_manifest == reference.import_manifest
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report

    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    before = raw_owner._publication_counters_v2()
    before_python = selected._native_python_counters()
    assert before.parser_bytes == len(W3C_RDFXML_SOURCE) == 628
    assert before.retained_rdf_header_rows == 1
    assert before.publication_structural_rows_copied == 0
    assert before.publication_structural_bytes_copied == 0

    expected_roots = tuple(
        (kind, canonical_bytes(value))
        for kind, values in (
            (1, reference.root.ontology_annotations),
            (2, reference.root.axioms),
            (3, reference.root.extension_components),
        )
        for value in values
    )
    direct = selected.view(EncodedStructuralView)
    assert decode_root_canonical_bytes(direct.buffers) == expected_roots
    assert encode_snapshot(selected) == encode_snapshot(reference)

    after = raw_owner._publication_counters_v2()
    after_python = selected._native_python_counters()
    assert after.publication_structural_rows_copied == 0
    assert after.publication_structural_bytes_copied == 0
    assert after_python.model_rows_materialized == before_python.model_rows_materialized
    selected.close()
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


def test_utf16_publishes_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        UTF16_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("UTF-16 crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(UTF16_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert selected.root.provenance.decoded_codepoint_length == len(UTF16_SOURCE_TEXT)
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(UTF16_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_processing_instructions_publish_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        PROCESSING_INSTRUCTION_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("processing instructions crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(PROCESSING_INSTRUCTION_SOURCE))

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
    assert counters.parser_bytes == len(PROCESSING_INSTRUCTION_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


def test_unknown_xml_attributes_publish_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        UNKNOWN_XML_ATTRIBUTE_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("unknown XML attributes crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(UNKNOWN_XML_ATTRIBUTE_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.ontology_annotations == reference.root.ontology_annotations
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(UNKNOWN_XML_ATTRIBUTE_SOURCE)
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


def test_empty_xml_language_reset_publishes_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        EMPTY_LANGUAGE_RESET_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("empty language reset crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(EMPTY_LANGUAGE_RESET_SOURCE))

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
    assert counters.parser_bytes == len(EMPTY_LANGUAGE_RESET_SOURCE)
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


def test_unicode_rdf_ids_publish_from_the_retained_parser_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        UNICODE_RDF_ID_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("Unicode RDF IDs crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(UNICODE_RDF_ID_SOURCE))

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
    assert counters.parser_bytes == len(UNICODE_RDF_ID_SOURCE)
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


def test_datatyped_empty_property_with_legacy_attribute_publishes_from_retained_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        DATATYPED_EMPTY_PROPERTY_ATTRIBUTE_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("datatyped empty property crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(DATATYPED_EMPTY_PROPERTY_ATTRIBUTE_SOURCE))

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.ontology_annotations == reference.root.ontology_annotations
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(DATATYPED_EMPTY_PROPERTY_ATTRIBUTE_SOURCE)
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


@pytest.mark.parametrize(
    "source",
    [ANONYMOUS_SOURCE, ANONYMOUS_ASSERTION_SOURCE, ANONYMOUS_SYMMETRIC_SOURCE],
)
def test_anonymous_individuals_keep_distinct_raw_and_effective_native_owners(
    extension: NativeTestExtension,
    source: bytes,
) -> None:
    reference = load_snapshot(
        source,
        document_iri=None,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("anonymous RDF/XML crossed the Python parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(Any, _retained_snapshot(source, document_iri=None))

    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    before = raw_owner._publication_counters_v2()
    raw_axioms = tuple(canonical_bytes(value) for value in selected.root.axioms)
    effective_axioms = tuple(canonical_bytes(value) for value in selected.iter_axioms())
    reference_raw = tuple(canonical_bytes(value) for value in reference.root.axioms)
    reference_effective = tuple(canonical_bytes(value) for value in reference.iter_axioms())

    assert type(raw_owner) is cast(Any, extension)._NativeSnapshotHandle
    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert raw_axioms == reference_raw
    assert effective_axioms == reference_effective
    assert raw_axioms != effective_axioms
    assert selected.root.document_fingerprint == reference.root.document_fingerprint
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected._anonymous_scopes == reference._anonymous_scopes
    assert not selected._native_wire_structural_aliases_v1()
    reference_wire = encode_snapshot(reference)
    before_wire_native = raw_owner._publication_counters_v2()
    before_wire_python = selected._native_python_counters()
    wire_error = AssertionError("anonymous RDF/XML wire crossed scalar traversal")
    with (
        patch.object(type(selected), "iter_axioms", side_effect=wire_error),
        patch.object(type(selected), "iter_extensions", side_effect=wire_error),
        patch.object(type(selected), "ontology_annotations", side_effect=wire_error),
        patch.object(type(selected), "signature", side_effect=wire_error),
    ):
        selected_wire = encode_snapshot(selected)
    after_wire_native = raw_owner._publication_counters_v2()
    after_wire_python = selected._native_python_counters()
    assert selected_wire == reference_wire
    assert (
        after_wire_native.encoded_view_requests
        == before_wire_native.encoded_view_requests + 3
    )
    assert after_wire_python == before_wire_python
    assert before.parser_bytes == len(source)
    assert before.publication_structural_rows_copied == 0
    assert before.publication_structural_bytes_copied == 0


def test_anonymous_alpha_permutations_obey_the_shared_canonical_work_limit() -> None:
    limits = ParseLimits(max_canonical_work=5_000)
    with pytest.raises(ResourceLimitError, match="max_canonical_work"):
        native._parse_rdfxml_retained_v2(
            ANONYMOUS_SYMMETRIC_SOURCE,
            document_iri=None,
            limits=limits,
        )
    with pytest.raises(ResourceLimitError, match="max_canonical_work"):
        load_snapshot(
            ANONYMOUS_SYMMETRIC_SOURCE,
            document_iri=None,
            options=LoadOptions(
                format=DocumentFormat.RDF_XML,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.PYTHON,
                limits=limits,
                collect_provenance=False,
            ),
        )


def test_anonymous_scoping_accounts_its_native_temporary_workspace() -> None:
    limits = ParseLimits(max_temporary_bytes=len(ANONYMOUS_SOURCE) * 3)
    with pytest.raises(ResourceLimitError, match="max_temporary_bytes"):
        native._parse_rdfxml_retained_v2(
            ANONYMOUS_SOURCE,
            document_iri=None,
            limits=limits,
        )


def test_anonymous_provenance_uses_effective_digests_without_python_rescoping() -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
        )

    reference = load_snapshot(
        ANONYMOUS_SOURCE,
        document_iri=None,
        options=options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("anonymous provenance crossed the Python parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(
            Any,
            _retained_snapshot(
                ANONYMOUS_SOURCE,
                options=options(BackendPreference.NATIVE),
                document_iri=None,
            ),
        )

    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = raw_owner._publication_counters_v2()
    assert tuple(selected.root.origin_index.entries) == tuple(reference.root.origin_index.entries)
    assert selected.origin_index == reference.origin_index
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.retained_origin_rows == 3
    assert counters.retained_origin_bytes > 0


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


def test_private_source_map_matches_python_with_prefixes_and_language_details(
    extension: NativeTestExtension,
) -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=False,
            preserve_source_map=True,
        )

    reference = load_snapshot(
        W3C_RDFXML_SOURCE,
        document_iri=None,
        options=options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("source-mapped RDF/XML crossed the Python parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(
            Any,
            _retained_snapshot(
                W3C_RDFXML_SOURCE,
                options=options(BackendPreference.NATIVE),
                document_iri=None,
            ),
        )

    handle = selected._native_snapshot_state.owner.handle
    raw_owner = handle._owner_v2
    before = raw_owner._publication_counters_v2()
    before_python = selected._native_python_counters()

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.source_map is not None
    assert reference.root.source_map is not None
    assert handle.attestation.capability_bits == 47
    assert before.retained_source_map_rows == 5
    assert before.retained_source_prefix_rows == 3
    assert before.source_map_rows_emitted == 0
    assert before.source_prefix_rows_emitted == 0
    assert before_python.auxiliary_rows_decoded == 0
    assert "parse-rdfxml-v1" not in extension.FEATURES

    assert selected.root.source_map == reference.root.source_map
    assert dict(selected.root.source_map.prefixes) == {
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    }
    assert encode_snapshot(selected) == encode_snapshot(reference)
    after = raw_owner._publication_counters_v2()
    after_python = selected._native_python_counters()
    assert after.source_map_rows_emitted > before.source_map_rows_emitted
    assert after.source_prefix_rows_emitted >= 3
    assert after_python.auxiliary_rows_decoded > before_python.auxiliary_rows_decoded


def test_private_source_map_preserves_every_constructor_occurrence_order() -> None:
    source = render_document(
        every_constructor_document(),
        format=DocumentFormat.RDF_XML,
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=False,
            preserve_source_map=True,
        )

    reference = load_snapshot(
        source,
        document_iri=DOCUMENT_IRI,
        options=options(BackendPreference.PYTHON),
    )
    selected = cast(
        Any,
        _retained_snapshot(
            source,
            options=options(BackendPreference.NATIVE),
            document_iri=DOCUMENT_IRI,
        ),
    )
    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = raw_owner._publication_counters_v2()

    assert selected.root.source_map is not None
    assert reference.root.source_map is not None
    assert selected.root.source_map == reference.root.source_map
    assert dict(selected.root.source_map.prefixes) == dict(reference.root.source_map.prefixes)
    assert selected.root.source_map.prefixes
    assert counters.retained_source_map_rows == sum(
        len(occurrences) for occurrences in reference.root.source_map.entries.values()
    )
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_private_source_map_and_provenance_match_under_default_combination() -> None:
    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            collect_provenance=True,
            preserve_source_map=True,
        )

    reference = load_snapshot(
        W3C_RDFXML_SOURCE,
        document_iri=None,
        options=options(BackendPreference.PYTHON),
    )
    selected = cast(
        Any,
        _retained_snapshot(
            W3C_RDFXML_SOURCE,
            options=options(BackendPreference.NATIVE),
            document_iri=None,
        ),
    )
    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = raw_owner._publication_counters_v2()

    assert selected.root.source_map == reference.root.source_map
    assert tuple(selected.root.origin_index.entries) == tuple(
        reference.root.origin_index.entries
    )
    assert {
        digest: tuple((item.occurrence, item.span) for item in occurrences)
        for digest, occurrences in selected.root.origin_index.entries.items()
    } == {
        digest: tuple((item.occurrence, item.span) for item in occurrences)
        for digest, occurrences in reference.root.origin_index.entries.items()
    }
    assert selected.origin_index == reference.origin_index
    assert counters.retained_source_map_rows > 0
    assert counters.retained_origin_rows > 0
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_private_source_map_accepts_zero_entries_with_prefixes() -> None:
    source = (
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        b'xmlns="urn:default:"/>'
    )

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            preserve_source_map=True,
        )

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))
    selected = cast(
        Any,
        _retained_snapshot(
            source,
            options=options(BackendPreference.NATIVE),
            document_iri=None,
        ),
    )
    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = raw_owner._publication_counters_v2()

    assert selected.root.source_map == reference.root.source_map
    assert selected.root.source_map is not None
    assert dict(selected.root.source_map.entries) == {}
    assert dict(selected.root.source_map.prefixes) == {
        "": "urn:default:",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    }
    assert counters.retained_source_map_rows == 0
    assert counters.retained_source_prefix_rows == 2
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_private_source_map_prefix_rebindings_match_python_and_stay_canonical() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:e="urn:first:"
         xmlns:xml="http://www.w3.org/XML/1998/namespace"
         xmlns="urn:default:">
  <owl:Class xmlns:e="urn:second:" xmlns="" rdf:about="urn:C"/>
</rdf:RDF>
"""

    def options(backend: BackendPreference) -> LoadOptions:
        return LoadOptions(
            format=DocumentFormat.RDF_XML,
            imports=ImportPolicy.IGNORE,
            backend=backend,
            preserve_source_map=True,
        )

    reference = load_snapshot(source, options=options(BackendPreference.PYTHON))
    selected = cast(
        Any,
        _retained_snapshot(
            source,
            options=options(BackendPreference.NATIVE),
            document_iri=None,
        ),
    )
    raw_owner = selected._native_snapshot_state.owner.handle._owner_v2
    before = raw_owner._publication_counters_v2()

    assert selected.root.source_map == reference.root.source_map
    assert selected.root.source_map is not None
    assert dict(selected.root.source_map.prefixes) == {
        "e": "urn:second:",
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "xml": "http://www.w3.org/XML/1998/namespace",
    }
    assert before.retained_source_prefix_rows == 4
    assert encode_snapshot(selected) == encode_snapshot(reference)


def test_private_source_map_limit_counts_rdfxml_language_rows() -> None:
    options = LoadOptions(
        format=DocumentFormat.RDF_XML,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=False,
        preserve_source_map=True,
        limits=ParseLimits(max_source_map_entries=4),
    )

    with pytest.raises(ResourceLimitError) as selected_error:
        _retained_snapshot(
            W3C_RDFXML_SOURCE,
            options=options,
            document_iri=None,
        )
    with pytest.raises(ResourceLimitError) as reference_error:
        load_snapshot(
            W3C_RDFXML_SOURCE,
            document_iri=None,
            options=LoadOptions(
                format=DocumentFormat.RDF_XML,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.PYTHON,
                preserve_source_map=True,
                limits=ParseLimits(max_source_map_entries=4),
            ),
        )

    assert selected_error.value.code == "NATIVE_WIRE_LIMIT"
    assert "max_source_map_entries" in str(selected_error.value)
    assert reference_error.value.limit == "max_source_map_entries"


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

    for source in (
        b"<?xml version='1.1'?><rdf:RDF "
        b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
        b"<!--bad---><rdf:RDF "
        b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
    ):
        with pytest.raises(OntologySyntaxError) as invalid_envelope:
            native._parse_rdfxml_retained_v2(source, document_iri=None)
        assert invalid_envelope.value.code == "RDFXML_SYNTAX"

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


@pytest.mark.parametrize(
    "local",
    (
        "RDF",
        "parseType",
        "resource",
        "datatype",
        "Description",
        "li",
        "aboutEach",
        "aboutEachPrefix",
        "bagID",
    ),
)
def test_forbidden_node_property_attributes_publish_no_retained_owner(
    local: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
        f"<rdf:Description rdf:about='urn:s' rdf:{local}='value'/>"
        "</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(OntologySyntaxError) as native_error:
        native._parse_rdfxml_retained_v2(source, document_iri=None)
    assert native_error.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "local",
    (
        "RDF",
        "about",
        "Description",
        "li",
        "aboutEach",
        "aboutEachPrefix",
        "bagID",
    ),
)
@pytest.mark.parametrize("object_attribute", ("", "rdf:resource='urn:o' "))
def test_forbidden_property_element_attributes_publish_no_retained_owner(
    local: str,
    object_attribute: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:e='urn:e:'><rdf:Description rdf:about='urn:s'>"
        f"<e:p {object_attribute}rdf:{local}='value'/>"
        "</rdf:Description></rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(OntologySyntaxError) as native_error:
        native._parse_rdfxml_retained_v2(source, document_iri=None)
    assert native_error.value.code == "RDFXML_SYNTAX"


def test_legacy_unqualified_attributes_publish_from_retained_owner(
    extension: NativeTestExtension,
) -> None:
    reference = load_snapshot(
        QUALIFIED_RDF_ATTRIBUTE_SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON),
    )
    unexpected = AssertionError("legacy RDF attributes crossed the Python RDF/XML parser")
    with patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected):
        selected = cast(
            Any,
            _retained_snapshot(LEGACY_UNQUALIFIED_RDF_ATTRIBUTE_SOURCE),
        )

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert selected.root.axioms == reference.root.axioms
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(LEGACY_UNQUALIFIED_RDF_ATTRIBUTE_SOURCE)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0


@pytest.mark.parametrize(
    "element",
    (
        "<owl:Class rdf:about='urn:C' about='urn:D'/>",
        "<owl:Class rdf:ID='C' ID='D'/>",
        (
            "<rdf:Description rdf:about='urn:C' "
            "rdf:type='http://www.w3.org/2002/07/owl#Class' "
            "type='http://www.w3.org/2002/07/owl#Class'/>"
        ),
        (
            "<owl:Class rdf:about='urn:C'><rdfs:subClassOf "
            "rdf:resource='urn:D' resource='urn:E'/></owl:Class>"
        ),
        (
            "<owl:Class rdf:about='urn:C'><owl:intersectionOf "
            "rdf:parseType='Collection' parseType='Collection'/></owl:Class>"
        ),
    ),
)
def test_qualified_and_legacy_attribute_aliases_publish_no_retained_owner(
    element: str,
) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xml:base='urn:legacy'>"
        f"{element}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as python_error:
        parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    assert python_error.value.code == "RDFXML_SYNTAX"
    with pytest.raises(OntologySyntaxError) as native_error:
        native._parse_rdfxml_retained_v2(source, document_iri=None)
    assert native_error.value.code == "RDFXML_SYNTAX"
