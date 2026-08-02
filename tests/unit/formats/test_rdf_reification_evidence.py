from __future__ import annotations

import pytest

from pyowl_core import (
    BackendPreference,
    LoadOptions,
    ParseLimits,
    UnsupportedSyntaxError,
    parse_document,
)

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
OPTIONS = LoadOptions(backend=BackendPreference.PYTHON)
REIFICATION = f"""\
  <owl:Axiom rdf:nodeID="record">
    <owl:annotatedSource rdf:resource="urn:C"/>
    <owl:annotatedProperty rdf:resource="{RDFS}subClassOf"/>
    <owl:annotatedTarget rdf:resource="urn:D"/>
  </owl:Axiom>
"""


def _document(body: str) -> bytes:
    return f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:rdfs="{RDFS}" xmlns:owl="{OWL}">
{body}</rdf:RDF>
""".encode()


def test_split_missing_main_triple_has_bounded_structural_evidence() -> None:
    with pytest.raises(UnsupportedSyntaxError) as caught:
        parse_document(_document(REIFICATION), format="rdfxml", options=OPTIONS)

    error = caught.value
    assert error.code == "RDF_AXIOM_REIFICATION"
    assert error.diagnostic is not None
    assert error.diagnostic.details == {
        "reification_error": "MAIN_TRIPLE_ABSENT",
        "reification_subject": "_:record",
        "annotated_source": "<urn:C>",
        "annotated_property": RDFS + "subClassOf",
        "annotated_target": "<urn:D>",
        "annotated_target_kind": "iri",
        "main_triple_present": False,
        "reification_issue_count": 1,
        "reification_evidence_count": 1,
        "reification_suppressed_count": 0,
    }


def test_whole_document_succeeds_where_the_external_split_fails() -> None:
    whole = _document(
        """
  <owl:Class rdf:about="urn:C"><rdfs:subClassOf rdf:resource="urn:D"/></owl:Class>
  <owl:Class rdf:about="urn:D"/>
"""
        + REIFICATION
    )
    document = parse_document(whole, format="rdfxml", options=OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant


@pytest.mark.parametrize(
    ("metadata", "reason"),
    (
        (
            '<owl:annotatedSource rdf:resource="urn:C"/>',
            "METADATA_INCOMPLETE",
        ),
        (
            '<owl:annotatedSource rdf:resource="urn:C"/>'
            '<owl:annotatedSource rdf:resource="urn:Other"/>'
            f'<owl:annotatedProperty rdf:resource="{RDFS}subClassOf"/>'
            '<owl:annotatedTarget rdf:resource="urn:D"/>',
            "METADATA_AMBIGUOUS",
        ),
    ),
)
def test_malformed_metadata_uses_stable_reason_without_guessed_fields(
    metadata: str,
    reason: str,
) -> None:
    source = _document(f'<owl:Axiom rdf:nodeID="record">{metadata}</owl:Axiom>')
    with pytest.raises(UnsupportedSyntaxError) as caught:
        parse_document(source, format="rdfxml", options=OPTIONS)

    assert caught.value.diagnostic is not None
    details = caught.value.diagnostic.details
    assert details["reification_error"] == reason
    assert details["reification_subject"] == "_:record"
    assert "main_triple_present" not in details


def test_multiple_reification_issues_report_retained_and_suppressed_counts() -> None:
    records = "".join(
        f"""
  <owl:Axiom rdf:nodeID="record-{index}">
    <owl:annotatedSource rdf:resource="urn:C{index}"/>
    <owl:annotatedProperty rdf:resource="{RDFS}subClassOf"/>
    <owl:annotatedTarget rdf:resource="urn:D{index}"/>
  </owl:Axiom>
"""
        for index in range(3)
    )
    with pytest.raises(UnsupportedSyntaxError) as caught:
        parse_document(
            _document(records),
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_diagnostics=2),
            ),
        )

    error = caught.value
    assert error.reification_issue_count == 3
    assert error.reification_evidence_count == 2
    assert error.reification_suppressed_count == 1
    assert tuple(item.details["reification_subject"] for item in error.reification_evidence) == (
        "_:record-0",
        "_:record-1",
    )
    assert all(
        item.details["reification_issue_count"] == 3
        and item.details["reification_evidence_count"] == 2
        and item.details["reification_suppressed_count"] == 1
        for item in error.reification_evidence
    )


def test_internal_zero_evidence_cap_still_reports_reconcilable_counts() -> None:
    from pyowl_core.io.formats.rdf import RDFGraph, RDFMapper

    mapper = RDFMapper(RDFGraph(), limits=ParseLimits(), document_iri=None)
    error = mapper._reification_exception((), 3)

    assert error.reification_evidence == ()
    assert error.reification_issue_count == 3
    assert error.reification_evidence_count == 0
    assert error.reification_suppressed_count == 3
    assert error.diagnostic is not None
    assert error.diagnostic.details == {
        "reification_issue_count": 3,
        "reification_evidence_count": 0,
        "reification_suppressed_count": 3,
    }
