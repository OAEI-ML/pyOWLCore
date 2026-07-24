from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

import pyowl_core.model as m
from pyowl_core import (
    IRI,
    BackendPreference,
    DocumentFormat,
    EncodedStructuralView,
    ImportPolicy,
    LoadOptions,
    canonical_bytes,
    encode_snapshot,
    load_snapshot,
    parse_document,
    render_document,
)
from pyowl_core.backends import native
from tests.conformance._support import every_constructor_document
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.foundation._support import NativeTestExtension, load_extension

SIMPLE_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:rdfxml:public-source"/>
  <owl:Class rdf:about="urn:rdfxml:A">
    <rdfs:subClassOf rdf:resource="urn:rdfxml:B"/>
  </owl:Class>
  <owl:Class rdf:about="urn:rdfxml:B"/>
</rdf:RDF>
"""
DOCUMENT_IRI = IRI("urn:rdfxml:public-source-document")


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
        raise AssertionError("caller-owned RDF/XML stream was read after EOF")


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


def _options(
    backend: BackendPreference,
    *,
    preserve_source_map: bool = False,
) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.RDF_XML,
        imports=ImportPolicy.IGNORE,
        backend=backend,
        collect_provenance=False,
        preserve_source_map=preserve_source_map,
    )


def _guarded_snapshot(source: object, *, preserve_source_map: bool = False) -> Any:
    unexpected = AssertionError("guarded public RDF/XML source crossed the Python parser")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected),
    ):
        return cast(
            Any,
            load_snapshot(
                cast(Any, source),
                document_iri=DOCUMENT_IRI,
                options=_options(
                    BackendPreference.NATIVE,
                    preserve_source_map=preserve_source_map,
                ),
            ),
        )


@pytest.mark.parametrize(
    "source",
    (
        SIMPLE_SOURCE,
        bytearray(SIMPLE_SOURCE),
        memoryview(SIMPLE_SOURCE),
    ),
    ids=("bytes", "bytearray", "memoryview"),
)
def test_guarded_public_rdfxml_accepts_each_buffer_source(source: object) -> None:
    selected = _guarded_snapshot(source)

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.ontology_id.ontology_iri == IRI("urn:rdfxml:public-source")
    counters = selected._native_snapshot_state.owner.handle._owner_v2
    assert counters._publication_counters_v2().parser_bytes == len(SIMPLE_SOURCE)


@pytest.mark.parametrize(
    "data",
    (SIMPLE_SOURCE, SIMPLE_SOURCE.decode()),
    ids=("binary", "text"),
)
def test_guarded_public_rdfxml_reads_caller_stream_once(data: bytes | str) -> None:
    source = _OneShotStream(data)
    selected = _guarded_snapshot(source)

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert source.read_calls == 2
    assert source.closed is False


def test_guarded_public_rdfxml_accepts_a_regular_path(tmp_path: Path) -> None:
    source = tmp_path / "ontology.rdf"
    source.write_bytes(SIMPLE_SOURCE)

    selected = _guarded_snapshot(source)

    assert type(selected).__name__ == "_NativeOntologySnapshot"
    assert selected.root.provenance.acquisition_locator == str(source)
    assert len(selected.root.provenance.source_sha256) == 32


def test_guarded_public_rdfxml_every_constructor_corpus_is_owner_first(
    extension: NativeTestExtension,
) -> None:
    source = render_document(
        every_constructor_document(),
        format=DocumentFormat.RDF_XML,
    )
    reference = load_snapshot(
        source,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON, preserve_source_map=True),
    )
    selected = _guarded_snapshot(source, preserve_source_map=True)

    owner = selected._native_snapshot_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeSnapshotHandle
    assert set(m.AXIOM_TYPES) <= {type(value) for value in reference.root.axioms}
    assert selected.root.axioms == reference.root.axioms
    assert selected.root.source_map == reference.root.source_map
    assert selected.root.rdf_mapping_report == reference.root.rdf_mapping_report
    assert selected.structural_fingerprint == reference.structural_fingerprint
    assert selected.logical_fingerprint == reference.logical_fingerprint
    assert selected.signature_fingerprint == reference.signature_fingerprint
    assert encode_snapshot(selected) == encode_snapshot(reference)
    assert counters.parser_bytes == len(source)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0

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


def test_guarded_public_rdfxml_every_constructor_document_retains_owner(
    extension: NativeTestExtension,
) -> None:
    source = render_document(
        every_constructor_document(),
        format=DocumentFormat.RDF_XML,
    )
    reference = parse_document(
        source,
        document_iri=DOCUMENT_IRI,
        options=_options(BackendPreference.PYTHON, preserve_source_map=True),
    )
    unexpected = AssertionError("guarded every-constructor document crossed Python")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch("pyowl_core.backends.python.parser.parse_rdfxml", side_effect=unexpected),
    ):
        selected = cast(
            Any,
            parse_document(
                source,
                document_iri=DOCUMENT_IRI,
                options=_options(
                    BackendPreference.NATIVE,
                    preserve_source_map=True,
                ),
            ),
        )

    owner = selected._native_document_state.owner.handle._owner_v2
    counters = owner._publication_counters_v2()
    assert type(owner) is cast(Any, extension)._NativeDocumentHandle
    assert selected == reference
    assert selected.source_map == reference.source_map
    assert selected.document_fingerprint == reference.document_fingerprint
    assert counters.parser_bytes == len(source)
    assert counters.publication_structural_rows_copied == 0
    assert counters.publication_structural_bytes_copied == 0
