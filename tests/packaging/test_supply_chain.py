from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools.packaging import supply_chain
from tools.packaging.supply_chain import (
    build_cyclonedx,
    build_dependency_inventory,
    build_provenance,
    generate_evidence,
    load_locked_packages,
    validate_inventory,
)
from tools.packaging.supply_chain import (
    main as supply_chain_main,
)

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PROVENANCE_INPUTS = {
    ".github/workflows/ci.yml",
    ".github/workflows/native-safety.yml",
    ".github/workflows/release.yml",
    ".github/workflows/wheels.yml",
    "MANIFEST.in",
    "native/Cargo.lock",
    "native/Cargo.toml",
    "native/build.rs",
    "pyowl_build.py",
    "pyproject.toml",
    "schemas/encoded-view-v1.json",
    "schemas/encoded-view-v1.toml",
    "schemas/encoded-view-v2.json",
    "schemas/encoded-view-v2.toml",
    "schemas/model-v2.toml",
    "schemas/version-decision-v2.toml",
    "setup.py",
    "tools/__init__.py",
    "tools/packaging/__init__.py",
    "tools/packaging/artifact_inspector.py",
    "tools/packaging/import_probe.py",
    "tools/packaging/platform_audit.py",
    "tools/packaging/release_report.py",
    "tools/packaging/release_tag.py",
    "tools/packaging/supply_chain.py",
}


def test_sdist_manifest_covers_native_build_inputs_and_prunes_outputs() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

    assert "include native/build.rs" in manifest
    assert "recursive-include native/src *.rs" in manifest
    assert "recursive-include native/tests *.rs" in manifest
    assert "prune tests/fuzz/native/target" in manifest
    assert "prune tests/miri/native/target" in manifest


def _copy_dependency_manifests(target: Path) -> None:
    shutil.copytree(ROOT / "THIRD_PARTY_LICENSES", target / "THIRD_PARTY_LICENSES")
    shutil.copy2(ROOT / "NOTICE", target / "NOTICE")
    (target / "native").mkdir()
    shutil.copy2(ROOT / "native" / "Cargo.lock", target / "native" / "Cargo.lock")
    shutil.copy2(ROOT / "native" / "Cargo.toml", target / "native" / "Cargo.toml")


def _copy_build_inputs(target: Path) -> None:
    for relative_path in supply_chain._BUILD_INPUT_PATHS:
        destination = target / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative_path, destination)


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
    assert inventory["release_blockers"] == [
        "third-party license review requires release-owner or counsel approval"
    ]


def test_release_mode_requires_explicit_legal_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "evidence"

    assert (
        supply_chain_main(
            [
                "--root",
                str(ROOT),
                "--output-dir",
                str(output_dir),
                "--require-approval",
            ]
        )
        == 1
    )
    assert not output_dir.exists()
    assert (
        "release: third-party license review requires release-owner or counsel approval"
        in capsys.readouterr().out
    )


def test_pure_and_native_sboms_are_variant_exact_and_deterministic() -> None:
    pure = build_cyclonedx(ROOT, "pure")
    native = build_cyclonedx(ROOT, "native")
    assert pure == build_cyclonedx(ROOT, "pure")
    assert pure["components"] == []
    assert pure["dependencies"][0]["dependsOn"] == []
    assert len(native["components"]) == 14
    assert native["metadata"]["component"]["version"] == "0.2.0"
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
        "pure_ci_image": (
            "python:3.10-slim@sha256:"
            "e8d6cdadc17ce7146e1bb286e6093d58c8cf582659a558ad51cd103829655e72"
        ),
    }
    inputs = provenance["inputs"]
    assert set(inputs) == EXPECTED_PROVENANCE_INPUTS
    for relative_path in EXPECTED_PROVENANCE_INPUTS:
        payload = (ROOT / relative_path).read_bytes()
        assert inputs[relative_path] == {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }


def test_build_provenance_parses_and_hashes_the_same_captured_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_build_inputs(tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "wheels.yml"
    original = workflow_path.read_bytes()
    replacement = original.replace(b"build==1.5.0", b"build==9.9.9")
    assert replacement != original
    original_pin = supply_chain._workflow_pin
    mutated = False

    def mutate_after_capture(text: str, pattern: str, label: str) -> str:
        nonlocal mutated
        if not mutated:
            workflow_path.write_bytes(replacement)
            mutated = True
        return original_pin(text, pattern, label)

    monkeypatch.setattr(supply_chain, "_workflow_pin", mutate_after_capture)

    provenance = build_provenance(tmp_path)

    assert mutated
    assert provenance["tools"]["python_build_frontend"] == "build==1.5.0"
    assert provenance["inputs"][".github/workflows/wheels.yml"] == {
        "bytes": len(original),
        "sha256": hashlib.sha256(original).hexdigest(),
    }
    assert workflow_path.read_bytes() == replacement


def test_build_provenance_rejects_symlinked_inputs(tmp_path: Path) -> None:
    _copy_build_inputs(tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "wheels.yml"
    target = tmp_path / "captured-wheels.yml"
    workflow_path.replace(target)
    try:
        workflow_path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="regular non-symlink file"):
        build_provenance(tmp_path)


def test_build_provenance_rejects_a_mutable_pure_ci_image(tmp_path: Path) -> None:
    _copy_build_inputs(tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    pinned = (
        "python:3.10-slim@sha256:e8d6cdadc17ce7146e1bb286e6093d58c8cf582659a558ad51cd103829655e72"
    )
    assert pinned in workflow
    workflow_path.write_text(workflow.replace(pinned, "python:3.10-slim"), encoding="utf-8")

    with pytest.raises(ValueError, match="pure-package CI image"):
        build_provenance(tmp_path)


def test_stable_build_input_reader_rejects_concurrent_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "input.txt"
    path.write_bytes(b"captured")
    original_lstat = Path.lstat
    inspections = 0

    def mutate_before_final_identity(selected: Path):
        nonlocal inspections
        if selected == path:
            inspections += 1
            if inspections == 2:
                selected.write_bytes(b"changed-after-read")
        return original_lstat(selected)

    monkeypatch.setattr(Path, "lstat", mutate_before_final_identity)

    with pytest.raises(ValueError, match="changed while reading"):
        supply_chain._read_stable_regular_file(path, label="input.txt")


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
        "pyowl-core-native 0.2.0; found "
        "pyowl-core-native 0.2.0, untracked-path-component 9.9.9"
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


def test_inventory_legal_approval_requires_a_toml_boolean(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    inventory_path = tmp_path / "THIRD_PARTY_LICENSES" / "inventory.toml"
    inventory = inventory_path.read_text(encoding="utf-8")
    inventory_path.write_text(
        inventory.replace("legal_approval = false", 'legal_approval = "false"'),
        encoding="utf-8",
    )

    assert validate_inventory(tmp_path) == [
        "inventory: cannot load THIRD_PARTY_LICENSES/inventory.toml: "
        "inventory legal_approval must be a boolean"
    ]


def test_inventory_rejects_unreviewed_spdx_expression(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    inventory_path = tmp_path / "THIRD_PARTY_LICENSES" / "inventory.toml"
    inventory = inventory_path.read_text(encoding="utf-8")
    inventory_path.write_text(
        inventory.replace(
            'license = "MIT OR Apache-2.0"',
            'license = "MIT"',
            1,
        ),
        encoding="utf-8",
    )

    assert "inventory: unreviewed SPDX expression 'MIT' for heck" in validate_inventory(tmp_path)


def test_inventory_rejects_license_selection_outside_reviewed_policy(
    tmp_path: Path,
) -> None:
    _copy_dependency_manifests(tmp_path)
    inventory_path = tmp_path / "THIRD_PARTY_LICENSES" / "inventory.toml"
    inventory = inventory_path.read_text(encoding="utf-8")
    inventory_path.write_text(
        inventory.replace(
            'selected_license = "Apache-2.0"',
            'selected_license = "MIT"',
            1,
        ),
        encoding="utf-8",
    )

    assert (
        "inventory: selected license does not match reviewed SPDX expression "
        "for heck 0.5.0; expected 'Apache-2.0', got 'MIT'" in validate_inventory(tmp_path)
    )


def test_inventory_rejects_unsafe_additional_license_path(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    inventory_path = tmp_path / "THIRD_PARTY_LICENSES" / "inventory.toml"
    inventory = inventory_path.read_text(encoding="utf-8")
    inventory_path.write_text(
        inventory.replace(
            'additional_license_file = "THIRD_PARTY_LICENSES/LLVM-exception.txt"',
            'additional_license_file = "THIRD_PARTY_LICENSES/../NOTICE"',
        ),
        encoding="utf-8",
    )

    assert (
        "inventory: unsafe additional license path 'THIRD_PARTY_LICENSES/../NOTICE'"
        in validate_inventory(tmp_path)
    )


def test_inventory_rejects_unreferenced_legal_payload(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    (tmp_path / "THIRD_PARTY_LICENSES" / "unreviewed.txt").write_text(
        "unreviewed",
        encoding="utf-8",
    )

    assert validate_inventory(tmp_path) == [
        "inventory: unreferenced legal payload files ['unreviewed.txt']"
    ]


def test_lock_checksum_requires_a_toml_string(tmp_path: Path) -> None:
    _copy_dependency_manifests(tmp_path)
    lock_path = tmp_path / "native" / "Cargo.lock"
    lock = lock_path.read_text(encoding="utf-8")
    lock_path.write_text(
        lock.replace(
            'checksum = "2304e00983f87ffb38b55b444b5e3b60a884b5d30c0fca7d82fe33449bbe55ea"',
            "checksum = 2304",
            1,
        ),
        encoding="utf-8",
    )

    assert validate_inventory(tmp_path) == [
        "inventory: cannot load native dependency manifests: Cargo.lock checksum must be a string"
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
