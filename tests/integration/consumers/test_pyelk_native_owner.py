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
    assert set(observed["formats"]) == {"functional", "rdfxml", "turtle"}
    assert set(observed["owners"]) == {"functional", "rdfxml", "turtle"}
    digests = set()
    for result in observed["formats"].values():
        assert result["encoded_buffers"] == 11
        assert result["encoded_exporters"] == 1
        assert result["parser_bytes"] > 0
        assert result["public_operations"] == 4
        assert result["scalar_facade_rows"] == 0
        assert len(result["compiler_digest"]) == 64
        digests.add(result["compiler_digest"])
    assert len(digests) == 1
    semantic_owners = {"direct", "decoded", "mmap", "overlay"}
    expected_request_deltas = {
        "direct": [1],
        "decoded": [0],
        "mmap": [0],
        "overlay": [0],
        "composite": [0, 1],
    }
    direct_fingerprint_accesses = {
        "structural_fingerprint": 0,
        "logical_fingerprint": 1,
        "signature_fingerprint": 1,
    }
    for matrix in observed["owners"].values():
        assert set(matrix) == {"direct", "decoded", "mmap", "overlay", "composite"}
        assert {matrix[owner]["compiler_digest"] for owner in semantic_owners} == digests
        assert len({matrix[owner]["encoded_buffer_bytes"] for owner in semantic_owners}) == 1
        for owner, result in matrix.items():
            assert result["public_operations"] == 4
            assert result["staging_copy_bytes"] == 0
            assert result["zero_copy_buffers"] == result["encoded_buffers"]
            assert result["encoded_view_request_deltas"] == expected_request_deltas[owner]
            assert len(result["compiler_digest"]) == 64
            if owner in {"direct", "decoded", "mmap"}:
                assert result["encoded_buffers"] == 11
                assert result["segments"] == 1
                assert result["referenced_views"] == 0
            elif owner == "overlay":
                assert result["encoded_buffers"] == 11
                assert result["segments"] == 2
                assert result["referenced_views"] == 1
            else:
                assert result["encoded_buffers"] == 22
                assert result["segments"] == 4
                assert result["referenced_views"] == 2
            if owner == "mmap":
                assert result["detached_buffers"] == 0
                assert result["indexed_buffers"] == 11
            else:
                assert result["detached_buffers"] == result["encoded_buffers"]
                assert result["indexed_buffers"] == 0
            if owner == "composite":
                assert result["fingerprint_accesses"] == {
                    "structural_fingerprint": 0,
                    "logical_fingerprint": 0,
                    "signature_fingerprint": 0,
                }
            else:
                assert result["fingerprint_accesses"] == direct_fingerprint_accesses
    functional = observed["owners"]["functional"]
    rdfxml = observed["owners"]["rdfxml"]
    for owner in (*sorted(semantic_owners), "composite"):
        assert functional[owner]["compiler_digest"] == rdfxml[owner]["compiler_digest"]
        assert functional[owner]["encoded_buffer_bytes"] == rdfxml[owner]["encoded_buffer_bytes"]
