"""Exhaustive, side-effect-free consumer capability negotiation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.document.snapshot import CoreCapabilities, OntologyView
from pyowl_core.exceptions import AdapterCompatibilityError

_SEMVER = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)"
    r"(?:(?:a|b|rc)\d+|(?:\.dev|\.post)\d+|-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)


def _integer_pair(name: str, value: object) -> tuple[int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not all(type(item) is int and item >= 0 for item in value)
    ):
        raise TypeError(f"{name} must be a pair of nonnegative integers")
    return value


def _positive_integer(name: str, value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class CoreContract:
    """The package-level dimensions paired with one view's capabilities."""

    package_version: str
    api_version: tuple[int, int]
    adapter_protocol: int
    model_schema: int
    wire_format: tuple[int, int]

    def __post_init__(self) -> None:
        if not isinstance(self.package_version, str) or not self.package_version:
            raise ValueError("package_version must be a nonempty string")
        object.__setattr__(self, "api_version", _integer_pair("api_version", self.api_version))
        object.__setattr__(
            self,
            "wire_format",
            _integer_pair("wire_format", self.wire_format),
        )
        _positive_integer("adapter_protocol", self.adapter_protocol)
        _positive_integer("model_schema", self.model_schema)

    @classmethod
    def current(cls) -> CoreContract:
        """Read the initialized core constants without plugin discovery or I/O."""

        import pyowl_core

        return cls(
            package_version=pyowl_core.__version__,
            api_version=pyowl_core.API_VERSION,
            adapter_protocol=pyowl_core.ADAPTER_PROTOCOL_VERSION,
            model_schema=pyowl_core.MODEL_SCHEMA_VERSION,
            wire_format=pyowl_core.WIRE_FORMAT_VERSION,
        )


@dataclass(frozen=True, slots=True)
class AdapterRequirement:
    """A consumer's complete declaration before private compilation begins."""

    consumer: str
    consumer_version: str
    consumer_api: str
    package_api: tuple[int, int] = (0, 1)
    adapter_protocol: int = 1
    model_schema: int = 1
    wire_major: int = 1
    minimum_wire_minor: int = 0
    required_features: frozenset[str] = frozenset()
    required_encoded_view_schemas: Mapping[str, int] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        for name in ("consumer", "consumer_version", "consumer_api"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty string")
        object.__setattr__(self, "package_api", _integer_pair("package_api", self.package_api))
        _positive_integer("adapter_protocol", self.adapter_protocol)
        _positive_integer("model_schema", self.model_schema)
        if type(self.wire_major) is not int or self.wire_major < 0:
            raise ValueError("wire_major must be a nonnegative integer")
        if type(self.minimum_wire_minor) is not int or self.minimum_wire_minor < 0:
            raise ValueError("minimum_wire_minor must be a nonnegative integer")
        features = frozenset(self.required_features)
        if not all(isinstance(item, str) and item for item in features):
            raise TypeError("required_features must contain nonempty strings")
        schemas: dict[str, int] = {}
        for name, schema in self.required_encoded_view_schemas.items():
            if not isinstance(name, str) or not name:
                raise TypeError("encoded view names must be nonempty strings")
            schemas[name] = _positive_integer("encoded view schema", schema)
        object.__setattr__(self, "required_features", features)
        object.__setattr__(self, "required_encoded_view_schemas", freeze_mapping(schemas))

    @property
    def package_range(self) -> str:
        major, minor = self.package_api
        return f">={major}.{minor},<{major}.{minor + 1}"


@dataclass(frozen=True, slots=True, order=True)
class CompatibilityIssue:
    """One stable incompatibility; reports retain every independently found issue."""

    code: str
    field: str
    expected: str
    actual: str

    def __post_init__(self) -> None:
        for name in ("code", "field", "expected", "actual"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty string")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True, slots=True)
class NegotiationReport:
    """Canonical exhaustive result of matching a requirement to one view."""

    requirement: AdapterRequirement
    core: CoreContract
    capabilities: CoreCapabilities | None
    issues: tuple[CompatibilityIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.requirement, AdapterRequirement):
            raise TypeError("requirement must be AdapterRequirement")
        if not isinstance(self.core, CoreContract):
            raise TypeError("core must be CoreContract")
        if self.capabilities is not None and not isinstance(self.capabilities, CoreCapabilities):
            raise TypeError("capabilities must be CoreCapabilities or None")
        issues = tuple(sorted(self.issues))
        if not all(isinstance(item, CompatibilityIssue) for item in issues):
            raise TypeError("issues must contain CompatibilityIssue values")
        object.__setattr__(self, "issues", issues)

    @property
    def compatible(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, object]:
        capabilities = self.capabilities
        return {
            "consumer": self.requirement.consumer,
            "consumer_version": self.requirement.consumer_version,
            "consumer_api": self.requirement.consumer_api,
            "compatible": self.compatible,
            "core": {
                "package_version": self.core.package_version,
                "api_version": list(self.core.api_version),
                "adapter_protocol": self.core.adapter_protocol,
                "model_schema": self.core.model_schema,
                "wire_format": list(self.core.wire_format),
            },
            "view": None
            if capabilities is None
            else {
                "adapter_protocol": capabilities.adapter_protocol,
                "model_schema": capabilities.model_schema,
                "wire_format": list(capabilities.wire_format),
                "features": sorted(capabilities.features),
                "encoded_view_schemas": dict(capabilities.encoded_view_schemas),
                "backend": capabilities.backend,
            },
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def raise_for_errors(self) -> None:
        if self.compatible:
            return
        encoded = json.dumps(
            [issue.to_dict() for issue in self.issues],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        message = (
            f"pyowl-core is incompatible with {self.requirement.consumer} "
            f"{self.requirement.consumer_version}: {len(self.issues)} issue(s)"
        )
        diagnostic = Diagnostic(
            code="ADAPTER_COMPATIBILITY",
            severity=Severity.ERROR,
            message=message,
            details={
                "consumer": self.requirement.consumer,
                "consumer_api": self.requirement.consumer_api,
                "issue_count": len(self.issues),
                "issues": encoded,
            },
        )
        raise AdapterCompatibilityError(message, diagnostic=diagnostic)


def negotiate_capabilities(
    capabilities: object,
    requirement: AdapterRequirement,
    *,
    core: CoreContract | None = None,
) -> NegotiationReport:
    """Return every package, schema, feature, and encoded-view mismatch."""

    if not isinstance(requirement, AdapterRequirement):
        raise TypeError("requirement must be AdapterRequirement")
    selected_core = CoreContract.current() if core is None else core
    if not isinstance(selected_core, CoreContract):
        raise TypeError("core must be CoreContract or None")
    issues: list[CompatibilityIssue] = []

    match = _SEMVER.fullmatch(selected_core.package_version)
    if match is None:
        issues.append(
            CompatibilityIssue(
                "CORE_PACKAGE_VERSION_INVALID",
                "package_version",
                requirement.package_range,
                selected_core.package_version,
            )
        )
    elif tuple(int(value) for value in match.groups()[:2]) != requirement.package_api:
        issues.append(
            CompatibilityIssue(
                "CORE_PACKAGE_API_MISMATCH",
                "package_version",
                requirement.package_range,
                selected_core.package_version,
            )
        )
    if selected_core.api_version != requirement.package_api:
        issues.append(
            CompatibilityIssue(
                "CORE_API_MISMATCH",
                "API_VERSION",
                str(requirement.package_api),
                str(selected_core.api_version),
            )
        )

    if not isinstance(capabilities, CoreCapabilities):
        issues.append(
            CompatibilityIssue(
                "ADAPTER_CAPABILITIES_TYPE",
                "capabilities",
                "pyowl_core.CoreCapabilities",
                type(capabilities).__name__,
            )
        )
        return NegotiationReport(requirement, selected_core, None, tuple(issues))

    _compare_exact(
        issues,
        "ADAPTER_PROTOCOL_MISMATCH",
        "adapter_protocol",
        requirement.adapter_protocol,
        capabilities.adapter_protocol,
    )
    _compare_exact(
        issues,
        "MODEL_SCHEMA_MISMATCH",
        "model_schema",
        requirement.model_schema,
        capabilities.model_schema,
    )
    _compare_exact(
        issues,
        "WIRE_MAJOR_MISMATCH",
        "wire_format.major",
        requirement.wire_major,
        capabilities.wire_format[0],
    )
    if capabilities.wire_format[1] < requirement.minimum_wire_minor:
        issues.append(
            CompatibilityIssue(
                "WIRE_MINOR_TOO_OLD",
                "wire_format.minor",
                f">={requirement.minimum_wire_minor}",
                str(capabilities.wire_format[1]),
            )
        )

    _compare_exact(
        issues,
        "CORE_VIEW_ADAPTER_DIVERGENCE",
        "core/view.adapter_protocol",
        selected_core.adapter_protocol,
        capabilities.adapter_protocol,
    )
    _compare_exact(
        issues,
        "CORE_VIEW_MODEL_DIVERGENCE",
        "core/view.model_schema",
        selected_core.model_schema,
        capabilities.model_schema,
    )
    _compare_exact(
        issues,
        "CORE_VIEW_WIRE_DIVERGENCE",
        "core/view.wire_format",
        selected_core.wire_format,
        capabilities.wire_format,
    )

    for feature in sorted(requirement.required_features - capabilities.features):
        issues.append(
            CompatibilityIssue("MISSING_FEATURE", f"feature:{feature}", "present", "missing")
        )
    for name, schema in requirement.required_encoded_view_schemas.items():
        actual = capabilities.encoded_view_schemas.get(name)
        if actual is None:
            issues.append(
                CompatibilityIssue(
                    "MISSING_ENCODED_VIEW",
                    f"encoded_view:{name}",
                    f">={schema}",
                    "missing",
                )
            )
        elif actual < schema:
            issues.append(
                CompatibilityIssue(
                    "ENCODED_VIEW_SCHEMA_TOO_OLD",
                    f"encoded_view:{name}",
                    f">={schema}",
                    str(actual),
                )
            )
    return NegotiationReport(requirement, selected_core, capabilities, tuple(issues))


def negotiate_view(
    view: OntologyView,
    requirement: AdapterRequirement,
    *,
    core: CoreContract | None = None,
) -> NegotiationReport:
    """Negotiate an already-coerced view without parsing, resolving, or discovery."""

    capabilities = getattr(view, "capabilities", None)
    return negotiate_capabilities(capabilities, requirement, core=core)


def require_compatible_view(
    view: OntologyView,
    requirement: AdapterRequirement,
    *,
    core: CoreContract | None = None,
) -> OntologyView:
    """Validate and return the exact supplied view identity or fail closed."""

    report = negotiate_view(view, requirement, core=core)
    report.raise_for_errors()
    return view


def _compare_exact(
    issues: list[CompatibilityIssue],
    code: str,
    field: str,
    expected: object,
    actual: object,
) -> None:
    if actual != expected:
        issues.append(CompatibilityIssue(code, field, str(expected), str(actual)))


__all__ = [
    "AdapterRequirement",
    "CompatibilityIssue",
    "CoreContract",
    "NegotiationReport",
    "negotiate_capabilities",
    "negotiate_view",
    "require_compatible_view",
]
