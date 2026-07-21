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
        assert resident["contract"]["contract_sha256"] == file_row["contract"]["contract_sha256"]
        assert resident["samples"][0]["metrics"]["temporary_bytes"] == 0
        assert file_row["samples"][0]["metrics"]["temporary_bytes"] > 0
    completion = cast(dict[str, Any], report["completion_requirements"])
    assert completion["file_lane_implemented"] is True


def test_complete_horned_common_adapter_requires_an_explicit_launcher(
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
    assert (
        "launcher environment PYOWL_CORE_HORNED_RUNNER is unset"
        in rows["horned-owl-common"]["reason"]
    )
    assert report["comparative_complete"] is False
    assert report["not_run_required"] == ["horned-owl-common"]
    completion = cast(dict[str, Any], report["completion_requirements"])
    assert "horned-owl-common" not in completion["missing_required_pins"]
    assert "owlapi-common" in completion["missing_required_pins"]


def test_complete_direct_adapter_requires_an_explicit_launcher(
    monkeypatch: object,
) -> None:
    cast(Any, monkeypatch).delenv("PYOWL_CORE_DIRECT_RUNNER", raising=False)
    report = run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=("pyowl-python-common", "pyowl-direct-rust-common"),
        warmups=0,
        repetitions=1,
    )

    rows = {value["lane"]: value for value in cast(list[dict[str, Any]], report["lanes"])}
    assert rows["pyowl-python-common"]["status"] == "ok"
    assert rows["pyowl-direct-rust-common"]["status"] == "not-run"
    assert (
        "launcher environment PYOWL_CORE_DIRECT_RUNNER is unset"
        in rows["pyowl-direct-rust-common"]["reason"]
    )
    completion = cast(dict[str, Any], report["completion_requirements"])
    assert "pyowl-direct-rust-common" not in completion["missing_required_pins"]


def test_complete_owlapi_pin_rejects_a_different_launcher(
    monkeypatch: object,
) -> None:
    cast(Any, monkeypatch).setenv("PYOWL_CORE_OWLAPI_RUNNER", "/usr/bin/false")
    report = run_comparator_baseline(
        corpus_ids=("generated-tiny-functional",),
        comparator_ids=("pyowl-python-common", "owlapi-common"),
        warmups=0,
        repetitions=1,
    )
    rows = {value["lane"]: value for value in cast(list[dict[str, Any]], report["lanes"])}

    assert rows["owlapi-common"]["status"] == "error"
    assert "SHA-256 differs" in rows["owlapi-common"]["reason"]


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
    assert (
        evidence["comparator_manifest_sha256"]
        != hashlib.sha256(DEFAULT_COMPARATOR_MANIFEST.read_bytes()).hexdigest()
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
        ROOT / "reports" / "performance" / "redesign-baseline" / "shared-host-py-horned-smoke.json"
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
        row["status"] == "ok" and len(cast(list[object], row["samples"])) == 3 for row in lanes
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


def test_committed_raw_horned_smoke_attests_real_persistent_lifecycle() -> None:
    evidence_path = (
        ROOT / "reports" / "performance" / "redesign-baseline" / "shared-host-horned-raw-smoke.json"
    )
    evidence = cast(dict[str, Any], json.loads(evidence_path.read_text(encoding="utf-8")))

    assert evidence["schema"] == "pyowl-core/comparator-baseline/v1"
    manifest_sha256 = "2ea395c5b1bbbdb6ce31013b59604b455fa837cc5a3a0d2485fc93e4c4614527"
    assert evidence["comparator_manifest_sha256"] == manifest_sha256
    assert (
        evidence["comparator_manifest_sha256"]
        != hashlib.sha256(DEFAULT_COMPARATOR_MANIFEST.read_bytes()).hexdigest()
    )
    source_identity = cast(dict[str, Any], evidence["source_identity"])
    source_inputs = cast(list[dict[str, Any]], source_identity["inputs"])
    inputs_by_path = {cast(str, value["path"]): value for value in source_inputs}
    assert inputs_by_path["benchmarks/comparators/comparators.toml"]["sha256"] == manifest_sha256
    environment = cast(dict[str, Any], evidence["environment"])
    assert environment["git_commit"] == "f6845ecf42cb756776084de286085ee70ccaad82"
    assert environment["git_dirty"] is False
    assert evidence["contract_valid"] is True
    assert evidence["execution_errors"] == []
    assert evidence["comparative_complete"] is False

    lanes = cast(list[dict[str, Any]], evidence["lanes"])
    assert len(lanes) == 8
    assert {cast(str, row["lane"]) for row in lanes} == {
        "pyowl-python-common",
        "horned-owl-raw",
    }
    assert all(
        row["status"] == "ok" and len(cast(list[object], row["samples"])) == 3 for row in lanes
    )
    raw_rows = [row for row in lanes if row["lane"] == "horned-owl-raw"]
    raw_samples = [
        cast(dict[str, Any], sample)
        for row in raw_rows
        for sample in cast(list[object], row["samples"])
    ]
    raw_inventories = [cast(dict[str, Any], sample["raw_inventory"]) for sample in raw_samples]
    assert {value["inventory_sha256"] for value in raw_inventories} == {
        "a980867761d4a9a25eb34dac6a3ce76dfa046533c341fae81839c3c36f076729"
    }
    assert {
        (
            value["axiom_count"],
            value["annotation_count"],
            value["import_count"],
            value["entity_count"],
            value["diagnostic_count"],
        )
        for value in raw_inventories
    } == {(15, 0, 0, 8, 0)}
    assert {
        cast(int, sample["metrics"]["temporary_bytes"])
        for sample in raw_samples
        if sample["input_mode"] == "resident-bytes"
    } == {0}
    assert all(
        cast(int, sample["metrics"]["temporary_bytes"]) > 0
        for sample in raw_samples
        if sample["input_mode"] == "file"
    )

    assertions = cast(list[dict[str, Any]], evidence["equality_assertions"])
    assert len(assertions) == 6
    assert all(row["passed"] is True for row in assertions)
    lifecycles = cast(list[dict[str, Any]], evidence["persistent_runner_lifecycles"])
    assert len(lifecycles) == 1
    lifecycle = lifecycles[0]
    assert lifecycle["lane"] == "horned-owl-raw"
    assert lifecycle["status"] == "pass"
    assert lifecycle["request_count"] == lifecycle["response_count"] == 8
    assert lifecycle["unique_ontology_instance_count"] == 8
    assert lifecycle["shutdown"] == "clean-exit"
    assert lifecycle["stderr_bytes"] == 0
    handshake = cast(dict[str, Any], lifecycle["handshake"])
    artifact = cast(dict[str, Any], handshake["artifact"])
    assert artifact["runner_revision"] == "pyowl-core-horned-raw-runner-v1"
    assert artifact["features"] == ["default"]
    assert artifact["artifact_sha256"] == (
        "877f6118b6f5823bb135d04e36fe2c2d3a2b4493feca8ac09b5fa6e91b9fff9e"
    )
    assert artifact["runner_sha256"] == (
        "f4f18428bf9f115635a168cd690b201ebdd11ff3c0589bb6196993d948223f8a"
    )
    completion = cast(dict[str, Any], evidence["completion_requirements"])
    assert completion["file_lane_implemented"] is True
    assert completion["paired_randomization_implemented"] is True
    assert completion["passed"] is False


def test_committed_horned_common_smoke_attests_exact_shared_runner_lanes() -> None:
    evidence_path = (
        ROOT
        / "reports"
        / "performance"
        / "redesign-baseline"
        / "shared-host-horned-common-smoke.json"
    )
    evidence = cast(dict[str, Any], json.loads(evidence_path.read_text(encoding="utf-8")))

    assert evidence["schema"] == "pyowl-core/comparator-baseline/v1"
    assert evidence["comparator_manifest_sha256"] == (
        "dab9725356d3cd34c0db47cf9b4f078f73d9a8d2919c6bec9225c74c252b3406"
    )
    source_inputs = {
        cast(str, value["path"]): value
        for value in cast(list[dict[str, Any]], evidence["source_identity"]["inputs"])
    }
    assert (
        source_inputs["benchmarks/comparators/comparators.toml"]["sha256"]
        == evidence["comparator_manifest_sha256"]
    )
    environment = cast(dict[str, Any], evidence["environment"])
    assert environment["git_commit"] == "f12a4f1d9661969ca9fb3777f5e379489eda8f23"
    assert environment["git_dirty"] is False
    assert evidence["contract_valid"] is True
    assert evidence["execution_errors"] == []
    assert evidence["not_run_required"] == []
    assert evidence["comparative_complete"] is False

    lanes = cast(list[dict[str, Any]], evidence["lanes"])
    assert len(lanes) == 12
    assert {cast(str, row["lane"]) for row in lanes} == {
        "pyowl-python-common",
        "horned-owl-raw",
        "horned-owl-common",
    }
    assert all(
        row["status"] == "ok" and len(cast(list[object], row["samples"])) == 3 for row in lanes
    )
    common_contracts = {
        cast(str, cast(dict[str, Any], row["contract"])["contract_sha256"])
        for row in lanes
        if row["lane"] in {"pyowl-python-common", "horned-owl-common"}
    }
    assert common_contracts == {"85a8fb3eed3ecd3d637b080f7816840abb0af59c34540f2f2ce22832ff92f1d8"}
    assertions = cast(list[dict[str, Any]], evidence["equality_assertions"])
    assert len(assertions) == 12
    assert all(row["passed"] is True for row in assertions)

    lifecycles = {
        cast(str, row["lane"]): row
        for row in cast(list[dict[str, Any]], evidence["persistent_runner_lifecycles"])
    }
    assert set(lifecycles) == {"horned-owl-raw", "horned-owl-common"}
    for lifecycle in lifecycles.values():
        assert lifecycle["status"] == "pass"
        assert lifecycle["request_count"] == lifecycle["response_count"] == 8
        assert lifecycle["unique_ontology_instance_count"] == 8
        assert lifecycle["shutdown"] == "clean-exit"
        assert lifecycle["stderr_bytes"] == 0
        artifact = cast(dict[str, Any], lifecycle["handshake"])["artifact"]
        assert artifact["features"] == ["default", "independent-common-contract-v1"]
        assert artifact["runner_sha256"] == (
            "ffd20194b7c3715d6d07ec8ba9167d590ed484c278305754148711c44ae8887b"
        )
    assert lifecycles["horned-owl-raw"]["handshake"]["artifact"]["runner_revision"] == (
        "pyowl-core-horned-raw-runner-v2"
    )
    assert (
        lifecycles["horned-owl-common"]["handshake"]["artifact"]["runner_revision"]
        == "pyowl-core-horned-common-runner-v1"
    )


def test_committed_direct_smoke_attests_retained_runner_lifecycle() -> None:
    evidence_path = (
        ROOT / "reports" / "performance" / "redesign-baseline" / "shared-host-direct-smoke.json"
    )
    evidence = cast(dict[str, Any], json.loads(evidence_path.read_text(encoding="utf-8")))

    assert evidence["schema"] == "pyowl-core/comparator-baseline/v1"
    assert evidence["contract_valid"] is True
    assert evidence["comparative_complete"] is False
    assert evidence["execution_errors"] == []
    assert evidence["not_run_required"] == []
    assert evidence["environment"]["git_commit"] == ("588853ef1a761101e721aeb4c527074a0a2276d6")
    assert evidence["environment"]["git_dirty"] is False
    manifest_sha256 = "4a6fca7c0247ab973db960c49565713a2caf0b7fd826a6d94730042016ec8a05"
    assert evidence["comparator_manifest_sha256"] == manifest_sha256
    assert manifest_sha256 != hashlib.sha256(DEFAULT_COMPARATOR_MANIFEST.read_bytes()).hexdigest()
    source_inputs = {
        cast(str, value["path"]): value
        for value in cast(list[dict[str, Any]], evidence["source_identity"]["inputs"])
    }
    assert source_inputs["benchmarks/comparators/comparators.toml"]["sha256"] == manifest_sha256

    corpora = cast(list[dict[str, Any]], evidence["corpora"])
    assert {(row["id"], row["format"]) for row in corpora} == {
        ("generated-tiny-functional", "functional"),
        ("generated-medium-rdfxml", "rdfxml"),
    }
    lanes = cast(list[dict[str, Any]], evidence["lanes"])
    assert len(lanes) == 16
    assert {row["lane"] for row in lanes} == {
        "pyowl-python-common",
        "pyowl-direct-rust-common",
    }
    assert all(row["status"] == "ok" and len(row["samples"]) == 3 for row in lanes)
    assertions = cast(list[dict[str, Any]], evidence["equality_assertions"])
    assert len(assertions) == 24
    assert all(row["passed"] is True for row in assertions)

    direct_samples = [
        sample
        for row in lanes
        if row["lane"] == "pyowl-direct-rust-common"
        for sample in cast(list[dict[str, Any]], row["samples"])
    ]
    assert all(
        sample["artifact"]["runner_revision"] == "pyowl-core-direct-rust-common-runner-v1"
        and sample["artifact"]["runner_sha256"]
        == "a36fd6f0bcef1ef60474585001425199ae2c5fec2b9fe21c33fd82bbdf982525"
        for sample in direct_samples
    )
    lifecycles = cast(list[dict[str, Any]], evidence["persistent_runner_lifecycles"])
    assert len(lifecycles) == 1
    lifecycle = lifecycles[0]
    assert lifecycle["lane"] == "pyowl-direct-rust-common"
    assert lifecycle["status"] == "pass"
    assert lifecycle["request_count"] == lifecycle["response_count"] == 16
    assert lifecycle["unique_ontology_instance_count"] == 16
    assert lifecycle["stderr_bytes"] == 0
    assert lifecycle["shutdown"] == "clean-exit"


def test_committed_owlapi_smoke_attests_exact_four_syntax_lifecycle() -> None:
    evidence_path = (
        ROOT / "reports" / "performance" / "redesign-baseline" / "shared-host-owlapi-smoke.json"
    )
    evidence = cast(dict[str, Any], json.loads(evidence_path.read_text(encoding="utf-8")))

    assert evidence["schema"] == "pyowl-core/comparator-baseline/v1"
    assert evidence["contract_valid"] is True
    assert evidence["comparative_complete"] is False
    assert evidence["execution_errors"] == []
    assert evidence["not_run_required"] == []
    assert evidence["environment"]["git_commit"] == ("c7e8b7264bbe5513867bd37a4018bda3ce2ddb07")
    assert evidence["environment"]["git_dirty"] is False
    manifest_sha256 = "9b5672b86c13d39c64b5ad4ff109a55844200eb00272e4552210d28d0c429de3"
    assert evidence["comparator_manifest_sha256"] == manifest_sha256
    source_inputs = {
        cast(str, value["path"]): value
        for value in cast(list[dict[str, Any]], evidence["source_identity"]["inputs"])
    }
    assert source_inputs["benchmarks/comparators/comparators.toml"]["sha256"] == manifest_sha256

    corpora = cast(list[dict[str, Any]], evidence["corpora"])
    assert {row["format"] for row in corpora} == {
        "functional",
        "owlxml",
        "rdfxml",
        "turtle",
    }
    assert {row["id"] for row in corpora} >= {
        "generated-annotation-list",
        "generated-import-diamond",
    }
    lanes = cast(list[dict[str, Any]], evidence["lanes"])
    assert len(lanes) == 48
    assert {row["lane"] for row in lanes} == {
        "pyowl-python-common",
        "owlapi-common",
    }
    assert all(row["status"] == "ok" and len(row["samples"]) == 3 for row in lanes)
    assertions = cast(list[dict[str, Any]], evidence["equality_assertions"])
    assert len(assertions) == 72
    assert all(row["passed"] is True for row in assertions)

    owlapi_samples = [
        sample
        for row in lanes
        if row["lane"] == "owlapi-common"
        for sample in cast(list[dict[str, Any]], row["samples"])
    ]
    assert all(
        sample["artifact"]["runner_revision"] == "pyowl-core-owlapi-common-runner-v1"
        and sample["artifact"]["artifact_sha256"]
        == "747b1a5269fee2992487dcde946f16dfbc14aa458d50854994a0485cf263ce07"
        and sample["artifact"]["runner_sha256"]
        == "04aeb9b6f995b1abcb26ae022969992ccc428a177b6aeaa76cf8a762a3ea7b07"
        for sample in owlapi_samples
    )
    lifecycles = cast(list[dict[str, Any]], evidence["persistent_runner_lifecycles"])
    assert len(lifecycles) == 1
    lifecycle = lifecycles[0]
    assert lifecycle["lane"] == "owlapi-common"
    assert lifecycle["status"] == "pass"
    assert lifecycle["request_count"] == lifecycle["response_count"] == 48
    assert lifecycle["unique_ontology_instance_count"] == 48
    assert lifecycle["stderr_bytes"] == 0
    assert lifecycle["shutdown"] == "clean-exit"


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
        tuple(lane for _, lane in sorted(by_block[block_index])) for block_index in sorted(by_block)
    )
