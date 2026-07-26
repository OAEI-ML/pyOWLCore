from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import patch

import pytest

import pyowl_core.model as m
from pyowl_core import (
    IRI,
    AcquisitionCache,
    BackendPreference,
    CancellationSource,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    OntologySyntaxError,
    OperationCancelledError,
    ParsedDocumentCache,
    ParseLimits,
    ResolvedDocument,
    ResourceLimitError,
    SnapshotLoader,
    UnsupportedSyntaxError,
    encode_snapshot,
    load_snapshot,
    parse_document,
    render_document,
)
from pyowl_core.backends import native, native_ingestion
from pyowl_core.io.formats.detection import detect_format
from pyowl_core.io.source import acquire_source
from tests.conformance._support import every_constructor_document
from tests.native.foundation._support import NativeTestExtension, load_extension

TURTLE_SOURCE = rb"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix ex: <urn:turtle-retained:> .
    ex:ontology a owl:Ontology .
    ex:A a owl:Class ; rdfs:subClassOf ex:B .
    ex:B a owl:Class .
"""

RDFXML_SOURCE = rb"""
    <rdf:RDF
        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
        xmlns:owl="http://www.w3.org/2002/07/owl#">
      <owl:Ontology rdf:about="urn:turtle-retained:ontology"/>
      <owl:Class rdf:about="urn:turtle-retained:A">
        <rdfs:subClassOf rdf:resource="urn:turtle-retained:B"/>
      </owl:Class>
      <owl:Class rdf:about="urn:turtle-retained:B"/>
    </rdf:RDF>
"""

TURTLE_IMPORT_ROOT = rb"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix ex: <urn:turtle-retained:> .
    ex:root a owl:Ontology ; owl:imports ex:child .
    ex:Root a owl:Class .
"""

TURTLE_IMPORT_CHILD = rb"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix ex: <urn:turtle-retained:> .
    ex:child-document a owl:Ontology .
    ex:Child a owl:Class .
"""

TURTLE_LEXICAL_SOURCES = (
    b"\xef\xbb\xbfBASE <https://example.test/base/> "
    b"PREFIX owl: <http://www.w3.org/2002/07/owl#> "
    b"PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
    b"<ontology> a owl:Ontology . "
    b"<A> a owl:Class ; rdfs:subClassOf <B> . "
    b"<B> a owl:Class .",
    rb'''
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix ex: <urn:turtle-lexical:> .
        ex:ontology a owl:Ontology .
        ex:note a owl:AnnotationProperty .
        ex:subject ex:note """line\n\u0041"""@EN-us, 'plain', 1, +2.5, -3e2, true .
    ''',
    rb"""
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix ex: <urn:turtle-lexical:> .
        ex:ontology a owl:Ontology .
        ex:Class\~Name a owl:Class ;
            owl:equivalentClass [
                a owl:Class ;
                owl:intersectionOf ( ex:B ex:C )
            ] .
        ex:B a owl:Class .
        ex:C a owl:Class .
        ex:Class\,Name a owl:Class .
        ex:Class%2CName a owl:Class .
    """,
)

INVALID_TURTLE_PERCENT_ESCAPE = rb"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix ex: <urn:ex:> .
    ex:ontology a owl:Ontology .
    ex:Class%2XName a owl:Class .
"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        pytest.skip(result.reason or "native backend is unavailable")
    if not hasattr(selected, "_parse_turtle_retained_v2"):
        pytest.skip("selected native artifact lacks retained Turtle ingestion")
    return selected


def test_private_turtle_owner_matches_shared_rdf_mapping_fingerprint(
    extension: NativeTestExtension,
) -> None:
    turtle = native._parse_turtle_retained_v2(
        TURTLE_SOURCE,
        document_iri="urn:turtle-retained:document",
        collect_provenance=True,
        preserve_source_map=True,
    )
    rdfxml = native._parse_rdfxml_retained_v2(
        RDFXML_SOURCE,
        document_iri="urn:turtle-retained:document",
        collect_provenance=True,
        preserve_source_map=True,
    )

    turtle_seed = native_ingestion._decode_retained_rdfxml_seed_v2(
        cast(bytes, turtle.summary),
        ParseLimits(),
    )
    rdfxml_seed = native_ingestion._decode_retained_rdfxml_seed_v2(
        cast(bytes, rdfxml.summary),
        ParseLimits(),
    )
    assert turtle.parsed is None
    assert type(turtle.storage) is extension._NativeParsedStructuralStorageV2
    assert tuple(name for name, _value in turtle.phase_timings) == (
        "native_turtle_syntax_parse_seconds",
        "native_rdf_mapping_seconds",
        "native_result_encode_seconds",
        "native_arena_construction_seconds",
        "native_freeze_seconds",
    )
    assert turtle_seed.structural.rows == (0, 3, 0)
    assert turtle_seed.total_triples == 4
    assert (
        turtle_seed.structural.document_fingerprint == rdfxml_seed.structural.document_fingerprint
    )
    assert turtle_seed.structural.rows == rdfxml_seed.structural.rows


def test_private_turtle_seam_fails_closed_and_retries_without_fallback(
    extension: NativeTestExtension,
) -> None:
    with pytest.raises(OntologySyntaxError) as malformed:
        native._parse_turtle_retained_v2(
            b"@prefix ex: <urn:ex:> . ex:A ex:p",
            document_iri=None,
        )
    assert malformed.value.code == "TURTLE_SYNTAX"

    with pytest.raises(OntologySyntaxError) as invalid_pname:
        native._parse_turtle_retained_v2(
            rb"""
                @prefix owl: <http://www.w3.org/2002/07/owl#> .
                @prefix ex: <urn:ex:> .
                ex:ontology a owl:Ontology .
                ex:Class\qName a owl:Class .
            """,
            document_iri=None,
        )
    assert invalid_pname.value.code == "TURTLE_SYNTAX"

    with pytest.raises(OntologySyntaxError) as invalid_percent:
        native._parse_turtle_retained_v2(
            INVALID_TURTLE_PERCENT_ESCAPE,
            document_iri=None,
        )
    assert invalid_percent.value.code == "TURTLE_SYNTAX"

    with pytest.raises(OntologySyntaxError) as encoding:
        native._parse_turtle_retained_v2(b"\xff", document_iri=None)
    assert encoding.value.code == "TURTLE_ENCODING"

    with pytest.raises(OntologySyntaxError) as relative:
        native._parse_turtle_retained_v2(
            b"<relative> <urn:p> <urn:o> .",
            document_iri=None,
            allow_partial_rdf_mapping=True,
        )
    assert relative.value.code == "TURTLE_RELATIVE_IRI"

    with pytest.raises(ResourceLimitError):
        native._parse_turtle_retained_v2(
            TURTLE_SOURCE,
            document_iri="urn:turtle-retained:document",
            limits=ParseLimits(max_triples=3),
        )

    cancellation = CancellationSource()
    cancellation.cancel("retained Turtle cancellation")
    with pytest.raises(OperationCancelledError, match="retained Turtle cancellation"):
        native._parse_turtle_retained_v2(
            TURTLE_SOURCE,
            document_iri="urn:turtle-retained:document",
            cancellation_token=cancellation.token,
        )

    retry = native._parse_turtle_retained_v2(
        TURTLE_SOURCE,
        document_iri="urn:turtle-retained:document",
    )
    assert type(retry.storage) is extension._NativeParsedStructuralStorageV2


def test_python_turtle_rejects_invalid_prefixed_name_percent_escape() -> None:
    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(
            INVALID_TURTLE_PERCENT_ESCAPE,
            options=LoadOptions(
                format=DocumentFormat.TURTLE,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.PYTHON,
            ),
        )
    assert raised.value.code == "TURTLE_SYNTAX"


def test_private_turtle_owner_publishes_without_python_structural_reconstruction(
    extension: NativeTestExtension,
) -> None:
    document_iri = IRI("urn:turtle-retained:document")
    options = LoadOptions(
        format=DocumentFormat.TURTLE,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=True,
        preserve_source_map=True,
    )
    payload = acquire_source(
        TURTLE_SOURCE,
        format=DocumentFormat.TURTLE,
        document_iri=document_iri,
        limits=options.limits,
    )
    detection = detect_format(TURTLE_SOURCE, explicit=DocumentFormat.TURTLE)
    parsed = native._parse_turtle_retained_v2(
        TURTLE_SOURCE,
        document_iri=document_iri.value,
        collect_provenance=True,
        preserve_source_map=True,
    )
    started = time.monotonic()
    selected = native_ingestion.publish_retained_turtle_snapshot_v2(
        cast(bytes, parsed.summary),
        parsed_native_storage=parsed.storage,
        phase_timings=parsed.phase_timings,
        payload=payload,
        detection=detection,
        document_iri=document_iri,
        media_type=None,
        options=options,
        resolver=None,
        cancellation_token=None,
        load_started=started,
        root_parse_started=started,
    )
    reference = load_snapshot(
        TURTLE_SOURCE,
        document_iri=document_iri,
        options=LoadOptions(
            format=DocumentFormat.TURTLE,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=True,
            preserve_source_map=True,
        ),
    )
    try:
        assert selected.capabilities.backend == "native"
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
        assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
        assert selected.report.timings["native_turtle_syntax_parse_seconds"] >= 0
        assert selected.report.timings["native_rdf_mapping_seconds"] >= 0
    finally:
        selected.close()


@pytest.mark.parametrize("loader", (load_snapshot, parse_document))
def test_guarded_public_turtle_routes_through_the_retained_owner(
    loader: Any,
) -> None:
    document_iri = IRI("urn:turtle-retained:public-document")
    native_options = LoadOptions(
        format=DocumentFormat.TURTLE,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=True,
        preserve_source_map=True,
    )
    reference = loader(
        TURTLE_SOURCE,
        document_iri=document_iri,
        options=LoadOptions(
            format=DocumentFormat.TURTLE,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=True,
            preserve_source_map=True,
        ),
    )
    unexpected = AssertionError("guarded public Turtle source crossed the Python parser")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_turtle", side_effect=unexpected),
    ):
        selected = cast(
            Any,
            loader(
                TURTLE_SOURCE,
                document_iri=document_iri,
                options=native_options,
            ),
        )

    if loader is load_snapshot:
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
        publication = (
            selected._native_snapshot_state.owner.handle._owner_v2._publication_counters_v2()
        )
        selected.close()
    else:
        assert type(selected).__name__ == "_NativeOntologyDocument"
        assert selected == reference
        assert selected.document_fingerprint == reference.document_fingerprint
        publication = (
            selected._native_document_state.owner.handle._owner_v2._publication_counters_v2()
        )
    assert publication.parser_bytes == len(TURTLE_SOURCE)
    assert publication.publication_structural_rows_copied == 0
    assert publication.publication_structural_bytes_copied == 0


def test_guarded_public_turtle_every_constructor_is_owner_first() -> None:
    source = render_document(
        every_constructor_document(),
        format=DocumentFormat.TURTLE,
    )
    python_options = LoadOptions(
        format=DocumentFormat.TURTLE,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
        collect_provenance=True,
        preserve_source_map=True,
    )
    reference = load_snapshot(source, options=python_options)
    unexpected = AssertionError("every-constructor Turtle crossed the Python parser")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_turtle", side_effect=unexpected),
    ):
        selected = cast(
            Any,
            load_snapshot(
                source,
                options=LoadOptions(
                    format=DocumentFormat.TURTLE,
                    imports=ImportPolicy.IGNORE,
                    backend=BackendPreference.NATIVE,
                    collect_provenance=True,
                    preserve_source_map=True,
                ),
            ),
        )

    try:
        assert set(m.AXIOM_TYPES) <= {type(value) for value in reference.root.axioms}
        assert selected.root.axioms == reference.root.axioms
        assert selected.root.source_map == reference.root.source_map
        assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
        assert encode_snapshot(selected) == encode_snapshot(reference)
        publication = (
            selected._native_snapshot_state.owner.handle._owner_v2._publication_counters_v2()
        )
        assert publication.parser_bytes == len(source)
        assert publication.publication_structural_rows_copied == 0
        assert publication.publication_structural_bytes_copied == 0
    finally:
        selected.close()


@pytest.mark.parametrize(
    "source",
    TURTLE_LEXICAL_SOURCES,
    ids=("bom-base-sparql-directives", "literal-forms", "pname-blank-list"),
)
def test_guarded_public_turtle_lexical_forms_match_python(source: bytes) -> None:
    document_iri = IRI("https://example.test/source/document.ttl")
    reference = load_snapshot(
        source,
        document_iri=document_iri,
        options=LoadOptions(
            format=DocumentFormat.TURTLE,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=True,
            preserve_source_map=True,
        ),
    )
    unexpected = AssertionError("guarded Turtle lexical form crossed the Python parser")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_turtle", side_effect=unexpected),
    ):
        selected = cast(
            Any,
            load_snapshot(
                source,
                document_iri=document_iri,
                options=LoadOptions(
                    format=DocumentFormat.TURTLE,
                    imports=ImportPolicy.IGNORE,
                    backend=BackendPreference.NATIVE,
                    collect_provenance=True,
                    preserve_source_map=True,
                ),
            ),
        )

    try:
        assert selected == reference
        assert selected.root.source_map == reference.root.source_map
        assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
        assert encode_snapshot(selected) == encode_snapshot(reference)
    finally:
        selected.close()


@pytest.mark.parametrize("loader", (load_snapshot, parse_document))
@pytest.mark.parametrize(
    ("source", "expected_error"),
    (
        (b"@prefix ex: <urn:ex:> . ex:A ex:p", OntologySyntaxError),
        (TURTLE_SOURCE, ResourceLimitError),
    ),
    ids=("syntax", "triple-limit"),
)
def test_guarded_public_turtle_failure_never_publishes(
    loader: Any,
    source: bytes,
    expected_error: type[Exception],
) -> None:
    limits = ParseLimits(max_triples=3) if expected_error is ResourceLimitError else ParseLimits()
    unexpected_parse = AssertionError("guarded Turtle failure crossed the Python parser")
    unexpected_publish = AssertionError("invalid Turtle reached retained publication")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_turtle", side_effect=unexpected_parse),
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.publish_retained_turtle",
            side_effect=unexpected_publish,
        ),
        pytest.raises(expected_error),
    ):
        loader(
            source,
            options=LoadOptions(
                format=DocumentFormat.TURTLE,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
                limits=limits,
            ),
        )


@pytest.mark.parametrize(
    ("child_source", "child_format"),
    (
        (TURTLE_IMPORT_CHILD, DocumentFormat.TURTLE),
        (RDFXML_SOURCE, DocumentFormat.RDF_XML),
    ),
    ids=("turtle-child", "rdfxml-child"),
)
def test_guarded_turtle_import_closure_merges_retained_owners(
    child_source: bytes,
    child_format: DocumentFormat,
) -> None:
    def load(backend: BackendPreference) -> Any:
        loader = SnapshotLoader(
            acquisition_cache=AcquisitionCache(),
            document_cache=ParsedDocumentCache(),
        )
        options = LoadOptions(
            format=DocumentFormat.TURTLE,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=backend,
            collect_provenance=True,
            preserve_source_map=True,
        )
        resolver = MappingResolver(
            {
                "urn:turtle-retained:child": ResolvedDocument(
                    child_source,
                    IRI("urn:turtle-retained:child-source"),
                    format=child_format,
                )
            }
        )
        if backend is BackendPreference.PYTHON:
            return loader.load(TURTLE_IMPORT_ROOT, options=options, resolver=resolver)
        unexpected = AssertionError("retained Turtle closure crossed the Python parser")
        with (
            patch(
                "pyowl_core.backends.parser._NativeBackendDriver.select",
                autospec=True,
                return_value="native",
            ),
            patch("pyowl_core.backends.python.parser.parse_turtle", side_effect=unexpected),
            patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected),
        ):
            return loader.load(TURTLE_IMPORT_ROOT, options=options, resolver=resolver)

    reference = load(BackendPreference.PYTHON)
    selected = load(BackendPreference.NATIVE)
    try:
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert selected == reference
        assert selected.import_manifest == reference.import_manifest
        assert selected.origin_index == reference.origin_index
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
        assert encode_snapshot(selected) == encode_snapshot(reference)
        assert len(selected.documents) == 2
        assert all(
            type(document).__name__ == "_NativeOntologyDocument" for document in selected.documents
        )
        publication = (
            selected._native_snapshot_state.owner.handle._owner_v2._publication_counters_v2()
        )
        assert publication.parser_bytes == len(TURTLE_IMPORT_ROOT) + len(child_source)
        assert publication.publication_structural_rows_copied == 0
        assert publication.publication_structural_bytes_copied == 0
    finally:
        selected.close()


def test_private_turtle_partial_mapping_and_bridge_allocations_are_transactional(
    extension: NativeTestExtension,
) -> None:
    partial_source = b'<urn:s> <urn:unknown:p> "value" .'
    with pytest.raises(UnsupportedSyntaxError):
        native._parse_turtle_retained_v2(partial_source, document_iri=None)
    partial = native._parse_turtle_retained_v2(
        partial_source,
        document_iri=None,
        allow_partial_rdf_mapping=True,
    )
    partial_seed = native_ingestion._decode_retained_rdfxml_seed_v2(
        cast(bytes, partial.summary),
        ParseLimits(),
    )
    assert partial_seed.structural.rows == (0, 0, 0)
    assert partial_seed.total_triples == 1

    probe = extension._turtle_retained_bridge_allocation_probe_v2
    source = bytearray(TURTLE_SOURCE)
    config = bytearray(native._encode_config(ParseLimits(), None, verify=False))
    original_source = bytes(source)
    original_config = bytes(config)
    output, allocations = probe(
        memoryview(source),
        "urn:turtle-retained:document",
        memoryview(config),
        True,
        False,
        True,
        False,
        None,
    )
    assert output.startswith(native._RETAINED_RDFXML_SEED_MAGIC_V2)
    assert allocations > 0
    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native Turtle retained bridge allocation failure$",
        ):
            probe(
                memoryview(source),
                "urn:turtle-retained:document",
                memoryview(config),
                True,
                False,
                True,
                False,
                fail_after,
            )
        assert source == original_source
        assert config == original_config

    retried, boundary = probe(
        source,
        "urn:turtle-retained:document",
        config,
        True,
        False,
        True,
        False,
        allocations,
    )
    assert retried == output
    assert boundary == allocations
