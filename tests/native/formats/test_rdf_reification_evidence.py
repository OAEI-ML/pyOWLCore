from __future__ import annotations

import pytest

from pyowl_core import (
    BackendPreference,
    LoadOptions,
    ParseLimits,
    UnsupportedSyntaxError,
    parse_document,
)
from pyowl_core.backends import native
from tests.native.foundation._support import NativeTestExtension, load_extension

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        pytest.skip(result.reason or "native backend unavailable")
    return selected


def _document(body: str) -> bytes:
    return f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:rdfs="{RDFS}" xmlns:owl="{OWL}">
{body}</rdf:RDF>
""".encode()


def _failure(source: bytes, backend: BackendPreference) -> UnsupportedSyntaxError:
    with pytest.raises(UnsupportedSyntaxError) as caught:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=backend,
                limits=ParseLimits(max_diagnostics=2),
            ),
        )
    return caught.value


@pytest.mark.parametrize(
    ("body", "reason"),
    (
        (
            f"""
  <owl:Axiom rdf:nodeID="record">
    <owl:annotatedSource rdf:resource="urn:C"/>
    <owl:annotatedProperty rdf:resource="{RDFS}subClassOf"/>
    <owl:annotatedTarget rdf:resource="urn:D"/>
  </owl:Axiom>
""",
            "MAIN_TRIPLE_ABSENT",
        ),
        (
            '<owl:Axiom rdf:nodeID="record">'
            '<owl:annotatedSource rdf:resource="urn:C"/>'
            "</owl:Axiom>",
            "METADATA_INCOMPLETE",
        ),
        (
            '<owl:Axiom rdf:nodeID="record">'
            '<owl:annotatedSource rdf:resource="urn:C"/>'
            '<owl:annotatedSource rdf:resource="urn:Other"/>'
            f'<owl:annotatedProperty rdf:resource="{RDFS}subClassOf"/>'
            '<owl:annotatedTarget rdf:resource="urn:D"/>'
            "</owl:Axiom>",
            "METADATA_AMBIGUOUS",
        ),
        (
            '<rdf:Description rdf:nodeID="record">'
            f'<rdf:type rdf:resource="{OWL}Axiom"/>'
            f'<rdf:type rdf:resource="{OWL}Annotation"/>'
            "</rdf:Description>",
            "NODE_KIND_CONFLICT",
        ),
    ),
)
def test_forced_native_reification_details_match_python(
    body: str,
    reason: str,
) -> None:
    source = _document(body)
    python = _failure(source, BackendPreference.PYTHON)
    selected = _failure(source, BackendPreference.NATIVE)

    assert python.code == selected.code == "RDF_AXIOM_REIFICATION"
    assert python.diagnostic is not None
    assert selected.diagnostic is not None
    assert selected.diagnostic.details == python.diagnostic.details
    assert selected.diagnostic.details["reification_error"] == reason
    assert selected.diagnostic.details["reification_subject"] == "_:record"
    if reason in {"METADATA_INCOMPLETE", "METADATA_AMBIGUOUS"}:
        assert "annotated_source" not in selected.diagnostic.details
        assert "main_triple_present" not in selected.diagnostic.details


def test_missing_main_triple_evidence_distinguishes_split_from_whole_document() -> None:
    reification = f"""
  <owl:Axiom rdf:nodeID="record">
    <owl:annotatedSource rdf:resource="urn:C"/>
    <owl:annotatedProperty rdf:resource="{RDFS}subClassOf"/>
    <owl:annotatedTarget rdf:resource="urn:D"/>
  </owl:Axiom>
"""
    split = _failure(_document(reification), BackendPreference.NATIVE)
    assert split.diagnostic is not None
    assert split.diagnostic.details["main_triple_present"] is False
    assert split.diagnostic.details["annotated_source"] == "<urn:C>"
    assert split.diagnostic.details["annotated_target"] == "<urn:D>"

    whole = _document(
        '<owl:Class rdf:about="urn:C">'
        '<rdfs:subClassOf rdf:resource="urn:D"/>'
        "</owl:Class>"
        '<owl:Class rdf:about="urn:D"/>' + reification
    )
    document = parse_document(
        whole,
        format="rdfxml",
        options=LoadOptions(backend=BackendPreference.NATIVE),
    )
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant


def test_forced_native_cyclic_reification_evidence_is_deterministic() -> None:
    source = _document(
        '<owl:Annotation rdf:nodeID="a">'
        '<owl:annotatedSource rdf:nodeID="b"/>'
        f'<owl:annotatedProperty rdf:resource="{RDFS}label"/>'
        '<owl:annotatedTarget rdf:resource="urn:o"/>'
        '<rdfs:seeAlso rdf:resource="urn:x"/>'
        "</owl:Annotation>"
        '<owl:Annotation rdf:nodeID="b">'
        '<owl:annotatedSource rdf:nodeID="a"/>'
        f'<owl:annotatedProperty rdf:resource="{RDFS}seeAlso"/>'
        '<owl:annotatedTarget rdf:resource="urn:x"/>'
        '<rdfs:label rdf:resource="urn:o"/>'
        "</owl:Annotation>"
    )
    python = _failure(source, BackendPreference.PYTHON)
    selected = _failure(source, BackendPreference.NATIVE)

    assert python.diagnostic is not None
    assert selected.diagnostic is not None
    assert selected.diagnostic.details == python.diagnostic.details
    assert selected.diagnostic.details == {
        "reification_error": "ANNOTATION_CYCLE",
        "annotated_source": "_:a",
        "annotated_property": RDFS + "seeAlso",
        "annotated_target": "<urn:x>",
        "annotated_target_kind": "iri",
        "main_triple_present": True,
        "reification_issue_count": 1,
        "reification_evidence_count": 1,
        "reification_suppressed_count": 0,
    }


def test_forced_native_multi_issue_evidence_respects_diagnostic_cap() -> None:
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
    source = _document(records)
    python = _failure(source, BackendPreference.PYTHON)
    selected = _failure(source, BackendPreference.NATIVE)

    assert selected.reification_issue_count == python.reification_issue_count == 3
    assert selected.reification_evidence_count == python.reification_evidence_count == 2
    assert selected.reification_suppressed_count == python.reification_suppressed_count == 1
    assert tuple(item.details for item in selected.reification_evidence) == tuple(
        item.details for item in python.reification_evidence
    )
    assert tuple(item.details["reification_subject"] for item in selected.reification_evidence) == (
        "_:record-0",
        "_:record-1",
    )


def test_forced_native_reification_evidence_redacts_credentials() -> None:
    source = _document(
        f"""
  <owl:Axiom rdf:nodeID="record">
    <owl:annotatedSource
      rdf:resource="https://source-user:source-secret@example.org/C"/>
    <owl:annotatedProperty rdf:resource="{RDFS}subClassOf"/>
    <owl:annotatedTarget
      rdf:resource="https://target-user:target-secret@example.org/D"/>
  </owl:Axiom>
"""
    )
    python = _failure(source, BackendPreference.PYTHON)
    selected = _failure(source, BackendPreference.NATIVE)

    assert selected.diagnostic is not None
    assert python.diagnostic is not None
    assert selected.diagnostic.details == python.diagnostic.details
    rendered = " ".join(str(value) for value in selected.diagnostic.details.values())
    assert "secret" not in rendered
    assert rendered.count("<redacted>@") == 2
