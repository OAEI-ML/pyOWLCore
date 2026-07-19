"""Fail-closed comparator pins and phase-fence validation for WP14."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, unused-ignore]

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COMPARATOR_MANIFEST = ROOT / "benchmarks" / "comparators" / "comparators.toml"

COMMON_BOUNDARY = "common-contract-ready"
RAW_HORNED_BOUNDARY = "horned-model-ready"
_BOUNDARIES = frozenset({COMMON_BOUNDARY, RAW_HORNED_BOUNDARY})
_FENCE_VALUES = frozenset({"inside", "outside", "not-applicable"})
_PIN_STATES = frozenset({"complete", "runtime-captured", "pending"})
_ADAPTERS = frozenset({"core-python", "core-native", "external-command"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_ROOT_FIELDS = frozenset(
    {
        "schema",
        "common_contract_schema",
        "corpus_manifest_sha256",
        "resolver_map_sha256",
        "jdk",
        "jvm_gc",
        "jvm_heap",
        "reference_machine",
        "comparator",
        "timing_fence",
    }
)
_REFERENCE_MACHINE_FIELDS = frozenset(
    {"id", "approval", "os", "cpu", "memory_bytes", "storage", "power_mode"}
)
_COMPARATOR_FIELDS = frozenset(
    {
        "id",
        "implementation",
        "boundary",
        "adapter",
        "pin_state",
        "version",
        "revision",
        "source_url",
        "artifact",
        "artifact_sha256",
        "features",
        "allocator",
        "thread_ceiling",
        "launcher_env",
        "gating",
        "required",
        "runner_pin_state",
        "runner_revision",
        "runner_sha256",
    }
)
_TIMING_FENCE_FIELDS = frozenset({"phase", "rationale", "lanes"})

REQUIRED_PHASES = (
    "byte_receipt",
    "syntax_parse",
    "rdf_to_owl_mapping",
    "interning",
    "canonicalization",
    "freeze",
    "document_fingerprint",
    "structural_fingerprint",
    "logical_fingerprint",
    "signature_fingerprint",
    "provenance",
    "diagnostics",
    "required_indexes",
    "common_adapter_traversal",
    "common_adapter_digests",
    "publication",
    "equality_assertion",
)

REQUIRED_IMPLEMENTATIONS = frozenset(
    {
        "pyowl-core-python",
        "pyowl-core-native-wheel",
        "pyowl-core-direct-rust",
        "horned-owl",
        "py-horned-owl",
        "owlapi",
    }
)

_NORMATIVE_LANE_FIELDS = (
    "implementation",
    "boundary",
    "adapter",
    "gating",
    "required",
)
_NORMATIVE_LANE_POLICY: Mapping[str, tuple[str, str, str, bool, bool]] = MappingProxyType(
    {
        "pyowl-python-common": (
            "pyowl-core-python",
            COMMON_BOUNDARY,
            "core-python",
            True,
            True,
        ),
        "pyowl-native-wheel-common": (
            "pyowl-core-native-wheel",
            COMMON_BOUNDARY,
            "core-native",
            True,
            True,
        ),
        "pyowl-direct-rust-common": (
            "pyowl-core-direct-rust",
            COMMON_BOUNDARY,
            "external-command",
            True,
            True,
        ),
        "horned-owl-raw": (
            "horned-owl",
            RAW_HORNED_BOUNDARY,
            "external-command",
            False,
            True,
        ),
        "horned-owl-common": (
            "horned-owl",
            COMMON_BOUNDARY,
            "external-command",
            True,
            True,
        ),
        "py-horned-common": (
            "py-horned-owl",
            COMMON_BOUNDARY,
            "external-command",
            True,
            True,
        ),
        "owlapi-common": (
            "owlapi",
            COMMON_BOUNDARY,
            "external-command",
            True,
            True,
        ),
    }
)


class ComparatorManifestError(ValueError):
    """Comparator evidence is incomplete, ambiguous, or unsafe to compare."""


@dataclass(frozen=True, slots=True)
class ComparatorPin:
    """One exact comparator lane and its artifact/runtime constraints."""

    id: str
    implementation: str
    boundary: str
    adapter: str
    pin_state: str
    version: str
    revision: str
    source_url: str
    artifact: str
    artifact_sha256: str | None
    features: tuple[str, ...]
    allocator: str
    thread_ceiling: int
    launcher_env: str | None
    gating: bool
    required: bool
    runner_pin_state: str | None = None
    runner_revision: str | None = None
    runner_sha256: str | None = None

    def __post_init__(self) -> None:
        if not _identifier(self.id):
            raise ComparatorManifestError(f"invalid comparator id: {self.id!r}")
        if not _identifier(self.implementation):
            raise ComparatorManifestError(f"{self.id}: invalid implementation id")
        if self.boundary not in _BOUNDARIES:
            raise ComparatorManifestError(f"{self.id}: unsupported readiness boundary")
        if self.adapter not in _ADAPTERS:
            raise ComparatorManifestError(f"{self.id}: unsupported adapter")
        if self.pin_state not in _PIN_STATES:
            raise ComparatorManifestError(f"{self.id}: unsupported pin_state")
        for name in ("version", "revision", "source_url", "artifact", "allocator"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ComparatorManifestError(f"{self.id}: {name} must be nonempty")
        if not self.source_url.startswith("https://"):
            raise ComparatorManifestError(f"{self.id}: source_url must use HTTPS")
        if self.pin_state == "complete":
            if self.artifact_sha256 is None or not _SHA256.fullmatch(self.artifact_sha256):
                raise ComparatorManifestError(
                    f"{self.id}: complete pin requires lowercase artifact SHA-256"
                )
        elif self.artifact_sha256 is not None:
            raise ComparatorManifestError(
                f"{self.id}: non-complete pin cannot advertise an artifact SHA-256"
            )
        if not self.features or any(not _identifier(value) for value in self.features):
            raise ComparatorManifestError(f"{self.id}: features must be identifiers")
        if len(set(self.features)) != len(self.features):
            raise ComparatorManifestError(f"{self.id}: duplicate feature")
        if (
            isinstance(self.thread_ceiling, bool)
            or not isinstance(self.thread_ceiling, int)
            or self.thread_ceiling < 1
        ):
            raise ComparatorManifestError(f"{self.id}: thread_ceiling must be positive")
        if self.adapter == "external-command" and not self.launcher_env:
            raise ComparatorManifestError(f"{self.id}: external adapter requires launcher_env")
        if self.adapter != "external-command" and self.launcher_env is not None:
            raise ComparatorManifestError(
                f"{self.id}: only external adapters may declare launcher_env"
            )
        if self.boundary == RAW_HORNED_BOUNDARY and (
            self.implementation != "horned-owl" or self.gating
        ):
            raise ComparatorManifestError(
                f"{self.id}: raw Horned readiness is Horned-only and non-gating"
            )
        if self.gating and self.boundary != COMMON_BOUNDARY:
            raise ComparatorManifestError(f"{self.id}: only common readiness may gate")
        runner_fields = (
            self.runner_pin_state,
            self.runner_revision,
            self.runner_sha256,
        )
        if self.adapter == "external-command":
            if self.runner_pin_state not in {"complete", "pending"}:
                raise ComparatorManifestError(
                    f"{self.id}: external runner pin_state must be complete or pending"
                )
            if not self.runner_revision:
                raise ComparatorManifestError(f"{self.id}: external runner requires a revision")
            if self.runner_pin_state == "complete":
                if self.runner_sha256 is None or not _SHA256.fullmatch(self.runner_sha256):
                    raise ComparatorManifestError(
                        f"{self.id}: complete external runner requires SHA-256"
                    )
            elif self.runner_sha256 is not None:
                raise ComparatorManifestError(f"{self.id}: pending runner cannot advertise SHA-256")
        elif any(value is not None for value in runner_fields):
            raise ComparatorManifestError(
                f"{self.id}: only external adapters may declare runner pins"
            )

    @property
    def artifact_is_runnable(self) -> bool:
        """Whether engine and independently pinned runner evidence is runnable."""

        engine_ready = self.pin_state in {"complete", "runtime-captured"}
        if self.adapter == "external-command":
            return (
                engine_ready
                and self.runner_pin_state == "complete"
                and self.runner_sha256 is not None
            )
        return engine_ready


@dataclass(frozen=True, slots=True)
class TimingFence:
    """One phase's inclusion decision for every comparator lane."""

    phase: str
    rationale: str
    lanes: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.phase not in REQUIRED_PHASES:
            raise ComparatorManifestError(f"unknown timing phase: {self.phase!r}")
        if not self.rationale:
            raise ComparatorManifestError(f"{self.phase}: rationale must be nonempty")
        if any(value not in _FENCE_VALUES for value in self.lanes.values()):
            raise ComparatorManifestError(f"{self.phase}: invalid fence value")
        object.__setattr__(self, "lanes", MappingProxyType(dict(self.lanes)))


@dataclass(frozen=True, slots=True)
class ReferenceMachine:
    id: str
    approval: str
    os: str
    cpu: str
    memory_bytes: int
    storage: str
    power_mode: str

    def __post_init__(self) -> None:
        for name in ("id", "approval", "os", "cpu", "storage", "power_mode"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ComparatorManifestError(f"reference_machine.{name} must be nonempty")
        if self.approval not in {"approved", "pending"}:
            raise ComparatorManifestError("reference_machine.approval must be approved or pending")
        if (
            isinstance(self.memory_bytes, bool)
            or not isinstance(self.memory_bytes, int)
            or self.memory_bytes < 1
        ):
            raise ComparatorManifestError("reference_machine.memory_bytes must be positive")


@dataclass(frozen=True, slots=True)
class ComparatorManifest:
    schema: int
    common_contract_schema: int
    corpus_manifest_sha256: str
    jdk: str
    jvm_gc: str
    jvm_heap: str
    resolver_map_sha256: str
    reference_machine: ReferenceMachine
    comparators: tuple[ComparatorPin, ...]
    timing_fences: tuple[TimingFence, ...]

    def __post_init__(self) -> None:
        if self.schema != 1 or self.common_contract_schema != 1:
            raise ComparatorManifestError("unsupported comparator/common-contract schema")
        for name in ("corpus_manifest_sha256", "resolver_map_sha256"):
            if not _SHA256.fullmatch(getattr(self, name)):
                raise ComparatorManifestError(f"{name} must be lowercase SHA-256")
        for name in ("jdk", "jvm_gc", "jvm_heap"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ComparatorManifestError(f"{name} must be nonempty")
        ids = tuple(value.id for value in self.comparators)
        if len(set(ids)) != len(ids):
            raise ComparatorManifestError("comparator ids must be unique")
        expected_ids = set(_NORMATIVE_LANE_POLICY)
        observed_ids = set(ids)
        if observed_ids != expected_ids:
            missing_ids = sorted(expected_ids - observed_ids)
            unexpected_ids = sorted(observed_ids - expected_ids)
            raise ComparatorManifestError(
                "comparator inventory differs from the normative seven lanes: "
                f"missing={missing_ids}, unexpected={unexpected_ids}"
            )
        by_id = {value.id: value for value in self.comparators}
        for comparator_id, expected in _NORMATIVE_LANE_POLICY.items():
            pin = by_id[comparator_id]
            observed = tuple(getattr(pin, field) for field in _NORMATIVE_LANE_FIELDS)
            if observed != expected:
                mismatched = [
                    field
                    for field, actual, required in zip(
                        _NORMATIVE_LANE_FIELDS, observed, expected, strict=True
                    )
                    if actual != required
                ]
                raise ComparatorManifestError(
                    f"{comparator_id}: normative lane policy differs: " + ", ".join(mismatched)
                )
        implementations = {value.implementation for value in self.comparators}
        missing = sorted(REQUIRED_IMPLEMENTATIONS - implementations)
        if missing:
            raise ComparatorManifestError(
                "missing comparator implementations: " + ", ".join(missing)
            )
        raw = [
            value
            for value in self.comparators
            if value.implementation == "horned-owl" and value.boundary == RAW_HORNED_BOUNDARY
        ]
        common = [
            value
            for value in self.comparators
            if value.implementation == "horned-owl" and value.boundary == COMMON_BOUNDARY
        ]
        if len(raw) != 1 or len(common) != 1:
            raise ComparatorManifestError("Horned requires exactly one raw and one common lane")
        engine_pin_fields = (
            "version",
            "revision",
            "source_url",
            "artifact",
            "artifact_sha256",
            "features",
            "allocator",
            "thread_ceiling",
        )
        mismatched = [
            name for name in engine_pin_fields if getattr(raw[0], name) != getattr(common[0], name)
        ]
        if mismatched:
            raise ComparatorManifestError(
                "raw/common Horned engine pins differ: " + ", ".join(mismatched)
            )
        phases = tuple(value.phase for value in self.timing_fences)
        if phases != REQUIRED_PHASES:
            raise ComparatorManifestError(
                "timing fences must contain every required phase in normative order"
            )
        expected_lanes = set(ids)
        for fence in self.timing_fences:
            if set(fence.lanes) != expected_lanes:
                raise ComparatorManifestError(
                    f"{fence.phase}: fence lanes differ from comparator inventory"
                )
        equality = self.fence("equality_assertion")
        if any(value != "outside" for value in equality.lanes.values()):
            raise ComparatorManifestError("equality assertions must be outside every timer")
        for pin in self.comparators:
            if pin.boundary == COMMON_BOUNDARY:
                for phase in (
                    "document_fingerprint",
                    "structural_fingerprint",
                    "logical_fingerprint",
                    "signature_fingerprint",
                    "provenance",
                    "diagnostics",
                    "common_adapter_traversal",
                    "common_adapter_digests",
                    "publication",
                ):
                    if self.fence(phase).lanes[pin.id] != "inside":
                        raise ComparatorManifestError(
                            f"{pin.id}: common-contract phase {phase} must be inside"
                        )
        raw_id = raw[0].id
        for phase in (
            "document_fingerprint",
            "structural_fingerprint",
            "logical_fingerprint",
            "signature_fingerprint",
            "common_adapter_traversal",
            "common_adapter_digests",
        ):
            if self.fence(phase).lanes[raw_id] != "not-applicable":
                raise ComparatorManifestError(
                    f"{raw_id}: raw Horned phase {phase} must be not-applicable"
                )

    def by_id(self, comparator_id: str) -> ComparatorPin:
        for value in self.comparators:
            if value.id == comparator_id:
                return value
        raise ComparatorManifestError(f"unknown comparator id: {comparator_id}")

    def fence(self, phase: str) -> TimingFence:
        for value in self.timing_fences:
            if value.phase == phase:
                return value
        raise ComparatorManifestError(f"unknown timing phase: {phase}")


def load_comparator_manifest(
    path: Path = DEFAULT_COMPARATOR_MANIFEST,
) -> ComparatorManifest:
    """Read and completely validate the comparator pin/fence ledger."""

    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ComparatorManifestError(f"cannot read comparator manifest: {error}") from error
    root = _mapping(payload, "manifest")
    _reject_unknown_fields(root, _ROOT_FIELDS, "manifest")
    machine = _mapping(root.get("reference_machine"), "reference_machine")
    _reject_unknown_fields(machine, _REFERENCE_MACHINE_FIELDS, "reference_machine")
    comparators = tuple(
        _parse_comparator(_mapping(value, "comparator row"))
        for value in _list(root.get("comparator"), "comparator")
    )
    ids = tuple(value.id for value in comparators)
    fences = tuple(
        _parse_fence(_mapping(value, "timing_fence row"), ids)
        for value in _list(root.get("timing_fence"), "timing_fence")
    )
    return ComparatorManifest(
        schema=_integer(root.get("schema"), "schema"),
        common_contract_schema=_integer(
            root.get("common_contract_schema"), "common_contract_schema"
        ),
        corpus_manifest_sha256=_string(
            root.get("corpus_manifest_sha256"), "corpus_manifest_sha256"
        ),
        jdk=_string(root.get("jdk"), "jdk"),
        jvm_gc=_string(root.get("jvm_gc"), "jvm_gc"),
        jvm_heap=_string(root.get("jvm_heap"), "jvm_heap"),
        resolver_map_sha256=_string(root.get("resolver_map_sha256"), "resolver_map_sha256"),
        reference_machine=ReferenceMachine(
            id=_string(machine.get("id"), "reference_machine.id"),
            approval=_string(machine.get("approval"), "reference_machine.approval"),
            os=_string(machine.get("os"), "reference_machine.os"),
            cpu=_string(machine.get("cpu"), "reference_machine.cpu"),
            memory_bytes=_integer(machine.get("memory_bytes"), "reference_machine.memory_bytes"),
            storage=_string(machine.get("storage"), "reference_machine.storage"),
            power_mode=_string(machine.get("power_mode"), "reference_machine.power_mode"),
        ),
        comparators=comparators,
        timing_fences=fences,
    )


def _parse_comparator(row: Mapping[str, Any]) -> ComparatorPin:
    _reject_unknown_fields(row, _COMPARATOR_FIELDS, "comparator")
    artifact_sha256 = row.get("artifact_sha256")
    launcher_env = row.get("launcher_env")
    runner_pin_state = row.get("runner_pin_state")
    runner_revision = row.get("runner_revision")
    runner_sha256 = row.get("runner_sha256")
    return ComparatorPin(
        id=_string(row.get("id"), "comparator.id"),
        implementation=_string(row.get("implementation"), "comparator.implementation"),
        boundary=_string(row.get("boundary"), "comparator.boundary"),
        adapter=_string(row.get("adapter"), "comparator.adapter"),
        pin_state=_string(row.get("pin_state"), "comparator.pin_state"),
        version=_string(row.get("version"), "comparator.version"),
        revision=_string(row.get("revision"), "comparator.revision"),
        source_url=_string(row.get("source_url"), "comparator.source_url"),
        artifact=_string(row.get("artifact"), "comparator.artifact"),
        artifact_sha256=(
            None
            if artifact_sha256 is None
            else _string(artifact_sha256, "comparator.artifact_sha256")
        ),
        features=_string_tuple(row.get("features"), "comparator.features"),
        allocator=_string(row.get("allocator"), "comparator.allocator"),
        thread_ceiling=_integer(row.get("thread_ceiling"), "comparator.thread_ceiling"),
        launcher_env=(
            None if launcher_env is None else _string(launcher_env, "comparator.launcher_env")
        ),
        gating=_boolean(row.get("gating"), "comparator.gating"),
        required=_boolean(row.get("required"), "comparator.required"),
        runner_pin_state=(
            None
            if runner_pin_state is None
            else _string(runner_pin_state, "comparator.runner_pin_state")
        ),
        runner_revision=(
            None
            if runner_revision is None
            else _string(runner_revision, "comparator.runner_revision")
        ),
        runner_sha256=(
            None if runner_sha256 is None else _string(runner_sha256, "comparator.runner_sha256")
        ),
    )


def _parse_fence(row: Mapping[str, Any], ids: tuple[str, ...]) -> TimingFence:
    _reject_unknown_fields(row, _TIMING_FENCE_FIELDS, "timing_fence")
    lanes = _mapping(row.get("lanes"), "timing_fence.lanes")
    clean: dict[str, str] = {}
    for comparator_id in ids:
        clean[comparator_id] = _string(lanes.get(comparator_id), comparator_id)
    if set(lanes) != set(ids):
        raise ComparatorManifestError("timing fence contains unknown/missing lanes")
    return TimingFence(
        phase=_string(row.get("phase"), "timing_fence.phase"),
        rationale=_string(row.get("rationale"), "timing_fence.rationale"),
        lanes=clean,
    )


def _identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9-]*", value))


def _reject_unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ComparatorManifestError(f"{field} contains unknown fields: " + ", ".join(unknown))


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ComparatorManifestError(f"{field} must be a table")
    return cast(Mapping[str, Any], value)


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ComparatorManifestError(f"{field} must be an array of tables")
    return cast(list[object], value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComparatorManifestError(f"{field} must be a nonempty string")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComparatorManifestError(f"{field} must be an integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ComparatorManifestError(f"{field} must be boolean")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ComparatorManifestError(f"{field} must be an array of strings")
    return tuple(cast(list[str], value))


__all__ = [
    "COMMON_BOUNDARY",
    "DEFAULT_COMPARATOR_MANIFEST",
    "RAW_HORNED_BOUNDARY",
    "REQUIRED_IMPLEMENTATIONS",
    "REQUIRED_PHASES",
    "ComparatorManifest",
    "ComparatorManifestError",
    "ComparatorPin",
    "ReferenceMachine",
    "TimingFence",
    "load_comparator_manifest",
]
