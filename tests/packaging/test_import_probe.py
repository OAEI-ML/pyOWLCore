from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.packaging.import_probe import _is_interpreter_bytecode_cache_write

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_REPORT = {
    "native_extension_loaded": False,
    "ok": True,
    "package": "pyowl_core",
    "schema": 1,
    "version": "0.1.0",
    "violations": [],
}


def test_import_probe_recognizes_only_tagged_interpreter_bytecode_cache_writes() -> None:
    cache_tag = sys.implementation.cache_tag
    assert cache_tag is not None
    assert _is_interpreter_bytecode_cache_write(
        f"/installed/pyowl_core/__pycache__/__init__.{cache_tag}.pyc"
    )
    assert _is_interpreter_bytecode_cache_write(
        Rf"C:\installed\pyowl_core\__pycache__\model.{cache_tag}.opt-1.pyc"
    )
    assert not _is_interpreter_bytecode_cache_write(f"/installed/pyowl_core/model.{cache_tag}.pyc")
    assert not _is_interpreter_bytecode_cache_write(
        "/installed/pyowl_core/__pycache__/model.unrelated.pyc"
    )
    assert not _is_interpreter_bytecode_cache_write(
        f"/installed/pyowl_core/__pycache__/model.{cache_tag}.json"
    )


def test_import_probe_recognizes_pypy_310_bytecode_cache_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.implementation, "cache_tag", "pypy310")

    assert _is_interpreter_bytecode_cache_write(
        "/installed/pyowl_core/__pycache__/__init__.pypy310.pyc"
    )


def test_package_import_has_no_write_network_process_warning_or_eager_native_side_effect() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "tools.packaging.import_probe"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report == EXPECTED_REPORT


def test_import_probe_writes_requested_report_after_audited_import(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    output = tmp_path / "evidence" / "import-probe.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.packaging.import_probe",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    assert json.loads(output.read_text(encoding="utf-8")) == EXPECTED_REPORT
