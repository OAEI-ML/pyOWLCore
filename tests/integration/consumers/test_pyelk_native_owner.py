from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
WORKSPACE = ROOT.parent
RUNNER = Path(__file__).with_name("_pyelk_native_owner_runner.py")


def _artifact(environment_name: str, candidates: tuple[Path, ...]) -> Path:
    selected = os.environ.get(environment_name)
    if selected:
        path = Path(selected).resolve()
        if path.is_file():
            return path
        pytest.skip(f"{environment_name} does not name a native artifact")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    pytest.skip(f"optional workspace artifact {environment_name} is unavailable")


def test_real_retained_owner_crosses_pyelk_without_scalar_materialization() -> None:
    core = _artifact(
        "PYOWL_CORE_TEST_NATIVE_LIBRARY",
        (
            ROOT / "native" / "target" / "release" / "lib_native.dylib",
            ROOT / "native" / "target" / "debug" / "lib_native.dylib",
        ),
    )
    pyelk_root = WORKSPACE / "pyELK"
    pyelk = _artifact(
        "PYOWL_CORE_TEST_PYELK_NATIVE_LIBRARY",
        (
            pyelk_root / "target" / "release" / "lib_native.dylib",
            pyelk_root / "target" / "debug" / "lib_native.dylib",
        ),
    )
    if not (pyelk_root / "src" / "pyelk").is_dir():
        pytest.skip("optional pyELK workspace source is unavailable")

    environment = dict(os.environ)
    python_path = (
        str(ROOT / "src"),
        str(pyelk_root / "src"),
        str(ROOT),
        environment.get("PYTHONPATH", ""),
    )
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(value for value in python_path if value),
            "PYOWL_CORE_TEST_NATIVE_LIBRARY": str(core),
            "PYOWL_CORE_TEST_PYELK_NATIVE_LIBRARY": str(pyelk),
            "PYOWL_CORE_TEST_RETAINED_NATIVE_LOAD": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(RUNNER)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    observed = json.loads(completed.stdout)
    assert observed["encoded_buffers"] == 11
    assert observed["encoded_exporters"] == 1
    assert observed["scalar_facade_rows"] == 0
    assert len(observed["compiler_digest"]) == 64
