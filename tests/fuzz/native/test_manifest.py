from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by Python 3.10
    import tomli as tomllib


def test_native_fuzz_manifest_pins_targets_and_unwind_policy() -> None:
    root = Path(__file__).parent
    with (root / "Cargo.toml").open("rb") as stream:
        manifest = tomllib.load(stream)
    assert manifest["dependencies"]["libfuzzer-sys"] == "=0.4.13"
    assert manifest["dependencies"]["pyo3"]["version"] == "=0.29.0"
    assert manifest["profile"]["release"]["panic"] == "unwind"
    targets = manifest["bin"]
    assert {target["name"] for target in targets} == {"functional", "wire"}
    for target in targets:
        assert (root / target["path"]).is_file()
