"""Python-3.10-compatible checker for the frozen wire-v1 schema ledger."""

from __future__ import annotations

import ast
from pathlib import Path

from pyowl_core.wire.schema import (
    CANONICAL_PROFILE,
    DIRECTORY_ENTRY_SIZE,
    HEADER_SIZE,
    MODEL_SCHEMA,
    REQUIRED_SECTIONS,
    SECTION_SCHEMAS,
    WIRE_MAJOR,
    WIRE_MINOR,
    SectionKind,
)


def check_schema(path: Path) -> tuple[str, ...]:
    root: dict[str, object] = {}
    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for original in path.read_text(encoding="utf-8").splitlines():
        line = original.split("#", 1)[0].strip()
        if not line or (line.startswith("[") and line != "[[sections]]"):
            current = None if line.startswith("[") else current
            continue
        if line == "[[sections]]":
            current = {}
            sections.append(current)
            continue
        if "=" not in line:
            continue
        key, encoded = (item.strip() for item in line.split("=", 1))
        if encoded in ("true", "false"):
            parsed_value = encoded == "true"
        else:
            try:
                parsed_value = ast.literal_eval(encoded)
            except (SyntaxError, ValueError):
                continue
        (root if current is None else current)[key] = parsed_value
    errors: list[str] = []
    expected_root = {
        "wire_major": WIRE_MAJOR,
        "wire_minor": WIRE_MINOR,
        "model_schema": MODEL_SCHEMA,
        "canonical_profile": CANONICAL_PROFILE,
        "header_bytes": HEADER_SIZE,
        "directory_entry_bytes": DIRECTORY_ENTRY_SIZE,
    }
    for key, expected in expected_root.items():
        if root.get(key) != expected:
            errors.append(f"line ledger mismatch for {key}: expected {expected!r}")
    observed: dict[int, tuple[str, bool, int]] = {}
    for section in sections:
        try:
            kind_value = section["kind"]
            name_value = section["name"]
            required_value = section["required"]
            schema_value = section["schema"]
            if (
                isinstance(kind_value, bool)
                or not isinstance(kind_value, int)
                or not isinstance(name_value, str)
                or not isinstance(required_value, bool)
                or isinstance(schema_value, bool)
                or not isinstance(schema_value, int)
            ):
                raise TypeError
            kind = kind_value
            observed[kind] = (
                name_value,
                required_value,
                schema_value,
            )
        except (KeyError, TypeError, ValueError):
            errors.append("malformed [[sections]] entry")
    expected_kinds = {int(item) for item in SectionKind}
    if set(observed) != expected_kinds:
        errors.append("section kind set differs from generated schema.py")
    for kind in SectionKind:
        value = observed.get(int(kind))
        expected_section = (kind.name, kind in REQUIRED_SECTIONS, SECTION_SCHEMAS[kind])
        if value is not None and value != expected_section:
            errors.append(
                f"section {kind.name} differs: {value!r} != {expected_section!r}"
            )
    return tuple(errors)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    errors = check_schema(root / "schemas" / "wire-v1.toml")
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["check_schema", "main"]
