"""Audit one native executable's dynamic dependencies for a Python runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, cast

SCHEMA = "pyowl-core/comparator-linkage-audit/v1"
SOURCE_IDENTITY_SCHEMA = "pyowl-core/comparator-linkage-source/v1"

_MAX_INSPECTOR_OUTPUT_BYTES = 4 * 1024**2
_SOURCE_IDENTITY_DOMAIN = b"pyowl-core:comparator-linkage-source:v1\x00"
_SOURCE_LABEL = "tools/benchmark/comparators/linkage_audit.py"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MACHO_DEPENDENCY = re.compile(
    r"^\s*(?P<dependency>.+?)\s+\("
    r"(?:compatibility version|current version|offset)\b"
)
_ELF_NEEDED = re.compile(
    r"^\s*0x[0-9a-fA-F]+\s+\(NEEDED\)\s+"
    r"Shared library:\s+\[(?P<dependency>[^\]]+)\]\s*$"
)
_OBJDUMP_NEEDED = re.compile(r"^\s*NEEDED\s+(?P<dependency>\S.*?)\s*$")
_OBJDUMP_DLL = re.compile(r"^\s*DLL Name:\s*(?P<dependency>\S.*?)\s*$", re.IGNORECASE)
_DUMPBIN_DLL = re.compile(r"^[A-Za-z0-9_.+-]+\.dll$", re.IGNORECASE)
_GNU_NM_UNDEFINED = re.compile(r"^\s*[UuWwVv]\s+(?P<symbol>\S+)\s*$")
_READELF_SYMBOL_ROW = re.compile(
    r"^\s*\d+:\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(?P<index>\S+)"
    r"(?:\s+(?P<symbol>\S+))?(?:\s+\(\d+\))?\s*$"
)
_DUMPBIN_IMPORT_SYMBOL = re.compile(r"^\s+(?:[0-9A-Fa-f]+\s+){1,4}(?P<symbol>[A-Za-z_?@$]\S*)\s*$")
_DUMPBIN_IMPORT_HEADING = re.compile(
    r"^section contains the following (?:delay load )?imports:$",
    re.IGNORECASE,
)
_LLVM_READOBJ_IMPORT_SYMBOL = re.compile(r"^\s*Symbol:\s+(?P<symbol>\S+)(?:\s+\([^)]*\))?\s*$")
_LLVM_READOBJ_IMPORT_NAME = re.compile(r"^\s*Name:\s+(?P<dependency>\S.*?)\s*$")
_PYTHON_SYMBOL = re.compile(r"_?Py(?:[A-Z_][A-Za-z0-9_]*)\Z")
_WINDOWS_PYTHON = re.compile(
    r"(?:"
    r"(?:lib)?python(?:\d+(?:\.\d+)*[a-z]*)?(?:_d)?"
    r"|libpypy\d+(?:\.\d+)*-c"
    r"|(?:lib)?python-native"
    r"|(?:lib)?rustpython_capi"
    r")\.dll\Z",
    re.IGNORECASE,
)
_UNIX_PYTHON = re.compile(
    r"(?:"
    r"libpython(?:\d+(?:\.\d+)*)?(?:[a-z]*)?"
    r"|libpypy\d+(?:\.\d+)*-c"
    r"|libpython-native"
    r"|librustpython_capi"
    r")"
    r"(?:\.so(?:\.\d+)*|(?:\.\d+)*\.dylib)\Z",
    re.IGNORECASE,
)
_MACHO_PYTHON_FRAMEWORK = re.compile(
    r"(?P<stem>python(?:\d+(?:\.\d+)*)?)\.framework\Z",
    re.IGNORECASE,
)

PlatformFamily = Literal["darwin", "linux", "windows"]
Locator = Callable[[str], str | None]


class LinkageOutputError(ValueError):
    """Raised when a successful inspector emits an unrecognized format."""


@dataclass(frozen=True, slots=True)
class _Inspector:
    executable: str
    arguments: tuple[str, ...]
    parser: Callable[[str], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _Inspection:
    values: tuple[str, ...]
    evidence: dict[str, object]
    finding: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _CapturedOutput:
    text: str | None
    byte_count: int
    sha256: str

    @property
    def exceeds_limit(self) -> bool:
        return self.byte_count > _MAX_INSPECTOR_OUTPUT_BYTES


@dataclass(frozen=True, slots=True)
class _InspectorResult:
    returncode: int
    stdout: _CapturedOutput
    stderr: _CapturedOutput


Runner = Callable[
    [Sequence[str]],
    subprocess.CompletedProcess[str] | _InspectorResult,
]


_DEPENDENCY_INSPECTORS: dict[PlatformFamily, tuple[_Inspector, ...]] = {
    "darwin": (_Inspector("otool", ("-L",), lambda value: _parse_otool(value)),),
    "linux": (
        _Inspector("readelf", ("-dW",), lambda value: _parse_readelf(value)),
        _Inspector("objdump", ("-p",), lambda value: _parse_objdump(value)),
    ),
    "windows": (
        _Inspector("dumpbin", ("/NOLOGO", "/DEPENDENTS"), lambda value: _parse_dumpbin(value)),
        _Inspector(
            "llvm-readobj",
            ("--coff-imports",),
            lambda value: _parse_llvm_readobj_dependencies(value),
        ),
    ),
}

_SYMBOL_INSPECTORS: dict[PlatformFamily, tuple[_Inspector, ...]] = {
    "darwin": (_Inspector("nm", ("-u",), lambda value: _parse_macho_nm(value)),),
    "linux": (
        _Inspector(
            "nm",
            ("-D", "--undefined-only"),
            lambda value: _parse_elf_nm(value),
        ),
        _Inspector(
            "readelf",
            ("--dyn-syms", "--wide"),
            lambda value: _parse_readelf_symbols(value),
        ),
    ),
    "windows": (
        _Inspector(
            "dumpbin",
            ("/NOLOGO", "/IMPORTS"),
            lambda value: _parse_dumpbin_imports(value),
        ),
        _Inspector(
            "llvm-readobj",
            ("--coff-imports",),
            lambda value: _parse_llvm_readobj_imports(value),
        ),
    ),
}


def audit_binary_linkage(
    binary: Path,
    *,
    expected_runner_sha256: str | None = None,
    platform_name: str | None = None,
    locator: Locator | None = None,
    runner: Runner | None = None,
) -> dict[str, object]:
    """Return deterministic evidence for one opt-in platform linkage audit."""

    if not isinstance(binary, Path):
        raise TypeError("binary must be a Path")
    if expected_runner_sha256 is not None and not _SHA256.fullmatch(expected_runner_sha256):
        raise ValueError("expected runner SHA-256 must be lowercase hexadecimal")

    selected_platform = platform_name or platform.system()
    family = _platform_family(selected_platform)
    artifact, artifact_findings, resolved = _bind_binary(binary, expected_runner_sha256)
    if resolved is None:
        return _report(
            platform_name=selected_platform,
            family=family,
            artifact=artifact,
            inspections={},
            dependencies=(),
            imported_python_symbols=(),
            findings=artifact_findings,
            status="fail",
            reason="the supplied binary could not be inspected",
        )
    if family is None:
        return _report(
            platform_name=selected_platform,
            family=None,
            artifact=artifact,
            inspections={},
            dependencies=(),
            imported_python_symbols=(),
            findings=artifact_findings,
            status="fail" if artifact_findings else "not-run",
            reason=f"unsupported linkage-audit platform: {selected_platform}",
        )

    selected_locator = locator or shutil.which
    selected_runner = runner or _run_inspector
    dependency_inspector, dependency_executable = _select_inspector(
        _DEPENDENCY_INSPECTORS[family], selected_locator
    )
    if dependency_inspector is None or dependency_executable is None:
        tried = ", ".join(value.executable for value in _DEPENDENCY_INSPECTORS[family])
        return _report(
            platform_name=selected_platform,
            family=family,
            artifact=artifact,
            inspections={},
            dependencies=(),
            imported_python_symbols=(),
            findings=artifact_findings,
            status="fail" if artifact_findings else "not-run",
            reason=f"no supported {family} dependency inspector is available (tried: {tried})",
        )
    symbol_inspector, symbol_executable = _select_inspector(
        _SYMBOL_INSPECTORS[family], selected_locator
    )
    if symbol_inspector is None or symbol_executable is None:
        tried = ", ".join(value.executable for value in _SYMBOL_INSPECTORS[family])
        return _report(
            platform_name=selected_platform,
            family=family,
            artifact=artifact,
            inspections={},
            dependencies=(),
            imported_python_symbols=(),
            findings=artifact_findings,
            status="fail" if artifact_findings else "not-run",
            reason=f"no supported {family} symbol inspector is available (tried: {tried})",
        )

    dependency_inspection = _inspect(
        dependency_inspector,
        dependency_executable,
        resolved,
        selected_runner,
        role="dependency",
    )
    inspections = {"dependencies": dependency_inspection.evidence}
    dependency_binding_finding, dependency_binding_reason = _verify_binary_binding(
        resolved,
        cast(str, artifact["runner_sha256"]),
    )
    if dependency_inspection.finding is not None or dependency_binding_finding is not None:
        return _report(
            platform_name=selected_platform,
            family=family,
            artifact=artifact,
            inspections=inspections,
            dependencies=dependency_inspection.values,
            imported_python_symbols=(),
            findings=[
                *artifact_findings,
                *(
                    (dependency_inspection.finding,)
                    if dependency_inspection.finding is not None
                    else ()
                ),
                *((dependency_binding_finding,) if dependency_binding_finding is not None else ()),
            ],
            status="fail",
            reason=dependency_inspection.reason or dependency_binding_reason,
        )

    symbol_inspection = _inspect(
        symbol_inspector,
        symbol_executable,
        resolved,
        selected_runner,
        role="symbol",
    )
    inspections["symbols"] = symbol_inspection.evidence
    symbol_binding_finding, symbol_binding_reason = _verify_binary_binding(
        resolved,
        cast(str, artifact["runner_sha256"]),
    )
    if symbol_inspection.finding is not None or symbol_binding_finding is not None:
        return _report(
            platform_name=selected_platform,
            family=family,
            artifact=artifact,
            inspections=inspections,
            dependencies=dependency_inspection.values,
            imported_python_symbols=(),
            findings=[
                *artifact_findings,
                *((symbol_inspection.finding,) if symbol_inspection.finding is not None else ()),
                *((symbol_binding_finding,) if symbol_binding_finding is not None else ()),
            ],
            status="fail",
            reason=symbol_inspection.reason or symbol_binding_reason,
        )

    forbidden = tuple(
        dependency
        for dependency in dependency_inspection.values
        if _is_python_runtime_dependency(dependency, family)
    )
    imported_python_symbols = tuple(
        symbol for symbol in symbol_inspection.values if _is_python_runtime_symbol(symbol, family)
    )
    findings = [
        *artifact_findings,
        *(f"forbidden Python runtime dependency: {value}" for value in forbidden),
        *(f"forbidden Python runtime symbol import: {value}" for value in imported_python_symbols),
    ]
    return _report(
        platform_name=selected_platform,
        family=family,
        artifact=artifact,
        inspections=inspections,
        dependencies=dependency_inspection.values,
        imported_python_symbols=imported_python_symbols,
        findings=findings,
        status="fail" if findings else "pass",
        reason=None,
    )


def _verify_binary_binding(path: Path, expected_sha256: str) -> tuple[str | None, str | None]:
    try:
        observed_sha256 = _sha256_file(path)
    except OSError as error:
        return (
            f"binary: cannot rebind input after inspection: {type(error).__name__}",
            "the supplied binary became unavailable during inspection",
        )
    if observed_sha256 != expected_sha256:
        return (
            "binary: content changed during linkage inspection",
            "the supplied binary changed during linkage inspection",
        )
    return None, None


def _bind_binary(
    binary: Path, expected_runner_sha256: str | None
) -> tuple[dict[str, object], list[str], Path | None]:
    findings: list[str] = []
    observed_sha256: str | None = None
    byte_count: int | None = None
    resolved: Path | None = None
    try:
        resolved = binary.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("supplied path is not a regular file")
        byte_count = resolved.stat().st_size
        observed_sha256 = _sha256_file(resolved)
    except (OSError, ValueError) as error:
        findings.append(f"binary: cannot bind input: {type(error).__name__}")
        resolved = None
    if (
        expected_runner_sha256 is not None
        and observed_sha256 is not None
        and observed_sha256 != expected_runner_sha256
    ):
        findings.append("binary: SHA-256 differs from expected binding")
    return (
        {
            "path": binary.as_posix(),
            "bytes": byte_count,
            "runner_sha256": observed_sha256,
            "expected_runner_sha256": expected_runner_sha256,
        },
        findings,
        resolved,
    )


def _platform_family(value: str) -> PlatformFamily | None:
    normalized = value.casefold()
    if normalized in {"darwin", "macos"}:
        return "darwin"
    if normalized == "linux":
        return "linux"
    if normalized in {"windows", "win32"}:
        return "windows"
    return None


def _select_inspector(
    inspectors: Sequence[_Inspector], locator: Locator
) -> tuple[_Inspector | None, str | None]:
    for inspector in inspectors:
        executable = locator(inspector.executable)
        if executable is not None:
            return inspector, executable
    return None, None


def _inspect(
    inspector: _Inspector,
    executable: str,
    binary: Path,
    runner: Runner,
    *,
    role: Literal["dependency", "symbol"],
) -> _Inspection:
    command = (executable, *inspector.arguments, str(binary))
    try:
        completed = _normalize_inspector_result(runner(command))
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        return _Inspection(
            (),
            _inspection_evidence(inspector, None),
            f"{inspector.executable}: {role} inspector execution failed: {type(error).__name__}",
            f"the platform {role} inspector did not complete",
        )

    evidence = _inspection_evidence(inspector, completed)
    if completed.stdout.exceeds_limit or completed.stderr.exceeds_limit:
        return _Inspection(
            (),
            evidence,
            f"{inspector.executable}: {role} inspector output exceeds limit",
            f"the platform {role} inspector emitted excessive output",
        )
    if completed.returncode != 0:
        return _Inspection(
            (),
            evidence,
            (f"{inspector.executable}: {role} inspector exited with status {completed.returncode}"),
            f"the platform {role} inspector rejected the supplied binary",
        )
    stdout = completed.stdout.text
    if stdout is None:
        raise AssertionError("bounded inspector stdout must be available")
    try:
        values = inspector.parser(stdout)
    except LinkageOutputError:
        return _Inspection(
            (),
            evidence,
            (f"{inspector.executable}: successful {role} output format was not recognized"),
            f"the platform {role} inspector output could not be parsed",
        )
    return _Inspection(values, evidence)


def _inspection_evidence(
    inspector: _Inspector,
    completed: _InspectorResult | None,
) -> dict[str, object]:
    if completed is None:
        return {
            "tool": inspector.executable,
            "arguments": list(inspector.arguments),
            "returncode": None,
            "stdout": None,
            "stderr": None,
        }
    return {
        "tool": inspector.executable,
        "arguments": list(inspector.arguments),
        "returncode": completed.returncode,
        "stdout": _captured_output_binding(completed.stdout),
        "stderr": _captured_output_binding(completed.stderr),
    }


def _capture_text(value: str) -> _CapturedOutput:
    payload = value.encode("utf-8")
    return _CapturedOutput(
        text=value,
        byte_count=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _captured_output_binding(value: _CapturedOutput) -> dict[str, object]:
    return {"bytes": value.byte_count, "sha256": value.sha256}


def _normalize_inspector_result(
    completed: subprocess.CompletedProcess[str] | _InspectorResult,
) -> _InspectorResult:
    if isinstance(completed, _InspectorResult):
        return completed
    if not isinstance(completed.returncode, int):
        raise TypeError("inspector return code must be an integer")
    if completed.stdout is not None and not isinstance(completed.stdout, str):
        raise TypeError("inspector stdout must be text")
    if completed.stderr is not None and not isinstance(completed.stderr, str):
        raise TypeError("inspector stderr must be text")
    return _InspectorResult(
        returncode=completed.returncode,
        stdout=_capture_text(completed.stdout or ""),
        stderr=_capture_text(completed.stderr or ""),
    )


def _run_inspector(command: Sequence[str]) -> _InspectorResult:
    environment = dict(os.environ)
    environment.update({"LANG": "C", "LC_ALL": "C"})
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        completed = subprocess.run(
            command,
            check=False,
            stdout=stdout,
            stderr=stderr,
            timeout=30,
            env=environment,
        )
        return _InspectorResult(
            returncode=completed.returncode,
            stdout=_capture_file(stdout),
            stderr=_capture_file(stderr),
        )


def _capture_file(stream: BinaryIO) -> _CapturedOutput:
    stream.seek(0, os.SEEK_END)
    byte_count = stream.tell()
    stream.seek(0)
    digest = hashlib.sha256()
    payload = bytearray() if byte_count <= _MAX_INSPECTOR_OUTPUT_BYTES else None
    for chunk in iter(lambda: stream.read(1024**2), b""):
        digest.update(chunk)
        if payload is not None:
            payload.extend(chunk)
    text = None if payload is None else payload.decode("utf-8", errors="replace")
    return _CapturedOutput(
        text=text,
        byte_count=byte_count,
        sha256=digest.hexdigest(),
    )


def _parse_otool(output: str) -> tuple[str, ...]:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise LinkageOutputError("unrecognized otool output")
    dependencies: list[str] = []
    headers = 0
    for line in lines:
        if not line[0].isspace():
            if not line.rstrip().endswith(":"):
                raise LinkageOutputError("unrecognized otool architecture header")
            headers += 1
            continue
        match = _MACHO_DEPENDENCY.match(line)
        if match is None:
            raise LinkageOutputError("unrecognized otool dependency row")
        dependencies.append(match.group("dependency"))
    if headers == 0:
        raise LinkageOutputError("missing otool image header")
    return tuple(sorted(set(dependencies)))


def _parse_readelf(output: str) -> tuple[str, ...]:
    recognized = "Dynamic section at offset " in output or "There is no dynamic section" in output
    if not recognized:
        raise LinkageOutputError("unrecognized readelf output")
    dependencies: list[str] = []
    for line in output.splitlines():
        match = _ELF_NEEDED.match(line)
        if match is not None:
            dependencies.append(match.group("dependency"))
        elif "(NEEDED)" in line:
            raise LinkageOutputError("unrecognized readelf dependency row")
    return tuple(sorted(set(dependencies)))


def _parse_objdump(output: str) -> tuple[str, ...]:
    if "file format " not in output:
        raise LinkageOutputError("unrecognized objdump output")
    dependencies: list[str] = []
    for line in output.splitlines():
        needed = _OBJDUMP_NEEDED.match(line)
        dll = _OBJDUMP_DLL.match(line)
        if needed is not None:
            dependencies.append(needed.group("dependency"))
        elif dll is not None:
            dependencies.append(dll.group("dependency"))
        elif re.match(r"^\s*NEEDED\b", line) or re.match(r"^\s*DLL Name:", line, re.IGNORECASE):
            raise LinkageOutputError("unrecognized objdump dependency row")
    return tuple(sorted(set(dependencies)))


def _parse_dumpbin(output: str) -> tuple[str, ...]:
    lines = output.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().casefold()
            in {
                "image has the following dependencies:",
                "image has the following delay load dependencies:",
            }
        ),
        None,
    )
    if start is None:
        raise LinkageOutputError("unrecognized dumpbin output")
    dependencies: list[str] = []
    finished = False
    for line in lines[start + 1 :]:
        value = line.strip()
        if value.casefold() == "summary":
            finished = True
            break
        if not value or value.casefold() == "image has the following delay load dependencies:":
            continue
        if _DUMPBIN_DLL.fullmatch(value):
            dependencies.append(value)
            continue
        raise LinkageOutputError("unrecognized dumpbin dependency row")
    if not finished:
        raise LinkageOutputError("unterminated dumpbin dependency output")
    return tuple(sorted(set(dependencies)))


def _parse_macho_nm(output: str) -> tuple[str, ...]:
    symbols: list[str] = []
    for line in output.splitlines():
        value = line.strip()
        if not value:
            continue
        if value.endswith(":") and "architecture" in value:
            continue
        if any(character.isspace() for character in value):
            raise LinkageOutputError("unrecognized Mach-O nm row")
        symbols.append(value)
    return tuple(sorted(set(symbols)))


def _parse_elf_nm(output: str) -> tuple[str, ...]:
    symbols: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        match = _GNU_NM_UNDEFINED.match(line)
        if match is None:
            raise LinkageOutputError("unrecognized ELF nm row")
        symbols.append(match.group("symbol"))
    return tuple(sorted(set(symbols)))


def _parse_readelf_symbols(output: str) -> tuple[str, ...]:
    if "Symbol table '.dynsym'" not in output:
        raise LinkageOutputError("unrecognized readelf symbol output")
    symbols: list[str] = []
    for line in output.splitlines():
        if re.match(r"^\s*\d+:", line) is None:
            continue
        match = _READELF_SYMBOL_ROW.match(line)
        if match is None:
            raise LinkageOutputError("unrecognized readelf symbol row")
        if match.group("index") == "UND" and (symbol := match.group("symbol")) is not None:
            symbols.append(symbol)
    return tuple(sorted(set(symbols)))


def _parse_dumpbin_imports(output: str) -> tuple[str, ...]:
    lines = output.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if _DUMPBIN_IMPORT_HEADING.fullmatch(line.strip()) is not None
        ),
        None,
    )
    if start is None:
        raise LinkageOutputError("unrecognized dumpbin import output")
    symbols: list[str] = []
    finished = False
    for line in lines[start + 1 :]:
        value = line.strip()
        if value.casefold() == "summary":
            finished = True
            break
        if _DUMPBIN_IMPORT_HEADING.fullmatch(value) is not None:
            continue
        if match := _DUMPBIN_IMPORT_SYMBOL.match(line):
            symbols.append(match.group("symbol"))
        symbols.extend(_python_symbol_tokens(line, "windows"))
    if not finished:
        raise LinkageOutputError("unterminated dumpbin import output")
    return tuple(sorted(set(symbols)))


def _parse_llvm_readobj_imports(output: str) -> tuple[str, ...]:
    if re.search(r"^\s*(?:Delay)?Import\s*\{\s*$", output, re.MULTILINE) is None:
        raise LinkageOutputError("unrecognized llvm-readobj import output")
    symbols: list[str] = []
    for line in output.splitlines():
        match = _LLVM_READOBJ_IMPORT_SYMBOL.match(line)
        if match is not None:
            symbols.append(match.group("symbol"))
        elif re.match(r"^\s*Symbol:", line):
            raise LinkageOutputError("unrecognized llvm-readobj import row")
    return tuple(sorted(set(symbols)))


def _parse_llvm_readobj_dependencies(output: str) -> tuple[str, ...]:
    dependencies: list[str] = []
    in_import = False
    found_import = False
    found_name = False
    for line in output.splitlines():
        if re.match(r"^\s*(?:Delay)?Import\s*\{\s*$", line):
            if in_import:
                raise LinkageOutputError("nested llvm-readobj import block")
            in_import = True
            found_import = True
            found_name = False
            continue
        if in_import and line.strip() == "}":
            if not found_name:
                raise LinkageOutputError("llvm-readobj import block has no name")
            in_import = False
            continue
        if not in_import:
            continue
        match = _LLVM_READOBJ_IMPORT_NAME.match(line)
        if match is not None:
            if found_name:
                raise LinkageOutputError("llvm-readobj import block has multiple names")
            dependencies.append(match.group("dependency"))
            found_name = True
        elif re.match(r"^\s*Name:", line):
            raise LinkageOutputError("unrecognized llvm-readobj dependency row")
    if not found_import or in_import:
        raise LinkageOutputError("unrecognized llvm-readobj import output")
    return tuple(sorted(set(dependencies)))


def _python_symbol_tokens(line: str, family: PlatformFamily) -> tuple[str, ...]:
    candidates = re.findall(r"[A-Za-z_?@$][A-Za-z0-9_?@$.+-]*", line)
    return tuple(
        candidate for candidate in candidates if _is_python_runtime_symbol(candidate, family)
    )


def _is_python_runtime_dependency(dependency: str, family: PlatformFamily) -> bool:
    normalized = dependency.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    if family == "windows":
        return _WINDOWS_PYTHON.fullmatch(basename) is not None
    if _UNIX_PYTHON.fullmatch(basename) is not None:
        return True
    if family != "darwin":
        return False
    for component in normalized.rsplit("/", 1)[0].split("/"):
        match = _MACHO_PYTHON_FRAMEWORK.fullmatch(component)
        if match is not None and basename.casefold() == match.group("stem").casefold():
            return True
    return False


def _is_python_runtime_symbol(symbol: str, family: PlatformFamily) -> bool:
    normalized = symbol.split("@", 1)[0]
    if normalized.startswith("__imp_"):
        normalized = normalized.removeprefix("__imp_")
    if family in {"darwin", "windows"} and normalized.startswith("_"):
        normalized = normalized[1:]
    return _PYTHON_SYMBOL.fullmatch(normalized) is not None


def _report(
    *,
    platform_name: str,
    family: PlatformFamily | None,
    artifact: dict[str, object],
    inspections: dict[str, dict[str, object]],
    dependencies: Sequence[str],
    imported_python_symbols: Sequence[str],
    findings: Sequence[str],
    status: Literal["pass", "fail", "not-run"],
    reason: str | None,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "status": status,
        "reason": reason,
        "source_identity": _source_identity(),
        "platform": {"reported": platform_name, "family": family},
        "binary": artifact,
        "inspectors": inspections,
        "dependencies": sorted(set(dependencies)),
        "imported_python_symbols": sorted(set(imported_python_symbols)),
        "findings": sorted(set(findings)),
    }


def _source_identity() -> dict[str, object]:
    path = Path(__file__).resolve()
    payload = path.read_bytes()
    rows = [
        {
            "path": _SOURCE_LABEL,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    ]
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema": SOURCE_IDENTITY_SCHEMA,
        "sha256": hashlib.sha256(_SOURCE_IDENTITY_DOMAIN + canonical).hexdigest(),
        "domain": _SOURCE_IDENTITY_DOMAIN[:-1].decode("ascii"),
        "preimage_format": (
            "UTF-8 domain, one NUL byte, then compact canonical JSON of "
            "path/bytes/sha256 rows sorted by path"
        ),
        "input_count": len(rows),
        "input_bytes": len(payload),
        "inputs": rows,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--expected-runner-sha256", required=True)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="return success for an explicit development-only unavailable-tool result",
    )
    parser.add_argument("--output", type=Path, help="write canonical JSON evidence to this path")
    args = parser.parse_args(argv)
    try:
        evidence = audit_binary_linkage(
            cast(Path, args.binary),
            expected_runner_sha256=cast(str, args.expected_runner_sha256),
        )
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    rendered = json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n"
    output = cast(Path | None, args.output)
    if output is None:
        print(rendered, end="")
    else:
        output.write_text(rendered, encoding="utf-8")
    if evidence["status"] == "pass":
        return 0
    if evidence["status"] == "not-run" and args.allow_partial:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "SOURCE_IDENTITY_SCHEMA", "audit_binary_linkage", "main"]
