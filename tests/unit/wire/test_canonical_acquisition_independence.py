from __future__ import annotations

from pathlib import Path

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    MappedOntologySnapshot,
    OntologyIdentityIndex,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    open_snapshot,
    parse_document,
)


@pytest.mark.parametrize(
    "ontology_header",
    ("<https://example.org/canonical>", ""),
    ids=("named", "anonymous"),
)
def test_acquisition_details_do_not_change_identity_or_canonical_wire(
    ontology_header: str,
    tmp_path: Path,
) -> None:
    declarations = (
        "Declaration(Class(<https://example.org/canonical#A>))",
        "Declaration(Class(<https://example.org/canonical#B>))",
    )
    compact = f"Ontology({ontology_header} {' '.join(declarations)})".encode()
    reformatted = (
        f"Ontology(\n  {ontology_header}\n  {declarations[1]}\n"
        f"  {declarations[0]}\n  {declarations[0]}\n)"
    ).encode()
    options = LoadOptions(
        backend=BackendPreference.PYTHON,
        imports=ImportPolicy.IGNORE,
    )
    first_document = parse_document(
        compact,
        format=DocumentFormat.FUNCTIONAL,
        document_iri="https://example.org/acquisition/first",
        options=options,
    )
    second_document = parse_document(
        reformatted,
        format=DocumentFormat.FUNCTIONAL,
        document_iri="https://example.org/acquisition/second",
        options=options,
    )

    assert first_document == second_document
    assert first_document.provenance.source_sha256 != second_document.provenance.source_sha256
    assert first_document.provenance.document_iri != second_document.provenance.document_iri

    first = load_snapshot(first_document, options=options)
    second = load_snapshot(second_document, options=options)
    assert first.structural_fingerprint == second.structural_fingerprint
    assert first.logical_fingerprint == second.logical_fingerprint
    assert first.signature_fingerprint == second.signature_fingerprint
    first_identity = first.view(OntologyIdentityIndex)
    second_identity = second.view(OntologyIdentityIndex)
    assert second_identity.documents == first_identity.documents
    assert second_identity.import_manifest_digest == first_identity.import_manifest_digest

    first_wire = encode_snapshot(first)
    second_wire = encode_snapshot(second)
    assert first_wire == second_wire

    decoded = decode_snapshot(first_wire)
    assert decoded.structural_fingerprint == first.structural_fingerprint
    decoded_identity = decoded.view(OntologyIdentityIndex)
    assert decoded_identity.documents == first_identity.documents
    assert decoded_identity.import_manifest_digest == first_identity.import_manifest_digest
    assert encode_snapshot(decoded) == first_wire

    path = tmp_path / "canonical.pyocore"
    path.write_bytes(first_wire)
    mapped = open_snapshot(path)
    assert isinstance(mapped, MappedOntologySnapshot)
    try:
        assert mapped.structural_fingerprint == first.structural_fingerprint
        mapped_identity = mapped.view(OntologyIdentityIndex)
        assert mapped_identity.documents == first_identity.documents
        assert mapped_identity.import_manifest_digest == first_identity.import_manifest_digest
        assert encode_snapshot(mapped) == first_wire
    finally:
        mapped.close()
