"""Pinned deterministic Functional Syntax inputs for the WP23 evidence lane."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT_LOCK = ROOT / "benchmarks" / "component_canonicalization" / "inputs.json"
INPUT_SCHEMA = "pyowl-core/component-canonicalization-input-lock/v1"
GENERATOR_ID = "pyowl-core/component-canonicalization-functional/v1"

_CASE_KINDS = frozenset({"fixed-components", "oversized-component"})
_PROFILES = frozenset({"smoke", "release"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ROOT_FIELDS = frozenset({"schema", "generator", "model_schema", "cases"})
_CASE_FIELDS = frozenset(
    {
        "id",
        "kind",
        "profiles",
        "component_count",
        "labels_per_component",
        "max_canonical_work",
        "source_bytes",
        "source_sha256",
    }
)


class InputLockError(ValueError):
    """The generated-input lock is incomplete, ambiguous, or stale."""


@dataclass(frozen=True, slots=True)
class InputCase:
    id: str
    kind: Literal["fixed-components", "oversized-component"]
    profiles: tuple[Literal["smoke", "release"], ...]
    component_count: int
    labels_per_component: int
    max_canonical_work: int
    source_bytes: int
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.id or any(
            character not in "-0123456789abcdefghijklmnopqrstuvwxyz" for character in self.id
        ):
            raise InputLockError("case id must be a nonempty lowercase identifier")
        if self.kind not in _CASE_KINDS:
            raise InputLockError(f"{self.id}: unsupported case kind")
        if not self.profiles or set(self.profiles).difference(_PROFILES):
            raise InputLockError(f"{self.id}: profiles must select smoke and/or release")
        if len(set(self.profiles)) != len(self.profiles):
            raise InputLockError(f"{self.id}: profiles contain duplicates")
        for name in (
            "component_count",
            "labels_per_component",
            "max_canonical_work",
            "source_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise InputLockError(f"{self.id}: {name} must be a positive integer")
        if _SHA256.fullmatch(self.source_sha256) is None:
            raise InputLockError(f"{self.id}: source_sha256 must be lowercase 64-hex")
        if self.kind == "fixed-components":
            if self.labels_per_component != 1:
                raise InputLockError(f"{self.id}: fixed-components currently pins size one")
            if self.max_canonical_work != 9:
                raise InputLockError(f"{self.id}: size-one components require work limit 9")
        else:
            if self.component_count != 1 or self.labels_per_component < 2:
                raise InputLockError(
                    f"{self.id}: oversized-component requires one multi-label component"
                )
            arcs = self.labels_per_component + self.labels_per_component * (
                self.labels_per_component - 1
            ) // 2
            if self.max_canonical_work != self.labels_per_component + 2 * arcs - 1:
                raise InputLockError(
                    f"{self.id}: oversized limit must be one below setup work"
                )


@dataclass(frozen=True, slots=True)
class InputLock:
    schema: str
    generator: str
    model_schema: int
    cases: tuple[InputCase, ...]
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if self.schema != INPUT_SCHEMA:
            raise InputLockError("unsupported component input-lock schema")
        if self.generator != GENERATOR_ID:
            raise InputLockError("unsupported component input generator")
        if self.model_schema != 2:
            raise InputLockError("component input lock must target model schema 2")
        if _SHA256.fullmatch(self.sha256) is None:
            raise InputLockError("component input lock digest must be lowercase 64-hex")
        ids = tuple(case.id for case in self.cases)
        if len(set(ids)) != len(ids):
            raise InputLockError("component input case ids must be unique")
        if not self.cases:
            raise InputLockError("component input lock must contain cases")
        for profile in _PROFILES:
            selected = self.for_profile(profile)
            if not selected:
                raise InputLockError(f"component input lock has no {profile} cases")
            if not any(case.kind == "fixed-components" for case in selected):
                raise InputLockError(f"{profile} profile lacks a scaling case")
            if not any(case.kind == "oversized-component" for case in selected):
                raise InputLockError(f"{profile} profile lacks an oversized case")

    def for_profile(self, profile: str) -> tuple[InputCase, ...]:
        if profile not in _PROFILES:
            raise InputLockError(f"unknown evidence profile: {profile!r}")
        return tuple(case for case in self.cases if profile in case.profiles)

    def by_id(self, case_id: str) -> InputCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise InputLockError(f"unknown component input case: {case_id!r}")


def fixed_components_source(components: int) -> bytes:
    """Generate disconnected isomorphic one-label components deterministically."""

    _positive_integer(components, "components")
    base = "https://example.org/pyowl-core/benchmark"
    class_iri = f"<{base}#AnonymousComponent>"
    assertions = "\n".join(
        f"  ClassAssertion({class_iri} _:component{index:08d})"
        for index in range(components)
    )
    return (
        f"Ontology(<{base}/anonymous-components>\n"
        f"  Declaration(Class({class_iri}))\n"
        f"{assertions}\n"
        ")\n"
    ).encode()


def oversized_component_source(labels: int) -> bytes:
    """Generate one connected symmetric component with a factorial candidate class."""

    _positive_integer(labels, "labels")
    if labels < 2:
        raise ValueError("labels must be at least two")
    members = " ".join(f"_:member{index:08d}" for index in range(labels))
    return (
        "Ontology(<https://example.org/pyowl-core/benchmark/oversized-component>\n"
        f"  SameIndividual({members})\n"
        ")\n"
    ).encode()


def source_for_case(case: InputCase) -> bytes:
    if not isinstance(case, InputCase):
        raise TypeError("case must be InputCase")
    if case.kind == "fixed-components":
        return fixed_components_source(case.component_count)
    return oversized_component_source(case.labels_per_component)


def load_input_lock(path: Path = DEFAULT_INPUT_LOCK) -> InputLock:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InputLockError(f"cannot load component input lock: {error}") from error
    root = _mapping(payload, "input lock")
    _exact_fields(root, _ROOT_FIELDS, "input lock")
    rows = root["cases"]
    if not isinstance(rows, list):
        raise InputLockError("input lock cases must be an array")
    cases = tuple(_parse_case(row, index) for index, row in enumerate(rows))
    return InputLock(
        _string(root["schema"], "schema"),
        _string(root["generator"], "generator"),
        _integer(root["model_schema"], "model_schema"),
        cases,
        path,
        hashlib.sha256(raw).hexdigest(),
    )


def verify_input_lock(lock: InputLock) -> None:
    if not isinstance(lock, InputLock):
        raise TypeError("lock must be InputLock")
    for case in lock.cases:
        source = source_for_case(case)
        if len(source) != case.source_bytes:
            raise InputLockError(f"{case.id}: generated byte count differs from lock")
        if hashlib.sha256(source).hexdigest() != case.source_sha256:
            raise InputLockError(f"{case.id}: generated SHA-256 differs from lock")


def _parse_case(value: object, index: int) -> InputCase:
    row = _mapping(value, f"cases[{index}]")
    _exact_fields(row, _CASE_FIELDS, f"cases[{index}]")
    profiles_value = row["profiles"]
    if not isinstance(profiles_value, list):
        raise InputLockError(f"cases[{index}].profiles must be an array")
    profiles = tuple(
        cast(Literal["smoke", "release"], _string(item, f"cases[{index}].profiles"))
        for item in profiles_value
    )
    return InputCase(
        _string(row["id"], f"cases[{index}].id"),
        cast(
            Literal["fixed-components", "oversized-component"],
            _string(row["kind"], f"cases[{index}].kind"),
        ),
        profiles,
        _integer(row["component_count"], f"cases[{index}].component_count"),
        _integer(row["labels_per_component"], f"cases[{index}].labels_per_component"),
        _integer(row["max_canonical_work"], f"cases[{index}].max_canonical_work"),
        _integer(row["source_bytes"], f"cases[{index}].source_bytes"),
        _string(row["source_sha256"], f"cases[{index}].source_sha256"),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputLockError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputLockError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        raise InputLockError(
            f"{label} fields differ: missing={sorted(expected - observed)!r}, "
            f"unknown={sorted(observed - expected)!r}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputLockError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputLockError(f"{label} must be an integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    selected = _integer(value, label)
    if selected < 1:
        raise ValueError(f"{label} must be positive")
    return selected


__all__ = [
    "DEFAULT_INPUT_LOCK",
    "GENERATOR_ID",
    "INPUT_SCHEMA",
    "InputCase",
    "InputLock",
    "InputLockError",
    "fixed_components_source",
    "load_input_lock",
    "oversized_component_source",
    "source_for_case",
    "verify_input_lock",
]
