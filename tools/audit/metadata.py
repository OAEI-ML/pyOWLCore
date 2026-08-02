"""Dependency-free checks for frozen package identity and release placeholders."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

from .common import run_cli


def _assignment(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"missing assignment {name}")


def audit_metadata(root: Path) -> list[str]:
    pyproject = root / "pyproject.toml"
    init = root / "src" / "pyowl_core" / "__init__.py"
    violations: list[str] = []
    if not pyproject.is_file() or not init.is_file():
        return ["metadata: missing pyproject.toml or package __init__.py"]
    text = pyproject.read_text(encoding="utf-8")
    expectations = {
        r'(?m)^name\s*=\s*"pyowl-core"\s*$': "distribution name",
        r'(?m)^version\s*=\s*"0\.2\.0"\s*$': "version",
        r'(?m)^requires-python\s*=\s*">=3\.10"\s*$': "Python requirement",
        r'(?m)^license\s*=\s*"Apache-2\.0"\s*$': "SPDX license",
        r'(?m)^license-files\s*=\s*\["LICENSE", "NOTICE", "THIRD_PARTY_LICENSES/\*"\]\s*$': (
            "license files"
        ),
        r"(?ms)^dependencies\s*=\s*\[\s*\]": "empty runtime dependencies",
        (
            r'(?m)^Homepage\s*=\s*"https://github\.com/OAEI-ML/pyOWLCore"\s*$'
        ): "homepage URL",
        (
            r'(?m)^Repository\s*=\s*"https://github\.com/OAEI-ML/pyOWLCore"\s*$'
        ): "repository URL",
        (
            r'(?m)^Documentation\s*=\s*"https://github\.com/OAEI-ML/'
            r'pyOWLCore/tree/main/docs"\s*$'
        ): "documentation URL",
        (
            r'(?m)^Issues\s*=\s*"https://github\.com/OAEI-ML/pyOWLCore/issues"\s*$'
        ): "issues URL",
    }
    for pattern, label in expectations.items():
        if not re.search(pattern, text):
            violations.append(f"metadata: incorrect {label}")
    if "OWNER" in text:
        violations.append("metadata: unresolved OWNER placeholder")
    marker = root / "src" / "pyowl_core" / "py.typed"
    if not marker.is_file():
        violations.append("metadata: missing py.typed")
    expected_values = {
        "__version__": "0.2.0",
        "API_VERSION": (0, 2),
        "MODEL_SCHEMA_VERSION": 2,
        "WIRE_FORMAT_VERSION": (1, 2),
        "ADAPTER_PROTOCOL_VERSION": 1,
    }
    for name, expected in expected_values.items():
        try:
            actual = _assignment(init, name)
        except (OSError, SyntaxError, ValueError) as error:
            violations.append(f"metadata: cannot read {name}: {error}")
        else:
            if actual != expected:
                violations.append(f"metadata: {name} is {actual!r}, expected {expected!r}")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(audit_metadata, argv)


if __name__ == "__main__":
    raise SystemExit(main())
