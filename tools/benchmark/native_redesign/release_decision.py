"""Derive the two fail-closed WP18 decisions from exact evidence rows."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import cast

INPUT_SCHEMA = "pyowl-core.native-redesign-release-evidence/1"
DECISION_SCHEMA = "pyowl-core.native-redesign-release-decision/1"

REQUIRED_CORE_GATES = (
    "semantic-differential-conformance",
    "security-resource-determinism",
    "retained-storage-no-eager-expansion",
    "encoded-view-installed-integration",
    "direct-decoded-mmap-overlay-composite-parity",
    "pure-and-native-artifact-matrix",
    "horned-direct-common-contract",
    "horned-wheel-common-contract",
    "version-schema-consistency",
    "artifact-license-sbom-java-audit",
)
REQUIRED_WORKSPACE_CONSUMERS: Mapping[str, str] = MappingProxyType(
    {
        "exact-om": "compatibility-consumer",
        "oaei-bioml-eval": "compatibility-consumer",
        "projector": "encoded-native-compiler",
        "pyelk": "encoded-native-compiler",
        "pyhermit": "encoded-native-compiler",
    }
)

_ROOT_FIELDS = frozenset({"schema", "core_revision", "core_gates", "workspace_consumers"})
_GATE_FIELDS = frozenset({"id", "status", "reason", "evidence"})
_CONSUMER_FIELDS = frozenset({"id", "role", "status", "revision", "reason", "evidence"})
_STATUSES = frozenset({"pass", "fail", "not-run"})
_GIT_REVISION = re.compile(r"[0-9a-f]{40}\Z")


class ReleaseDecisionError(ValueError):
    """Release evidence is incomplete, ambiguous, or claims an unproved pass."""


def load_release_evidence(path: Path) -> dict[str, object]:
    """Load one evidence document and return its derived decisions."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseDecisionError(f"cannot load release evidence: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseDecisionError("release evidence root must be an object")
    return evaluate_release_decision(cast(dict[str, object], payload))


def evaluate_release_decision(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate evidence and derive core and workspace decisions independently."""

    _require_exact_fields(payload, _ROOT_FIELDS, "release evidence")
    if payload["schema"] != INPUT_SCHEMA:
        raise ReleaseDecisionError(f"unsupported release evidence schema: {payload['schema']!r}")
    core_revision = _require_revision(payload["core_revision"], "core_revision")
    core_rows = _validate_rows(
        payload["core_gates"],
        expected=MappingProxyType({value: None for value in REQUIRED_CORE_GATES}),
        consumer=False,
    )
    consumer_rows = _validate_rows(
        payload["workspace_consumers"],
        expected=REQUIRED_WORKSPACE_CONSUMERS,
        consumer=True,
    )

    core_blockers = tuple(_blocker(row) for row in core_rows if row["status"] != "pass")
    consumer_blockers = tuple(
        _blocker(row, include_revision=True) for row in consumer_rows if row["status"] != "pass"
    )
    core_release_eligible = not core_blockers
    consumers_complete = not consumer_blockers
    workspace_optimization_complete = core_release_eligible and consumers_complete

    workspace_blockers: list[dict[str, object]] = list(consumer_blockers)
    if not core_release_eligible:
        workspace_blockers.insert(
            0,
            {
                "id": "core-release-eligibility",
                "status": "fail",
                "reason": "workspace completion requires core_release_eligible=true",
            },
        )

    return {
        "schema": DECISION_SCHEMA,
        "source_schema": INPUT_SCHEMA,
        "core_revision": core_revision,
        "core_release_eligible": core_release_eligible,
        "workspace_optimization_complete": workspace_optimization_complete,
        "core": {
            "status": "pass" if core_release_eligible else "fail",
            "required_gate_count": len(REQUIRED_CORE_GATES),
            "passed_gate_count": len(REQUIRED_CORE_GATES) - len(core_blockers),
            "blockers": list(core_blockers),
        },
        "workspace": {
            "status": "pass" if workspace_optimization_complete else "fail",
            "required_consumer_count": len(REQUIRED_WORKSPACE_CONSUMERS),
            "passed_consumer_count": len(REQUIRED_WORKSPACE_CONSUMERS) - len(consumer_blockers),
            "not_run_consumers": [
                cast(str, row["id"]) for row in consumer_rows if row["status"] == "not-run"
            ],
            "blockers": workspace_blockers,
        },
        "claims": {
            "core_release_allowed": core_release_eligible,
            "multi_consumer_native_performance_allowed": workspace_optimization_complete,
        },
    }


def _validate_rows(
    value: object,
    *,
    expected: Mapping[str, str | None],
    consumer: bool,
) -> tuple[dict[str, object], ...]:
    label = "workspace_consumers" if consumer else "core_gates"
    if not isinstance(value, list):
        raise ReleaseDecisionError(f"{label} must be an array")
    rows: list[dict[str, object]] = []
    observed: set[str] = set()
    fields = _CONSUMER_FIELDS if consumer else _GATE_FIELDS
    for index, candidate in enumerate(value):
        if not isinstance(candidate, dict):
            raise ReleaseDecisionError(f"{label}[{index}] must be an object")
        row = cast(dict[str, object], candidate)
        _require_exact_fields(row, fields, f"{label}[{index}]")
        identifier = row["id"]
        if not isinstance(identifier, str) or identifier not in expected:
            raise ReleaseDecisionError(f"{label}[{index}] has unknown id: {identifier!r}")
        if identifier in observed:
            raise ReleaseDecisionError(f"{label} contains duplicate id: {identifier}")
        observed.add(identifier)
        _validate_status_row(row, f"{label}[{index}]")
        if consumer:
            required_role = expected[identifier]
            if row["role"] != required_role:
                raise ReleaseDecisionError(
                    f"{identifier}: role must be {required_role!r}, got {row['role']!r}"
                )
            revision = row["revision"]
            if revision is not None:
                _require_revision(revision, f"{identifier}.revision")
            if row["status"] in {"pass", "fail"} and revision is None:
                raise ReleaseDecisionError(
                    f"{identifier}: {row['status']} requires an exact revision"
                )
        rows.append(dict(row))
    missing = set(expected).difference(observed)
    if missing:
        raise ReleaseDecisionError(f"{label} is missing required ids: {sorted(missing)!r}")
    return tuple(sorted(rows, key=lambda row: cast(str, row["id"])))


def _validate_status_row(row: Mapping[str, object], label: str) -> None:
    status = row["status"]
    if status not in _STATUSES:
        raise ReleaseDecisionError(f"{label}.status must be pass, fail, or not-run")
    reason = row["reason"]
    evidence = row["evidence"]
    if not isinstance(evidence, list) or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        raise ReleaseDecisionError(f"{label}.evidence must be an array of nonempty strings")
    if len(set(cast(list[str], evidence))) != len(evidence):
        raise ReleaseDecisionError(f"{label}.evidence contains duplicates")
    if status == "pass":
        if not evidence:
            raise ReleaseDecisionError(f"{label}: pass requires evidence")
        if reason is not None:
            raise ReleaseDecisionError(f"{label}: pass reason must be null")
        return
    if not isinstance(reason, str) or not reason.strip():
        raise ReleaseDecisionError(f"{label}: {status} requires a nonempty reason")


def _blocker(
    row: Mapping[str, object],
    *,
    include_revision: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": row["id"],
        "status": row["status"],
        "reason": row["reason"],
    }
    if include_revision:
        result["revision"] = row["revision"]
    return result


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    observed = set(value)
    missing = expected.difference(observed)
    unknown = observed.difference(expected)
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append(f"missing fields {sorted(missing)!r}")
        if unknown:
            parts.append(f"unknown fields {sorted(unknown)!r}")
        raise ReleaseDecisionError(f"{label}: {'; '.join(parts)}")


def _require_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _GIT_REVISION.fullmatch(value):
        raise ReleaseDecisionError(f"{label} must be an exact lowercase 40-hex Git revision")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = load_release_evidence(arguments.evidence)
    except ReleaseDecisionError as error:
        parser.error(str(error))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(rendered, end="")
    else:
        arguments.output.write_text(rendered, encoding="utf-8")
    return 0 if result["core_release_eligible"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
