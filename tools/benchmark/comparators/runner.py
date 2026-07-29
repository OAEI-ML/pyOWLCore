"""Offline WP14 comparator orchestration and correctness qualification."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
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
from tools.benchmark.report import (
    ReportError,
    collect_environment,
    validate_reference_observation,
    write_json,
)

from .adapters import (
    ADAPTER_RESULT_SCHEMA,
    DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    MAX_SUBPROCESS_REQUEST_BYTES,
    MAX_SUBPROCESS_STDERR_BYTES,
    MAX_SUBPROCESS_STDOUT_BYTES,
    AdapterRequest,
    adapter_status_result,
    default_options,
    options_digest,
    run_core_adapter,
    run_external_adapter,
    sanitize_failure,
)
from .common_contract import common_contract_equality_key
from .fresh import FRESH_PROTOCOL_SCHEMA, FreshRunnerError, run_fresh_subprocess
from .manifest import (
    COMMON_BOUNDARY,
    DEFAULT_COMPARATOR_MANIFEST,
    ROOT,
    ComparatorManifest,
    ComparatorPin,
    load_comparator_manifest,
)
from .persistent import (
    PERSISTENT_PROTOCOL_SCHEMA,
    PersistentExternalRunner,
    PersistentRunnerError,
    PersistentRunnerUnavailable,
    unavailable_lifecycle_audit,
)
from .ratio_statistics import (
    DEFAULT_BOOTSTRAP_RESAMPLES,
    MAX_U64,
    RatioStatisticsError,
    paired_bootstrap_ratio_summary,
)
from .rss_interval import RSS_INTERVAL_SCHEMA

REPORT_SCHEMA = "pyowl-core/comparator-baseline/v1"
SOURCE_IDENTITY_SCHEMA = "pyowl-core/comparator-runtime-source/v1"
SOURCE_IDENTITY_DOMAIN = b"pyowl-core:comparator-runtime-source:v1\x00"
PAIRED_SCHEDULE_SCHEMA = "pyowl-core/comparator-paired-schedule/v1"
RATIO_GATES_SCHEMA = "pyowl-core/comparator-ratio-gates/v1"
DEFAULT_SCHEDULE_SEED = 0
_REQUIRED_PROCESS_MODES = ("fresh-process", "steady-process")
_REQUIRED_INPUT_MODES = ("resident-bytes", "file")
_REQUIRED_CORPUS_TIERS = ("medium", "large")
_STARTUP_TO_READY_WALL = "startup-to-ready-wall"
_CALL_TO_READY_WALL = "call-to-ready-wall"
_INCREMENTAL_PEAK_RSS = "incremental-peak-rss"
_STEADY_INTERVAL_PEAK_RSS = "steady-interval-peak-rss"
_MAX_STEADY_RSS_SAMPLE_GAP_NS = 10_000_000
_RSS_INTERVAL_FIELDS = frozenset(
    {
        "schema",
        "source",
        "pid",
        "quiescent_current_bytes",
        "interval_peak_bytes",
        "incremental_peak_bytes",
        "sample_count",
        "maximum_sample_gap_ns",
    }
)
_FRESH_STARTUP_METRIC_SOURCES = {
    "pyowl-python-common": ("metrics", "startup_to_ready_ns"),
    "pyowl-native-wheel-common": ("metrics", "startup_to_ready_ns"),
    "pyowl-direct-rust-common": ("transport_metrics", "parent_wall_ns"),
    "horned-owl-common": ("transport_metrics", "parent_wall_ns"),
    "py-horned-common": ("transport_metrics", "parent_wall_ns"),
}
_CALL_TO_READY_METRIC_SOURCES = {
    "pyowl-python-common": ("metrics", "wall_ns"),
    "pyowl-native-wheel-common": ("metrics", "wall_ns"),
    "pyowl-direct-rust-common": ("transport_metrics", "parent_wall_ns"),
    "horned-owl-raw": ("transport_metrics", "parent_wall_ns"),
    "horned-owl-common": ("transport_metrics", "parent_wall_ns"),
    "py-horned-common": ("transport_metrics", "parent_wall_ns"),
    "owlapi-common": ("transport_metrics", "parent_wall_ns"),
}
_FRESH_CORE_METRICS = (
    "wall_ns",
    "cpu_ns",
    "startup_to_ready_cpu_ns",
    "load_ns",
    "common_adapter_ns",
    "rss_peak_before_bytes",
    "rss_peak_after_bytes",
    "rss_peak_increment_bytes",
)
_FRESH_CORE_RESULT_FIELDS = frozenset(
    {
        "schema",
        "lane",
        "implementation",
        "boundary",
        "status",
        "reason",
        "corpus_id",
        "source_sha256",
        "options_sha256",
        "input_mode",
        "process_mode",
        "contract",
        "raw_inventory",
        "metrics",
        "timed_validation",
        "artifact",
    }
)
_FRESH_CORE_ARTIFACT_FIELDS = frozenset(
    {
        "pin_state",
        "version",
        "revision",
        "artifact",
        "artifact_sha256",
        "features",
        "allocator",
        "thread_ceiling",
        "runner_revision",
        "runner_sha256",
    }
)
_FRESH_CORE_TIMED_VALIDATION_FIELDS = frozenset(
    {
        "schema",
        "inside_timed_envelope",
        "full_contract_validation",
        "contract_sha256",
        "validation_ns",
    }
)
_SOURCE_FILES = (
    "pyproject.toml",
    "setup.py",
    "pyowl_build.py",
    "native/Cargo.toml",
    "native/Cargo.lock",
    "native/build.rs",
    "schemas/encoded-view-v1.json",
    "schemas/encoded-view-v1.toml",
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
    seed: int = DEFAULT_SCHEDULE_SEED,
    reference_cpu_model: str | None = None,
    reference_storage: str | None = None,
    reference_power_mode: str | None = None,
) -> dict[str, Any]:
    """Run a correctness-qualified raw-sample baseline without network access."""

    if warmups < 0 or repetitions < 1:
        raise ComparatorRunError("warmups must be nonnegative and repetitions positive")
    try:
        _require_u64(seed, "seed")
    except (TypeError, ValueError) as error:
        raise ComparatorRunError(str(error)) from error
    for api_name, label, value in (
        ("reference_cpu_model", "reference CPU model", reference_cpu_model),
        ("reference_storage", "reference storage", reference_storage),
        ("reference_power_mode", "reference power mode", reference_power_mode),
    ):
        if value is None:
            continue
        try:
            validate_reference_observation(value, label)
        except ReportError as error:
            raise ComparatorRunError(f"{api_name}: {error}") from error
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

    persistent_runners, persistent_failures, persistent_audits = _start_persistent_lifecycles(
        pins, process_modes
    )
    rows: list[dict[str, Any]] = []
    try:
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
                    samples_by_pin: dict[str, list[dict[str, Any]]] = {pin.id: [] for pin in pins}
                    if process_mode == "steady-process":
                        for block_index in range(warmups):
                            ordered_pins = _paired_implementation_order(
                                pins,
                                request=request,
                                seed=seed,
                                block_kind="warmup",
                                block_index=block_index,
                            )
                            for pin in ordered_pins:
                                try:
                                    _run_once_with_persistent_lifecycle(
                                        pin,
                                        request,
                                        runners=persistent_runners,
                                        failures=persistent_failures,
                                    )
                                finally:
                                    _cleanup_barrier()
                    for block_index in range(repetitions):
                        ordered_pins = _paired_implementation_order(
                            pins,
                            request=request,
                            seed=seed,
                            block_kind="measured",
                            block_index=block_index,
                        )
                        for order_index, pin in enumerate(ordered_pins):
                            try:
                                sample = _run_once_with_persistent_lifecycle(
                                    pin,
                                    request,
                                    runners=persistent_runners,
                                    failures=persistent_failures,
                                )
                            finally:
                                _cleanup_barrier()
                            samples_by_pin[pin.id].append(
                                _with_schedule_metadata(
                                    sample,
                                    seed=seed,
                                    block_index=block_index,
                                    order_index=order_index,
                                    block_size=len(ordered_pins),
                                )
                            )
                    for pin in pins:
                        rows.append(_aggregate_samples(pin, request, samples_by_pin[pin.id]))
    finally:
        persistent_audits.extend(_close_persistent_lifecycles(persistent_runners))
    persistent_audits.sort(key=lambda value: cast(str, value["lane"]))
    _apply_persistent_lifecycle_failures(rows, persistent_audits)

    assertions = _equality_assertions(rows)
    required_lanes = {value.id for value in pins if value.required}
    completed_required = {
        cast(str, value["lane"])
        for value in rows
        if value["status"] == "ok" and value["lane"] in required_lanes
    }
    environment = collect_environment(
        ROOT,
        reference_cpu_model=reference_cpu_model,
        reference_storage=reference_storage,
        reference_power_mode=reference_power_mode,
    )
    if (
        comparator_source_identity(
            comparator_manifest_path=comparator_manifest_path,
            corpus_manifest_path=corpus_manifest_path,
        )["sha256"]
        != source_identity["sha256"]
    ):
        raise ComparatorRunError("runtime source inputs changed during the comparator run")
    machine_evidence = _reference_machine_evidence(comparator_manifest, environment)
    ratio_gates = _evaluate_ratio_gates(
        corpora=corpora,
        rows=rows,
        repetitions=repetitions,
        seed=seed,
    )
    completion = _completion_requirements(
        comparator_manifest=comparator_manifest,
        corpora=corpora,
        rows=rows,
        assertions=assertions,
        process_modes=process_modes,
        input_modes=input_modes,
        reference_machine_matches=cast(bool, machine_evidence["matches"]),
        ratio_gates=ratio_gates,
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
            "schedule": {
                "schema": PAIRED_SCHEDULE_SCHEMA,
                "seed": seed,
                "seed_type": "unsigned-64-bit integer",
                "algorithm": "SHA-256 rank per scenario, block kind/index, and lane",
                "warmup_blocks": warmups,
                "measured_blocks": repetitions,
                "warmups_apply_to": ["steady-process"],
                "cleanup_barrier": "gc.collect after every implementation invocation",
            },
            "input_modes": list(input_modes),
            "process_modes": list(process_modes),
            "network_during_samples": (
                "core lanes are offline; fresh external lanes are isolated one-shot "
                "processes and steady external lanes use one audited process per lane"
            ),
            "fresh_external_protocol": FRESH_PROTOCOL_SCHEMA,
            "fresh_completion": {
                "boundary": (
                    "authenticated completed PID/sequence/ontology token after full result "
                    "construction and validation, before publish or response serialization"
                ),
                "publish": "parent writes one authenticated release frame then closes stdin",
                "child_cpu": (
                    "successful fresh results report absolute child process CPU as "
                    "metrics.startup_to_ready_cpu_ns at the completion boundary"
                ),
                "parent_cpu": (
                    "transport_metrics.parent_cpu_ns is supervisor/harness CPU through "
                    "authenticated completion"
                ),
            },
            "persistent_external_protocol": PERSISTENT_PROTOCOL_SCHEMA,
            "persistent_external_startup": "outside every call-to-ready sample",
            "steady_rss": {
                "schema": RSS_INTERVAL_SCHEMA,
                "boundary": (
                    "current RSS at authenticated prepared/quiescent boundary through "
                    "authenticated query-ready completion, before publish and response "
                    "serialization"
                ),
                "sampler": (
                    "outside the target process in a pre-spawned helper process; external "
                    "sampling is armed only after the authenticated prepared acknowledgement"
                ),
                "sample_interval_ns": 1_000_000,
                "maximum_accepted_sample_gap_ns": _MAX_STEADY_RSS_SAMPLE_GAP_NS,
                "lifetime_ru_maxrss_is_not_used_for_steady_ratio_gates": True,
            },
            "comparison_order": (
                "seeded implementation-order shuffle within every paired warmup/measured block"
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
        "persistent_runner_lifecycles": persistent_audits,
        "equality_assertions": assertions,
        "execution_errors": execution_errors,
        "contract_valid": assertions_pass and not execution_errors,
        "completion_requirements": completion,
        "ratio_gates": ratio_gates,
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


def _start_persistent_lifecycles(
    pins: Sequence[ComparatorPin],
    process_modes: Sequence[str],
) -> tuple[
    dict[str, PersistentExternalRunner],
    dict[str, tuple[str, str]],
    list[dict[str, Any]],
]:
    runners: dict[str, PersistentExternalRunner] = {}
    failures: dict[str, tuple[str, str]] = {}
    audits: list[dict[str, Any]] = []
    if "steady-process" not in process_modes:
        return runners, failures, audits
    for pin in pins:
        if pin.adapter != "external-command":
            continue
        try:
            runners[pin.id] = PersistentExternalRunner.open(pin, cwd=ROOT)
        except PersistentRunnerUnavailable as error:
            reason = sanitize_failure(error)
            failures[pin.id] = ("not-run", reason)
            audits.append(unavailable_lifecycle_audit(pin, status="not-run", reason=reason))
        except (OSError, TypeError, ValueError, PersistentRunnerError) as error:
            reason = sanitize_failure(error)
            failures[pin.id] = ("error", reason)
            audits.append(unavailable_lifecycle_audit(pin, status="error", reason=reason))
    return runners, failures, audits


def _run_once_with_persistent_lifecycle(
    pin: ComparatorPin,
    request: AdapterRequest,
    *,
    runners: Mapping[str, PersistentExternalRunner],
    failures: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    if request.process_mode != "steady-process" or pin.adapter != "external-command":
        return _run_once(pin, request)
    runner = runners.get(pin.id)
    if runner is not None:
        return runner.run(request)
    status, reason = failures.get(
        pin.id,
        ("error", "persistent external lifecycle was not prepared"),
    )
    return adapter_status_result(pin, request, status, reason)


def _close_persistent_lifecycles(
    runners: Mapping[str, PersistentExternalRunner],
) -> list[dict[str, Any]]:
    return [runners[lane].close() for lane in sorted(runners)]


def _apply_persistent_lifecycle_failures(
    rows: Sequence[dict[str, Any]],
    audits: Sequence[Mapping[str, Any]],
) -> None:
    failures = {
        cast(str, audit["lane"]): sanitize_failure(audit.get("reason"))
        for audit in audits
        if audit.get("status") == "error"
    }
    for row in rows:
        lane = cast(str, row.get("lane"))
        if row.get("process_mode") != "steady-process" or lane not in failures:
            continue
        row["status"] = "error"
        row["reason"] = f"persistent lifecycle audit failed: {failures[lane]}"
        row["contract"] = None


def _paired_implementation_order(
    pins: Sequence[ComparatorPin],
    *,
    request: AdapterRequest,
    seed: int,
    block_kind: str,
    block_index: int,
) -> tuple[ComparatorPin, ...]:
    """Return a reproducible seed-derived permutation for one paired block."""

    if block_kind not in {"warmup", "measured"}:
        raise ValueError("block_kind must be warmup or measured")
    _require_u64(seed, "seed")
    _nonnegative_integer(block_index, "block_index")
    scenario = {
        "schema": PAIRED_SCHEDULE_SCHEMA,
        "seed": seed,
        "corpus_id": request.corpus_id,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
        "block_kind": block_kind,
        "block_index": block_index,
    }
    prefix = _canonical_json(scenario) + b"\x00"

    def rank(pin: ComparatorPin) -> tuple[bytes, str]:
        return hashlib.sha256(prefix + pin.id.encode("utf-8")).digest(), pin.id

    return tuple(sorted(pins, key=rank))


def _with_schedule_metadata(
    sample: Mapping[str, Any],
    *,
    seed: int,
    block_index: int,
    order_index: int,
    block_size: int,
) -> dict[str, Any]:
    result = dict(sample)
    result.update(
        {
            "schedule_seed": seed,
            "paired_block": block_index,
            "implementation_order": order_index,
            "paired_block_size": block_size,
        }
    )
    return result


def _cleanup_barrier() -> None:
    """Run the equal out-of-timer cleanup barrier between paired invocations."""

    gc.collect()


def _run_fresh_core(pin: ComparatorPin, request: AdapterRequest) -> dict[str, Any]:
    try:
        completed = run_fresh_subprocess(
            (sys.executable, "-m", "tools.benchmark.comparators.worker"),
            request.protocol_dict(pin),
            timeout=DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
            max_request_bytes=MAX_SUBPROCESS_REQUEST_BYTES,
            max_stdout_bytes=MAX_SUBPROCESS_STDOUT_BYTES,
            max_stderr_bytes=MAX_SUBPROCESS_STDERR_BYTES,
            cwd=ROOT,
        )
    except (OSError, TypeError, ValueError, FreshRunnerError) as error:
        return adapter_status_result(
            pin,
            request,
            "error",
            f"isolated worker could not start: {type(error).__name__}: {error}",
        )
    result = completed.result
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
        metrics["startup_to_ready_ns"] = completed.parent_wall_ns
    result["transport_metrics"] = {
        "parent_wall_ns": completed.parent_wall_ns,
        "parent_cpu_ns": completed.parent_cpu_ns,
        "request_bytes": completed.request_bytes,
        "stdout_bytes": completed.stdout_bytes,
        "stderr_bytes": completed.stderr_bytes,
        "fresh_protocol": FRESH_PROTOCOL_SCHEMA,
        "fresh_sequence": 0,
        "fresh_runner_pid": completed.runner_pid,
        "ontology_instance_id": completed.ontology_instance_id,
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
    if set(value) != _FRESH_CORE_RESULT_FIELDS:
        raise ValueError("fresh-process result fields differ from adapter schema v1")
    artifact = value.get("artifact")
    if not isinstance(artifact, Mapping):
        raise TypeError("fresh-process result lacks artifact evidence")
    if set(artifact) != _FRESH_CORE_ARTIFACT_FIELDS:
        raise ValueError("fresh-process artifact fields differ from adapter schema v1")
    expected_artifact: tuple[tuple[str, object], ...] = (
        ("pin_state", pin.pin_state),
        ("version", pin.version),
        ("revision", pin.revision),
        ("artifact", pin.artifact),
        ("features", list(pin.features)),
        ("allocator", pin.allocator),
        ("runner_revision", pin.runner_revision),
        ("runner_sha256", pin.runner_sha256),
    )
    for name, expected_value in expected_artifact:
        if artifact.get(name) != expected_value:
            raise ValueError(f"fresh-process artifact {name} differs from expected pin")
    observed_thread_ceiling = artifact.get("thread_ceiling")
    if (
        isinstance(observed_thread_ceiling, bool)
        or not isinstance(observed_thread_ceiling, int)
        or observed_thread_ceiling != pin.thread_ceiling
    ):
        raise ValueError("fresh-process artifact thread_ceiling differs from expected pin")
    if status != "ok":
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            raise ValueError("non-success fresh-process result requires a reason")
        metrics = value.get("metrics")
        if not isinstance(metrics, Mapping) or metrics:
            raise ValueError("non-success fresh-process result must report empty metrics")
        if value.get("contract") is not None or value.get("raw_inventory") is not None:
            raise ValueError("non-success fresh-process result must not report ontology evidence")
        if value.get("timed_validation") is not None:
            raise ValueError("non-success fresh-process result must not report timed validation")
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
    if numeric["startup_to_ready_cpu_ns"] < numeric["cpu_ns"]:
        raise ValueError("startup-to-ready CPU is below call-to-ready CPU")
    before = numeric["rss_peak_before_bytes"]
    after = numeric["rss_peak_after_bytes"]
    increment = numeric["rss_peak_increment_bytes"]
    if after < before or increment != after - before:
        raise ValueError("fresh-process RSS evidence is internally inconsistent")
    contract = value.get("contract")
    if not isinstance(contract, Mapping):
        raise TypeError("successful fresh-process result lacks a common contract")
    if value.get("raw_inventory") is not None:
        raise ValueError("successful common fresh-process result must not report raw inventory")
    attestation = value.get("timed_validation")
    if not isinstance(attestation, Mapping):
        raise TypeError("fresh-process result lacks timed validation attestation")
    if set(attestation) != _FRESH_CORE_TIMED_VALIDATION_FIELDS:
        raise ValueError("fresh-process timed validation fields differ from schema v1")
    if attestation.get("schema") != "pyowl-core/comparator-timed-validation/v1":
        raise ValueError("fresh-process timed validation schema differs")
    if (
        attestation.get("inside_timed_envelope") is not True
        or attestation.get("full_contract_validation") is not True
        or attestation.get("contract_sha256") != contract.get("contract_sha256")
    ):
        raise ValueError("fresh-process contract was not fully validated inside its timer")
    validation_ns = _nonnegative_integer(
        attestation.get("validation_ns"),
        "timed_validation.validation_ns",
    )
    if validation_ns > numeric["common_adapter_ns"]:
        raise ValueError("fresh-process validation exceeds common adapter timing")
    common_contract_equality_key(cast(Mapping[str, Any], contract))
    artifact_sha256 = artifact.get("artifact_sha256")
    if pin.adapter == "core-native":
        if not _is_sha256(artifact_sha256):
            raise ValueError("installed native-wheel result lacks its artifact SHA-256")
        if pin.artifact_sha256 is not None and artifact_sha256 != pin.artifact_sha256:
            raise ValueError("installed native-wheel artifact SHA-256 differs from its pin")
    elif artifact_sha256 is not None:
        raise ValueError(
            "pure-Python fresh-process result unexpectedly reports an artifact SHA-256"
        )


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


def _evaluate_ratio_gates(
    *,
    corpora: Sequence[Corpus],
    rows: Sequence[Mapping[str, Any]],
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    """Evaluate the fixed WP14 minimum ratios from paired resident-byte samples."""

    medium_corpora, large_corpora, annotation_corpora, large_rdfxml_corpora = (
        _representative_corpus_ids(corpora)
    )
    representative_corpora = medium_corpora + large_corpora
    qualification_reasons: list[dict[str, object]] = []
    if not medium_corpora:
        qualification_reasons.append(
            {"reason": "no non-synthetic representative medium corpus was selected"}
        )
    if not large_corpora:
        qualification_reasons.append(
            {"reason": "no non-synthetic representative large corpus was selected"}
        )
    if not large_rdfxml_corpora:
        qualification_reasons.append(
            {"reason": "no representative large biomedical RDF/XML corpus was selected"}
        )
    if not annotation_corpora:
        qualification_reasons.append(
            {
                "reason": (
                    "no annotation/list-heavy representative medium-or-larger corpus was selected"
                )
            }
        )

    rows_by_scenario: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (
            cast(str, row.get("lane")),
            cast(str, row.get("corpus_id")),
            cast(str, row.get("input_mode")),
            cast(str, row.get("process_mode")),
        )
        rows_by_scenario.setdefault(key, []).append(row)

    comparisons: list[dict[str, Any]] = []
    for comparison_id, numerator_lane, denominator_lane in (
        (
            "direct-rust-vs-horned-common",
            "pyowl-direct-rust-common",
            "horned-owl-common",
        ),
        (
            "installed-wheel-vs-py-horned-common",
            "pyowl-native-wheel-common",
            "py-horned-common",
        ),
    ):
        for process_mode in _REQUIRED_PROCESS_MODES:
            wall_selector = (
                _STARTUP_TO_READY_WALL if process_mode == "fresh-process" else _CALL_TO_READY_WALL
            )
            rss_selector = (
                _INCREMENTAL_PEAK_RSS
                if process_mode == "fresh-process"
                else _STEADY_INTERVAL_PEAK_RSS
            )
            metric_results = {
                "wall": _evaluate_ratio_metric(
                    rows_by_scenario=rows_by_scenario,
                    corpus_ids=representative_corpora,
                    large_corpus_ids=large_corpora,
                    numerator_lane=numerator_lane,
                    denominator_lane=denominator_lane,
                    process_mode=process_mode,
                    metric_selector=wall_selector,
                    repetitions=repetitions,
                    schedule_seed=seed,
                    bootstrap_seed=_derived_statistics_seed(
                        seed, f"{comparison_id}/{process_mode}/{wall_selector}"
                    ),
                    aggregate_threshold=1.10,
                    large_corpus_threshold=1.25,
                    gate_on_upper_bound=True,
                ),
                "rss": _evaluate_ratio_metric(
                    rows_by_scenario=rows_by_scenario,
                    corpus_ids=representative_corpora,
                    large_corpus_ids=large_corpora,
                    numerator_lane=numerator_lane,
                    denominator_lane=denominator_lane,
                    process_mode=process_mode,
                    metric_selector=rss_selector,
                    repetitions=repetitions,
                    schedule_seed=seed,
                    bootstrap_seed=_derived_statistics_seed(
                        seed, f"{comparison_id}/{process_mode}/rss_peak_increment_bytes"
                    ),
                    aggregate_threshold=1.15,
                    large_corpus_threshold=1.25,
                    gate_on_upper_bound=True,
                ),
            }
            comparisons.append(
                {
                    "id": f"{comparison_id}/{process_mode}/resident-bytes",
                    "numerator_lane": numerator_lane,
                    "denominator_lane": denominator_lane,
                    "boundary": COMMON_BOUNDARY,
                    "input_mode": "resident-bytes",
                    "process_mode": process_mode,
                    "passed": all(value["passed"] is True for value in metric_results.values()),
                    "metrics": metric_results,
                }
            )

    overhead: list[dict[str, Any]] = []
    for process_mode in _REQUIRED_PROCESS_MODES:
        result = _evaluate_ratio_metric(
            rows_by_scenario=rows_by_scenario,
            corpus_ids=representative_corpora,
            large_corpus_ids=(),
            numerator_lane="pyowl-native-wheel-common",
            denominator_lane="pyowl-direct-rust-common",
            process_mode=process_mode,
            metric_selector=_CALL_TO_READY_WALL,
            repetitions=repetitions,
            schedule_seed=seed,
            bootstrap_seed=_derived_statistics_seed(
                seed,
                f"installed-wheel-vs-direct-rust/{process_mode}/{_CALL_TO_READY_WALL}",
            ),
            aggregate_threshold=1.15,
            large_corpus_threshold=None,
            gate_on_upper_bound=False,
        )
        overhead.append(
            {
                "id": f"installed-wheel-call-overhead/{process_mode}/resident-bytes",
                "numerator_lane": "pyowl-native-wheel-common",
                "denominator_lane": "pyowl-direct-rust-common",
                "metric": "call-to-ready wall_ns",
                "input_mode": "resident-bytes",
                "process_mode": process_mode,
                "passed": result["passed"],
                "result": result,
            }
        )

    reasons = list(qualification_reasons)
    for comparison in comparisons:
        comparison_id = cast(str, comparison["id"])
        metrics = cast(Mapping[str, Mapping[str, Any]], comparison["metrics"])
        for metric_label, metric_result in metrics.items():
            for reason in cast(Sequence[Mapping[str, object]], metric_result["reasons"]):
                reasons.append({"comparison": comparison_id, "metric": metric_label, **reason})
    for comparison in overhead:
        overhead_result = cast(Mapping[str, Any], comparison["result"])
        for reason in cast(Sequence[Mapping[str, object]], overhead_result["reasons"]):
            reasons.append(
                {
                    "comparison": comparison["id"],
                    "metric": "call-to-ready wall_ns",
                    **reason,
                }
            )

    passed = (
        not qualification_reasons
        and all(value["passed"] is True for value in comparisons)
        and all(value["passed"] is True for value in overhead)
    )
    return {
        "schema": RATIO_GATES_SCHEMA,
        "configured": True,
        "passed": passed,
        "ratio_direction": "pyowl-core native / comparator",
        "required_corpora": {
            "medium": list(medium_corpora),
            "large": list(large_corpora),
            "large_biomedical_rdfxml": list(large_rdfxml_corpora),
            "annotation_list_heavy": list(annotation_corpora),
        },
        "required_input_mode": "resident-bytes",
        "required_process_modes": list(_REQUIRED_PROCESS_MODES),
        "raw_horned_equivalence_denominator_allowed": False,
        "excluded_equivalence_denominator_lanes": ["horned-owl-raw"],
        "comparisons": comparisons,
        "installed_wheel_call_to_ready_overhead": overhead,
        "reasons": reasons,
    }


def _evaluate_ratio_metric(
    *,
    rows_by_scenario: Mapping[tuple[str, str, str, str], Sequence[Mapping[str, Any]]],
    corpus_ids: Sequence[str],
    large_corpus_ids: Sequence[str],
    numerator_lane: str,
    denominator_lane: str,
    process_mode: str,
    metric_selector: str,
    repetitions: int,
    schedule_seed: int,
    bootstrap_seed: int,
    aggregate_threshold: float,
    large_corpus_threshold: float | None,
    gate_on_upper_bound: bool,
) -> dict[str, Any]:
    pairs_by_corpus, reasons = _paired_metric_samples(
        rows_by_scenario=rows_by_scenario,
        corpus_ids=corpus_ids,
        numerator_lane=numerator_lane,
        denominator_lane=denominator_lane,
        process_mode=process_mode,
        metric_selector=metric_selector,
        repetitions=repetitions,
        schedule_seed=schedule_seed,
    )
    summary: dict[str, object] | None = None
    if not reasons:
        try:
            summary = paired_bootstrap_ratio_summary(
                pairs_by_corpus,
                seed=bootstrap_seed,
                resamples=DEFAULT_BOOTSTRAP_RESAMPLES,
            )
        except RatioStatisticsError as error:
            reasons.append({"reason": str(error)})

    aggregate_value: float | None = None
    aggregate_passed = False
    guardrails: list[dict[str, object]] = []
    if summary is not None:
        aggregate = cast(Mapping[str, float], summary["aggregate"])
        statistic_name = "upper_confidence_bound" if gate_on_upper_bound else "estimate"
        aggregate_value = aggregate[statistic_name]
        aggregate_passed = aggregate_value <= aggregate_threshold
        by_corpus = {
            cast(str, value["corpus_id"]): cast(float, value["median_ratio"])
            for value in cast(Sequence[Mapping[str, object]], summary["corpora"])
        }
        if large_corpus_threshold is not None:
            for corpus_id in large_corpus_ids:
                median_ratio = by_corpus[corpus_id]
                guardrails.append(
                    {
                        "corpus_id": corpus_id,
                        "median_ratio": median_ratio,
                        "threshold": large_corpus_threshold,
                        "passed": median_ratio <= large_corpus_threshold,
                    }
                )
    guardrails_passed = all(value["passed"] is True for value in guardrails)
    passed = not reasons and aggregate_passed and guardrails_passed
    return {
        "metric": _metric_output_label(metric_selector),
        "metric_selector": metric_selector,
        "sample_sources": {
            "numerator": _metric_source_path(
                metric_selector,
                numerator_lane,
                process_mode=process_mode,
            ),
            "denominator": _metric_source_path(
                metric_selector,
                denominator_lane,
                process_mode=process_mode,
            ),
        },
        "passed": passed,
        "gate_statistic": (
            "aggregate upper endpoint of two-sided 95% paired-bootstrap interval"
            if gate_on_upper_bound
            else "aggregate median-ratio estimate"
        ),
        "aggregate_threshold": aggregate_threshold,
        "aggregate_value": aggregate_value,
        "aggregate_passed": aggregate_passed,
        "large_corpus_threshold": large_corpus_threshold,
        "large_corpus_guardrails": guardrails,
        "large_corpus_guardrails_passed": guardrails_passed,
        "statistics": summary,
        "reasons": reasons,
    }


def _paired_metric_samples(
    *,
    rows_by_scenario: Mapping[tuple[str, str, str, str], Sequence[Mapping[str, Any]]],
    corpus_ids: Sequence[str],
    numerator_lane: str,
    denominator_lane: str,
    process_mode: str,
    metric_selector: str,
    repetitions: int,
    schedule_seed: int,
) -> tuple[dict[str, tuple[tuple[int, int], ...]], list[dict[str, object]]]:
    pairs_by_corpus: dict[str, tuple[tuple[int, int], ...]] = {}
    reasons: list[dict[str, object]] = []
    if not corpus_ids:
        return {}, [{"reason": "required representative corpus set is empty"}]
    for corpus_id in corpus_ids:
        scenario = {
            "corpus_id": corpus_id,
            "input_mode": "resident-bytes",
            "process_mode": process_mode,
        }
        numerator_rows = rows_by_scenario.get(
            (numerator_lane, corpus_id, "resident-bytes", process_mode), ()
        )
        denominator_rows = rows_by_scenario.get(
            (denominator_lane, corpus_id, "resident-bytes", process_mode), ()
        )
        if len(numerator_rows) != 1:
            reasons.append(
                {
                    **scenario,
                    "lane": numerator_lane,
                    "reason": (
                        "required scenario row is missing"
                        if not numerator_rows
                        else "required scenario row is duplicated"
                    ),
                }
            )
            continue
        if len(denominator_rows) != 1:
            reasons.append(
                {
                    **scenario,
                    "lane": denominator_lane,
                    "reason": (
                        "required scenario row is missing"
                        if not denominator_rows
                        else "required scenario row is duplicated"
                    ),
                }
            )
            continue
        numerator_row = numerator_rows[0]
        denominator_row = denominator_rows[0]
        row_reason = _ratio_row_reason(numerator_row, numerator_lane)
        if row_reason is not None:
            reasons.append({**scenario, "lane": numerator_lane, "reason": row_reason})
            continue
        row_reason = _ratio_row_reason(denominator_row, denominator_lane)
        if row_reason is not None:
            reasons.append({**scenario, "lane": denominator_lane, "reason": row_reason})
            continue
        try:
            numerator_contract = cast(Mapping[str, Any], numerator_row["contract"])
            denominator_contract = cast(Mapping[str, Any], denominator_row["contract"])
            if common_contract_equality_key(numerator_contract) != common_contract_equality_key(
                denominator_contract
            ):
                reasons.append(
                    {
                        **scenario,
                        "reason": "numerator and denominator common contracts differ",
                    }
                )
                continue
        except (KeyError, TypeError, ValueError):
            reasons.append({**scenario, "reason": "common-contract equality evidence is invalid"})
            continue
        numerator_samples, numerator_reason = _samples_by_paired_block(
            numerator_row,
            lane=numerator_lane,
            process_mode=process_mode,
            metric_selector=metric_selector,
            repetitions=repetitions,
            schedule_seed=schedule_seed,
            allow_zero_metric=metric_selector in {_INCREMENTAL_PEAK_RSS, _STEADY_INTERVAL_PEAK_RSS},
        )
        denominator_samples, denominator_reason = _samples_by_paired_block(
            denominator_row,
            lane=denominator_lane,
            process_mode=process_mode,
            metric_selector=metric_selector,
            repetitions=repetitions,
            schedule_seed=schedule_seed,
            allow_zero_metric=False,
        )
        if numerator_reason is not None:
            reasons.append({**scenario, "lane": numerator_lane, "reason": numerator_reason})
            continue
        if denominator_reason is not None:
            reasons.append({**scenario, "lane": denominator_lane, "reason": denominator_reason})
            continue
        pairs: list[tuple[int, int]] = []
        pairing_invalid = False
        for block_index in range(repetitions):
            numerator_sample = numerator_samples[block_index]
            denominator_sample = denominator_samples[block_index]
            if (
                numerator_sample["paired_block_size"] != denominator_sample["paired_block_size"]
                or numerator_sample["implementation_order"]
                == denominator_sample["implementation_order"]
            ):
                reasons.append(
                    {
                        **scenario,
                        "paired_block": block_index,
                        "reason": "paired block has inconsistent or duplicate implementation order",
                    }
                )
                pairing_invalid = True
                break
            pairs.append((numerator_sample["metric_value"], denominator_sample["metric_value"]))
        if not pairing_invalid:
            pairs_by_corpus[corpus_id] = tuple(pairs)
    return pairs_by_corpus, reasons


def _ratio_row_reason(row: Mapping[str, Any], lane: str) -> str | None:
    if lane == "horned-owl-raw" or row.get("boundary") != COMMON_BOUNDARY:
        return "raw/asymmetric readiness is forbidden as an equivalence denominator"
    if row.get("status") != "ok":
        reason = sanitize_failure(row.get("reason"))
        return f"scenario status is {row.get('status')}: {reason}"
    if not isinstance(row.get("contract"), Mapping):
        return "successful common-ready row lacks its equality contract"
    return None


def _samples_by_paired_block(
    row: Mapping[str, Any],
    *,
    lane: str,
    process_mode: str,
    metric_selector: str,
    repetitions: int,
    schedule_seed: int,
    allow_zero_metric: bool,
) -> tuple[dict[int, dict[str, int]], str | None]:
    samples = row.get("samples")
    if not isinstance(samples, list) or len(samples) != repetitions:
        return {}, f"{lane}: expected exactly {repetitions} measured raw samples"
    by_block: dict[int, dict[str, int]] = {}
    for sample_index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            return {}, f"{lane}: raw sample {sample_index} is not an object"
        try:
            observed_seed = _require_u64(sample.get("schedule_seed"), "schedule_seed")
            block_index = _nonnegative_integer(sample.get("paired_block"), "paired_block")
            order_index = _nonnegative_integer(
                sample.get("implementation_order"), "implementation_order"
            )
            block_size = _nonnegative_integer(sample.get("paired_block_size"), "paired_block_size")
        except (TypeError, ValueError) as error:
            return {}, f"{lane}: raw sample {sample_index} schedule is invalid: {error}"
        if observed_seed != schedule_seed:
            return {}, f"{lane}: raw sample {sample_index} uses a different schedule seed"
        if block_index >= repetitions or block_size < 1 or order_index >= block_size:
            return {}, f"{lane}: raw sample {sample_index} schedule is out of range"
        if block_index in by_block:
            return {}, f"{lane}: paired block {block_index} is duplicated"
        try:
            metric_value = _selected_metric_value(
                sample,
                lane=lane,
                process_mode=process_mode,
                metric_selector=metric_selector,
                allow_zero=allow_zero_metric,
            )
        except (TypeError, ValueError) as error:
            return {}, f"{lane}: paired block {block_index} has invalid metric: {error}"
        by_block[block_index] = {
            "implementation_order": order_index,
            "paired_block_size": block_size,
            "metric_value": metric_value,
        }
    if set(by_block) != set(range(repetitions)):
        return {}, f"{lane}: paired block coverage is incomplete"
    return by_block, None


def _selected_metric_value(
    sample: Mapping[str, Any],
    *,
    lane: str,
    process_mode: str,
    metric_selector: str,
    allow_zero: bool,
) -> int:
    if metric_selector == _STEADY_INTERVAL_PEAK_RSS:
        return _steady_interval_rss_value(sample, allow_zero=allow_zero)
    source = _metric_source(
        metric_selector,
        lane,
        process_mode=process_mode,
    )
    container: object = sample
    for name in source[:-1]:
        if not isinstance(container, Mapping):
            raise TypeError(f"{'.'.join(source[:-1])} must be an object")
        container = container.get(name)
    if not isinstance(container, Mapping):
        raise TypeError(f"{'.'.join(source[:-1])} must be an object")
    metric = container.get(source[-1])
    name = ".".join(source)
    return _require_u64(metric, name) if allow_zero else _positive_u64_metric(metric, name)


def _steady_interval_rss_value(sample: Mapping[str, Any], *, allow_zero: bool) -> int:
    transport = sample.get("transport_metrics")
    if not isinstance(transport, Mapping):
        raise TypeError("transport_metrics must be an object")
    interval = transport.get("rss_interval")
    if not isinstance(interval, Mapping):
        raise TypeError("transport_metrics.rss_interval must be an object")
    if set(interval) != _RSS_INTERVAL_FIELDS or interval.get("schema") != RSS_INTERVAL_SCHEMA:
        raise ValueError("transport_metrics.rss_interval fields differ")
    source = interval.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("transport_metrics.rss_interval.source must be nonempty")
    pid = _positive_u64_metric(interval.get("pid"), "transport_metrics.rss_interval.pid")
    baseline = _require_u64(
        interval.get("quiescent_current_bytes"),
        "transport_metrics.rss_interval.quiescent_current_bytes",
    )
    peak = _require_u64(
        interval.get("interval_peak_bytes"),
        "transport_metrics.rss_interval.interval_peak_bytes",
    )
    increment = _require_u64(
        interval.get("incremental_peak_bytes"),
        "transport_metrics.rss_interval.incremental_peak_bytes",
    )
    sample_count = _positive_u64_metric(
        interval.get("sample_count"),
        "transport_metrics.rss_interval.sample_count",
    )
    maximum_gap = _require_u64(
        interval.get("maximum_sample_gap_ns"),
        "transport_metrics.rss_interval.maximum_sample_gap_ns",
    )
    if pid < 1 or sample_count < 2:
        raise ValueError("transport_metrics.rss_interval counters are invalid")
    if peak < baseline or increment != peak - baseline:
        raise ValueError("transport_metrics.rss_interval is internally inconsistent")
    if maximum_gap > _MAX_STEADY_RSS_SAMPLE_GAP_NS:
        raise ValueError("transport_metrics.rss_interval sampling gap exceeds 10 ms")
    if not allow_zero and increment == 0:
        raise ValueError("transport_metrics.rss_interval.incremental_peak_bytes must be positive")
    return increment


def _metric_source(
    metric_selector: str,
    lane: str,
    *,
    process_mode: str | None = None,
) -> tuple[str, ...]:
    if metric_selector == _CALL_TO_READY_WALL:
        if process_mode == "fresh-process":
            return "metrics", "wall_ns"
        if process_mode not in {None, "steady-process"}:
            raise ValueError(f"unsupported call-to-ready process mode: {process_mode}")
        try:
            return _CALL_TO_READY_METRIC_SOURCES[lane]
        except KeyError:
            raise ValueError(f"{lane}: no call-to-ready metric source is defined") from None
    if metric_selector == _INCREMENTAL_PEAK_RSS:
        return "metrics", "rss_peak_increment_bytes"
    if metric_selector == _STEADY_INTERVAL_PEAK_RSS:
        return "transport_metrics", "rss_interval", "incremental_peak_bytes"
    if metric_selector == _STARTUP_TO_READY_WALL:
        try:
            return _FRESH_STARTUP_METRIC_SOURCES[lane]
        except KeyError:
            raise ValueError(
                f"{lane}: no fresh-process startup-to-ready metric source is defined"
            ) from None
    raise ValueError(f"unsupported ratio metric selector: {metric_selector}")


def _metric_source_path(
    metric_selector: str,
    lane: str,
    *,
    process_mode: str | None = None,
) -> str:
    return ".".join(
        _metric_source(
            metric_selector,
            lane,
            process_mode=process_mode,
        )
    )


def _metric_output_label(metric_selector: str) -> str:
    labels = {
        _STARTUP_TO_READY_WALL: "startup-to-ready wall_ns",
        _CALL_TO_READY_WALL: "call-to-ready wall_ns",
        _INCREMENTAL_PEAK_RSS: "incremental peak RSS bytes",
        _STEADY_INTERVAL_PEAK_RSS: "incremental peak RSS bytes",
    }
    try:
        return labels[metric_selector]
    except KeyError:
        raise ValueError(f"unsupported ratio metric selector: {metric_selector}") from None


def _derived_statistics_seed(seed: int, label: str) -> int:
    payload = seed.to_bytes(8, "big") + b"\x00" + label.encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _representative_corpus_ids(
    corpora: Sequence[Corpus],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    medium = tuple(
        value.id
        for value in corpora
        if value.tier == "medium" and "synthetic" not in value.families
    )
    large = tuple(
        value.id for value in corpora if value.tier == "large" and "synthetic" not in value.families
    )
    representative = set(medium + large)
    annotation = tuple(
        value.id
        for value in corpora
        if value.id in representative and "annotation-list-heavy" in value.families
    )
    large_rdfxml = tuple(
        value.id
        for value in corpora
        if value.id in large and value.format.value == "rdfxml" and "biomedical" in value.families
    )
    return medium, large, annotation, large_rdfxml


def _completion_requirements(
    *,
    comparator_manifest: ComparatorManifest,
    corpora: Sequence[Corpus],
    rows: Sequence[Mapping[str, Any]],
    assertions: Sequence[Mapping[str, Any]],
    process_modes: Sequence[str],
    input_modes: Sequence[str],
    reference_machine_matches: bool,
    ratio_gates: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate the complete release matrix; partial smoke runs fail closed."""

    required_pins = tuple(value for value in comparator_manifest.comparators if value.required)
    required_pin_ids = tuple(value.id for value in required_pins)
    medium_corpora, large_corpora, annotation_corpora, large_rdfxml_corpora = (
        _representative_corpus_ids(corpora)
    )
    representative_corpora = medium_corpora + large_corpora
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
    paired_randomization_implemented = True
    ratio_gates_configured = ratio_gates.get("configured") is True
    ratio_gates_passed = ratio_gates.get("passed") is True
    all_scenarios_succeeded = bool(representative_corpora) and not failures

    reasons: list[str] = []
    if not medium_corpora:
        reasons.append("no non-synthetic representative medium corpus was selected")
    if not large_corpora:
        reasons.append("no non-synthetic representative large corpus was selected")
    if not large_rdfxml_corpora:
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
    if not ratio_gates_configured:
        reasons.append("executable comparative ratio gates are not configured")
    elif not ratio_gates_passed:
        reasons.append("executable comparative ratio gates are configured but did not pass")

    passed = all(
        (
            medium_corpora,
            large_corpora,
            large_rdfxml_corpora,
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
            "large_biomedical_rdfxml": list(large_rdfxml_corpora),
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
        "ratio_gates": ratio_gates,
        "reasons": reasons,
    }


def _reference_machine_evidence(
    manifest: ComparatorManifest,
    environment: Mapping[str, Any],
) -> dict[str, object]:
    platform = cast(Mapping[str, Any], environment.get("platform", {}))
    cpu = cast(Mapping[str, Any], environment.get("cpu", {}))
    memory = cast(Mapping[str, Any], environment.get("memory", {}))
    observation_sources = cast(
        Mapping[str, Any],
        environment.get("machine_observation_sources", {}),
    )
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
    fields["cpu"] = fields["cpu"] and observation_sources.get("cpu_model") in {
        "platform-probe",
        "operator-supplied",
    }
    for name in ("storage", "power_mode"):
        fields[name] = fields[name] and observation_sources.get(name) == "operator-supplied"
    return {
        "matches": all(fields.values()),
        "expected": expected,
        "observed": observed,
        "observation_sources": dict(observation_sources),
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


def _require_u64(value: object, name: str) -> int:
    return _nonnegative_integer(value, name)


def _positive_u64_metric(value: object, name: str) -> int:
    result = _require_u64(value, name)
    if result == 0:
        raise ValueError(f"{name} must be positive for a ratio gate")
    return result


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
    parser.add_argument(
        "--seed",
        type=_parse_cli_u64,
        default=DEFAULT_SCHEDULE_SEED,
        help="unsigned 64-bit seed for paired implementation-order scheduling",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--reference-cpu-model",
        type=_parse_reference_observation,
        help="exact operator-observed CPU model when the platform probe is unavailable",
    )
    parser.add_argument(
        "--reference-storage",
        type=_parse_reference_observation,
        help="exact approved storage device and cache-state procedure",
    )
    parser.add_argument(
        "--reference-power-mode",
        type=_parse_reference_observation,
        help="exact approved fixed power/performance configuration",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "return success for error-free, contract-valid development evidence "
            "that is not comparative"
        ),
    )
    return parser


def _parse_cli_u64(value: str) -> int:
    if not value or any(character not in "0123456789" for character in value):
        raise argparse.ArgumentTypeError("seed must be an unsigned decimal integer")
    parsed = int(value, 10)
    if parsed > MAX_U64:
        raise argparse.ArgumentTypeError("seed must fit unsigned 64-bit range")
    return parsed


def _parse_reference_observation(value: object) -> str:
    try:
        validated = validate_reference_observation(value, "reference observation")
    except ReportError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    assert validated is not None
    return validated


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
        seed=args.seed,
        reference_cpu_model=args.reference_cpu_model,
        reference_storage=args.reference_storage,
        reference_power_mode=args.reference_power_mode,
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
    "PAIRED_SCHEDULE_SCHEMA",
    "RATIO_GATES_SCHEMA",
    "REPORT_SCHEMA",
    "SOURCE_IDENTITY_SCHEMA",
    "ComparatorRunError",
    "check_comparator_contract",
    "comparator_source_identity",
    "run_comparator_baseline",
]
