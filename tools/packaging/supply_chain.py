"""Validate the locked native dependency inventory and emit deterministic SBOMs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
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
_BUILD_INPUT_PATHS = (
    ".github/workflows/native-safety.yml",
    ".github/workflows/wheels.yml",
    "MANIFEST.in",
    "native/Cargo.lock",
    "native/Cargo.toml",
    "pyowl_build.py",
    "pyproject.toml",
    "setup.py",
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
    legal_approval: bool
    components: tuple[InventoryComponent, ...]


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        loaded = tomllib.load(stream)
    if not isinstance(loaded, dict):  # pragma: no cover - tomllib contract
        raise ValueError(f"{path} did not contain a TOML table")
    return loaded


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
        packages.append(
            LockedPackage(
                name=str(raw["name"]),
                version=str(raw["version"]),
                checksum=str(checksum) if checksum is not None else None,
                dependencies=tuple(dependencies),
                source=str(source) if source is not None else None,
            )
        )
    return tuple(packages)


def load_inventory(path: Path) -> Inventory:
    """Load the reviewed inventory with strict required fields."""

    raw = _load_toml(path)
    raw_components = raw.get("component", [])
    if not isinstance(raw_components, list):
        raise ValueError("inventory component field must be an array")
    components: list[InventoryComponent] = []
    for entry in raw_components:
        if not isinstance(entry, dict):
            raise ValueError("inventory component entry must be a table")
        additional = entry.get("additional_license_file")
        components.append(
            InventoryComponent(
                name=str(entry["name"]),
                version=str(entry["version"]),
                license_expression=str(entry["license"]),
                selected_license=str(entry["selected_license"]),
                scope=str(entry["scope"]),
                additional_license_file=str(additional) if additional is not None else None,
            )
        )
    return Inventory(
        schema=int(raw["schema"]),
        lockfile=str(raw["lockfile"]),
        legal_approval=bool(raw["legal_approval"]),
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
        return [f"inventory: cannot load {inventory_path.relative_to(root)}: {error}"]
    violations: list[str] = []
    violations.extend(validate_notice(root, inventory))
    if inventory.schema != 1:
        violations.append(f"inventory: unsupported schema {inventory.schema}")
    if inventory.lockfile != "native/Cargo.lock":
        violations.append(
            f"inventory: lockfile must be exactly native/Cargo.lock, got {inventory.lockfile!r}"
        )
    lock_path = root / "native" / "Cargo.lock"
    try:
        all_locked = load_locked_packages(lock_path)
        manifest = _load_toml(root / "native" / "Cargo.toml")
        manifest_package = manifest["package"]
        if not isinstance(manifest_package, dict):
            raise ValueError("native Cargo.toml package field must be a table")
        local_key = (str(manifest_package["name"]), str(manifest_package["version"]))
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
        if package.checksum is None:
            violations.append(
                f"inventory: registry component lacks checksum {package.name} {package.version}"
            )
        if package.name.casefold() in _FORBIDDEN_COMPONENTS:
            violations.append(f"inventory: forbidden Java/JVM component {package.name}")
    for component in inventory.components:
        if not component.license_expression.strip() or not component.selected_license.strip():
            violations.append(
                f"inventory: missing license selection {component.name} {component.version}"
            )
        if component.scope not in {"native-build", "native-runtime"}:
            violations.append(f"inventory: invalid scope {component.scope!r} for {component.name}")
        if component.additional_license_file is not None:
            license_path = root / component.additional_license_file
            if not license_path.is_file() or not license_path.read_text(encoding="utf-8").strip():
                violations.append(
                    "inventory: missing additional license file "
                    f"{component.additional_license_file}"
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


def _file_identity(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def build_provenance(root: Path) -> dict[str, Any]:
    """Bind exact release tool pins to every deterministic build input."""

    wheels = (root / ".github" / "workflows" / "wheels.yml").read_text(encoding="utf-8")
    cargo = _load_toml(root / "native" / "Cargo.toml")
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
    inputs: dict[str, dict[str, Any]] = {}
    for relative_path in _BUILD_INPUT_PATHS:
        path = root / relative_path
        if not path.is_file():
            raise ValueError(f"build provenance: missing build input {relative_path}")
        inputs[relative_path] = _file_identity(path)
    return {
        "schema": "pyowl-core.build-provenance/1",
        "distribution": "pyowl-core",
        "version": _project_version(root),
        "source_date_epoch": int(source_date_epoch),
        "tools": {
            "rust_toolchain": rust_toolchain,
            "cargo_manifest_rust_version": rust_msrv,
            "python_build_frontend": f"build=={build_version}",
            "python_build_backend": f"setuptools=={setuptools_version}",
            "wheel_builder": f"wheel=={wheel_version}",
            "cibuildwheel_action": f"pypa/cibuildwheel@{cibuildwheel_revision}",
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
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else root / "reports" / "release" / _project_version(root)
    )
    violations = validate_inventory(root)
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
