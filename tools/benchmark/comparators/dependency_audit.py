"""Audit comparator and Java exclusion without building or fetching artifacts."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from tools.packaging.artifact_inspector import inspect_artifact

try:
    import tomllib  # type: ignore[import-untyped, unused-ignore]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, import-untyped, unused-ignore]

SCHEMA = "pyowl-core/comparator-dependency-audit/v1"
ArtifactKind = Literal["wheel", "sdist"]

_SOURCE_IDENTITY_SCHEMA = "pyowl-core/comparator-source-identity/v1"
_SOURCE_IDENTITY_DOMAIN = "pyowl-core:comparator-source-identity:v1"
_SOURCE_IDENTITY_PREIMAGE = (
    "UTF-8 domain, one NUL byte, then UTF-8 canonical JSON of inputs; canonical JSON uses "
    "sorted keys, compact separators, ensure_ascii=false, inputs sorted by path, and checks "
    "sorted by identifier"
)

_BANNED_DEPENDENCIES = frozenset(
    {
        "deeponto",
        "horned",
        "horned-bin",
        "horned-owl",
        "j4rs",
        "javabridge",
        "jni",
        "jni-sys",
        "jnius",
        "jpype",
        "jpype1",
        "mowl",
        "owlapi",
        "owlapi-distribution",
        "py-horned-owl",
        "pyjnius",
        "robot",
    }
)
_BANNED_MODULES = frozenset(
    _BANNED_DEPENDENCIES
    | {
        "horned_owl",
        "java",
        "jpype1",
        "py_horned_owl",
        "pyhornedowl",
    }
)
_COMPARATOR_PATHS = (
    ("benchmarks", "comparators"),
    ("tests", "benchmark", "comparators"),
    ("tools", "benchmark", "comparators"),
)
_JAVA_SUFFIXES = frozenset({".class", ".ear", ".jar", ".jmod", ".war"})
_NATIVE_SUFFIXES = (".dll", ".dylib", ".pyd", ".so")
_BINARY_MARKERS = (
    b"horned-owl",
    b"horned_owl",
    b"JNI_CreateJavaVM",
    b"libjli",
    b"libjvm",
    b"org.semanticweb.owlapi",
    b"py-horned-owl",
    b"py_horned_owl",
)
_SKIP_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "target",
    }
)
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 1024**3
_MAX_TEXT_BYTES = 4 * 1024**2
_MAX_BINARY_SCAN_BYTES = 128 * 1024**2
_ARTIFACT_DEPENDENCY_FILES = frozenset(
    {
        "cargo.lock",
        "cargo.toml",
        "inventory.toml",
        "pipfile",
        "pipfile.lock",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
    }
)
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
_RUST_IMPORT = re.compile(
    r"(?m)^\s*(?:extern\s+crate|use)\s+"
    r"(?:horned_owl|jni|j4rs|owlapi)(?:::|\s|;)"
)
_RUST_JAVA_COMMAND = re.compile(r'Command::new\s*\(\s*"(?:java|javac|mvn|gradle)"')
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SUBPROCESS_CALLS = frozenset({"call", "check_call", "check_output", "Popen", "run"})
_JAVA_EXECUTABLES = frozenset({"gradle", "java", "javac", "mvn"})
_DETACHED_AUDIT_DIRECTORY = "reports/performance/redesign-baseline"
_DETACHED_AUDIT_PATTERN = "dependency-audit-*.json"


@dataclass(frozen=True, slots=True)
class ArtifactExpectation:
    """One supplied distribution and any independently recorded expectation."""

    path: Path
    kind: ArtifactKind | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("artifact expectation path must be Path")
        if self.kind not in {None, "wheel", "sdist"}:
            raise ValueError("artifact expectation kind must be wheel or sdist")
        if self.sha256 is not None and not _SHA256.fullmatch(self.sha256):
            raise ValueError("artifact expectation SHA-256 must be lowercase hexadecimal")


def audit_dependency_exclusion(
    root: Path,
    artifacts: Sequence[Path | ArtifactExpectation] = (),
) -> dict[str, object]:
    """Return deterministic, JSON-safe exclusion evidence for one source tree."""

    selected_root = root.resolve()
    source_checks = (
        _metadata_check(selected_root),
        _source_import_check(selected_root),
        _payload_manifest_check(selected_root),
    )
    source_identity = _source_identity(selected_root, source_checks)
    artifact_check = _artifact_check(selected_root, artifacts)
    statuses = [cast(str, row["status"]) for row in source_checks]
    statuses.append(cast(str, source_identity["status"]))
    statuses.append(cast(str, artifact_check["status"]))
    if "fail" in statuses:
        status = "fail"
    elif "not-run" in statuses:
        status = "not-run"
    else:
        status = "pass"
    return {
        "schema": SCHEMA,
        "status": status,
        "source_identity": source_identity,
        "source_checks": list(source_checks),
        "artifact_check": artifact_check,
    }


def _source_identity(
    root: Path,
    checks: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Bind the exact inspected source inputs to a documented canonical preimage."""

    bindings: dict[str, set[str]] = {}
    findings: list[str] = []
    for check in checks:
        identifier = check.get("id")
        inputs = check.get("inputs")
        if not isinstance(identifier, str) or not isinstance(inputs, list):
            findings.append("source identity: malformed source-check input inventory")
            continue
        for relative in inputs:
            if not isinstance(relative, str):
                findings.append("source identity: non-string source-check input path")
                continue
            bindings.setdefault(relative, set()).add(identifier)

    rows: list[dict[str, object]] = []
    for relative in sorted(bindings):
        normalized = PurePosixPath(relative)
        path = root.joinpath(*normalized.parts)
        try:
            if (
                normalized.is_absolute()
                or not normalized.parts
                or any(part in {"", ".", ".."} for part in normalized.parts)
                or not path.resolve(strict=True).is_relative_to(root)
            ):
                raise ValueError("input path escapes source root")
            payload = path.read_bytes()
        except (OSError, ValueError) as error:
            findings.append(
                f"source identity:{relative}: cannot bind input: {type(error).__name__}"
            )
            continue
        rows.append(
            {
                "path": relative,
                "checks": sorted(bindings[relative]),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    canonical_inputs = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = None
    if not findings:
        digest = hashlib.sha256(
            _SOURCE_IDENTITY_DOMAIN.encode("utf-8") + b"\0" + canonical_inputs
        ).hexdigest()
    return {
        "schema": _SOURCE_IDENTITY_SCHEMA,
        "status": "fail" if findings else "pass",
        "sha256": digest,
        "domain": _SOURCE_IDENTITY_DOMAIN,
        "preimage_format": _SOURCE_IDENTITY_PREIMAGE,
        "input_count": len(rows),
        "input_bytes": sum(cast(int, row["bytes"]) for row in rows),
        "inputs": rows,
        "findings": sorted(set(findings)),
        "git": _git_identity(root, tuple(bindings)),
    }


def _git_identity(root: Path, inputs: Sequence[str]) -> dict[str, object]:
    """Return Git provenance scoped to inputs, keeping detached evidence out of dirty state."""

    unavailable: dict[str, object] = {
        "available": False,
        "revision": None,
        "dirty": None,
        "dirty_scope": "repository-excluding-detached-audit-evidence",
        "inspected_inputs_dirty": None,
    }
    top_level_payload = _git_output(root, "rev-parse", "--show-toplevel")
    revision_payload = _git_output(root, "rev-parse", "--verify", "HEAD")
    if top_level_payload is None or revision_payload is None:
        return unavailable
    try:
        top_level = Path(os.fsdecode(top_level_payload.rstrip(b"\n"))).resolve()
        revision = revision_payload.decode("ascii").strip()
        prefix = root.relative_to(top_level)
    except (UnicodeError, ValueError):
        return unavailable
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        return unavailable

    changed_payload = _git_output(root, "diff", "--name-only", "-z", "HEAD", "--")
    untracked_payload = _git_output(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
    )
    if changed_payload is None or untracked_payload is None:
        return unavailable
    changed = _nul_paths(changed_payload) | _nul_paths(untracked_payload)
    inspected = {(prefix / PurePosixPath(value)).as_posix() for value in inputs}
    detached_root = PurePosixPath((prefix / _DETACHED_AUDIT_DIRECTORY).as_posix())
    provenance_changes = {
        value
        for value in changed
        if not (
            PurePosixPath(value).match(_DETACHED_AUDIT_PATTERN)
            and detached_root in PurePosixPath(value).parents
        )
    }
    return {
        "available": True,
        "revision": revision.casefold(),
        "dirty": bool(provenance_changes),
        "dirty_scope": "repository-excluding-detached-audit-evidence",
        "inspected_inputs_dirty": bool(changed & inspected),
    }


def _git_output(root: Path, *arguments: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ("git", "-C", os.fspath(root), *arguments),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _nul_paths(payload: bytes) -> set[str]:
    return {PurePosixPath(os.fsdecode(value)).as_posix() for value in payload.split(b"\0") if value}


def _metadata_check(root: Path) -> dict[str, object]:
    inputs: list[str] = []
    findings: list[str] = []
    for path in _dependency_metadata_files(root):
        relative = path.relative_to(root).as_posix()
        inputs.append(relative)
        try:
            rows = _metadata_dependencies(path)
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as error:
            findings.append(f"metadata:{relative}: cannot inspect: {type(error).__name__}")
            continue
        for scope, dependency in rows:
            normalized = _normalize_name(dependency)
            if normalized in _BANNED_DEPENDENCIES:
                findings.append(f"metadata:{scope}:{relative}: forbidden dependency {normalized}")
    return _check("dependency-metadata", inputs, findings)


def _dependency_metadata_files(root: Path) -> tuple[Path, ...]:
    selected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or _skip(path, root) or _is_comparator_path(path, root):
            continue
        lowered = path.name.casefold()
        if lowered in {
            "cargo.lock",
            "cargo.toml",
            "pipfile",
            "pipfile.lock",
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
            "tox.ini",
        } or (lowered.startswith("requirements") and path.suffix.casefold() in {".in", ".txt"}):
            selected.append(path)
    inventory = root / "THIRD_PARTY_LICENSES" / "inventory.toml"
    if inventory.is_file() and inventory not in selected:
        selected.append(inventory)
    return tuple(sorted(selected, key=lambda value: value.relative_to(root).as_posix()))


def _metadata_dependencies(path: Path) -> tuple[tuple[str, str], ...]:
    lowered = path.name.casefold()
    if lowered == "pyproject.toml":
        return _pyproject_dependencies(path.read_bytes())
    if lowered == "cargo.toml":
        return _cargo_manifest_dependencies(path.read_bytes())
    if lowered == "cargo.lock":
        return _cargo_lock_dependencies(path.read_bytes())
    if path.as_posix().endswith("THIRD_PARTY_LICENSES/inventory.toml"):
        return _inventory_dependencies(path.read_bytes())
    if lowered.startswith("requirements"):
        return tuple(
            ("ordinary-test", name)
            for line in path.read_text(encoding="utf-8").splitlines()
            if (name := _requirement_name(line)) is not None
        )
    return _text_dependencies(path.read_text(encoding="utf-8"))


def _pyproject_dependencies(payload: bytes) -> tuple[tuple[str, str], ...]:
    data = tomllib.loads(payload.decode("utf-8"))
    rows: list[tuple[str, str]] = []
    build = _mapping(data.get("build-system"))
    rows.extend(("build", name) for name in _requirements(build.get("requires")))
    project = _mapping(data.get("project"))
    rows.extend(("runtime", name) for name in _requirements(project.get("dependencies")))
    extras = _mapping(project.get("optional-dependencies"))
    for extra, values in sorted(extras.items()):
        scope = "ordinary-test" if extra.casefold() in {"dev", "test", "tests"} else "runtime"
        rows.extend((scope, name) for name in _requirements(values))
    return tuple(rows)


def _cargo_manifest_dependencies(payload: bytes) -> tuple[tuple[str, str], ...]:
    data = tomllib.loads(payload.decode("utf-8"))
    rows: list[tuple[str, str]] = []

    def visit(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        mapping = cast(Mapping[str, object], value)
        for key, nested in mapping.items():
            normalized = key.casefold().replace("_", "-")
            if normalized in {"dependencies", "build-dependencies", "dev-dependencies"}:
                scope = {
                    "dependencies": "runtime",
                    "build-dependencies": "build",
                    "dev-dependencies": "ordinary-test",
                }[normalized]
                for name, specification in _mapping(nested).items():
                    rows.append((scope, name))
                    package = _mapping(specification).get("package")
                    if isinstance(package, str):
                        rows.append((scope, package))
            visit(nested)

    visit(data)
    return tuple(rows)


def _cargo_lock_dependencies(payload: bytes) -> tuple[tuple[str, str], ...]:
    data = tomllib.loads(payload.decode("utf-8"))
    packages = data.get("package", ())
    if not isinstance(packages, list):
        return ()
    return tuple(
        ("locked", name)
        for row in packages
        if isinstance(row, Mapping) and isinstance((name := row.get("name")), str)
    )


def _inventory_dependencies(payload: bytes) -> tuple[tuple[str, str], ...]:
    data = tomllib.loads(payload.decode("utf-8"))
    components = data.get("component", ())
    if not isinstance(components, list):
        return ()
    return tuple(
        ("inventory", name)
        for row in components
        if isinstance(row, Mapping) and isinstance((name := row.get("name")), str)
    )


def _text_dependencies(text: str) -> tuple[tuple[str, str], ...]:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text)
    return tuple(
        ("build", token) for token in tokens if _normalize_name(token) in _BANNED_DEPENDENCIES
    )


def _source_import_check(root: Path) -> dict[str, object]:
    inputs: list[str] = []
    findings: list[str] = []
    for path, scope in _source_files(root):
        relative = path.relative_to(root).as_posix()
        inputs.append(relative)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            findings.append(f"source:{relative}: cannot inspect: {type(error).__name__}")
            continue
        if path.suffix.casefold() in {".py", ".pyi"}:
            findings.extend(_python_source_findings(relative, scope, text))
        else:
            if _RUST_IMPORT.search(text):
                findings.append(f"source:{scope}:{relative}: forbidden Rust comparator import")
            if _RUST_JAVA_COMMAND.search(text):
                findings.append(f"source:{scope}:{relative}: forbidden Java command")
    return _check("source-imports", inputs, findings)


def _source_files(root: Path) -> tuple[tuple[Path, str], ...]:
    selected: list[tuple[Path, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or _skip(path, root) or _is_comparator_path(path, root):
            continue
        relative = path.relative_to(root)
        suffix = path.suffix.casefold()
        scope: str | None = None
        if (relative.parts[:2] == ("src", "pyowl_core") and suffix in {".py", ".pyi"}) or (
            relative.parts[:2] == ("native", "src") and suffix == ".rs"
        ):
            scope = "runtime"
        elif relative.as_posix() in {"native/build.rs", "pyowl_build.py", "setup.py"}:
            scope = "build"
        elif relative.parts and relative.parts[0] == "tests" and suffix in {".py", ".rs"}:
            scope = "ordinary-test"
        if scope is not None:
            selected.append((path, scope))
    return tuple(sorted(selected, key=lambda row: row[0].relative_to(root).as_posix()))


def _python_source_findings(relative: str, scope: str, text: str) -> list[str]:
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError:
        return [f"source:{scope}:{relative}: cannot parse Python source"]
    findings: list[str] = []
    importlib_aliases: set[str] = set()
    dynamic_import_aliases = {"__import__"}
    subprocess_aliases: set[str] = set()
    subprocess_call_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                findings.extend(_forbidden_module_finding(relative, scope, alias.name))
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
                elif alias.name == "subprocess":
                    subprocess_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            findings.extend(_forbidden_module_finding(relative, scope, node.module))
            if node.module == "importlib":
                dynamic_import_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
            elif node.module == "subprocess":
                subprocess_call_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in _SUBPROCESS_CALLS
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        imported = _dynamic_import_name(node, importlib_aliases, dynamic_import_aliases)
        if imported is not None:
            findings.extend(_forbidden_module_finding(relative, scope, imported))
        if _is_java_subprocess_call(node, subprocess_aliases, subprocess_call_aliases):
            findings.append(f"source:{scope}:{relative}: forbidden Java command")
    return findings


def _dynamic_import_name(
    node: ast.Call,
    importlib_aliases: set[str],
    dynamic_import_aliases: set[str],
) -> str | None:
    recognized = isinstance(node.func, ast.Name) and node.func.id in dynamic_import_aliases
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in importlib_aliases
        and node.func.attr == "import_module"
    ):
        recognized = True
    if not recognized:
        return None
    value = _call_argument(node, 0, "name")
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _is_java_subprocess_call(
    node: ast.Call,
    subprocess_aliases: set[str],
    subprocess_call_aliases: set[str],
) -> bool:
    recognized = isinstance(node.func, ast.Name) and node.func.id in subprocess_call_aliases
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in subprocess_aliases
        and node.func.attr in _SUBPROCESS_CALLS
    ):
        recognized = True
    if not recognized:
        return False
    command = _call_argument(node, 0, "args")
    executable: str | None = None
    if isinstance(command, ast.Constant) and isinstance(command.value, str):
        parts = command.value.strip().split(maxsplit=1)
        executable = parts[0] if parts else None
    elif (
        isinstance(command, (ast.List, ast.Tuple))
        and command.elts
        and isinstance(command.elts[0], ast.Constant)
        and isinstance(command.elts[0].value, str)
    ):
        executable = command.elts[0].value
    if executable is None:
        return False
    command_name = executable.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return command_name in _JAVA_EXECUTABLES


def _call_argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((row.value for row in node.keywords if row.arg == keyword), None)


def _forbidden_module_finding(relative: str, scope: str, module: str) -> list[str]:
    top_level = module.split(".", 1)[0]
    banned_keys = {_normalize_name(value) for value in _BANNED_MODULES}
    if _normalize_name(top_level) not in banned_keys and not module.casefold().startswith(
        "org.semanticweb.owlapi"
    ):
        return []
    return [f"source:{scope}:{relative}: forbidden import {module}"]


def _payload_manifest_check(root: Path) -> dict[str, object]:
    findings: list[str] = []
    inputs: list[str] = []
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        findings.append("payload: missing pyproject.toml")
    else:
        inputs.append("pyproject.toml")
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            setuptools = _mapping(_mapping(data.get("tool")).get("setuptools"))
            package_dir = _mapping(setuptools.get("package-dir"))
            package_find = _mapping(setuptools.get("packages"))
            if package_dir.get("") != "src":
                findings.append("payload: setuptools package-dir must map the root to src")
            find_config = _mapping(package_find.get("find"))
            where = find_config.get("where")
            if where != ["src"]:
                findings.append("payload: setuptools package discovery must be restricted to src")
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            findings.append(f"payload: cannot inspect pyproject.toml: {type(error).__name__}")
    manifest_path = root / "MANIFEST.in"
    if not manifest_path.is_file():
        findings.append("payload: missing MANIFEST.in")
    else:
        inputs.append("MANIFEST.in")
        try:
            findings.extend(_manifest_findings(manifest_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError) as error:
            findings.append(f"payload: cannot inspect MANIFEST.in: {type(error).__name__}")
    return _check("package-payload-manifests", inputs, findings)


def _manifest_findings(text: str) -> list[str]:
    commands = [
        " ".join(line.split())
        for raw in text.splitlines()
        if (line := raw.split("#", 1)[0].strip())
    ]
    findings: list[str] = []
    for target in _COMPARATOR_PATHS:
        path = "/".join(target)
        broad_root = target[0]
        include_indexes = [
            index
            for index, command in enumerate(commands)
            if command.startswith((f"graft {broad_root}", f"recursive-include {broad_root} "))
            or (
                command.startswith(("include ", "recursive-include ", "graft ")) and path in command
            )
        ]
        if not include_indexes:
            continue
        last_include = max(include_indexes)
        excluded_after = any(
            index > last_include
            and command
            in {
                f"prune {path}",
                f"recursive-exclude {path} *",
            }
            for index, command in enumerate(commands)
        )
        if not excluded_after:
            findings.append(f"payload: MANIFEST.in does not exclude comparator path {path}")
    report_include_indexes = [
        index
        for index, command in enumerate(commands)
        if command.startswith(("graft reports", "recursive-include reports "))
        or (
            command.startswith(("include ", "recursive-include ", "graft "))
            and _DETACHED_AUDIT_DIRECTORY in command
        )
    ]
    if report_include_indexes:
        last_include = max(report_include_indexes)
        detached_after = any(
            index > last_include
            and command
            in {
                f"exclude {_DETACHED_AUDIT_DIRECTORY}/{_DETACHED_AUDIT_PATTERN}",
                f"recursive-exclude {_DETACHED_AUDIT_DIRECTORY} {_DETACHED_AUDIT_PATTERN}",
            }
            for index, command in enumerate(commands)
        )
        if not detached_after:
            findings.append("payload: MANIFEST.in does not detach dependency-audit evidence")
    return findings


def _artifact_check(
    root: Path,
    artifacts: Sequence[Path | ArtifactExpectation],
) -> dict[str, object]:
    if not artifacts:
        return {
            "status": "not-run",
            "reason": "no built wheel or sdist was supplied",
            "findings": [],
            "artifacts": [],
            "platform_linkage": _platform_linkage_disclosure(),
        }

    expectations: list[ArtifactExpectation] = []
    findings: list[str] = []
    for value in artifacts:
        if isinstance(value, ArtifactExpectation):
            expectation = value
        elif isinstance(value, Path):
            expectation = ArtifactExpectation(path=value)
        else:
            findings.append(f"artifact set: unsupported expectation type {type(value).__name__}")
            continue
        expectations.append(
            ArtifactExpectation(
                path=expectation.path.resolve(),
                kind=expectation.kind,
                sha256=expectation.sha256,
            )
        )

    resolved_paths = [value.path for value in expectations]
    duplicate_paths = sorted(
        (path for path in set(resolved_paths) if resolved_paths.count(path) > 1),
        key=str,
    )
    findings.extend(
        f"artifact set: duplicate path {_display_path(root, value)}" for value in duplicate_paths
    )

    project_name, project_version, project_error = _project_identity(root)
    if project_error is not None:
        findings.append(project_error)
    rows = [
        _inspect_artifact(
            root,
            expectation,
            expected_name=project_name,
            expected_version=project_version,
        )
        for expectation in expectations
    ]
    rows.sort(key=lambda row: (cast(str, row["kind"]), cast(str, row["path"])))

    digests = [cast(str, row["sha256"]) for row in rows if isinstance(row.get("sha256"), str)]
    duplicate_digests = sorted(digest for digest in set(digests) if digests.count(digest) > 1)
    findings.extend(
        f"artifact set: duplicate content SHA-256 {digest}" for digest in duplicate_digests
    )
    status = (
        "fail" if findings or not rows or any(row["status"] == "fail" for row in rows) else "pass"
    )
    return {
        "status": status,
        "reason": None,
        "findings": sorted(set(findings)),
        "artifacts": rows,
        "platform_linkage": _platform_linkage_disclosure(),
    }


def _platform_linkage_disclosure() -> dict[str, str]:
    return {
        "status": "not-run",
        "reason": "static archive inspection does not perform a platform linkage audit",
    }


def _inspect_artifact(
    root: Path,
    expectation: ArtifactExpectation,
    *,
    expected_name: str,
    expected_version: str,
) -> dict[str, object]:
    path = expectation.path
    label = _display_path(root, path)
    findings: list[str] = []
    member_count = 0
    total_bytes = 0
    digest: str | None = None
    kind = "unknown"
    expected_kind = expectation.kind or _filename_kind(path)
    structure: dict[str, object] = {}
    marker_evidence: dict[str, object] = {
        "status": "not-applicable",
        "method": "bounded static byte-marker scan; not a platform linkage audit",
        "libraries": [],
    }
    try:
        digest = _sha256_file(path)
        if expectation.sha256 is not None and digest != expectation.sha256:
            findings.append("artifact: SHA-256 differs from expected binding")
        inferred_kind = _filename_kind(path)
        if expected_kind is None:
            findings.append("artifact: filename does not declare wheel or tar-sdist kind")
        if (
            expectation.kind is not None
            and inferred_kind is not None
            and expectation.kind != inferred_kind
        ):
            findings.append("artifact: explicit kind differs from the distribution filename")

        if path.suffix.casefold() == ".whl" and zipfile.is_zipfile(path):
            kind = "wheel"
            members = _zip_members(path)
        elif tarfile.is_tarfile(path):
            kind = "sdist"
            members = _tar_members(path)
        else:
            members = ()
            findings.append("artifact: supplied file is neither a wheel nor a tar sdist")
        if expected_kind is not None and kind != expected_kind:
            findings.append(
                f"artifact: detected kind {kind!r} differs from expected {expected_kind!r}"
            )

        member_count = len(members)
        total_bytes = sum(size for _name, size, _payload in members)
        archive_findings = _archive_findings(members)
        findings.extend(archive_findings)
        if not archive_findings and kind in {"wheel", "sdist"}:
            inspected = inspect_artifact(path, expected_version=expected_version)
            structure = {
                "variant": inspected.variant,
                "metadata": inspected.metadata,
                "release_blockers": list(inspected.release_blockers),
                "deferred_platform_checks": list(inspected.deferred_platform_checks),
            }
            findings.extend(f"structure: {value}" for value in inspected.errors)
            if kind == "wheel":
                findings.extend(
                    _wheel_identity_findings(
                        path,
                        members,
                        expected_name=expected_name,
                        expected_version=expected_version,
                    )
                )
            else:
                findings.extend(
                    _sdist_identity_findings(
                        path,
                        members,
                        expected_name=expected_name,
                        expected_version=expected_version,
                    )
                )
            findings.extend(_artifact_member_findings(members))
            marker_evidence, marker_findings = _dynamic_library_marker_evidence(members)
            findings.extend(marker_findings)
    except (
        AssertionError,
        KeyError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        csv.Error,
        tarfile.TarError,
        tomllib.TOMLDecodeError,
        zipfile.BadZipFile,
    ) as error:
        findings.append(f"artifact: cannot inspect: {type(error).__name__}")
    return {
        "path": label,
        "kind": kind,
        "expected_kind": expected_kind,
        "sha256": digest,
        "expected_sha256": expectation.sha256,
        "sha256_bound": expectation.sha256 is not None,
        "member_count": member_count,
        "uncompressed_bytes": total_bytes,
        "structure": structure,
        "dynamic_library_markers": marker_evidence,
        "status": "fail" if findings else "pass",
        "findings": sorted(set(findings)),
    }


def _project_identity(root: Path) -> tuple[str, str, str | None]:
    try:
        payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = _mapping(payload.get("project"))
        name = project.get("name")
        version = project.get("version")
        if not isinstance(name, str) or _normalize_name(name) != "pyowl-core":
            raise ValueError("project name is not pyowl-core")
        if not isinstance(version, str) or not version:
            raise ValueError("project version is absent")
        return name, version, None
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as error:
        return (
            "pyowl-core",
            "",
            f"artifact set: cannot establish project identity: {type(error).__name__}",
        )


def _filename_kind(path: Path) -> ArtifactKind | None:
    lowered = path.name.casefold()
    if lowered.endswith(".whl"):
        return "wheel"
    if lowered.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        return "sdist"
    return None


def _archive_findings(
    members: Sequence[tuple[str, int, bytes | None]],
) -> list[str]:
    findings: list[str] = []
    names = [name for name, _size, _payload in members]
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        findings.append("artifact: member count exceeds audit limit")
    if len(set(names)) != len(names):
        findings.append("artifact: duplicate archive member name")
    if len({name.casefold() for name in names}) != len(names):
        findings.append("artifact: case-insensitive archive member collision")
    total_bytes = 0
    for name, size, _payload in members:
        normalized = PurePosixPath(name.replace("\\", "/"))
        if (
            normalized.is_absolute()
            or not normalized.parts
            or any(part in {"", ".", ".."} for part in normalized.parts)
        ):
            findings.append(f"artifact: unsafe archive member path {name!r}")
        if size < 0:
            findings.append(f"artifact: negative member size {name}")
        elif size > _MAX_BINARY_SCAN_BYTES:
            findings.append(f"artifact: member exceeds inspection byte limit {name}")
        total_bytes += max(0, size)
    if total_bytes > _MAX_ARCHIVE_BYTES:
        findings.append("artifact: uncompressed bytes exceed audit limit")
    return findings


def _wheel_identity_findings(
    path: Path,
    members: Sequence[tuple[str, int, bytes | None]],
    *,
    expected_name: str,
    expected_version: str,
) -> list[str]:
    findings: list[str] = []
    distribution = re.sub(r"[-_.]+", "_", expected_name)
    version = expected_version.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    expected_members = {
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
        f"{dist_info}/WHEEL",
    }
    names = [name for name, _size, _payload in members]
    name_set = set(names)
    missing = sorted(expected_members - name_set)
    if missing:
        findings.append("wheel: missing identity member(s): " + ", ".join(missing))
    dist_info_roots = {
        PurePosixPath(name).parts[0]
        for name in names
        if PurePosixPath(name).parts
        and PurePosixPath(name).parts[0].casefold().endswith(".dist-info")
    }
    if dist_info_roots != {dist_info}:
        findings.append("wheel: dist-info root does not exactly match project identity")
    if "pyowl_core/__init__.py" not in name_set:
        findings.append("wheel: missing pyowl_core/__init__.py")
    if not path.name.startswith(f"{distribution}-{version}-"):
        findings.append("wheel: filename does not match project name/version")

    record_name = f"{dist_info}/RECORD"
    record_payload = next(
        (payload for name, _size, payload in members if name == record_name),
        None,
    )
    if record_payload is not None:
        rows = list(csv.reader(io.StringIO(record_payload.decode("utf-8"))))
        malformed = [row for row in rows if len(row) != 3 or not row[0]]
        if malformed:
            findings.append("wheel: RECORD contains malformed rows")
        recorded_names = [row[0] for row in rows if len(row) == 3 and row[0]]
        if len(set(recorded_names)) != len(recorded_names):
            findings.append("wheel: RECORD contains duplicate member rows")
        if set(recorded_names) != name_set:
            findings.append("wheel: RECORD member set does not match archive")
    return findings


def _sdist_identity_findings(
    path: Path,
    members: Sequence[tuple[str, int, bytes | None]],
    *,
    expected_name: str,
    expected_version: str,
) -> list[str]:
    findings: list[str] = []
    distribution = re.sub(r"[-_.]+", "_", expected_name)
    expected_root = f"{distribution}-{expected_version}"
    names = [name for name, _size, _payload in members]
    roots = {
        PurePosixPath(name.replace("\\", "/")).parts[0]
        for name in names
        if PurePosixPath(name.replace("\\", "/")).parts
    }
    if roots != {expected_root}:
        findings.append("sdist: archive root does not exactly match project identity")
    required = {
        f"{expected_root}/PKG-INFO",
        f"{expected_root}/pyproject.toml",
        f"{expected_root}/src/pyowl_core/__init__.py",
    }
    missing = sorted(required - set(names))
    if missing:
        findings.append("sdist: missing identity/layout member(s): " + ", ".join(missing))
    if not path.name.startswith(f"{expected_root}."):
        findings.append("sdist: filename does not match project name/version")

    pyproject_name = f"{expected_root}/pyproject.toml"
    pyproject_payload = next(
        (payload for name, _size, payload in members if name == pyproject_name),
        None,
    )
    if pyproject_payload is not None:
        payload = tomllib.loads(pyproject_payload.decode("utf-8"))
        project = _mapping(payload.get("project"))
        if project.get("name") != expected_name or project.get("version") != expected_version:
            findings.append("sdist: pyproject identity differs from project source")
    return findings


def _dynamic_library_marker_evidence(
    members: Sequence[tuple[str, int, bytes | None]],
) -> tuple[dict[str, object], list[str]]:
    rows: list[dict[str, object]] = []
    findings: list[str] = []
    for name, size, payload in members:
        if not name.casefold().endswith(_NATIVE_SUFFIXES):
            continue
        if payload is None:
            rows.append(
                {
                    "path": name,
                    "bytes": size,
                    "sha256": None,
                    "markers": [],
                    "status": "not-scanned",
                }
            )
            findings.append(f"artifact: native library was not marker-scanned {name}")
            continue
        lowered = payload.lower()
        markers = sorted(
            marker.decode("ascii") for marker in _BINARY_MARKERS if marker.lower() in lowered
        )
        rows.append(
            {
                "path": name,
                "bytes": size,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "markers": markers,
                "status": "fail" if markers else "pass",
            }
        )
        findings.extend(
            f"artifact: forbidden comparator/JVM marker {marker} in {name}" for marker in markers
        )
    status = "not-applicable" if not rows else ("fail" if findings else "pass")
    return (
        {
            "status": status,
            "method": "bounded static byte-marker scan; not a platform linkage audit",
            "libraries": rows,
        },
        findings,
    )


def _zip_members(path: Path) -> tuple[tuple[str, int, bytes | None], ...]:
    rows: list[tuple[str, int, bytes | None]] = []
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        read_payloads = (
            len(entries) <= _MAX_ARCHIVE_MEMBERS
            and sum(info.file_size for info in entries) <= _MAX_ARCHIVE_BYTES
        )
        for info in entries:
            payload = (
                _bounded_archive_payload(info.filename, info.file_size, archive.read)
                if read_payloads
                else None
            )
            rows.append((info.filename, info.file_size, payload))
    return tuple(rows)


def _tar_members(path: Path) -> tuple[tuple[str, int, bytes | None], ...]:
    rows: list[tuple[str, int, bytes | None]] = []
    with tarfile.open(path, "r:*") as archive:
        entries = archive.getmembers()
        read_payloads = (
            len(entries) <= _MAX_ARCHIVE_MEMBERS
            and sum(info.size for info in entries) <= _MAX_ARCHIVE_BYTES
        )
        for info in entries:
            payload: bytes | None = None
            if read_payloads and info.isfile() and _should_read_member(info.name, info.size):
                stream = archive.extractfile(info)
                if stream is not None:
                    payload = stream.read()
            rows.append((info.name, info.size, payload))
    return tuple(rows)


def _bounded_archive_payload(
    name: str,
    size: int,
    reader: Callable[[str], bytes],
) -> bytes | None:
    if not _should_read_member(name, size):
        return None
    return reader(name)


def _should_read_member(name: str, size: int) -> bool:
    path = PurePosixPath(name)
    lowered = path.name.casefold()
    if (
        path.suffix.casefold() in {".py", ".pyi"}
        or lowered in _ARTIFACT_DEPENDENCY_FILES
        or lowered in {"metadata", "pkg-info", "record", "wheel"}
        or lowered.startswith("requirements")
    ):
        return size <= _MAX_TEXT_BYTES
    return name.casefold().endswith(_NATIVE_SUFFIXES) and size <= _MAX_BINARY_SCAN_BYTES


def _artifact_member_findings(
    members: Iterable[tuple[str, int, bytes | None]],
) -> list[str]:
    findings: list[str] = []
    for name, _size, payload in members:
        normalized = PurePosixPath(name.replace("\\", "/"))
        if normalized.suffix.casefold() in _JAVA_SUFFIXES:
            findings.append(f"artifact: forbidden Java member {name}")
        folded_parts = tuple(part.casefold() for part in normalized.parts)
        for target in _COMPARATOR_PATHS:
            if _contains_path(folded_parts, target):
                findings.append(f"artifact: forbidden comparator path {name}")
                break
        is_python_source = normalized.suffix.casefold() in {".py", ".pyi"}
        if is_python_source and payload is None:
            findings.append(f"artifact: Python source was not inspected within byte limits {name}")
        if payload is None:
            continue
        if is_python_source:
            try:
                source = payload.decode("utf-8")
            except UnicodeError as error:
                findings.append(
                    f"artifact: Python source cannot be decoded {name}: {type(error).__name__}"
                )
            else:
                findings.extend(_python_source_findings(name, "artifact", source))
        if normalized.name.casefold() in {"metadata", "pkg-info"}:
            for dependency in _metadata_requires_dist(payload):
                if _normalize_name(dependency) in _BANNED_DEPENDENCIES:
                    findings.append(
                        f"artifact: forbidden dependency {_normalize_name(dependency)} in {name}"
                    )
        elif normalized.name.casefold() == "pyproject.toml":
            for _scope, dependency in _pyproject_dependencies(payload):
                if _normalize_name(dependency) in _BANNED_DEPENDENCIES:
                    findings.append(
                        f"artifact: forbidden dependency {_normalize_name(dependency)} in {name}"
                    )
        elif normalized.name.casefold() == "cargo.toml":
            for _scope, dependency in _cargo_manifest_dependencies(payload):
                if _normalize_name(dependency) in _BANNED_DEPENDENCIES:
                    findings.append(
                        f"artifact: forbidden dependency {_normalize_name(dependency)} in {name}"
                    )
        elif normalized.name.casefold() == "cargo.lock":
            for _scope, dependency in _cargo_lock_dependencies(payload):
                if _normalize_name(dependency) in _BANNED_DEPENDENCIES:
                    findings.append(
                        f"artifact: forbidden dependency {_normalize_name(dependency)} in {name}"
                    )
        elif normalized.name.casefold() == "inventory.toml":
            for _scope, dependency in _inventory_dependencies(payload):
                if _normalize_name(dependency) in _BANNED_DEPENDENCIES:
                    findings.append(
                        f"artifact: forbidden dependency {_normalize_name(dependency)} in {name}"
                    )
        elif (
            normalized.name.casefold() in _ARTIFACT_DEPENDENCY_FILES
            or normalized.name.casefold().startswith("requirements")
        ):
            for _scope, dependency in _text_dependencies(payload.decode("utf-8")):
                if _normalize_name(dependency) in _BANNED_DEPENDENCIES:
                    findings.append(
                        f"artifact: forbidden dependency {_normalize_name(dependency)} in {name}"
                    )
    return findings


def _metadata_requires_dist(payload: bytes) -> tuple[str, ...]:
    text = payload.decode("utf-8", errors="replace")
    names: list[str] = []
    for line in text.splitlines():
        if not line.casefold().startswith("requires-dist:"):
            continue
        name = _requirement_name(line.split(":", 1)[1])
        if name is not None:
            names.append(name)
    return tuple(names)


def _requirements(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for raw in value:
        if isinstance(raw, str) and (name := _requirement_name(raw)) is not None:
            names.append(name)
    return tuple(names)


def _requirement_name(value: str) -> str | None:
    match = _REQUIREMENT_NAME.match(value)
    return None if match is None else match.group(1)


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _check(identifier: str, inputs: Iterable[str], findings: Iterable[str]) -> dict[str, object]:
    selected_findings = sorted(set(findings))
    return {
        "id": identifier,
        "status": "fail" if selected_findings else "pass",
        "inputs": sorted(set(inputs)),
        "findings": selected_findings,
    }


def _skip(path: Path, root: Path) -> bool:
    return any(part in _SKIP_PARTS for part in path.relative_to(root).parts)


def _is_comparator_path(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    return any(_contains_path(parts, target) for target in _COMPARATOR_PATHS)


def _contains_path(parts: Sequence[str], target: Sequence[str]) -> bool:
    size = len(target)
    return any(tuple(parts[index : index + size]) == tuple(target) for index in range(len(parts)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", action="append", default=[], type=Path)
    parser.add_argument(
        "--artifact-kind",
        action="append",
        choices=("wheel", "sdist"),
        help="expected kind for the corresponding --artifact (repeat for every artifact)",
    )
    parser.add_argument(
        "--artifact-sha256",
        action="append",
        help="expected lowercase SHA-256 for the corresponding --artifact",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="return success for an explicit development-only not-run audit",
    )
    parser.add_argument("--output", type=Path, help="write canonical JSON evidence to this path")
    args = parser.parse_args(argv)
    paths = cast(list[Path], args.artifact)
    kinds = cast(list[ArtifactKind], args.artifact_kind or [])
    digests = cast(list[str], args.artifact_sha256 or [])
    if kinds and len(kinds) != len(paths):
        parser.error("--artifact-kind must be repeated once for every --artifact")
    if digests and len(digests) != len(paths):
        parser.error("--artifact-sha256 must be repeated once for every --artifact")
    try:
        expectations = tuple(
            ArtifactExpectation(
                path=path,
                kind=None if not kinds else kinds[index],
                sha256=None if not digests else digests[index],
            )
            for index, path in enumerate(paths)
        )
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    evidence = audit_dependency_exclusion(args.root, expectations)
    rendered = json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    if evidence["status"] == "pass":
        return 0
    if evidence["status"] == "not-run" and args.allow_partial:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "ArtifactExpectation", "audit_dependency_exclusion", "main"]
