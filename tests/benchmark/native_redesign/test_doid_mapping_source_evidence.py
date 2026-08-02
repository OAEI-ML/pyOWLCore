from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import RDFMappingReport, RDFTripleEvidence, UnsupportedSyntaxError
from tools.benchmark.native_redesign import doid_mapping_evidence as evidence_module
from tools.benchmark.native_redesign.doid_mapping_evidence import (
    DoidEvidenceError,
    capture_strict_failure,
    load_evidence,
    validate_evidence,
)

ROOT = Path(__file__).parents[3]
EVIDENCE = (
    ROOT / "reports" / "performance" / "native-redesign" / "doid-rdf-mapping-source-evidence.json"
)
EVIDENCE_SHA256 = "b0953cd57c8177d02a0a5dbbb7828dad2ed4254c2aca23d5b36eec8e7a7698c3"


def _mapping(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_checked_doid_mapping_evidence_is_complete_current_and_nonclaiming() -> None:
    payload = load_evidence(EVIDENCE)
    assert hashlib.sha256(EVIDENCE.read_bytes()).hexdigest() == EVIDENCE_SHA256
    assert payload["status"] == "pass"
    assert payload["source"] == {
        "bytes": 28_385_948,
        "corpus_id": "oaei-bioml-doid-2026",
        "format": "rdfxml",
        "revision": "OAEI-ML/bio-ml@2026/ontologies/DOID-611355c44553-doid.owl",
        "sha256": "611355c445537fcf4bae2c519f1b3598af5a8fea793274316e35525b7d05e945",
    }
    report = _mapping(_mapping(payload["result"])["mapping_report"])
    assert (
        report["total_triples"],
        report["consumed_triples"],
        report["dropped_triples"],
    ) == (305_919, 305_901, 18)
    assert report["retained_unconsumed_count"] == 18
    assert report["suppressed_unconsumed_count"] == 0
    assert report["predicates"] == [
        "http://purl.obolibrary.org/obo/OBI_9991118",
        "http://www.geneontology.org/formats/oboInOwl#created_by",
    ]
    measurement = _mapping(payload["measurement"])
    assert measurement["sample_count"] == 1
    assert measurement["formal_performance_claim"] is False
    assert cast(int, measurement["wall_ns"]) > 0
    assert cast(int, measurement["peak_rss_bytes"]) >= 28_385_948


def test_capture_invokes_the_public_parser_once_and_uses_first_exception_report() -> None:
    report = RDFMappingReport(
        conformant=False,
        consumed_triples=0,
        total_triples=1,
        unconsumed=(RDFTripleEvidence("<urn:s>", "urn:p", "'value'", "literal"),),
        rule_ids=("OWL2-RDF-REVERSE",),
    )
    failure = UnsupportedSyntaxError(
        "incomplete",
        code="RDF_MAPPING_INCOMPLETE",
        rdf_mapping_report=report,
    )
    clock = iter((100, 225))
    with patch.object(evidence_module, "parse_document", side_effect=failure) as parser:
        result, measurement = capture_strict_failure(
            Path("not-opened-by-mocked-parser.rdf"),
            clock_ns=lambda: next(clock),
            rss_peak_bytes=lambda: 4_096,
        )

    assert parser.call_count == 1
    options = parser.call_args.kwargs["options"]
    assert options.allow_partial_rdf_mapping is False
    assert options.limits.max_diagnostics == 32
    assert _mapping(result["mapping_report"])["dropped_triples"] == 1
    assert measurement["wall_ns"] == 125
    assert measurement["peak_rss_bytes"] == 4_096


def test_validator_fails_closed_on_every_claim_bearing_surface() -> None:
    original = load_evidence(EVIDENCE)
    candidates: list[dict[str, object]] = []

    unknown = copy.deepcopy(original)
    unknown["unreviewed"] = True
    candidates.append(unknown)

    source = copy.deepcopy(original)
    _mapping(source["source"])["sha256"] = "0" * 64
    candidates.append(source)

    floating_source_size = copy.deepcopy(original)
    _mapping(floating_source_size["source"])["bytes"] = 28_385_948.0
    candidates.append(floating_source_size)

    numeric_boolean = copy.deepcopy(original)
    _mapping(numeric_boolean["contract"])["strict_rdf_mapping"] = 1
    candidates.append(numeric_boolean)

    second_parse = copy.deepcopy(original)
    _mapping(second_parse["contract"])["parse_entrypoint_calls"] = 2
    candidates.append(second_parse)

    implementation = copy.deepcopy(original)
    hashes = _mapping(_mapping(implementation["implementation"])["source_sha256"])
    hashes["src/pyowl_core/io/formats/rdf.py"] = "0" * 64
    candidates.append(implementation)

    counts = copy.deepcopy(original)
    report = _mapping(_mapping(counts["result"])["mapping_report"])
    report["consumed_triples"] = 305_900
    candidates.append(counts)

    example = copy.deepcopy(original)
    report = _mapping(_mapping(example["result"])["mapping_report"])
    _mapping(cast(list[object], report["unconsumed"])[0])["subject"] = "<urn:tampered>"
    candidates.append(example)

    claim = copy.deepcopy(original)
    _mapping(claim["measurement"])["formal_performance_claim"] = True
    candidates.append(claim)

    for candidate in candidates:
        with pytest.raises(DoidEvidenceError):
            validate_evidence(candidate)


def test_loader_rejects_duplicate_keys_and_noncanonical_json(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    with pytest.raises(DoidEvidenceError, match="duplicate"):
        load_evidence(duplicate)

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(load_evidence(EVIDENCE)), encoding="utf-8")
    with pytest.raises(DoidEvidenceError, match="canonical"):
        load_evidence(noncanonical)
