from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from tools.benchmark.native_redesign.release_decision import (
    DECISION_SCHEMA,
    INPUT_SCHEMA,
    REQUIRED_CORE_GATES,
    REQUIRED_WORKSPACE_CONSUMERS,
    ReleaseDecisionError,
    evaluate_release_decision,
    load_release_evidence,
    main,
)

ROOT = Path(__file__).parents[3]
CHECKPOINT = ROOT / "reports" / "performance" / "native-redesign" / "checkpoint-evidence.json"


def test_complete_exact_evidence_enables_both_decisions() -> None:
    result = evaluate_release_decision(_evidence())

    assert result["schema"] == DECISION_SCHEMA
    assert result["core_release_eligible"] is True
    assert result["workspace_optimization_complete"] is True
    assert result["claims"] == {
        "core_release_allowed": True,
        "multi_consumer_native_performance_allowed": True,
    }
    assert result["core"] == {
        "status": "pass",
        "required_gate_count": len(REQUIRED_CORE_GATES),
        "passed_gate_count": len(REQUIRED_CORE_GATES),
        "blockers": [],
    }
    assert cast(dict[str, object], result["workspace"])["not_run_consumers"] == []


def test_unavailable_consumers_do_not_block_core_but_block_workspace_claim() -> None:
    payload = _evidence()
    consumers = cast(list[dict[str, object]], payload["workspace_consumers"])
    projector = next(row for row in consumers if row["id"] == "projector")
    projector.update(
        status="not-run",
        revision=None,
        reason="candidate native compiler artifact is unavailable",
        evidence=[],
    )

    result = evaluate_release_decision(payload)
    workspace = cast(dict[str, object], result["workspace"])

    assert result["core_release_eligible"] is True
    assert result["workspace_optimization_complete"] is False
    assert workspace["not_run_consumers"] == ["projector"]
    assert workspace["blockers"] == [
        {
            "id": "projector",
            "status": "not-run",
            "reason": "candidate native compiler artifact is unavailable",
            "revision": None,
        }
    ]


@pytest.mark.parametrize("status", ("fail", "not-run"))
def test_incomplete_core_gate_blocks_both_decisions(status: str) -> None:
    payload = _evidence()
    gates = cast(list[dict[str, object]], payload["core_gates"])
    gates[0].update(status=status, reason="required evidence is incomplete", evidence=[])

    result = evaluate_release_decision(payload)
    core = cast(dict[str, object], result["core"])
    workspace = cast(dict[str, object], result["workspace"])

    assert result["core_release_eligible"] is False
    assert result["workspace_optimization_complete"] is False
    assert core["passed_gate_count"] == len(REQUIRED_CORE_GATES) - 1
    assert cast(list[dict[str, object]], workspace["blockers"])[0] == {
        "id": "core-release-eligibility",
        "status": "fail",
        "reason": "workspace completion requires core_release_eligible=true",
    }


def test_pass_requires_evidence_and_non_pass_requires_reason() -> None:
    no_evidence = _evidence()
    cast(list[dict[str, object]], no_evidence["core_gates"])[0]["evidence"] = []
    with pytest.raises(ReleaseDecisionError, match="pass requires evidence"):
        evaluate_release_decision(no_evidence)

    no_reason = _evidence()
    row = cast(list[dict[str, object]], no_reason["workspace_consumers"])[0]
    row.update(status="not-run", revision=None, reason=None, evidence=[])
    with pytest.raises(ReleaseDecisionError, match="requires a nonempty reason"):
        evaluate_release_decision(no_reason)


def test_consumer_pass_or_fail_is_bound_to_an_exact_revision() -> None:
    payload = _evidence()
    row = cast(list[dict[str, object]], payload["workspace_consumers"])[0]
    row["revision"] = None

    with pytest.raises(ReleaseDecisionError, match="pass requires an exact revision"):
        evaluate_release_decision(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda value: value.update(extra=True), "unknown fields"),
        (
            lambda value: cast(list[dict[str, object]], value["core_gates"]).pop(),
            "missing required ids",
        ),
        (
            lambda value: cast(list[dict[str, object]], value["core_gates"]).append(
                dict(cast(list[dict[str, object]], value["core_gates"])[0])
            ),
            "duplicate id",
        ),
        (
            lambda value: cast(list[dict[str, object]], value["workspace_consumers"])[0].update(
                role="encoded-native-compiler"
            ),
            "role must be",
        ),
    ),
)
def test_evidence_shape_cannot_weaken_normative_gate_set(
    mutation: Any,
    message: str,
) -> None:
    payload = _evidence()
    mutation(payload)

    with pytest.raises(ReleaseDecisionError, match=message):
        evaluate_release_decision(payload)


def test_loader_and_cli_emit_deterministic_fail_closed_decision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _evidence()
    row = cast(list[dict[str, object]], payload["core_gates"])[0]
    row.update(status="not-run", reason="reference-machine evidence unavailable", evidence=[])
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    expected = load_release_evidence(path)
    assert main([str(path)]) == 1
    observed = json.loads(capsys.readouterr().out)

    assert observed == expected
    assert observed["core_release_eligible"] is False


def test_checked_in_checkpoint_is_truthfully_fail_closed() -> None:
    result = load_release_evidence(CHECKPOINT)
    core = cast(dict[str, object], result["core"])
    workspace = cast(dict[str, object], result["workspace"])

    assert result["core_release_eligible"] is False
    assert result["workspace_optimization_complete"] is False
    assert core["passed_gate_count"] == 0
    assert workspace["passed_consumer_count"] == 0
    assert workspace["not_run_consumers"] == sorted(REQUIRED_WORKSPACE_CONSUMERS)


def _evidence() -> dict[str, object]:
    return {
        "schema": INPUT_SCHEMA,
        "core_revision": "a" * 40,
        "core_gates": [
            {
                "id": identifier,
                "status": "pass",
                "reason": None,
                "evidence": [f"reports/evidence/{identifier}.json"],
            }
            for identifier in REQUIRED_CORE_GATES
        ],
        "workspace_consumers": [
            {
                "id": identifier,
                "role": role,
                "status": "pass",
                "revision": "b" * 40,
                "reason": None,
                "evidence": [f"reports/evidence/{identifier}.json"],
            }
            for identifier, role in REQUIRED_WORKSPACE_CONSUMERS.items()
        ],
    }
