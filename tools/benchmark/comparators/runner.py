"""Offline WP14 comparator orchestration and correctness qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from tools.benchmark.manifest import (
    DEFAULT_MANIFEST,
    Corpus,
    generated_bytes,
    load_manifest,
    verify_prepared,
)
from tools.benchmark.report import collect_environment, write_json

from .adapters import (
    ADAPTER_RESULT_SCHEMA,
    DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    MAX_SUBPROCESS_STDERR_BYTES,
    MAX_SUBPROCESS_STDOUT_BYTES,
    AdapterRequest,
    adapter_status_result,
    default_options,
    options_digest,
    run_bounded_subprocess,
    run_core_adapter,
    run_external_adapter,
    sanitize_failure,
)
from .common_contract import common_contract_equality_key
from .manifest import (
    COMMON_BOUNDARY,
    DEFAULT_COMPARATOR_MANIFEST,
    ROOT,
    ComparatorManifest,
    ComparatorPin,
    load_comparator_manifest,
)

REPORT_SCHEMA = "pyowl-core/comparator-baseline/v1"
SOURCE_IDENTITY_SCHEMA = "pyowl-core/comparator-runtime-source/v1"
SOURCE_IDENTITY_DOMAIN = b"pyowl-core:comparator-runtime-source:v1\x00"
_REQUIRED_PROCESS_MODES = ("fresh-process", "steady-process")
_REQUIRED_INPUT_MODES = ("resident-bytes", "file")
_REQUIRED_CORPUS_TIERS = ("medium", "large")
_FRESH_CORE_METRICS = (
    "wall_ns",
    "cpu_ns",
    "load_ns",
    "common_adapter_ns",
    "rss_peak_before_bytes",
    "rss_peak_after_bytes",
    "rss_peak_increment_bytes",
)
_SOURCE_FILES = (
    "pyproject.toml",
    "setup.py",
    "pyowl_build.py",
    "native/Cargo.toml",
    "native/Cargo.lock",
    "native/build.rs",
)
_SOURCE_TREES = (
    ("src/pyowl_core", frozenset({".py", ".pyi", ".typed"})),
    ("native/src", frozenset({".rs"})),
    ("tools/benchmark", frozenset({".py"})),
)


class ComparatorRunError(RuntimeError):
    """The benchmark request itself is invalid; lane failures stay in the report."""


def check_comparator_contract(
    *,
    comparator_manifest_path: Path = DEFAULT_COMPARATOR_MANIFEST,
    corpus_manifest_path: Path = DEFAULT_MANIFEST,
) -> ComparatorManifest:
    """Validate pins, phase fences, and the exact corpus-manifest linkage."""

    manifest = load_comparator_manifest(comparator_manifest_path)
    observed = hashlib.sha256(corpus_manifest_path.read_bytes()).hexdigest()
    if observed != manifest.corpus_manifest_sha256:
        raise ComparatorRunError(
            "comparator pin ledger references a different corpus manifest SHA-256"
        )
    return manifest


def run_comparator_baseline(
    *,
    comparator_manifest_path: Path = DEFAULT_COMPARATOR_MANIFEST,
    corpus_manifest_path: Path = DEFAULT_MANIFEST,
    cache_dir: Path | None = None,
    corpus_ids: Sequence[str] = ("generated-tiny-functional",),
    comparator_ids: Sequence[str] = ("pyowl-python-common",),
    process_modes: Sequence[str] = ("steady-process",),
    input_modes: Sequence[str] = ("resident-bytes",),
    warmups: int = 1,
    repetitions: int = 5,
) -> dict[str, Any]:
    """Run a correctness-qualified raw-sample baseline without network access."""

    if warmups < 0 or repetitions < 1:
        raise ComparatorRunError("warmups must be nonnegative and repetitions positive")
    _require_unique_nonempty(corpus_ids, "corpus_ids")
    _require_unique_nonempty(comparator_ids, "comparator_ids")
    _require_unique_nonempty(process_modes, "process_modes")
    _require_unique_nonempty(input_modes, "input_modes")
    if not process_modes or any(
        value not in {"steady-process", "fresh-process"} for value in process_modes
    ):
        raise ComparatorRunError("process_modes contain unsupported values")
    if not input_modes or any(value not in {"resident-bytes", "file"} for value in input_modes):
        raise ComparatorRunError("input_modes contain unsupported values")
    comparator_manifest = check_comparator_contract(
        comparator_manifest_path=comparator_manifest_path,
        corpus_manifest_path=corpus_manifest_path,
    )
    corpus_manifest = load_manifest(corpus_manifest_path)
    source_identity = comparator_source_identity(
        comparator_manifest_path=comparator_manifest_path,
        corpus_manifest_path=corpus_manifest_path,
    )
    corpora = tuple(corpus_manifest.by_id(value) for value in corpus_ids)
    pins = tuple(comparator_manifest.by_id(value) for value in comparator_ids)
    resolved_cache = cache_dir or ROOT / "benchmarks" / "results" / "corpora"
    sources = {value.id: _source(value, resolved_cache) for value in corpora}

    rows: list[dict[str, Any]] = []
    for corpus in corpora:
        source = sources[corpus.id]
        options = default_options(corpus.format)
        options_sha256 = options_digest(options)
        for input_mode in input_modes:
            for process_mode in process_modes:
                request = AdapterRequest(
                    corpus_id=corpus.id,
                    source=source,
                    source_sha256=corpus.sha256,
                    format=corpus.format,
                    options=options,
                    options_sha256=options_sha256,
                    input_mode=input_mode,
                    process_mode=process_mode,
                )
                for pin in pins:
                    if process_mode == "steady-process":
                        for _ in range(warmups):
                            _run_once(pin, request)
                    samples = [_run_once(pin, request) for _ in range(repetitions)]
                    rows.append(_aggregate_samples(pin, request, samples))

    assertions = _equality_assertions(rows)
    required_lanes = {value.id for value in pins if value.required}
    completed_required = {
        cast(str, value["lane"])
        for value in rows
        if value["status"] == "ok" and value["lane"] in required_lanes
    }
    environment = collect_environment(ROOT)
    if (
        comparator_source_identity(
            comparator_manifest_path=comparator_manifest_path,
            corpus_manifest_path=corpus_manifest_path,
        )["sha256"]
        != source_identity["sha256"]
    ):
        raise ComparatorRunError("runtime source inputs changed during the comparator run")
    machine_evidence = _reference_machine_evidence(comparator_manifest, environment)
    completion = _completion_requirements(
        comparator_manifest=comparator_manifest,
        corpora=corpora,
        rows=rows,
        assertions=assertions,
        process_modes=process_modes,
        input_modes=input_modes,
        reference_machine_matches=cast(bool, machine_evidence["matches"]),
    )
    execution_errors = [
        {
            "lane": value["lane"],
            "corpus_id": value["corpus_id"],
            "input_mode": value["input_mode"],
            "process_mode": value["process_mode"],
            "reason": sanitize_failure(value.get("reason")),
        }
        for value in rows
        if value.get("status") == "error"
    ]
    assertions_pass = bool(assertions) and all(value.get("passed") is True for value in assertions)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "comparator_manifest_sha256": hashlib.sha256(
            comparator_manifest_path.read_bytes()
        ).hexdigest(),
        "corpus_manifest_sha256": comparator_manifest.corpus_manifest_sha256,
        "source_identity": source_identity,
        "reference_machine": {
            "id": comparator_manifest.reference_machine.id,
            "approval": comparator_manifest.reference_machine.approval,
            "environment_evidence": machine_evidence,
        },
        "environment": environment,
        "methodology": {
            "warmups": warmups,
            "repetitions": repetitions,
            "input_modes": list(input_modes),
            "process_modes": list(process_modes),
            "network_during_samples": (
                "core lanes are offline; external process isolation is not implemented"
            ),
            "comparison_order": (
                "deterministic caller order; paired randomization is not implemented"
            ),
            "file_lane_execution": (
                "pinned bytes are hash-checked and prepared before timing; the timer includes "
                "the implementation's file open/read and records temporary bytes"
            ),
            "post_timer_work": "already-published scalar/digest equality only",
            "profiler_attached": False,
        },
        "corpora": [
            {
                "id": value.id,
                "tier": value.tier,
                "format": value.format.value,
                "sha256": value.sha256,
                "bytes": value.counts.bytes,
            }
            for value in corpora
        ],
        "lanes": rows,
        "equality_assertions": assertions,
        "execution_errors": execution_errors,
        "contract_valid": assertions_pass and not execution_errors,
        "completion_requirements": completion,
        "comparative_complete": completion["passed"],
        "not_run_required": sorted(required_lanes - completed_required),
    }
    return report


def _run_once(pin: ComparatorPin, request: AdapterRequest) -> dict[str, Any]:
    if pin.adapter in {"core-python", "core-native"}:
        if request.process_mode == "fresh-process":
            return _run_fresh_core(pin, request)
        return run_core_adapter(pin, request)
    return run_external_adapter(pin, request)


def _run_fresh_core(pin: ComparatorPin, request: AdapterRequest) -> dict[str, Any]:
    body = _canonical_json(request.protocol_dict(pin))
    start = time.perf_counter_ns()
    try:
        completed = run_bounded_subprocess(
            (sys.executable, "-m", "tools.benchmark.comparators.worker"),
            body,
            timeout=DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_SUBPROCESS_STDOUT_BYTES,
            max_stderr_bytes=MAX_SUBPROCESS_STDERR_BYTES,
            cwd=ROOT,
        )
    except (OSError, TypeError, ValueError) as error:
        return adapter_status_result(
            pin,
            request,
            "error",
            f"isolated worker could not start: {type(error).__name__}: {error}",
        )
    startup_to_ready_ns = time.perf_counter_ns() - start
    if completed.timed_out:
        return adapter_status_result(
            pin,
            request,
            "error",
            "isolated worker exceeded its explicit wall-time limit",
        )
    if completed.output_limit is not None:
        return adapter_status_result(
            pin,
            request,
            "error",
            f"isolated worker exceeded its {completed.output_limit} output limit",
        )
    if completed.returncode != 0:
        reason = sanitize_failure(completed.stderr.decode("utf-8", "replace"))
        return adapter_status_result(
            pin,
            request,
            "error",
            f"isolated worker exited {completed.returncode}: {reason}",
        )
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return adapter_status_result(
            pin,
            request,
            "error",
            f"isolated worker output is invalid: {type(error).__name__}: {error}",
        )
    if not isinstance(decoded, dict):
        return adapter_status_result(
            pin,
            request,
            "error",
            "isolated worker output must be a JSON object",
        )
    result = cast(dict[str, Any], decoded)
    try:
        _validate_fresh_core_result(pin, request, result)
    except (TypeError, ValueError) as error:
        return adapter_status_result(
            pin,
            request,
            "error",
            f"isolated worker result is invalid: {type(error).__name__}: {error}",
        )
    if result["status"] != "ok":
        result["reason"] = sanitize_failure(result.get("reason"))
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        metrics["startup_to_ready_ns"] = startup_to_ready_ns
    result["transport_metrics"] = {
        "request_bytes": len(body),
        "stdout_bytes": len(completed.stdout),
        "stderr_bytes": len(completed.stderr),
    }
    return result


def _validate_fresh_core_result(
    pin: ComparatorPin,
    request: AdapterRequest,
    value: Mapping[str, Any],
) -> None:
    for name, expected in (
        ("schema", ADAPTER_RESULT_SCHEMA),
        ("lane", pin.id),
        ("implementation", pin.implementation),
        ("boundary", pin.boundary),
        ("corpus_id", request.corpus_id),
        ("source_sha256", request.source_sha256),
        ("options_sha256", request.options_sha256),
        ("input_mode", request.input_mode),
        ("process_mode", request.process_mode),
    ):
        if value.get(name) != expected:
            raise ValueError(f"fresh-process result {name} differs from request/pin")
    status = value.get("status")
    if status not in {"ok", "not-run", "ineligible", "error"}:
        raise ValueError("fresh-process result has invalid status")
    artifact = value.get("artifact")
    if not isinstance(artifact, Mapping):
        raise TypeError("fresh-process result lacks artifact evidence")
    expected_artifact: tuple[tuple[str, object], ...] = (
        ("pin_state", pin.pin_state),
        ("version", pin.version),
        ("revision", pin.revision),
        ("artifact", pin.artifact),
        ("features", list(pin.features)),
        ("allocator", pin.allocator),
        ("thread_ceiling", pin.thread_ceiling),
        ("runner_revision", pin.runner_revision),
        ("runner_sha256", pin.runner_sha256),
    )
    for name, expected_value in expected_artifact:
        if artifact.get(name) != expected_value:
            raise ValueError(f"fresh-process artifact {name} differs from expected pin")
    if status != "ok":
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            raise ValueError("non-success fresh-process result requires a reason")
        return
    if value.get("reason") is not None:
        raise ValueError("successful fresh-process result must not contain a reason")
    metrics = value.get("metrics")
    if not isinstance(metrics, Mapping):
        raise TypeError("successful fresh-process result lacks metrics")
    numeric = {
        name: _nonnegative_integer(metrics.get(name), f"metrics.{name}")
        for name in _FRESH_CORE_METRICS
    }
    if numeric["load_ns"] + numeric["common_adapter_ns"] > numeric["wall_ns"]:
        raise ValueError("fresh-process phases exceed the timed wall envelope")
    before = numeric["rss_peak_before_bytes"]
    after = numeric["rss_peak_after_bytes"]
    increment = numeric["rss_peak_increment_bytes"]
    if after < before or increment != after - before:
        raise ValueError("fresh-process RSS evidence is internally inconsistent")
    contract = value.get("contract")
    if not isinstance(contract, Mapping):
        raise TypeError("successful fresh-process result lacks a common contract")
    attestation = value.get("timed_validation")
    if not isinstance(attestation, Mapping):
        raise TypeError("fresh-process result lacks timed validation attestation")
    if attestation.get("schema") != "pyowl-core/comparator-timed-validation/v1":
        raise ValueError("fresh-process timed validation schema differs")
    if (
        attestation.get("inside_timed_envelope") is not True
        or attestation.get("full_contract_validation") is not True
        or attestation.get("contract_sha256") != contract.get("contract_sha256")
    ):
        raise ValueError("fresh-process contract was not fully validated inside its timer")
    _nonnegative_integer(
        attestation.get("validation_ns"),
        "timed_validation.validation_ns",
    )
    common_contract_equality_key(cast(Mapping[str, Any], contract))
    artifact_sha256 = artifact.get("artifact_sha256")
    if pin.adapter == "core-native" and not _is_sha256(artifact_sha256):
        raise ValueError("installed native-wheel result lacks its artifact SHA-256")


def _aggregate_samples(
    pin: ComparatorPin,
    request: AdapterRequest,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    reason: str | None
    statuses = {cast(str, value["status"]) for value in samples}
    if len(statuses) != 1:
        status = "error"
        reason = "sample statuses changed within one lane"
    else:
        status = next(iter(statuses))
        reasons = {cast(str | None, value.get("reason")) for value in samples}
        reason = next(iter(reasons)) if len(reasons) == 1 else "sample reasons changed"
    contracts = [
        cast(Mapping[str, Any], value["contract"])
        for value in samples
        if isinstance(value.get("contract"), Mapping)
    ]
    if status == "ok" and pin.boundary == COMMON_BOUNDARY:
        if len(contracts) != len(samples):
            status = "error"
            reason = "common-ready sample lacks contract"
        else:
            keys = {common_contract_equality_key(value) for value in contracts}
            if len(keys) != 1:
                status = "error"
                reason = "common contract changed across repetitions"
    return {
        "lane": pin.id,
        "implementation": pin.implementation,
        "boundary": pin.boundary,
        "gating": pin.gating,
        "required": pin.required,
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
        "status": status,
        "reason": reason,
        "samples": [_compact_sample(value) for value in samples],
        "contract": contracts[0] if contracts and status == "ok" else None,
    }


def _compact_sample(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep raw metrics while publishing an ontology-sized contract only once."""

    result = dict(value)
    contract = result.pop("contract", None)
    if isinstance(contract, Mapping):
        result["contract_sha256"] = contract.get("contract_sha256")
    return result


def _equality_assertions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    input_modes: dict[tuple[str, str, str], dict[str, Mapping[str, Any]]] = {}
    for value in rows:
        if value.get("boundary") != COMMON_BOUNDARY or value.get("status") != "ok":
            continue
        key = (
            cast(str, value["corpus_id"]),
            cast(str, value["input_mode"]),
            cast(str, value["process_mode"]),
        )
        grouped.setdefault(key, []).append(value)
        mode_key = (
            cast(str, value["corpus_id"]),
            cast(str, value["process_mode"]),
            cast(str, value["lane"]),
        )
        input_modes.setdefault(mode_key, {})[cast(str, value["input_mode"])] = value
    assertions: list[dict[str, object]] = []
    for key, values in sorted(grouped.items()):
        reference = next(
            (value for value in values if value["lane"] == "pyowl-python-common"),
            None,
        )
        if reference is None:
            assertions.append(
                {
                    "id": "/".join(key) + "/common-contract-reference",
                    "passed": False,
                    "reason": "Python common-contract reference was not run",
                }
            )
            continue
        reference_contract = cast(Mapping[str, Any], reference["contract"])
        reference_key = common_contract_equality_key(reference_contract)
        for value in values:
            candidate = cast(Mapping[str, Any], value["contract"])
            passed = common_contract_equality_key(candidate) == reference_key
            assertions.append(
                {
                    "id": "/".join(key) + f"/{value['lane']}/common-contract-equality",
                    "passed": passed,
                    "reason": None if passed else "published output inventory/digests differ",
                }
            )
    for key, mode_rows in sorted(input_modes.items()):
        resident = mode_rows.get("resident-bytes")
        file_row = mode_rows.get("file")
        if resident is None or file_row is None:
            continue
        resident_contract = cast(Mapping[str, Any], resident["contract"])
        file_contract = cast(Mapping[str, Any], file_row["contract"])
        passed = common_contract_equality_key(resident_contract) == common_contract_equality_key(
            file_contract
        )
        assertions.append(
            {
                "id": "/".join(key) + "/resident-file-common-contract-equality",
                "passed": passed,
                "reason": None if passed else "resident-byte and file inventories/digests differ",
            }
        )
    if not assertions:
        assertions.append(
            {
                "id": "common-contract-equality",
                "passed": False,
                "reason": "no common-ready Python reference result was available",
            }
        )
    return assertions


def _completion_requirements(
    *,
    comparator_manifest: ComparatorManifest,
    corpora: Sequence[Corpus],
    rows: Sequence[Mapping[str, Any]],
    assertions: Sequence[Mapping[str, Any]],
    process_modes: Sequence[str],
    input_modes: Sequence[str],
    reference_machine_matches: bool,
) -> dict[str, Any]:
    """Evaluate the complete release matrix; partial smoke runs fail closed."""

    required_pins = tuple(value for value in comparator_manifest.comparators if value.required)
    required_pin_ids = tuple(value.id for value in required_pins)
    medium_corpora = tuple(
        value.id
        for value in corpora
        if value.tier == "medium" and "synthetic" not in value.families
    )
    large_corpora = tuple(
        value.id
        for value in corpora
        if value.tier == "large"
        and value.format.value == "rdfxml"
        and "biomedical" in value.families
    )
    representative_corpora = medium_corpora + large_corpora
    annotation_corpora = tuple(
        value.id
        for value in corpora
        if value.id in representative_corpora and "annotation-list-heavy" in value.families
    )
    rows_by_scenario = {
        (
            cast(str, value.get("lane")),
            cast(str, value.get("corpus_id")),
            cast(str, value.get("input_mode")),
            cast(str, value.get("process_mode")),
        ): value
        for value in rows
    }
    failures: list[dict[str, str]] = []
    successful_scenarios = 0
    for corpus_id in representative_corpora:
        for input_mode in _REQUIRED_INPUT_MODES:
            for process_mode in _REQUIRED_PROCESS_MODES:
                for pin_id in required_pin_ids:
                    key = (pin_id, corpus_id, input_mode, process_mode)
                    row = rows_by_scenario.get(key)
                    if row is not None and row.get("status") == "ok":
                        successful_scenarios += 1
                        continue
                    failures.append(
                        {
                            "lane": pin_id,
                            "corpus_id": corpus_id,
                            "input_mode": input_mode,
                            "process_mode": process_mode,
                            "status": ("missing" if row is None else cast(str, row.get("status"))),
                            "reason": (
                                "required scenario was not requested"
                                if row is None
                                else sanitize_failure(row.get("reason"))
                            ),
                        }
                    )

    common_pins = tuple(value for value in required_pins if value.boundary == COMMON_BOUNDARY)
    contracts_match = bool(medium_corpora and large_corpora and annotation_corpora)
    for corpus_id in representative_corpora:
        for input_mode in _REQUIRED_INPUT_MODES:
            for process_mode in _REQUIRED_PROCESS_MODES:
                reference = rows_by_scenario.get(
                    ("pyowl-python-common", corpus_id, input_mode, process_mode)
                )
                if reference is None or reference.get("status") != "ok":
                    contracts_match = False
                    continue
                reference_contract = reference.get("contract")
                if not isinstance(reference_contract, Mapping):
                    contracts_match = False
                    continue
                try:
                    reference_key = common_contract_equality_key(reference_contract)
                    for pin in common_pins:
                        candidate = rows_by_scenario.get(
                            (pin.id, corpus_id, input_mode, process_mode)
                        )
                        if candidate is None or candidate.get("status") != "ok":
                            contracts_match = False
                            continue
                        candidate_contract = candidate.get("contract")
                        if not isinstance(candidate_contract, Mapping) or (
                            common_contract_equality_key(candidate_contract) != reference_key
                        ):
                            contracts_match = False
                except (TypeError, ValueError):
                    contracts_match = False

    required_modes_requested = set(process_modes) == set(_REQUIRED_PROCESS_MODES) and set(
        input_modes
    ) == set(_REQUIRED_INPUT_MODES)
    selected_lane_ids = {cast(str, value.get("lane")) for value in rows}
    missing_required_pins = sorted(set(required_pin_ids) - selected_lane_ids)
    assertions_pass = bool(assertions) and all(value.get("passed") is True for value in assertions)
    machine_approved = (
        comparator_manifest.reference_machine.approval == "approved" and reference_machine_matches
    )
    file_lane_implemented = True
    paired_randomization_implemented = False
    ratio_gates_configured = False
    ratio_gates_passed = False
    all_scenarios_succeeded = bool(representative_corpora) and not failures

    reasons: list[str] = []
    if not medium_corpora:
        reasons.append("no non-synthetic representative medium corpus was selected")
    if not large_corpora:
        reasons.append("no representative large biomedical RDF/XML corpus was selected")
    if not annotation_corpora:
        reasons.append("no annotation/list-heavy medium-or-larger corpus was selected")
    if not required_modes_requested:
        reasons.append("fresh/steady and resident/file modes were not all requested")
    if missing_required_pins:
        reasons.append("not every required comparator pin was selected")
    if failures:
        reasons.append("one or more Cartesian required scenarios did not succeed")
    if not contracts_match or not assertions_pass:
        reasons.append("the complete required common-contract matrix did not match")
    if not machine_approved:
        reasons.append("the reference machine is not approved and matched to captured evidence")
    if not file_lane_implemented:
        reasons.append("file-lane execution is not implemented")
    if not paired_randomization_implemented:
        reasons.append("paired implementation-order randomization is not implemented")
    if not ratio_gates_configured or not ratio_gates_passed:
        reasons.append("executable comparative ratio gates are not configured and passing")

    passed = all(
        (
            medium_corpora,
            large_corpora,
            annotation_corpora,
            required_modes_requested,
            not missing_required_pins,
            all_scenarios_succeeded,
            contracts_match,
            assertions_pass,
            machine_approved,
            file_lane_implemented,
            paired_randomization_implemented,
            ratio_gates_configured,
            ratio_gates_passed,
        )
    )
    return {
        "passed": passed,
        "required_pin_ids": list(required_pin_ids),
        "required_process_modes": list(_REQUIRED_PROCESS_MODES),
        "required_input_modes": list(_REQUIRED_INPUT_MODES),
        "required_corpus_tiers": list(_REQUIRED_CORPUS_TIERS),
        "selected_representative_corpora": {
            "medium": list(medium_corpora),
            "large": list(large_corpora),
            "annotation_list_heavy": list(annotation_corpora),
        },
        "missing_required_pins": missing_required_pins,
        "required_modes_requested": required_modes_requested,
        "expected_scenario_count": (
            len(representative_corpora)
            * len(_REQUIRED_INPUT_MODES)
            * len(_REQUIRED_PROCESS_MODES)
            * len(required_pin_ids)
        ),
        "successful_scenario_count": successful_scenarios,
        "scenario_failures": failures,
        "all_scenarios_succeeded": all_scenarios_succeeded,
        "contracts_match": contracts_match and assertions_pass,
        "reference_machine_approved": machine_approved,
        "reference_machine_matches_environment": reference_machine_matches,
        "file_lane_implemented": file_lane_implemented,
        "paired_randomization_implemented": paired_randomization_implemented,
        "ratio_gates": {
            "configured": ratio_gates_configured,
            "passed": ratio_gates_passed,
            "reason": "no executable ratio-gate configuration is wired into this runner",
        },
        "reasons": reasons,
    }


def _reference_machine_evidence(
    manifest: ComparatorManifest,
    environment: Mapping[str, Any],
) -> dict[str, object]:
    platform = cast(Mapping[str, Any], environment.get("platform", {}))
    cpu = cast(Mapping[str, Any], environment.get("cpu", {}))
    memory = cast(Mapping[str, Any], environment.get("memory", {}))
    observed = {
        "os": " ".join(
            str(platform.get(name, "")) for name in ("system", "release", "machine")
        ).strip(),
        "cpu": f"{cpu.get('logical_count')} logical CPUs; {cpu.get('model')}",
        "memory_bytes": memory.get("physical_bytes"),
        "storage": environment.get("storage"),
        "power_mode": environment.get("power_mode"),
    }
    expected = {
        "os": manifest.reference_machine.os,
        "cpu": manifest.reference_machine.cpu,
        "memory_bytes": manifest.reference_machine.memory_bytes,
        "storage": manifest.reference_machine.storage,
        "power_mode": manifest.reference_machine.power_mode,
    }
    fields = {name: observed[name] == value for name, value in expected.items()}
    return {
        "matches": all(fields.values()),
        "expected": expected,
        "observed": observed,
        "field_matches": fields,
    }


def _source(corpus: Corpus, cache_dir: Path) -> bytes:
    if corpus.source == "generated":
        return generated_bytes(corpus)
    path = cache_dir / corpus.filename
    if not path.is_file():
        raise ComparatorRunError(
            f"prepared corpus is absent: {corpus.id}; run explicit manifest preparation"
        )
    verify_prepared(corpus, path)
    return path.read_bytes()


def comparator_source_identity(
    *,
    comparator_manifest_path: Path = DEFAULT_COMPARATOR_MANIFEST,
    corpus_manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, object]:
    """Bind every repository-owned runtime input that can affect comparator output."""

    candidates: dict[str, Path] = {}

    def add(path: Path, *, external_label: str | None = None) -> None:
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ComparatorRunError(
                f"runtime source identity input is unavailable: {path.name}"
            ) from error
        try:
            label = resolved.relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            if external_label is None:
                raise ComparatorRunError(
                    "runtime source identity input escapes repository"
                ) from None
            label = external_label
        existing = candidates.get(label)
        if existing is not None and existing != resolved:
            raise ComparatorRunError(f"runtime source identity label collides: {label}")
        candidates[label] = resolved

    for relative in _SOURCE_FILES:
        add(ROOT / relative)
    for relative, suffixes in _SOURCE_TREES:
        tree = ROOT / relative
        for path in tree.rglob("*"):
            if path.is_file() and path.suffix.casefold() in suffixes:
                add(path)
    add(comparator_manifest_path, external_label="@comparator-manifest")
    add(corpus_manifest_path, external_label="@corpus-manifest")

    rows = [
        {
            "path": label,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for label, path in sorted(candidates.items())
        for payload in (path.read_bytes(),)
    ]
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema": SOURCE_IDENTITY_SCHEMA,
        "sha256": hashlib.sha256(SOURCE_IDENTITY_DOMAIN + canonical).hexdigest(),
        "domain": SOURCE_IDENTITY_DOMAIN[:-1].decode("ascii"),
        "preimage_format": (
            "UTF-8 domain, one NUL byte, then compact canonical JSON of path/bytes/sha256 "
            "rows sorted by path"
        ),
        "input_count": len(rows),
        "input_bytes": sum(cast(int, row["bytes"]) for row in rows),
        "inputs": rows,
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_unique_nonempty(values: Sequence[str], name: str) -> None:
    if not values:
        raise ComparatorRunError(f"{name} must be nonempty")
    if any(not isinstance(value, str) or not value for value in values):
        raise ComparatorRunError(f"{name} must contain nonempty strings")
    if len(set(values)) != len(values):
        raise ComparatorRunError(f"{name} must not contain duplicates")


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0 or value > 2**64 - 1:
        raise ValueError(f"{name} must fit unsigned 64-bit range")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_COMPARATOR_MANIFEST)
    parser.add_argument("--corpus-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--corpus", action="append", dest="corpora")
    parser.add_argument("--lane", action="append", dest="lanes")
    parser.add_argument("--process-mode", action="append", dest="process_modes")
    parser.add_argument("--input-mode", action="append", dest="input_modes")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "return success for error-free, contract-valid development evidence "
            "that is not comparative"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.check:
        manifest = check_comparator_contract(
            comparator_manifest_path=args.manifest,
            corpus_manifest_path=args.corpus_manifest,
        )
        print(
            f"comparator contract valid: {len(manifest.comparators)} lanes, "
            f"{len(manifest.timing_fences)} phases"
        )
        return 0
    report = run_comparator_baseline(
        comparator_manifest_path=args.manifest,
        corpus_manifest_path=args.corpus_manifest,
        corpus_ids=tuple(args.corpora or ("generated-tiny-functional",)),
        comparator_ids=tuple(args.lanes or ("pyowl-python-common",)),
        process_modes=tuple(args.process_modes or ("steady-process",)),
        input_modes=tuple(args.input_modes or ("resident-bytes",)),
        warmups=args.warmups,
        repetitions=args.repetitions,
    )
    if args.output is None:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        write_json(args.output, report)
    if report["comparative_complete"]:
        return 0
    return 0 if args.allow_partial and report["contract_valid"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


__all__ = [
    "REPORT_SCHEMA",
    "SOURCE_IDENTITY_SCHEMA",
    "ComparatorRunError",
    "check_comparator_contract",
    "comparator_source_identity",
    "run_comparator_baseline",
]
