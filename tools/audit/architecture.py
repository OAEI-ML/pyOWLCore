"""Enforce core layering and the no-reverse-dependency boundary."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from .common import run_cli

_CONSUMERS = (
    "exact_om",
    "exactom",
    "pyelk",
    "pyhermit",
    "pyowl2vec",
    "oaei_bio",
    "deeponto",
    "mowl",
)
_MODEL_FORBIDDEN = (
    "pyowl_core.api",
    "pyowl_core.backends",
    "pyowl_core.document",
    "pyowl_core.index",
    "pyowl_core.io",
    "pyowl_core.wire",
    "rdflib",
    "owlready2",
)


def _module_name(source: Path, path: Path) -> str:
    relative = path.relative_to(source).with_suffix("")
    parts = ["pyowl_core", *relative.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import_from(path: Path, module_name: str, node: ast.ImportFrom) -> list[str]:
    if node.level == 0:
        base = node.module or ""
    else:
        package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
        parts = package.split(".")
        keep = len(parts) - (node.level - 1)
        if keep < 1:
            return ["." * node.level + (node.module or "")]
        base = ".".join(parts[:keep])
        if node.module:
            base = f"{base}.{node.module}"
    imports = [base]
    imports.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return imports


def _imports(source: Path, path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        return [(0, f"<unreadable: {error}>")]
    found: list[tuple[int, str]] = []
    module_name = _module_name(source, path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                (node.lineno, imported)
                for imported in _resolve_import_from(path, module_name, node)
            )
    return found


def audit_architecture(root: Path) -> list[str]:
    source = root / "src" / "pyowl_core"
    if not source.is_dir():
        return ["architecture: missing src/pyowl_core"]
    violations: list[str] = []
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        in_model = "model" in path.relative_to(source).parts[:-1]
        in_python_backend = path.is_relative_to(source / "backends" / "python")
        for line, imported in _imports(source, path):
            normalized = imported.lower().replace("-", "_")
            if imported.startswith("<unreadable"):
                violations.append(f"architecture: {relative}:{line}: {imported}")
                continue
            if any(
                normalized == consumer or normalized.startswith(consumer + ".")
                for consumer in _CONSUMERS
            ):
                violations.append(
                    f"architecture: {relative}:{line}: reverse dependency {imported!r}"
                )
            if in_model and any(
                imported == prefix or imported.startswith(prefix + ".")
                for prefix in _MODEL_FORBIDDEN
            ):
                violations.append(
                    f"architecture: {relative}:{line}: model layer imports {imported!r}"
                )
            if in_python_backend and (
                imported == "pyowl_core._native"
                or imported.startswith("pyowl_core.backends.native")
            ):
                violations.append(
                    f"architecture: {relative}:{line}: Python backend imports native backend"
                )
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(audit_architecture, argv)


if __name__ == "__main__":
    raise SystemExit(main())
