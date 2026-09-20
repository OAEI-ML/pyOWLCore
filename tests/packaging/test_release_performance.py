from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from tools.packaging.release_performance import (
    REQUIRED_EVIDENCE,
    SCOPED_APPROVAL,
    main,
    validate_policy,
)


def _policy(tmp_path: Path) -> Path:
    evidence = {}
    for name in sorted(REQUIRED_EVIDENCE | {"validation.json"}):
        blob = f"recorded evidence: {name}\n".encode()
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(blob)
        evidence[name] = hashlib.sha256(blob).hexdigest()
    policy = tmp_path / "performance-policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema": 1,
                "version": "0.2.1",
                "approval": SCOPED_APPROVAL,
                "evidence": evidence,
            }
        )
    )
    return policy


def test_policy_preserves_every_verified_byte(tmp_path: Path) -> None:
    path = _policy(tmp_path)
    evidence, blobs = validate_policy(path, version="0.2.1")
    assert set(blobs) == {*evidence, "performance-policy.json"}
    for name, blob in blobs.items():
        assert blob == (tmp_path / name).read_bytes()


@pytest.mark.parametrize("version", ["0.2.0", "0.2.2", "0.3.0", "0.2.1rc1"])
def test_other_versions_require_full_performance_run(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError, match="full Native performance"):
        validate_policy(_policy(tmp_path), version=version)


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema", 2), ("schema", True), ("version", "0.2.2"), ("approval", "pending")],
)
def test_unapproved_or_wrong_policy_rejected(tmp_path: Path, field: str, value: object) -> None:
    path = _policy(tmp_path)
    policy = json.loads(path.read_text())
    policy[field] = value
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match=r"approved 0\.2\.1"):
        validate_policy(path, version="0.2.1")


def test_modified_validation_evidence_rejected(tmp_path: Path) -> None:
    path = _policy(tmp_path)
    (tmp_path / "validation.json").write_text("modified evidence")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_policy(path, version="0.2.1")


def test_approval_documents_alone_are_insufficient(tmp_path: Path) -> None:
    path = _policy(tmp_path)
    policy = json.loads(path.read_text())
    policy["evidence"] = {
        name: value
        for name, value in policy["evidence"].items()
        if name in {"benchmark-policy.md", "owner-release-authorization.md"}
    }
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="validation evidence"):
        validate_policy(path, version="0.2.1")


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "a/../outside", "a\\outside", "evidence.json"]
)
def test_evidence_cannot_escape_or_overwrite_receipt(tmp_path: Path, name: str) -> None:
    path = _policy(tmp_path)
    policy = json.loads(path.read_text())
    policy["evidence"][name] = "0" * 64
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="invalid evidence"):
        validate_policy(path, version="0.2.1")


def test_evidence_symlink_is_rejected(tmp_path: Path) -> None:
    path = _policy(tmp_path)
    evidence = tmp_path / "validation.json"
    original = tmp_path / "original.json"
    evidence.rename(original)
    try:
        evidence.symlink_to(original)
    except OSError:
        pytest.skip("creating symlinks is unavailable on this runner")
    with pytest.raises(ValueError, match="symlink"):
        validate_policy(path, version="0.2.1")


def test_cli_binds_candidate_and_preserves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = _policy(tmp_path)
    candidate = tmp_path / "release-report.json"
    candidate.write_text(json.dumps({"version": "0.2.1", "source_revision": "a" * 40}))
    output = tmp_path / "bundle" / "evidence.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_performance",
            "--policy",
            str(policy),
            "--version",
            "0.2.1",
            "--source-revision",
            "a" * 40,
            "--candidate-report",
            str(candidate),
            "--output",
            str(output),
        ],
    )
    main()
    receipt = json.loads(output.read_text())
    assert (
        receipt["candidate_release_report_sha256"]
        == hashlib.sha256(candidate.read_bytes()).hexdigest()
    )
    assert receipt["full_comparator_study"] == "not-executed-by-this-policy"
    assert receipt["approval"] == SCOPED_APPROVAL
    assert capsys.readouterr().out.strip() == hashlib.sha256(output.read_bytes()).hexdigest()
    for name in receipt["evidence"]:
        assert (output.parent / name).read_bytes() == (tmp_path / name).read_bytes()


def test_cli_rejects_candidate_from_another_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(tmp_path)
    candidate = tmp_path / "release-report.json"
    candidate.write_text(json.dumps({"version": "0.2.1", "source_revision": "b" * 40}))
    output = tmp_path / "bundle" / "evidence.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_performance",
            "--policy",
            str(policy),
            "--version",
            "0.2.1",
            "--source-revision",
            "a" * 40,
            "--candidate-report",
            str(candidate),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit):
        main()
    assert not output.exists()


@pytest.mark.parametrize(
    "missing",
    [
        "consumer-stack.json",
        "consumer-stack-tests.log",
        "native-optimization/completed-summary.json",
    ],
)
def test_unrelated_evidence_cannot_replace_required_validation(
    tmp_path: Path, missing: str
) -> None:
    path = _policy(tmp_path)
    policy = json.loads(path.read_text())
    del policy["evidence"][missing]
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="validation evidence"):
        validate_policy(path, version="0.2.1")
