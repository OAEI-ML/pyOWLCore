from __future__ import annotations

import re
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


def test_native_fuzz_targets_track_the_current_production_module_graph() -> None:
    root = Path(__file__).parent
    functional = (root / "fuzz_targets" / "functional.rs").read_text(encoding="utf-8")
    wire = (root / "fuzz_targets" / "wire.rs").read_text(encoding="utf-8")
    parser = (root.parents[2] / "native" / "src" / "parse" / "mod.rs").read_text(
        encoding="utf-8"
    )
    cancellation = (root.parents[2] / "native" / "src" / "cancel.rs").read_text(
        encoding="utf-8"
    )
    for target in (functional, wire):
        assert "native/src/hash.rs" in target
        assert "native/src/model/mod.rs" in target
    assert "#[cfg(not(fuzzing))]\nmod retained;" in parser
    assert (
        '#[cfg(any(fuzzing, not(feature = "extension-module")))]\n'
        "pub(crate) type InterruptSlot = Arc<()>;"
    ) in cancellation


def test_wire_fuzzer_uses_the_production_abi_version() -> None:
    repository = Path(__file__).parents[3]
    production = (repository / "native" / "src" / "lib.rs").read_text(encoding="utf-8")
    harness = (
        Path(__file__).parent / "fuzz_targets" / "wire.rs"
    ).read_text(encoding="utf-8")
    pattern = re.compile(r"(?:pub\(crate\)\s+)?const ABI_VERSION: u32 = (\d+);")
    production_match = pattern.search(production)
    harness_match = pattern.search(harness)
    assert production_match is not None
    assert harness_match is not None
    assert production_match.group(1) == harness_match.group(1)


def test_miri_harness_is_dependency_free_and_locked() -> None:
    root = Path(__file__).parents[2] / "miri" / "native"
    with (root / "Cargo.toml").open("rb") as stream:
        manifest = tomllib.load(stream)
    assert "dependencies" not in manifest
    assert (root / "Cargo.lock").is_file()
    harness = (root / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "#![forbid(unsafe_code)]" in harness
    for module in ("canonical.rs", "error.rs", "hash.rs", "limits.rs"):
        assert f'native/src/{module}' in harness
