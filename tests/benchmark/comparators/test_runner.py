from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

import tools.benchmark.comparators.runner as runner_module
from tools.benchmark.comparators.manifest import DEFAULT_COMPARATOR_MANIFEST, ROOT
from tools.benchmark.comparators.ratio_statistics import MAX_U64
from tools.benchmark.comparators.runner import (
    ComparatorRunError,
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
        "large_biomedical_rdfxml": [],
        "annotation_list_heavy": [],
    }
    assert completion["file_lane_implemented"] is True
    assert completion["paired_randomization_implemented"] is True
    assert completion["ratio_gates"]["configured"] is True
    assert completion["ratio_gates"]["passed"] is False
    assert report["methodology"]["schedule"]["seed"] == 0
    assert "seeded" in report["methodology"]["comparison_order"]
    sample = rows[0]["samples"][0]
    assert sample["schedule_seed"] == 0
    assert sample["paired_block"] == 0
    assert sample["implementation_order"] == 0
    assert sample["paired_block_size"] == 1


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
    original_run_once = runner_module._run_once_with_persistent_lifecycle

    def inject_error(
        pin: Any,
        request: Any,
        *,
        runners: Any,
        failures: Any,
    ) -> dict[str, Any]:
        result = original_run_once(
            pin,
            request,
            runners=runners,
            failures=failures,
        )
        if pin.id == "horned-owl-common":
            result["status"] = "error"
            result["reason"] = "hostile runner fixture"
        return result

    monkeypatch.setattr(
        runner_module,
        "_run_once_with_persistent_lifecycle",
        inject_error,
    )
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


def test_paired_schedule_is_reproducible_and_balances_warmups_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run_once = runner_module._run_once_with_persistent_lifecycle

    def execute() -> tuple[list[str], int, dict[str, Any]]:
        invocations: list[str] = []
        cleanup_count = 0

        def record_run(
            pin: Any,
            request: Any,
            *,
            runners: Any,
            failures: Any,
        ) -> dict[str, Any]:
            invocations.append(cast(str, pin.id))
            return original_run_once(
                pin,
                request,
                runners=runners,
                failures=failures,
            )

        def record_cleanup() -> None:
            nonlocal cleanup_count
            cleanup_count += 1

        with monkeypatch.context() as context:
            context.setattr(
                runner_module,
                "_run_once_with_persistent_lifecycle",
                record_run,
            )
            context.setattr(runner_module, "_cleanup_barrier", record_cleanup)
            report = run_comparator_baseline(
                corpus_ids=("generated-tiny-functional",),
                comparator_ids=("pyowl-python-common", "horned-owl-common"),
                process_modes=("steady-process",),
                input_modes=("resident-bytes",),
                warmups=2,
                repetitions=3,
                seed=123456789,
            )
        return invocations, cleanup_count, report

    first_invocations, first_cleanup_count, first_report = execute()
    second_invocations, second_cleanup_count, second_report = execute()

    assert first_invocations == second_invocations
    assert first_cleanup_count == second_cleanup_count == 10
    assert first_invocations.count("pyowl-python-common") == 5
    assert first_invocations.count("horned-owl-common") == 5
    assert _measured_schedule(first_report) == _measured_schedule(second_report)
    for row in cast(list[dict[str, Any]], second_report["lanes"]):
        assert [sample["paired_block"] for sample in row["samples"]] == [0, 1, 2]
        assert all(sample["schedule_seed"] == 123456789 for sample in row["samples"])


@pytest.mark.parametrize("seed", [-1, MAX_U64 + 1, True, 1.0])
def test_runner_api_rejects_non_u64_seed(seed: object) -> None:
    with pytest.raises(ComparatorRunError, match="seed"):
        run_comparator_baseline(warmups=0, repetitions=1, seed=cast(Any, seed))


@pytest.mark.parametrize("seed", ["-1", str(MAX_U64 + 1), "+1", "1.0", "true"])
def test_cli_rejects_seed_outside_exact_unsigned_decimal_domain(seed: str) -> None:
    with pytest.raises(SystemExit) as raised:
        main(("--seed", seed, "--allow-partial"))
    assert raised.value.code == 2


def test_cli_records_maximum_u64_seed(tmp_path: Path) -> None:
    output = tmp_path / "seed.json"
    assert (
        main(
            (
                "--seed",
                str(MAX_U64),
                "--warmups",
                "0",
                "--repetitions",
                "1",
                "--allow-partial",
                "--output",
                str(output),
            )
        )
        == 0
    )
    report = cast(dict[str, Any], json.loads(output.read_text(encoding="utf-8")))
    assert report["methodology"]["schedule"]["seed"] == MAX_U64
    assert report["lanes"][0]["samples"][0]["schedule_seed"] == MAX_U64


def test_committed_shared_host_smoke_is_self_bound_historical_evidence() -> None:
    evidence_path = (
        ROOT / "reports" / "performance" / "redesign-baseline" / "shared-host-smoke.json"
    )
    evidence = cast(dict[str, Any], json.loads(evidence_path.read_text(encoding="utf-8")))

    assert evidence["schema"] == "pyowl-core/comparator-baseline/v1"
    assert evidence["comparator_manifest_sha256"] == (
        "54fa26e8f35b4d252e2b0b62bfea90635b66e37892244117c40d7d1967df23ae"
    )
    assert evidence["comparator_manifest_sha256"] != hashlib.sha256(
        DEFAULT_COMPARATOR_MANIFEST.read_bytes()
    ).hexdigest()
    assert (
        evidence["corpus_manifest_sha256"]
        == hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest()
    )
    source_identity = cast(dict[str, Any], evidence["source_identity"])
    source_inputs = cast(list[dict[str, Any]], source_identity["inputs"])
    assert source_identity["schema"] == "pyowl-core/comparator-runtime-source/v1"
    assert len(cast(str, source_identity["sha256"])) == 64
    assert source_identity["input_count"] == len(source_inputs)
    inputs_by_path = {cast(str, value["path"]): value for value in source_inputs}
    assert "tools/benchmark/comparators/runner.py" in inputs_by_path
    assert (
        inputs_by_path["benchmarks/comparators/comparators.toml"]["sha256"]
        == evidence["comparator_manifest_sha256"]
    )
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


def test_committed_py_horned_smoke_attests_real_persistent_lifecycle() -> None:
    evidence_path = (
        ROOT
        / "reports"
        / "performance"
        / "redesign-baseline"
        / "shared-host-py-horned-smoke.json"
    )
    evidence = cast(dict[str, Any], json.loads(evidence_path.read_text(encoding="utf-8")))

    assert evidence["schema"] == "pyowl-core/comparator-baseline/v1"
    assert evidence["comparator_manifest_sha256"] == (
        "a5c5599d50276a0a479db798f721ce2379ac72bbb6ae7c7a6516ee8ff6dc2985"
    )
    source_identity = cast(dict[str, Any], evidence["source_identity"])
    source_inputs = cast(list[dict[str, Any]], source_identity["inputs"])
    inputs_by_path = {cast(str, value["path"]): value for value in source_inputs}
    assert (
        inputs_by_path["benchmarks/comparators/comparators.toml"]["sha256"]
        == evidence["comparator_manifest_sha256"]
    )
    environment = cast(dict[str, Any], evidence["environment"])
    assert environment["git_commit"] == "3315c2276123f0c228e476412363f41a9c6dd21d"
    assert environment["git_dirty"] is False
    assert evidence["contract_valid"] is True
    assert evidence["execution_errors"] == []
    assert evidence["comparative_complete"] is False

    lanes = cast(list[dict[str, Any]], evidence["lanes"])
    assert len(lanes) == 8
    assert {cast(str, row["lane"]) for row in lanes} == {
        "pyowl-python-common",
        "py-horned-common",
    }
    assert all(
        row["status"] == "ok" and len(cast(list[object], row["samples"])) == 3
        for row in lanes
    )
    assertions = cast(list[dict[str, Any]], evidence["equality_assertions"])
    assert len(assertions) == 12
    assert all(row["passed"] is True for row in assertions)

    lifecycles = cast(list[dict[str, Any]], evidence["persistent_runner_lifecycles"])
    assert len(lifecycles) == 1
    lifecycle = lifecycles[0]
    assert lifecycle["lane"] == "py-horned-common"
    assert lifecycle["status"] == "pass"
    assert lifecycle["request_count"] == lifecycle["response_count"] == 8
    assert lifecycle["unique_ontology_instance_count"] == 8
    assert lifecycle["shutdown"] == "clean-exit"
    assert lifecycle["stderr_bytes"] == 0
    handshake = cast(dict[str, Any], lifecycle["handshake"])
    artifact = cast(dict[str, Any], handshake["artifact"])
    assert artifact["runner_revision"] == "pyowl-core-py-horned-common-runner-v2"
    assert artifact["features"] == [
        "abi3-wrapper",
        "independent-common-contract-v1",
        "verified-sdist-install-v1",
    ]
    assert artifact["artifact_sha256"] == (
        "7146d0887c5ec119e423e56c9221cc0ca7da54739be36ce3ed916503348f942d"
    )
    assert artifact["runner_sha256"] == (
        "4e1c48058a84e336d31da33077eff2bd2a69aa64c9787e27b1029efe3c0f8012"
    )
    completion = cast(dict[str, Any], evidence["completion_requirements"])
    assert completion["file_lane_implemented"] is True
    assert completion["paired_randomization_implemented"] is True
    assert completion["passed"] is False


def _measured_schedule(report: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    rows = cast(Sequence[Mapping[str, Any]], report["lanes"])
    by_block: dict[int, list[tuple[int, str]]] = {}
    for row in rows:
        for sample in cast(Sequence[Mapping[str, Any]], row["samples"]):
            by_block.setdefault(cast(int, sample["paired_block"]), []).append(
                (
                    cast(int, sample["implementation_order"]),
                    cast(str, row["lane"]),
                )
            )
    return tuple(
        tuple(lane for _, lane in sorted(by_block[block_index]))
        for block_index in sorted(by_block)
    )
