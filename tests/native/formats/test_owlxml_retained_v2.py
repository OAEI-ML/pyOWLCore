from __future__ import annotations

import time
from typing import cast

import pytest

from pyowl_core import (
    IRI,
    BackendPreference,
    CancellationSource,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologySyntaxError,
    OperationCancelledError,
    ParseLimits,
    ResourceLimitError,
    UnsupportedSyntaxError,
    load_snapshot,
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


def test_owlxml_remains_absent_from_the_advertised_capability_ledger() -> None:
    assert "parse-owlxml-v1" not in native.probe(refresh=True).features
