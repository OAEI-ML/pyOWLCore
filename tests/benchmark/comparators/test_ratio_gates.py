from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest

import tools.benchmark.comparators.runner as runner_module
from tools.benchmark.comparators.manifest import COMMON_BOUNDARY
from tools.benchmark.manifest import Corpus, load_manifest

_LANES = (
    "pyowl-direct-rust-common",
    "horned-owl-common",
    "pyowl-native-wheel-common",
    "py-horned-common",
)


def test_required_ratio_gates_pass_constant_paired_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_contract_key_and_resamples(monkeypatch)
    corpora = _required_corpora()
    rows = _ratio_rows(corpora, repetitions=5)

    gates = runner_module._evaluate_ratio_gates(
        corpora=corpora,
        rows=rows,
        repetitions=5,
        seed=7,
    )

    assert gates["configured"] is True
    assert gates["passed"] is True
    assert gates["raw_horned_equivalence_denominator_allowed"] is False
    assert gates["excluded_equivalence_denominator_lanes"] == ["horned-owl-raw"]
    assert {value["denominator_lane"] for value in gates["comparisons"]} == {
        "horned-owl-common",
        "py-horned-common",
    }
    assert all(value["passed"] is True for value in gates["comparisons"])
    assert all(
        value["passed"] is True
        for value in gates["installed_wheel_call_to_ready_overhead"]
    )
    wheel_comparison = next(
        value
        for value in gates["comparisons"]
        if value["id"]
        == "installed-wheel-vs-py-horned-common/steady-process/resident-bytes"
    )
    wall = cast(dict[str, Any], wheel_comparison["metrics"])["wall"]
    assert wall["aggregate_value"] == pytest.approx(1.09)
    assert wall["aggregate_threshold"] == 1.10
    assert wall["large_corpus_threshold"] == 1.25
    fresh_direct = next(
        value
        for value in gates["comparisons"]
        if value["id"] == "direct-rust-vs-horned-common/fresh-process/resident-bytes"
    )
    fresh_wall = cast(dict[str, Any], fresh_direct["metrics"])["wall"]
    assert fresh_wall["metric"] == "startup-to-ready wall_ns"
    assert fresh_wall["sample_sources"] == {
        "numerator": "transport_metrics.parent_wall_ns",
        "denominator": "transport_metrics.parent_wall_ns",
    }
    fresh_wheel = next(
        value
        for value in gates["comparisons"]
        if value["id"]
        == "installed-wheel-vs-py-horned-common/fresh-process/resident-bytes"
    )
    assert cast(dict[str, Any], fresh_wheel["metrics"])["wall"]["sample_sources"] == {
        "numerator": "metrics.startup_to_ready_ns",
        "denominator": "transport_metrics.parent_wall_ns",
    }
    assert wall["metric"] == "call-to-ready wall_ns"


def test_large_corpus_guardrail_cannot_be_averaged_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_contract_key_and_resamples(monkeypatch)
    corpora = _required_corpora()
    rows = _ratio_rows(corpora, repetitions=5)
    medium_id, large_id = (value.id for value in corpora)
    for row in rows:
        if row["lane"] != "pyowl-direct-rust-common":
            continue
        replacement = 50 if row["corpus_id"] == medium_id else 130
        for sample in row["samples"]:
            sample["metrics"]["wall_ns"] = replacement

    gates = runner_module._evaluate_ratio_gates(
        corpora=corpora,
        rows=rows,
        repetitions=5,
        seed=7,
    )
    direct = next(
        value
        for value in gates["comparisons"]
        if value["id"] == "direct-rust-vs-horned-common/steady-process/resident-bytes"
    )
    wall = cast(dict[str, Any], direct["metrics"])["wall"]

    assert wall["aggregate_passed"] is True
    assert wall["aggregate_value"] < 1.10
    assert wall["large_corpus_guardrails"] == [
        {
            "corpus_id": large_id,
            "median_ratio": 1.3,
            "threshold": 1.25,
            "passed": False,
        }
    ]
    assert wall["passed"] is False
    assert gates["passed"] is False


def test_fresh_gate_cannot_pass_from_fast_child_wall_when_parent_startup_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_contract_key_and_resamples(monkeypatch)
    corpora = _required_corpora()
    rows = _ratio_rows(corpora, repetitions=3)
    for row in rows:
        if row["process_mode"] != "fresh-process":
            continue
        if row["lane"] == "pyowl-direct-rust-common":
            for sample in row["samples"]:
                sample["metrics"]["wall_ns"] = 1
                sample["transport_metrics"]["parent_wall_ns"] = 200
        elif row["lane"] == "horned-owl-common":
            for sample in row["samples"]:
                sample["metrics"]["wall_ns"] = 100
                sample["transport_metrics"]["parent_wall_ns"] = 100
        elif row["lane"] == "pyowl-native-wheel-common":
            for sample in row["samples"]:
                sample["metrics"]["wall_ns"] = 1
                sample["metrics"]["startup_to_ready_ns"] = 200
        elif row["lane"] == "py-horned-common":
            for sample in row["samples"]:
                sample["metrics"]["wall_ns"] = 100
                sample["transport_metrics"]["parent_wall_ns"] = 100

    gates = runner_module._evaluate_ratio_gates(
        corpora=corpora,
        rows=rows,
        repetitions=3,
        seed=7,
    )
    fresh = next(
        value
        for value in gates["comparisons"]
        if value["id"] == "direct-rust-vs-horned-common/fresh-process/resident-bytes"
    )
    steady = next(
        value
        for value in gates["comparisons"]
        if value["id"] == "direct-rust-vs-horned-common/steady-process/resident-bytes"
    )
    fresh_wheel = next(
        value
        for value in gates["comparisons"]
        if value["id"]
        == "installed-wheel-vs-py-horned-common/fresh-process/resident-bytes"
    )
    fresh_wall = cast(dict[str, Any], fresh["metrics"])["wall"]
    steady_wall = cast(dict[str, Any], steady["metrics"])["wall"]
    fresh_wheel_wall = cast(dict[str, Any], fresh_wheel["metrics"])["wall"]

    assert fresh_wall["metric_selector"] == "startup-to-ready-wall"
    assert fresh_wall["aggregate_value"] == pytest.approx(2.0)
    assert fresh_wall["passed"] is False
    assert fresh_wheel_wall["aggregate_value"] == pytest.approx(2.0)
    assert fresh_wheel_wall["passed"] is False
    assert steady_wall["metric_selector"] == "call-to-ready-wall"
    assert steady_wall["aggregate_value"] == pytest.approx(1.0)
    assert steady_wall["passed"] is True


def test_nonpositive_metric_and_missing_common_denominator_fail_with_scenario_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_contract_key_and_resamples(monkeypatch)
    corpora = _required_corpora()
    rows = _ratio_rows(corpora, repetitions=2)
    rows = [
        row
        for row in rows
        if not (
            row["lane"] == "horned-owl-common"
            and row["process_mode"] == "fresh-process"
        )
    ]
    raw_rows = _ratio_rows(corpora, repetitions=2, lanes=("horned-owl-raw",))
    rows.extend(raw_rows)
    invalid_row = next(
        row
        for row in rows
        if row["lane"] == "py-horned-common"
        and row["process_mode"] == "steady-process"
    )
    invalid_row["samples"][0]["metrics"]["rss_peak_increment_bytes"] = 0

    gates = runner_module._evaluate_ratio_gates(
        corpora=corpora,
        rows=rows,
        repetitions=2,
        seed=7,
    )

    assert gates["passed"] is False
    reasons = cast(list[dict[str, Any]], gates["reasons"])
    assert any(
        reason.get("lane") == "horned-owl-common"
        and reason.get("process_mode") == "fresh-process"
        and "missing" in cast(str, reason["reason"])
        for reason in reasons
    )
    assert any(
        reason.get("lane") == "py-horned-common"
        and reason.get("process_mode") == "steady-process"
        and "must be positive" in cast(str, reason["reason"])
        for reason in reasons
    )
    assert all(value["denominator_lane"] != "horned-owl-raw" for value in gates["comparisons"])


def _patch_contract_key_and_resamples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "DEFAULT_BOOTSTRAP_RESAMPLES", 200)
    monkeypatch.setattr(runner_module, "common_contract_equality_key", lambda _: ("same",))


def _required_corpora() -> tuple[Corpus, Corpus]:
    manifest = load_manifest()
    return (
        manifest.by_id("oaei-bioml-doid-2024"),
        manifest.by_id("oaei-bioml-ncit-2024"),
    )


def _ratio_rows(
    corpora: Sequence[Corpus],
    *,
    repetitions: int,
    lanes: Sequence[str] = _LANES,
) -> list[dict[str, Any]]:
    wall_value = {
        "pyowl-direct-rust-common": 100,
        "horned-owl-common": 100,
        "pyowl-native-wheel-common": 109,
        "py-horned-common": 100,
        "horned-owl-raw": 1,
    }
    rows: list[dict[str, Any]] = []
    block_size = len(lanes)
    for corpus in corpora:
        for process_mode in ("fresh-process", "steady-process"):
            for order_index, lane in enumerate(lanes):
                rows.append(
                    {
                        "lane": lane,
                        "boundary": (
                            "horned-model-ready"
                            if lane == "horned-owl-raw"
                            else COMMON_BOUNDARY
                        ),
                        "status": "ok",
                        "reason": None,
                        "corpus_id": corpus.id,
                        "input_mode": "resident-bytes",
                        "process_mode": process_mode,
                        "contract": {"same": True},
                        "samples": [
                            _sample(
                                block_index,
                                lane=lane,
                                process_mode=process_mode,
                                order_index=order_index,
                                block_size=block_size,
                                wall_ns=wall_value[lane],
                            )
                            for block_index in range(repetitions)
                        ],
                    }
                )
    return rows


def _sample(
    block_index: int,
    *,
    lane: str,
    process_mode: str,
    order_index: int,
    block_size: int,
    wall_ns: int,
) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "schedule_seed": 7,
        "paired_block": block_index,
        "implementation_order": order_index,
        "paired_block_size": block_size,
        "metrics": {
            "wall_ns": wall_ns,
            "rss_peak_increment_bytes": wall_ns,
        },
    }
    if process_mode == "fresh-process":
        if lane == "pyowl-native-wheel-common":
            sample["metrics"]["startup_to_ready_ns"] = wall_ns
        else:
            sample["transport_metrics"] = {"parent_wall_ns": wall_ns}
    return sample
