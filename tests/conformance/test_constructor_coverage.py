from __future__ import annotations

import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    LoadOptions,
    decode_snapshot,
    encode_snapshot,
    parse_document,
    render_document,
)
from pyowl_core.index.common import FIELD_ROLE_TABLE, iter_structural_occurrences
from tests.conformance._support import every_constructor_document, python_snapshot
from tests.generated.model.fixtures import model_fixtures
from tools.wire_reference.reference import read_wire, reencode


def test_every_registered_fixture_has_canonical_visitor_and_reference_evidence() -> None:
    fixtures = model_fixtures()
    assert set(fixtures) == set(m.MODEL_CONSTRUCTORS)
    assert len(FIELD_ROLE_TABLE) == sum(len(spec.fields) for spec in m.CONSTRUCTOR_SPECS)
    for constructor, fixture in fixtures.items():
        assert isinstance(fixture, constructor)
        encoded = m.canonical_bytes(fixture)
        assert m.decode_canonical(encoded) == fixture
        assert m.structural_digest(fixture) == m.structural_digest(m.decode_canonical(encoded))
        assert next(m.walk(fixture)) is fixture
        occurrence, path, role = next(iter_structural_occurrences(fixture))
        assert occurrence is fixture
        assert path == ()
        assert role.value == "root"


def test_every_constructor_crosses_all_required_formats() -> None:
    document = every_constructor_document()
    for format in DocumentFormat:
        rendered = render_document(document, format=format)
        reparsed = parse_document(
            rendered,
            format=format,
            document_iri=document.document_iri,
            options=LoadOptions(backend=BackendPreference.PYTHON),
        )
        assert reparsed == document
        assert reparsed.document_fingerprint == document.document_fingerprint
        if format in (DocumentFormat.RDF_XML, DocumentFormat.TURTLE):
            assert reparsed.rdf_mapping_report is not None
            assert reparsed.rdf_mapping_report.conformant


def test_every_constructor_crosses_canonical_wire_and_independent_reader() -> None:
    snapshot = python_snapshot(every_constructor_document(include_swrl=True))
    encoded = encode_snapshot(snapshot)
    image = read_wire(encoded)
    assert reencode(image) == encoded
    decoded = decode_snapshot(encoded)
    assert decoded.structural_fingerprint == snapshot.structural_fingerprint
    assert decoded.logical_fingerprint == snapshot.logical_fingerprint
    assert tuple(decoded.iter_axioms()) == tuple(snapshot.iter_axioms())
    assert tuple(decoded.iter_extensions()) == tuple(snapshot.iter_extensions())


def test_constructor_ledger_has_no_unowned_or_unhandled_row() -> None:
    from tools.corpus.coverage import build_coverage

    ledger = build_coverage()
    rows = ledger["rows"]
    assert isinstance(rows, list)
    expected = {spec.constructor.__name__: spec for spec in m.CONSTRUCTOR_SPECS}
    assert {row["constructor"] for row in rows} == set(expected)
    required_value = ledger["required_evidence_columns"]
    assert isinstance(required_value, list)
    required = set(required_value)
    for row in rows:
        spec = expected[row["constructor"]]
        assert row["schema_tag"] == spec.tag
        assert tuple(row["fields"]) == spec.fields
        assert set(row["evidence"]) == required
