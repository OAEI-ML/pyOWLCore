from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
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

OWL = "http://www.w3.org/2002/07/owl#"
SOURCE = f"""\
<Ontology xmlns="{OWL}" xmlns:e="urn:source:"
    ontologyIRI="urn:owlxml:ontology" versionIRI="urn:owlxml:version">
  <Prefix name="ex:" IRI="urn:owlxml:"/>
  <Annotation>
    <AnnotationProperty abbreviatedIRI="ex:note"/>
    <Literal xml:lang="EN-gb">ontology</Literal>
  </Annotation>
  <SubClassOf>
    <Annotation>
      <AnnotationProperty abbreviatedIRI="ex:note"/>
      <IRI>urn:owlxml:value</IRI>
    </Annotation>
    <ObjectSomeValuesFrom>
      <ObjectProperty abbreviatedIRI="ex:p"/>
      <ObjectOneOf><AnonymousIndividual nodeID="anonymous"/></ObjectOneOf>
    </ObjectSomeValuesFrom>
    <Class abbreviatedIRI="ex:D"/>
  </SubClassOf>
</Ontology>
""".encode()
DOCUMENT_IRI = IRI("urn:owlxml:document")
MIXED_ROOT = f"""\
<Ontology xmlns="{OWL}" ontologyIRI="urn:owlxml:mixed-root">
  <Import>urn:owlxml:child:functional</Import>
  <Import>urn:owlxml:child:rdfxml</Import>
  <Import>urn:owlxml:child:turtle</Import>
  <Declaration><Class IRI="urn:owlxml:Root"/></Declaration>
</Ontology>
""".encode()
MIXED_FUNCTIONAL_CHILD = b"""\
Ontology(<urn:owlxml:child:functional>
  Declaration(Class(<urn:owlxml:FunctionalChild>))
)
"""
MIXED_RDFXML_CHILD = b"""\
<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:owlxml:child:rdfxml"/>
  <owl:Class rdf:about="urn:owlxml:RdfXmlChild"/>
</rdf:RDF>
"""
MIXED_TURTLE_CHILD = b"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<urn:owlxml:child:turtle> a owl:Ontology .
<urn:owlxml:TurtleChild> a owl:Class .
"""


class _OneShotStream:
    def __init__(self, data: bytes | str) -> None:
        self._data = data
        self.read_calls = 0
        self.closed = False

    def read(self, _size: int = -1) -> bytes | str:
        self.read_calls += 1
        if self.read_calls == 1:
            return self._data
        if self.read_calls == 2:
            return type(self._data)()
        raise AssertionError("caller-owned OWL/XML stream was read after EOF")


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        pytest.skip(result.reason or "native backend is unavailable")
    if not hasattr(selected, "_parse_owlxml_retained_v2"):
        pytest.skip("selected native artifact lacks retained OWL/XML ingestion")
    return selected


def _public_options(
    backend: BackendPreference,
    *,
    imports: ImportPolicy = ImportPolicy.IGNORE,
    limits: ParseLimits | None = None,
    collect_provenance: bool = True,
    preserve_source_map: bool = True,
) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.OWL_XML,
        imports=imports,
        backend=backend,
        limits=ParseLimits() if limits is None else limits,
        collect_provenance=collect_provenance,
        preserve_source_map=preserve_source_map,
    )


def _guarded_public(
    loader: Any,
    source: object,
    *,
    options: LoadOptions | None = None,
) -> Any:
    unexpected = AssertionError("guarded public OWL/XML source crossed the Python parser")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_owlxml", side_effect=unexpected),
    ):
        return loader(
            cast(Any, source),
            document_iri=DOCUMENT_IRI,
            options=(
                _public_options(BackendPreference.NATIVE) if options is None else options
            ),
        )


def _publish(
    source: bytes,
    *,
    document_iri: IRI | None = None,
    preserve_source_map: bool = True,
):
    options = LoadOptions(
        format=DocumentFormat.OWL_XML,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=True,
        preserve_source_map=preserve_source_map,
    )
    payload = acquire_source(
        source,
        format=DocumentFormat.OWL_XML,
        document_iri=document_iri,
        limits=options.limits,
    )
    detection = detect_format(source, explicit=DocumentFormat.OWL_XML)
    parsed = native._parse_owlxml_retained_v2(
        source,
        document_iri=None if document_iri is None else document_iri.value,
        collect_provenance=True,
        preserve_source_map=preserve_source_map,
    )
    started = time.monotonic()
    snapshot = native_ingestion.publish_retained_owlxml_snapshot_v2(
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
    return parsed, snapshot


def test_private_owlxml_owner_is_non_rdf_and_matches_python_fingerprints(
    extension: NativeTestExtension,
) -> None:
    parsed, selected = _publish(SOURCE, document_iri=IRI("urn:owlxml:document"))
    reference = load_snapshot(
        SOURCE,
        document_iri=IRI("urn:owlxml:document"),
        options=LoadOptions(
            format=DocumentFormat.OWL_XML,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=True,
            preserve_source_map=True,
        ),
    )
    seed = native_ingestion._decode_retained_functional_seed_v2(
        cast(bytes, parsed.summary),
        ParseLimits(),
    )
    try:
        assert parsed.parsed is None
        assert type(parsed.storage) is extension._NativeParsedStructuralStorageV2
        assert seed.rows == (1, 1, 0)
        assert tuple(name for name, _value in parsed.phase_timings) == (
            "native_owlxml_syntax_parse_seconds",
            "native_owlxml_structural_mapping_seconds",
            "native_result_encode_seconds",
            "native_arena_construction_seconds",
            "native_freeze_seconds",
        )
        assert selected.root.rdf_mapping_report is None
        assert selected.root.axioms == reference.root.axioms
        assert selected.root.ontology_annotations == reference.root.ontology_annotations
        assert selected.root.source_map == reference.root.source_map
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
    finally:
        selected.close()


def test_private_owlxml_every_constructor_document_matches_python() -> None:
    source = render_document(
        every_constructor_document(),
        format=DocumentFormat.OWL_XML,
    )
    _parsed, selected = _publish(source, preserve_source_map=False)
    reference = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.OWL_XML,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=True,
        ),
    )
    try:
        assert selected.root.axioms == reference.root.axioms
        assert selected.root.ontology_annotations == reference.root.ontology_annotations
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
    finally:
        selected.close()


@pytest.mark.parametrize("loader", (load_snapshot, parse_document))
def test_guarded_public_owlxml_routes_through_the_retained_owner(loader: Any) -> None:
    reference = loader(
        SOURCE,
        document_iri=DOCUMENT_IRI,
        options=_public_options(BackendPreference.PYTHON),
    )
    selected = _guarded_public(loader, SOURCE)
    selected_document = selected.root if loader is load_snapshot else selected
    reference_document = reference.root if loader is load_snapshot else reference
    try:
        assert type(selected_document).__name__ == "_NativeOntologyDocument"
        assert selected_document == reference_document
        assert selected_document.source_map == reference_document.source_map
        assert selected_document.origin_index == reference_document.origin_index
        assert selected_document.rdf_mapping_report is None
        assert (
            selected_document.provenance.source_sha256
            == reference_document.provenance.source_sha256
        )
        assert (
            selected_document.provenance.digest_kind
            is reference_document.provenance.digest_kind
        )
        assert (
            selected_document.provenance.decoded_codepoint_length
            == reference_document.provenance.decoded_codepoint_length
        )
        assert selected_document.provenance.byte_length == len(SOURCE)
        assert selected_document.provenance.document_iri == DOCUMENT_IRI
        assert selected_document.provenance.backend == "native"
        assert selected_document.provenance.parser == "pyowl_core.backends.native"
        if loader is load_snapshot:
            assert selected.structural_fingerprint == reference.structural_fingerprint
            assert selected.logical_fingerprint == reference.logical_fingerprint
            assert selected.signature_fingerprint == reference.signature_fingerprint
            owner = selected._native_snapshot_state.owner.handle._owner_v2
        else:
            assert selected.document_fingerprint == reference.document_fingerprint
            owner = selected._native_document_state.owner.handle._owner_v2
        counters = owner._publication_counters_v2()
        assert counters.parser_bytes == len(SOURCE)
        assert counters.publication_structural_rows_copied == 0
        assert counters.publication_structural_bytes_copied == 0
    finally:
        if loader is load_snapshot:
            selected.close()


@pytest.mark.parametrize(
    ("collect_provenance", "preserve_source_map"),
    ((False, False), (True, False), (False, True), (True, True)),
)
def test_guarded_public_owlxml_provenance_option_matrix(
    collect_provenance: bool,
    preserve_source_map: bool,
) -> None:
    python_options = _public_options(
        BackendPreference.PYTHON,
        collect_provenance=collect_provenance,
        preserve_source_map=preserve_source_map,
    )
    native_options = _public_options(
        BackendPreference.NATIVE,
        collect_provenance=collect_provenance,
        preserve_source_map=preserve_source_map,
    )
    reference = load_snapshot(
        SOURCE,
        document_iri=DOCUMENT_IRI,
        options=python_options,
    )
    selected = _guarded_public(
        load_snapshot,
        SOURCE,
        options=native_options,
    )
    try:
        assert selected.root.axioms == reference.root.axioms
        assert selected.root.ontology_annotations == reference.root.ontology_annotations
        assert selected.root.origin_index == reference.root.origin_index
        assert selected.root.source_map == reference.root.source_map
        assert (selected.root.origin_index is not None) is collect_provenance
        assert (selected.root.source_map is not None) is preserve_source_map
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
    finally:
        selected.close()


def test_guarded_public_owlxml_content_detection_routes_to_retained_owner() -> None:
    python_options = LoadOptions(
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
        collect_provenance=True,
        preserve_source_map=True,
    )
    native_options = replace(
        python_options,
        backend=BackendPreference.NATIVE,
    )
    reference = load_snapshot(
        SOURCE,
        document_iri=DOCUMENT_IRI,
        options=python_options,
    )
    selected = _guarded_public(
        load_snapshot,
        SOURCE,
        options=native_options,
    )
    try:
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert selected.root.provenance.format is DocumentFormat.OWL_XML
        assert (
            selected.root.provenance.detection_basis
            is reference.root.provenance.detection_basis
        )
        assert selected.root.source_map == reference.root.source_map
        assert selected.structural_fingerprint == reference.structural_fingerprint
    finally:
        selected.close()


@pytest.mark.parametrize(
    "source",
    (SOURCE, bytearray(SOURCE), memoryview(SOURCE)),
    ids=("bytes", "bytearray", "memoryview"),
)
def test_guarded_public_owlxml_accepts_each_buffer_source(source: object) -> None:
    selected = _guarded_public(load_snapshot, source)
    try:
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert selected.root.provenance.byte_length == len(SOURCE)
        assert selected.root.provenance.acquisition_locator is None
    finally:
        selected.close()


@pytest.mark.parametrize("data", (SOURCE, SOURCE.decode()), ids=("binary", "text"))
def test_guarded_public_owlxml_reads_caller_stream_once(data: bytes | str) -> None:
    source = _OneShotStream(data)
    selected = _guarded_public(load_snapshot, source)
    try:
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert source.read_calls == 2
        assert source.closed is False
    finally:
        selected.close()


@pytest.mark.parametrize("path_kind", ("pathlike", "string"))
def test_guarded_public_owlxml_accepts_regular_paths(
    tmp_path: Path,
    path_kind: str,
) -> None:
    path = tmp_path / "ontology.owx"
    path.write_bytes(SOURCE)
    source: object = path if path_kind == "pathlike" else str(path)
    selected = _guarded_public(load_snapshot, source)
    try:
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert selected.root.provenance.acquisition_locator == str(path)
        assert selected.root.provenance.byte_length == len(SOURCE)
    finally:
        selected.close()


def test_guarded_public_owlxml_every_constructor_is_owner_first() -> None:
    source = render_document(
        every_constructor_document(),
        format=DocumentFormat.OWL_XML,
    )
    reference = load_snapshot(
        source,
        document_iri=DOCUMENT_IRI,
        options=_public_options(BackendPreference.PYTHON),
    )
    selected = _guarded_public(load_snapshot, source)
    try:
        assert set(m.AXIOM_TYPES) <= {type(value) for value in reference.root.axioms}
        assert selected.root.axioms == reference.root.axioms
        assert selected.root.ontology_annotations == reference.root.ontology_annotations
        assert selected.root.source_map == reference.root.source_map
        assert selected.root.origin_index == reference.root.origin_index
        assert selected.root.rdf_mapping_report is None
        assert selected.structural_fingerprint == reference.structural_fingerprint
        assert selected.logical_fingerprint == reference.logical_fingerprint
        assert selected.signature_fingerprint == reference.signature_fingerprint
        assert encode_snapshot(selected) == encode_snapshot(reference)
        counters = (
            selected._native_snapshot_state.owner.handle._owner_v2._publication_counters_v2()
        )
        assert counters.parser_bytes == len(source)
        assert counters.publication_structural_rows_copied == 0
        assert counters.publication_structural_bytes_copied == 0
    finally:
        selected.close()


def test_private_owlxml_seam_fails_closed_and_retries_without_fallback() -> None:
    with pytest.raises(OntologySyntaxError) as malformed:
        native._parse_owlxml_retained_v2(
            f'<Ontology xmlns="{OWL}"><Declaration></Ontology>'.encode(),
            document_iri=None,
        )
    assert malformed.value.code == "OWLXML_SYNTAX"

    with pytest.raises(OntologySyntaxError) as forbidden:
        native._parse_owlxml_retained_v2(
            f'<!DOCTYPE Ontology><Ontology xmlns="{OWL}"/>'.encode(),
            document_iri=None,
        )
    assert forbidden.value.code == "XML_FORBIDDEN_CONSTRUCT"

    with pytest.raises(ResourceLimitError):
        native._parse_owlxml_retained_v2(
            SOURCE,
            document_iri=None,
            limits=ParseLimits(max_axioms=1, max_nesting_depth=3),
        )

    cancellation = CancellationSource()
    cancellation.cancel("retained OWL/XML cancellation")
    with pytest.raises(OperationCancelledError, match="retained OWL/XML cancellation"):
        native._parse_owlxml_retained_v2(
            SOURCE,
            document_iri=None,
            cancellation_token=cancellation.token,
        )

    imported = f'<Ontology xmlns="{OWL}"><Import>urn:import</Import></Ontology>'.encode()
    with pytest.raises(UnsupportedSyntaxError) as imports:
        native._parse_owlxml_retained_v2(
            imported,
            document_iri=None,
            require_empty_imports=True,
        )
    assert imports.value.code == "OWLXML_RETAINED_UNSUPPORTED"

    retry = native._parse_owlxml_retained_v2(SOURCE, document_iri=None)
    assert retry.summary is not None


@pytest.mark.parametrize("loader", (load_snapshot, parse_document))
@pytest.mark.parametrize(
    ("source", "limits", "expected_error"),
    (
        (
            f'<Ontology xmlns="{OWL}"><Declaration></Ontology>'.encode(),
            ParseLimits(),
            OntologySyntaxError,
        ),
        (
            f'<!DOCTYPE Ontology><Ontology xmlns="{OWL}"/>'.encode(),
            ParseLimits(),
            OntologySyntaxError,
        ),
        (
            SOURCE,
            ParseLimits(max_axioms=1, max_nesting_depth=3),
            ResourceLimitError,
        ),
    ),
    ids=("malformed", "forbidden-xml", "limit"),
)
def test_guarded_public_owlxml_failures_never_fallback_or_publish(
    loader: Any,
    source: bytes,
    limits: ParseLimits,
    expected_error: type[Exception],
) -> None:
    unexpected_parse = AssertionError("failed OWL/XML crossed the Python parser")
    unexpected_publish = AssertionError("failed OWL/XML reached retained publication")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch(
            "pyowl_core.backends.python.parser.parse_owlxml",
            side_effect=unexpected_parse,
        ),
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.publish_retained_owlxml",
            side_effect=unexpected_publish,
        ),
        pytest.raises(expected_error),
    ):
        loader(
            source,
            document_iri=DOCUMENT_IRI,
            options=_public_options(BackendPreference.NATIVE, limits=limits),
        )

    retried = _guarded_public(loader, SOURCE)
    if loader is load_snapshot:
        retried.close()


def test_guarded_public_owlxml_cancellation_never_fallbacks() -> None:
    cancellation = CancellationSource()
    cancellation.cancel("guarded public OWL/XML cancellation")
    unexpected_parse = AssertionError("cancelled OWL/XML crossed the Python parser")
    unexpected_publish = AssertionError("cancelled OWL/XML reached retained publication")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch(
            "pyowl_core.backends.python.parser.parse_owlxml",
            side_effect=unexpected_parse,
        ),
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.publish_retained_owlxml",
            side_effect=unexpected_publish,
        ),
        pytest.raises(
            OperationCancelledError,
            match="guarded public OWL/XML cancellation",
        ),
    ):
        load_snapshot(
            SOURCE,
            document_iri=DOCUMENT_IRI,
            options=_public_options(BackendPreference.NATIVE),
            cancellation_token=cancellation.token,
        )


@pytest.mark.parametrize(
    "policy",
    (ImportPolicy.RESOLVE_LOCAL, ImportPolicy.RESOLVE_STRICT),
)
def test_guarded_owlxml_mixed_format_import_closure_merges_retained_owners(
    policy: ImportPolicy,
) -> None:
    resolved = {
        "urn:owlxml:child:functional": ResolvedDocument(
            MIXED_FUNCTIONAL_CHILD,
            IRI("urn:owlxml:source:functional"),
            format=DocumentFormat.FUNCTIONAL,
        ),
        "urn:owlxml:child:rdfxml": ResolvedDocument(
            MIXED_RDFXML_CHILD,
            IRI("urn:owlxml:source:rdfxml"),
            format=DocumentFormat.RDF_XML,
        ),
        "urn:owlxml:child:turtle": ResolvedDocument(
            MIXED_TURTLE_CHILD,
            IRI("urn:owlxml:source:turtle"),
            format=DocumentFormat.TURTLE,
        ),
    }

    def load(backend: BackendPreference) -> Any:
        loader = SnapshotLoader(
            acquisition_cache=AcquisitionCache(),
            document_cache=ParsedDocumentCache(),
        )
        options = _public_options(
            backend,
            imports=policy,
        )
        resolver = MappingResolver(resolved)
        if backend is BackendPreference.PYTHON:
            return loader.load(
                MIXED_ROOT,
                document_iri=DOCUMENT_IRI,
                options=options,
                resolver=resolver,
            )
        unexpected = AssertionError("mixed OWL/XML closure crossed a Python parser")
        with (
            patch(
                "pyowl_core.backends.parser._NativeBackendDriver.select",
                autospec=True,
                return_value="native",
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_functional",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_owlxml",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_rdfxml",
                side_effect=unexpected,
            ),
            patch(
                "pyowl_core.backends.python.parser.parse_turtle",
                side_effect=unexpected,
            ),
        ):
            return loader.load(
                MIXED_ROOT,
                document_iri=DOCUMENT_IRI,
                options=options,
                resolver=resolver,
            )

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
        assert len(selected.documents) == 4
        assert all(
            type(document).__name__ == "_NativeOntologyDocument"
            for document in selected.documents
        )
        assert tuple(document.source_map for document in selected.documents) == tuple(
            document.source_map for document in reference.documents
        )
        assert {document.provenance.format for document in selected.documents} == {
            DocumentFormat.FUNCTIONAL,
            DocumentFormat.OWL_XML,
            DocumentFormat.RDF_XML,
            DocumentFormat.TURTLE,
        }
        assert all(document.provenance.backend == "native" for document in selected.documents)
        counters = (
            selected._native_snapshot_state.owner.handle._owner_v2._publication_counters_v2()
        )
        assert counters.parser_bytes == sum(
            map(
                len,
                (
                    MIXED_ROOT,
                    MIXED_FUNCTIONAL_CHILD,
                    MIXED_RDFXML_CHILD,
                    MIXED_TURTLE_CHILD,
                ),
            )
        )
        assert counters.publication_structural_rows_copied == 0
        assert counters.publication_structural_bytes_copied == 0
    finally:
        selected.close()


def test_private_owlxml_bridge_allocations_are_transactional(
    extension: NativeTestExtension,
) -> None:
    probe = extension._owlxml_retained_bridge_allocation_probe_v2
    source = bytearray(SOURCE)
    config = bytearray(native._encode_config(ParseLimits(), None, verify=False))
    original_source = bytes(source)
    original_config = bytes(config)

    encoded, allocations = probe(
        memoryview(source),
        "urn:owlxml:document",
        memoryview(config),
        True,
        True,
        False,
        None,
    )
    assert encoded.startswith(native._RETAINED_FUNCTIONAL_SEED_MAGIC_V2)
    assert allocations > 0
    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native OWL/XML retained bridge allocation failure$",
        ):
            probe(
                memoryview(source),
                "urn:owlxml:document",
                memoryview(config),
                True,
                True,
                False,
                fail_after,
            )
        assert source == original_source
        assert config == original_config

    assert probe(
        source,
        "urn:owlxml:document",
        config,
        True,
        True,
        False,
        allocations,
    ) == (encoded, allocations)


def test_owlxml_is_registered_in_the_ingestion_capability_ledger() -> None:
    assert "parse-owlxml-v1" in native.probe(refresh=True).features


def test_forced_owlxml_acquires_once_and_publishes_native_storage() -> None:
    source = _OneShotStream(SOURCE)
    selected = load_snapshot(
        source,
        document_iri=DOCUMENT_IRI,
        options=_public_options(BackendPreference.NATIVE),
    )
    try:
        assert type(selected).__name__ == "_NativeOntologySnapshot"
        assert source.read_calls == 2
        assert source.closed is False
    finally:
        selected.close()
