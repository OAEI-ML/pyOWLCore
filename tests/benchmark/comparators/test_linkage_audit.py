from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from tools.benchmark.comparators.linkage_audit import (
    SCHEMA,
    SOURCE_IDENTITY_SCHEMA,
    audit_binary_linkage,
    main,
)
from tools.benchmark.comparators.runner import comparator_source_identity

_MACHO_CLEAN = """\
/tmp/direct:
\t/usr/lib/libSystem.B.dylib \
(compatibility version 1.0.0, current version 1292.100.5)
"""
_MACHO_PYTHON = """\
/tmp/direct:
\t/usr/local/Frameworks/Python.framework/Versions/3.14/Python \
(compatibility version 3.14.0, current version 3.14.0)
\t/usr/lib/libSystem.B.dylib \
(compatibility version 1.0.0, current version 1292.100.5)
"""
_ELF_CLEAN = """\
Dynamic section at offset 0x1 contains 1 entry:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
"""
_ELF_PYTHON = """\
Dynamic section at offset 0x1 contains 2 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libpython3.13.so.1.0]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
"""
_PE_DUMPBIN_CLEAN = """\
Microsoft (R) COFF/PE Dumper
Image has the following dependencies:

    KERNEL32.dll
    VCRUNTIME140.dll

Summary
"""
_PE_DUMPBIN_IMPORTS_CLEAN = """\
Microsoft (R) COFF/PE Dumper
Section contains the following imports:

    KERNEL32.dll
              42    0 00001000 GetLastError

Summary
"""


def test_macho_python_framework_dependency_fails(tmp_path: Path) -> None:
    binary = _binary(tmp_path)

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(_MACHO_PYTHON, "_malloc\n"),
    )

    assert report["schema"] == SCHEMA
    assert report["status"] == "fail"
    assert report["dependencies"] == [
        "/usr/lib/libSystem.B.dylib",
        "/usr/local/Frameworks/Python.framework/Versions/3.14/Python",
    ]
    assert report["imported_python_symbols"] == []
    assert report["findings"] == [
        "forbidden Python runtime dependency: "
        "/usr/local/Frameworks/Python.framework/Versions/3.14/Python"
    ]


@pytest.mark.parametrize(
    "dependency",
    [
        "@rpath/Python3.framework/Versions/3.8/Python3",
        "/opt/frameworks/Python3.13.framework/Versions/3.13/Python3.13",
    ],
)
def test_macho_versioned_python_framework_dependencies_fail(
    tmp_path: Path,
    dependency: str,
) -> None:
    binary = _binary(tmp_path)
    dependencies = (
        f"/tmp/direct:\n\t{dependency} (compatibility version 3.0.0, current version 3.0.0)\n"
    )

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(dependencies, "_malloc\n"),
    )

    assert report["status"] == "fail"
    assert report["findings"] == [f"forbidden Python runtime dependency: {dependency}"]


@pytest.mark.parametrize(
    "dependency",
    [
        "@rpath/PythonTools.framework/Versions/A/PythonTools",
        "@rpath/Python3.framework/Versions/3.8/Python",
    ],
)
def test_macho_non_runtime_framework_names_do_not_false_positive(
    tmp_path: Path,
    dependency: str,
) -> None:
    binary = _binary(tmp_path)
    dependencies = (
        f"/tmp/direct:\n\t{dependency} (compatibility version 1.0.0, current version 1.0.0)\n"
    )

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(dependencies, "_malloc\n"),
    )

    assert report["status"] == "pass"
    assert report["findings"] == []


def test_macho_python_symbols_fail_without_python_library(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    symbols = "_PyBytes_AsString\n__Py_Dealloc\n_malloc\n"

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(_MACHO_CLEAN, symbols),
    )

    assert report["status"] == "fail"
    assert report["imported_python_symbols"] == ["_PyBytes_AsString", "__Py_Dealloc"]
    assert report["findings"] == [
        "forbidden Python runtime symbol import: _PyBytes_AsString",
        "forbidden Python runtime symbol import: __Py_Dealloc",
    ]


def test_clean_macho_binds_binary_and_both_inspector_outputs(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    symbols = "___error\n_malloc\n"

    report = audit_binary_linkage(
        binary,
        expected_runner_sha256=digest,
        platform_name="macOS",
        locator=_locator("otool", "nm"),
        runner=_runner(_MACHO_CLEAN, symbols),
    )

    assert report["status"] == "pass"
    assert report["reason"] is None
    assert report["binary"] == {
        "path": binary.as_posix(),
        "bytes": len(binary.read_bytes()),
        "runner_sha256": digest,
        "expected_runner_sha256": digest,
    }
    inspections = report["inspectors"]
    assert isinstance(inspections, dict)
    assert inspections["dependencies"]["tool"] == "otool"
    assert inspections["dependencies"]["stdout"] == _output_binding(_MACHO_CLEAN)
    assert inspections["symbols"]["tool"] == "nm"
    assert inspections["symbols"]["stdout"] == _output_binding(symbols)
    assert report["findings"] == []


def test_universal_macho_checks_every_architecture_and_symbol_table(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
/tmp/direct (architecture x86_64):
\t/usr/lib/libSystem.B.dylib \
(compatibility version 1.0.0, current version 1292.100.5)
/tmp/direct (architecture arm64):
\t/usr/lib/libpython3.13t.dylib \
(compatibility version 3.13.0, current version 3.13.0)
"""
    symbols = """\
/tmp/direct (for architecture x86_64):
_malloc
/tmp/direct (for architecture arm64):
_PyGILState_Ensure
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(dependencies, symbols),
    )

    assert report["status"] == "fail"
    assert report["findings"] == [
        "forbidden Python runtime dependency: /usr/lib/libpython3.13t.dylib",
        "forbidden Python runtime symbol import: _PyGILState_Ensure",
    ]


def test_macho_pyo3_alternate_runtime_dependencies_fail_without_symbols(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
/tmp/direct:
\t@rpath/libpypy3.11-c.1.dylib \
(compatibility version 1.0.0, current version 1.0.0)
\t@rpath/libpython-native.dylib \
(compatibility version 1.0.0, current version 1.0.0)
\t@rpath/librustpython_capi.dylib \
(compatibility version 1.0.0, current version 1.0.0)
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(dependencies, "_malloc\n"),
    )

    assert report["status"] == "fail"
    assert report["findings"] == [
        "forbidden Python runtime dependency: @rpath/libpypy3.11-c.1.dylib",
        "forbidden Python runtime dependency: @rpath/libpython-native.dylib",
        "forbidden Python runtime dependency: @rpath/librustpython_capi.dylib",
    ]


def test_macho_python_runtime_lookalikes_do_not_false_positive(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
/tmp/direct:
\t@rpath/libpypy3.11-c-helper.dylib \
(compatibility version 1.0.0, current version 1.0.0)
\t@rpath/libpython-native-tools.dylib \
(compatibility version 1.0.0, current version 1.0.0)
\t@rpath/librustpython_capi_extra.dylib \
(compatibility version 1.0.0, current version 1.0.0)
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(dependencies, "_malloc\n"),
    )

    assert report["status"] == "pass"
    assert report["findings"] == []


def test_elf_python_dependency_and_undefined_symbols_fail(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    symbols = """\
                 U PyBytes_AsString
                 U _Py_Dealloc@PYTHON_3.13
                 U malloc@GLIBC_2.2.5
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf", "nm"),
        runner=_runner(_ELF_PYTHON, symbols),
    )

    assert report["status"] == "fail"
    assert report["imported_python_symbols"] == [
        "PyBytes_AsString",
        "_Py_Dealloc@PYTHON_3.13",
    ]
    assert report["findings"] == [
        "forbidden Python runtime dependency: libpython3.13.so.1.0",
        "forbidden Python runtime symbol import: PyBytes_AsString",
        "forbidden Python runtime symbol import: _Py_Dealloc@PYTHON_3.13",
    ]


def test_elf_pyo3_alternate_runtime_dependencies_fail_without_symbols(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
Dynamic section at offset 0x1 contains 3 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libpypy3.11-c.so.1]
 0x0000000000000001 (NEEDED)             Shared library: [libpython-native.so]
 0x0000000000000001 (NEEDED)             Shared library: [librustpython_capi.so]
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf", "nm"),
        runner=_runner(dependencies, "                 U malloc@GLIBC_2.2.5\n"),
    )

    assert report["status"] == "fail"
    assert report["findings"] == [
        "forbidden Python runtime dependency: libpypy3.11-c.so.1",
        "forbidden Python runtime dependency: libpython-native.so",
        "forbidden Python runtime dependency: librustpython_capi.so",
    ]


def test_elf_python_runtime_lookalikes_do_not_false_positive(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
Dynamic section at offset 0x1 contains 3 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libpypy3.11-c-helper.so]
 0x0000000000000001 (NEEDED)             Shared library: [libpython-native-tools.so]
 0x0000000000000001 (NEEDED)             Shared library: [librustpython_capi_extra.so]
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf", "nm"),
        runner=_runner(dependencies, "                 U malloc@GLIBC_2.2.5\n"),
    )

    assert report["status"] == "pass"
    assert report["findings"] == []


def test_linux_symbol_inspector_falls_back_to_readelf(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    symbols = """\
Symbol table '.dynsym' contains 3 entries:
   Num:    Value          Size Type    Bind   Vis      Ndx Name
     0: 0000000000000000     0 NOTYPE  LOCAL  DEFAULT  UND
     1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND PyErr_SetString
     2: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND malloc@GLIBC_2.2.5
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf"),
        runner=_runner(_ELF_CLEAN, symbols),
    )

    assert report["status"] == "fail"
    assert _inspectors(report)["symbols"]["tool"] == "readelf"
    assert report["findings"] == ["forbidden Python runtime symbol import: PyErr_SetString"]


def test_linux_dependency_falls_back_to_objdump_with_nm_symbols(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    output = """\
fixture:     file format elf64-x86-64
  NEEDED               libc.so.6
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("objdump", "nm"),
        runner=_runner(output, "                 U malloc@GLIBC_2.2.5\n"),
    )

    assert report["status"] == "pass"
    assert _inspectors(report)["dependencies"]["tool"] == "objdump"
    assert report["dependencies"] == ["libc.so.6"]


def test_windows_dumpbin_dependency_and_import_symbols_fail(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    dependencies = _PE_DUMPBIN_CLEAN.replace(
        "    KERNEL32.dll\n",
        "    KERNEL32.dll\n    python313t.dll\n",
    )
    symbols = """\
Microsoft (R) COFF/PE Dumper
Section contains the following imports:

    BRIDGE.dll
              42    0 00001000 __imp_PyGILState_Ensure
              43    1 00001008 GetLastError

Summary
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("dumpbin"),
        runner=_runner(dependencies, symbols),
    )

    assert report["status"] == "fail"
    assert report["findings"] == [
        "forbidden Python runtime dependency: python313t.dll",
        "forbidden Python runtime symbol import: __imp_PyGILState_Ensure",
    ]


def test_windows_llvm_readobj_symbol_fallback(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
fixture.exe:     file format pei-x86-64
        DLL Name: KERNEL32.dll
"""
    symbols = """\
File: fixture.exe
Format: COFF-x86-64
DelayImport {
  Name: BRIDGE.dll
  Symbol: _Py_Dealloc (42)
}
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("llvm-readobj"),
        runner=_runner(dependencies, symbols),
    )

    assert report["status"] == "fail"
    assert _inspectors(report)["dependencies"]["tool"] == "llvm-readobj"
    assert _inspectors(report)["symbols"]["tool"] == "llvm-readobj"
    assert report["dependencies"] == ["BRIDGE.dll"]
    assert report["findings"] == ["forbidden Python runtime symbol import: _Py_Dealloc"]


def test_windows_llvm_readobj_detects_ordinal_only_python_delay_dependency(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)
    output = """\
File: fixture.exe
Format: COFF-x86-64
DelayImport {
  Name: python313t.dll
  Attributes: 0x1
  Symbol: 42
}
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("llvm-readobj"),
        runner=_runner(output, output),
    )

    assert report["status"] == "fail"
    assert report["dependencies"] == ["python313t.dll"]
    assert report["imported_python_symbols"] == []
    assert report["findings"] == ["forbidden Python runtime dependency: python313t.dll"]


def test_windows_dumpbin_checks_delay_load_dependencies_and_symbols(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
Microsoft (R) COFF/PE Dumper
Image has the following dependencies:

    KERNEL32.dll

Image has the following delay load dependencies:

    python313t.dll

Summary
"""
    symbols = """\
Microsoft (R) COFF/PE Dumper
Section contains the following imports:

    KERNEL32.dll
              42 GetLastError

Section contains the following delay load imports:

    python313t.dll
    0000000140039000 Import Address Table
    0000000140030508 Import Name Table
                     0 time date stamp

    nonstandard-layout [__imp__PyGILState_Ensure@4]

Summary
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("dumpbin"),
        runner=_runner(dependencies, symbols),
    )

    assert report["status"] == "fail"
    assert report["findings"] == [
        "forbidden Python runtime dependency: python313t.dll",
        "forbidden Python runtime symbol import: __imp__PyGILState_Ensure@4",
    ]


def test_windows_pyo3_alternate_delay_dependencies_fail_without_named_symbols(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
Microsoft (R) COFF/PE Dumper
Image has the following dependencies:

    KERNEL32.dll

Image has the following delay load dependencies:

    libpypy3.11-c.dll
    python-native.dll
    rustpython_capi.dll

Summary
"""
    symbols = """\
Microsoft (R) COFF/PE Dumper
Section contains the following delay load imports:

    libpypy3.11-c.dll
              42

Summary
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("dumpbin"),
        runner=_runner(dependencies, symbols),
    )

    assert report["status"] == "fail"
    assert report["imported_python_symbols"] == []
    assert report["findings"] == [
        "forbidden Python runtime dependency: libpypy3.11-c.dll",
        "forbidden Python runtime dependency: python-native.dll",
        "forbidden Python runtime dependency: rustpython_capi.dll",
    ]


def test_windows_python_runtime_lookalikes_do_not_false_positive(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    dependencies = """\
Microsoft (R) COFF/PE Dumper
Image has the following dependencies:

    libpypy3.11-c-helper.dll
    python-native-tools.dll
    rustpython_capi_extra.dll

Summary
"""

    report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("dumpbin"),
        runner=_runner(dependencies, _PE_DUMPBIN_IMPORTS_CLEAN),
    )

    assert report["status"] == "pass"
    assert report["findings"] == []


def test_windows_objdump_only_fails_closed_without_delay_import_coverage(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)

    report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("objdump"),
    )

    assert report["status"] == "not-run"
    assert report["reason"] == (
        "no supported windows dependency inspector is available (tried: dumpbin, llvm-readobj)"
    )


def test_missing_platform_tools_are_explicit_not_run_and_cli_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _binary(tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    report = audit_binary_linkage(binary, platform_name="Linux", locator=lambda _name: None)

    assert report["status"] == "not-run"
    assert report["reason"] == (
        "no supported linux dependency inspector is available (tried: readelf, objdump)"
    )
    monkeypatch.setattr("platform.system", lambda: "Linux")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    arguments = ("--binary", str(binary), "--expected-runner-sha256", digest)
    assert main(arguments) == 1
    assert main((*arguments, "--allow-partial")) == 0


def test_missing_symbol_tool_is_explicit_not_run(tmp_path: Path) -> None:
    binary = _binary(tmp_path)

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool"),
    )

    assert report["status"] == "not-run"
    assert report["reason"] == ("no supported darwin symbol inspector is available (tried: nm)")


def test_dependency_and_symbol_inspector_failures_fail_closed(tmp_path: Path) -> None:
    binary = _binary(tmp_path)

    rejected = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf", "nm"),
        runner=_runner("", "", dependency_returncode=1),
    )
    unknown = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf", "nm"),
        runner=_runner(_ELF_CLEAN, "unexpected successful output"),
    )

    assert rejected["status"] == "fail"
    assert rejected["findings"] == ["readelf: dependency inspector exited with status 1"]
    assert unknown["status"] == "fail"
    assert unknown["findings"] == ["nm: successful symbol output format was not recognized"]


def test_malformed_dependency_and_symbol_rows_fail_closed(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    malformed_needed = _ELF_CLEAN.replace(
        "Shared library: [libc.so.6]",
        "Shared library: libpython3.13.so.1.0",
    )
    malformed_symbol = """\
Symbol table '.dynsym' contains 2 entries:
   Num:    Value          Size Type    Bind   Vis      Ndx Name
     0: 0000000000000000     0 NOTYPE  LOCAL  DEFAULT  UND
     1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND PyErr_SetString unexpected
"""

    dependency_report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf", "nm"),
        runner=_runner(malformed_needed, ""),
    )
    symbol_report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("readelf"),
        runner=_runner(_ELF_CLEAN, malformed_symbol),
    )

    assert dependency_report["status"] == "fail"
    assert dependency_report["findings"] == [
        "readelf: successful dependency output format was not recognized"
    ]
    assert symbol_report["status"] == "fail"
    assert symbol_report["findings"] == [
        "readelf: successful symbol output format was not recognized"
    ]


def test_malformed_objdump_and_llvm_readobj_rows_fail_closed(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    malformed_dependency = """\
fixture:     file format elf64-x86-64
  NEEDED
"""
    malformed_symbol = """\
File: fixture.exe
Format: COFF-x86-64
Import {
  Name: BRIDGE.dll
  Symbol: PyErr_SetString unexpected-metadata
}
"""

    dependency_report = audit_binary_linkage(
        binary,
        platform_name="Linux",
        locator=_locator("objdump", "nm"),
        runner=_runner(malformed_dependency, malformed_symbol),
    )
    symbol_report = audit_binary_linkage(
        binary,
        platform_name="Windows",
        locator=_locator("llvm-readobj"),
        runner=_runner(malformed_symbol, malformed_symbol),
    )

    assert dependency_report["status"] == "fail"
    assert dependency_report["findings"] == [
        "objdump: successful dependency output format was not recognized"
    ]
    assert symbol_report["status"] == "fail"
    assert symbol_report["findings"] == [
        "llvm-readobj: successful symbol output format was not recognized"
    ]


def test_binary_change_between_inspectors_stops_before_second_tool(tmp_path: Path) -> None:
    binary = _binary(tmp_path)
    calls = 0

    def mutate(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls != 1:
            pytest.fail("symbol inspection must not run after the binary binding changes")
        binary.write_bytes(b"replacement")
        return subprocess.CompletedProcess(command, 0, stdout=_MACHO_CLEAN, stderr="")

    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=mutate,
    )

    assert report["status"] == "fail"
    assert report["findings"] == ["binary: content changed during linkage inspection"]
    assert calls == 1
    assert set(_inspectors(report)) == {"dependencies"}


def test_default_runner_binds_oversized_output_without_loading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _binary(tmp_path)
    payload = b"x" * (4 * 1024**2 + 1)

    def emit(
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        stdout = cast(BinaryIO, kwargs["stdout"])
        stdout.write(payload)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", emit)
    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
    )

    assert report["status"] == "fail"
    assert report["findings"] == ["otool: dependency inspector output exceeds limit"]
    stdout = _inspectors(report)["dependencies"]["stdout"]
    assert stdout == {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_digest_mismatch_and_unsupported_platform_cannot_pass(tmp_path: Path) -> None:
    binary = _binary(tmp_path)

    mismatch = audit_binary_linkage(
        binary,
        expected_runner_sha256="0" * 64,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(_MACHO_CLEAN, "_malloc\n"),
    )
    unsupported = audit_binary_linkage(binary, platform_name="Plan9")

    assert mismatch["status"] == "fail"
    assert mismatch["findings"] == ["binary: SHA-256 differs from expected binding"]
    assert unsupported["status"] == "not-run"
    assert unsupported["reason"] == "unsupported linkage-audit platform: Plan9"


def test_missing_binary_fails_before_tool_selection(tmp_path: Path) -> None:
    report = audit_binary_linkage(tmp_path / "missing", platform_name="Linux")

    assert report["status"] == "fail"
    assert report["reason"] == "the supplied binary could not be inspected"
    assert report["findings"] == ["binary: cannot bind input: FileNotFoundError"]


def test_evidence_binds_auditor_source_and_runtime_identity_covers_it(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path)
    report = audit_binary_linkage(
        binary,
        platform_name="Darwin",
        locator=_locator("otool", "nm"),
        runner=_runner(_MACHO_CLEAN, "_malloc\n"),
    )
    source = report["source_identity"]

    assert isinstance(source, dict)
    assert source["schema"] == SOURCE_IDENTITY_SCHEMA
    assert source["input_count"] == 1
    rows = source["inputs"]
    assert isinstance(rows, list)
    assert rows[0]["path"] == "tools/benchmark/comparators/linkage_audit.py"
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert (
        source["sha256"]
        == hashlib.sha256(b"pyowl-core:comparator-linkage-source:v1\0" + canonical).hexdigest()
    )

    runtime_rows = comparator_source_identity()["inputs"]
    assert isinstance(runtime_rows, list)
    assert any(
        row["path"] == "tools/benchmark/comparators/linkage_audit.py"
        for row in runtime_rows
        if isinstance(row, dict)
    )


def _binary(tmp_path: Path) -> Path:
    binary = tmp_path / "direct"
    binary.write_bytes(b"fixture executable")
    return binary


def _locator(*available: str) -> Callable[[str], str | None]:
    selected = set(available)
    return lambda name: f"/tools/{name}" if name in selected else None


def _runner(
    dependency_stdout: str,
    symbol_stdout: str = "",
    *,
    dependency_returncode: int = 0,
    symbol_returncode: int = 0,
) -> Callable[[Sequence[str]], subprocess.CompletedProcess[str]]:
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        executable = Path(command[0]).name
        arguments = set(command[1:-1])
        if executable.endswith("objdump") and "-p" in arguments:
            stdout = dependency_stdout
            returncode = dependency_returncode
        elif arguments & {"-u", "--undefined-only", "--dyn-syms", "/IMPORTS", "--coff-imports"}:
            stdout = symbol_stdout
            returncode = symbol_returncode
        else:
            stdout = dependency_stdout
            returncode = dependency_returncode
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=stdout,
            stderr="",
        )

    return run


def _output_binding(value: str) -> dict[str, object]:
    payload = value.encode("utf-8")
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _inspectors(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], report["inspectors"])
