"""Coarse comparator adapters with a strict JSON subprocess protocol."""

from __future__ import annotations

import base64
import gc
import hashlib
import importlib.util
import json
import math
import os
import re
import resource
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, cast

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologySnapshot,
    load_snapshot,
)
from pyowl_core.exceptions import BackendUnavailableError

from ..native_redesign.encoded_contract import EncodedContractUnavailable
from .common_contract import (
    build_core_common_contract,
    build_encoded_core_common_contract,
    common_contract_equality_key,
    validate_common_contract,
)
from .manifest import COMMON_BOUNDARY, ROOT, ComparatorPin

ADAPTER_RESULT_SCHEMA = "pyowl-core/comparator-adapter-result/v1"
ADAPTER_REQUEST_SCHEMA = "pyowl-core/comparator-adapter-request/v2"
TIMED_VALIDATION_SCHEMA = "pyowl-core/comparator-timed-validation/v1"
RAW_INVENTORY_SCHEMA = "pyowl-core/comparator-raw-inventory/v1"
RAW_INVENTORY_DIGEST_DOMAIN = b"pyowl-core:comparator-raw-inventory:v1\x00"

DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 900.0
MAX_SUBPROCESS_REQUEST_BYTES = 512 * 1024**2
MAX_SUBPROCESS_STDOUT_BYTES = 256 * 1024**2
MAX_SUBPROCESS_STDERR_BYTES = 64 * 1024
MAX_FAILURE_CHARS = 1_000
MAX_PHASE_METRICS = 64
MAX_PHASE_NAME_CHARS = 80
MAX_U64 = 2**64 - 1
_DOCUMENT_IRI_PREFIX = "urn:pyowl-core:comparator-source:sha256:"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_URL = re.compile(r"(?i)\b(?:https?|file)://\S+")
_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^\s\"']+")
_SECRET = re.compile(
    r"(?i)\b((?:authorization|password|passwd|secret|token)\b\s*[:=]\s*|bearer\s+)"
    r"[^\s,;]+"
)

_EXTERNAL_METRICS = (
    "wall_ns",
    "cpu_ns",
    "load_ns",
    "rss_peak_before_bytes",
    "rss_peak_after_bytes",
    "rss_peak_increment_bytes",
    "temporary_bytes",
    "object_count",
)
_RESULT_FIELDS = frozenset(
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
_ARTIFACT_FIELDS = frozenset(
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
_TIMED_VALIDATION_FIELDS = frozenset(
    {
        "schema",
        "inside_timed_envelope",
        "full_contract_validation",
        "contract_sha256",
        "validation_ns",
    }
)
_RAW_INVENTORY_FIELDS = frozenset(
    {
        "schema",
        "model_kind",
        "axiom_count",
        "annotation_count",
        "import_count",
        "entity_count",
        "diagnostic_count",
        "inventory_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """Bounded subprocess output plus an explicit timeout/limit outcome."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limit: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    corpus_id: str
    source: bytes
    source_sha256: str
    format: DocumentFormat
    options: LoadOptions
    options_sha256: str
    input_mode: str
    process_mode: str

    def __post_init__(self) -> None:
        if not self.corpus_id:
            raise ValueError("corpus_id must be nonempty")
        if not isinstance(self.source, bytes):
            raise TypeError("source must be immutable bytes")
        if hashlib.sha256(self.source).hexdigest() != self.source_sha256:
            raise ValueError("source bytes differ from pinned SHA-256")
        if not isinstance(self.format, DocumentFormat):
            raise TypeError("format must be DocumentFormat")
        if not isinstance(self.options, LoadOptions):
            raise TypeError("options must be LoadOptions")
        if self.options.format is not self.format:
            raise ValueError("request format differs from options.format")
        observed_options = options_digest(self.options)
        if observed_options != self.options_sha256:
            raise ValueError("options differ from declared options SHA-256")
        if self.input_mode not in {"resident-bytes", "file"}:
            raise ValueError("input_mode must be resident-bytes or file")
        if self.process_mode not in {"steady-process", "fresh-process"}:
            raise ValueError("process_mode must be steady-process or fresh-process")

    def protocol_dict(
        self,
        pin: ComparatorPin,
    ) -> dict[str, object]:
        return {
            "schema": ADAPTER_REQUEST_SCHEMA,
            "lane": pin.id,
            "implementation": pin.implementation,
            "boundary": pin.boundary,
            "corpus_id": self.corpus_id,
            "source_b64": base64.b64encode(self.source).decode("ascii"),
            "source_sha256": self.source_sha256,
            "document_iri": self.document_iri,
            "format": self.format.value,
            "options_sha256": self.options_sha256,
            "options": options_inventory(self.options),
            "input_mode": self.input_mode,
            "process_mode": self.process_mode,
            "expected_artifact_sha256": pin.artifact_sha256,
            "expected_features": list(pin.features),
            "expected_allocator": pin.allocator,
            "expected_thread_ceiling": pin.thread_ceiling,
            "expected_runner_revision": pin.runner_revision,
            "expected_runner_sha256": pin.runner_sha256,
        }

    @property
    def document_iri(self) -> str:
        """Stable semantic base shared by resident-byte and prepared-file lanes."""

        return comparator_document_iri(self.source_sha256)


def comparator_document_iri(source_sha256: str) -> str:
    """Return the path-independent document IRI bound to pinned source bytes."""

    if not isinstance(source_sha256, str) or not _SHA256.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256")
    return _DOCUMENT_IRI_PREFIX + source_sha256


def run_core_adapter(
    pin: ComparatorPin,
    request: AdapterRequest,
    *,
    isolated_process: bool = False,
) -> dict[str, Any]:
    """Run the current Python or delivered native facade inside this process."""

    expected_adapter = {
        "pyowl-core-python": "core-python",
        "pyowl-core-native-wheel": "core-native",
    }.get(pin.implementation)
    if expected_adapter is None or pin.adapter != expected_adapter:
        raise ValueError(f"{pin.id}: not an in-process core comparator")
    if pin.boundary != COMMON_BOUNDARY:
        raise ValueError(f"{pin.id}: core adapters require common readiness")
    if request.process_mode != "steady-process" and not isolated_process:
        return _not_run(pin, request, "fresh core lane requires the isolated worker")
    if not pin.artifact_is_runnable:
        return _not_run(pin, request, "artifact pin is pending")
    if pin.adapter == "core-native" and not _native_is_from_installed_wheel():
        return _not_run(
            pin,
            request,
            "delivered-wheel lane refuses a source-tree/native build; use an isolated wheel venv",
        )

    backend = BackendPreference.PYTHON if pin.adapter == "core-python" else BackendPreference.NATIVE
    options = _replace_backend(request.options, backend)
    encoded_metrics: dict[str, int] = {}
    try:
        with _core_input_source(request) as input_source:
            gc.collect()
            rss_before = _rss_peak_bytes()
            blocks_before = _allocated_blocks()
            objects_before = len(gc.get_objects())
            cpu_start = time.process_time_ns()
            wall_start = time.perf_counter_ns()
            load_start = time.perf_counter_ns()
            loaded = load_snapshot(
                input_source,
                document_iri=request.document_iri,
                options=options,
            )
            load_end = time.perf_counter_ns()
            if not isinstance(loaded, OntologySnapshot):
                raise TypeError("core comparator did not publish OntologySnapshot")
            contract_start = time.perf_counter_ns()
            if pin.adapter == "core-native":
                encoded = build_encoded_core_common_contract(
                    loaded,
                    corpus_id=request.corpus_id,
                    source_sha256=request.source_sha256,
                    options_sha256=request.options_sha256,
                )
                contract = encoded.contract
                encoded_metrics = encoded.evidence.to_metrics()
            else:
                contract = build_core_common_contract(
                    loaded,
                    corpus_id=request.corpus_id,
                    source_sha256=request.source_sha256,
                    options_sha256=request.options_sha256,
                )
            validation_start = time.perf_counter_ns()
            validate_common_contract(contract)
            validation_end = time.perf_counter_ns()
            contract_end = time.perf_counter_ns()
            wall_end = time.perf_counter_ns()
            cpu_end = time.process_time_ns()
            objects_after = len(gc.get_objects())
            blocks_after = _allocated_blocks()
            rss_after = _rss_peak_bytes()
    except BackendUnavailableError as error:
        return _not_run(pin, request, f"native backend unavailable: {error}")
    except EncodedContractUnavailable as error:
        return _not_run(pin, request, str(error))
    except Exception as error:  # comparator failures are evidence, not harness crashes
        return _error(pin, request, error)
    return {
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": pin.id,
        "implementation": pin.implementation,
        "boundary": pin.boundary,
        "status": "ok",
        "reason": None,
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
        "contract": contract,
        "timed_validation": {
            "schema": TIMED_VALIDATION_SCHEMA,
            "inside_timed_envelope": True,
            "full_contract_validation": True,
            "contract_sha256": contract["contract_sha256"],
            "validation_ns": validation_end - validation_start,
        },
        "raw_inventory": None,
        "metrics": {
            "wall_ns": wall_end - wall_start,
            "cpu_ns": cpu_end - cpu_start,
            "load_ns": load_end - load_start,
            "common_adapter_ns": contract_end - contract_start,
            "rss_peak_before_bytes": rss_before,
            "rss_peak_after_bytes": rss_after,
            "rss_peak_increment_bytes": max(0, rss_after - rss_before),
            "temporary_bytes": len(request.source) if request.input_mode == "file" else 0,
            "python_allocated_blocks_increment": max(0, blocks_after - blocks_before),
            "python_gc_objects_increment": max(0, objects_after - objects_before),
            "core_report_seconds": dict(loaded.report.timings),
            **encoded_metrics,
        },
        "artifact": {
            "pin_state": pin.pin_state,
            "version": pin.version,
            "revision": pin.revision,
            "artifact": pin.artifact,
            "artifact_sha256": _loaded_core_artifact_sha256(pin),
            "features": list(pin.features),
            "allocator": pin.allocator,
            "thread_ceiling": pin.thread_ceiling,
            "runner_revision": pin.runner_revision,
            "runner_sha256": pin.runner_sha256,
        },
    }


@contextmanager
def _core_input_source(request: AdapterRequest) -> Iterator[bytes | Path]:
    """Prepare a file lane outside the timed envelope and retain it through loading."""

    if request.input_mode == "resident-bytes":
        yield request.source
        return
    with tempfile.TemporaryDirectory(prefix="pyowl-core-comparator-") as directory:
        path = Path(directory) / "ontology-input"
        written = path.write_bytes(request.source)
        if written != len(request.source):  # pragma: no cover - Path.write_bytes is exact or raises
            raise OSError("prepared comparator file was truncated")
        yield path


def run_external_adapter(
    pin: ComparatorPin,
    request: AdapterRequest,
    *,
    timeout_seconds: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    max_stdout_bytes: int = MAX_SUBPROCESS_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_SUBPROCESS_STDERR_BYTES,
) -> dict[str, Any]:
    """Invoke one isolated runner; absence is explicit ``not-run`` evidence."""

    if pin.adapter != "external-command" or pin.launcher_env is None:
        raise ValueError(f"{pin.id}: not an external comparator")
    if not pin.artifact_is_runnable:
        return _not_run(pin, request, "artifact or external runner pin is pending")
    if request.process_mode == "steady-process":
        return _not_run(
            pin,
            request,
            "steady external requests require the audited persistent lifecycle",
        )
    command_text = os.environ.get(pin.launcher_env)
    if not command_text:
        return _not_run(pin, request, f"launcher environment {pin.launcher_env} is unset")
    try:
        command = tuple(shlex.split(command_text))
    except ValueError as error:
        return _error(pin, request, error)
    if not command:
        return _not_run(pin, request, f"launcher environment {pin.launcher_env} is empty")
    try:
        command = _verified_runner_command(pin, command)
    except (OSError, ValueError) as error:
        return _error(pin, request, error)
    body = json.dumps(
        request.protocol_dict(pin),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    try:
        completed = run_bounded_subprocess(
            command,
            body,
            timeout=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            env=_external_environment(pin),
        )
    except (OSError, TypeError, ValueError) as error:
        return _error(pin, request, error)
    parent_wall_ns = time.perf_counter_ns() - wall_start
    parent_cpu_ns = time.process_time_ns() - cpu_start
    if completed.timed_out:
        return _error(
            pin,
            request,
            TimeoutError(f"external adapter exceeded {timeout_seconds:g} seconds"),
        )
    if completed.output_limit is not None:
        return _error(
            pin,
            request,
            RuntimeError(f"external adapter exceeded {completed.output_limit} output limit"),
        )
    if completed.returncode != 0:
        detail = sanitize_failure(completed.stderr.decode("utf-8", "replace"))
        return _error(
            pin,
            request,
            RuntimeError(f"runner exited {completed.returncode}: {detail}"),
        )
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return _error(pin, request, error)
    if not isinstance(decoded, dict):
        return _error(pin, request, TypeError("runner output must be a JSON object"))
    result = cast(dict[str, Any], decoded)
    try:
        _validate_external_result(
            pin,
            request,
            result,
        )
    except (TypeError, ValueError) as error:
        return _error(pin, request, error)
    if result.get("status") != "ok":
        result["reason"] = sanitize_failure(result.get("reason", "external adapter failed"))
    result.setdefault("transport_metrics", {})
    transport = result["transport_metrics"]
    if not isinstance(transport, dict):
        return _error(pin, request, TypeError("transport_metrics must be an object"))
    transport.update(
        {
            "parent_wall_ns": parent_wall_ns,
            "parent_cpu_ns": parent_cpu_ns,
            "request_bytes": len(body),
            "stdout_bytes": len(completed.stdout),
            "stderr_bytes": len(completed.stderr),
        }
    )
    return result


def options_inventory(options: LoadOptions) -> dict[str, object]:
    """JSON-safe, exact semantic options passed to every comparator."""

    limits = options.limits
    limit_fields = {field.name: getattr(limits, field.name) for field in fields(limits)}
    return {
        "format": None if options.format is None else options.format.value,
        "imports": options.imports.value,
        "offline": options.offline,
        "preserve_source_map": options.preserve_source_map,
        "collect_provenance": options.collect_provenance,
        "validate_owl2_dl": options.validate_owl2_dl,
        "deterministic": options.deterministic,
        "limits": limit_fields,
    }


def options_digest(options: LoadOptions) -> str:
    """Return the canonical semantic-options digest used by every lane."""

    encoded = json.dumps(
        options_inventory(options),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def raw_inventory_digest(
    *,
    axiom_count: int,
    annotation_count: int,
    import_count: int,
    entity_count: int,
    diagnostic_count: int,
) -> str:
    """Hash the exact v1 raw-inventory scalar preimage for runner attestations."""

    counts = {
        "axiom_count": axiom_count,
        "annotation_count": annotation_count,
        "import_count": import_count,
        "entity_count": entity_count,
        "diagnostic_count": diagnostic_count,
    }
    for name, value in counts.items():
        _nonnegative_integer(value, f"raw_inventory.{name}")
    payload = {
        "schema": RAW_INVENTORY_SCHEMA,
        "model_kind": "horned-model-ready",
        **counts,
    }
    preimage = RAW_INVENTORY_DIGEST_DOMAIN + json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


def _validate_external_result(
    pin: ComparatorPin,
    request: AdapterRequest,
    value: Mapping[str, Any],
) -> None:
    unknown = set(value) - _RESULT_FIELDS
    if unknown:
        raise ValueError("external result contains unknown fields")
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
            raise ValueError(f"external result {name} differs from request/pin")
    status = value.get("status")
    if status not in {"ok", "not-run", "ineligible", "error"}:
        raise ValueError("external result has invalid status")
    if status == "ok":
        if value.get("reason") is not None:
            raise ValueError("successful external result must not contain a failure reason")
        metrics = _mapping(value.get("metrics"), "external result metrics")
        _validate_external_metrics(metrics, common=pin.boundary == COMMON_BOUNDARY)
        if pin.boundary == COMMON_BOUNDARY:
            contract = value.get("contract")
            if not isinstance(contract, Mapping):
                raise TypeError("common external result lacks contract")
            attestation = _mapping(
                value.get("timed_validation"),
                "external timed validation attestation",
            )
            _validate_timed_attestation(attestation, contract, metrics)
            common_contract_equality_key(cast(Mapping[str, Any], contract))
        else:
            inventory = value.get("raw_inventory")
            if not isinstance(inventory, Mapping):
                raise TypeError("raw external result lacks raw_inventory")
            _validate_raw_inventory(cast(Mapping[str, Any], inventory))
    elif not isinstance(value.get("reason"), str) or not value.get("reason"):
        raise ValueError("non-success external result requires a bounded reason")
    artifact = value.get("artifact")
    if not isinstance(artifact, Mapping):
        raise TypeError("external result lacks artifact evidence")
    if set(artifact) != _ARTIFACT_FIELDS:
        raise ValueError("external artifact evidence fields differ from schema v1")
    if status == "ok":
        expected_artifact: tuple[tuple[str, object], ...] = (
            ("pin_state", pin.pin_state),
            ("version", pin.version),
            ("revision", pin.revision),
            ("artifact", pin.artifact),
            ("features", list(pin.features)),
            ("allocator", pin.allocator),
        )
        for name, expected_value in expected_artifact:
            if artifact.get(name) != expected_value:
                raise ValueError(f"external runner {name} differs from expected pin")
        observed_thread_ceiling = artifact.get("thread_ceiling")
        if (
            isinstance(observed_thread_ceiling, bool)
            or not isinstance(observed_thread_ceiling, int)
            or observed_thread_ceiling != pin.thread_ceiling
        ):
            raise ValueError("external runner thread_ceiling differs from expected pin")
        observed_artifact_sha256 = artifact.get("artifact_sha256")
        if not isinstance(observed_artifact_sha256, str) or not _SHA256.fullmatch(
            observed_artifact_sha256
        ):
            raise ValueError("external runner lacks a lowercase artifact SHA-256")
        if pin.artifact_sha256 is not None and observed_artifact_sha256 != pin.artifact_sha256:
            raise ValueError("external runner artifact_sha256 differs from expected pin")
        for name, adapter_expected in (
            ("runner_revision", pin.runner_revision),
            ("runner_sha256", pin.runner_sha256),
        ):
            if adapter_expected is not None and artifact.get(name) != adapter_expected:
                raise ValueError(f"external runner {name} differs from expected pin")


def _validate_external_metrics(metrics: Mapping[str, Any], *, common: bool) -> None:
    allowed = set(_EXTERNAL_METRICS) | {"common_adapter_ns", "phase_ns"}
    if set(metrics) - allowed:
        raise ValueError("external metrics contain unknown fields")
    required = _EXTERNAL_METRICS + (("common_adapter_ns",) if common else ())
    for name in required:
        _nonnegative_integer(metrics.get(name), f"metrics.{name}")
    phase_ns = metrics.get("phase_ns")
    if phase_ns is not None:
        phases = _mapping(phase_ns, "metrics.phase_ns")
        if len(phases) > MAX_PHASE_METRICS:
            raise ValueError("metrics.phase_ns exceeds its entry limit")
        for name, value in phases.items():
            if not isinstance(name, str) or not name or len(name) > MAX_PHASE_NAME_CHARS:
                raise ValueError("metrics.phase_ns contains an invalid phase name")
            _nonnegative_integer(value, f"metrics.phase_ns.{name}")
    wall_ns = cast(int, metrics["wall_ns"])
    load_ns = cast(int, metrics["load_ns"])
    if load_ns > wall_ns:
        raise ValueError("metrics.load_ns exceeds the timed envelope")
    if common:
        common_adapter_ns = cast(int, metrics["common_adapter_ns"])
        if load_ns + common_adapter_ns > wall_ns:
            raise ValueError("external phases exceed the timed wall envelope")
    before = cast(int, metrics["rss_peak_before_bytes"])
    after = cast(int, metrics["rss_peak_after_bytes"])
    increment = cast(int, metrics["rss_peak_increment_bytes"])
    if after < before or increment != after - before:
        raise ValueError("RSS peak evidence is internally inconsistent")


def _validate_timed_attestation(
    attestation: Mapping[str, Any],
    contract: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> None:
    if set(attestation) != _TIMED_VALIDATION_FIELDS:
        raise ValueError("external timed validation fields differ from schema v1")
    if attestation.get("schema") != TIMED_VALIDATION_SCHEMA:
        raise ValueError("external timed validation schema differs")
    if attestation.get("inside_timed_envelope") is not True:
        raise ValueError("external contract validation was not attested inside the timer")
    if attestation.get("full_contract_validation") is not True:
        raise ValueError("external adapter did not attest full contract validation")
    if attestation.get("contract_sha256") != contract.get("contract_sha256"):
        raise ValueError("external timed validation attests a different contract")
    validation_ns = _nonnegative_integer(
        attestation.get("validation_ns"),
        "timed_validation.validation_ns",
    )
    if validation_ns > cast(float, metrics["common_adapter_ns"]):
        raise ValueError("external contract validation exceeds common adapter timing")


def _validate_raw_inventory(value: Mapping[str, Any]) -> None:
    if set(value) != _RAW_INVENTORY_FIELDS:
        raise ValueError("raw inventory fields differ from schema v1")
    if value.get("schema") != RAW_INVENTORY_SCHEMA:
        raise ValueError("raw inventory schema differs")
    if value.get("model_kind") != "horned-model-ready":
        raise ValueError("raw inventory model kind differs")
    for name in (
        "axiom_count",
        "annotation_count",
        "import_count",
        "entity_count",
        "diagnostic_count",
    ):
        _nonnegative_integer(value.get(name), f"raw_inventory.{name}")
    digest = value.get("inventory_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("raw inventory digest must be lowercase SHA-256")
    expected = raw_inventory_digest(
        axiom_count=cast(int, value["axiom_count"]),
        annotation_count=cast(int, value["annotation_count"]),
        import_count=cast(int, value["import_count"]),
        entity_count=cast(int, value["entity_count"]),
        diagnostic_count=cast(int, value["diagnostic_count"]),
    )
    if digest != expected:
        raise ValueError("raw inventory digest differs from its canonical scalar preimage")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return cast(Mapping[str, Any], value)


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0 or value > MAX_U64:
        raise ValueError(f"{name} must fit unsigned 64-bit range")
    return value


def _replace_backend(options: LoadOptions, backend: BackendPreference) -> LoadOptions:
    return LoadOptions(
        format=options.format,
        imports=options.imports,
        backend=backend,
        limits=options.limits,
        offline=options.offline,
        preserve_source_map=options.preserve_source_map,
        collect_provenance=options.collect_provenance,
        validate_owl2_dl=options.validate_owl2_dl,
        deterministic=options.deterministic,
    )


def default_options(format: DocumentFormat) -> LoadOptions:
    return LoadOptions(
        format=format,
        imports=ImportPolicy.RECORD_UNRESOLVED,
        backend=BackendPreference.PYTHON,
        offline=True,
        preserve_source_map=False,
        collect_provenance=True,
        validate_owl2_dl=False,
        deterministic=True,
    )


def _loaded_core_artifact_sha256(pin: ComparatorPin) -> str | None:
    if pin.adapter == "core-python":
        return None
    try:
        import pyowl_core._native as native_extension

        path = getattr(native_extension, "__file__", None)
        if not isinstance(path, str):
            return None
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()
    except (ImportError, OSError):
        return None


def _native_is_from_installed_wheel() -> bool:
    package = importlib.util.find_spec("pyowl_core")
    extension = importlib.util.find_spec("pyowl_core._native")
    if package is None or package.origin is None or extension is None or extension.origin is None:
        return False
    root = ROOT.resolve()
    for raw in (package.origin, extension.origin):
        try:
            if Path(raw).resolve().is_relative_to(root):
                return False
        except (OSError, ValueError):
            return False
    return True


def run_bounded_subprocess(
    command: Sequence[str],
    body: bytes,
    *,
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> BoundedProcessResult:
    """Run a subprocess with wall-time and file-backed output ceilings."""

    if not command or any(not isinstance(value, str) or not value for value in command):
        raise ValueError("subprocess command must contain nonempty strings")
    if not isinstance(body, bytes):
        raise TypeError("subprocess input must be bytes")
    if len(body) > MAX_SUBPROCESS_REQUEST_BYTES:
        raise ValueError("subprocess request exceeds the configured byte limit")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("subprocess timeout must be numeric")
    if not math.isfinite(float(timeout)) or timeout <= 0:
        raise ValueError("subprocess timeout must be finite and positive")
    for name, limit in (
        ("stdout", max_stdout_bytes),
        ("stderr", max_stderr_bytes),
    ):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"subprocess {name} limit must be a positive integer")

    with (
        tempfile.TemporaryFile(mode="w+b") as request_file,
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        request_file.write(body)
        request_file.seek(0)
        process = subprocess.Popen(
            tuple(command),
            stdin=request_file,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            env=None if env is None else dict(env),
            start_new_session=os.name == "posix",
        )
        deadline = time.monotonic() + float(timeout)
        timed_out = False
        output_limit: str | None = None
        while process.poll() is None:
            stdout_size = os.fstat(stdout_file.fileno()).st_size
            stderr_size = os.fstat(stderr_file.fileno()).st_size
            if stdout_size > max_stdout_bytes:
                output_limit = "stdout"
                _terminate_process(process)
                break
            if stderr_size > max_stderr_bytes:
                output_limit = "stderr"
                _terminate_process(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process(process)
                break
            time.sleep(0.01)
        try:
            returncode = process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            returncode = process.wait()

        stdout_size = os.fstat(stdout_file.fileno()).st_size
        stderr_size = os.fstat(stderr_file.fileno()).st_size
        if output_limit is None and stdout_size > max_stdout_bytes:
            output_limit = "stdout"
        if output_limit is None and stderr_size > max_stderr_bytes:
            output_limit = "stderr"
        stdout_file.seek(0)
        stderr_file.seek(0)
        return BoundedProcessResult(
            returncode=returncode,
            stdout=stdout_file.read(max_stdout_bytes),
            stderr=stderr_file.read(max_stderr_bytes),
            timed_out=timed_out,
            output_limit=output_limit,
        )


def sanitize_failure(value: object, *, limit: int = MAX_FAILURE_CHARS) -> str:
    """Return one bounded diagnostic without paths, URLs, secrets, or controls."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("failure diagnostic limit must be a positive integer")
    rendered = _CONTROL.sub(" ", str(value))
    rendered = _SECRET.sub(lambda match: f"{match.group(1)}<redacted>", rendered)
    rendered = _URL.sub("<url>", rendered)
    rendered = _PATH.sub("<path>", rendered)
    rendered = " ".join(rendered.split())
    return (rendered or "unspecified comparator failure")[:limit]


def adapter_status_result(
    pin: ComparatorPin,
    request: AdapterRequest,
    status: str,
    reason: str,
) -> dict[str, Any]:
    """Build one fail-closed adapter-shaped non-success sample."""

    if status not in {"not-run", "ineligible", "error"}:
        raise ValueError("adapter failure status is invalid")
    return _status_result(pin, request, status, reason)


def _not_run(pin: ComparatorPin, request: AdapterRequest, reason: str) -> dict[str, Any]:
    return _status_result(pin, request, "not-run", reason)


def _error(pin: ComparatorPin, request: AdapterRequest, error: BaseException) -> dict[str, Any]:
    return _status_result(
        pin,
        request,
        "error",
        sanitize_failure(f"{type(error).__name__}: {error}"),
    )


def _status_result(
    pin: ComparatorPin,
    request: AdapterRequest,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": pin.id,
        "implementation": pin.implementation,
        "boundary": pin.boundary,
        "status": status,
        "reason": sanitize_failure(reason),
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
        "contract": None,
        "raw_inventory": None,
        "metrics": {},
        "artifact": {
            "pin_state": pin.pin_state,
            "version": pin.version,
            "revision": pin.revision,
            "artifact": pin.artifact,
            "artifact_sha256": pin.artifact_sha256,
            "features": list(pin.features),
            "allocator": pin.allocator,
            "thread_ceiling": pin.thread_ceiling,
            "runner_revision": pin.runner_revision,
            "runner_sha256": pin.runner_sha256,
        },
    }


def _verified_runner_command(
    pin: ComparatorPin,
    command: Sequence[str],
) -> tuple[str, ...]:
    if len(command) != 1:
        raise ValueError("external runner command must be one pinned executable without arguments")
    if pin.runner_sha256 is None:
        raise ValueError("external runner SHA-256 is not pinned")
    executable = Path(command[0])
    if not executable.is_absolute():
        raise ValueError("external runner executable must be an absolute path")
    resolved = executable.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("external runner executable is not a regular file")
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    if digest.hexdigest() != pin.runner_sha256:
        raise ValueError("external runner executable SHA-256 differs from its pin")
    return (str(resolved),)


def _external_environment(pin: ComparatorPin) -> dict[str, str]:
    """Return a minimal, lane-bound child environment with deterministic ceilings.

    Raw and common Horned lanes intentionally share one executable pin.  The
    launcher therefore needs an authenticated, parent-selected lane before it
    emits its pre-request handshake; the request itself arrives only after that
    handshake has been validated.  These three values are descriptive inputs,
    not trust anchors: the parent still verifies every handshake/result field
    against ``pin`` and the executable digest before accepting evidence.
    """

    selected = {
        name: value
        for name in ("LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TMPDIR")
        if (value := os.environ.get(name)) is not None
    }
    ceiling = str(pin.thread_ceiling)
    selected.update(
        {
            "MKL_NUM_THREADS": ceiling,
            "NUMEXPR_NUM_THREADS": ceiling,
            "OMP_NUM_THREADS": ceiling,
            "OPENBLAS_NUM_THREADS": ceiling,
            "PYOWL_CORE_COMPARATOR_BOUNDARY": pin.boundary,
            "PYOWL_CORE_COMPARATOR_IMPLEMENTATION": pin.implementation,
            "PYOWL_CORE_COMPARATOR_LANE": pin.id,
            "RAYON_NUM_THREADS": ceiling,
            "TOKIO_WORKER_THREADS": ceiling,
        }
    )
    return selected


def _terminate_process(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = 0.2,
) -> None:
    """Terminate a process group, then kill it if the grace period expires."""

    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:  # pragma: no cover - kernel-level process failure
        process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)


def _rss_peak_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(value)
    return int(value) * 1024


def _allocated_blocks() -> int:
    getter = getattr(sys, "getallocatedblocks", None)
    return int(getter()) if getter is not None else 0


__all__ = [
    "ADAPTER_REQUEST_SCHEMA",
    "ADAPTER_RESULT_SCHEMA",
    "DEFAULT_SUBPROCESS_TIMEOUT_SECONDS",
    "MAX_SUBPROCESS_REQUEST_BYTES",
    "MAX_SUBPROCESS_STDERR_BYTES",
    "MAX_SUBPROCESS_STDOUT_BYTES",
    "RAW_INVENTORY_DIGEST_DOMAIN",
    "RAW_INVENTORY_SCHEMA",
    "TIMED_VALIDATION_SCHEMA",
    "AdapterRequest",
    "BoundedProcessResult",
    "adapter_status_result",
    "default_options",
    "options_digest",
    "options_inventory",
    "raw_inventory_digest",
    "run_bounded_subprocess",
    "run_core_adapter",
    "run_external_adapter",
    "sanitize_failure",
]
