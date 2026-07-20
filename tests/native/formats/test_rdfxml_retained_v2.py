from __future__ import annotations

import io
import time
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    IRI,
    BackendPreference,
    BackendUnavailableError,
    CancellationSource,
    DocumentFormat,
    ImportPolicy,
    ImportResolver,
    ImportStatus,
    LoadOptions,
    OntologySyntaxError,
    OperationCancelledError,
    ParseLimits,
    ResourceLimitError,
    UnresolvedImportWarning,
    encode_snapshot,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.backends.native_ingestion import publish_retained_rdfxml_snapshot_v2
from pyowl_core.cancellation import CancellationToken
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
        collect_provenance=False,
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

    unread = _UnreadableRdfXml(SOURCE)
    with pytest.raises(BackendUnavailableError, match="parse-rdfxml-v1"):
        load_snapshot(
            unread,
            document_iri=DOCUMENT_IRI,
            options=_options(BackendPreference.NATIVE),
        )
    assert unread.tell() == 0


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
