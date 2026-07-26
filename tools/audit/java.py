"""Detect Java artifacts and runtime/build dependency drift."""

from __future__ import annotations

import os
import re
import tarfile
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from .common import run_cli

_ARTIFACT_SUFFIXES = {".class", ".ear", ".jar", ".jmod", ".war"}
_ARCHIVE_SUFFIXES = {".bz2", ".gz", ".tar", ".tgz", ".whl", ".xz", ".zip"}
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_SCANNED_TEXT_BYTES = 4 * 1024**2
_SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "target",
    "venv",
}
_SKIP_PREFIXES = (("benchmarks", "comparators", "runners", "owlapi", "runtime"),)
_TEXT_NAMES = {
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
_DEPENDENCY_PATTERN = re.compile(
    r"(?im)(?:^|[=<'\"\s,])(?:jpype1?|owlapi|robot|deeponto|mowl)(?:$|[>='\"\s,;])"
)
_SOURCE_PATTERNS = (
    re.compile(r"(?m)^\s*(?:from|import)\s+(?:jpype|deeponto|mowl)(?:\.|\s|$)"),
    re.compile(r"(?i)\b(?:subprocess\.(?:run|call|popen)|Popen)\s*\([^\n]*['\"]java['\"]"),
    re.compile(r"(?i)\b(?:owlapi|org\.semanticweb\.owlapi)\b"),
)


def _is_skipped(parts: tuple[str, ...]) -> bool:
    return any(part in _SKIP_PARTS for part in parts) or any(
        parts[: len(prefix)] == prefix for prefix in _SKIP_PREFIXES
    )


def _files(root: Path) -> Iterable[Path]:
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        selected = Path(directory)
        try:
            relative_parts = selected.relative_to(root).parts
        except ValueError:
            dirnames.clear()
            continue
        dirnames[:] = sorted(name for name in dirnames if not _is_skipped((*relative_parts, name)))
        for name in sorted(filenames):
            path = selected / name
            if path.is_file():
                yield path


def _scan_archive_text(label: str, name: str, payload: bytes) -> list[str]:
    if len(payload) > _MAX_SCANNED_TEXT_BYTES:
        return []
    member_path = PurePosixPath(name)
    should_scan = member_path.name.lower() in _TEXT_NAMES or member_path.suffix.lower() in {
        ".py",
        ".toml",
        ".cfg",
        ".ini",
        ".lock",
    }
    if not should_scan:
        return []
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        return []
    violations: list[str] = []
    if member_path.name.lower() in _TEXT_NAMES and _DEPENDENCY_PATTERN.search(text):
        violations.append(f"java: forbidden dependency in archive: {label}!{name}")
    normalized = "/" + member_path.as_posix().lstrip("/")
    if "/src/pyowl_core/" in normalized and any(
        pattern.search(text) for pattern in _SOURCE_PATTERNS
    ):
        violations.append(f"java: forbidden runtime integration in archive: {label}!{name}")
    return violations


def _archive_violations(path: Path, label: str) -> list[str]:
    if path.suffix.lower() not in _ARCHIVE_SUFFIXES:
        return []
    violations: list[str] = []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                zip_members = archive.infolist()
                if len(zip_members) > _MAX_ARCHIVE_MEMBERS:
                    return [f"java: archive member limit exceeded: {label}"]
                for zip_member in zip_members:
                    if PurePosixPath(zip_member.filename).suffix.lower() in _ARTIFACT_SUFFIXES:
                        violations.append(
                            f"java: forbidden archive member: {label}!{zip_member.filename}"
                        )
                    if not zip_member.is_dir() and zip_member.file_size <= _MAX_SCANNED_TEXT_BYTES:
                        violations.extend(
                            _scan_archive_text(
                                label,
                                zip_member.filename,
                                archive.read(zip_member),
                            )
                        )
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                tar_members = archive.getmembers()
                if len(tar_members) > _MAX_ARCHIVE_MEMBERS:
                    return [f"java: archive member limit exceeded: {label}"]
                for tar_member in tar_members:
                    if PurePosixPath(tar_member.name).suffix.lower() in _ARTIFACT_SUFFIXES:
                        violations.append(
                            f"java: forbidden archive member: {label}!{tar_member.name}"
                        )
                    if tar_member.isfile() and tar_member.size <= _MAX_SCANNED_TEXT_BYTES:
                        extracted = archive.extractfile(tar_member)
                        if extracted is not None:
                            violations.extend(
                                _scan_archive_text(label, tar_member.name, extracted.read())
                            )
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return []
    return violations


def audit_java(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(_files(root)):
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() in _ARTIFACT_SUFFIXES:
            violations.append(f"java: forbidden artifact: {relative}")
            continue
        violations.extend(_archive_violations(path, relative))
        should_scan = path.name.lower() in _TEXT_NAMES or path.suffix.lower() in {
            ".py",
            ".toml",
            ".cfg",
            ".ini",
            ".lock",
        }
        if not should_scan or path.is_relative_to(root / "tools" / "audit"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if path.name.lower() in _TEXT_NAMES and _DEPENDENCY_PATTERN.search(text):
            violations.append(f"java: forbidden dependency declaration: {relative}")
        if path.is_relative_to(root / "src"):
            for pattern in _SOURCE_PATTERNS:
                if pattern.search(text):
                    violations.append(f"java: forbidden runtime integration: {relative}")
                    break
    return sorted(set(violations))


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(audit_java, argv)


if __name__ == "__main__":
    raise SystemExit(main())
