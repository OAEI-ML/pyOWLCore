from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

import tools.benchmark.comparators.runner as runner_module
from tools.benchmark.comparators.manifest import DEFAULT_COMPARATOR_MANIFEST, ROOT
from tools.benchmark.comparators.runner import (
    main,
    run_comparator_baseline,
)
from tools.benchmark.manifest import DEFAULT_MANIFEST


def test_python_reference_reports_separate_fresh_and_steady_raw_samples() -> None:
    report = run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=("pyowl-python-common",),
        process_modes=("steady-process", "fresh-process"),
        input_modes=("resident-bytes",),
        warmups=0,
        repetitions=1,
    )

    rows = cast(list[dict[str, Any]], report["lanes"])
    assert report["contract_valid"] is True
    assert report["comparative_complete"] is False
    assert {row["process_mode"] for row in rows} == {"steady-process", "fresh-process"}
    assert all(row["status"] == "ok" for row in rows)
    assert all(len(row["samples"]) == 1 for row in rows)
    assert all("contract" not in row["samples"][0] for row in rows)
    assert all(row["samples"][0]["contract_sha256"] for row in rows)
    fresh = next(row for row in rows if row["process_mode"] == "fresh-process")
    assert fresh["samples"][0]["metrics"]["startup_to_ready_ns"] > 0
    assert all(item["passed"] is True for item in report["equality_assertions"])
    completion = cast(dict[str, Any], report["completion_requirements"])
    assert completion["passed"] is False
    assert completion["selected_representative_corpora"] == {
        "medium": [],
        "large": [],
        "annotation_list_heavy": [],
    }
    assert completion["file_lane_implemented"] is True
    assert completion["paired_randomization_implemented"] is False
    assert completion["ratio_gates"] == {
        "configured": False,
        "passed": False,
        "reason": "no executable ratio-gate configuration is wired into this runner",
    }
    assert "not implemented" in report["methodology"]["comparison_order"]


def test_python_file_lane_is_timed_and_semantically_matches_resident_bytes() -> None:
    report = run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=("pyowl-python-common",),
        process_modes=("steady-process", "fresh-process"),
        input_modes=("resident-bytes", "file"),
        warmups=0,
        repetitions=1,
    )

    rows = {
        (cast(str, row["input_mode"]), cast(str, row["process_mode"])): row
        for row in cast(list[dict[str, Any]], report["lanes"])
    }
    for process_mode in ("steady-process", "fresh-process"):
        resident = rows[("resident-bytes", process_mode)]
        file_row = rows[("file", process_mode)]
        assert resident["status"] == file_row["status"] == "ok"
        assert resident["contract"]["contract_sha256"] == file_row["contract"][
            "contract_sha256"
        ]
        assert resident["samples"][0]["metrics"]["temporary_bytes"] == 0
        assert file_row["samples"][0]["metrics"]["temporary_bytes"] > 0
    completion = cast(dict[str, Any], report["completion_requirements"])
    assert completion["file_lane_implemented"] is True


def test_pending_horned_common_adapter_is_not_run_and_never_a_pass(
    monkeypatch: object,
) -> None:
    # pytest's monkeypatch fixture is intentionally used through its public method
    cast(Any, monkeypatch).delenv("PYOWL_CORE_HORNED_RUNNER", raising=False)
    report = run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=("pyowl-python-common", "horned-owl-common"),
        warmups=0,
        repetitions=1,
    )

    rows = {value["lane"]: value for value in cast(list[dict[str, Any]], report["lanes"])}
    assert rows["pyowl-python-common"]["status"] == "ok"
    assert rows["horned-owl-common"]["status"] == "not-run"
    assert "pending" in rows["horned-owl-common"]["reason"]
    assert report["comparative_complete"] is False
    assert report["not_run_required"] == ["horned-owl-common"]
    completion = cast(dict[str, Any], report["completion_requirements"])
    assert "horned-owl-common" not in completion["missing_required_pins"]
    assert "owlapi-common" in completion["missing_required_pins"]


def test_pending_owlapi_pin_cannot_execute_even_when_launcher_is_set(
    monkeypatch: object,
) -> None:
    cast(Any, monkeypatch).setenv("PYOWL_CORE_OWLAPI_RUNNER", "/bin/false")
    report = run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=("pyowl-python-common", "owlapi-common"),
        warmups=0,
        repetitions=1,
    )
    rows = {value["lane"]: value for value in cast(list[dict[str, Any]], report["lanes"])}

    assert rows["owlapi-common"]["status"] == "not-run"
    assert "pending" in rows["owlapi-common"]["reason"]


def test_cli_requires_explicit_partial_evidence_mode(tmp_path: Path) -> None:
    output = tmp_path / "smoke.json"
    arguments = (
        "--warmups",
        "0",
        "--repetitions",
        "1",
        "--output",
        str(output),
    )

    assert main(arguments) == 1
    assert main((*arguments, "--allow-partial")) == 0


def test_partial_mode_never_masks_a_selected_lane_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_run_once = runner_module._run_once

    def inject_error(pin: Any, request: Any) -> dict[str, Any]:
        result = original_run_once(pin, request)
        if pin.id == "horned-owl-common":
            result["status"] = "error"
            result["reason"] = "hostile runner fixture"
        return result

    monkeypatch.setattr(runner_module, "_run_once", inject_error)
    report = run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=("pyowl-python-common", "horned-owl-common"),
        warmups=0,
        repetitions=1,
    )

    assert all(item["passed"] is True for item in report["equality_assertions"])
    assert report["contract_valid"] is False
    assert report["execution_errors"] == [
        {
            "lane": "horned-owl-common",
            "corpus_id": "generated-tiny-functional",
            "input_mode": "resident-bytes",
            "process_mode": "steady-process",
            "reason": "hostile runner fixture",
        }
    ]

    def replay_report(**_: object) -> dict[str, Any]:
        return report

    monkeypatch.setattr(runner_module, "run_comparator_baseline", replay_report)
    assert main(("--allow-partial", "--output", str(tmp_path / "report.json"))) == 1


def test_committed_shared_host_smoke_matches_current_manifests() -> None:
    evidence_path = (
        ROOT / "reports" / "performance" / "redesign-baseline" / "shared-host-smoke.json"
    )
    evidence = cast(dict[str, Any], json.loads(evidence_path.read_text(encoding="utf-8")))

    assert evidence["schema"] == "pyowl-core/comparator-baseline/v1"
    assert (
        evidence["comparator_manifest_sha256"]
        == hashlib.sha256(DEFAULT_COMPARATOR_MANIFEST.read_bytes()).hexdigest()
    )
    assert (
        evidence["corpus_manifest_sha256"]
        == hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest()
    )
    source_identity = cast(dict[str, Any], evidence["source_identity"])
    source_inputs = cast(list[dict[str, Any]], source_identity["inputs"])
    assert source_identity["schema"] == "pyowl-core/comparator-runtime-source/v1"
    assert len(cast(str, source_identity["sha256"])) == 64
    assert source_identity["input_count"] == len(source_inputs)
    assert "tools/benchmark/comparators/runner.py" in {
        cast(str, value["path"]) for value in source_inputs
    }
    environment = cast(dict[str, Any], evidence["environment"])
    assert environment["git_dirty"] is False

    audit_path = (
        ROOT / "reports" / "performance" / "redesign-baseline" / "dependency-audit-shared-host.json"
    )
    audit = cast(dict[str, Any], json.loads(audit_path.read_text(encoding="utf-8")))
    audit_identity = cast(dict[str, Any], audit["source_identity"])
    audit_git = cast(dict[str, Any], audit_identity["git"])
    assert audit["status"] == "pass"
    assert audit_git["revision"] == environment["git_commit"]
    assert audit_git["dirty"] is False
    assert audit_git["inspected_inputs_dirty"] is False
    assert evidence["contract_valid"] is True
    assert evidence["execution_errors"] == []
    assert evidence["comparative_complete"] is False
    assert len(cast(list[object], evidence["lanes"])) == 14
    completion = cast(dict[str, Any], evidence["completion_requirements"])
    assert completion["passed"] is False
    assert completion["file_lane_implemented"] is False
    assert completion["paired_randomization_implemented"] is False
