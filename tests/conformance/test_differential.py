from __future__ import annotations

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    parse_document,
    render_document,
)
from pyowl_core.backends import native
from tests.conformance._support import (
    every_constructor_document,
    native_requested,
    python_snapshot,
)
from tests.native.foundation._support import load_extension
from tools.corpus.differential import core_comparison
from tools.wire_reference.reference import read_wire, reencode


def test_core_cross_syntax_and_independent_differential_has_frozen_result() -> None:
    report = core_comparison()
    assert report == {
        "axioms": 4,
        "document_fingerprint": "55291b5bd2afd97fed64e8cc75edc6c42a75cebe6b9fd57584c7c58443aee703",
        "formats": ["functional", "owlxml", "rdfxml", "turtle"],
        "independent_wire": True,
        "wire_bytes": 6188,
    }


def test_functional_python_native_and_independent_wire_cross_product() -> None:
    source_document = every_constructor_document()
    functional = render_document(source_document, format=DocumentFormat.FUNCTIONAL)
    python_document = parse_document(
        functional,
        format=DocumentFormat.FUNCTIONAL,
        options=LoadOptions(backend=BackendPreference.PYTHON),
    )
    assert python_document == source_document

    documents = [python_document]
    if native_requested():
        load_extension()
        native._reset_probe_cache_for_tests()
        probe = native.probe(refresh=True)
        assert probe.available and "parse-functional-v1" in probe.features, probe.reason
        native_document = parse_document(
            functional,
            format=DocumentFormat.FUNCTIONAL,
            options=LoadOptions(backend=BackendPreference.NATIVE),
        )
        assert native_document == python_document
        assert native_document.document_fingerprint == python_document.document_fingerprint
        documents.append(native_document)

    for document in documents:
        snapshot = load_snapshot(
            document,
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                imports=ImportPolicy.IGNORE,
            ),
        )
        encoded = encode_snapshot(snapshot)
        assert reencode(read_wire(encoded)) == encoded
        decoded = decode_snapshot(encoded)
        assert decoded.structural_fingerprint == snapshot.structural_fingerprint
        if native_requested():
            assert native.roundtrip_wire(encoded) == encoded


def test_python_and_native_wire_and_axiom_index_parity_when_requested() -> None:
    snapshot = python_snapshot(every_constructor_document())
    encoded = encode_snapshot(snapshot)
    if not native_requested():
        assert reencode(read_wire(encoded)) == encoded
        return
    load_extension()
    native._reset_probe_cache_for_tests()
    probe = native.probe(refresh=True)
    assert probe.available, probe.reason
    assert native.encode_snapshot(snapshot) == encoded
    native_decoded = native.decode_snapshot(encoded)
    assert native_decoded.structural_fingerprint == snapshot.structural_fingerprint
    axioms = tuple(sorted(snapshot.iter_axioms(), key=lambda value: value.canonical_bytes()))
    partition = native.partition_axioms(axioms)
    assert sum(len(postings) for postings in partition.postings.values()) == len(axioms)
