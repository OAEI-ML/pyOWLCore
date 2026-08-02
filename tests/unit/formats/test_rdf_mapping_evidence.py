from __future__ import annotations

from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    LoadOptions,
    ParseLimits,
    RDFMappingReport,
    UnsupportedSyntaxError,
    parse_document,
)
from pyowl_core.io.formats.rdfxml import parse_rdfxml as parse_rdfxml_reference

SOURCE = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:e="urn:evidence:">
  <rdf:Description rdf:about="urn:subject">
    <e:literal>value</e:literal>
    <e:resource rdf:resource="urn:object"/>
    <e:blank rdf:nodeID="object"/>
  </rdf:Description>
</rdf:RDF>
"""


def _options(*, partial: bool, max_diagnostics: int = 10) -> LoadOptions:
    return LoadOptions(
        backend=BackendPreference.PYTHON,
        allow_partial_rdf_mapping=partial,
        limits=ParseLimits(max_diagnostics=max_diagnostics),
    )


def test_strict_failure_carries_the_complete_bounded_first_pass_report() -> None:
    with (
        patch(
            "pyowl_core.backends.python.parser.parse_rdfxml",
            wraps=parse_rdfxml_reference,
        ) as parser,
        pytest.raises(UnsupportedSyntaxError) as caught,
    ):
        parse_document(SOURCE, format="rdfxml", options=_options(partial=False))

    assert parser.call_count == 1
    error = caught.value
    assert error.code == "RDF_MAPPING_INCOMPLETE"
    report = error.rdf_mapping_report
    assert isinstance(report, RDFMappingReport)
    assert not report.conformant
    assert report.total_triples == 3
    assert report.consumed_triples == 0
    assert report.dropped_triples == 3
    assert {item.object_kind for item in report.unconsumed} == {
        "blank",
        "iri",
        "literal",
    }


def test_strict_and_partial_reports_match_at_the_configured_evidence_bound() -> None:
    options = _options(partial=False, max_diagnostics=2)
    with pytest.raises(UnsupportedSyntaxError) as caught:
        parse_document(SOURCE, format="rdfxml", options=options)
    strict = caught.value.rdf_mapping_report
    assert isinstance(strict, RDFMappingReport)

    partial = parse_document(
        SOURCE,
        format="rdfxml",
        options=_options(partial=True, max_diagnostics=2),
    )
    assert partial.rdf_mapping_report == strict
    assert len(strict.unconsumed) == 2


@pytest.mark.parametrize("kind", ("IRI", "resource", ""))
def test_rdf_evidence_rejects_noncontractual_object_kinds(kind: str) -> None:
    from pyowl_core import RDFTripleEvidence

    with pytest.raises(ValueError, match="object_kind"):
        RDFTripleEvidence("<urn:s>", "urn:p", "<urn:o>", kind)


def test_mapping_evidence_redacts_uri_credentials() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:e="https://predicate-user:predicate-secret@example.org/">
  <rdf:Description rdf:about="https://subject-user:subject-secret@example.org/s">
    <e:value rdf:resource="https://object-user:object-secret@example.org/o"/>
  </rdf:Description>
</rdf:RDF>
"""
    with pytest.raises(UnsupportedSyntaxError) as caught:
        parse_document(source, format="rdfxml", options=_options(partial=False))

    report = caught.value.rdf_mapping_report
    assert report is not None
    evidence = report.unconsumed[0]
    rendered = " ".join((evidence.subject, evidence.predicate, evidence.object))
    assert "secret" not in rendered
    assert rendered.count("<redacted>@") == 3
