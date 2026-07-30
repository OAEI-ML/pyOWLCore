"""Fail-closed target-platform audits for pyowl-core native wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from .artifact_inspector import inspect_artifact

Platform = Literal["linux", "macos", "windows"]
Runner = Callable[[tuple[str, ...]], str]

SCHEMA = "pyowl-core.platform-audit/1"
APPROVED_LANES: dict[str, tuple[Platform, str, str]] = {
    "linux-x86_64": ("linux", "x86_64", "manylinux_2_28_x86_64"),
    "linux-aarch64": ("linux", "aarch64", "manylinux_2_28_aarch64"),
    "macos-x86_64": ("macos", "x86_64", "macosx_13_0_x86_64"),
    "macos-arm64": ("macos", "arm64", "macosx_13_0_arm64"),
    "windows-x86_64": ("windows", "AMD64", "win_amd64"),
}
_NATIVE_SUFFIXES = (".dll", ".dylib", ".pyd", ".so")
_FORBIDDEN_BINARY_MARKERS = (
    b"/home/runner/work/",
    b"/Users/runner/work/",
    b"/Users/runner/.cargo/",
    b"/github/home/.cargo/",
    b"/root/.cargo/",
    b"/github/workspace/",
    b"/project/",
    b"D:\\a\\",
    b"\\Users\\runneradmin\\",
    b"\\Users\\runneradmin\\.cargo\\",
    b"libjvm",
    b"jvm.dll",
    b"java.dll",
    b"libjli",
)
_FORBIDDEN_TOOL_OUTPUT = re.compile(r"(?i)(?:libjvm|jvm\.dll|java\.dll|libjli)")
_LINUX_ALLOWED = re.compile(
    r"^(?:ld-linux[^/]*|lib(?:c|dl|gcc_s|m|pthread|rt|util)\.so(?:\.[0-9]+)*)$"
)
_WINDOWS_ALLOWED = re.compile(
    r"(?i)^(?:api-ms-win-[a-z0-9-]+|python3(?:10|11|12|13|14)|"
    r"advapi32|bcrypt(?:primitives)?|comdlg32|crypt32|gdi32|kernel32|msvcp140|"
    r"ntdll|ole32|"
    r"oleaut32|rpcrt4|secur32|shell32|ucrtbase|user32|userenv|vcruntime140(?:_1)?|"
    r"winhttp|winmm|ws2_32)\.dll$"
)
_EXPECTED_TOOLS: dict[Platform, frozenset[str]] = {
    "linux": frozenset(
        {"auditwheel show", "file -b", "readelf -d", "nm -D --defined-only"}
    ),
    "macos": frozenset(
        {
            "delocate-listdeps --all",
            "file -b",
            "otool -L",
            "otool -D",
            "otool -l",
            "nm -gU",
        }
    ),
    "windows": frozenset(
        {
            "delvewheel show",
            "dumpbin /HEADERS",
            "dumpbin /DEPENDENTS",
            "dumpbin /EXPORTS",
        }
    ),
}


class PlatformAuditError(ValueError):
    """A native wheel failed a target-platform release policy."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: tuple[str, ...]) -> str:
    if shutil.which(command[0]) is None:
        raise PlatformAuditError(f"required platform audit tool is unavailable: {command[0]}")
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PlatformAuditError(f"platform audit command failed: {' '.join(command)}") from error
    return completed.stdout + completed.stderr


def _tool(
    runner: Runner,
    command: tuple[str, ...],
    outputs: dict[str, str],
) -> str:
    output = runner(command)
    label = " ".join(command[:-1])
    normalized = output.replace(command[-1], "<subject>")
    outputs[label] = _sha256_bytes(normalized.encode("utf-8"))
    if _FORBIDDEN_TOOL_OUTPUT.search(normalized):
        raise PlatformAuditError(f"forbidden Java/build-path marker in {label} output")
    return output


def _native_member(wheel: Path) -> tuple[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        members = [
            name
            for name in archive.namelist()
            if name.startswith("pyowl_core/")
            and PurePosixPath(name).name.startswith("_native.")
            and name.casefold().endswith(_NATIVE_SUFFIXES)
        ]
        if len(members) != 1:
            raise PlatformAuditError(
                f"{wheel.name}: expected exactly one pyowl_core/_native extension, "
                f"found {len(members)}"
            )
        return members[0], archive.read(members[0])


def _require_architecture(output: str, arch: str) -> None:
    patterns = {
        "x86_64": r"(?i)(?:x86[-_ ]64|amd64)",
        "aarch64": r"(?i)(?:aarch64|arm64)",
        "arm64": r"(?i)(?:arm64|aarch64)",
        "AMD64": r"(?i)(?:x64|amd64|8664 machine)",
    }
    if re.search(patterns[arch], output) is None:
        raise PlatformAuditError(f"native extension does not report required architecture {arch}")


def _python_exports(output: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"_?PyInit_[A-Za-z0-9_]+", output))))


def _audit_linux(
    wheel: Path,
    binary: Path,
    arch: str,
    runner: Runner,
    outputs: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    auditwheel = _tool(runner, ("auditwheel", "show", str(wheel)), outputs)
    file_output = _tool(runner, ("file", "-b", str(binary)), outputs)
    dynamic = _tool(runner, ("readelf", "-d", str(binary)), outputs)
    symbols = _tool(runner, ("nm", "-D", "--defined-only", str(binary)), outputs)
    _require_architecture(file_output, arch)
    if "manylinux_2_28" not in wheel.name or "manylinux_2_28" not in auditwheel:
        raise PlatformAuditError("Linux wheel is not proven against the manylinux_2_28 baseline")
    if re.search(r"\((?:RPATH|RUNPATH)\)", dynamic):
        raise PlatformAuditError("Linux extension contains an unapproved RPATH/RUNPATH")
    dependencies = tuple(
        sorted(set(re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", dynamic)))
    )
    unexpected = tuple(name for name in dependencies if _LINUX_ALLOWED.fullmatch(name) is None)
    if unexpected:
        raise PlatformAuditError(f"unapproved Linux dynamic dependencies: {', '.join(unexpected)}")
    exports = _python_exports(symbols)
    if exports != ("PyInit__native",):
        raise PlatformAuditError(f"unexpected Python exports: {exports!r}")
    return dependencies, exports, None


def _macos_dependencies(output: str) -> tuple[str, ...]:
    lines = output.splitlines()[1:]
    return tuple(sorted(line.strip().split(" (", 1)[0] for line in lines if line.strip()))


def _audit_macos(
    wheel: Path,
    binary: Path,
    arch: str,
    runner: Runner,
    outputs: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    _tool(runner, ("delocate-listdeps", "--all", str(wheel)), outputs)
    file_output = _tool(runner, ("file", "-b", str(binary)), outputs)
    linked = _tool(runner, ("otool", "-L", str(binary)), outputs)
    install_id_output = _tool(runner, ("otool", "-D", str(binary)), outputs)
    load_commands = _tool(runner, ("otool", "-l", str(binary)), outputs)
    symbols = _tool(runner, ("nm", "-gU", str(binary)), outputs)
    _require_architecture(file_output, arch)
    install_ids = tuple(line.strip() for line in install_id_output.splitlines()[1:] if line.strip())
    expected_id = f"@rpath/{binary.name}"
    if install_ids != (expected_id,):
        raise PlatformAuditError(
            f"macOS install name is {install_ids!r}, expected {(expected_id,)!r}"
        )
    if "LC_RPATH" in load_commands:
        raise PlatformAuditError("macOS extension contains an unapproved LC_RPATH")
    id_block = re.search(
        r"cmd LC_ID_DYLIB(?:(?!Load command).)*?time stamp\s+(\d+)",
        load_commands,
        re.DOTALL,
    )
    if id_block is None or id_block.group(1) != "0":
        raise PlatformAuditError("macOS LC_ID_DYLIB timestamp is not normalized to zero")
    minimum = re.search(
        r"cmd LC_(?:BUILD_VERSION|VERSION_MIN_MACOSX)(?:(?!Load command).)*?"
        r"(?:minos|version)\s+([0-9.]+)",
        load_commands,
        re.DOTALL,
    )
    if minimum is None or tuple(int(part) for part in minimum.group(1).split(".")) < (13, 0):
        raise PlatformAuditError("macOS deployment target is older than 13.0")
    dependencies = _macos_dependencies(linked)
    unexpected = tuple(
        name
        for name in dependencies
        if name != expected_id
        and not name.startswith("/usr/lib/")
        and not name.startswith("/System/Library/")
    )
    if unexpected:
        raise PlatformAuditError(f"unapproved macOS dynamic dependencies: {', '.join(unexpected)}")
    exports = _python_exports(symbols)
    if exports != ("_PyInit__native",):
        raise PlatformAuditError(f"unexpected Python exports: {exports!r}")
    return dependencies, exports, expected_id


def _audit_windows(
    wheel: Path,
    binary: Path,
    arch: str,
    runner: Runner,
    outputs: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    _tool(runner, ("delvewheel", "show", str(wheel)), outputs)
    headers = _tool(runner, ("dumpbin", "/HEADERS", str(binary)), outputs)
    dependent_output = _tool(runner, ("dumpbin", "/DEPENDENTS", str(binary)), outputs)
    symbol_output = _tool(runner, ("dumpbin", "/EXPORTS", str(binary)), outputs)
    _require_architecture(headers, arch)
    dependencies = tuple(
        sorted(set(re.findall(r"(?im)^\s*([A-Za-z0-9_.-]+\.dll)\s*$", dependent_output)))
    )
    unexpected = tuple(name for name in dependencies if _WINDOWS_ALLOWED.fullmatch(name) is None)
    if unexpected:
        raise PlatformAuditError(
            f"unapproved Windows dynamic dependencies: {', '.join(unexpected)}"
        )
    exports = _python_exports(symbol_output)
    if exports != ("PyInit__native",):
        raise PlatformAuditError(f"unexpected Python exports: {exports!r}")
    return dependencies, exports, None


def audit_native_wheel(
    wheel: Path,
    *,
    lane: str,
    runner: Runner = _run,
) -> dict[str, object]:
    """Audit one wheel on its target host and return deterministic evidence."""

    try:
        platform, arch, required_tag = APPROVED_LANES[lane]
    except KeyError as error:
        raise PlatformAuditError(f"unknown platform audit lane {lane!r}") from error
    wheel = wheel.resolve()
    inspection = inspect_artifact(wheel, expected_variant="native")
    if not inspection.ok:
        raise PlatformAuditError(f"{wheel.name}: structural inspection failed: {inspection.errors}")
    if required_tag not in wheel.name:
        raise PlatformAuditError(f"{wheel.name}: expected platform tag containing {required_tag!r}")
    member, payload = _native_member(wheel)
    lowered = payload.lower()
    marker = next(
        (value for value in _FORBIDDEN_BINARY_MARKERS if value.lower() in lowered),
        None,
    )
    if marker is not None:
        raise PlatformAuditError(
            f"{wheel.name}: forbidden Java/build-path marker {marker.decode(errors='replace')!r}"
        )
    outputs: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="pyowl-platform-audit-") as temporary:
        binary = Path(temporary) / PurePosixPath(member).name
        binary.write_bytes(payload)
        if platform == "linux":
            dependencies, exports, install_name = _audit_linux(
                wheel, binary, arch, runner, outputs
            )
        elif platform == "macos":
            dependencies, exports, install_name = _audit_macos(
                wheel, binary, arch, runner, outputs
            )
        else:
            dependencies, exports, install_name = _audit_windows(
                wheel, binary, arch, runner, outputs
            )
    return {
        "filename": wheel.name,
        "sha256": _sha256_file(wheel),
        "native_member": member,
        "native_sha256": _sha256_bytes(payload),
        "dependencies": list(dependencies),
        "exports": list(exports),
        "install_name": install_name,
        "tool_output_sha256": dict(sorted(outputs.items())),
    }


def build_lane_manifest(
    wheels: Sequence[Path],
    *,
    lane: str,
    source_revision: str,
    runner: Runner = _run,
) -> dict[str, object]:
    """Audit an approved five-wheel lane and bind it to source."""

    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise PlatformAuditError("source revision must be a full lowercase Git SHA")
    if lane not in APPROVED_LANES:
        raise PlatformAuditError(f"unknown platform audit lane {lane!r}")
    selected = tuple(sorted((path.resolve() for path in wheels), key=lambda path: path.name))
    if len(selected) != 5 or len({path.name for path in selected}) != 5:
        raise PlatformAuditError("a platform lane must contain exactly five unique wheels")
    versions = {
        match.group(1)
        for path in selected
        if (match := re.search(r"-cp(31[0-4])-cp\1-", path.name)) is not None
    }
    if versions != {"310", "311", "312", "313", "314"}:
        raise PlatformAuditError("a platform lane must contain CPython 3.10 through 3.14")
    platform, arch, _ = APPROVED_LANES[lane]
    reports = [audit_native_wheel(path, lane=lane, runner=runner) for path in selected]
    return {
        "schema": SCHEMA,
        "source_revision": source_revision,
        "lane": lane,
        "platform": platform,
        "architecture": arch,
        "status": "passed",
        "wheel_count": len(reports),
        "wheels": reports,
    }


def _manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PlatformAuditError(f"invalid platform audit JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise PlatformAuditError(f"platform audit manifest is not an object: {path}")
    return cast(dict[str, Any], payload)


def verify_audit_set(
    manifests: Sequence[Path],
    *,
    artifact_dir: Path,
    source_revision: str,
) -> dict[str, object]:
    """Verify five lane manifests cover exactly the candidate native wheels."""

    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise PlatformAuditError("source revision must be a full lowercase Git SHA")
    if len(manifests) != len(APPROVED_LANES):
        raise PlatformAuditError("the platform audit set must contain exactly five manifests")
    expected_fields = {
        "schema",
        "source_revision",
        "lane",
        "platform",
        "architecture",
        "status",
        "wheel_count",
        "wheels",
    }
    expected_wheel_fields = {
        "filename",
        "sha256",
        "native_member",
        "native_sha256",
        "dependencies",
        "exports",
        "install_name",
        "tool_output_sha256",
    }
    covered: dict[str, str] = {}
    lanes: dict[str, dict[str, object]] = {}
    for path in manifests:
        payload = _manifest(path)
        if set(payload) != expected_fields:
            raise PlatformAuditError(f"unexpected platform audit fields in {path}")
        lane = payload.get("lane")
        if not isinstance(lane, str) or lane not in APPROVED_LANES or lane in lanes:
            raise PlatformAuditError(f"unknown or duplicate platform audit lane in {path}")
        platform, arch, required_tag = APPROVED_LANES[lane]
        if (
            payload.get("schema") != SCHEMA
            or payload.get("source_revision") != source_revision
            or payload.get("platform") != platform
            or payload.get("architecture") != arch
            or payload.get("status") != "passed"
            or payload.get("wheel_count") != 5
        ):
            raise PlatformAuditError(f"invalid platform audit identity/status in {path}")
        wheel_rows = payload.get("wheels")
        if not isinstance(wheel_rows, list) or len(wheel_rows) != 5:
            raise PlatformAuditError(f"invalid wheel evidence count in {path}")
        lane_versions: set[str] = set()
        for row in wheel_rows:
            if not isinstance(row, dict) or set(row) != expected_wheel_fields:
                raise PlatformAuditError(f"invalid wheel evidence row in {path}")
            filename = row.get("filename")
            digest = row.get("sha256")
            native_digest = row.get("native_sha256")
            native_member = row.get("native_member")
            dependencies = row.get("dependencies")
            exports = row.get("exports")
            install_name = row.get("install_name")
            tool_hashes = row.get("tool_output_sha256")
            if (
                not isinstance(filename, str)
                or PurePosixPath(filename).name != filename
                or required_tag not in filename
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not isinstance(native_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", native_digest) is None
                or not isinstance(native_member, str)
                or not native_member.startswith("pyowl_core/_native.")
                or not isinstance(dependencies, list)
                or not all(isinstance(item, str) for item in dependencies)
                or not isinstance(exports, list)
                or exports not in [["PyInit__native"], ["_PyInit__native"]]
                or (install_name is not None and not isinstance(install_name, str))
                or not isinstance(tool_hashes, dict)
                or not tool_hashes
                or not all(
                    isinstance(name, str)
                    and isinstance(value, str)
                    and re.fullmatch(r"[0-9a-f]{64}", value) is not None
                    for name, value in tool_hashes.items()
                )
            ):
                raise PlatformAuditError(f"wheel evidence lacks filename/hash in {path}")
            version_match = re.search(r"-cp(31[0-4])-cp\1-", filename)
            if version_match is None:
                raise PlatformAuditError(f"wheel evidence has an invalid CPython tag in {path}")
            lane_versions.add(version_match.group(1))
            artifact_path = artifact_dir / filename
            if not artifact_path.is_file() or _sha256_file(artifact_path) != digest:
                raise PlatformAuditError(f"wheel evidence digest does not match {filename}")
            try:
                with zipfile.ZipFile(artifact_path) as archive:
                    native_payload = archive.read(native_member)
            except (KeyError, zipfile.BadZipFile) as error:
                raise PlatformAuditError(
                    f"wheel evidence native member does not match {filename}"
                ) from error
            if _sha256_bytes(native_payload) != native_digest:
                raise PlatformAuditError(f"native payload digest does not match {filename}")
            expected_export = ["_PyInit__native"] if platform == "macos" else ["PyInit__native"]
            if exports != expected_export or set(tool_hashes) != _EXPECTED_TOOLS[platform]:
                raise PlatformAuditError(f"wheel evidence has incomplete platform proof in {path}")
            if platform == "macos":
                expected_install_name = f"@rpath/{PurePosixPath(native_member).name}"
                dependency_error = any(
                    name != expected_install_name
                    and not name.startswith("/usr/lib/")
                    and not name.startswith("/System/Library/")
                    for name in dependencies
                )
                if install_name != expected_install_name or dependency_error:
                    raise PlatformAuditError(f"invalid macOS policy evidence in {path}")
            elif install_name is not None:
                raise PlatformAuditError(f"unexpected non-macOS install name in {path}")
            elif platform == "linux" and any(
                _LINUX_ALLOWED.fullmatch(name) is None for name in dependencies
            ):
                raise PlatformAuditError(f"invalid Linux dependency evidence in {path}")
            elif platform == "windows" and any(
                _WINDOWS_ALLOWED.fullmatch(name) is None for name in dependencies
            ):
                raise PlatformAuditError(f"invalid Windows dependency evidence in {path}")
            if filename in covered:
                raise PlatformAuditError(f"wheel is covered by more than one lane: {filename}")
            covered[filename] = digest
        if lane_versions != {"310", "311", "312", "313", "314"}:
            raise PlatformAuditError(f"platform lane has an incomplete CPython matrix in {path}")
        lanes[lane] = {
            "platform": platform,
            "architecture": arch,
            "manifest_sha256": _sha256_file(path),
        }
    if set(lanes) != set(APPROVED_LANES):
        raise PlatformAuditError("platform audit set does not cover every approved lane")
    candidate = {
        path.name: _sha256_file(path)
        for path in artifact_dir.iterdir()
        if path.is_file() and path.suffix == ".whl" and "-py3-none-any" not in path.name
    }
    if len(candidate) != 25 or covered != candidate:
        raise PlatformAuditError("platform audit hashes do not exactly cover 25 candidate wheels")
    return {
        "schema": SCHEMA,
        "source_revision": source_revision,
        "status": "passed",
        "lane_count": len(lanes),
        "wheel_count": len(covered),
        "lanes": dict(sorted(lanes.items())),
    }


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    lane_parser = commands.add_parser("audit", help="audit one five-wheel target lane")
    lane_parser.add_argument("wheels", nargs="+", type=Path)
    lane_parser.add_argument("--lane", required=True, choices=tuple(APPROVED_LANES))
    lane_parser.add_argument("--source-revision", required=True)
    lane_parser.add_argument("--output", required=True, type=Path)
    set_parser = commands.add_parser("verify-set", help="verify the complete hosted audit set")
    set_parser.add_argument("manifests", nargs="+", type=Path)
    set_parser.add_argument("--artifact-dir", required=True, type=Path)
    set_parser.add_argument("--source-revision", required=True)
    set_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            report = build_lane_manifest(
                args.wheels,
                lane=args.lane,
                source_revision=args.source_revision,
            )
        else:
            report = verify_audit_set(
                args.manifests,
                artifact_dir=args.artifact_dir.resolve(),
                source_revision=args.source_revision,
            )
    except (OSError, PlatformAuditError, zipfile.BadZipFile) as error:
        parser.error(str(error))
    _write_report(args.output, report)
    print(f"platform audit passed: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
