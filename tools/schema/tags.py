"""Small dependency-free tag ledger validator and code generator.

The deliberately restricted TOML subset works on Python 3.10 without adding a
runtime or tool bootstrap dependency. Schema ledgers use only scalar keys and
``[[tag]]`` tables; accepting more syntax would make accidental ambiguity more
likely, not more useful.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]*$")
_STATUSES = {"active", "retired"}
_MAX_TAG = 2**32 - 1


class SchemaError(ValueError):
    """A stable schema-ledger validation failure."""


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    value: int
    status: str = "active"

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise SchemaError(f"invalid tag name: {self.name!r}")
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise SchemaError(f"tag {self.name} value must be an integer")
        if not 1 <= self.value <= _MAX_TAG:
            raise SchemaError(f"tag {self.name} value must be in 1..{_MAX_TAG}")
        if self.status not in _STATUSES:
            raise SchemaError(f"tag {self.name} has invalid status {self.status!r}")


@dataclass(frozen=True, slots=True)
class TagLedger:
    namespace: str
    tags: tuple[Tag, ...]
    schema: int = 1

    def __post_init__(self) -> None:
        if self.schema != 1:
            raise SchemaError(f"unsupported tag-ledger schema: {self.schema!r}")
        if not _NAMESPACE.fullmatch(self.namespace):
            raise SchemaError(f"invalid namespace: {self.namespace!r}")
        names: set[str] = set()
        values: set[int] = set()
        for tag in self.tags:
            if tag.name in names:
                raise SchemaError(f"duplicate tag name: {tag.name}")
            if tag.value in values:
                raise SchemaError(f"duplicate tag value: {tag.value}")
            names.add(tag.name)
            values.add(tag.value)

    @classmethod
    def parse(cls, text: str) -> TagLedger:
        root: dict[str, object] = {}
        raw_tags: list[dict[str, object]] = []
        current: dict[str, object] | None = None
        for number, original in enumerate(text.splitlines(), 1):
            line = _strip_comment(original).strip()
            if not line:
                continue
            if line == "[[tag]]":
                current = {}
                raw_tags.append(current)
                continue
            if "=" not in line:
                raise SchemaError(f"line {number}: expected key = value")
            key, raw_value = (part.strip() for part in line.split("=", 1))
            if not re.fullmatch(r"[a-z_]+", key):
                raise SchemaError(f"line {number}: invalid key {key!r}")
            target = root if current is None else current
            if key in target:
                raise SchemaError(f"line {number}: duplicate key {key!r}")
            target[key] = _parse_scalar(raw_value, number)
        unknown_root = set(root) - {"schema", "namespace"}
        if unknown_root:
            raise SchemaError(f"unknown ledger keys: {sorted(unknown_root)!r}")
        if set(root) != {"schema", "namespace"}:
            raise SchemaError("ledger requires schema and namespace")
        tags: list[Tag] = []
        for index, raw_tag in enumerate(raw_tags, 1):
            unknown_tag = set(raw_tag) - {"name", "value", "status"}
            if unknown_tag:
                raise SchemaError(f"tag {index} has unknown keys: {sorted(unknown_tag)!r}")
            if not {"name", "value"}.issubset(raw_tag):
                raise SchemaError(f"tag {index} requires name and value")
            try:
                tags.append(
                    Tag(
                        name=_expect_type(raw_tag["name"], str, "tag name"),
                        value=_expect_int(raw_tag["value"], "tag value"),
                        status=_expect_type(raw_tag.get("status", "active"), str, "tag status"),
                    )
                )
            except (TypeError, ValueError) as error:
                if isinstance(error, SchemaError):
                    raise
                raise SchemaError(f"tag {index}: {error}") from error
        return cls(
            schema=_expect_int(root["schema"], "schema"),
            namespace=_expect_type(root["namespace"], str, "namespace"),
            tags=tuple(tags),
        )

    @classmethod
    def load(cls, path: Path) -> TagLedger:
        return cls.parse(path.read_text(encoding="utf-8"))

    def render(self) -> str:
        lines = [f"schema = {self.schema}", f"namespace = {self.namespace!r}"]
        for tag in sorted(self.tags, key=lambda item: (item.value, item.name)):
            lines.extend(
                [
                    "",
                    "[[tag]]",
                    f"name = {tag.name!r}",
                    f"value = {tag.value}",
                    f"status = {tag.status!r}",
                ]
            )
        return "\n".join(lines) + "\n"

    def render_python(self) -> str:
        lines = [
            '"""Generated tag constants; do not edit by hand."""',
            "",
            f"SCHEMA_NAMESPACE = {json.dumps(self.namespace)}",
            f"SCHEMA_VERSION = {self.schema}",
            "",
        ]
        exported = ["SCHEMA_NAMESPACE", "SCHEMA_VERSION"]
        for tag in sorted(self.tags, key=lambda item: (item.value, item.name)):
            if tag.status == "active":
                lines.append(f"{tag.name} = {tag.value}")
                exported.append(tag.name)
            else:
                lines.append(f"# retired: {tag.name} = {tag.value}")
        lines.extend(["", "__all__ = ["])
        lines.extend(f"    {json.dumps(name)}," for name in sorted(exported))
        lines.extend(["]", ""])
        return "\n".join(lines)


def _strip_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote is not None:
            escaped = True
            continue
        if character in {"'", '"'}:
            quote = None if quote == character else character if quote is None else quote
        elif character == "#" and quote is None:
            return line[:index]
    return line


def _parse_scalar(value: str, number: int) -> object:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise SchemaError(f"line {number}: invalid scalar value") from error
    if isinstance(parsed, bool) or not isinstance(parsed, (str, int)):
        raise SchemaError(f"line {number}: values must be strings or integers")
    return parsed


def _expect_type(value: object, expected: type[str], label: str) -> str:
    if not isinstance(value, expected):
        raise SchemaError(f"{label} must be a string")
    return value


def _expect_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{label} must be an integer")
    return value


def validate_evolution(previous: TagLedger, current: TagLedger) -> None:
    """Reject removal, reassignment, reuse, or reactivation of reserved tags."""

    if previous.namespace != current.namespace:
        raise SchemaError("schema namespace cannot change")
    old_by_name = {tag.name: tag for tag in previous.tags}
    new_by_name = {tag.name: tag for tag in current.tags}
    old_by_value = {tag.value: tag for tag in previous.tags}
    new_by_value = {tag.value: tag for tag in current.tags}
    for value, old in old_by_value.items():
        new = new_by_value.get(value)
        if new is None:
            raise SchemaError(f"tag value {value} was removed; retain a retired reservation")
        if new.name != old.name:
            raise SchemaError(f"tag value {value} was reused by {new.name}")
    for name, old in old_by_name.items():
        new = new_by_name.get(name)
        if new is None:
            raise SchemaError(f"tag {name} was removed; retire it instead")
        if new.value != old.value:
            raise SchemaError(f"tag {name} was reassigned from {old.value} to {new.value}")
        if old.status == "retired" and new.status != "retired":
            raise SchemaError(f"retired tag {name} cannot be reactivated")


def _write_or_check(path: Path, content: str, check: bool) -> None:
    if check:
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise SchemaError(f"generated file is stale: {path}")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="validate a ledger")
    check_parser.add_argument("ledger", type=Path)
    check_parser.add_argument("--previous", type=Path)
    render_parser = subparsers.add_parser("render", help="print canonical ledger TOML")
    render_parser.add_argument("ledger", type=Path)
    generate_parser = subparsers.add_parser("generate", help="generate Python constants")
    generate_parser.add_argument("ledger", type=Path)
    generate_parser.add_argument("output", type=Path)
    generate_parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        ledger = TagLedger.load(args.ledger)
        if args.command == "check":
            if args.previous is not None:
                validate_evolution(TagLedger.load(args.previous), ledger)
        elif args.command == "render":
            sys.stdout.write(ledger.render())
        else:
            _write_or_check(args.output, ledger.render_python(), args.check)
    except (OSError, SchemaError) as error:
        print(f"schema error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
