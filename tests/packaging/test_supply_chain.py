from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from tools.packaging.supply_chain import (
    build_cyclonedx,
    build_dependency_inventory,
    build_provenance,
    generate_evidence,
    load_locked_packages,
    validate_inventory,
)

ROOT = Path(__file__).resolve().parents[2]


def _copy_dependency_manifests(target: Path) -> None:
    shutil.copytree(ROOT / "THIRD_PARTY_LICENSES", target / "THIRD_PARTY_LICENSES")
    shutil.copy2(ROOT / "NOTICE", target / "NOTICE")
    (target / "native").mkdir()
    shutil.copy2(ROOT / "native" / "Cargo.lock", target / "native" / "Cargo.lock")
    shutil.copy2(ROOT / "native" / "Cargo.toml", target / "native" / "Cargo.toml")


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


def test_build_provenance_binds_exact_toolchain_and_lock_hash() -> None:
    provenance = build_provenance(ROOT)

    assert provenance["schema"] == "pyowl-core.build-provenance/1"
    assert provenance["source_date_epoch"] == 1_735_689_600
    assert provenance["tools"] == {
        "rust_toolchain": "1.83.0",
        "cargo_manifest_rust_version": "1.83",
        "python_build_frontend": "build==1.5.0",
        "python_build_backend": "setuptools==83.0.0",
        "wheel_builder": "wheel==0.45.1",
        "cibuildwheel_action": ("pypa/cibuildwheel@294735312765b09d24a2fbec22660ce817587d55"),
    }
    lock = (ROOT / "native" / "Cargo.lock").read_bytes()
    assert provenance["inputs"]["native/Cargo.lock"] == {
        "bytes": len(lock),
        "sha256": hashlib.sha256(lock).hexdigest(),
    }


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
    _copy_dependency_manifests(tmp_path)
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


def test_inventory_rejects_source_less_component_omitted_from_sbom(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    lock_path = tmp_path / "native" / "Cargo.lock"
    lock = lock_path.read_text(encoding="utf-8")
    lock += '\n[[package]]\nname = "untracked-path-component"\nversion = "9.9.9"\n'
    lock_path.write_text(lock, encoding="utf-8")

    assert validate_inventory(tmp_path) == [
        "inventory: source-less lock packages must contain only "
        "pyowl-core-native 0.1.0-dev.0; found "
        "pyowl-core-native 0.1.0-dev.0, untracked-path-component 9.9.9"
    ]


def test_inventory_cannot_redirect_the_reviewed_lockfile(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    inventory_path = tmp_path / "THIRD_PARTY_LICENSES" / "inventory.toml"
    inventory = inventory_path.read_text(encoding="utf-8")
    inventory_path.write_text(
        inventory.replace(
            'lockfile = "native/Cargo.lock"',
            'lockfile = "../unreviewed/Cargo.lock"',
        ),
        encoding="utf-8",
    )

    assert validate_inventory(tmp_path) == [
        "inventory: lockfile must be exactly native/Cargo.lock, got '../unreviewed/Cargo.lock'"
    ]


def test_inventory_rejects_notice_component_drift(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    notice_path = tmp_path / "NOTICE"
    notice = notice_path.read_text(encoding="utf-8")
    notice_path.write_text(
        notice.replace("- pyo3 0.29.0: Apache-2.0 [native-runtime]\n", ""),
        encoding="utf-8",
    )

    assert validate_inventory(tmp_path) == [
        "inventory: NOTICE native component block does not match inventory.toml"
    ]
