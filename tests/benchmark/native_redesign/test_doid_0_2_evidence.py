from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[3]
EVIDENCE = (
    ROOT / "reports" / "performance" / "native-redesign" / "doid-0.2.0-installed-vs-py-horned.json"
)

NATIVE_LANE = "pyowl-native-wheel-common"
COMPARATOR_LANE = "py-horned-common"
SOURCE_SHA256 = "76f41cce3616ad1a9ba6353f469e96bde7addba5d43e541651a3ab703f9ba2bc"
OPTIONS_SHA256 = "fdfc954b7b8f0253c8e90ee4542170f506ca069ac6bd93744ac0ceabf04f8d2f"
CONTRACT_SHA256 = "b2f861b0cf79b6b22a502441823d655cd7f6d76ff0ade3a248e75389d5793966"

SAMPLE_KEYS = {
    "process_mode",
    "lane",
    "paired_block",
    "implementation_order",
    "load_ns",
    "common_ready_wall_ns",
    "rss_increment_bytes",
    "rss_maximum_sample_gap_ns",
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


def _rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return [_mapping(value) for value in _sequence(evidence["samples"])]


def _mode_rows(evidence: dict[str, Any], *, mode: str, lane: str) -> list[dict[str, Any]]:
    return sorted(
        (row for row in _rows(evidence) if row["process_mode"] == mode and row["lane"] == lane),
        key=lambda row: _integer(row["paired_block"]),
    )


def test_doid_0_2_evidence_pins_the_installed_artifacts_and_contract() -> None:
    evidence = _load()

    assert evidence["schema"] == "pyowl-core/doid-installed-vs-py-horned-evidence/v2"
    assert set(evidence) == {
        "schema",
        "scope",
        "raw_artifact",
        "source_identity",
        "identities",
        "contract",
        "environment",
        "samples",
        "comparisons",
        "interpretations",
    }

    raw = _mapping(evidence["raw_artifact"])
    assert raw == {
        "filename": "doid-21f4e41-two-lane.json",
        "bytes": 86992371,
        "sha256": "949ae3ef40908ec66882e385613e2b21f03929e146528589955cdd29d7e4cd6d",
        "schema": "pyowl-core/comparator-baseline/v1",
        "captured_utc": "2026-08-02T06:25:09.078116+00:00",
        "contract_valid": False,
        "contract_valid_false_only_because": (
            "the user-scoped two-lane run omitted the Python common-contract reference"
        ),
        "execution_errors": [],
    }

    source = _mapping(evidence["source_identity"])
    assert source["git_commit"] == "21f4e418da005e260f6056dc9ce0ac4a8af4210d"
    assert source["git_tree"] == "d74e59c6ae8ae125fde5cf311505f4180f9f2b1e"
    assert source["git_dirty"] is False
    assert source["runtime_source_input_count"] == 202
    assert source["runtime_source_bytes"] == 5937251

    identities = _mapping(evidence["identities"])
    core = _mapping(identities["pyowl_core"])
    assert core["version"] == "0.2.0"
    assert core["api_version"] == [0, 2]
    assert core["model_schema"] == 2
    assert core["wire_format"] == [1, 2]
    assert _mapping(core["wheel"]) == {
        "artifact": "pyowl_core-0.2.0-cp312-cp312-macosx_14_0_x86_64.whl",
        "bytes": 1765045,
        "sha256": "f969d1fff70844660580b73915f65871d407d5d870e1af8c3498d23fd68a661e",
    }
    assert _mapping(core["extension"]) == {
        "artifact": "_native.cpython-312-darwin.so",
        "bytes": 3295168,
        "sha256": "226373ef1c8f83603ca1467515ea9c6502e982943777ca0b09cf8c1e506a8032",
    }

    py_horned = _mapping(identities["py_horned"])
    assert py_horned["package"] == "py-horned-owl"
    assert py_horned["version"] == "1.4.0"
    assert py_horned["runner_revision"] == "pyowl-core-py-horned-common-runner-v10"
    assert py_horned["pin_state"] == "complete"

    input_identity = _mapping(identities["input"])
    assert input_identity == {
        "bytes": 6687536,
        "format": "rdfxml",
        "sha256": SOURCE_SHA256,
    }

    contract = _mapping(evidence["contract"])
    assert contract["sha256"] == CONTRACT_SHA256
    assert contract["options_sha256"] == OPTIONS_SHA256
    assert contract["model_schema"] == 2
    assert contract["counts"] == {
        "axioms": 55687,
        "signature": 8516,
        "documents": 1,
        "ontology_annotations": 0,
        "extensions": 0,
        "diagnostics": 0,
        "origins": 55687,
    }
    assert contract["all_twelve_sample_digests_equal"] is True
    assert contract["native_and_py_horned_equal"] is True
    assert contract["full_validation_inside_every_timed_envelope"] is True

    hash_values = [
        raw["sha256"],
        source["runtime_source_sha256"],
        source["comparator_manifest_sha256"],
        source["corpus_manifest_sha256"],
        _mapping(core["wheel"])["sha256"],
        _mapping(core["extension"])["sha256"],
        py_horned["artifact_sha256"],
        py_horned["runner_sha256"],
        input_identity["sha256"],
        contract["sha256"],
        contract["options_sha256"],
        *_mapping(contract["fingerprints"]).values(),
    ]
    assert all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in hash_values
    )


def test_doid_0_2_samples_are_complete_paired_and_positive() -> None:
    evidence = _load()
    scope = _mapping(evidence["scope"])
    samples = _rows(evidence)

    assert scope["sample_count"] == len(samples) == 12
    assert scope["warmups"] == 1
    assert scope["repetitions_per_lane_and_mode"] == 3
    assert scope["schedule_seed"] == 180643
    assert Counter((row["process_mode"], row["lane"]) for row in samples) == {
        ("fresh-process", NATIVE_LANE): 3,
        ("fresh-process", COMPARATOR_LANE): 3,
        ("steady-process", NATIVE_LANE): 3,
        ("steady-process", COMPARATOR_LANE): 3,
    }

    for row in samples:
        assert set(row) == SAMPLE_KEYS
        assert _integer(row["paired_block"]) in range(3)
        assert _integer(row["implementation_order"]) in (0, 1)
        assert _integer(row["load_ns"]) > 0
        assert _integer(row["common_ready_wall_ns"]) > 0
        assert _integer(row["rss_increment_bytes"]) >= 0
        gap = row["rss_maximum_sample_gap_ns"]
        if row["process_mode"] == "fresh-process":
            assert gap is None
        else:
            assert _integer(gap) > 0

    for mode in ("fresh-process", "steady-process"):
        native = _mode_rows(evidence, mode=mode, lane=NATIVE_LANE)
        comparator = _mode_rows(evidence, mode=mode, lane=COMPARATOR_LANE)
        assert [row["paired_block"] for row in native] == [0, 1, 2]
        assert [row["paired_block"] for row in comparator] == [0, 1, 2]
        assert {
            (_integer(left["implementation_order"]), _integer(right["implementation_order"]))
            for left, right in zip(native, comparator, strict=True)
        } <= {(0, 1), (1, 0)}


@pytest.mark.parametrize(
    ("mode", "comparison_key", "sample_key"),
    [
        ("fresh-process", "raw_engine_load", "load_ns"),
        ("fresh-process", "common_contract_ready_wall", "common_ready_wall_ns"),
        ("steady-process", "raw_engine_load", "load_ns"),
        ("steady-process", "common_contract_ready_wall", "common_ready_wall_ns"),
    ],
)
def test_doid_0_2_wall_and_load_summaries_recompute(
    mode: str, comparison_key: str, sample_key: str
) -> None:
    evidence = _load()
    native = _mode_rows(evidence, mode=mode, lane=NATIVE_LANE)
    comparator = _mode_rows(evidence, mode=mode, lane=COMPARATOR_LANE)
    values = [
        (_integer(left[sample_key]), _integer(right[sample_key]))
        for left, right in zip(native, comparator, strict=True)
    ]
    comparison_mode = mode.replace("-process", "_process")
    comparison = _mapping(
        _mapping(_mapping(evidence["comparisons"])[comparison_mode])[comparison_key]
    )

    native_median = statistics.median(left for left, _ in values)
    comparator_median = statistics.median(right for _, right in values)
    paired_ratio_median = statistics.median(left / right for left, right in values)
    assert comparison["native_median_ns"] == native_median
    assert comparison["py_horned_median_ns"] == comparator_median
    assert comparison["paired_ratio_median"] == pytest.approx(paired_ratio_median)

    if comparison_key == "raw_engine_load":
        assert comparison["interpretation"] == "native-slower"
        assert paired_ratio_median > 1.0
    else:
        assert comparison["passed"] is True
        assert (
            _sequence(comparison["confidence_interval_95"])[1]
            <= comparison["upper_bound_threshold"]
        )
        assert comparison["py_horned_over_native"] == pytest.approx(1.0 / paired_ratio_median)


def test_doid_0_2_rss_and_claim_scope_fail_closed() -> None:
    evidence = _load()
    scope = _mapping(evidence["scope"])
    comparisons = _mapping(evidence["comparisons"])
    fresh = _mapping(comparisons["fresh_process"])
    steady = _mapping(comparisons["steady_process"])

    fresh_native = _mode_rows(evidence, mode="fresh-process", lane=NATIVE_LANE)
    fresh_comparator = _mode_rows(evidence, mode="fresh-process", lane=COMPARATOR_LANE)
    fresh_ratios = [
        _integer(left["rss_increment_bytes"]) / _integer(right["rss_increment_bytes"])
        for left, right in zip(fresh_native, fresh_comparator, strict=True)
    ]
    fresh_rss = _mapping(fresh["incremental_peak_rss"])
    assert fresh_rss["paired_ratio_median"] == pytest.approx(statistics.median(fresh_ratios))
    assert fresh_rss["passed"] is True
    assert _sequence(fresh_rss["confidence_interval_95"])[1] <= fresh_rss["upper_bound_threshold"]

    steady_rss = _mapping(steady["incremental_peak_rss"])
    assert steady_rss["evaluable"] is False
    maximum_gap = _integer(steady_rss["maximum_accepted_sample_gap_ns"])
    steady_samples = [row for row in _rows(evidence) if row["process_mode"] == "steady-process"]
    assert any(_integer(row["rss_maximum_sample_gap_ns"]) > maximum_gap for row in steady_samples)

    assert scope["python_reference_included"] is False
    assert scope["formal_release_claim"] is False
    assert _mapping(evidence["raw_artifact"])["contract_valid"] is False
    environment = _mapping(evidence["environment"])
    assert environment["host_class"] == "shared concurrent development host"
    assert environment["reference_machine_approval"] == "pending"
    assert environment["reference_machine_matches"] is False
    interpretations = _mapping(evidence["interpretations"])
    assert "no portable or formal release-performance claim" in interpretations["portability"]
    assert "py-horned reaches its engine model faster" in interpretations["raw_parse_qualification"]
