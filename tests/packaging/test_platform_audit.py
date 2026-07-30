from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.packaging import platform_audit

REVISION = "a" * 40


def _wheel(
    tmp_path: Path,
    tag: str,
    member: str,
    payload: bytes = b"native fixture",
) -> Path:
    path = tmp_path / f"pyowl_core-0.1.0-cp310-cp310-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"pyowl_core/{member}", payload)
    return path


def _structurally_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        platform_audit,
        "inspect_artifact",
        lambda *args, **kwargs: SimpleNamespace(ok=True, errors=()),
    )


def _linux_runner(command: tuple[str, ...]) -> str:
    if command[:2] == ("auditwheel", "show"):
        return "wheel is consistent with manylinux_2_28_x86_64\n"
    if command[:2] == ("file", "-b"):
        return "ELF 64-bit LSB shared object, x86-64\n"
    if command[:2] == ("readelf", "-d"):
        return (
            "0x1 (NEEDED) Shared library: [libgcc_s.so.1]\n"
            "0x1 (NEEDED) Shared library: [libc.so.6]\n"
        )
    if command[:3] == ("nm", "-D", "--defined-only"):
        return "0000000000001000 T PyInit__native\n"
    raise AssertionError(command)


def test_linux_lane_enforces_dependencies_exports_and_architecture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _structurally_valid(monkeypatch)
    wheel = _wheel(
        tmp_path,
        "manylinux_2_28_x86_64",
        "_native.cpython-310-x86_64-linux-gnu.so",
    )
    result = platform_audit.audit_native_wheel(
        wheel,
        lane="linux-x86_64",
        runner=_linux_runner,
    )
    assert result["dependencies"] == ["libc.so.6", "libgcc_s.so.1"]
    assert result["exports"] == ["PyInit__native"]
    assert len(result["tool_output_sha256"]) == 4  # type: ignore[arg-type]


def test_linux_lane_rejects_rpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _structurally_valid(monkeypatch)
    wheel = _wheel(
        tmp_path,
        "manylinux_2_28_x86_64",
        "_native.cpython-310-x86_64-linux-gnu.so",
    )

    def runner(command: tuple[str, ...]) -> str:
        output = _linux_runner(command)
        if command[:2] == ("readelf", "-d"):
            return output + "0x1d (RUNPATH) Library runpath: [/home/build]\n"
        return output

    with pytest.raises(platform_audit.PlatformAuditError, match="RPATH/RUNPATH"):
        platform_audit.audit_native_wheel(wheel, lane="linux-x86_64", runner=runner)


def test_native_binary_rejects_embedded_runner_cargo_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _structurally_valid(monkeypatch)
    wheel = _wheel(
        tmp_path,
        "manylinux_2_28_x86_64",
        "_native.cpython-310-x86_64-linux-gnu.so",
        b"panic at /github/home/.cargo/registry/src/crate/src/lib.rs",
    )
    with pytest.raises(platform_audit.PlatformAuditError, match="build-path marker"):
        platform_audit.audit_native_wheel(
            wheel,
            lane="linux-x86_64",
            runner=_linux_runner,
        )


def test_macos_lane_enforces_install_name_timestamp_and_deployment_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _structurally_valid(monkeypatch)
    member = "_native.cpython-310-darwin.so"
    wheel = _wheel(tmp_path, "macosx_13_0_x86_64", member)

    def runner(command: tuple[str, ...]) -> str:
        binary = command[-1]
        if command[:2] == ("delocate-listdeps", "--all"):
            return "/usr/lib/libSystem.B.dylib\n"
        if command[:2] == ("file", "-b"):
            return "Mach-O 64-bit dynamically linked shared library x86_64\n"
        if command[:2] == ("otool", "-L"):
            return (
                f"{binary}:\n"
                f"\t@rpath/{member} (compatibility version 0.0.0)\n"
                "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
            )
        if command[:2] == ("otool", "-D"):
            return f"{binary}:\n@rpath/{member}\n"
        if command[:2] == ("otool", "-l"):
            return (
                "Load command 3\n cmd LC_ID_DYLIB\n time stamp 0 Thu Jan 1 00:00:00 1970\n"
                "Load command 4\n cmd LC_BUILD_VERSION\n minos 13.0\n"
            )
        if command[:2] == ("nm", "-gU"):
            return "0000000000001000 T _PyInit__native\n"
        raise AssertionError(command)

    result = platform_audit.audit_native_wheel(
        wheel,
        lane="macos-x86_64",
        runner=runner,
    )
    assert result["install_name"] == f"@rpath/{member}"
    assert result["exports"] == ["_PyInit__native"]
    repeated = platform_audit.audit_native_wheel(
        wheel,
        lane="macos-x86_64",
        runner=runner,
    )
    assert repeated["tool_output_sha256"] == result["tool_output_sha256"]


def test_windows_lane_rejects_non_system_dll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _structurally_valid(monkeypatch)
    wheel = _wheel(tmp_path, "win_amd64", "_native.cp310-win_amd64.pyd")

    def runner(command: tuple[str, ...]) -> str:
        if command[:2] == ("delvewheel", "show"):
            return "external dependencies inspected\n"
        if command[:2] == ("dumpbin", "/HEADERS"):
            return "8664 machine (x64)\n"
        if command[:2] == ("dumpbin", "/DEPENDENTS"):
            return " KERNEL32.dll\n forbidden_vendor.dll\n"
        if command[:2] == ("dumpbin", "/EXPORTS"):
            return "1 0 00001000 PyInit__native\n"
        raise AssertionError(command)

    with pytest.raises(platform_audit.PlatformAuditError, match="forbidden_vendor"):
        platform_audit.audit_native_wheel(wheel, lane="windows-x86_64", runner=runner)


def _wheel_row(path: Path, *, platform: str) -> dict[str, object]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    tools = {
        "linux": ("auditwheel show", "file -b", "readelf -d", "nm -D --defined-only"),
        "macos": (
            "delocate-listdeps --all",
            "file -b",
            "otool -L",
            "otool -D",
            "otool -l",
            "nm -gU",
        ),
        "windows": (
            "delvewheel show",
            "dumpbin /HEADERS",
            "dumpbin /DEPENDENTS",
            "dumpbin /EXPORTS",
        ),
    }[platform]
    return {
        "filename": path.name,
        "sha256": digest,
        "native_member": "pyowl_core/_native.fixture.so",
        "native_sha256": hashlib.sha256(b"native fixture").hexdigest(),
        "dependencies": [],
        "exports": ["_PyInit__native" if platform == "macos" else "PyInit__native"],
        "install_name": "@rpath/_native.fixture.so" if platform == "macos" else None,
        "tool_output_sha256": {tool: "c" * 64 for tool in tools},
    }


def test_complete_audit_set_is_exactly_hash_bound(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "dist"
    manifest_dir = tmp_path / "manifests"
    artifact_dir.mkdir()
    manifest_dir.mkdir()
    manifests: list[Path] = []
    for lane, (platform, arch, tag) in platform_audit.APPROVED_LANES.items():
        rows: list[dict[str, object]] = []
        for version in range(310, 315):
            wheel = artifact_dir / f"pyowl_core-0.1.0-cp{version}-cp{version}-{tag}.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("pyowl_core/_native.fixture.so", b"native fixture")
            rows.append(_wheel_row(wheel, platform=platform))
        payload = {
            "schema": platform_audit.SCHEMA,
            "source_revision": REVISION,
            "lane": lane,
            "platform": platform,
            "architecture": arch,
            "status": "passed",
            "wheel_count": 5,
            "wheels": rows,
        }
        manifest = manifest_dir / f"{lane}.json"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        manifests.append(manifest)

    report = platform_audit.verify_audit_set(
        manifests,
        artifact_dir=artifact_dir,
        source_revision=REVISION,
    )
    assert report["status"] == "passed"
    assert report["lane_count"] == 5
    assert report["wheel_count"] == 25

    artifact = next(artifact_dir.iterdir())
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("tampered", b"yes")
    with pytest.raises(platform_audit.PlatformAuditError, match="digest does not match"):
        platform_audit.verify_audit_set(
            manifests,
            artifact_dir=artifact_dir,
            source_revision=REVISION,
        )
