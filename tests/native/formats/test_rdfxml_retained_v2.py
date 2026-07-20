from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    IRI,
    BackendPreference,
    BackendUnavailableError,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.backends.native_ingestion import publish_retained_rdfxml_snapshot_v2
from pyowl_core.exceptions import UnsupportedSyntaxError
from pyowl_core.io.formats.detection import detect_format
from pyowl_core.io.source import acquire_source
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
DOCUMENT_IRI = IRI("urn:rdfxml:document")


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


def _retained_snapshot() -> object:
    options = _options(BackendPreference.NATIVE)
    payload = acquire_source(
        SOURCE,
        format=DocumentFormat.RDF_XML,
        document_iri=DOCUMENT_IRI,
        limits=options.limits,
    )
    detection = detect_format(payload.data, explicit=DocumentFormat.RDF_XML)
    started = time.monotonic()
    parsed = native._parse_rdfxml_retained_v2(
        SOURCE,
        document_iri=DOCUMENT_IRI.value,
        limits=options.limits,
        collect_provenance=False,
        allow_partial_rdf_mapping=False,
        require_empty_imports=False,
    )
    if parsed.summary is None or parsed.storage is None:
        raise AssertionError("retained RDF/XML parser returned no owner-first result")
    return publish_retained_rdfxml_snapshot_v2(
        parsed.summary,
        parsed_native_storage=parsed.storage,
        phase_timings=parsed.phase_timings,
        payload=payload,
        detection=detection,
        document_iri=DOCUMENT_IRI,
        media_type=None,
        options=options,
        resolver=None,
        cancellation_token=None,
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
    assert selected.root.axioms == reference.root.axioms


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


def test_private_rdfxml_seam_rejects_unowned_semantics_before_publication() -> None:
    with pytest.raises(UnsupportedSyntaxError, match="does not yet support provenance"):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            collect_provenance=True,
        )
    with pytest.raises(UnsupportedSyntaxError, match="does not yet support provenance"):
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

    with pytest.raises(UnsupportedSyntaxError, match="resolver-backed imports"):
        native._parse_rdfxml_retained_v2(
            SOURCE,
            document_iri=DOCUMENT_IRI.value,
            require_empty_imports=True,
        )
