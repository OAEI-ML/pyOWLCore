from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from tools.benchmark.component_canonicalization.evidence import (
    EvidenceError,
    generate_report,
    load_report,
    main,
    validate_report,
)
from tools.benchmark.component_canonicalization.inputs import (
    InputLock,
    InputLockError,
    fixed_components_source,
    load_input_lock,
    verify_input_lock,
)
from tools.benchmark.manifest import generated_bytes, load_manifest
from tools.benchmark.report import canonical_json_bytes


@pytest.fixture(scope="module")
def input_lock() -> InputLock:
    lock = load_input_lock()
    verify_input_lock(lock)
    return lock


@pytest.fixture(scope="module")
def smoke_report(input_lock: InputLock) -> dict[str, object]:
    return generate_report(input_lock, profile="smoke")


def _rows(report: dict[str, object]) -> dict[str, dict[str, Any]]:
    values = cast(list[dict[str, Any]], report["cases"])
    return {cast(str, row["id"]): row for row in values}


def test_input_lock_pins_smoke_and_public_release_generator(input_lock: InputLock) -> None:
    assert input_lock.sha256 == (
        "b03be31e945465800b5713caa9e5f544c10b0a72c2967588736071b97c5fbbd0"
    )
    assert tuple(case.id for case in input_lock.for_profile("smoke")) == (
        "fixed-1",
        "fixed-8",
        "fixed-64",
        "oversized-4",
    )
    assert tuple(case.id for case in input_lock.for_profile("release")) == (
        "fixed-1",
        "fixed-8",
        "fixed-64",
        "fixed-50000",
        "oversized-4",
    )

    release_case = input_lock.by_id("fixed-50000")
    corpus = load_manifest().by_id("generated-component-scaling-functional")
    source = fixed_components_source(50_000)
    assert len(source) == release_case.source_bytes == corpus.counts.bytes == 5_000_159
    assert hashlib.sha256(source).hexdigest() == release_case.source_sha256 == corpus.sha256
    assert generated_bytes(corpus) == source


def test_smoke_report_proves_additive_fixed_component_work(
    smoke_report: dict[str, object],
) -> None:
    rows = _rows(smoke_report)
    assert smoke_report["status"] == "pass"
    assert smoke_report["claims"] == {
        "input_hashes_verified": True,
        "fixed_component_work_is_additive": True,
        "fixed_component_work_per_component": 9,
        "oversized_component_failed_closed": True,
        "bounded_component_metrics_present": True,
        "performance_claim": None,
    }

    for count in (1, 8, 64):
        row = rows[f"fixed-{count}"]
        work = cast(dict[str, int], row["work"])
        shape = cast(dict[str, int], row["shape"])
        evidence_input = cast(dict[str, int | str], row["input"])
        assert evidence_input["max_canonical_work"] == 9
        assert work == {
            "component_record_count": count,
            "total_setup_work": 3 * count,
            "total_refinement_work": 4 * count,
            "total_candidate_order_work": 2 * count,
            "total_canonical_work": 9 * count,
            "largest_component_work": 9,
            "maximum_refinement_rounds": 1,
            "total_permutations_examined": count,
        }
        assert shape == {
            "component_count": count,
            "largest_component_labels": 1,
            "largest_component_arcs": 1,
            "largest_component_roots": 1,
            "maximum_root_interval_span": 1,
            "maximum_open_root_intervals": 1,
            "total_labels": count,
            "total_arcs": count,
        }


def test_smoke_report_captures_exact_functional_outputs_and_wp19_error(
    smoke_report: dict[str, object],
) -> None:
    rows = _rows(smoke_report)
    expected_fingerprints = {
        "fixed-1": "1e4e978b32f92d8a1cd63584836339095104f33dedb69da9b021b080736e459d",
        "fixed-8": "981f21b791b03747e7216cba773368cde118e2b061ddfc17971f5e23ea74bd0e",
        "fixed-64": "96f2634f62ca2d71fe3230eba492b61c78b9601fb1c069c3ca34213b5306d0c3",
    }
    for case_id, digest in expected_fingerprints.items():
        output = cast(dict[str, object], rows[case_id]["output"])
        assert output["document_fingerprint_schema"] == 2
        assert output["document_fingerprint_sha256"] == digest

    oversized = rows["oversized-4"]
    assert oversized["shape"] == {
        "component_count": 1,
        "largest_component_labels": 4,
        "largest_component_arcs": 10,
        "largest_component_roots": 1,
        "maximum_root_interval_span": 1,
        "maximum_open_root_intervals": 1,
        "total_labels": 4,
        "total_arcs": 10,
    }
    assert oversized["work"] is None
    assert oversized["output"] is None
    assert oversized["error"] == {
        "type": "ResourceLimitError",
        "code": "RESOURCE_LIMIT",
        "limit": "max_canonical_work",
        "observed": 24,
        "allowed": 23,
        "details": {
            "component_count": 1,
            "largest_component_labels": 4,
            "largest_component_arcs": 10,
            "refinement_rounds": 0,
            "work_term": "setup",
        },
    }


def test_report_is_deterministic_and_has_a_frozen_smoke_digest(
    input_lock: InputLock,
    smoke_report: dict[str, object],
) -> None:
    assert generate_report(input_lock, profile="smoke") == smoke_report
    assert hashlib.sha256(canonical_json_bytes(smoke_report)).hexdigest() == (
        "fb32ab633f157c50d97d4b511fb965caba4454daf3aeebd11d8d0c842319b2e9"
    )


def test_validator_fails_closed_on_every_claim_bearing_surface(
    input_lock: InputLock,
    smoke_report: dict[str, object],
) -> None:
    candidates: list[dict[str, object]] = []

    unknown = copy.deepcopy(smoke_report)
    unknown["unreviewed"] = True
    candidates.append(unknown)

    missing = copy.deepcopy(smoke_report)
    cast(list[object], missing["cases"]).pop()
    candidates.append(missing)

    source = copy.deepcopy(smoke_report)
    _rows(source)["fixed-8"]["input"]["sha256"] = "0" * 64
    candidates.append(source)

    work = copy.deepcopy(smoke_report)
    _rows(work)["fixed-64"]["work"]["total_canonical_work"] = 575
    candidates.append(work)

    boolean_count = copy.deepcopy(smoke_report)
    _rows(boolean_count)["fixed-1"]["shape"]["component_count"] = True
    candidates.append(boolean_count)

    numeric_boolean = copy.deepcopy(smoke_report)
    cast(dict[str, object], numeric_boolean["claims"])["input_hashes_verified"] = 1
    candidates.append(numeric_boolean)

    boolean_api_major = copy.deepcopy(smoke_report)
    cast(dict[str, Any], boolean_api_major["contract"])["api_version"][0] = False
    candidates.append(boolean_api_major)

    floating_schema = copy.deepcopy(smoke_report)
    cast(dict[str, object], floating_schema["contract"])["model_schema"] = 2.0
    candidates.append(floating_schema)

    error = copy.deepcopy(smoke_report)
    _rows(error)["oversized-4"]["error"]["details"]["work_term"] = "guessed"
    candidates.append(error)

    claim = copy.deepcopy(smoke_report)
    cast(dict[str, object], claim["claims"])["performance_claim"] = "linear time"
    candidates.append(claim)

    for candidate in candidates:
        with pytest.raises(EvidenceError):
            validate_report(candidate, input_lock)


def test_json_loaders_reject_duplicate_keys(
    tmp_path: Path,
    input_lock: InputLock,
) -> None:
    duplicate_lock = tmp_path / "inputs.json"
    duplicate_lock.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    with pytest.raises(InputLockError, match="duplicate"):
        load_input_lock(duplicate_lock)

    duplicate_report = tmp_path / "report.json"
    duplicate_report.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    with pytest.raises(EvidenceError, match="duplicate"):
        load_report(duplicate_report, input_lock)


def test_cli_generates_checks_and_rejects_tampered_reports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path = tmp_path / "smoke.json"
    assert main(("generate", "--profile", "smoke", "--output", str(report_path))) == 0
    assert "component evidence written" in capsys.readouterr().out
    assert main(("check", str(report_path))) == 0
    assert "component evidence OK" in capsys.readouterr().out

    payload = report_path.read_text(encoding="utf-8").replace(
        '"total_canonical_work": 576',
        '"total_canonical_work": 575',
    )
    report_path.write_text(payload, encoding="utf-8")
    assert main(("check", str(report_path))) == 2
    assert "component evidence error" in capsys.readouterr().err
