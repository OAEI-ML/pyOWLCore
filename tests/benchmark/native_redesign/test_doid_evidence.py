from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, cast

import pytest

from tools.benchmark.comparators.ratio_statistics import paired_bootstrap_ratio_summary

ROOT = Path(__file__).parents[3]
EVIDENCE = ROOT / "reports" / "performance" / "native-redesign" / "doid-installed-vs-py-horned.json"

SCHEMA = "pyowl-core/doid-installed-vs-py-horned-evidence/v1"
CORPUS_ID = "oaei-bioml-doid-2024"
NATIVE_LANE = "pyowl-native-wheel-common"
COMPARATOR_LANE = "py-horned-common"
SOURCE_SHA256 = "76f41cce3616ad1a9ba6353f469e96bde7addba5d43e541651a3ab703f9ba2bc"
OPTIONS_SHA256 = "fdfc954b7b8f0253c8e90ee4542170f506ca069ac6bd93744ac0ceabf04f8d2f"
CONTRACT_SHA256 = "e52c01bbbaf8a4ad9b9a57277a6fbaf420ccba3da0390575f21c256cbe0f3ff1"

ROW_KEYS = {
    "process_mode",
    "lane",
    "paired_block",
    "paired_block_size",
    "implementation_order",
    "schedule_seed",
    "status",
    "source_sha256",
    "options_sha256",
    "contract_sha256",
    "metrics",
    "transport",
}
METRIC_KEYS = {
    "wall_ns",
    "load_ns",
    "common_adapter_ns",
    "cpu_ns",
    "startup_to_ready_ns",
    "startup_to_ready_cpu_ns",
    "rss_peak_before_bytes",
    "rss_peak_after_bytes",
    "rss_peak_increment_bytes",
    "temporary_bytes",
    "timed_validation_ns",
}
TRANSPORT_KEYS = {
    "parent_wall_ns",
    "parent_cpu_ns",
    "rss_interval_incremental_peak_bytes",
    "rss_interval_peak_bytes",
    "rss_interval_quiescent_current_bytes",
    "rss_interval_maximum_sample_gap_ns",
    "rss_interval_sample_count",
}


def _load() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(EVIDENCE.read_text(encoding="utf-8")))


def _mapping(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _sequence(value: object) -> list[Any]:
    assert isinstance(value, list)
    return cast(list[Any], value)


def _integer(value: object) -> int:
    assert isinstance(value, int)
    assert not isinstance(value, bool)
    return value


def _at(value: dict[str, Any], path: tuple[str, ...]) -> object:
    selected: object = value
    for key in path:
        selected = _mapping(selected)[key]
    return selected


def _selected_integer(row: dict[str, Any], selector: str) -> int:
    selected: object = row
    for key in selector.split("."):
        selected = _mapping(selected)[key]
    return _integer(selected)


def test_doid_evidence_pins_exact_sources_artifacts_and_environment() -> None:
    evidence = _load()

    assert evidence["schema"] == SCHEMA
    assert set(evidence) == {
        "schema",
        "scope",
        "source_artifacts",
        "identities",
        "contract",
        "environment",
        "measurement_contract",
        "raw_samples",
        "paired_comparisons",
        "interpretations",
    }

    expected_hashes = {
        ("source_artifacts", "fresh", "sha256"): (
            "d4f7283faa88f4a85ad86ac03361ebd2058bfd65816b29c2adde7c4b11a04acc"
        ),
        ("source_artifacts", "steady", "sha256"): (
            "ad64fb80e058d228f6d62e4d56cb1c4d5207549ddf78145be6eb19c0067fe022"
        ),
        ("identities", "pyowl_core", "comparator_runtime_source_sha256"): (
            "06f5b9c34c3daefd3cf110dde4b8c66883c61de9a7aa4711021bc27ba07c1c70"
        ),
        ("identities", "pyowl_core", "comparator_manifest_sha256"): (
            "9ce89e228341de71e8349beab6dad427ca0431224b778d98b92e5c61939c9824"
        ),
        ("identities", "pyowl_core", "corpus_manifest_sha256"): (
            "9442d9c813c3333a4e65dbbee9ce7519d4af9a10b3c1df3ccec31c2d43ba26d1"
        ),
        ("identities", "input", "sha256"): SOURCE_SHA256,
        ("identities", "input", "options_sha256"): OPTIONS_SHA256,
        ("identities", "installed_native", "wheel", "sha256"): (
            "18269febae8e34592a5e80ce67710da34df1e16a36d23001b2e92ede60d7123b"
        ),
        ("identities", "installed_native", "extension", "sha256"): (
            "39e02360635ff698b4113dbb9695617c77641d16142688d050bf9993ddf1e4a1"
        ),
        ("identities", "py_horned", "artifact_sha256"): (
            "7146d0887c5ec119e423e56c9221cc0ca7da54739be36ce3ed916503348f942d"
        ),
        ("identities", "py_horned", "runner_sha256"): (
            "a0560ea886258c8f2291a2bf0565bbcb14a40feebe069ba6a757a9d821a60849"
        ),
        ("contract", "sha256"): CONTRACT_SHA256,
    }
    for path, expected in expected_hashes.items():
        observed = _at(evidence, path)
        assert observed == expected
        assert isinstance(observed, str)
        assert re.fullmatch(r"[0-9a-f]{64}", observed)

    core = _mapping(_mapping(evidence["identities"])["pyowl_core"])
    assert core["git_commit"] == "005c3ccad129757b3a9be125dc064b812b607ef5"
    assert core["git_tree"] == "d4f3f29f6594b59f3d45a4811c38fb761a7028b9"
    assert core["git_dirty"] is False
    assert core["version"] == "0.1.0.dev0"

    installed = _mapping(_mapping(evidence["identities"])["installed_native"])
    assert _mapping(installed["wheel"]) == {
        "artifact": "pyowl_core-0.1.0.dev0-cp312-cp312-macosx_14_0_x86_64.whl",
        "bytes": 1631524,
        "sha256": "18269febae8e34592a5e80ce67710da34df1e16a36d23001b2e92ede60d7123b",
    }
    assert _mapping(installed["extension"]) == {
        "artifact": "_native.cpython-312-darwin.so",
        "bytes": 2972028,
        "sha256": "39e02360635ff698b4113dbb9695617c77641d16142688d050bf9993ddf1e4a1",
    }

    source_artifacts = _mapping(evidence["source_artifacts"])
    assert _mapping(source_artifacts["fresh"])["bytes"] == 43532571
    assert _mapping(source_artifacts["steady"])["bytes"] == 43534346
    assert all(
        _mapping(source_artifacts[mode])["raw_schema"] == "pyowl-core/comparator-baseline/v1"
        for mode in ("fresh", "steady")
    )

    environment = _mapping(evidence["environment"])
    assert environment["host_class"] == "shared concurrent development host"
    assert environment["reference_machine_approval"] == "pending"
    assert environment["reference_machine_matches"] is False
    assert environment["logical_cpu_count"] == 12
    assert environment["physical_memory_bytes"] == 34359738368
    assert _mapping(environment["python"])["version"] == "3.12.3"


def test_all_twelve_raw_rows_are_exactly_paired_and_contract_equal() -> None:
    evidence = _load()
    scope = _mapping(evidence["scope"])
    samples = [_mapping(value) for value in _sequence(evidence["raw_samples"])]

    assert scope["raw_sample_count"] == 12
    assert len(samples) == 12
    assert Counter((row["process_mode"], row["lane"]) for row in samples) == {
        ("fresh-process", NATIVE_LANE): 3,
        ("fresh-process", COMPARATOR_LANE): 3,
        ("steady-process", NATIVE_LANE): 3,
        ("steady-process", COMPARATOR_LANE): 3,
    }
    assert {(row["process_mode"], row["lane"], row["paired_block"]) for row in samples} == {
        (mode, lane, block)
        for mode in ("fresh-process", "steady-process")
        for lane in (NATIVE_LANE, COMPARATOR_LANE)
        for block in range(3)
    }

    for row in samples:
        assert set(row) == ROW_KEYS
        assert row["paired_block_size"] == 2
        assert row["schedule_seed"] == 180643
        assert row["status"] == "ok"
        assert row["source_sha256"] == SOURCE_SHA256
        assert row["options_sha256"] == OPTIONS_SHA256
        assert row["contract_sha256"] == CONTRACT_SHA256
        _integer(row["paired_block"])
        _integer(row["implementation_order"])

        metrics = _mapping(row["metrics"])
        transport = _mapping(row["transport"])
        assert set(metrics) == METRIC_KEYS
        assert set(transport) == TRANSPORT_KEYS
        for key, value in metrics.items():
            if key in {"startup_to_ready_ns", "startup_to_ready_cpu_ns"} and value is None:
                continue
            assert _integer(value) >= 0
        for value in transport.values():
            if value is not None:
                assert _integer(value) >= 0

    for mode in ("fresh-process", "steady-process"):
        for block in range(3):
            block_rows = [
                row
                for row in samples
                if row["process_mode"] == mode and row["paired_block"] == block
            ]
            assert {row["implementation_order"] for row in block_rows} == {0, 1}

    contract = _mapping(evidence["contract"])
    assert {row["contract_sha256"] for row in samples} == {CONTRACT_SHA256}
    assert contract["all_12_sample_digests_equal"] is True
    assert contract["native_and_py_horned_equal"] is True
    assert contract["lane_mismatch_observed"] is False
    assert contract["full_validation_inside_every_timed_envelope"] is True


def test_paired_medians_confidence_intervals_and_gates_recompute() -> None:
    evidence = _load()
    samples = [_mapping(value) for value in _sequence(evidence["raw_samples"])]
    rows = {
        (cast(str, row["process_mode"]), cast(str, row["lane"]), _integer(row["paired_block"])): row
        for row in samples
    }
    comparisons = [_mapping(value) for value in _sequence(evidence["paired_comparisons"])]

    assert {row["id"] for row in comparisons} == {
        "fresh-process/raw-engine-load",
        "fresh-process/common-contract-ready-wall",
        "fresh-process/incremental-peak-rss",
        "steady-process/raw-engine-load",
        "steady-process/common-contract-ready-wall",
        "steady-process/incremental-peak-rss",
    }

    for comparison in comparisons:
        mode = cast(str, comparison["process_mode"])
        numerator_selector = cast(str, comparison["numerator_selector"])
        denominator_selector = cast(str, comparison["denominator_selector"])
        pairs = [
            (
                _selected_integer(rows[(mode, NATIVE_LANE, block)], numerator_selector),
                _selected_integer(rows[(mode, COMPARATOR_LANE, block)], denominator_selector),
            )
            for block in range(3)
        ]
        stored_pairs = [_mapping(value) for value in _sequence(comparison["pairs"])]

        assert [
            (
                _integer(row["paired_block"]),
                _integer(row["numerator"]),
                _integer(row["denominator"]),
            )
            for row in stored_pairs
        ] == [
            (block, numerator, denominator) for block, (numerator, denominator) in enumerate(pairs)
        ]
        assert [row["ratio"] for row in stored_pairs] == pytest.approx(
            [numerator / denominator for numerator, denominator in pairs]
        )
        assert comparison["numerator_median"] == statistics.median(
            numerator for numerator, _ in pairs
        )
        assert comparison["denominator_median"] == statistics.median(
            denominator for _, denominator in pairs
        )
        assert comparison["paired_ratio_median"] == pytest.approx(
            statistics.median(numerator / denominator for numerator, denominator in pairs)
        )

        confidence = comparison["confidence_interval_95"]
        gate = comparison["gate"]
        if confidence is None:
            gate_value = _mapping(gate)
            assert gate_value["evaluable"] is False
            assert gate_value["passed"] is None
            continue

        confidence_value = _mapping(confidence)
        statistics_value = paired_bootstrap_ratio_summary(
            {CORPUS_ID: tuple(pairs)},
            seed=_integer(confidence_value["bootstrap_seed"]),
            resamples=_integer(confidence_value["resamples"]),
        )
        aggregate = _mapping(statistics_value["aggregate"])
        assert comparison["paired_ratio_median"] == pytest.approx(aggregate["estimate"])
        assert confidence_value["lower"] == pytest.approx(aggregate["lower_confidence_bound"])
        assert confidence_value["upper"] == pytest.approx(aggregate["upper_confidence_bound"])

        if gate is None:
            assert comparison["boundary"] == "raw-engine-load"
            assert confidence_value["classification"] == ("diagnostic-recomputed-not-harness-gate")
            assert comparison["paired_ratio_median"] > 1.0
            assert comparison["interpretation"] == "native-slower"
            continue

        gate_value = _mapping(gate)
        assert "classification" not in confidence_value
        assert gate_value["evaluable"] is True
        assert gate_value["statistic"] == "upper-confidence-bound"
        expected_pass = confidence_value["upper"] <= gate_value["threshold"]
        assert gate_value["passed"] is expected_pass


def test_interpretations_fail_closed_for_scope_and_invalid_steady_rss() -> None:
    evidence = _load()
    scope = _mapping(evidence["scope"])
    contract = _mapping(evidence["contract"])
    source_artifacts = _mapping(evidence["source_artifacts"])
    comparisons = {
        cast(str, row["id"]): row
        for row in (_mapping(value) for value in _sequence(evidence["paired_comparisons"]))
    }

    assert scope["python_reference_included"] is False
    assert scope["larger_corpora_included"] is False
    assert scope["approved_reference_host"] is False
    assert scope["formal_release_claim"] is False
    assert contract["raw_reports_contract_valid"] is False
    assert contract["raw_reports_contract_valid_false_only_because"] == (
        "the user-scoped two-lane run omitted the Python common-contract reference"
    )
    assert all(
        _mapping(source_artifacts[mode])["raw_contract_valid"] is False
        and _mapping(source_artifacts[mode])["contract_valid_false_reason"]
        == "Python common-contract reference was not run"
        for mode in ("fresh", "steady")
    )

    for comparison_id in (
        "fresh-process/common-contract-ready-wall",
        "fresh-process/incremental-peak-rss",
        "steady-process/common-contract-ready-wall",
    ):
        assert _mapping(comparisons[comparison_id]["gate"])["passed"] is True

    steady_rss = comparisons["steady-process/incremental-peak-rss"]
    assert steady_rss["confidence_interval_95"] is None
    assert steady_rss["interpretation"] == "no-formal-decision"
    assert _mapping(steady_rss["gate"])["passed"] is None

    maximum_gap = _integer(
        _mapping(evidence["measurement_contract"])["steady_rss_maximum_accepted_sample_gap_ns"]
    )
    steady_samples = [
        _mapping(value)
        for value in _sequence(evidence["raw_samples"])
        if _mapping(value)["process_mode"] == "steady-process"
    ]
    assert all(
        _selected_integer(row, "transport.rss_interval_maximum_sample_gap_ns") > maximum_gap
        for row in steady_samples
    )
