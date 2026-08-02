from __future__ import annotations

import copy
import os
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from pyowl_core import BackendPreference, ParseLimits
from tools.benchmark.biomedical_gate import evidence as evidence_module
from tools.benchmark.biomedical_gate.contract import (
    ANONYMOUS_TELEMETRY_NAMES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_STDERR_BYTES,
    WORKER_REQUEST_SCHEMA,
    BiomedicalGateError,
    default_limits_row,
    options_for,
    options_row,
    parse_request,
    sha256_json,
    telemetry_row,
    validate_worker_result,
)
from tools.benchmark.comparators.fresh import run_fresh_subprocess
from tools.benchmark.manifest import ROOT, load_manifest

_NATIVE_SHA256 = "27d07a79de9921b6d93f40d965fa6f8f6ef1bc1d0e8c07877face09d01b7dc27"


@pytest.fixture(scope="module")
def python_result() -> tuple[dict[str, object], dict[str, object]]:
    request: dict[str, object] = {
        "schema": WORKER_REQUEST_SCHEMA,
        "corpus_id": "generated-tiny-functional",
        "source_path": None,
        "backend": "python",
        "expected_native_sha256": None,
        "require_native_telemetry": False,
    }
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    exchange = run_fresh_subprocess(
        (sys.executable, "-m", "tools.benchmark.biomedical_gate.worker"),
        request,
        timeout=30.0,
        max_request_bytes=MAX_REQUEST_BYTES,
        max_stdout_bytes=MAX_RESPONSE_BYTES,
        max_stderr_bytes=MAX_STDERR_BYTES,
        cwd=ROOT,
        env=environment,
    )
    return request, exchange.result


def test_options_lock_uses_every_exact_default_parse_limit() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    options = options_for(corpus, BackendPreference.NATIVE)
    row = options_row(options)

    assert row["backend"] == "native"
    assert row["imports"] == "ignore"
    assert row["allow_partial_rdf_mapping"] is False
    assert row["limits"] == default_limits_row()
    assert tuple(default_limits_row()) == tuple(field.name for field in fields(ParseLimits))
    assert len(sha256_json(row)) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("backend", "auto"),
        ("require_native_telemetry", "yes"),
        ("expected_native_sha256", "0" * 63),
    ),
)
def test_request_contract_rejects_invalid_values(field: str, value: object) -> None:
    request: dict[str, object] = {
        "schema": WORKER_REQUEST_SCHEMA,
        "corpus_id": "generated-tiny-functional",
        "source_path": None,
        "backend": "native",
        "expected_native_sha256": "0" * 64,
        "require_native_telemetry": True,
    }
    request[field] = value
    with pytest.raises(BiomedicalGateError):
        parse_request(request)


def test_fresh_python_fixture_proves_one_document_protocol(
    python_result: tuple[dict[str, object], dict[str, object]],
) -> None:
    request, result = python_result
    corpus = load_manifest().by_id("generated-tiny-functional")
    validated = validate_worker_result(result, request=request, corpus=corpus)

    contract = validated["contract"]
    counts = validated["output"]["counts"]
    measurement = validated["measurement"]
    assert contract["load_entrypoint_calls"] == 1
    assert contract["consumer_chunking"] is False
    assert contract["document_count"] == 1
    assert counts["source_bytes"] == corpus.counts.bytes
    assert counts["axioms"] == corpus.counts.axioms
    assert measurement["fresh_process_peak_rss_bytes"] > 0


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("contract", "selected_backend"), "native"),
        (("contract", "load_entrypoint_calls"), 2),
        (("output", "counts", "documents"), 2),
        (("measurement", "sample_count"), 2),
    ),
)
def test_worker_result_tampering_fails_closed(
    python_result: tuple[dict[str, object], dict[str, object]],
    path: tuple[str, ...],
    value: object,
) -> None:
    request, result = python_result
    tampered = copy.deepcopy(result)
    target = tampered
    for name in path[:-1]:
        target = target[name]  # type: ignore[assignment]
    target[path[-1]] = value
    with pytest.raises(BiomedicalGateError):
        validate_worker_result(
            tampered,
            request=request,
            corpus=load_manifest().by_id("generated-tiny-functional"),
        )


def test_telemetry_contract_includes_accounted_bytes_and_rejects_missing() -> None:
    timings = {name: float(index + 1) for index, name in enumerate(ANONYMOUS_TELEMETRY_NAMES)}
    row = telemetry_row(timings, required=True)
    assert row is not None
    assert row["native_anonymous_accounted_bytes"] == len(ANONYMOUS_TELEMETRY_NAMES)

    del timings["native_anonymous_accounted_bytes"]
    with pytest.raises(BiomedicalGateError, match="accounted_bytes"):
        telemetry_row(timings, required=True)


def test_ncit_alpha_gate_and_fma_anchor_are_not_overclaimed() -> None:
    ncit = evidence_module._correctness_reference("oaei-bioml-ncit-2026")
    ncit_alpha = ncit["alpha_equivalence"]
    assert ncit_alpha["status"] == "not-run"
    assert ncit_alpha["passed"] is None
    assert ncit_alpha["blocks_release_gate"] is True
    assert "v0.1.1" in ncit_alpha["reason"]

    fma = evidence_module._correctness_reference("oaei-bioml-fma-2026")
    assert fma["count_qualification"] == "incident-regression-anchor-not-parity-oracle"
    assert fma["count_anchor"] == {
        "axioms": 791_162,
        "declarations": 104_942,
        "gate": False,
        "role": "reported-composed-workaround-regression-anchor-only",
    }
    assert fma["alpha_equivalence"]["passed"] is None


def test_duplicate_json_keys_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"id":"first","id":"second"}', encoding="utf-8")
    with pytest.raises(BiomedicalGateError, match="duplicate JSON object key"):
        evidence_module.load_case(path, expected_native_sha256="0" * 64)


def test_retained_fixed_release_case_cross_pins_python_component_evidence() -> None:
    case = evidence_module.load_case(
        evidence_module.DEFAULT_FIXED_CASE,
        expected_native_sha256=_NATIVE_SHA256,
    )
    cross_pin = evidence_module._component_cross_pin(
        case,
        evidence_module.DEFAULT_COMPONENT_REPORT,
    )

    assert cross_pin["source_sha256"] == (
        "e93b82497f07b7d44abec136010a0999e3434d97e2d5a46aa3fba29a0796868f"
    )
    assert cross_pin["axiom_count"] == 50_001
    assert cross_pin["document_fingerprint_sha256"] == (
        "d95eacbbad0f0fc91c567599ece59404ff8e372153763999153ddf6044460832"
    )
