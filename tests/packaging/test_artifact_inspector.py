from __future__ import annotations

import base64
import csv
import hashlib
import io
import tarfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from tools.packaging import artifact_inspector
from tools.packaging.artifact_inspector import inspect_artifact

_METADATA = b"""Metadata-Version: 2.4
Name: pyowl-core
Version: 0.1.0.dev0
Requires-Python: >=3.10
License-Expression: Apache-2.0
Provides-Extra: dev
Requires-Dist: pytest>=8; extra == "dev"

fixture
"""
_PYPROJECT = b"""[project]
name = "pyowl-core"
version = "0.1.0.dev0"
requires-python = ">=3.10"
license = "Apache-2.0"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]
"""
_LICENSE_FILES = {
    "LICENSE": b"Apache License 2.0",
    "NOTICE": b"pyowl-core",
    "LLVM-exception.txt": b"LLVM exception",
    "Unicode-3.0.txt": b"Unicode License v3",
    "inventory.toml": b"schema = 1",
}


def _record(
    entries: dict[str, bytes],
    record_name: str,
    *,
    duplicate_member: str | None = None,
    malformed_row: bool = False,
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, payload in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        row = (name, "sha256=" + digest.decode("ascii"), len(payload))
        writer.writerow(row)
        if name == duplicate_member:
            writer.writerow(row)
    if malformed_row:
        writer.writerow(("unexpected", "extra"))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _wheel(
    tmp_path: Path,
    variant: str = "pure",
    *,
    dist_info: str = "pyowl_core-0.1.0.dev0.dist-info",
    extra_binary: str | None = None,
    extra_entries: Mapping[str, bytes] | None = None,
    internal_tag: str | None = None,
    malformed_record_row: bool = False,
    metadata: bytes = _METADATA,
    record_duplicate: str | None = None,
    root_is_purelib: str | None = None,
    wheel_version: str = "1.0",
) -> Path:
    tag = "py3-none-any" if variant == "pure" else "cp310-cp310-manylinux_2_28_x86_64"
    purelib = (
        ("true" if variant == "pure" else "false") if root_is_purelib is None else root_is_purelib
    )
    entries = {
        "pyowl_core/__init__.py": b'__version__ = "0.1.0.dev0"\n',
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": (
            f"Wheel-Version: {wheel_version}\n"
            "Generator: pyowl-core-test-fixture\n"
            f"Root-Is-Purelib: {purelib}\n"
            f"Tag: {internal_tag or tag}\n"
        ).encode(),
    }
    for name, payload in _LICENSE_FILES.items():
        entries[f"{dist_info}/licenses/THIRD_PARTY_LICENSES/{name}"] = payload
    if variant == "native":
        entries["pyowl_core/_native.cpython-310-x86_64-linux-gnu.so"] = b"native-fixture"
    if extra_binary is not None:
        entries[extra_binary] = b"unapproved-native-fixture"
    if extra_entries is not None:
        entries.update(extra_entries)
    record_name = f"{dist_info}/RECORD"
    entries[record_name] = _record(
        entries,
        record_name,
        duplicate_member=record_duplicate,
        malformed_row=malformed_record_row,
    )
    path = tmp_path / f"pyowl_core-0.1.0.dev0-{tag}.whl"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if name.endswith(".so") else 0o100644) << 16
            archive.writestr(info, payload)
    return path


def _sdist(
    tmp_path: Path,
    *,
    duplicate_member: str | None = None,
    metadata: bytes = _METADATA,
    pyproject: bytes = _PYPROJECT,
    root: str = "pyowl_core-0.1.0.dev0",
) -> Path:
    entries = {
        f"{root}/PKG-INFO": metadata,
        f"{root}/pyproject.toml": pyproject,
        f"{root}/setup.py": b"from setuptools import setup\nsetup()\n",
        f"{root}/src/pyowl_core/__init__.py": b"",
        f"{root}/native/Cargo.lock": b"version = 4\n",
        f"{root}/native/Cargo.toml": b"[package]\n",
        f"{root}/native/src/lib.rs": b"",
    }
    for name, payload in _LICENSE_FILES.items():
        entries[f"{root}/THIRD_PARTY_LICENSES/{name}"] = payload
    path = tmp_path / "pyowl_core-0.1.0.dev0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 1_735_689_600
            archive.addfile(info, io.BytesIO(payload))
        if duplicate_member is not None:
            name = f"{root}/{duplicate_member}"
            payload = b"ambiguous replacement"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 1_735_689_600
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_pure_wheel_structure_metadata_and_record_are_verified(tmp_path: Path) -> None:
    result = inspect_artifact(_wheel(tmp_path), expected_variant="pure")
    assert result.ok
    assert result.variant == "pure"
    assert result.deferred_platform_checks == ()
    assert result.release_blockers == (
        "metadata: approved repository/docs/issues URLs are not configured",
    )


def test_native_wheel_is_not_mislabeled_as_universal_or_release_ready(tmp_path: Path) -> None:
    result = inspect_artifact(_wheel(tmp_path, "native"), expected_variant="native")
    assert result.ok
    assert result.variant == "native"
    assert result.deferred_platform_checks == (
        "native dynamic dependencies/rpaths/symbols require the target-platform audit job",
    )
    assert not result.release_ready


def test_wheel_payload_fingerprint_excludes_only_native_extension(
    tmp_path: Path,
) -> None:
    pure = inspect_artifact(_wheel(tmp_path))
    native = inspect_artifact(_wheel(tmp_path, "native"))
    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    drifted = inspect_artifact(
        _wheel(
            drift_root,
            "native",
            extra_entries={"pyowl_core/__init__.py": b"platform-specific drift\n"},
        )
    )

    assert pure.non_native_payload_sha256 is not None
    assert native.non_native_payload_sha256 == pure.non_native_payload_sha256
    assert drifted.non_native_payload_sha256 != pure.non_native_payload_sha256


def test_legal_payload_fingerprint_matches_sdist_and_detects_tampering(
    tmp_path: Path,
) -> None:
    wheel = inspect_artifact(_wheel(tmp_path))
    sdist_root = tmp_path / "sdist"
    sdist_root.mkdir()
    sdist = inspect_artifact(_sdist(sdist_root))
    tampered_root = tmp_path / "tampered"
    tampered_root.mkdir()
    tampered = inspect_artifact(
        _wheel(
            tampered_root,
            extra_entries={
                "pyowl_core-0.1.0.dev0.dist-info/"
                "licenses/THIRD_PARTY_LICENSES/NOTICE": b"tampered notice"
            },
        )
    )

    assert wheel.legal_payload_sha256 is not None
    assert sdist.legal_payload_sha256 == wheel.legal_payload_sha256
    assert tampered.legal_payload_sha256 != wheel.legal_payload_sha256


def test_native_wheel_internal_tag_must_match_filename(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path,
        "native",
        internal_tag="cp310-cp310-manylinux_2_17_x86_64",
    )

    result = inspect_artifact(wheel, expected_variant="native")

    assert not result.ok
    assert (
        "wheel: native WHEEL tags ['cp310-cp310-manylinux_2_17_x86_64'] "
        "do not match filename tag 'cp310-cp310-manylinux_2_28_x86_64'" in result.errors
    )


@pytest.mark.parametrize(
    ("variant", "declared", "expected"),
    (
        ("pure", "false", "true"),
        ("native", "true", "false"),
    ),
)
def test_wheel_purelib_claim_must_match_contents(
    tmp_path: Path,
    variant: str,
    declared: str,
    expected: str,
) -> None:
    result = inspect_artifact(
        _wheel(tmp_path, variant, root_is_purelib=declared),
        expected_variant=variant,  # type: ignore[arg-type]
    )

    assert not result.ok
    assert (
        f"wheel: Root-Is-Purelib must be exactly {expected!r} for {variant}, "
        f"got [{declared!r}]" in result.errors
    )


def test_wheel_version_must_be_exactly_supported(tmp_path: Path) -> None:
    result = inspect_artifact(_wheel(tmp_path, wheel_version="2.0"))

    assert not result.ok
    assert "wheel: Wheel-Version must be exactly '1.0', got ['2.0']" in result.errors


def test_pure_wheel_rejects_native_binary_outside_package(tmp_path: Path) -> None:
    result = inspect_artifact(
        _wheel(tmp_path, extra_binary="payload/vendor.so"),
        expected_variant="pure",
    )

    assert not result.ok
    assert "wheel: pure artifact contains native binaries: payload/vendor.so" in result.errors


def test_wheel_rejects_duplicate_required_license_basename(tmp_path: Path) -> None:
    result = inspect_artifact(
        _wheel(tmp_path, extra_entries={"pyowl_core/NOTICE": b"ambiguous notice"})
    )

    assert not result.ok
    assert "license: duplicate required files in wheel: NOTICE" in result.errors


def test_native_wheel_rejects_additional_binary_outside_package(tmp_path: Path) -> None:
    result = inspect_artifact(
        _wheel(tmp_path, "native", extra_binary="payload/vendor.dll"),
        expected_variant="native",
    )

    assert not result.ok
    assert (
        "wheel: native artifact must contain exactly one pyowl_core/_native extension"
        in result.errors
    )


def test_wheel_metadata_root_must_match_project_identity(tmp_path: Path) -> None:
    result = inspect_artifact(_wheel(tmp_path, dist_info="foreign-0.1.0.dev0.dist-info"))

    assert not result.ok
    assert "wheel: dist-info root does not exactly match project identity" in result.errors
    assert any(error.startswith("wheel: missing identity member(s):") for error in result.errors)


def test_metadata_rejects_optional_marker_with_runtime_escape(tmp_path: Path) -> None:
    metadata = _METADATA.replace(
        b'Requires-Dist: pytest>=8; extra == "dev"',
        b'Requires-Dist: requests; extra == "dev" or python_version >= "3.10"',
    )

    result = inspect_artifact(_wheel(tmp_path, metadata=metadata))

    assert not result.ok
    assert any(
        error.startswith("metadata: unexpected runtime dependency requests;")
        for error in result.errors
    )


def test_metadata_accepts_conditional_extra_only_dependency(tmp_path: Path) -> None:
    metadata = _METADATA.replace(
        b'Requires-Dist: pytest>=8; extra == "dev"',
        b'Requires-Dist: tomli>=2; python_version < "3.11" and extra == "dev"',
    )

    result = inspect_artifact(_wheel(tmp_path, metadata=metadata))

    assert result.ok


def test_sdist_contains_complete_sources_without_binaries(tmp_path: Path) -> None:
    result = inspect_artifact(_sdist(tmp_path), expected_variant="sdist")
    assert result.ok
    assert result.variant == "sdist"


@pytest.mark.parametrize(
    ("pyproject", "expected_error"),
    [
        (
            _PYPROJECT.replace(b'version = "0.1.0.dev0"', b'version = "9.9.9"'),
            "sdist: pyproject [project].version does not match PKG-INFO Version",
        ),
        (
            _PYPROJECT.replace(b'license = "Apache-2.0"', b'license = "MIT"'),
            "sdist: pyproject [project].license does not match PKG-INFO License-Expression",
        ),
        (
            _PYPROJECT.replace(b"dependencies = []", b'dependencies = ["requests"]'),
            "sdist: pyproject dependency declarations do not match PKG-INFO requirements",
        ),
        (
            _PYPROJECT.replace(b'dev = ["pytest>=8"]', b'dev = ["requests"]'),
            "sdist: pyproject dependency declarations do not match PKG-INFO requirements",
        ),
    ],
)
def test_sdist_rejects_embedded_project_metadata_drift(
    tmp_path: Path,
    pyproject: bytes,
    expected_error: str,
) -> None:
    result = inspect_artifact(_sdist(tmp_path, pyproject=pyproject))

    assert not result.ok
    assert expected_error in result.errors


def test_sdist_root_must_match_project_identity(tmp_path: Path) -> None:
    result = inspect_artifact(
        _sdist(tmp_path, root="foreign-0.1.0.dev0"),
        expected_variant="sdist",
    )

    assert not result.ok
    assert "sdist: archive root does not exactly match project identity" in result.errors
    assert "sdist: missing required source PKG-INFO" in result.errors


def test_sdist_duplicate_member_blocks_before_ambiguous_payload_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _sdist(tmp_path, duplicate_member="PKG-INFO")
    monkeypatch.setattr(
        artifact_inspector._SdistReader,
        "read",
        lambda *args: pytest.fail("ambiguous archive payload was read"),
    )

    result = inspect_artifact(archive)

    assert not result.ok
    assert result.metadata == {}
    assert "archive: duplicate member name" in result.errors


def test_record_tampering_is_rejected(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        entries = [(entry, archive.read(entry)) for entry in archive.infolist()]
    with zipfile.ZipFile(wheel, "w") as archive:
        for entry, payload in entries:
            archive.writestr(
                entry,
                b"tampered" if entry.filename == "pyowl_core/__init__.py" else payload,
            )
    result = inspect_artifact(wheel)
    assert not result.ok
    assert any("RECORD digest mismatch" in error for error in result.errors)


def test_record_rejects_duplicate_member_rows(tmp_path: Path) -> None:
    result = inspect_artifact(_wheel(tmp_path, record_duplicate="pyowl_core/__init__.py"))

    assert not result.ok
    assert "wheel: RECORD contains duplicate member rows" in result.errors


def test_record_rejects_malformed_extra_rows(tmp_path: Path) -> None:
    result = inspect_artifact(_wheel(tmp_path, malformed_record_row=True))

    assert not result.ok
    assert "wheel: RECORD contains malformed rows" in result.errors


def test_java_artifact_and_unsafe_member_are_rejected(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../escape.jar", b"forbidden")
    result = inspect_artifact(wheel)
    assert not result.ok
    assert any("unsafe member path" in error for error in result.errors)
    assert any("forbidden artifact" in error for error in result.errors)


@pytest.mark.parametrize(
    "member",
    (
        "payload//value.txt",
        "payload/./value.txt",
        "payload\\value.txt",
        "C:/payload.txt",
    ),
)
def test_wheel_rejects_noncanonical_member_paths(
    tmp_path: Path,
    member: str,
) -> None:
    result = inspect_artifact(_wheel(tmp_path, extra_entries={member: b"noncanonical"}))

    assert not result.ok
    assert f"archive: unsafe member path {member!r}" in result.errors


def test_wheel_rejects_normalized_file_directory_collision(tmp_path: Path) -> None:
    result = inspect_artifact(
        _wheel(
            tmp_path,
            extra_entries={
                "payload": b"file",
                "payload/": b"",
            },
        )
    )

    assert not result.ok
    assert "archive: normalized member collision" in result.errors


def test_declared_member_limit_blocks_before_payload_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _wheel(tmp_path)

    def sizes(reader: artifact_inspector.ArchiveReader) -> tuple[int, ...]:
        names = reader.names()
        return (artifact_inspector._MAX_MEMBER_BYTES + 1, *(0 for _ in names[1:]))

    monkeypatch.setattr(artifact_inspector._WheelReader, "sizes", sizes)
    monkeypatch.setattr(
        artifact_inspector._WheelReader,
        "read",
        lambda *args: pytest.fail("oversized archive payload was read"),
    )

    result = inspect_artifact(wheel)

    assert not result.ok
    assert any("archive: member exceeds byte limit" in error for error in result.errors)


def test_declared_total_limit_blocks_before_payload_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _wheel(tmp_path)

    def sizes(reader: artifact_inspector.ArchiveReader) -> tuple[int, ...]:
        names = reader.names()
        per_member = artifact_inspector._MAX_TOTAL_BYTES // len(names) + 1
        assert per_member <= artifact_inspector._MAX_MEMBER_BYTES
        return (per_member,) * len(names)

    monkeypatch.setattr(artifact_inspector._WheelReader, "sizes", sizes)
    monkeypatch.setattr(
        artifact_inspector._WheelReader,
        "read",
        lambda *args: pytest.fail("oversized archive payload was read"),
    )

    result = inspect_artifact(wheel)

    assert not result.ok
    assert any("archive: uncompressed bytes" in error for error in result.errors)


def test_release_mode_requires_real_project_urls(tmp_path: Path) -> None:
    result = inspect_artifact(_wheel(tmp_path), require_project_urls=True)
    assert not result.ok
    assert result.release_blockers == ()
    assert "metadata: approved repository/docs/issues URLs are not configured" in result.errors


def test_release_cli_does_not_reject_separately_evidenced_platform_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "native.whl"
    artifact.write_bytes(b"fixture")
    result = artifact_inspector.InspectionResult(
        path=str(artifact),
        kind="wheel",
        variant="native",
        member_count=1,
        uncompressed_bytes=1,
        metadata={},
        errors=(),
        release_blockers=(),
        deferred_platform_checks=("target-platform audit required",),
    )
    monkeypatch.setattr(artifact_inspector, "inspect_artifact", lambda *args, **kwargs: result)
    assert artifact_inspector.main([str(artifact), "--release"]) == 0
