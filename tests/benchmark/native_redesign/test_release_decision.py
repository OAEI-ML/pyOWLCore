from __future__ import annotations

import hashlib
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


def test_complete_exact_evidence_enables_both_decisions(tmp_path: Path) -> None:
    result = evaluate_release_decision(_evidence(tmp_path), evidence_root=tmp_path)

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


def test_unavailable_consumers_do_not_block_core_but_block_workspace_claim(
    tmp_path: Path,
) -> None:
    payload = _evidence(tmp_path)
    consumers = cast(list[dict[str, object]], payload["workspace_consumers"])
    projector = next(row for row in consumers if row["id"] == "projector")
    projector.update(
        status="not-run",
        revision=None,
        reason="candidate native compiler artifact is unavailable",
        evidence=[],
    )

    result = evaluate_release_decision(payload, evidence_root=tmp_path)
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
def test_incomplete_core_gate_blocks_both_decisions(status: str, tmp_path: Path) -> None:
    payload = _evidence(tmp_path)
    gates = cast(list[dict[str, object]], payload["core_gates"])
    gates[0].update(status=status, reason="required evidence is incomplete", evidence=[])

    result = evaluate_release_decision(payload, evidence_root=tmp_path)
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


def test_pass_requires_evidence_and_non_pass_requires_reason(tmp_path: Path) -> None:
    no_evidence = _evidence(tmp_path)
    cast(list[dict[str, object]], no_evidence["core_gates"])[0]["evidence"] = []
    with pytest.raises(ReleaseDecisionError, match="pass requires evidence"):
        evaluate_release_decision(no_evidence, evidence_root=tmp_path)

    no_reason = _evidence(tmp_path)
    row = cast(list[dict[str, object]], no_reason["workspace_consumers"])[0]
    row.update(status="not-run", revision=None, reason=None, evidence=[])
    with pytest.raises(ReleaseDecisionError, match="requires a nonempty reason"):
        evaluate_release_decision(no_reason, evidence_root=tmp_path)


def test_consumer_pass_or_fail_is_bound_to_an_exact_revision(tmp_path: Path) -> None:
    payload = _evidence(tmp_path)
    row = cast(list[dict[str, object]], payload["workspace_consumers"])[0]
    row["revision"] = None

    with pytest.raises(ReleaseDecisionError, match="pass requires an exact revision"):
        evaluate_release_decision(payload, evidence_root=tmp_path)


def test_pass_evidence_requires_a_root_and_matching_file_digest(tmp_path: Path) -> None:
    payload = _evidence(tmp_path)

    with pytest.raises(ReleaseDecisionError, match="requires an evidence root"):
        evaluate_release_decision(payload)

    evidence = cast(
        list[dict[str, str]],
        cast(list[dict[str, object]], payload["core_gates"])[0]["evidence"],
    )
    evidence[0]["sha256"] = "0" * 64
    with pytest.raises(ReleaseDecisionError, match="sha256 does not match"):
        evaluate_release_decision(payload, evidence_root=tmp_path)


def test_pass_evidence_must_exist_within_manifest_directory(tmp_path: Path) -> None:
    payload = _evidence(tmp_path)
    evidence = cast(
        list[dict[str, str]],
        cast(list[dict[str, object]], payload["core_gates"])[0]["evidence"],
    )
    evidence[0]["path"] = "../outside.json"

    with pytest.raises(ReleaseDecisionError, match="escapes the evidence root"):
        evaluate_release_decision(payload, evidence_root=tmp_path)

    evidence[0]["path"] = "evidence/missing.json"
    with pytest.raises(ReleaseDecisionError, match="cannot be loaded"):
        evaluate_release_decision(payload, evidence_root=tmp_path)


def test_pass_evidence_must_be_a_regular_non_symlink_file(tmp_path: Path) -> None:
    payload = _evidence(tmp_path)
    evidence = cast(
        list[dict[str, str]],
        cast(list[dict[str, object]], payload["core_gates"])[0]["evidence"],
    )
    path = tmp_path / evidence[0]["path"]
    target = path.with_name(f"captured-{path.name}")
    path.replace(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ReleaseDecisionError, match="regular non-symlink file"):
        evaluate_release_decision(payload, evidence_root=tmp_path)


def test_pass_evidence_changed_during_hashing_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _evidence(tmp_path)
    evidence = cast(
        list[dict[str, str]],
        cast(list[dict[str, object]], payload["core_gates"])[0]["evidence"],
    )
    path = tmp_path / evidence[0]["path"]
    original_lstat = Path.lstat
    inspections = 0

    def mutate_before_final_identity(selected: Path) -> Any:
        nonlocal inspections
        if selected == path:
            inspections += 1
            if inspections == 2:
                selected.write_bytes(b"changed-after-read")
        return original_lstat(selected)

    monkeypatch.setattr(Path, "lstat", mutate_before_final_identity)

    with pytest.raises(ReleaseDecisionError, match="changed while reading"):
        evaluate_release_decision(payload, evidence_root=tmp_path)


def test_loader_rejects_a_symlinked_evidence_manifest(tmp_path: Path) -> None:
    target = tmp_path / "captured-manifest.json"
    target.write_text(json.dumps(_evidence(tmp_path)), encoding="utf-8")
    path = tmp_path / "evidence.json"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ReleaseDecisionError, match="regular non-symlink file"):
        load_release_evidence(path)


def test_legacy_schema_cannot_claim_a_pass(tmp_path: Path) -> None:
    payload = _evidence(tmp_path)
    payload["schema"] = "pyowl-core.native-redesign-release-evidence/1"
    for row in cast(list[dict[str, object]], payload["core_gates"]):
        row["evidence"] = ["legacy-evidence.json"]
    for row in cast(list[dict[str, object]], payload["workspace_consumers"]):
        row["evidence"] = ["legacy-evidence.json"]

    with pytest.raises(ReleaseDecisionError, match="schema 1 cannot claim pass"):
        evaluate_release_decision(payload, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda value: value.update(extra=True), "unknown fields"),
        (
            lambda value: value.update(schema=[]),
            "unsupported release evidence schema",
        ),
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
    tmp_path: Path,
) -> None:
    payload = _evidence(tmp_path)
    mutation(payload)

    with pytest.raises(ReleaseDecisionError, match=message):
        evaluate_release_decision(payload, evidence_root=tmp_path)


def test_loader_and_cli_emit_deterministic_fail_closed_decision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _evidence(tmp_path)
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


def _evidence(root: Path) -> dict[str, object]:
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(exist_ok=True)

    def evidence(identifier: str) -> list[dict[str, str]]:
        path = evidence_dir / f"{identifier}.json"
        path.write_text(json.dumps({"id": identifier}), encoding="utf-8")
        return [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ]

    return {
        "schema": INPUT_SCHEMA,
        "core_revision": "a" * 40,
        "core_gates": [
            {
                "id": identifier,
                "status": "pass",
                "reason": None,
                "evidence": evidence(identifier),
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
                "evidence": evidence(identifier),
            }
            for identifier, role in REQUIRED_WORKSPACE_CONSUMERS.items()
        ],
    }
