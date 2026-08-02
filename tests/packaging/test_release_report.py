from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.packaging import release_report
from tools.packaging.artifact_inspector import InspectionResult
from tools.packaging.release_report import REQUIRED_RELEASE_GATES, ReleaseGate

REVISION = "a" * 40
VERSION = "0.2.0"


def _result(
    path: Path,
    variant: str,
    *,
    legal_sha256: str = "e" * 64,
    payload_sha256: str = "c" * 64,
) -> InspectionResult:
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
        non_native_payload_sha256=(None if variant == "sdist" else payload_sha256),
        legal_payload_sha256=legal_sha256,
    )


def _artifacts(tmp_path: Path, *, complete: bool = False) -> tuple[Path, ...]:
    names = (
        release_report.expected_artifact_filenames(VERSION)
        if complete
        else (
            f"pyowl_core-{VERSION}-py3-none-any.whl",
            f"pyowl_core-{VERSION}-cp310-cp310-platform.whl",
            f"pyowl_core-{VERSION}.tar.gz",
        )
    )
    paths = tuple(tmp_path / name for name in sorted(names))
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


def test_expected_artifact_matrix_covers_every_supported_target() -> None:
    filenames = release_report.expected_artifact_filenames(VERSION)

    assert len(filenames) == 27
    assert f"pyowl_core-{VERSION}-py3-none-any.whl" in filenames
    assert f"pyowl_core-{VERSION}.tar.gz" in filenames
    for python_tag in ("cp310", "cp311", "cp312", "cp313", "cp314"):
        for platform_tag in (
            "manylinux_2_28_x86_64",
            "manylinux_2_28_aarch64",
            "macosx_13_0_x86_64",
            "macosx_13_0_arm64",
            "win_amd64",
        ):
            assert f"pyowl_core-{VERSION}-{python_tag}-{python_tag}-{platform_tag}.whl" in filenames


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
    paths = _artifacts(tmp_path, complete=True)
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
    assert len(artifacts) == len(paths) == 27
    by_filename = {row["filename"]: row for row in artifacts}
    for path in paths:
        row = by_filename[path.name]
        expected_variant = (
            "sdist"
            if path.name.endswith(".tar.gz")
            else "pure"
            if "py3-none-any" in path.name
            else "native"
        )
        assert row["bytes"] == path.stat().st_size
        assert row["kind"] == ("sdist" if expected_variant == "sdist" else "wheel")
        assert row["variant"] == expected_variant
        assert row["inspection_ok"] is True
        assert row["legal_payload_sha256"] == "e" * 64


def test_release_report_rejects_missing_supported_native_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _artifacts(tmp_path, complete=True)
    missing = next(path for path in paths if "-cp314-cp314-win_amd64.whl" in path.name)
    missing.unlink()
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

    assert not report["release_ready"]
    blockers = report["blockers"]
    assert isinstance(blockers, list)
    assert f"artifact set is missing required artifact: {missing.name}" in blockers
    assert "artifact set must contain exactly 25 native wheels; found 24" in blockers


def test_release_report_rejects_unapproved_native_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _artifacts(tmp_path, complete=True)
    source = next(path for path in paths if "-cp314-cp314-win_amd64.whl" in path.name)
    unexpected = source.with_name(source.name.replace("cp314-cp314", "cp315-cp315"))
    source.rename(unexpected)
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

    assert not report["release_ready"]
    blockers = report["blockers"]
    assert isinstance(blockers, list)
    assert f"artifact set is missing required artifact: {source.name}" in blockers
    assert f"artifact set contains unexpected artifact: {unexpected.name}" in blockers


def test_release_report_rejects_native_python_payload_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _artifacts(tmp_path, complete=True)
    drifted = next(path for path in paths if "-cp314-cp314-win_amd64.whl" in path.name)

    def inspect(path: Path, **kwargs: object) -> InspectionResult:
        del kwargs
        if path.name.endswith(".tar.gz"):
            variant = "sdist"
        elif "py3-none-any" in path.name:
            variant = "pure"
        else:
            variant = "native"
        fingerprint = "d" * 64 if path == drifted else "c" * 64
        return _result(path, variant, payload_sha256=fingerprint)

    monkeypatch.setattr(release_report, "inspect_artifact", inspect)
    gates = {
        name: ReleaseGate(status="passed", evidence=f"evidence/{name}.json")
        for name in REQUIRED_RELEASE_GATES
    }

    report = release_report.build_release_report(
        tmp_path,
        source_revision=REVISION,
        gates=gates,
    )

    assert not report["release_ready"]
    blockers = report["blockers"]
    assert isinstance(blockers, list)
    assert f"artifact non-native payload differs from pure wheel: {drifted.name}" in blockers


def test_release_report_rejects_cross_artifact_legal_payload_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _artifacts(tmp_path, complete=True)
    drifted = next(path for path in paths if "-cp314-cp314-win_amd64.whl" in path.name)

    def inspect(path: Path, **kwargs: object) -> InspectionResult:
        del kwargs
        if path.name.endswith(".tar.gz"):
            variant = "sdist"
        elif "py3-none-any" in path.name:
            variant = "pure"
        else:
            variant = "native"
        fingerprint = "f" * 64 if path == drifted else "e" * 64
        return _result(path, variant, legal_sha256=fingerprint)

    monkeypatch.setattr(release_report, "inspect_artifact", inspect)
    gates = {
        name: ReleaseGate(status="passed", evidence=f"evidence/{name}.json")
        for name in REQUIRED_RELEASE_GATES
    }

    report = release_report.build_release_report(
        tmp_path,
        source_revision=REVISION,
        gates=gates,
    )

    assert not report["release_ready"]
    blockers = report["blockers"]
    assert isinstance(blockers, list)
    assert f"artifact legal payload differs across artifact set: {drifted.name}" in blockers


def test_release_report_rejects_symlinked_artifact(
    tmp_path: Path,
) -> None:
    paths = _artifacts(tmp_path, complete=True)
    artifact = paths[0]
    target = tmp_path.parent / f"{tmp_path.name}-{artifact.name}"
    target.write_bytes(artifact.read_bytes())
    artifact.unlink()
    try:
        artifact.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="regular non-symlink file"):
        release_report.build_release_report(
            tmp_path,
            source_revision=REVISION,
            gates={},
        )


def test_release_report_rejects_artifact_changed_during_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _artifacts(tmp_path, complete=True)
    changed = paths[0]

    def inspect(path: Path, **kwargs: object) -> InspectionResult:
        del kwargs
        if path == changed:
            path.write_bytes(b"changed-during-inspection")
        if path.name.endswith(".tar.gz"):
            variant = "sdist"
        elif "py3-none-any" in path.name:
            variant = "pure"
        else:
            variant = "native"
        return _result(path, variant)

    monkeypatch.setattr(release_report, "inspect_artifact", inspect)
    with pytest.raises(ValueError, match=f"artifact changed during inspection: {changed.name}"):
        release_report.build_release_report(
            tmp_path,
            source_revision=REVISION,
            gates={},
        )


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


def test_gate_file_is_strict_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "gates.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "gates": {
                    "legal_review": {
                        "status": "blocked",
                        "evidence": "approval has not been recorded",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert release_report.load_gate_file(path) == {
        "legal_review": ReleaseGate(status="blocked", evidence="approval has not been recorded")
    }

    path.write_text('{"schema": 1, "gates": {"invented": {}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown release gate"):
        release_report.load_gate_file(path)
