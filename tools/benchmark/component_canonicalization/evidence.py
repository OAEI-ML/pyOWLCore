"""Generate and fail-closed validate deterministic WP23 component evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pyowl_core.model as model
from pyowl_core import (
    API_VERSION,
    MODEL_SCHEMA_VERSION,
    BackendPreference,
    ImportPolicy,
    LoadOptions,
    ParseLimits,
    ResourceLimitError,
    parse_document,
)
from pyowl_core.document import document as document_impl

from ..report import write_json
from .inputs import (
    DEFAULT_INPUT_LOCK,
    InputCase,
    InputLock,
    InputLockError,
    load_input_lock,
    source_for_case,
    verify_input_lock,
)

REPORT_SCHEMA = "pyowl-core/component-canonicalization-evidence/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_ROOT_FIELDS = frozenset(
    {"schema", "profile", "input_lock", "contract", "status", "cases", "claims"}
)
_LOCK_FIELDS = frozenset({"schema", "generator", "sha256"})
_CONTRACT_FIELDS = frozenset({"api_version", "model_schema", "backend", "format"})
_CASE_FIELDS = frozenset({"id", "kind", "status", "input", "shape", "work", "error", "output"})
_INPUT_FIELDS = frozenset(
    {
        "bytes",
        "sha256",
        "component_count",
        "labels_per_component",
        "max_canonical_work",
    }
)
_SHAPE_FIELDS = frozenset(
    {
        "component_count",
        "largest_component_labels",
        "largest_component_arcs",
        "largest_component_roots",
        "maximum_root_interval_span",
        "maximum_open_root_intervals",
        "total_labels",
        "total_arcs",
    }
)
_WORK_FIELDS = frozenset(
    {
        "component_record_count",
        "total_setup_work",
        "total_refinement_work",
        "total_candidate_order_work",
        "total_canonical_work",
        "largest_component_work",
        "maximum_refinement_rounds",
        "total_permutations_examined",
    }
)
_ERROR_FIELDS = frozenset({"type", "code", "limit", "observed", "allowed", "details"})
_ERROR_DETAIL_FIELDS = frozenset(
    {
        "component_count",
        "largest_component_labels",
        "largest_component_arcs",
        "refinement_rounds",
        "work_term",
    }
)
_OUTPUT_FIELDS = frozenset(
    {
        "axiom_count",
        "anonymous_individual_count",
        "document_fingerprint_schema",
        "document_fingerprint_sha256",
    }
)
_CLAIM_FIELDS = frozenset(
    {
        "input_hashes_verified",
        "fixed_component_work_is_additive",
        "fixed_component_work_per_component",
        "oversized_component_failed_closed",
        "bounded_component_metrics_present",
        "performance_claim",
    }
)

_COMPONENT_CLASS = model.Class(
    model.IRI("https://example.org/pyowl-core/benchmark#AnonymousComponent")
)


class EvidenceError(ValueError):
    """Component evidence is incomplete, inconsistent, or overclaims results."""


def generate_report(lock: InputLock, *, profile: str = "smoke") -> dict[str, object]:
    """Run one deterministic evidence profile and validate it before returning."""

    if not isinstance(lock, InputLock):
        raise TypeError("lock must be InputLock")
    verify_input_lock(lock)
    selected = lock.for_profile(profile)
    rows = [_run_case(case) for case in selected]
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "profile": profile,
        "input_lock": {
            "schema": lock.schema,
            "generator": lock.generator,
            "sha256": lock.sha256,
        },
        "contract": {
            "api_version": list(API_VERSION),
            "model_schema": MODEL_SCHEMA_VERSION,
            "backend": "python",
            "format": "functional",
        },
        "status": "pass",
        "cases": rows,
        "claims": _derive_claims(rows),
    }
    validate_report(report, lock)
    return report


def load_report(path: Path, lock: InputLock) -> dict[str, object]:
    """Load and validate a report against its exact generated-input lock."""

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"cannot load component evidence: {error}") from error
    if not isinstance(payload, dict):
        raise EvidenceError("component evidence root must be an object")
    report = cast(dict[str, object], payload)
    validate_report(report, lock)
    return report


def validate_report(payload: Mapping[str, object], lock: InputLock) -> None:
    """Fail closed on missing, unknown, stale, or internally inconsistent evidence."""

    if not isinstance(lock, InputLock):
        raise TypeError("lock must be InputLock")
    try:
        verify_input_lock(lock)
    except InputLockError as error:
        raise EvidenceError(f"input lock verification failed: {error}") from error
    _exact_fields(payload, _ROOT_FIELDS, "report")
    if payload["schema"] != REPORT_SCHEMA:
        raise EvidenceError("unsupported component evidence schema")
    profile = _string(payload["profile"], "profile")
    expected_cases = lock.for_profile(profile)
    input_lock = _mapping(payload["input_lock"], "input_lock")
    _exact_fields(input_lock, _LOCK_FIELDS, "input_lock")
    expected_lock = {
        "schema": lock.schema,
        "generator": lock.generator,
        "sha256": lock.sha256,
    }
    if dict(input_lock) != expected_lock:
        raise EvidenceError("report input-lock identity differs from selected lock")
    contract = _mapping(payload["contract"], "contract")
    _exact_fields(contract, _CONTRACT_FIELDS, "contract")
    api_version = contract["api_version"]
    if not isinstance(api_version, list) or len(api_version) != 2:
        raise EvidenceError("contract.api_version must be a two-integer array")
    for index, value in enumerate(api_version):
        _nonnegative_integer(value, f"contract.api_version[{index}]")
    _positive_integer(contract["model_schema"], "contract.model_schema")
    _string(contract["backend"], "contract.backend")
    _string(contract["format"], "contract.format")
    if dict(contract) != {
        "api_version": list(API_VERSION),
        "model_schema": MODEL_SCHEMA_VERSION,
        "backend": "python",
        "format": "functional",
    }:
        raise EvidenceError("report runtime contract differs from the active schema")
    if payload["status"] != "pass":
        raise EvidenceError("component evidence status must be pass")

    rows_value = payload["cases"]
    if not isinstance(rows_value, list):
        raise EvidenceError("report cases must be an array")
    rows = tuple(_mapping(row, f"cases[{index}]") for index, row in enumerate(rows_value))
    expected_ids = tuple(case.id for case in expected_cases)
    observed_ids = tuple(_string(row.get("id"), "case id") for row in rows)
    if observed_ids != expected_ids:
        raise EvidenceError("report cases differ from the selected profile or lock order")
    for row, case in zip(rows, expected_cases, strict=True):
        _validate_case(row, case)

    claims = _mapping(payload["claims"], "claims")
    _exact_fields(claims, _CLAIM_FIELDS, "claims")
    for field in (
        "input_hashes_verified",
        "fixed_component_work_is_additive",
        "oversized_component_failed_closed",
        "bounded_component_metrics_present",
    ):
        _boolean(claims[field], f"claims.{field}")
    work_per_component = claims["fixed_component_work_per_component"]
    if work_per_component is not None:
        _positive_integer(
            work_per_component,
            "claims.fixed_component_work_per_component",
        )
    if claims["performance_claim"] is not None:
        raise EvidenceError("claims.performance_claim must remain null")
    expected_claims = _derive_claims(rows)
    if dict(claims) != expected_claims:
        raise EvidenceError("report claims are not derivable from validated case evidence")


def _run_case(case: InputCase) -> dict[str, object]:
    source = source_for_case(case)
    source_digest = hashlib.sha256(source).hexdigest()
    if len(source) != case.source_bytes or source_digest != case.source_sha256:
        raise EvidenceError(f"{case.id}: generated input differs from the lock")
    input_row = {
        "bytes": len(source),
        "sha256": source_digest,
        "component_count": case.component_count,
        "labels_per_component": case.labels_per_component,
        "max_canonical_work": case.max_canonical_work,
    }
    if case.kind == "fixed-components":
        roots = _fixed_component_roots(case.component_count)
        limits = ParseLimits(max_canonical_work=case.max_canonical_work)
        components, partition = document_impl._partition_blank_graph(roots, limits=limits)
        _, work = document_impl._canonicalize_blank_components(
            components,
            partition,
            limits=limits,
        )
        document = parse_document(
            source,
            format="functional",
            options=LoadOptions(
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.PYTHON,
                limits=limits,
            ),
        )
        anonymous = {
            value
            for axiom in document.axioms
            for value in model.walk(axiom)
            if isinstance(value, model.AnonymousIndividual)
        }
        return {
            "id": case.id,
            "kind": case.kind,
            "status": "pass",
            "input": input_row,
            "shape": _shape_row(partition),
            "work": _work_row(work),
            "error": None,
            "output": {
                "axiom_count": len(document.axioms),
                "anonymous_individual_count": len(anonymous),
                "document_fingerprint_schema": document.document_fingerprint.schema,
                "document_fingerprint_sha256": document.document_fingerprint.hex,
            },
        }

    roots = _oversized_component_roots(case.labels_per_component)
    limits = ParseLimits(max_canonical_work=case.max_canonical_work)
    components, partition = document_impl._partition_blank_graph(roots, limits=limits)
    direct_error = _capture_limit(
        lambda: document_impl._canonicalize_blank_components(
            components,
            partition,
            limits=limits,
        )
    )
    public_error = _capture_limit(
        lambda: parse_document(
            source,
            format="functional",
            options=LoadOptions(
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.PYTHON,
                limits=limits,
            ),
        )
    )
    if _error_row(direct_error) != _error_row(public_error):
        raise EvidenceError(f"{case.id}: direct and Functional error fields differ")
    return {
        "id": case.id,
        "kind": case.kind,
        "status": "pass",
        "input": input_row,
        "shape": _shape_row(partition),
        "work": None,
        "error": _error_row(public_error),
        "output": None,
    }


def _fixed_component_roots(count: int) -> tuple[model.AxiomNode, ...]:
    return tuple(
        model.ClassAssertion(
            _COMPONENT_CLASS,
            document_impl.provisional_anonymous(f"component{index:08d}"),
        )
        for index in range(count)
    )


def _oversized_component_roots(labels: int) -> tuple[model.AxiomNode, ...]:
    individuals = model.CanonicalSet[model.Individual](
        document_impl.provisional_anonymous(f"member{index:08d}") for index in range(labels)
    )
    return (model.SameIndividual(individuals),)


def _capture_limit(operation: Callable[[], object]) -> ResourceLimitError:
    try:
        operation()
    except ResourceLimitError as error:
        return error
    raise EvidenceError("expected max_canonical_work failure did not occur")


def _shape_row(partition: Any) -> dict[str, int]:
    return {
        "component_count": partition.component_count,
        "largest_component_labels": partition.largest_component_labels,
        "largest_component_arcs": partition.largest_component_arcs,
        "largest_component_roots": partition.largest_component_roots,
        "maximum_root_interval_span": partition.maximum_root_interval_span,
        "maximum_open_root_intervals": partition.maximum_open_root_intervals,
        "total_labels": partition.total_labels,
        "total_arcs": partition.total_arcs,
    }


def _work_row(work: Any) -> dict[str, int]:
    return {
        "component_record_count": len(work.components),
        "total_setup_work": work.total_setup_work,
        "total_refinement_work": work.total_refinement_work,
        "total_candidate_order_work": work.total_candidate_order_work,
        "total_canonical_work": work.total_canonical_work,
        "largest_component_work": work.largest_component_work,
        "maximum_refinement_rounds": work.maximum_refinement_rounds,
        "total_permutations_examined": work.total_permutations_examined,
    }


def _error_row(error: ResourceLimitError) -> dict[str, object]:
    return {
        "type": type(error).__name__,
        "code": error.code,
        "limit": error.limit,
        "observed": error.observed,
        "allowed": error.allowed,
        "details": dict(error.details),
    }


def _validate_case(row: Mapping[str, object], case: InputCase) -> None:
    _exact_fields(row, _CASE_FIELDS, case.id)
    if row["id"] != case.id or row["kind"] != case.kind or row["status"] != "pass":
        raise EvidenceError(f"{case.id}: identity/kind/status differs from input lock")
    input_row = _mapping(row["input"], f"{case.id}.input")
    _exact_fields(input_row, _INPUT_FIELDS, f"{case.id}.input")
    for field in (
        "bytes",
        "component_count",
        "labels_per_component",
        "max_canonical_work",
    ):
        _positive_integer(input_row[field], f"{case.id}.input.{field}")
    if dict(input_row) != {
        "bytes": case.source_bytes,
        "sha256": case.source_sha256,
        "component_count": case.component_count,
        "labels_per_component": case.labels_per_component,
        "max_canonical_work": case.max_canonical_work,
    }:
        raise EvidenceError(f"{case.id}: input evidence differs from lock")
    shape = _mapping(row["shape"], f"{case.id}.shape")
    _exact_fields(shape, _SHAPE_FIELDS, f"{case.id}.shape")
    for field in _SHAPE_FIELDS:
        _positive_integer(shape[field], f"{case.id}.shape.{field}")
    expected_shape = _expected_shape(case)
    if dict(shape) != expected_shape:
        raise EvidenceError(f"{case.id}: component shape differs from generated fixture")

    if case.kind == "fixed-components":
        if row["error"] is not None:
            raise EvidenceError(f"{case.id}: successful scaling case contains an error")
        work = _mapping(row["work"], f"{case.id}.work")
        _exact_fields(work, _WORK_FIELDS, f"{case.id}.work")
        for field in _WORK_FIELDS:
            _positive_integer(work[field], f"{case.id}.work.{field}")
        if dict(work) != _expected_work(case):
            raise EvidenceError(f"{case.id}: charged work is not fixed-size additive")
        output = _mapping(row["output"], f"{case.id}.output")
        _exact_fields(output, _OUTPUT_FIELDS, f"{case.id}.output")
        _positive_integer(output["axiom_count"], f"{case.id}.output.axiom_count")
        _positive_integer(
            output["anonymous_individual_count"],
            f"{case.id}.output.anonymous_individual_count",
        )
        _positive_integer(
            output["document_fingerprint_schema"],
            f"{case.id}.output.document_fingerprint_schema",
        )
        if output["axiom_count"] != case.component_count + 1:
            raise EvidenceError(f"{case.id}: Functional axiom count differs")
        if output["anonymous_individual_count"] != case.component_count:
            raise EvidenceError(f"{case.id}: anonymous individuals collapsed")
        if output["document_fingerprint_schema"] != 2:
            raise EvidenceError(f"{case.id}: fingerprint is not model schema 2")
        digest = output["document_fingerprint_sha256"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise EvidenceError(f"{case.id}: fingerprint must be lowercase SHA-256")
        return

    if row["work"] is not None or row["output"] is not None:
        raise EvidenceError(f"{case.id}: failed case cannot publish work/output")
    error = _mapping(row["error"], f"{case.id}.error")
    _exact_fields(error, _ERROR_FIELDS, f"{case.id}.error")
    arcs = (
        case.labels_per_component + case.labels_per_component * (case.labels_per_component - 1) // 2
    )
    setup_work = case.labels_per_component + 2 * arcs
    expected_error = {
        "type": "ResourceLimitError",
        "code": "RESOURCE_LIMIT",
        "limit": "max_canonical_work",
        "observed": setup_work,
        "allowed": case.max_canonical_work,
        "details": {
            "component_count": 1,
            "largest_component_labels": case.labels_per_component,
            "largest_component_arcs": arcs,
            "refinement_rounds": 0,
            "work_term": "setup",
        },
    }
    details = _mapping(error.get("details"), f"{case.id}.error.details")
    _exact_fields(details, _ERROR_DETAIL_FIELDS, f"{case.id}.error.details")
    _positive_integer(error.get("observed"), f"{case.id}.error.observed")
    _positive_integer(error.get("allowed"), f"{case.id}.error.allowed")
    for field in (
        "component_count",
        "largest_component_labels",
        "largest_component_arcs",
    ):
        _positive_integer(details[field], f"{case.id}.error.details.{field}")
    _nonnegative_integer(
        details["refinement_rounds"],
        f"{case.id}.error.details.refinement_rounds",
    )
    if dict(error) != expected_error:
        raise EvidenceError(f"{case.id}: typed WP19 limit evidence differs")


def _expected_shape(case: InputCase) -> dict[str, int]:
    if case.kind == "fixed-components":
        return {
            "component_count": case.component_count,
            "largest_component_labels": 1,
            "largest_component_arcs": 1,
            "largest_component_roots": 1,
            "maximum_root_interval_span": 1,
            "maximum_open_root_intervals": 1,
            "total_labels": case.component_count,
            "total_arcs": case.component_count,
        }
    labels = case.labels_per_component
    arcs = labels + labels * (labels - 1) // 2
    return {
        "component_count": 1,
        "largest_component_labels": labels,
        "largest_component_arcs": arcs,
        "largest_component_roots": 1,
        "maximum_root_interval_span": 1,
        "maximum_open_root_intervals": 1,
        "total_labels": labels,
        "total_arcs": arcs,
    }


def _expected_work(case: InputCase) -> dict[str, int]:
    count = case.component_count
    return {
        "component_record_count": count,
        "total_setup_work": 3 * count,
        "total_refinement_work": 4 * count,
        "total_candidate_order_work": 2 * count,
        "total_canonical_work": 9 * count,
        "largest_component_work": 9,
        "maximum_refinement_rounds": 1,
        "total_permutations_examined": count,
    }


def _derive_claims(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    fixed = [row for row in rows if row.get("kind") == "fixed-components"]
    oversized = [row for row in rows if row.get("kind") == "oversized-component"]
    work_per_component: set[int] = set()
    for row in fixed:
        input_row = _mapping(row.get("input"), "claim input")
        work = _mapping(row.get("work"), "claim work")
        count = _positive_integer(input_row.get("component_count"), "claim component_count")
        total = _positive_integer(work.get("total_canonical_work"), "claim total work")
        if total % count:
            raise EvidenceError("fixed-component total work is not divisible by component count")
        work_per_component.add(total // count)
    additive = len(fixed) >= 2 and work_per_component == {9}
    oversized_closed = bool(oversized) and all(row.get("error") is not None for row in oversized)
    bounded_metrics = bool(rows) and all(
        isinstance(row.get("shape"), Mapping)
        and set(cast(Mapping[str, object], row["shape"])) == _SHAPE_FIELDS
        for row in rows
    )
    return {
        "input_hashes_verified": True,
        "fixed_component_work_is_additive": additive,
        "fixed_component_work_per_component": 9 if additive else None,
        "oversized_component_failed_closed": oversized_closed,
        "bounded_component_metrics_present": bounded_metrics,
        "performance_claim": None,
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        raise EvidenceError(
            f"{label} fields differ: missing={sorted(expected - observed)!r}, "
            f"unknown={sorted(observed - expected)!r}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{label} must be a nonempty string")
    return value


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvidenceError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceError(f"{label} must be a nonnegative integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"{label} must be a boolean")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_INPUT_LOCK)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="run and atomically publish evidence")
    generate.add_argument("--profile", choices=("smoke", "release"), default="smoke")
    generate.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check", help="validate existing evidence without rerunning")
    check.add_argument("report", type=Path)
    arguments = parser.parse_args(argv)
    try:
        lock = load_input_lock(arguments.lock)
        verify_input_lock(lock)
        if arguments.command == "generate":
            report = generate_report(lock, profile=arguments.profile)
            digest = write_json(arguments.output, report)
            print(f"component evidence written: sha256={digest}")
        else:
            report = load_report(arguments.report, lock)
            digest = hashlib.sha256(arguments.report.read_bytes()).hexdigest()
            print(f"component evidence OK: profile={report['profile']}, sha256={digest}")
        return 0
    except (EvidenceError, InputLockError, OSError) as error:
        print(f"component evidence error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through package entry point
    raise SystemExit(main())


__all__ = [
    "REPORT_SCHEMA",
    "EvidenceError",
    "generate_report",
    "load_report",
    "main",
    "validate_report",
]
