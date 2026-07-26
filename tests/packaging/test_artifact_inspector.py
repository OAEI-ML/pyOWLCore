from __future__ import annotations

import base64
import csv
import hashlib
import io
import tarfile
import zipfile
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
_LICENSE_FILES = {
    "LICENSE": b"Apache License 2.0",
    "NOTICE": b"pyowl-core",
    "LLVM-exception.txt": b"LLVM exception",
    "Unicode-3.0.txt": b"Unicode License v3",
    "inventory.toml": b"schema = 1",
}


def _record(entries: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, payload in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + digest.decode("ascii"), len(payload)))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _wheel(
    tmp_path: Path,
    variant: str = "pure",
    *,
    extra_binary: str | None = None,
    internal_tag: str | None = None,
) -> Path:
    dist_info = "pyowl_core-0.1.0.dev0.dist-info"
    tag = "py3-none-any" if variant == "pure" else "cp310-cp310-manylinux_2_28_x86_64"
    entries = {
        "pyowl_core/__init__.py": b'__version__ = "0.1.0.dev0"\n',
        f"{dist_info}/METADATA": _METADATA,
        f"{dist_info}/WHEEL": (f"Wheel-Version: 1.0\nTag: {internal_tag or tag}\n".encode()),
    }
    for name, payload in _LICENSE_FILES.items():
        entries[f"{dist_info}/licenses/THIRD_PARTY_LICENSES/{name}"] = payload
    if variant == "native":
        entries["pyowl_core/_native.cpython-310-x86_64-linux-gnu.so"] = b"native-fixture"
    if extra_binary is not None:
        entries[extra_binary] = b"unapproved-native-fixture"
    record_name = f"{dist_info}/RECORD"
    entries[record_name] = _record(entries, record_name)
    path = tmp_path / f"pyowl_core-0.1.0.dev0-{tag}.whl"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if name.endswith(".so") else 0o100644) << 16
            archive.writestr(info, payload)
    return path


def _sdist(tmp_path: Path) -> Path:
    root = "pyowl_core-0.1.0.dev0"
    entries = {
        f"{root}/PKG-INFO": _METADATA,
        f"{root}/pyproject.toml": b"[build-system]\n",
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


def test_pure_wheel_rejects_native_binary_outside_package(tmp_path: Path) -> None:
    result = inspect_artifact(
        _wheel(tmp_path, extra_binary="payload/vendor.so"),
        expected_variant="pure",
    )

    assert not result.ok
    assert "wheel: pure artifact contains native binaries: payload/vendor.so" in result.errors


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


def test_sdist_contains_complete_sources_without_binaries(tmp_path: Path) -> None:
    result = inspect_artifact(_sdist(tmp_path), expected_variant="sdist")
    assert result.ok
    assert result.variant == "sdist"


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


def test_java_artifact_and_unsafe_member_are_rejected(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../escape.jar", b"forbidden")
    result = inspect_artifact(wheel)
    assert not result.ok
    assert any("unsafe member path" in error for error in result.errors)
    assert any("forbidden artifact" in error for error in result.errors)


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
