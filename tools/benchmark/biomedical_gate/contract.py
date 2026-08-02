"""Shared strict contracts for the WP23 biomedical subprocess gate."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import fields
from typing import Any, cast

from pyowl_core import BackendPreference, ImportPolicy, LoadOptions, ParseLimits

from ..manifest import Corpus

WORKER_REQUEST_SCHEMA = "pyowl-core/biomedical-one-document-request/v1"
WORKER_RESULT_SCHEMA = "pyowl-core/biomedical-one-document-result/v1"
REPORT_SCHEMA = "pyowl-core/biomedical-one-document-gate/v1"

PUBLIC_CASE_IDS = (
    "generated-component-scaling-functional",
    "oaei-bioml-fma-2026",
    "oaei-bioml-ncit-2026",
)
PRIVATE_CASE_IDS = ("snomed-ct-rdfxml", "snomed-ct-functional")

ANONYMOUS_TELEMETRY_NAMES = (
    "native_anonymous_component_count",
    "native_anonymous_total_labels",
    "native_anonymous_total_arcs",
    "native_anonymous_largest_component_labels",
    "native_anonymous_largest_component_arcs",
    "native_anonymous_largest_component_roots",
    "native_anonymous_maximum_root_interval_span",
    "native_anonymous_maximum_open_root_intervals",
    "native_anonymous_total_setup_work",
    "native_anonymous_total_refinement_work",
    "native_anonymous_total_candidate_order_work",
    "native_anonymous_total_canonical_work",
    "native_anonymous_largest_component_work",
    "native_anonymous_maximum_refinement_rounds",
    "native_anonymous_total_permutations_examined",
    "native_anonymous_accounted_bytes",
)

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024**2
MAX_STDERR_BYTES = 16 * 1024
MAX_EXACT_FLOAT_INTEGER = 1 << 53

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "corpus_id",
        "source_path",
        "backend",
        "expected_native_sha256",
        "require_native_telemetry",
    }
)
_RESULT_FIELDS = frozenset(
    {"schema", "status", "corpus", "contract", "runtime", "output", "measurement"}
)
_CONTRACT_FIELDS = frozenset(
    {
        "requested_backend",
        "selected_backend",
        "api_version",
        "model_schema",
        "adapter_protocol",
        "wire_format",
        "encoded_view_schemas",
        "options",
        "options_sha256",
        "default_parse_limits",
        "default_parse_limits_sha256",
        "load_entrypoint_calls",
        "consumer_chunking",
        "document_count",
    }
)
_OUTPUT_FIELDS = frozenset(
    {
        "root_document_key",
        "complete_import_closure",
        "counts",
        "fingerprints",
        "inventories",
        "common_contract_sha256",
        "anonymous_components",
        "encoded_evidence",
    }
)
_COUNT_FIELDS = frozenset(
    {
        "source_bytes",
        "documents",
        "axioms",
        "declarations",
        "ontology_annotations",
        "extensions",
        "signature_entities",
        "imports",
        "diagnostics",
        "rdf_triples",
    }
)
_MEASUREMENT_FIELDS = frozenset(
    {
        "load_wall_ns",
        "child_startup_to_ready_cpu_ns",
        "fresh_process_peak_rss_bytes",
        "rss_platform_unit",
        "sample_count",
        "portable_performance_claim",
    }
)


class BiomedicalGateError(ValueError):
    """Biomedical evidence is missing, stale, malformed, or overclaimed."""


def canonical_bytes(value: object) -> bytes:
    """Return the compact canonical encoding used for contract digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def default_limits_row() -> dict[str, int | float | None]:
    limits = ParseLimits()
    return {field.name: getattr(limits, field.name) for field in fields(ParseLimits)}


def options_for(corpus: Corpus, backend: BackendPreference) -> LoadOptions:
    return LoadOptions(
        format=corpus.format,
        imports=ImportPolicy.IGNORE,
        backend=backend,
        limits=ParseLimits(),
        offline=True,
        preserve_source_map=False,
        collect_provenance=True,
        validate_owl2_dl=False,
        deterministic=True,
        allow_partial_rdf_mapping=False,
    )


def options_row(options: LoadOptions) -> dict[str, object]:
    return {
        "format": None if options.format is None else options.format.value,
        "imports": options.imports.value,
        "backend": options.backend.value,
        "limits": {
            field.name: getattr(options.limits, field.name) for field in fields(ParseLimits)
        },
        "offline": options.offline,
        "preserve_source_map": options.preserve_source_map,
        "collect_provenance": options.collect_provenance,
        "validate_owl2_dl": options.validate_owl2_dl,
        "deterministic": options.deterministic,
        "allow_partial_rdf_mapping": options.allow_partial_rdf_mapping,
    }


def corpus_identity(corpus: Corpus) -> dict[str, object]:
    """Return every manifest field needed to identify one exact input."""

    return {
        "id": corpus.id,
        "tier": corpus.tier,
        "families": list(corpus.families),
        "source": corpus.source,
        "format": corpus.format.value,
        "revision": corpus.revision,
        "url": corpus.url,
        "bytes": corpus.counts.bytes,
        "sha256": corpus.sha256,
        "triples": corpus.counts.triples,
        "axioms": corpus.counts.axioms,
        "entities": corpus.counts.entities,
        "imports": corpus.counts.imports,
        "count_basis": corpus.counts.basis,
        "generator": corpus.generator,
        "generator_size": corpus.generator_size,
        "license": corpus.license,
        "license_url": corpus.license_url,
        "acquired": corpus.acquired,
        "redistribution": corpus.redistribution,
    }


def parse_request(value: object) -> dict[str, object]:
    row = _mapping(value, "request")
    _exact_fields(row, _REQUEST_FIELDS, "request")
    if row["schema"] != WORKER_REQUEST_SCHEMA:
        raise BiomedicalGateError("unsupported biomedical request schema")
    corpus_id = _string(row["corpus_id"], "request.corpus_id")
    backend = _string(row["backend"], "request.backend")
    if backend not in {BackendPreference.NATIVE.value, BackendPreference.PYTHON.value}:
        raise BiomedicalGateError("request.backend must be native or python")
    source_path = row["source_path"]
    if source_path is not None:
        _string(source_path, "request.source_path")
    expected_native = row["expected_native_sha256"]
    if expected_native is not None:
        _digest(expected_native, "request.expected_native_sha256")
    require_telemetry = _boolean(
        row["require_native_telemetry"], "request.require_native_telemetry"
    )
    if backend == "native" and expected_native is None:
        raise BiomedicalGateError("native request requires an expected extension digest")
    if backend != "native" and require_telemetry:
        raise BiomedicalGateError("native telemetry cannot be required from the Python backend")
    return {
        "schema": WORKER_REQUEST_SCHEMA,
        "corpus_id": corpus_id,
        "source_path": source_path,
        "backend": backend,
        "expected_native_sha256": expected_native,
        "require_native_telemetry": require_telemetry,
    }


def validate_worker_result(
    value: object,
    *,
    request: Mapping[str, object],
    corpus: Corpus,
) -> dict[str, Any]:
    """Validate all bounded worker evidence before accepting completion."""

    parsed_request = parse_request(request)
    row = _mapping(value, "worker result")
    _exact_fields(row, _RESULT_FIELDS, "worker result")
    if row["schema"] != WORKER_RESULT_SCHEMA or row["status"] != "pass":
        raise BiomedicalGateError("worker did not publish a passing schema-v1 result")
    if dict(_mapping(row["corpus"], "worker result corpus")) != corpus_identity(corpus):
        raise BiomedicalGateError("worker corpus identity differs from the locked manifest")

    contract = _mapping(row["contract"], "worker result contract")
    _exact_fields(contract, _CONTRACT_FIELDS, "worker result contract")
    requested_backend = cast(str, parsed_request["backend"])
    if contract["requested_backend"] != requested_backend:
        raise BiomedicalGateError("worker requested backend differs")
    if contract["selected_backend"] != requested_backend:
        raise BiomedicalGateError("worker selected backend differs from the forced request")
    if contract["api_version"] != [0, 2] or contract["model_schema"] != 2:
        raise BiomedicalGateError("worker API/model schema differs from the release contract")
    _positive_integer(contract["adapter_protocol"], "contract.adapter_protocol")
    wire = contract["wire_format"]
    if not isinstance(wire, list) or wire != [1, 2]:
        raise BiomedicalGateError("worker wire-format ledger differs")
    encoded = _mapping(contract["encoded_view_schemas"], "contract.encoded_view_schemas")
    if encoded.get("pyowl-core/structural-columns") != 2:
        raise BiomedicalGateError("worker encoded structural schema differs")
    expected_options = options_row(options_for(corpus, BackendPreference(requested_backend)))
    if dict(_mapping(contract["options"], "contract.options")) != expected_options:
        raise BiomedicalGateError("worker options differ from the one-document gate")
    if contract["options_sha256"] != sha256_json(expected_options):
        raise BiomedicalGateError("worker options digest differs")
    limits = default_limits_row()
    if dict(_mapping(contract["default_parse_limits"], "contract.default_parse_limits")) != limits:
        raise BiomedicalGateError("worker did not use exact default ParseLimits")
    if contract["default_parse_limits_sha256"] != sha256_json(limits):
        raise BiomedicalGateError("worker default ParseLimits digest differs")
    if (
        contract["load_entrypoint_calls"] != 1
        or contract["consumer_chunking"] is not False
        or contract["document_count"] != 1
    ):
        raise BiomedicalGateError("worker did not perform exactly one unchunked document load")

    runtime = _mapping(row["runtime"], "worker result runtime")
    _validate_runtime(runtime, expected_native=parsed_request["expected_native_sha256"])

    output = _mapping(row["output"], "worker result output")
    _exact_fields(output, _OUTPUT_FIELDS, "worker result output")
    _string(output["root_document_key"], "output.root_document_key")
    _boolean(output["complete_import_closure"], "output.complete_import_closure")
    counts = _mapping(output["counts"], "output.counts")
    _exact_fields(counts, _COUNT_FIELDS, "output.counts")
    for name in _COUNT_FIELDS - {"rdf_triples"}:
        _nonnegative_integer(counts[name], f"output.counts.{name}")
    if counts["rdf_triples"] is not None:
        _nonnegative_integer(counts["rdf_triples"], "output.counts.rdf_triples")
    if counts["source_bytes"] != corpus.counts.bytes or counts["documents"] != 1:
        raise BiomedicalGateError("worker source/document counts differ from the input lock")
    if counts["axioms"] < 1 or counts["declarations"] < 1:
        raise BiomedicalGateError("worker did not publish positive axiom/declaration counts")
    if corpus.id == "generated-component-scaling-functional" and (
        counts["axioms"] != corpus.counts.axioms or counts["signature_entities"] != 1
    ):
        raise BiomedicalGateError("generated component counts differ from their exact lock")
    fingerprints = _mapping(output["fingerprints"], "output.fingerprints")
    if set(fingerprints) != {"document", "structural", "logical", "signature"}:
        raise BiomedicalGateError("worker must publish all four fingerprints")
    for name, evidence in fingerprints.items():
        item = _mapping(evidence, f"output.fingerprints.{name}")
        if set(item) != {
            "algorithm",
            "schema",
            "preimage_bytes",
            "preimage_sha256",
            "digest",
        }:
            raise BiomedicalGateError(f"{name} fingerprint fields differ")
        if item["algorithm"] != "sha256" or item["schema"] != 2:
            raise BiomedicalGateError(f"{name} fingerprint contract differs")
        _positive_integer(item["preimage_bytes"], f"{name}.preimage_bytes")
        _digest(item["preimage_sha256"], f"{name}.preimage_sha256")
        _digest(item["digest"], f"{name}.digest")
        if item["preimage_sha256"] != item["digest"]:
            raise BiomedicalGateError(f"{name} fingerprint preimage digest differs")
    _digest(output["common_contract_sha256"], "output.common_contract_sha256")
    inventories = _mapping(output["inventories"], "output.inventories")
    if set(inventories) != {
        "ontology_annotations",
        "axioms",
        "extensions",
        "signature",
        "documents",
    }:
        raise BiomedicalGateError("worker inventory set differs")
    _validate_inventories(inventories, counts)

    anonymous = output["anonymous_components"]
    if parsed_request["require_native_telemetry"]:
        anonymous_row = _mapping(anonymous, "output.anonymous_components")
        if set(anonymous_row) != set(ANONYMOUS_TELEMETRY_NAMES):
            raise BiomedicalGateError("worker anonymous telemetry names differ")
        for name in ANONYMOUS_TELEMETRY_NAMES:
            value_item = anonymous_row[name]
            if (
                isinstance(value_item, bool)
                or not isinstance(value_item, int)
                or not 0 <= value_item <= MAX_EXACT_FLOAT_INTEGER
            ):
                raise BiomedicalGateError(f"worker anonymous telemetry {name} is inexact")
    elif anonymous is not None:
        raise BiomedicalGateError("optional Python telemetry must be null")

    evidence = _mapping(output["encoded_evidence"], "output.encoded_evidence")
    for name, number in evidence.items():
        _nonnegative_integer(number, f"output.encoded_evidence.{name}")
    if requested_backend == "native":
        if evidence.get("native_common_contract_summary_count") != 1:
            raise BiomedicalGateError("native compact common-contract summary was not used once")
        if evidence.get("encoded_scalar_traversal_calls") != 0:
            raise BiomedicalGateError("native worker performed scalar structural traversal")
        if evidence.get("encoded_structural_nodes_materialized") != 0:
            raise BiomedicalGateError("native worker materialized structural nodes")

    measurement = _mapping(row["measurement"], "worker result measurement")
    _exact_fields(measurement, _MEASUREMENT_FIELDS, "worker result measurement")
    for name in (
        "load_wall_ns",
        "child_startup_to_ready_cpu_ns",
        "fresh_process_peak_rss_bytes",
        "sample_count",
    ):
        _positive_integer(measurement[name], f"measurement.{name}")
    if measurement["sample_count"] != 1:
        raise BiomedicalGateError("biomedical evidence must remain a single observation")
    if measurement["rss_platform_unit"] not in {"bytes", "kib-converted-to-bytes"}:
        raise BiomedicalGateError("worker RSS platform unit differs")
    if measurement["portable_performance_claim"] is not False:
        raise BiomedicalGateError("single observations cannot claim portable performance")
    return cast(dict[str, Any], row)


def telemetry_row(timings: Mapping[str, float], *, required: bool) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for name in ANONYMOUS_TELEMETRY_NAMES:
        raw = timings.get(name)
        if raw is None:
            if required:
                raise BiomedicalGateError(f"native load report lacks telemetry {name}")
            return None
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(raw)
            or raw < 0
            or raw > MAX_EXACT_FLOAT_INTEGER
            or float(raw) != int(raw)
        ):
            raise BiomedicalGateError(f"native telemetry {name} is not an exact bounded integer")
        values[name] = int(raw)
    return values


def _validate_runtime(runtime: Mapping[str, Any], *, expected_native: object) -> None:
    expected = {
        "python",
        "platform",
        "cpu",
        "memory",
        "package",
        "native",
        "identity_sha256",
    }
    if set(runtime) != expected:
        raise BiomedicalGateError("worker runtime fields differ")
    _digest(runtime["identity_sha256"], "runtime.identity_sha256")
    unsigned = {name: runtime[name] for name in runtime if name != "identity_sha256"}
    if runtime["identity_sha256"] != sha256_json(unsigned):
        raise BiomedicalGateError("worker runtime identity digest differs")
    native = _mapping(runtime["native"], "runtime.native")
    if expected_native is not None:
        if native.get("available") is not True or native.get("sha256") != expected_native:
            raise BiomedicalGateError("worker native artifact differs from the expected extension")
        if native.get("version") != "0.2.0":
            raise BiomedicalGateError("worker native extension version differs")


def _validate_inventories(inventories: Mapping[str, Any], counts: Mapping[str, Any]) -> None:
    for name, raw in inventories.items():
        row = _mapping(raw, f"inventory.{name}")
        if set(row) != {"count", "canonical_bytes", "transcript_bytes", "sha256"}:
            raise BiomedicalGateError(f"inventory {name} fields differ")
        for field_name in ("count", "canonical_bytes", "transcript_bytes"):
            _nonnegative_integer(row[field_name], f"inventory.{name}.{field_name}")
        _digest(row["sha256"], f"inventory.{name}.sha256")
    expected = {
        "ontology_annotations": counts["ontology_annotations"],
        "axioms": counts["axioms"],
        "extensions": counts["extensions"],
        "signature": counts["signature_entities"],
        "documents": counts["documents"],
    }
    if {name: _mapping(inventories[name], name)["count"] for name in expected} != expected:
        raise BiomedicalGateError("worker inventory counts differ from output counts")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BiomedicalGateError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        raise BiomedicalGateError(
            f"{label} fields differ: missing={sorted(expected - observed)!r}, "
            f"unknown={sorted(observed - expected)!r}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BiomedicalGateError(f"{label} must be a nonempty string")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise BiomedicalGateError(f"{label} must be boolean")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BiomedicalGateError(f"{label} must be a nonnegative integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    selected = _nonnegative_integer(value, label)
    if selected < 1:
        raise BiomedicalGateError(f"{label} must be positive")
    return selected


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise BiomedicalGateError(f"{label} must be a lowercase SHA-256")
    return value


__all__ = [
    "ANONYMOUS_TELEMETRY_NAMES",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_STDERR_BYTES",
    "PRIVATE_CASE_IDS",
    "PUBLIC_CASE_IDS",
    "REPORT_SCHEMA",
    "WORKER_REQUEST_SCHEMA",
    "WORKER_RESULT_SCHEMA",
    "BiomedicalGateError",
    "canonical_bytes",
    "corpus_identity",
    "default_limits_row",
    "options_for",
    "options_row",
    "parse_request",
    "sha256_json",
    "telemetry_row",
    "validate_worker_result",
]
