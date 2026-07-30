"""Validate the locked native dependency inventory and emit deterministic SBOMs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 lane
    import tomli as tomllib  # type: ignore[import-not-found, unused-ignore]

Variant = Literal["pure", "native"]
_LOCAL_NATIVE_CRATE = "pyowl-core-native"
_FORBIDDEN_COMPONENTS = {"deeponto", "jpype", "jpype1", "mowl", "owlapi", "robot"}
_NOTICE_INVENTORY_START = "<!-- pyowl-core-native-inventory:start -->"
_NOTICE_INVENTORY_END = "<!-- pyowl-core-native-inventory:end -->"
_LICENSE_SELECTION_POLICY = (
    "Apache-2.0 is selected where the resolved crate offers MIT OR Apache-2.0"
)
_LICENSE_SELECTIONS = {
    "Apache-2.0 OR MIT": "Apache-2.0",
    "MIT OR Apache-2.0": "Apache-2.0",
    "Apache-2.0 WITH LLVM-exception": "Apache-2.0 WITH LLVM-exception",
    "(MIT OR Apache-2.0) AND Unicode-3.0": "Apache-2.0 AND Unicode-3.0",
}
_ADDITIONAL_LICENSE_FILES = {
    "Apache-2.0 WITH LLVM-exception": "THIRD_PARTY_LICENSES/LLVM-exception.txt",
    "Apache-2.0 AND Unicode-3.0": "THIRD_PARTY_LICENSES/Unicode-3.0.txt",
}
_DEVELOPMENT_LICENSE_FILES = {"W3C-RDF-tests-BSD-3-Clause.txt"}
_BUILD_INPUT_PATHS = (
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
    "setup.py",
    "tools/__init__.py",
    "tools/packaging/__init__.py",
    "tools/packaging/artifact_inspector.py",
    "tools/packaging/import_probe.py",
    "tools/packaging/platform_audit.py",
    "tools/packaging/release_report.py",
    "tools/packaging/release_tag.py",
    "tools/packaging/supply_chain.py",
)


@dataclass(frozen=True, slots=True)
class LockedPackage:
    """One package resolved by Cargo.lock."""

    name: str
    version: str
    checksum: str | None
    dependencies: tuple[str, ...]
    source: str | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.version)

    @property
    def bom_ref(self) -> str:
        return f"pkg:cargo/{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class InventoryComponent:
    """Reviewed license declaration for one locked third-party crate."""

    name: str
    version: str
    license_expression: str
    selected_license: str
    scope: str
    additional_license_file: str | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.version)


@dataclass(frozen=True, slots=True)
class Inventory:
    """The checked third-party inventory and external approval state."""

    schema: int
    lockfile: str
    selection: str
    legal_approval: bool
    components: tuple[InventoryComponent, ...]


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        loaded = tomllib.load(stream)
    if not isinstance(loaded, dict):  # pragma: no cover - tomllib contract
        raise ValueError(f"{path} did not contain a TOML table")
    return loaded


def _required_string(values: dict[str, Any], field: str, *, context: str) -> str:
    value = values.get(field)
    if type(value) is not str or not value:
        raise ValueError(f"{context} {field} must be a non-empty string")
    return value


def load_locked_packages(path: Path) -> tuple[LockedPackage, ...]:
    """Load Cargo.lock without invoking Cargo or accessing a registry."""

    raw_packages = _load_toml(path).get("package", [])
    if not isinstance(raw_packages, list):
        raise ValueError("Cargo.lock package field must be an array")
    packages: list[LockedPackage] = []
    for raw in raw_packages:
        if not isinstance(raw, dict):
            raise ValueError("Cargo.lock package entry must be a table")
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(value, str) for value in dependencies
        ):
            raise ValueError("Cargo.lock dependencies must be strings")
        checksum = raw.get("checksum")
        source = raw.get("source")
        if checksum is not None and type(checksum) is not str:
            raise ValueError("Cargo.lock checksum must be a string")
        if source is not None and type(source) is not str:
            raise ValueError("Cargo.lock source must be a string")
        packages.append(
            LockedPackage(
                name=_required_string(raw, "name", context="Cargo.lock package"),
                version=_required_string(raw, "version", context="Cargo.lock package"),
                checksum=checksum,
                dependencies=tuple(dependencies),
                source=source,
            )
        )
    return tuple(packages)


def load_inventory(path: Path) -> Inventory:
    """Load the reviewed inventory with strict required fields."""

    raw = _load_toml(path)
    expected_root_fields = {
        "schema",
        "lockfile",
        "selection",
        "legal_approval",
        "component",
    }
    if set(raw) != expected_root_fields:
        raise ValueError(
            "inventory root fields must be exactly "
            f"{sorted(expected_root_fields)!r}, got {sorted(raw)!r}"
        )
    schema = raw.get("schema")
    legal_approval = raw.get("legal_approval")
    if type(schema) is not int:
        raise ValueError("inventory schema must be an integer")
    if type(legal_approval) is not bool:
        raise ValueError("inventory legal_approval must be a boolean")
    raw_components = raw.get("component", [])
    if not isinstance(raw_components, list):
        raise ValueError("inventory component field must be an array")
    components: list[InventoryComponent] = []
    for entry in raw_components:
        if not isinstance(entry, dict):
            raise ValueError("inventory component entry must be a table")
        required_fields = {"name", "version", "license", "selected_license", "scope"}
        allowed_fields = {*required_fields, "additional_license_file"}
        if not required_fields <= set(entry) or not set(entry) <= allowed_fields:
            raise ValueError("inventory component fields must be exactly the reviewed schema")
        additional = entry.get("additional_license_file")
        if additional is not None and (type(additional) is not str or not additional):
            raise ValueError("inventory additional_license_file must be a non-empty string")
        components.append(
            InventoryComponent(
                name=_required_string(entry, "name", context="inventory component"),
                version=_required_string(entry, "version", context="inventory component"),
                license_expression=_required_string(
                    entry,
                    "license",
                    context="inventory component",
                ),
                selected_license=_required_string(
                    entry,
                    "selected_license",
                    context="inventory component",
                ),
                scope=_required_string(entry, "scope", context="inventory component"),
                additional_license_file=additional,
            )
        )
    return Inventory(
        schema=schema,
        lockfile=_required_string(raw, "lockfile", context="inventory"),
        selection=_required_string(raw, "selection", context="inventory"),
        legal_approval=legal_approval,
        components=tuple(components),
    )


def _third_party(packages: tuple[LockedPackage, ...]) -> tuple[LockedPackage, ...]:
    return tuple(package for package in packages if package.source is not None)


def _notice_inventory_lines(inventory: Inventory) -> tuple[str, ...]:
    lines: list[str] = []
    for component in sorted(inventory.components, key=lambda item: item.key):
        line = (
            f"- {component.name} {component.version}: "
            f"{component.selected_license} [{component.scope}]"
        )
        if component.additional_license_file is not None:
            line += f"; additional terms: {component.additional_license_file}"
        lines.append(line)
    return tuple(lines)


def validate_notice(root: Path, inventory: Inventory) -> list[str]:
    """Require NOTICE's native component block to exactly match the review ledger."""

    try:
        lines = (root / "NOTICE").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return [f"inventory: cannot load NOTICE: {error}"]
    if lines.count(_NOTICE_INVENTORY_START) != 1 or lines.count(_NOTICE_INVENTORY_END) != 1:
        return ["inventory: NOTICE must contain exactly one native component block"]
    start = lines.index(_NOTICE_INVENTORY_START)
    end = lines.index(_NOTICE_INVENTORY_END)
    if end <= start or tuple(lines[start + 1 : end]) != _notice_inventory_lines(inventory):
        return ["inventory: NOTICE native component block does not match inventory.toml"]
    return []


def validate_inventory(root: Path) -> list[str]:
    """Return deterministic drift/license violations without network access."""

    inventory_path = root / "THIRD_PARTY_LICENSES" / "inventory.toml"
    try:
        inventory = load_inventory(inventory_path)
    except (KeyError, OSError, TypeError, ValueError) as error:
        display_path = inventory_path.relative_to(root).as_posix()
        return [f"inventory: cannot load {display_path}: {error}"]
    violations: list[str] = []
    violations.extend(validate_notice(root, inventory))
    if inventory.schema != 1:
        violations.append(f"inventory: unsupported schema {inventory.schema}")
    if inventory.lockfile != "native/Cargo.lock":
        violations.append(
            f"inventory: lockfile must be exactly native/Cargo.lock, got {inventory.lockfile!r}"
        )
    if inventory.selection != _LICENSE_SELECTION_POLICY:
        violations.append("inventory: license selection policy differs from the reviewed policy")
    lock_path = root / "native" / "Cargo.lock"
    try:
        all_locked = load_locked_packages(lock_path)
        manifest = _load_toml(root / "native" / "Cargo.toml")
        manifest_package = manifest["package"]
        if not isinstance(manifest_package, dict):
            raise ValueError("native Cargo.toml package field must be a table")
        local_key = (
            _required_string(
                manifest_package,
                "name",
                context="native Cargo.toml package",
            ),
            _required_string(
                manifest_package,
                "version",
                context="native Cargo.toml package",
            ),
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        return [f"inventory: cannot load native dependency manifests: {error}"]

    all_locked_keys = [package.key for package in all_locked]
    if len(set(all_locked_keys)) != len(all_locked_keys):
        violations.append("inventory: duplicate locked package identity")
    local_packages = [package for package in all_locked if package.source is None]
    if [package.key for package in local_packages] != [local_key]:
        rendered = ", ".join(f"{package.name} {package.version}" for package in local_packages)
        violations.append(
            "inventory: source-less lock packages must contain only "
            f"{local_key[0]} {local_key[1]}; found {rendered or 'none'}"
        )
    for package in all_locked:
        for dependency in package.dependencies:
            if _dependency_key(dependency, all_locked) is None:
                violations.append(
                    "inventory: unresolved locked dependency "
                    f"{dependency!r} required by {package.name} {package.version}"
                )

    locked = _third_party(all_locked)

    locked_keys = {package.key for package in locked}
    inventory_keys = {component.key for component in inventory.components}
    if len(inventory_keys) != len(inventory.components):
        violations.append("inventory: duplicate component entry")
    for key in sorted(locked_keys - inventory_keys):
        violations.append(f"inventory: unreviewed locked component {key[0]} {key[1]}")
    for key in sorted(inventory_keys - locked_keys):
        violations.append(f"inventory: component is not locked {key[0]} {key[1]}")

    for package in locked:
        if package.checksum is None or re.fullmatch(r"[0-9a-f]{64}", package.checksum) is None:
            violations.append(
                "inventory: registry component lacks a lowercase SHA-256 checksum "
                f"{package.name} {package.version}"
            )
        if package.name.casefold() in _FORBIDDEN_COMPONENTS:
            violations.append(f"inventory: forbidden Java/JVM component {package.name}")
    expected_legal_files = {
        "README.md",
        "inventory.toml",
        *_DEVELOPMENT_LICENSE_FILES,
    }
    for component in inventory.components:
        expected_selection = _LICENSE_SELECTIONS.get(component.license_expression)
        if expected_selection is None:
            violations.append(
                "inventory: unreviewed SPDX expression "
                f"{component.license_expression!r} for {component.name}"
            )
        elif component.selected_license != expected_selection:
            violations.append(
                "inventory: selected license does not match reviewed SPDX expression "
                f"for {component.name} {component.version}; expected "
                f"{expected_selection!r}, got {component.selected_license!r}"
            )
        if component.scope not in {"native-build", "native-runtime"}:
            violations.append(f"inventory: invalid scope {component.scope!r} for {component.name}")
        expected_additional = _ADDITIONAL_LICENSE_FILES.get(component.selected_license)
        if component.additional_license_file != expected_additional:
            violations.append(
                "inventory: additional license file does not match selected license "
                f"for {component.name} {component.version}; expected "
                f"{expected_additional!r}, got {component.additional_license_file!r}"
            )
        if component.additional_license_file is None:
            continue
        relative = PurePosixPath(component.additional_license_file)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "THIRD_PARTY_LICENSES"
            or relative.parts[1] in {"", ".", ".."}
        ):
            violations.append(
                f"inventory: unsafe additional license path {component.additional_license_file!r}"
            )
            continue
        expected_legal_files.add(relative.name)
        license_path = root / component.additional_license_file
        if (
            license_path.is_symlink()
            or not license_path.is_file()
            or not license_path.read_text(encoding="utf-8").strip()
        ):
            violations.append(
                f"inventory: missing additional license file {component.additional_license_file}"
            )
    legal_root = root / "THIRD_PARTY_LICENSES"
    try:
        legal_entries = tuple(legal_root.iterdir())
    except OSError as error:
        violations.append(f"inventory: cannot enumerate legal payloads: {error}")
    else:
        unsafe_legal_entries = sorted(
            path.name for path in legal_entries if path.is_symlink() or not path.is_file()
        )
        if unsafe_legal_entries:
            violations.append(
                f"inventory: legal payload entries must be regular files {unsafe_legal_entries}"
            )
        actual_legal_files = {
            path.name for path in legal_entries if path.is_file() and not path.is_symlink()
        }
        missing_legal_files = sorted(expected_legal_files - actual_legal_files)
        if missing_legal_files:
            violations.append(f"inventory: missing legal payload files {missing_legal_files}")
        unexpected_legal_files = sorted(actual_legal_files - expected_legal_files)
        if unexpected_legal_files:
            violations.append(
                f"inventory: unreferenced legal payload files {unexpected_legal_files}"
            )
    return sorted(violations)


def _project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    if match is None:
        raise ValueError("pyproject.toml has no literal project version")
    return match.group(1)


def _dependency_key(dependency: str, packages: tuple[LockedPackage, ...]) -> tuple[str, str] | None:
    """Resolve Cargo.lock's ``name [version]`` dependency notation."""

    fields = dependency.split()
    name = fields[0]
    candidates = [package for package in packages if package.name == name]
    if len(candidates) == 1:
        return candidates[0].key
    if len(fields) > 1:
        for candidate in candidates:
            if candidate.version == fields[1]:
                return candidate.key
    return None


def build_cyclonedx(root: Path, variant: Variant) -> dict[str, Any]:
    """Build a deterministic CycloneDX 1.5 document for one artifact lane."""

    violations = validate_inventory(root)
    if violations:
        raise ValueError("; ".join(violations))
    version = _project_version(root)
    inventory = load_inventory(root / "THIRD_PARTY_LICENSES" / "inventory.toml")
    all_packages = load_locked_packages(root / inventory.lockfile)
    packages = _third_party(all_packages) if variant == "native" else ()
    by_key = {package.key: package for package in packages}
    inventory_by_key = {component.key: component for component in inventory.components}
    root_ref = f"pkg:pypi/pyowl-core@{version}?variant={variant}"

    components: list[dict[str, Any]] = []
    for package in sorted(packages, key=lambda item: item.key):
        reviewed = inventory_by_key[package.key]
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": package.bom_ref,
            "name": package.name,
            "version": package.version,
            "purl": package.bom_ref,
            "licenses": [{"expression": reviewed.license_expression}],
            "properties": [
                {"name": "pyowl-core:selected-license", "value": reviewed.selected_license},
                {"name": "pyowl-core:scope", "value": reviewed.scope},
            ],
        }
        if package.checksum is not None:
            component["hashes"] = [{"alg": "SHA-256", "content": package.checksum}]
        components.append(component)

    dependency_rows: list[dict[str, Any]] = []
    if variant == "native":
        local = next(package for package in all_packages if package.name == _LOCAL_NATIVE_CRATE)
        root_dependencies = []
        for dependency in local.dependencies:
            key = _dependency_key(dependency, packages)
            if key in by_key:
                root_dependencies.append(by_key[key].bom_ref)
        dependency_rows.append({"ref": root_ref, "dependsOn": sorted(root_dependencies)})
        for package in sorted(packages, key=lambda item: item.key):
            resolved: list[str] = []
            for dependency in package.dependencies:
                key = _dependency_key(dependency, packages)
                if key in by_key:
                    resolved.append(by_key[key].bom_ref)
            dependency_rows.append({"ref": package.bom_ref, "dependsOn": sorted(resolved)})
    else:
        dependency_rows.append({"ref": root_ref, "dependsOn": []})

    serial = uuid.uuid5(uuid.NAMESPACE_URL, root_ref)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "library",
                "bom-ref": root_ref,
                "name": "pyowl-core",
                "version": version,
                "purl": f"pkg:pypi/pyowl-core@{version}",
                "licenses": [{"expression": "Apache-2.0"}],
                "properties": [{"name": "pyowl-core:artifact-variant", "value": variant}],
            }
        },
        "components": components,
        "dependencies": dependency_rows,
    }


def build_dependency_inventory(root: Path) -> dict[str, Any]:
    """Render a human-auditable machine ledger from the lock and review file."""

    violations = validate_inventory(root)
    if violations:
        raise ValueError("; ".join(violations))
    inventory = load_inventory(root / "THIRD_PARTY_LICENSES" / "inventory.toml")
    locked = {package.key: package for package in load_locked_packages(root / inventory.lockfile)}
    components = []
    for reviewed in sorted(inventory.components, key=lambda item: item.key):
        package = locked[reviewed.key]
        components.append(
            {
                "name": reviewed.name,
                "version": reviewed.version,
                "checksum_sha256": package.checksum,
                "license": reviewed.license_expression,
                "selected_license": reviewed.selected_license,
                "scope": reviewed.scope,
                "source": package.source,
            }
        )
    return {
        "schema": 1,
        "distribution": "pyowl-core",
        "version": _project_version(root),
        "python_runtime_dependencies": [],
        "native_components": components,
        "java_components": [],
        "legal_approval": inventory.legal_approval,
        "release_blockers": (
            []
            if inventory.legal_approval
            else ["third-party license review requires release-owner or counsel approval"]
        ),
    }


def _workflow_pin(text: str, pattern: str, label: str) -> str:
    values: set[str] = set(re.findall(pattern, text))
    if len(values) != 1:
        raise ValueError(f"build provenance: expected one unique {label}, got {sorted(values)!r}")
    return values.pop()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_stable_regular_file(path: Path, *, label: str) -> bytes:
    try:
        initial = path.lstat()
    except OSError as error:
        raise ValueError(
            f"build provenance: cannot inspect build input {label}: {error}"
        ) from error
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError(
            f"build provenance: build input must be a regular non-symlink file: {label}"
        )
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read()
            completed = os.fstat(stream.fileno())
        final = path.lstat()
    except OSError as error:
        raise ValueError(f"build provenance: cannot read build input {label}: {error}") from error
    identities = {
        _stat_identity(initial),
        _stat_identity(opened),
        _stat_identity(completed),
        _stat_identity(final),
    }
    if len(identities) != 1 or not stat.S_ISREG(opened.st_mode) or len(payload) != opened.st_size:
        raise ValueError(f"build provenance: build input changed while reading: {label}")
    return payload


def _payload_identity(payload: bytes) -> dict[str, Any]:
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _decode_build_input(payload: bytes, label: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"build provenance: build input is not UTF-8: {label}") from error


def _load_build_toml(payload: bytes, label: str) -> dict[str, Any]:
    try:
        loaded = tomllib.loads(_decode_build_input(payload, label))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"build provenance: cannot parse TOML build input {label}") from error
    if not isinstance(loaded, dict):  # pragma: no cover - tomllib contract
        raise ValueError(f"build provenance: TOML build input is not a table: {label}")
    return loaded


def _captured_project_version(payload: bytes) -> str:
    text = _decode_build_input(payload, "pyproject.toml")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    if match is None:
        raise ValueError("build provenance: pyproject.toml has no literal project version")
    return match.group(1)


def build_provenance(root: Path) -> dict[str, Any]:
    """Bind exact release tool pins to every deterministic build input."""

    payloads = {
        relative_path: _read_stable_regular_file(
            root / relative_path,
            label=relative_path,
        )
        for relative_path in _BUILD_INPUT_PATHS
    }
    wheels = _decode_build_input(
        payloads[".github/workflows/wheels.yml"],
        ".github/workflows/wheels.yml",
    )
    ci = _decode_build_input(
        payloads[".github/workflows/ci.yml"],
        ".github/workflows/ci.yml",
    )
    cargo = _load_build_toml(payloads["native/Cargo.toml"], "native/Cargo.toml")
    package = cargo.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("rust-version"), str):
        raise ValueError("build provenance: native Cargo.toml has no literal rust-version")
    rust_msrv = package["rust-version"]
    rust_toolchain = _workflow_pin(
        wheels,
        r"rustup toolchain install ([0-9]+\.[0-9]+\.[0-9]+)",
        "Rust release toolchain",
    )
    if not rust_toolchain.startswith(f"{rust_msrv}."):
        raise ValueError(
            "build provenance: Cargo rust-version "
            f"{rust_msrv!r} does not match workflow toolchain {rust_toolchain!r}"
        )
    source_date_epoch = _workflow_pin(
        wheels,
        r'(?m)^\s*SOURCE_DATE_EPOCH:\s*"([0-9]+)"\s*$',
        "SOURCE_DATE_EPOCH",
    )
    build_version = _workflow_pin(wheels, r"\bbuild==([0-9][^\s]+)", "build frontend")
    setuptools_version = _workflow_pin(
        wheels,
        r"\bsetuptools==([0-9][^\s]+)",
        "setuptools backend",
    )
    wheel_version = _workflow_pin(wheels, r"\bwheel==([0-9][^\s]+)", "wheel builder")
    cibuildwheel_revision = _workflow_pin(
        wheels,
        r"pypa/cibuildwheel@([0-9a-f]{40})",
        "cibuildwheel action revision",
    )
    pure_ci_image = _workflow_pin(
        ci,
        r"(?m)^\s*container:\s*(python:3\.10-slim@sha256:[0-9a-f]{64})\s*$",
        "pure-package CI image",
    )
    inputs = {
        relative_path: _payload_identity(payloads[relative_path])
        for relative_path in _BUILD_INPUT_PATHS
    }
    return {
        "schema": "pyowl-core.build-provenance/1",
        "distribution": "pyowl-core",
        "version": _captured_project_version(payloads["pyproject.toml"]),
        "source_date_epoch": int(source_date_epoch),
        "tools": {
            "rust_toolchain": rust_toolchain,
            "cargo_manifest_rust_version": rust_msrv,
            "python_build_frontend": f"build=={build_version}",
            "python_build_backend": f"setuptools=={setuptools_version}",
            "wheel_builder": f"wheel=={wheel_version}",
            "cibuildwheel_action": f"pypa/cibuildwheel@{cibuildwheel_revision}",
            "pure_ci_image": pure_ci_image,
        },
        "inputs": inputs,
    }


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generate_evidence(root: Path, output_dir: Path, *, check: bool = False) -> list[str]:
    """Write or verify the deterministic inventory and pure/native SBOM files."""

    documents = {
        "build-provenance.json": build_provenance(root),
        "dependency-inventory.json": build_dependency_inventory(root),
        "sbom-native.cdx.json": build_cyclonedx(root, "native"),
        "sbom-pure.cdx.json": build_cyclonedx(root, "pure"),
    }
    drift: list[str] = []
    if not check:
        output_dir.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        path = output_dir / name
        rendered = _canonical_json(document)
        if check:
            try:
                actual = path.read_text(encoding="utf-8")
            except OSError:
                drift.append(f"supply-chain: missing generated evidence {path}")
            else:
                if actual != rendered:
                    drift.append(f"supply-chain: generated evidence drift {path}")
        else:
            path.write_text(rendered, encoding="utf-8")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--require-approval",
        action="store_true",
        help="fail unless the reviewed inventory records release-owner legal approval",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else root / "reports" / "release" / _project_version(root)
    )
    violations = validate_inventory(root)
    if not violations and args.require_approval:
        inventory = load_inventory(root / "THIRD_PARTY_LICENSES" / "inventory.toml")
        if not inventory.legal_approval:
            violations.append(
                "release: third-party license review requires release-owner or counsel approval"
            )
    if not violations:
        violations.extend(generate_evidence(root, output_dir, check=args.check))
    for violation in violations:
        print(violation)
    if violations:
        return 1
    action = "verified" if args.check else "generated"
    print(f"supply-chain evidence {action}: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
