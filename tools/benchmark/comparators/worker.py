"""Isolated fresh-process worker for pyowl-core comparator lanes."""

from __future__ import annotations

import base64
import hashlib
import resource
import sys
import time
from collections.abc import Mapping
from dataclasses import fields
from typing import Any, cast

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    ParseLimits,
)

from .adapters import (
    ADAPTER_REQUEST_SCHEMA,
    MAX_SUBPROCESS_REQUEST_BYTES,
    MAX_SUBPROCESS_STDOUT_BYTES,
    AdapterRequest,
    comparator_document_iri,
    run_core_adapter,
    sanitize_failure,
)
from .fresh import publish_fresh_result, read_fresh_request
from .manifest import load_comparator_manifest

_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "lane",
        "implementation",
        "boundary",
        "corpus_id",
        "source_b64",
        "source_sha256",
        "document_iri",
        "format",
        "options_sha256",
        "options",
        "input_mode",
        "process_mode",
        "expected_artifact_sha256",
        "expected_features",
        "expected_allocator",
        "expected_thread_ceiling",
        "expected_runner_revision",
        "expected_runner_sha256",
    }
)
_OPTION_FIELDS = frozenset(
    {
        "format",
        "imports",
        "offline",
        "preserve_source_map",
        "collect_provenance",
        "validate_owl2_dl",
        "deterministic",
        "limits",
    }
)


def main() -> int:
    try:
        value = cast(
            Mapping[str, Any],
            read_fresh_request(max_request_bytes=MAX_SUBPROCESS_REQUEST_BYTES),
        )
        if set(value) != _REQUEST_FIELDS:
            raise ValueError("request fields differ from adapter protocol v2")
        if value.get("schema") != ADAPTER_REQUEST_SCHEMA:
            raise ValueError("unsupported adapter request schema")
        lane = _string(value.get("lane"), "lane")
        pin = load_comparator_manifest().by_id(lane)
        for name, expected in (
            ("implementation", pin.implementation),
            ("boundary", pin.boundary),
            ("expected_artifact_sha256", pin.artifact_sha256),
            ("expected_features", list(pin.features)),
            ("expected_allocator", pin.allocator),
            ("expected_thread_ceiling", pin.thread_ceiling),
            ("expected_runner_revision", pin.runner_revision),
            ("expected_runner_sha256", pin.runner_sha256),
        ):
            if value.get(name) != expected:
                raise ValueError(f"request {name} differs from comparator pin")
        source = base64.b64decode(_string(value.get("source_b64"), "source_b64"), validate=True)
        source_sha256 = _string(value.get("source_sha256"), "source_sha256")
        if hashlib.sha256(source).hexdigest() != source_sha256:
            raise ValueError("request source differs from pinned digest")
        if value.get("document_iri") != comparator_document_iri(source_sha256):
            raise ValueError("request document_iri differs from pinned source digest")
        options = _options(value.get("options"))
        request = AdapterRequest(
            corpus_id=_string(value.get("corpus_id"), "corpus_id"),
            source=source,
            source_sha256=source_sha256,
            format=DocumentFormat(_string(value.get("format"), "format")),
            options=options,
            options_sha256=_string(value.get("options_sha256"), "options_sha256"),
            input_mode=_string(value.get("input_mode"), "input_mode"),
            process_mode=_string(value.get("process_mode"), "process_mode"),
        )
        result = run_core_adapter(pin, request, isolated_process=True)
        _finalize_completion_metrics(result)
    except Exception as error:
        sys.stderr.write(sanitize_failure(f"{type(error).__name__}: {error}") + "\n")
        return 2
    try:
        publish_fresh_result(
            result,
            max_request_bytes=MAX_SUBPROCESS_REQUEST_BYTES,
            max_response_bytes=MAX_SUBPROCESS_STDOUT_BYTES,
        )
    except Exception as error:
        sys.stderr.write(sanitize_failure(f"{type(error).__name__}: {error}") + "\n")
        return 2
    return 0


def _finalize_completion_metrics(result: Mapping[str, Any]) -> None:
    """Publish RSS and child CPU evidence at the assembled completion boundary."""

    if result.get("status") != "ok":
        return
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return
    before = metrics.get("rss_peak_before_bytes")
    if isinstance(before, bool) or not isinstance(before, int) or before < 0:
        return
    metrics["startup_to_ready_cpu_ns"] = 0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    after = int(usage) if sys.platform == "darwin" else int(usage) * 1024
    metrics["rss_peak_after_bytes"] = after
    metrics["rss_peak_increment_bytes"] = max(0, after - before)
    metrics["startup_to_ready_cpu_ns"] = time.process_time_ns()


def _options(value: object) -> LoadOptions:
    if not isinstance(value, Mapping):
        raise TypeError("options must be an object")
    options = cast(Mapping[str, Any], value)
    if set(options) != _OPTION_FIELDS:
        raise ValueError("options fields differ from adapter protocol v1")
    limits_raw = options.get("limits")
    if not isinstance(limits_raw, Mapping):
        raise TypeError("options.limits must be an object")
    limit_values = cast(Mapping[str, Any], limits_raw)
    if set(limit_values) != {field.name for field in fields(ParseLimits)}:
        raise ValueError("options.limits fields differ from ParseLimits")
    limits = ParseLimits(**dict(limit_values))
    format_raw = options.get("format")
    return LoadOptions(
        format=(
            None if format_raw is None else DocumentFormat(_string(format_raw, "options.format"))
        ),
        imports=ImportPolicy(_string(options.get("imports"), "options.imports")),
        backend=BackendPreference.PYTHON,
        limits=limits,
        offline=_boolean(options.get("offline"), "options.offline"),
        preserve_source_map=_boolean(
            options.get("preserve_source_map"), "options.preserve_source_map"
        ),
        collect_provenance=_boolean(
            options.get("collect_provenance"), "options.collect_provenance"
        ),
        validate_owl2_dl=_boolean(options.get("validate_owl2_dl"), "options.validate_owl2_dl"),
        deterministic=_boolean(options.get("deterministic"), "options.deterministic"),
    )


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a nonempty string")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be boolean")
    return value


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    raise SystemExit(main())
