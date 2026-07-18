from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tools.packaging import release_report
from tools.packaging.artifact_inspector import InspectionResult
from tools.packaging.release_report import REQUIRED_RELEASE_GATES, ReleaseGate

REVISION = "a" * 40


def _result(path: Path, variant: str) -> InspectionResult:
    return InspectionResult(
        path=str(path),
        kind="sdist" if variant == "sdist" else "wheel",
        variant=variant,  # type: ignore[arg-type]
        member_count=1,
        uncompressed_bytes=1,
        metadata={},
        errors=(),
        release_blockers=(),
        deferred_platform_checks=(
            ("target platform audit required",) if variant == "native" else ()
        ),
    )


def _artifacts(tmp_path: Path) -> tuple[Path, ...]:
    paths = (
        tmp_path / "pyowl_core-0.1.0.dev0-py3-none-any.whl",
        tmp_path / "pyowl_core-0.1.0.dev0-cp310-cp310-platform.whl",
        tmp_path / "pyowl_core-0.1.0.dev0.tar.gz",
    )
    for index, path in enumerate(paths):
        path.write_bytes(f"artifact-{index}".encode())
    return paths


def _patch_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    def inspect(path: Path, **kwargs: object) -> InspectionResult:
        del kwargs
        if path.name.endswith(".tar.gz"):
            variant = "sdist"
        elif "py3-none-any" in path.name:
            variant = "pure"
        else:
            variant = "native"
        return _result(path, variant)

    monkeypatch.setattr(release_report, "inspect_artifact", inspect)


def test_incomplete_candidate_records_every_missing_external_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _artifacts(tmp_path)
    _patch_inspection(monkeypatch)
    report = release_report.build_release_report(
        tmp_path,
        source_revision=REVISION,
        gates={},
    )
    assert not report["release_ready"]
    blockers = report["blockers"]
    assert isinstance(blockers, list)
    for gate in REQUIRED_RELEASE_GATES:
        assert f"release gate has no evidence: {gate}" in blockers


def test_all_evidenced_gates_can_close_a_complete_artifact_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _artifacts(tmp_path)
    _patch_inspection(monkeypatch)
    gates = {
        name: ReleaseGate(status="passed", evidence=f"evidence/{name}.json")
        for name in REQUIRED_RELEASE_GATES
    }
    report = release_report.build_release_report(
        tmp_path,
        source_revision=REVISION,
        gates=gates,
    )
    assert report["release_ready"]
    assert report["blockers"] == []
    artifacts = report["artifacts"]
    assert isinstance(artifacts, list)
    expected_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert {row["filename"]: row["sha256"] for row in artifacts} == expected_hashes


@pytest.mark.parametrize("revision", ["main", "A" * 40, "a" * 39, "a" * 41])
def test_source_revision_must_be_an_exact_commit(
    tmp_path: Path,
    revision: str,
) -> None:
    with pytest.raises(ValueError, match="full lowercase"):
        release_report.build_release_report(
            tmp_path,
            source_revision=revision,
            gates={},
        )


def test_gate_parser_requires_known_name_status_and_evidence() -> None:
    name, gate = release_report.parse_gate("legal_review=blocked:owner approval pending")
    assert name == "legal_review"
    assert gate == ReleaseGate(status="blocked", evidence="owner approval pending")
    with pytest.raises(Exception, match="unknown release gate"):
        release_report.parse_gate("unknown=passed:evidence")
    with pytest.raises(Exception, match="evidence"):
        release_report.parse_gate("legal_review=passed:")
