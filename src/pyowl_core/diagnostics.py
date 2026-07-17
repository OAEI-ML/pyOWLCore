"""Stable, immutable diagnostics shared by all future pyowl-core phases."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TypeAlias, cast

from ._immutable import FrozenMap, freeze_mapping

DiagnosticScalar: TypeAlias = str | int | bool
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def validate_diagnostic_code(code: str) -> str:
    if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
        raise ValueError(f"diagnostic codes must match ^[A-Z][A-Z0-9_]*$; received {code!r}")
    return code


def _optional_nonnegative(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be a nonnegative integer or None")


def _optional_positive(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A half-open source location with optional byte and text coordinates."""

    byte_start: int | None = None
    byte_end: int | None = None
    line_start: int | None = None
    column_start: int | None = None
    line_end: int | None = None
    column_end: int | None = None

    def __post_init__(self) -> None:
        _optional_nonnegative("byte_start", self.byte_start)
        _optional_nonnegative("byte_end", self.byte_end)
        _optional_positive("line_start", self.line_start)
        _optional_positive("column_start", self.column_start)
        _optional_positive("line_end", self.line_end)
        _optional_positive("column_end", self.column_end)
        if (
            self.byte_start is not None
            and self.byte_end is not None
            and self.byte_end < self.byte_start
        ):
            raise ValueError("byte_end must not precede byte_start")
        if self.line_start is not None and self.line_end is not None:
            start_column = self.column_start or 1
            end_column = self.column_end or 1
            if (self.line_end, end_column) < (self.line_start, start_column):
                raise ValueError("text end must not precede text start")

    def to_dict(self) -> dict[str, int | None]:
        return {
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "line_start": self.line_start,
            "column_start": self.column_start,
            "line_end": self.line_end,
            "column_end": self.column_end,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SourceSpan:
        allowed = {
            "byte_start",
            "byte_end",
            "line_start",
            "column_start",
            "line_end",
            "column_end",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown SourceSpan fields: {sorted(unknown)!r}")
        kwargs: dict[str, int | None] = {}
        for name in allowed:
            raw = value.get(name)
            if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int)):
                raise TypeError(f"SourceSpan.{name} must be an integer or None")
            kwargs[name] = raw
        return cls(**kwargs)


def _reference_text(value: object) -> str:
    iri_value = getattr(value, "value", None)
    if isinstance(iri_value, str):
        return iri_value
    return str(value)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    document_iri: object | None = None
    source_span: SourceSpan | None = None
    import_chain: tuple[object, ...] = ()
    details: Mapping[str, DiagnosticScalar] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        validate_diagnostic_code(self.code)
        if not isinstance(self.severity, Severity):
            raise TypeError("severity must be a Severity value")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be a nonempty string")
        if self.source_span is not None and not isinstance(self.source_span, SourceSpan):
            raise TypeError("source_span must be SourceSpan or None")
        chain = tuple(self.import_chain)
        clean_details: dict[str, DiagnosticScalar] = {}
        for key, value in self.details.items():
            if not isinstance(key, str) or not key:
                raise TypeError("diagnostic detail keys must be nonempty strings")
            if not isinstance(value, (str, int, bool)):
                raise TypeError("diagnostic detail values must be str, int, or bool")
            clean_details[key] = value
        object.__setattr__(self, "import_chain", chain)
        object.__setattr__(self, "details", freeze_mapping(clean_details))

    def with_details(self, **details: DiagnosticScalar) -> Diagnostic:
        merged = dict(self.details)
        merged.update(details)
        return replace(self, details=merged)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "document_iri": (
                None if self.document_iri is None else _reference_text(self.document_iri)
            ),
            "source_span": None if self.source_span is None else self.source_span.to_dict(),
            "import_chain": [_reference_text(item) for item in self.import_chain],
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Diagnostic:
        required = {"code", "severity", "message"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"missing Diagnostic fields: {sorted(missing)!r}")
        unknown = set(value) - {
            "code",
            "severity",
            "message",
            "document_iri",
            "source_span",
            "import_chain",
            "details",
        }
        if unknown:
            raise ValueError(f"unknown Diagnostic fields: {sorted(unknown)!r}")
        code = value["code"]
        severity = value["severity"]
        message = value["message"]
        if (
            not isinstance(code, str)
            or not isinstance(severity, str)
            or not isinstance(message, str)
        ):
            raise TypeError("Diagnostic code, severity, and message must be strings")
        raw_span = value.get("source_span")
        if raw_span is not None and not isinstance(raw_span, Mapping):
            raise TypeError("Diagnostic source_span must be a mapping or None")
        raw_chain = value.get("import_chain", ())
        if not isinstance(raw_chain, (list, tuple)) or not all(
            isinstance(item, str) for item in raw_chain
        ):
            raise TypeError("Diagnostic import_chain must contain strings")
        raw_details = value.get("details", {})
        if not isinstance(raw_details, Mapping):
            raise TypeError("Diagnostic details must be a mapping")
        document_iri = value.get("document_iri")
        if document_iri is not None and not isinstance(document_iri, str):
            raise TypeError("Diagnostic document_iri must be a string or None")
        return cls(
            code=code,
            severity=Severity(severity),
            message=message,
            document_iri=document_iri,
            source_span=(
                None
                if raw_span is None
                else SourceSpan.from_dict(cast(Mapping[str, object], raw_span))
            ),
            import_chain=tuple(cast(list[str] | tuple[str, ...], raw_chain)),
            details=cast(Mapping[str, DiagnosticScalar], raw_details),
        )


__all__ = [
    "Diagnostic",
    "DiagnosticScalar",
    "Severity",
    "SourceSpan",
    "validate_diagnostic_code",
]
