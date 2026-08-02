from __future__ import annotations

from dataclasses import replace

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    LoadOptions,
    OntologyDocument,
    OptionConflictError,
    ParsedDocumentCache,
    SnapshotLoader,
    coerce_snapshot,
    load_snapshot,
    parse_document,
)

SOURCE = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:e="urn:diagnostic:">
  <rdf:Description rdf:about="urn:subject">
    <e:unknown rdf:resource="urn:object"/>
  </rdf:Description>
</rdf:RDF>
"""


def _diagnostic_document() -> OntologyDocument:
    return parse_document(
        SOURCE,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            allow_partial_rdf_mapping=True,
        ),
    )


def test_partial_document_is_rejected_by_snapshot_coercion_and_loader() -> None:
    document = _diagnostic_document()
    for operation in (coerce_snapshot, SnapshotLoader().load):
        with pytest.raises(OptionConflictError) as caught:
            operation(document)
        assert caught.value.code == "PARTIAL_RDF_MAPPING_SNAPSHOT_FORBIDDEN"


def test_partial_document_cannot_enter_the_public_parsed_document_cache() -> None:
    document = _diagnostic_document()
    cache = ParsedDocumentCache()
    with pytest.raises(OptionConflictError) as caught:
        cache.publish(("diagnostic",), document)
    assert caught.value.code == "PARTIAL_RDF_MAPPING_SNAPSHOT_FORBIDDEN"
    assert cache.get(("diagnostic",)) is None


def test_direct_snapshot_construction_rejects_partial_documents_and_options() -> None:
    baseline = load_snapshot(
        b"Ontology(<urn:valid>)",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            format=DocumentFormat.FUNCTIONAL,
        ),
    )
    diagnostic = _diagnostic_document()

    with pytest.raises(OptionConflictError) as document_error:
        replace(baseline, root=diagnostic, documents=(diagnostic,))
    assert document_error.value.code == "PARTIAL_RDF_MAPPING_SNAPSHOT_FORBIDDEN"

    with pytest.raises(OptionConflictError) as option_error:
        replace(
            baseline,
            load_options=replace(
                baseline.load_options,
                allow_partial_rdf_mapping=True,
            ),
        )
    assert option_error.value.code == "PARTIAL_RDF_MAPPING_SNAPSHOT_FORBIDDEN"
