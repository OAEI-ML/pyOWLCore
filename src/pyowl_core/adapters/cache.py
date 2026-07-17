"""Versioned, path-free consumer compiler cache identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.document.document import Fingerprint
from pyowl_core.document.snapshot import OntologyView
from pyowl_core.exceptions import AdapterCompatibilityError

from .compatibility import CoreContract


class CacheScope(str, Enum):
    """Select the core fingerprint required by one consumer compiler."""

    LOGICAL = "logical"
    STRUCTURAL = "structural"


def _nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _integer_pair(name: str, value: object) -> tuple[int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not all(type(item) is int and item >= 0 for item in value)
    ):
        raise TypeError(f"{name} must be a pair of nonnegative integers")
    return value


@dataclass(frozen=True, slots=True)
class ConsumerCacheKey:
    """All semantic inputs needed to reuse a consumer-private compiled IR.

    The key deliberately accepts no source path, mtime, Python hash, object ID,
    serialization spelling, or backend-private pointer.
    """

    consumer: str
    consumer_version: str
    consumer_api: str
    compiler_schema: str
    compatibility_id: str
    core_package_version: str
    core_api_version: tuple[int, int]
    core_model_schema: int
    core_wire_format: tuple[int, int]
    core_adapter_protocol: int
    scope: CacheScope
    primary_fingerprint: Fingerprint
    signature_fingerprint: Fingerprint
    semantic_options_sha256: bytes

    def __post_init__(self) -> None:
        for name in (
            "consumer",
            "consumer_version",
            "consumer_api",
            "compiler_schema",
            "compatibility_id",
            "core_package_version",
        ):
            _nonempty(name, getattr(self, name))
        object.__setattr__(
            self,
            "core_api_version",
            _integer_pair("core_api_version", self.core_api_version),
        )
        object.__setattr__(
            self,
            "core_wire_format",
            _integer_pair("core_wire_format", self.core_wire_format),
        )
        for name in ("core_model_schema", "core_adapter_protocol"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.scope, CacheScope):
            raise TypeError("scope must be CacheScope")
        for name in ("primary_fingerprint", "signature_fingerprint"):
            fingerprint = getattr(self, name)
            if not isinstance(fingerprint, Fingerprint):
                raise TypeError(f"{name} must be Fingerprint")
            if fingerprint.schema != self.core_model_schema:
                raise ValueError(f"{name} schema must match core_model_schema")
        if (
            not isinstance(self.semantic_options_sha256, bytes)
            or len(self.semantic_options_sha256) != 32
        ):
            raise ValueError("semantic_options_sha256 must be exactly 32 bytes")

    @classmethod
    def for_view(
        cls,
        view: OntologyView,
        *,
        consumer: str,
        consumer_version: str,
        consumer_api: str,
        compiler_schema: str,
        compatibility_id: str,
        scope: CacheScope,
        semantic_options_sha256: bytes,
        core: CoreContract | None = None,
    ) -> ConsumerCacheKey:
        """Construct a key from public versions and canonical fingerprints."""

        selected_core = CoreContract.current() if core is None else core
        if not isinstance(selected_core, CoreContract):
            raise TypeError("core must be CoreContract or None")
        if not isinstance(scope, CacheScope):
            raise TypeError("scope must be CacheScope")
        primary = (
            view.logical_fingerprint if scope is CacheScope.LOGICAL else view.structural_fingerprint
        )
        return cls(
            consumer=consumer,
            consumer_version=consumer_version,
            consumer_api=consumer_api,
            compiler_schema=compiler_schema,
            compatibility_id=compatibility_id,
            core_package_version=selected_core.package_version,
            core_api_version=selected_core.api_version,
            core_model_schema=selected_core.model_schema,
            core_wire_format=selected_core.wire_format,
            core_adapter_protocol=selected_core.adapter_protocol,
            scope=scope,
            primary_fingerprint=primary,
            signature_fingerprint=view.signature_fingerprint,
            semantic_options_sha256=semantic_options_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "consumer": self.consumer,
            "consumer_version": self.consumer_version,
            "consumer_api": self.consumer_api,
            "compiler_schema": self.compiler_schema,
            "compatibility_id": self.compatibility_id,
            "core": {
                "package_version": self.core_package_version,
                "api_version": list(self.core_api_version),
                "model_schema": self.core_model_schema,
                "wire_format": list(self.core_wire_format),
                "adapter_protocol": self.core_adapter_protocol,
            },
            "scope": self.scope.value,
            "primary_fingerprint": _fingerprint_dict(self.primary_fingerprint),
            "signature_fingerprint": _fingerprint_dict(self.signature_fingerprint),
            "semantic_options_sha256": self.semantic_options_sha256.hex(),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(b"pyowl-core/consumer-cache-key/v1\0" + self.canonical_bytes).digest()

    @property
    def hex(self) -> str:
        return self.digest.hex()


@dataclass(frozen=True, slots=True, order=True)
class CacheKeyIssue:
    code: str
    field: str
    expected: str
    actual: str

    def __post_init__(self) -> None:
        for name in ("code", "field", "expected", "actual"):
            _nonempty(name, getattr(self, name))

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True, slots=True)
class CacheKeyReport:
    """Exhaustive comparison of a retained key to current expected identity."""

    actual: ConsumerCacheKey
    expected: ConsumerCacheKey
    issues: tuple[CacheKeyIssue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.actual, ConsumerCacheKey):
            raise TypeError("actual must be ConsumerCacheKey")
        if not isinstance(self.expected, ConsumerCacheKey):
            raise TypeError("expected must be ConsumerCacheKey")
        issues = tuple(sorted(self.issues))
        if not all(isinstance(item, CacheKeyIssue) for item in issues):
            raise TypeError("issues must contain CacheKeyIssue values")
        object.__setattr__(self, "issues", issues)

    @property
    def compatible(self) -> bool:
        return not self.issues

    def raise_for_errors(self) -> None:
        if self.compatible:
            return
        encoded = json.dumps(
            [issue.to_dict() for issue in self.issues],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        message = f"consumer cache key is incompatible: {len(self.issues)} issue(s)"
        diagnostic = Diagnostic(
            code="ADAPTER_CACHE_KEY_MISMATCH",
            severity=Severity.ERROR,
            message=message,
            details={
                "actual_sha256": self.actual.hex,
                "expected_sha256": self.expected.hex,
                "issue_count": len(self.issues),
                "issues": encoded,
            },
        )
        raise AdapterCompatibilityError(message, diagnostic=diagnostic)


def compare_cache_keys(actual: ConsumerCacheKey, expected: ConsumerCacheKey) -> CacheKeyReport:
    """Compare every cache-key field instead of failing at the first mismatch."""

    if not isinstance(actual, ConsumerCacheKey) or not isinstance(expected, ConsumerCacheKey):
        raise TypeError("actual and expected must be ConsumerCacheKey")
    issues: list[CacheKeyIssue] = []
    fields = (
        "consumer",
        "consumer_version",
        "consumer_api",
        "compiler_schema",
        "compatibility_id",
        "core_package_version",
        "core_api_version",
        "core_model_schema",
        "core_wire_format",
        "core_adapter_protocol",
        "scope",
        "primary_fingerprint",
        "signature_fingerprint",
        "semantic_options_sha256",
    )
    for field in fields:
        actual_value = getattr(actual, field)
        expected_value = getattr(expected, field)
        if actual_value != expected_value:
            issues.append(
                CacheKeyIssue(
                    "CACHE_KEY_FIELD_MISMATCH",
                    field,
                    _display(expected_value),
                    _display(actual_value),
                )
            )
    return CacheKeyReport(actual, expected, tuple(issues))


def _display(value: object) -> str:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Fingerprint):
        return f"{value.algorithm}:{value.schema}:{value.hex}"
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _fingerprint_dict(value: Fingerprint) -> dict[str, object]:
    return {"algorithm": value.algorithm, "schema": value.schema, "digest": value.hex}


__all__ = [
    "CacheKeyIssue",
    "CacheKeyReport",
    "CacheScope",
    "ConsumerCacheKey",
    "compare_cache_keys",
]
