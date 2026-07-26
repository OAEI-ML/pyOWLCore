"""Inspect pyowl-core wheels and sdists without installing or executing them."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import stat
import tarfile
import zipfile
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

ArtifactKind = Literal["wheel", "sdist"]
ArtifactVariant = Literal["pure", "native", "sdist"]
_MAX_MEMBERS = 100_000
_MAX_MEMBER_BYTES = 128 * 1024**2
_MAX_TOTAL_BYTES = 1024 * 1024**2
_NATIVE_SUFFIXES = (".dylib", ".dll", ".pyd", ".so")
_JAVA_SUFFIXES = (".class", ".ear", ".jar", ".jmod", ".war")
_FORBIDDEN_PATH_PARTS = {
    ".git",
    ".gradle",
    ".m2",
    "__pycache__",
    "node_modules",
    "target",
}
_FORBIDDEN_NAMES = {
    "build.gradle",
    "build.gradle.kts",
    "gradlew",
    "gradlew.bat",
    "pom.xml",
}
_JAVA_DEPENDENCY_TEXT = re.compile(
    rb"(?i)(?:\bjpype1?\b|\bdeeponto\b|\bmowl\b|org\.semanticweb\.owlapi|"
    rb"(?:^|[=<'\"\s,])robot(?:$|[>='\"\s,;]))"
)
_JAVA_RUNTIME_TEXT = (
    re.compile(rb"(?m)^\s*(?:from|import)\s+(?:jpype|deeponto|mowl)(?:\.|\s|$)"),
    re.compile(rb"(?i)\b(?:subprocess\.(?:run|call|popen)|Popen)\s*\([^\n]*['\"]java['\"]"),
    re.compile(rb"(?i)\b(?:owlapi|org\.semanticweb\.owlapi)\b"),
)
_EXTRA_MARKER = re.compile(
    r"""(?:extra\s*==\s*(?:"[^"\r\n]+"|'[^'\r\n]+')|"""
    r"""(?:"[^"\r\n]+"|'[^'\r\n]+')\s*==\s*extra)\Z"""
)
_MAX_MARKER_CHARACTERS = 8192
_MAX_MARKER_DEPTH = 64
_TEXT_SUFFIXES = {".cfg", ".ini", ".lock", ".md", ".py", ".toml", ".txt"}
_DEPENDENCY_FILENAMES = {
    "cargo.lock",
    "cargo.toml",
    "pdm.lock",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
}
_REQUIRED_LICENSE_BASENAMES = {
    "LICENSE",
    "NOTICE",
    "LLVM-exception.txt",
    "Unicode-3.0.txt",
    "inventory.toml",
}


class ArchiveReader(Protocol):
    def names(self) -> tuple[str, ...]: ...

    def read(self, name: str) -> bytes: ...

    def mode(self, name: str) -> int: ...

    def sizes(self) -> tuple[int, ...]: ...


@dataclass(frozen=True, slots=True)
class InspectionResult:
    """Machine-readable artifact facts, violations, and external blockers."""

    path: str
    kind: ArtifactKind
    variant: ArtifactVariant
    member_count: int
    uncompressed_bytes: int
    metadata: dict[str, str]
    errors: tuple[str, ...]
    release_blockers: tuple[str, ...]
    deferred_platform_checks: tuple[str, ...]
    non_native_payload_sha256: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def release_ready(self) -> bool:
        return not self.errors and not self.release_blockers and not self.deferred_platform_checks

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["ok"] = self.ok
        value["release_ready"] = self.release_ready
        return value


class _WheelReader:
    def __init__(self, path: Path) -> None:
        self._archive = zipfile.ZipFile(path)
        self._entries = tuple(self._archive.infolist())
        self._info = {entry.filename: entry for entry in self._entries}

    def names(self) -> tuple[str, ...]:
        return tuple(entry.filename for entry in self._entries)

    def read(self, name: str) -> bytes:
        return self._archive.read(name)

    def mode(self, name: str) -> int:
        return self._info[name].external_attr >> 16

    def sizes(self) -> tuple[int, ...]:
        return tuple(entry.file_size for entry in self._entries)

    def close(self) -> None:
        self._archive.close()


class _SdistReader:
    def __init__(self, path: Path) -> None:
        self._archive = tarfile.open(path, "r:*")  # noqa: SIM115 - closed by reader
        self._info = {entry.name: entry for entry in self._archive.getmembers()}

    def names(self) -> tuple[str, ...]:
        return tuple(self._info)

    def read(self, name: str) -> bytes:
        stream = self._archive.extractfile(self._info[name])
        return b"" if stream is None else stream.read()

    def mode(self, name: str) -> int:
        return self._info[name].mode

    def sizes(self) -> tuple[int, ...]:
        return tuple(entry.size for entry in self._info.values())

    def unsafe_links(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self._info.values() if entry.issym() or entry.islnk())

    def close(self) -> None:
        self._archive.close()


def _artifact_kind(path: Path) -> ArtifactKind:
    if path.suffix == ".whl" and zipfile.is_zipfile(path):
        return "wheel"
    if tarfile.is_tarfile(path):
        return "sdist"
    raise ValueError(f"unsupported or corrupt artifact: {path}")


def _normalized_member(name: str) -> PurePosixPath | None:
    if "\\" in name or any(ord(character) < 32 or ord(character) == 127 for character in name):
        return None
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.match(r"[A-Za-z]:", path.parts[0]) is not None
    ):
        return None
    canonical = path.as_posix()
    if name not in {canonical, f"{canonical}/"}:
        return None
    return path


def _split_marker_expression(
    expression: str,
    operator: str,
) -> tuple[str, ...] | None:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    position = 0
    while position < len(expression):
        character = expression[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            position += 1
            continue
        if character in {"'", '"'}:
            quote = character
            position += 1
            continue
        if character == "(":
            depth += 1
            position += 1
            continue
        if character == ")":
            depth -= 1
            if depth < 0:
                return None
            position += 1
            continue
        end = position + len(operator)
        before = expression[position - 1] if position else " "
        after = expression[end] if end < len(expression) else " "
        if (
            depth == 0
            and expression[position:end].casefold() == operator
            and not (before.isalnum() or before == "_")
            and not (after.isalnum() or after == "_")
        ):
            part = expression[start:position].strip()
            if not part:
                return None
            parts.append(part)
            start = end
            position = end
            continue
        position += 1
    if quote is not None or depth != 0:
        return None
    final = expression[start:].strip()
    if not final:
        return None
    parts.append(final)
    return tuple(parts)


def _has_enclosing_parentheses(expression: str) -> bool:
    if not expression.startswith("(") or not expression.endswith(")"):
        return False
    depth = 0
    quote: str | None = None
    escaped = False
    for position, character in enumerate(expression):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and position != len(expression) - 1:
                return False
    return depth == 0 and quote is None


def _marker_requires_extra(expression: str, *, depth: int = 0) -> bool:
    expression = expression.strip()
    if not expression or len(expression) > _MAX_MARKER_CHARACTERS or depth >= _MAX_MARKER_DEPTH:
        return False
    if _has_enclosing_parentheses(expression):
        return _marker_requires_extra(expression[1:-1], depth=depth + 1)
    alternatives = _split_marker_expression(expression, "or")
    if alternatives is None:
        return False
    if len(alternatives) > 1:
        return all(_marker_requires_extra(part, depth=depth + 1) for part in alternatives)
    conjunctions = _split_marker_expression(expression, "and")
    if conjunctions is None:
        return False
    if len(conjunctions) > 1:
        return any(_marker_requires_extra(part, depth=depth + 1) for part in conjunctions)
    return _EXTRA_MARKER.fullmatch(expression) is not None


def _is_extra_only_requirement(requirement: str) -> bool:
    package, separator, marker = requirement.partition(";")
    return bool(package.strip() and separator and _marker_requires_extra(marker))


def _metadata(reader: ArchiveReader, kind: ArtifactKind) -> tuple[dict[str, str], str | None]:
    suffix = ".dist-info/METADATA" if kind == "wheel" else "/PKG-INFO"
    candidates = [
        name
        for name in reader.names()
        if name.endswith(suffix) and len(PurePosixPath(name).parts) == 2
    ]
    if len(candidates) != 1:
        return {}, f"metadata: expected exactly one {suffix}, found {len(candidates)}"
    message = BytesParser(policy=default).parsebytes(reader.read(candidates[0]))
    fields = {
        "Name": str(message.get("Name", "")),
        "Version": str(message.get("Version", "")),
        "Requires-Python": str(message.get("Requires-Python", "")),
        "License-Expression": str(message.get("License-Expression", "")),
    }
    fields["Project-URL-Count"] = str(len(message.get_all("Project-URL", [])))
    fields["Requires-Dist-Count"] = str(len(message.get_all("Requires-Dist", [])))
    runtime_requirements = [
        str(requirement)
        for requirement in message.get_all("Requires-Dist", [])
        if not _is_extra_only_requirement(str(requirement))
    ]
    if runtime_requirements:
        fields["Runtime-Requires-Dist"] = " | ".join(runtime_requirements)
    return fields, None


def _validate_metadata(
    metadata: dict[str, str], expected_version: str, require_project_urls: bool
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    blockers: list[str] = []
    expected = {
        "Name": "pyowl-core",
        "Version": expected_version,
        "Requires-Python": ">=3.10",
        "License-Expression": "Apache-2.0",
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            errors.append(f"metadata: {field} is {metadata.get(field, '')!r}, expected {value!r}")
    if "Runtime-Requires-Dist" in metadata:
        errors.append(
            f"metadata: unexpected runtime dependency {metadata['Runtime-Requires-Dist']}"
        )
    if metadata.get("Project-URL-Count") == "0":
        message = "metadata: approved repository/docs/issues URLs are not configured"
        if require_project_urls:
            errors.append(message)
        else:
            blockers.append(message)
    return errors, blockers


def _validate_common(reader: ArchiveReader) -> tuple[list[str], int, bool]:
    errors: list[str] = []
    names = reader.names()
    sizes = reader.sizes()
    resource_safe = True
    if len(sizes) != len(names):
        errors.append("archive: member metadata count does not match member names")
        resource_safe = False
    if len(names) > _MAX_MEMBERS:
        errors.append(f"archive: member count {len(names)} exceeds {_MAX_MEMBERS}")
        resource_safe = False
    if len(set(names)) != len(names):
        errors.append("archive: duplicate member name")
    if len({name.casefold() for name in names}) != len(names):
        errors.append("archive: case-insensitive member collision")
    normalized_names = [
        normalized.as_posix().casefold()
        for name in names
        if (normalized := _normalized_member(name)) is not None
    ]
    if len(set(normalized_names)) != len(normalized_names):
        errors.append("archive: normalized member collision")
    total = sum(max(size, 0) for size in sizes)
    for name, size in zip(names, sizes, strict=False):
        if size < 0:
            errors.append(f"archive: member has negative byte size {name}")
            resource_safe = False
        elif size > _MAX_MEMBER_BYTES:
            errors.append(f"archive: member exceeds byte limit {name}")
            resource_safe = False
    if total > _MAX_TOTAL_BYTES:
        errors.append(f"archive: uncompressed bytes {total} exceed {_MAX_TOTAL_BYTES}")
        resource_safe = False
    for name in names:
        normalized = _normalized_member(name)
        if normalized is None:
            errors.append(f"archive: unsafe member path {name!r}")
            normalized = PurePosixPath(name.replace("\\", "/"))
        lowered_parts = {part.casefold() for part in normalized.parts}
        if lowered_parts & _FORBIDDEN_PATH_PARTS:
            errors.append(f"archive: forbidden build/cache path {name}")
        if normalized.name.casefold() in _FORBIDDEN_NAMES:
            errors.append(f"java: forbidden build file {name}")
        suffix = normalized.suffix.casefold()
        if suffix in _JAVA_SUFFIXES:
            errors.append(f"java: forbidden artifact {name}")
    if not resource_safe:
        return errors, total, False
    for name, declared_size in zip(names, sizes, strict=True):
        normalized = _normalized_member(name)
        if normalized is None:
            normalized = PurePosixPath(name.replace("\\", "/"))
        suffix = normalized.suffix.casefold()
        payload = reader.read(name)
        if len(payload) != declared_size:
            errors.append(f"archive: member byte size differs from archive metadata {name}")
        if suffix in _TEXT_SUFFIXES and len(payload) <= 4 * 1024**2:
            if normalized.name.casefold() in _DEPENDENCY_FILENAMES and _JAVA_DEPENDENCY_TEXT.search(
                payload
            ):
                errors.append(f"java: forbidden dependency declaration {name}")
            normalized_text_path = "/" + normalized.as_posix().lstrip("/")
            if "/src/pyowl_core/" in normalized_text_path and any(
                pattern.search(payload) for pattern in _JAVA_RUNTIME_TEXT
            ):
                errors.append(f"java: forbidden runtime integration {name}")
        if suffix == ".pth":
            errors.append(f"side-effect: .pth startup hook is forbidden: {name}")
    return errors, total, True


def _validate_licenses(names: tuple[str, ...], kind: ArtifactKind) -> list[str]:
    basenames = {PurePosixPath(name).name for name in names}
    missing = sorted(_REQUIRED_LICENSE_BASENAMES - basenames)
    if missing:
        return [f"license: missing required files in {kind}: {', '.join(missing)}"]
    return []


def _wheel_variant(reader: ArchiveReader) -> ArtifactVariant:
    binaries = [
        name
        for name in reader.names()
        if name.startswith("pyowl_core/") and name.casefold().endswith(_NATIVE_SUFFIXES)
    ]
    return "native" if binaries else "pure"


def _validate_record(reader: ArchiveReader) -> list[str]:
    errors: list[str] = []
    candidates = [name for name in reader.names() if name.endswith(".dist-info/RECORD")]
    if len(candidates) != 1:
        return [f"wheel: expected exactly one RECORD, found {len(candidates)}"]
    record_name = candidates[0]
    rows = list(csv.reader(io.StringIO(reader.read(record_name).decode("utf-8"))))
    valid_rows = [row for row in rows if len(row) == 3 and row[0]]
    if len(valid_rows) != len(rows):
        errors.append("wheel: RECORD contains malformed rows")
    recorded_names = [row[0] for row in valid_rows]
    if len(set(recorded_names)) != len(recorded_names):
        errors.append("wheel: RECORD contains duplicate member rows")
    recorded = {row[0]: row for row in valid_rows}
    if set(recorded) != set(reader.names()):
        errors.append("wheel: RECORD member set does not match archive")
    for name, row in recorded.items():
        digest, size = row[1], row[2]
        if name == record_name:
            if digest or size:
                errors.append("wheel: RECORD must not hash itself")
            continue
        payload = reader.read(name)
        expected_digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        if digest != "sha256=" + expected_digest.decode("ascii"):
            errors.append(f"wheel: RECORD digest mismatch {name}")
        if size != str(len(payload)):
            errors.append(f"wheel: RECORD size mismatch {name}")
    return errors


def _non_native_payload_sha256(reader: ArchiveReader) -> str:
    digest = hashlib.sha256(b"pyowl-core:wheel-non-native-payload:v1\0")
    names: list[str] = []
    for name in reader.names():
        parts = PurePosixPath(name).parts
        if (
            parts
            and not name.endswith("/")
            and not parts[0].casefold().endswith(".dist-info")
            and not name.casefold().endswith(_NATIVE_SUFFIXES)
        ):
            names.append(name)
    for name in sorted(names):
        encoded_name = name.encode("utf-8")
        payload = reader.read(name)
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _validate_wheel(
    reader: ArchiveReader,
    variant: ArtifactVariant,
    filename: str,
    expected_version: str,
) -> list[str]:
    errors = _validate_record(reader)
    names = reader.names()
    distribution = re.sub(r"[-_.]+", "_", "pyowl-core")
    version = expected_version.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    required_identity_members = {
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
        f"{dist_info}/WHEEL",
        "pyowl_core/__init__.py",
    }
    missing_identity = sorted(required_identity_members - set(names))
    if missing_identity:
        errors.append("wheel: missing identity member(s): " + ", ".join(missing_identity))
    dist_info_roots = {
        PurePosixPath(name).parts[0]
        for name in names
        if PurePosixPath(name).parts
        and PurePosixPath(name).parts[0].casefold().endswith(".dist-info")
    }
    if dist_info_roots != {dist_info}:
        errors.append("wheel: dist-info root does not exactly match project identity")
    if not filename.startswith(f"{distribution}-{version}-"):
        errors.append("wheel: filename does not match project name/version")
    wheel_files = [name for name in reader.names() if name.endswith(".dist-info/WHEEL")]
    if len(wheel_files) != 1:
        return [*errors, f"wheel: expected exactly one WHEEL file, found {len(wheel_files)}"]
    wheel_message = BytesParser(policy=default).parsebytes(reader.read(wheel_files[0]))
    wheel_versions = [str(value).strip() for value in wheel_message.get_all("Wheel-Version", [])]
    if wheel_versions != ["1.0"]:
        errors.append(f"wheel: Wheel-Version must be exactly '1.0', got {wheel_versions!r}")
    purelib_values = [str(value).strip() for value in wheel_message.get_all("Root-Is-Purelib", [])]
    expected_purelib = "true" if variant == "pure" else "false"
    if purelib_values != [expected_purelib]:
        errors.append(
            "wheel: Root-Is-Purelib must be exactly "
            f"{expected_purelib!r} for {variant}, got {purelib_values!r}"
        )
    tags = [str(value).strip() for value in wheel_message.get_all("Tag", [])]
    binaries = [name for name in reader.names() if name.casefold().endswith(_NATIVE_SUFFIXES)]
    if variant == "pure":
        if binaries:
            errors.append(f"wheel: pure artifact contains native binaries: {', '.join(binaries)}")
        if tags != ["py3-none-any"] or not filename.endswith("-py3-none-any.whl"):
            errors.append(f"wheel: pure artifact has non-universal tags {tags!r}")
    elif variant == "native":
        expected_binary = [
            name
            for name in binaries
            if PurePosixPath(name).parent == PurePosixPath("pyowl_core")
            and PurePosixPath(name).name.startswith("_native.")
        ]
        if len(expected_binary) != 1 or len(binaries) != 1:
            errors.append(
                "wheel: native artifact must contain exactly one pyowl_core/_native extension"
            )
        joined_tags = " ".join(tags).casefold()
        if not tags or "none-any" in joined_tags or "abi3" in joined_tags:
            errors.append(f"wheel: native artifact has unsupported tags {tags!r}")
        filename_parts = filename.removesuffix(".whl").rsplit("-", 3)
        filename_tag = "-".join(filename_parts[-3:]) if len(filename_parts) == 4 else None
        if filename_tag is None or tags != [filename_tag]:
            errors.append(
                f"wheel: native WHEEL tags {tags!r} do not match filename tag {filename_tag!r}"
            )
        if any(re.search(r"cp\d+t(?:-|_)", tag.casefold()) for tag in tags):
            errors.append("wheel: free-threaded native tag is not approved")
    for name in reader.names():
        mode = reader.mode(name)
        if not mode or name.endswith("/"):
            continue
        executable = bool(mode & 0o111)
        is_native = name.casefold().endswith(_NATIVE_SUFFIXES)
        if executable and not is_native:
            errors.append(f"wheel: unexpected executable bit {name}")
        if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
            errors.append(f"wheel: non-regular member {name}")
    return errors


def _validate_sdist(
    reader: _SdistReader,
    filename: str,
    expected_version: str,
) -> list[str]:
    errors: list[str] = []
    names = reader.names()
    if reader.unsafe_links():
        errors.append(f"sdist: links are forbidden: {', '.join(reader.unsafe_links())}")
    distribution = re.sub(r"[-_.]+", "_", "pyowl-core")
    expected_root = f"{distribution}-{expected_version}"
    roots = {
        PurePosixPath(name.replace("\\", "/")).parts[0]
        for name in names
        if PurePosixPath(name.replace("\\", "/")).parts
    }
    if roots != {expected_root}:
        errors.append("sdist: archive root does not exactly match project identity")
    required_sources = {
        "PKG-INFO",
        "native/Cargo.lock",
        "native/Cargo.toml",
        "native/src/lib.rs",
        "pyproject.toml",
        "setup.py",
        "src/pyowl_core/__init__.py",
    }
    for relative_path in sorted(required_sources):
        if f"{expected_root}/{relative_path}" not in names:
            errors.append(f"sdist: missing required source {relative_path}")
    if filename != f"{expected_root}.tar.gz":
        errors.append("sdist: filename does not match project name/version")
    binaries = [name for name in names if name.casefold().endswith(_NATIVE_SUFFIXES)]
    if binaries:
        errors.append(f"sdist: platform binary is forbidden: {', '.join(binaries)}")
    return errors


def inspect_artifact(
    path: Path,
    *,
    expected_version: str = "0.1.0.dev0",
    expected_variant: ArtifactVariant | None = None,
    require_project_urls: bool = False,
) -> InspectionResult:
    """Inspect one archive and return all deterministic findings."""

    path = path.resolve()
    kind = _artifact_kind(path)
    reader: _WheelReader | _SdistReader
    reader = _WheelReader(path) if kind == "wheel" else _SdistReader(path)
    try:
        variant: ArtifactVariant = _wheel_variant(reader) if kind == "wheel" else "sdist"
        errors, total, resource_safe = _validate_common(reader)
        if expected_variant is not None and variant != expected_variant:
            errors.append(f"artifact: detected variant {variant!r}, expected {expected_variant!r}")
        if not resource_safe:
            return InspectionResult(
                path=str(path),
                kind=kind,
                variant=variant,
                member_count=len(reader.names()),
                uncompressed_bytes=total,
                metadata={},
                errors=tuple(sorted(set(errors))),
                release_blockers=(),
                deferred_platform_checks=(),
            )
        metadata, metadata_error = _metadata(reader, kind)
        if metadata_error is not None:
            errors.append(metadata_error)
            blockers: list[str] = []
        else:
            metadata_errors, blockers = _validate_metadata(
                metadata, expected_version, require_project_urls
            )
            errors.extend(metadata_errors)
        errors.extend(_validate_licenses(reader.names(), kind))
        deferred: list[str] = []
        if kind == "wheel":
            errors.extend(_validate_wheel(reader, variant, path.name, expected_version))
            if variant == "native":
                deferred.append(
                    "native dynamic dependencies/rpaths/symbols require the "
                    "target-platform audit job"
                )
        else:
            assert isinstance(reader, _SdistReader)
            errors.extend(_validate_sdist(reader, path.name, expected_version))
        return InspectionResult(
            path=str(path),
            kind=kind,
            variant=variant,
            member_count=len(reader.names()),
            uncompressed_bytes=total,
            metadata=metadata,
            errors=tuple(sorted(set(errors))),
            release_blockers=tuple(sorted(set(blockers))),
            deferred_platform_checks=tuple(sorted(set(deferred))),
            non_native_payload_sha256=(
                _non_native_payload_sha256(reader) if kind == "wheel" and not errors else None
            ),
        )
    finally:
        reader.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--expected-version", default="0.1.0.dev0")
    parser.add_argument("--variant", choices=("pure", "native", "sdist"))
    parser.add_argument("--release", action="store_true", help="require approved project URLs")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    reports: list[dict[str, object]] = []
    status = 0
    for artifact in args.artifacts:
        result = inspect_artifact(
            artifact,
            expected_version=args.expected_version,
            expected_variant=args.variant,
            require_project_urls=args.release,
        )
        reports.append(result.to_dict())
        # ``--release`` promotes missing project URLs to structural errors.
        # Target-platform binary checks remain separately evidenced by the
        # checksum-bound platform gate and cannot be repeated on this host.
        if not result.ok:
            status = 1
    rendered = json.dumps(reports, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
