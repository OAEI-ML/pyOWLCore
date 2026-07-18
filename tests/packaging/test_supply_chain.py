from __future__ import annotations

import json
import shutil
from pathlib import Path

from tools.packaging.supply_chain import (
    build_cyclonedx,
    build_dependency_inventory,
    generate_evidence,
    load_locked_packages,
    validate_inventory,
)

ROOT = Path(__file__).resolve().parents[2]


def test_reviewed_inventory_exactly_matches_cargo_lock() -> None:
    assert validate_inventory(ROOT) == []
    inventory = build_dependency_inventory(ROOT)
    locked = [
        package
        for package in load_locked_packages(ROOT / "native" / "Cargo.lock")
        if package.source is not None
    ]
    assert len(inventory["native_components"]) == len(locked) == 14
    assert inventory["python_runtime_dependencies"] == []
    assert inventory["java_components"] == []
    assert inventory["legal_approval"] is False


def test_pure_and_native_sboms_are_variant_exact_and_deterministic() -> None:
    pure = build_cyclonedx(ROOT, "pure")
    native = build_cyclonedx(ROOT, "native")
    assert pure == build_cyclonedx(ROOT, "pure")
    assert pure["components"] == []
    assert pure["dependencies"][0]["dependsOn"] == []
    assert len(native["components"]) == 14
    assert native["metadata"]["component"]["version"] == "0.1.0.dev0"
    assert all("pkg:cargo/" in component["bom-ref"] for component in native["components"])
    assert all(component["hashes"][0]["alg"] == "SHA-256" for component in native["components"])


def test_generated_evidence_check_detects_and_reports_drift(tmp_path: Path) -> None:
    assert generate_evidence(ROOT, tmp_path) == []
    assert generate_evidence(ROOT, tmp_path, check=True) == []
    path = tmp_path / "sbom-pure.cdx.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["version"] = 99
    path.write_text(json.dumps(document), encoding="utf-8")
    assert generate_evidence(ROOT, tmp_path, check=True) == [
        f"supply-chain: generated evidence drift {path}"
    ]


def test_inventory_rejects_unreviewed_lock_component(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "THIRD_PARTY_LICENSES", tmp_path / "THIRD_PARTY_LICENSES")
    (tmp_path / "native").mkdir()
    lock = (ROOT / "native" / "Cargo.lock").read_text(encoding="utf-8")
    lock += (
        "\n[[package]]\n"
        'name = "unexpected-crate"\n'
        'version = "9.9.9"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        'checksum = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
    )
    (tmp_path / "native" / "Cargo.lock").write_text(lock, encoding="utf-8")
    assert validate_inventory(tmp_path) == [
        "inventory: unreviewed locked component unexpected-crate 9.9.9"
    ]
